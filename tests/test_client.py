import unittest
import urllib.parse

from tests.support import FakeTransport, StubAuth, json_response

from informdirect import errors
from informdirect.client import Client, extract_items
from informdirect.config import Settings
from informdirect.endpoints import EndpointMap

ENDPOINTS = {
    "_status": "test",
    "pagination": {"style": "auto", "page_param": "page", "page_size_param": "pageSize"},
    "operations": {
        "list_companies": {"method": "GET", "path": "/companies"},
        "get_company": {"method": "GET", "path": "/companies/{company_id}"},
    },
}


def make_client(transport, *, page_size=2, max_retries=3, endpoints=None):
    settings = Settings.load(
        base_url="https://api.example.com/v1", auth_mode="api_key", api_key="k",
        page_size=page_size, max_retries=max_retries, backoff_base=0.0,
    )
    return Client(
        settings,
        transport=transport,
        auth=StubAuth(),
        endpoints=EndpointMap.from_dict(endpoints or ENDPOINTS, source="test"),
        sleep=lambda _: None,
    )


class RequestTest(unittest.TestCase):
    def test_builds_url_and_query(self):
        transport = FakeTransport([json_response({"ok": True})])
        client = make_client(transport)
        client.get("/companies", params={"q": "brown & co", "empty": None})
        url = transport.urls[0]
        self.assertTrue(url.startswith("https://api.example.com/v1/companies?"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["q"], ["brown & co"])
        self.assertNotIn("empty", query)

    def test_maps_status_codes_to_specific_errors(self):
        cases = [
            (400, errors.BadRequestError), (403, errors.ForbiddenError),
            (404, errors.NotFoundError), (422, errors.BadRequestError),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                transport = FakeTransport([json_response({"message": "nope"}, status)])
                with self.assertRaises(expected):
                    make_client(transport).get("/companies")

    def test_error_carries_message_and_request_id(self):
        transport = FakeTransport([
            json_response({"message": "company not found"}, 404,
                          headers={"X-Request-Id": "req-77"})
        ])
        with self.assertRaises(errors.NotFoundError) as ctx:
            make_client(transport).get("/companies/1")
        self.assertIn("company not found", str(ctx.exception))
        self.assertEqual(ctx.exception.request_id, "req-77")

    def test_retries_on_429_then_succeeds(self):
        transport = FakeTransport([
            json_response({"message": "slow down"}, 429, {"Retry-After": "0"}),
            json_response({"ok": True}),
        ])
        client = make_client(transport)
        self.assertTrue(client.get("/companies").json()["ok"])
        self.assertEqual(len(transport.calls), 2)

    def test_gives_up_after_max_retries(self):
        transport = FakeTransport([json_response({}, 503) for _ in range(4)])
        with self.assertRaises(errors.ServerError):
            make_client(transport, max_retries=3).get("/companies")
        self.assertEqual(len(transport.calls), 4)

    def test_does_not_retry_a_400(self):
        transport = FakeTransport([json_response({}, 400)])
        with self.assertRaises(errors.BadRequestError):
            make_client(transport).get("/companies")
        self.assertEqual(len(transport.calls), 1)

    def test_401_refreshes_the_token_exactly_once(self):
        transport = FakeTransport([
            json_response({}, 401),
            json_response({"ok": True}),
        ])
        auth = StubAuth()
        client = make_client(transport)
        client.auth = auth
        client.get("/companies")
        self.assertEqual(auth.invalidations, 1)
        self.assertEqual(transport.calls[0]["headers"]["Authorization"],
                         "Bearer stub-token")
        self.assertEqual(transport.calls[1]["headers"]["Authorization"],
                         "Bearer stub-token+1")

    def test_repeated_401_eventually_raises(self):
        transport = FakeTransport([json_response({}, 401) for _ in range(5)])
        with self.assertRaises(errors.ApiError) as ctx:
            make_client(transport).get("/companies")
        self.assertEqual(ctx.exception.status, 401)

    def test_transport_errors_are_retried_then_reraised(self):
        transport = FakeTransport([errors.TransportError("reset") for _ in range(3)])
        with self.assertRaises(errors.TransportError):
            make_client(transport, max_retries=2).get("/companies")
        self.assertEqual(len(transport.calls), 3)

    def test_call_resolves_a_named_operation(self):
        transport = FakeTransport([json_response({})])
        make_client(transport).call("get_company", path_params={"company_id": "SC 123"})
        self.assertEqual(transport.urls[0],
                         "https://api.example.com/v1/companies/SC%20123")

    def test_unknown_operation_is_a_clear_error(self):
        transport = FakeTransport([])
        with self.assertRaises(errors.EndpointNotConfigured) as ctx:
            make_client(transport).call("list_mortgages")
        self.assertIn("list_companies", str(ctx.exception))


class ExtractItemsTest(unittest.TestCase):
    def test_bare_list(self):
        self.assertEqual(extract_items([{"a": 1}]), [{"a": 1}])

    def test_named_keys(self):
        for key in ("items", "data", "results", "records", "value", "companies"):
            with self.subTest(key=key):
                self.assertEqual(extract_items({key: [1, 2]}), [1, 2])

    def test_sole_list_value_wins_when_key_is_unknown(self):
        self.assertEqual(extract_items({"widgets": [1], "total": 1}), [1])

    def test_ambiguous_shape_yields_nothing(self):
        self.assertEqual(extract_items({"a": [1], "b": [2]}), [])

    def test_non_dict_payloads(self):
        self.assertEqual(extract_items(None), [])
        self.assertEqual(extract_items("text"), [])


class PaginationTest(unittest.TestCase):
    def test_page_and_total_pages(self):
        transport = FakeTransport([
            json_response({"items": [1, 2], "page": 1, "totalPages": 2}),
            json_response({"items": [3], "page": 2, "totalPages": 2}),
        ])
        client = make_client(transport)
        self.assertEqual(list(client.paginate("list_companies")), [1, 2, 3])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(transport.urls[1]).query)
        self.assertEqual(query["page"], ["2"])
        self.assertEqual(query["pageSize"], ["2"])

    def test_total_count(self):
        transport = FakeTransport([
            json_response({"data": [1, 2], "totalCount": 3}),
            json_response({"data": [3], "totalCount": 3}),
        ])
        self.assertEqual(list(make_client(transport).paginate("list_companies")),
                         [1, 2, 3])

    def test_short_page_ends_iteration_without_metadata(self):
        transport = FakeTransport([
            json_response([1, 2]),
            json_response([3]),
        ])
        self.assertEqual(list(make_client(transport).paginate("list_companies")),
                         [1, 2, 3])
        self.assertEqual(len(transport.calls), 2)

    def test_exact_full_last_page_then_empty(self):
        transport = FakeTransport([
            json_response([1, 2]),
            json_response([]),
        ])
        self.assertEqual(list(make_client(transport).paginate("list_companies")),
                         [1, 2])

    def test_next_url_is_followed_verbatim(self):
        transport = FakeTransport([
            json_response({"items": [1, 2],
                           "nextPageUrl": "https://api.example.com/v1/companies?c=xyz"}),
            json_response({"items": [3]}),
        ])
        self.assertEqual(list(make_client(transport).paginate("list_companies")),
                         [1, 2, 3])
        self.assertEqual(transport.urls[1], "https://api.example.com/v1/companies?c=xyz")

    def test_link_header_next(self):
        transport = FakeTransport([
            json_response({"items": [1, 2]}, headers={
                "Link": '<https://api.example.com/v1/companies?page=2>; rel="next"'}),
            json_response({"items": [3]}),
        ])
        self.assertEqual(list(make_client(transport).paginate("list_companies")),
                         [1, 2, 3])
        self.assertEqual(transport.urls[1],
                         "https://api.example.com/v1/companies?page=2")

    def test_cursor_style(self):
        endpoints = dict(ENDPOINTS)
        endpoints["pagination"] = {"style": "cursor", "cursor_param": "cursor"}
        transport = FakeTransport([
            json_response({"items": [1, 2], "nextCursor": "abc"}),
            json_response({"items": [3]}),
        ])
        client = make_client(transport, endpoints=endpoints)
        self.assertEqual(list(client.paginate("list_companies")), [1, 2, 3])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(transport.urls[1]).query)
        self.assertEqual(query["cursor"], ["abc"])

    def test_page_budget_is_enforced(self):
        endpoints = {
            "_status": "test",
            "pagination": {"style": "auto", "max_pages": 3,
                           "page_param": "page", "page_size_param": "pageSize"},
            "operations": {"list_companies": {"method": "GET", "path": "/companies"}},
        }
        counter = iter(range(1000))
        transport = FakeTransport(
            handler=lambda *a: json_response({"items": [next(counter), next(counter)]})
        )
        client = make_client(transport, endpoints=endpoints)
        with self.assertRaises(errors.InformDirectError) as ctx:
            list(client.paginate("list_companies"))
        self.assertIn("max_pages", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()


class RepeatedPageTest(unittest.TestCase):
    def test_api_that_ignores_paging_params_stops_after_one_page(self):
        transport = FakeTransport(
            handler=lambda *a: json_response({"items": [{"n": 1}, {"n": 2}]})
        )
        client = make_client(transport)
        self.assertEqual(list(client.paginate("list_companies")),
                         [{"n": 1}, {"n": 2}])
        self.assertEqual(len(transport.calls), 2)

    def test_genuinely_different_pages_still_continue(self):
        transport = FakeTransport([
            json_response({"items": [{"n": 1}, {"n": 2}]}),
            json_response({"items": [{"n": 3}, {"n": 4}]}),
            json_response({"items": [{"n": 5}]}),
        ])
        client = make_client(transport)
        self.assertEqual([i["n"] for i in client.paginate("list_companies")],
                         [1, 2, 3, 4, 5])
