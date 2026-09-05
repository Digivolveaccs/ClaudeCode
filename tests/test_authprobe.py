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

    def test_all_400_reports_that_no_shape_was_understood(self):
        code, output = self._run(lambda *a: json_response(ASPNET_400, status=400))
        self.assertEqual(code, 2)
        self.assertIn("The apiKey field is required.", output)
        self.assertIn("none of them was understood", output)

    def test_warns_when_no_refresh_token_comes_back(self):
        code, output = self._run(lambda *a: json_response({"accessToken": "a1"}))
        self.assertEqual(code, 0)
        self.assertIn("no refresh token", output)


if __name__ == "__main__":
    unittest.main()


class AnalyseTest(unittest.TestCase):
    """401 vs 400 is the signal: understood-but-refused vs not understood."""

    def _attempts(self, statuses):
        attempts = authprobe.build_attempts(URL, KEY)
        for attempt in attempts:
            attempt.status = statuses.get(attempt.kind, 400)
        return attempts

    def test_a_401_identifies_the_field_even_though_nothing_worked(self):
        result = authprobe.analyse(self._attempts({("json", "apiKey"): 401}))
        self.assertEqual(result["verdict"], "shape_ok_key_refused")
        self.assertEqual(result["shape"].kind, ("json", "apiKey"))

    def test_a_json_shape_is_preferred_over_a_form_one(self):
        result = authprobe.analyse(self._attempts({
            ("json", "apiKey"): 401, ("form", "apiKey"): 401}))
        self.assertEqual(result["shape"].kind[0], "json")

    def test_all_400_means_nothing_was_understood(self):
        result = authprobe.analyse(self._attempts({}))
        self.assertEqual(result["verdict"], "no_shape_understood")
        self.assertIsNone(result["shape"])

    def test_a_real_token_beats_every_status_signal(self):
        attempts = self._attempts({("json", "apiKey"): 401})
        attempts[2].token = "a1"
        result = authprobe.analyse(attempts)
        self.assertEqual(result["verdict"], "working")
        self.assertIs(result["shape"], attempts[2])


class EnvironmentProbeTest(unittest.TestCase):
    def _shape(self):
        return next(a for a in authprobe.build_attempts(URL, KEY)
                    if a.kind == ("json", "apiKey"))

    def test_one_attempt_per_host_and_path_all_using_the_known_shape(self):
        attempts = authprobe.build_environment_attempts(
            KEY, self._shape(),
            hosts=("https://a.example.com", "https://b.example.com"),
            paths=("/authenticate", "/v1/authenticate"))
        self.assertEqual(len(attempts), 4)
        for attempt in attempts:
            self.assertEqual(attempt.kind, ("json", "apiKey"))
            self.assertEqual(json.loads(attempt.body), {"apiKey": KEY})
        self.assertEqual(attempts[0].url, "https://a.example.com/authenticate")
        self.assertEqual(attempts[0].label, "a.example.com/authenticate")

    def test_the_real_candidate_lists_cover_sandbox_naming(self):
        joined = " ".join(authprobe.HOST_CANDIDATES)
        for expected in ("sandbox-api", "api-sandbox", "test-api"):
            self.assertIn(expected, joined)
        self.assertIn("/sandbox/authenticate", authprobe.PATH_CANDIDATES)

    def test_a_host_that_authenticates_is_found(self):
        def handler(method, url, headers=None, body=None):
            if url.startswith("https://sandbox-api."):
                return json_response({"accessToken": "a1", "refreshToken": "r1"})
            return json_response({}, status=401)

        attempts = authprobe.run(
            authprobe.build_environment_attempts(KEY, self._shape()),
            transport=FakeTransport(handler=handler))
        working = [a for a in attempts if a.ok]
        self.assertTrue(working)
        self.assertTrue(all(w.label.startswith("sandbox-api.") for w in working))


class HostProbeCliTest(unittest.TestCase):
    def _run(self, handler, argv=None):
        from unittest import mock

        from informdirect.cli import main
        from informdirect.config import Settings

        settings = Settings.load(base_url="https://api.informdirect.co.uk",
                                 api_key=KEY, cache_tokens=False)
        buffer = io.StringIO()
        with mock.patch("informdirect.cli._settings", return_value=settings):
            with mock.patch.object(authprobe, "UrllibTransport",
                                   lambda: FakeTransport(handler=handler)):
                with redirect_stdout(buffer):
                    code = main(argv or ["authtest"])
        return code, buffer.getvalue()

    def _split_status(self, method, url, headers=None, body=None):
        """Mimic the live API: apiKey understood (401), everything else 400."""
        if body and b'"apiKey"' in body:
            return json_response({}, status=401)
        if body and b"apiKey=" in body:
            return json_response({}, status=401)
        return json_response({}, status=400)

    def test_reports_the_shape_and_moves_on_to_hosts(self):
        code, output = self._run(self._split_status)
        self.assertEqual(code, 2)
        self.assertIn("Request shape identified", output)
        self.assertIn('"api_key_field": "apiKey"', output)
        self.assertIn("likely sandbox hosts", output)
        self.assertIn("support@informdirect.co.uk", output)
        self.assertIn("...123456", output)
        self.assertNotIn(KEY, output)

    def test_no_host_probe_stops_after_the_shape(self):
        code, output = self._run(self._split_status, ["authtest", "--no-host-probe"])
        self.assertEqual(code, 2)
        self.assertIn("Request shape identified", output)
        self.assertNotIn("likely sandbox hosts", output)

    def test_a_sandbox_host_that_works_is_reported(self):
        def handler(method, url, headers=None, body=None):
            if url.startswith("https://sandbox-api.") and body and b'"apiKey"' in body:
                return json_response({"accessToken": "a1", "refreshToken": "r1"})
            return self._split_status(method, url, headers, body)

        code, output = self._run(handler)
        self.assertEqual(code, 0)
        self.assertIn("This one authenticated", output)
        self.assertIn("sandbox-api.informdirect.co.uk", output)


class TwoPhaseProbeTest(unittest.TestCase):
    """A host that does not resolve must cost one attempt, not seven."""

    def _shape(self):
        return next(a for a in authprobe.build_attempts(URL, KEY)
                    if a.kind == ("json", "apiKey"))

    def test_dead_hosts_are_tried_once_each(self):
        calls = []

        def handler(method, url, headers=None, body=None):
            calls.append(url)
            raise OSError("nodename nor servname provided")

        hosts = ("https://a.example.com", "https://b.example.com")
        paths = ("/authenticate", "/v1/authenticate", "/api/authenticate")
        authprobe.probe_environments(
            KEY, self._shape(), hosts=hosts, paths=paths,
            transport=FakeTransport(handler=handler))
        self.assertEqual(len(calls), 2, calls)
        self.assertTrue(all(u.endswith("/authenticate") for u in calls))

    def test_a_live_host_gets_the_remaining_paths(self):
        calls = []

        def handler(method, url, headers=None, body=None):
            calls.append(url)
            if url.startswith("https://b."):
                return json_response({}, status=401)
            raise OSError("no such host")

        hosts = ("https://a.example.com", "https://b.example.com")
        paths = ("/authenticate", "/v1/authenticate")
        authprobe.probe_environments(
            KEY, self._shape(), hosts=hosts, paths=paths,
            transport=FakeTransport(handler=handler))
        self.assertEqual(calls, [
            "https://a.example.com/authenticate",
            "https://b.example.com/authenticate",
            "https://b.example.com/v1/authenticate",
        ])

    def test_probing_stops_the_moment_something_authenticates(self):
        calls = []

        def handler(method, url, headers=None, body=None):
            calls.append(url)
            return json_response({"accessToken": "a1"})

        results = authprobe.probe_environments(
            KEY, self._shape(),
            hosts=("https://a.example.com", "https://b.example.com"),
            paths=("/authenticate", "/v1/authenticate"),
            transport=FakeTransport(handler=handler))
        self.assertEqual(len(calls), 1)
        self.assertTrue(results[-1].ok)

    def test_each_result_is_reported_as_it_lands(self):
        seen = []
        authprobe.probe_environments(
            KEY, self._shape(), hosts=("https://a.example.com",),
            paths=("/authenticate",),
            transport=FakeTransport(handler=lambda *a: json_response({}, 401)),
            on_host=seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].status, 401)
