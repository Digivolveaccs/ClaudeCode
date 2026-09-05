"""Membership reconciliation - the question the live API can actually answer."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.support import FakeTransport, StubAuth, json_response

from informdirect.client import Client
from informdirect.config import Settings
from informdirect.endpoints import EndpointMap
from informdirect.models import Company
from informdirect.portfolio import Portfolio
from informdirect.reconcile import (
    CHARITY, NAME_MISMATCH, NOT_IN_INFORMDIRECT, NOT_IN_PLANNER, OK, UNMATCHED,
    reconcile_membership, rows_from_dicts,
)

# The live envelope, verbatim in shape.
LIVE_ENVELOPE = {"Companies": [
    {"CompanyNumber": "01234567", "Name": "SANDBOX ONE LIMITED",
     "PublicUrl": "/c/abc"},
    {"CompanyNumber": "07654321", "Name": "PMC FLOORS LTD", "PublicUrl": "/c/def"},
]}

ENDPOINTS = {
    "_status": "confirmed",
    "operations": {
        "list_companies": {"method": "GET", "path": "/companies"},
        "get_company": {"method": "GET", "path": "/companies/{company_id}"},
        "add_company": {"method": "POST", "path": "/companies/add"},
    },
}


def api_company(number, name):
    return Company.from_api({"CompanyNumber": number, "Name": name,
                             "PublicUrl": "/c/x"})


def rows(*specs):
    return rows_from_dicts([
        {"Company Number": n, "Company Name": nm, "Notes": note}
        for n, nm, note in specs
    ])


class EnvelopeTest(unittest.TestCase):
    def test_the_live_pascalcase_envelope_parses(self):
        from informdirect.client import extract_items

        items = extract_items(LIVE_ENVELOPE)
        self.assertEqual(len(items), 2)
        company = Company.from_api(items[0])
        self.assertEqual(company.company_number, "01234567")
        self.assertEqual(company.name, "SANDBOX ONE LIMITED")
        # The three fields are all there is - the rest must come back empty.
        self.assertIsNone(company.accounts_due)
        self.assertIsNone(company.accounts_next_made_up_to)
        self.assertEqual(company.status, "")

    def test_a_company_with_no_dates_has_no_deadline_year(self):
        self.assertIsNone(api_company("01234567", "X").next_deadline_year)


class MembershipTest(unittest.TestCase):
    def test_linked_and_agreeing(self):
        result = reconcile_membership(
            [api_company("01234567", "SANDBOX ONE LIMITED")],
            rows(("01234567", "Sandbox One Ltd", "")))
        self.assertEqual([a.kind for a in result.actions], [OK])
        self.assertEqual(result.matched, 1)

    def test_company_suffixes_and_case_do_not_count_as_a_mismatch(self):
        for planner in ("SANDBOX ONE LIMITED", "Sandbox One Ltd",
                        "sandbox one limited", "The Sandbox One Company Ltd"):
            with self.subTest(planner=planner):
                result = reconcile_membership(
                    [api_company("01234567", "SANDBOX ONE LIMITED")],
                    rows(("01234567", planner, "")))
                self.assertEqual([a.kind for a in result.actions], [OK], planner)

    def test_a_genuinely_different_name_is_flagged(self):
        result = reconcile_membership(
            [api_company("01234567", "SANDBOX ONE LIMITED")],
            rows(("01234567", "Completely Different Ltd", "")))
        action = result.of_kind(NAME_MISMATCH)[0]
        self.assertEqual(action.current["planner"], "Completely Different Ltd")
        self.assertEqual(action.proposed["inform_direct"], "SANDBOX ONE LIMITED")

    def test_planner_company_missing_from_the_portfolio(self):
        result = reconcile_membership([], rows(("09999999", "New Client Ltd", "")))
        action = result.of_kind(NOT_IN_INFORMDIRECT)[0]
        self.assertEqual(action.company_number, "09999999")
        self.assertIn("--add", action.reason)
        self.assertTrue(action.needs_human)

    def test_portfolio_company_missing_from_the_planner(self):
        result = reconcile_membership(
            [api_company("01234567", "SANDBOX ONE LIMITED")], [])
        self.assertEqual([a.kind for a in result.actions], [NOT_IN_PLANNER])

    def test_charities_are_never_expected_in_inform_direct(self):
        result = reconcile_membership([], rows(("", "Helping Hands", "Charity 1122334")))
        self.assertEqual([a.kind for a in result.actions], [CHARITY])

    def test_a_row_with_no_number_cannot_be_matched(self):
        result = reconcile_membership([], rows(("", "Mystery Co", "")))
        self.assertEqual([a.kind for a in result.actions], [UNMATCHED])

    def test_numbers_are_normalised_on_both_sides(self):
        result = reconcile_membership(
            [api_company("1234567", "SANDBOX ONE LIMITED")],
            rows(("01234567", "Sandbox One Ltd", "")))
        self.assertEqual([a.kind for a in result.actions], [OK])


class MembershipCliTest(unittest.TestCase):
    def _portfolio(self, added=None):
        def handler(method, url, headers=None, body=None):
            if method == "POST":
                if added is not None:
                    added.append(json.loads(body))
                return json_response({"Companies": [{"CompanyNumber": "09999999",
                                                     "Name": "NEW CLIENT LTD"}]})
            return json_response(LIVE_ENVELOPE)

        settings = Settings.load(base_url="https://sandbox-api.example.com",
                                 auth_mode="api_key", api_key="k",
                                 backoff_base=0.0)
        client = Client(settings, transport=FakeTransport(handler=handler),
                        auth=StubAuth(),
                        endpoints=EndpointMap.from_dict(ENDPOINTS, source="test"),
                        sleep=lambda _: None)
        return Portfolio(client=client)

    def _run(self, planner_csv, argv_extra=(), added=None):
        from unittest import mock

        from informdirect.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            planner = Path(tmp) / "planner.csv"
            planner.write_text(planner_csv)
            buffer = io.StringIO()
            with mock.patch("informdirect.cli._portfolio",
                            return_value=self._portfolio(added)):
                with mock.patch("informdirect.cli.ADD_COMPANY_PAUSE", 0):
                    with redirect_stdout(buffer):
                        code = main(["membership", "--planner", str(planner),
                                     *argv_extra])
            return code, buffer.getvalue()

    PLANNER = ("Company Number,Company Name,Notes\n"
               "01234567,Sandbox One Ltd,\n"
               "09999999,New Client Ltd,\n")

    def test_reports_both_directions(self):
        code, output = self._run(self.PLANNER)
        self.assertEqual(code, 2)
        self.assertIn("not_in_informdirect", output)
        self.assertIn("09999999", output)
        self.assertIn("not_in_planner", output)
        self.assertIn("07654321", output)

    def test_add_without_confirm_only_says_what_it_would_do(self):
        added = []
        code, output = self._run(self.PLANNER, ["--add"], added)
        self.assertIn("would be linked", output)
        self.assertEqual(added, [])

    def test_add_with_confirm_links_the_missing_companies(self):
        added = []
        code, output = self._run(self.PLANNER, ["--add", "--confirm"], added)
        self.assertEqual(added, [{"CompanyNumber": "09999999"}],
                         "must send the confirmed field name")
        self.assertIn("added     09999999", output)

    def test_limit_caps_how_many_are_added(self):
        planner = "Company Number,Company Name,Notes\n" + "".join(
            f"0999999{i},Client {i},\n" for i in range(5))
        added = []
        code, output = self._run(planner, ["--add", "--confirm", "--limit", "2"],
                                 added)
        self.assertEqual(len(added), 2)

    def test_json_output_is_machine_readable(self):
        code, output = self._run(self.PLANNER, ["--json"])
        payload = json.loads(output)
        self.assertEqual(payload["summary"]["in_inform_direct"], 2)
        self.assertEqual(payload["summary"]["planner_rows"], 2)
        self.assertIn("not_in_informdirect", payload["summary"]["counts"])


if __name__ == "__main__":
    unittest.main()


class DetailEnvelopeTest(unittest.TestCase):
    """GET /companies/{n} returns the list envelope, so it must be unwrapped."""

    def _portfolio(self, payload):
        settings = Settings.load(base_url="https://sandbox-api.example.com",
                                 auth_mode="api_key", api_key="k", backoff_base=0.0)
        client = Client(settings,
                        transport=FakeTransport(handler=lambda *a: json_response(payload)),
                        auth=StubAuth(),
                        endpoints=EndpointMap.from_dict(ENDPOINTS, source="test"),
                        sleep=lambda _: None)
        return Portfolio(client=client)

    def test_detail_fetch_unwraps_the_companies_array(self):
        company = self._portfolio(LIVE_ENVELOPE).get_company("01234567")
        self.assertIsNotNone(company)
        self.assertEqual(company.company_number, "01234567")
        self.assertEqual(company.name, "SANDBOX ONE LIMITED")

    def test_an_empty_envelope_yields_no_company(self):
        self.assertIsNone(self._portfolio({"Companies": []}).get_company("01234567"))

    def test_a_bare_record_still_works(self):
        company = self._portfolio(
            {"CompanyNumber": "01234567", "Name": "X LTD"}).get_company("01234567")
        self.assertEqual(company.name, "X LTD")

    def test_add_company_unwraps_its_response_too(self):
        company = self._portfolio(LIVE_ENVELOPE).add_company("1234567")
        self.assertEqual(company.company_number, "01234567")

    def test_add_company_sends_the_padded_number_under_the_confirmed_field(self):
        sent = []

        def handler(method, url, headers=None, body=None):
            if body:
                sent.append(json.loads(body))
            return json_response(LIVE_ENVELOPE)

        settings = Settings.load(base_url="https://sandbox-api.example.com",
                                 auth_mode="api_key", api_key="k", backoff_base=0.0)
        client = Client(settings, transport=FakeTransport(handler=handler),
                        auth=StubAuth(),
                        endpoints=EndpointMap.from_dict(ENDPOINTS, source="test"),
                        sleep=lambda _: None)
        Portfolio(client=client).add_company("1234567")
        self.assertEqual(sent, [{"CompanyNumber": "01234567"}])


class AddCompanyBehaviourTest(unittest.TestCase):
    """Behaviours confirmed live: 201 created, 422 already linked, 404 unknown."""

    def _run(self, handler, argv_extra=()):
        from unittest import mock

        from informdirect.cli import main

        settings = Settings.load(base_url="https://sandbox-api.example.com",
                                 auth_mode="api_key", api_key="k", backoff_base=0.0)
        client = Client(settings, transport=FakeTransport(handler=handler),
                        auth=StubAuth(),
                        endpoints=EndpointMap.from_dict(ENDPOINTS, source="test"),
                        sleep=lambda _: None)
        portfolio = Portfolio(client=client)
        with tempfile.TemporaryDirectory() as tmp:
            planner = Path(tmp) / "p.csv"
            planner.write_text("Company Number,Company Name\n"
                               "05251849,Adoreum Ltd\n")
            buffer = io.StringIO()
            with mock.patch("informdirect.cli._portfolio", return_value=portfolio):
                with mock.patch("informdirect.cli.ADD_COMPANY_PAUSE", 0):
                    with redirect_stdout(buffer):
                        main(["membership", "--planner", str(planner), "--add",
                              "--confirm", *argv_extra])
            return buffer.getvalue()

    def _handler(self, post_status, post_body):
        def handler(method, url, headers=None, body=None):
            if method == "POST":
                return json_response(post_body, status=post_status)
            return json_response({"Companies": []})
        return handler

    def test_201_counts_as_added(self):
        out = self._run(self._handler(
            201, {"Message": "Company added with no authentication code."}))
        self.assertIn("added     05251849", out)
        self.assertIn("added 1, already linked 0, failed 0", out)

    def test_422_already_associated_is_not_a_failure(self):
        out = self._run(self._handler(
            422, {"Message": "Company already associated with this account."}))
        self.assertIn("already   05251849", out)
        self.assertIn("added 0, already linked 1, failed 0", out)

    def test_404_unknown_company_is_reported_plainly(self):
        out = self._run(self._handler(404, {"Message": "Company could not be found."}))
        self.assertIn("NOT FOUND", out)
        self.assertIn("Companies House does not know", out)

    def test_429_stops_the_run(self):
        out = self._run(self._handler(
            429, {"Message": "This end point is not meant for bulk uploading"}))
        self.assertIn("RATE LIMIT", out)
        self.assertIn("smaller --limit", out)

    def test_adding_without_an_auth_code_says_so(self):
        out = self._run(self._handler(201, {"Message": "added"}))
        self.assertIn("authentication code", out)

    def test_the_auth_code_is_sent_when_given(self):
        sent = []

        def handler(method, url, headers=None, body=None):
            if method == "POST":
                sent.append(json.loads(body))
                return json_response({"Message": "ok"}, status=201)
            return json_response({"Companies": []})

        out = self._run(handler, ["--auth-code", "AB12CD"])
        self.assertEqual(sent, [{"CompanyNumber": "05251849",
                                 "AuthenticationCode": "AB12CD"}])
        self.assertNotIn("without a Companies House", out)


class AlreadyLinkedMappingTest(unittest.TestCase):
    def test_422_maps_to_already_linked_only_for_that_message(self):
        from informdirect import errors

        linked = errors.from_response(
            422, "Company already associated with this account.")
        self.assertIsInstance(linked, errors.AlreadyLinkedError)

        other = errors.from_response(422, "Some other validation problem")
        self.assertIsInstance(other, errors.BadRequestError)
        self.assertNotIsInstance(other, errors.AlreadyLinkedError)
