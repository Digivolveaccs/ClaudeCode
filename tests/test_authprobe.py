import io
import json
import unittest
import urllib.parse
from contextlib import redirect_stdout

from tests.support import FakeTransport, json_response

from informdirect import authprobe
from informdirect.transport import HttpResponse

KEY = "sandbox-key-123456"
URL = "https://api.example.com/authenticate"

ASPNET_400 = {
    "type": "https://tools.ietf.org/html/rfc7231#section-6.5.1",
    "title": "One or more validation errors occurred.",
    "status": 400,
    "errors": {"apiKey": ["The apiKey field is required."]},
}


class BuildAttemptsTest(unittest.TestCase):
    def test_covers_body_form_header_and_bearer_shapes(self):
        attempts = authprobe.build_attempts(URL, KEY)
        kinds = {a.kind[0] for a in attempts}
        self.assertEqual(kinds, {"json", "form", "header", "bearer", "raw", "query"})

    def test_json_bodies_carry_the_key_under_each_candidate_field(self):
        attempts = [a for a in authprobe.build_attempts(URL, KEY)
                    if a.kind[0] == "json"]
        for attempt in attempts:
            self.assertEqual(json.loads(attempt.body), {attempt.kind[1]: KEY})
            self.assertEqual(attempt.headers["Content-Type"], "application/json")

    def test_header_shapes_send_no_body(self):
        for attempt in authprobe.build_attempts(URL, KEY):
            if attempt.kind[0] == "header":
                self.assertIsNone(attempt.body)
                self.assertEqual(attempt.headers[attempt.kind[1]], KEY)

    def test_bearer_shape_prefixes_the_key(self):
        bearer = next(a for a in authprobe.build_attempts(URL, KEY)
                      if a.kind[0] == "bearer")
        self.assertEqual(bearer.headers["Authorization"], f"Bearer {KEY}")

    def test_query_shape_appends_the_key(self):
        query = next(a for a in authprobe.build_attempts(URL, KEY)
                     if a.kind[0] == "query")
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(query.url).query)
        self.assertEqual(parsed["apiKey"], [KEY])

    def test_every_attempt_posts(self):
        self.assertTrue(all(a.method == "POST"
                            for a in authprobe.build_attempts(URL, KEY)))


class RunTest(unittest.TestCase):
    def test_aspnet_validation_errors_are_surfaced_with_the_field_name(self):
        transport = FakeTransport(
            handler=lambda *a: json_response(ASPNET_400, status=400))
        results = authprobe.run(authprobe.build_attempts(URL, KEY),
                                transport=transport)
        self.assertTrue(all(not r.ok for r in results))
        self.assertIn("apiKey: The apiKey field is required.", results[0].detail)
        self.assertIn("One or more validation errors", results[0].detail)

    def test_a_working_shape_is_detected_and_its_tokens_read(self):
        def handler(method, url, headers=None, body=None):
            if body and b'"api_key"' in body:
                return json_response({"accessToken": "a1", "refreshToken": "r1"})
            return json_response(ASPNET_400, status=400)

        results = authprobe.run(authprobe.build_attempts(URL, KEY),
                                transport=FakeTransport(handler=handler))
        winners = [r for r in results if r.ok]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].kind, ("json", "api_key"))
        self.assertEqual(winners[0].token, "a1")
        self.assertEqual(winners[0].refresh, "r1")
        self.assertIn('"api_key_field": "api_key"', winners[0].settings_hint())

    def test_header_winner_reports_the_header_name(self):
        def handler(method, url, headers=None, body=None):
            if (headers or {}).get("Ocp-Apim-Subscription-Key"):
                return json_response({"token": "a1"})
            return json_response({}, status=400)

        results = authprobe.run(authprobe.build_attempts(URL, KEY),
                                transport=FakeTransport(handler=handler))
        winner = next(r for r in results if r.ok)
        self.assertIn('"api_key_header": "Ocp-Apim-Subscription-Key"',
                      winner.settings_hint())

    def test_transport_failures_are_recorded_not_raised(self):
        def handler(*a, **kw):
            raise OSError("connection reset")

        results = authprobe.run(authprobe.build_attempts(URL, KEY),
                                transport=FakeTransport(handler=handler))
        self.assertTrue(all(r.error for r in results))
        self.assertIsNone(results[0].status)

    def test_empty_and_non_json_bodies_are_described(self):
        transport = FakeTransport(
            handler=lambda *a: HttpResponse(status=500, headers={}, body=b""))
        results = authprobe.run(authprobe.build_attempts(URL, KEY),
                                transport=transport)
        self.assertEqual(results[0].detail, "(empty body)")

    def test_a_token_nested_in_a_data_envelope_is_found(self):
        transport = FakeTransport(
            handler=lambda *a: json_response({"data": {"accessToken": "a1"}}))
        results = authprobe.run(authprobe.build_attempts(URL, KEY),
                                transport=transport)
        self.assertEqual(results[0].token, "a1")


class CliTest(unittest.TestCase):
    def _run(self, handler):
        from unittest import mock

        from informdirect.cli import main
        from informdirect.config import Settings

        settings = Settings.load(base_url="https://api.example.com", api_key=KEY,
                                 cache_tokens=False)
        buffer = io.StringIO()
        # Swap the transport authprobe.run builds for itself, rather than the
        # function under test.
        with mock.patch("informdirect.cli._settings", return_value=settings):
            with mock.patch.object(authprobe, "UrllibTransport",
                                   lambda: FakeTransport(handler=handler)):
                with redirect_stdout(buffer):
                    code = main(["authtest"])
        return code, buffer.getvalue()

    def test_reports_the_winning_shape_and_what_to_configure(self):
        def handler(method, url, headers=None, body=None):
            if body and b'"apiKey"' in body:
                return json_response({"accessToken": "a1", "refreshToken": "r1"})
            return json_response(ASPNET_400, status=400)

        code, output = self._run(handler)
        self.assertEqual(code, 0)
        self.assertIn("WORKS", output)
        self.assertIn('"api_key_field": "apiKey"', output)
        self.assertNotIn(KEY, output, "the key must not be echoed in full")
        self.assertIn("...123456", output)

    def test_all_failing_points_at_the_validation_messages(self):
        code, output = self._run(lambda *a: json_response(ASPNET_400, status=400))
        self.assertEqual(code, 2)
        self.assertIn("The apiKey field is required.", output)
        self.assertIn("Nothing returned a token", output)

    def test_warns_when_no_refresh_token_comes_back(self):
        code, output = self._run(lambda *a: json_response({"accessToken": "a1"}))
        self.assertEqual(code, 0)
        self.assertIn("no refresh token", output)


if __name__ == "__main__":
    unittest.main()
