import base64
import tempfile
import unittest
import urllib.parse
from pathlib import Path

from tests.support import FakeTransport, json_response

from informdirect.auth import OAuth2ClientCredentials, TokenCache, build_auth
from informdirect.config import Settings
from informdirect.errors import AuthError


def oauth_settings(**kw):
    base = dict(
        base_url="https://api.example.com/v1",
        token_url="https://auth.example.com/oauth2/token",
        client_id="cid", client_secret="csecret", cache_tokens=False,
    )
    base.update(kw)
    return Settings.load(**base)


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class OAuthTest(unittest.TestCase):
    def test_basic_style_sends_authorization_header(self):
        transport = FakeTransport([json_response({"access_token": "t1",
                                                  "expires_in": 3600})])
        auth = OAuth2ClientCredentials(oauth_settings(token_auth_style="basic"),
                                       transport=transport, clock=Clock())
        self.assertEqual(auth.token(), "t1")
        header = transport.calls[0]["headers"]["Authorization"]
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        self.assertEqual(decoded, "cid:csecret")
        body = urllib.parse.parse_qs(transport.calls[0]["body"].decode())
        self.assertNotIn("client_secret", body)

    def test_body_style_sends_credentials_in_the_form(self):
        transport = FakeTransport([json_response({"access_token": "t1"})])
        auth = OAuth2ClientCredentials(oauth_settings(token_auth_style="body"),
                                       transport=transport, clock=Clock())
        auth.token()
        body = urllib.parse.parse_qs(transport.calls[0]["body"].decode())
        self.assertEqual(body["client_secret"], ["csecret"])
        self.assertNotIn("Authorization", transport.calls[0]["headers"])

    def test_auto_falls_back_from_basic_to_body(self):
        transport = FakeTransport([
            json_response({"error": "invalid_client"}, status=401),
            json_response({"access_token": "t2", "expires_in": 900}),
        ])
        auth = OAuth2ClientCredentials(oauth_settings(token_auth_style="auto"),
                                       transport=transport, clock=Clock())
        self.assertEqual(auth.token(), "t2")
        self.assertEqual(len(transport.calls), 2)
        # The working style is remembered, so the next refresh is a single call.
        auth.invalidate()
        transport.queue.append(json_response({"access_token": "t3"}))
        self.assertEqual(auth.token(), "t3")
        self.assertEqual(len(transport.calls), 3)

    def test_auto_reports_the_last_failure_when_both_styles_fail(self):
        transport = FakeTransport([
            json_response({"error_description": "bad basic"}, status=401),
            json_response({"error_description": "bad body"}, status=401),
        ])
        auth = OAuth2ClientCredentials(oauth_settings(), transport=transport,
                                       clock=Clock())
        with self.assertRaises(AuthError) as ctx:
            auth.token()
        self.assertIn("bad body", str(ctx.exception))

    def test_token_is_reused_until_it_nears_expiry(self):
        clock = Clock()
        transport = FakeTransport([
            json_response({"access_token": "t1", "expires_in": 300}),
            json_response({"access_token": "t2", "expires_in": 300}),
        ])
        auth = OAuth2ClientCredentials(oauth_settings(token_auth_style="basic"),
                                       transport=transport, clock=clock)
        self.assertEqual(auth.token(), "t1")
        clock.now += 100
        self.assertEqual(auth.token(), "t1")
        self.assertEqual(len(transport.calls), 1)
        clock.now += 145  # 245s in: past 300 - 60 skew
        self.assertEqual(auth.token(), "t2")

    def test_scope_and_audience_are_forwarded(self):
        transport = FakeTransport([json_response({"access_token": "t"})])
        auth = OAuth2ClientCredentials(
            oauth_settings(token_auth_style="basic", scope="portfolio.read",
                           audience="https://api.example.com"),
            transport=transport, clock=Clock())
        auth.token()
        body = urllib.parse.parse_qs(transport.calls[0]["body"].decode())
        self.assertEqual(body["scope"], ["portfolio.read"])
        self.assertEqual(body["audience"], ["https://api.example.com"])

    def test_missing_access_token_is_an_auth_error(self):
        transport = FakeTransport([json_response({"token": "wrong-key"})])
        auth = OAuth2ClientCredentials(oauth_settings(token_auth_style="basic"),
                                       transport=transport, clock=Clock())
        with self.assertRaises(AuthError) as ctx:
            auth.token()
        self.assertIn("access_token", str(ctx.exception))

    def test_non_json_token_body_is_an_auth_error(self):
        from informdirect.transport import HttpResponse
        transport = FakeTransport([HttpResponse(status=200, headers={}, body=b"<html>")])
        auth = OAuth2ClientCredentials(oauth_settings(token_auth_style="basic"),
                                       transport=transport, clock=Clock())
        with self.assertRaises(AuthError):
            auth.token()


class TokenCacheTest(unittest.TestCase):
    def test_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = TokenCache(Path(tmp) / "cache.json", key="k")
            self.assertIsNone(cache.read(now=0))
            cache.write("tok", expires_at=500)
            self.assertEqual(cache.read(now=100), ("tok", 500.0))
            self.assertIsNone(cache.read(now=500))
            cache.clear()
            self.assertIsNone(cache.read(now=100))

    def test_cache_is_keyed_by_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            TokenCache(path, key="a").write("tok-a", 500)
            TokenCache(path, key="b").write("tok-b", 500)
            self.assertEqual(TokenCache(path, "a").read(0)[0], "tok-a")
            self.assertEqual(TokenCache(path, "b").read(0)[0], "tok-b")

    def test_unwritable_cache_does_not_raise(self):
        cache = TokenCache("/proc/definitely/not/writable/cache.json", key="k")
        cache.write("tok", 500)  # must not raise
        self.assertIsNone(cache.read(now=0))

    def test_cached_token_is_used_instead_of_a_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.json"
            settings = oauth_settings(cache_tokens=True, token_cache_path=str(path))
            transport = FakeTransport([json_response({"access_token": "t1",
                                                      "expires_in": 3600})])
            clock = Clock()
            first = OAuth2ClientCredentials(settings, transport=transport, clock=clock)
            self.assertEqual(first.token(), "t1")

            second = OAuth2ClientCredentials(
                settings, transport=FakeTransport([]), clock=clock)
            self.assertEqual(second.token(), "t1")


class BuildAuthTest(unittest.TestCase):
    def test_api_key_mode(self):
        settings = Settings.load(base_url="https://x", auth_mode="api_key",
                                 api_key="abc", api_key_header="X-ID-Key")
        auth = build_auth(settings)
        headers = auth.apply({})
        self.assertEqual(headers["X-ID-Key"], "abc")
        self.assertFalse(auth.invalidate())


if __name__ == "__main__":
    unittest.main()
