"""Authentication strategies.

Inform Direct's documented flow is `api_key_token` and it is the default: you
POST your API key to the authentication endpoint, get back a short-lived access
token (15 minutes) and a refresh token, and call /refresh when a request comes
back 401. Refresh returns a *new* refresh token as well as a new access token,
so the stored one is rotated each time.

Two others are available in case the account is provisioned differently:

  api_key  - a static key sent in `api_key_header` on every request
  oauth2   - RFC 6749 client-credentials grant against `token_url`

All three expose the same surface: `apply(headers)` and `invalidate()`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import threading
import time
import urllib.parse
from pathlib import Path

from .errors import AuthError
from .transport import UrllibTransport

# Refresh this many seconds before the token actually expires.
EXPIRY_SKEW = 60.0


class ApiKeyAuth:
    def __init__(self, settings):
        self._header = settings.api_key_header
        self._key = settings.api_key

    def apply(self, headers):
        headers[self._header] = self._key
        return headers

    def invalidate(self):
        """Nothing to invalidate - a static key is either right or wrong."""
        return False

    def describe(self):
        return f"api_key header={self._header}"


class OAuth2ClientCredentials:
    def __init__(self, settings, transport=None, cache=None, clock=time.time):
        self._settings = settings
        self._transport = transport or UrllibTransport()
        self._clock = clock
        self._lock = threading.Lock()
        self._token = None
        self._expires_at = 0.0
        self._style = settings.token_auth_style
        self._cache = cache
        if self._cache is None and settings.cache_tokens:
            self._cache = TokenCache(settings.token_cache_path, self._cache_key())

    # -- public ---------------------------------------------------------- #

    def apply(self, headers):
        headers["Authorization"] = "Bearer " + self.token()
        return headers

    def token(self):
        with self._lock:
            if self._token and self._clock() < self._expires_at:
                return self._token
            if self._cache is not None:
                cached = self._cache.read(self._clock())
                if cached:
                    self._token, self._expires_at = cached
                    return self._token
            self._refresh_locked()
            return self._token

    def invalidate(self):
        """Drop the current token so the next call fetches a fresh one."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
            if self._cache is not None:
                self._cache.clear()
        return True

    def describe(self):
        return f"oauth2 client_credentials token_url={self._settings.token_url}"

    # -- internals -------------------------------------------------------- #

    def _cache_key(self):
        s = self._settings
        return "|".join([s.token_url, s.client_id, s.scope or "", s.audience or ""])

    def _refresh_locked(self):
        styles = ["basic", "body"] if self._style == "auto" else [self._style]
        last_error = None
        for style in styles:
            try:
                token, expires_in = self._request_token(style)
            except AuthError as exc:
                last_error = exc
                continue
            self._token = token
            self._expires_at = self._clock() + max(expires_in - EXPIRY_SKEW, 0.0)
            if self._style == "auto":
                # Remember what worked so we stop paying for the failed attempt.
                self._style = style
            if self._cache is not None:
                self._cache.write(token, self._expires_at)
            return
        raise last_error or AuthError("could not obtain an access token")

    def _request_token(self, style):
        settings = self._settings
        form = {"grant_type": "client_credentials"}
        if settings.scope:
            form["scope"] = settings.scope
        if settings.audience:
            form["audience"] = settings.audience

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": settings.user_agent,
        }
        if style == "basic":
            raw = f"{settings.client_id}:{settings.client_secret}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
        else:
            form["client_id"] = settings.client_id
            form["client_secret"] = settings.client_secret

        body = urllib.parse.urlencode(form).encode("utf-8")
        response = self._transport.send(
            "POST", settings.token_url, headers=headers, body=body,
            timeout=settings.timeout,
        )

        if not response.ok:
            raise AuthError(
                f"token request failed ({style} credentials): HTTP {response.status} "
                f"{_error_detail(response)}"
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise AuthError(
                f"token endpoint returned a non-JSON body: {response.text[:200]!r}"
            )
        token = payload.get("access_token")
        if not token:
            raise AuthError(
                "token endpoint response had no access_token (keys: "
                + ", ".join(sorted(payload)) + ")"
            )
        try:
            expires_in = float(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            expires_in = 3600.0
        return token, expires_in


def _error_detail(response):
    """The most specific thing the server said about why it refused.

    ASP.NET Core answers a bad request with RFC 7807 ProblemDetails - a `title`
    plus an `errors` map naming each offending field. That map is the single
    most useful thing when the request shape is still unconfirmed, so it is
    surfaced rather than dropped.
    """
    payload = response.json()
    if isinstance(payload, dict):
        for key in ("error_description", "error", "message", "detail"):
            if payload.get(key):
                return str(payload[key])
        errors = payload.get("errors")
        if isinstance(errors, dict) and errors:
            parts = []
            for field, problems in list(errors.items())[:5]:
                if isinstance(problems, (list, tuple)):
                    problems = "; ".join(str(x) for x in problems)
                parts.append(f"{field}: {problems}")
            title = payload.get("title") or "validation failed"
            return f"{title} ({'; '.join(parts)})"
        if payload.get("title"):
            return str(payload["title"])
    text = response.text.strip()
    return text[:300] if text else "(empty response body)"


class TokenCache:
    """Access tokens on disk, keyed by credentials, mode 0600.

    Saves a token round-trip on every CLI invocation. Set `cache_tokens: false`
    if you would rather nothing touched the filesystem.
    """

    def __init__(self, path, key):
        self.path = Path(path).expanduser()
        self.key = key

    def read(self, now):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        entry = data.get(self.key) if isinstance(data, dict) else None
        if not isinstance(entry, dict):
            return None
        token = entry.get("access_token")
        expires_at = entry.get("expires_at")
        if not token or not isinstance(expires_at, (int, float)):
            return None
        if now >= expires_at:
            return None
        return token, float(expires_at)

    def write(self, token, expires_at):
        data = self._read_all()
        data[self.key] = {"access_token": token, "expires_at": expires_at}
        self._write_all(data)

    def read_tokens(self, now):
        """(access_token or None, refresh_token or None, expires_at).

        The access token is dropped once expired, but the refresh token is still
        returned - that is the whole point of holding it.
        """
        data = self._read_all()
        entry = data.get(self.key)
        if not isinstance(entry, dict):
            return None
        refresh = entry.get("refresh_token") or None
        expires_at = entry.get("expires_at")
        access = entry.get("access_token") or None
        if not isinstance(expires_at, (int, float)) or now >= expires_at:
            access, expires_at = None, 0.0
        if not access and not refresh:
            return None
        return access, refresh, float(expires_at)

    def write_tokens(self, access, refresh, expires_at):
        data = self._read_all()
        data[self.key] = {
            "access_token": access,
            "refresh_token": refresh,
            "expires_at": expires_at,
        }
        self._write_all(data)

    def clear(self):
        data = self._read_all()
        if data.pop(self.key, None) is not None:
            self._write_all(data)

    def _read_all(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_all(self, data):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data))
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self.path)
        except OSError:
            # A cache that cannot be written is a performance problem, not a
            # correctness one - carry on with an in-memory token.
            pass


class ApiKeyTokenAuth:
    """Inform Direct's documented flow: API key -> access token + refresh token.

    The access token lasts 15 minutes. When it expires we try /refresh first and
    fall back to re-authenticating with the API key, so a rotated-out or revoked
    refresh token self-heals rather than wedging the client.
    """

    def __init__(self, settings, transport=None, cache=None, clock=time.time):
        self._settings = settings
        self._transport = transport or UrllibTransport()
        self._clock = clock
        self._lock = threading.Lock()
        self._access = None
        self._refresh = None
        self._expires_at = 0.0
        self._key_in = settings.api_key_in
        self._cache = cache
        if self._cache is None and settings.cache_tokens:
            self._cache = TokenCache(settings.token_cache_path, self._cache_key())

    # -- public ---------------------------------------------------------- #

    def apply(self, headers):
        headers["Authorization"] = "Bearer " + self.token()
        return headers

    def token(self):
        with self._lock:
            if self._access and self._clock() < self._expires_at:
                return self._access
            if self._cache is not None:
                cached = self._cache.read_tokens(self._clock())
                if cached:
                    self._access, self._refresh, self._expires_at = cached
                    if self._access:
                        return self._access
            self._obtain_locked()
            return self._access

    def invalidate(self):
        """Drop the access token but keep the refresh token for the next call."""
        with self._lock:
            self._access = None
            self._expires_at = 0.0
        return True

    def describe(self):
        return f"api_key_token auth_url={self._settings.resolved_auth_url()}"

    # -- internals -------------------------------------------------------- #

    def _cache_key(self):
        s = self._settings
        digest = hashlib.sha256(s.api_key.encode("utf-8")).hexdigest()[:16]
        return f"api_key_token|{s.resolved_auth_url()}|{digest}"

    def _obtain_locked(self):
        if self._refresh:
            try:
                self._store(self._refresh_tokens())
                return
            except AuthError:
                # Refresh tokens rotate and can be revoked; fall back to the key.
                self._refresh = None
        self._store(self._authenticate())

    def _store(self, parsed):
        access, refresh, expires_in = parsed
        self._access = access
        if refresh:
            self._refresh = refresh
        self._expires_at = self._clock() + max(expires_in - EXPIRY_SKEW, 0.0)
        if self._cache is not None:
            self._cache.write_tokens(self._access, self._refresh, self._expires_at)

    def _authenticate(self):
        settings = self._settings
        styles = ["body", "header"] if self._key_in == "auto" else [self._key_in]
        attempts = []
        for style in styles:
            headers = {"Accept": "application/json",
                       "User-Agent": settings.user_agent}
            body = None
            if style == "header":
                headers[settings.api_key_header] = settings.api_key
            else:
                headers["Content-Type"] = "application/json"
                body = json.dumps({settings.api_key_field: settings.api_key}).encode()
            response = self._transport.send(
                "POST", settings.resolved_auth_url(), headers=headers, body=body,
                timeout=settings.timeout,
            )
            if response.ok:
                if self._key_in == "auto":
                    self._key_in = style
                return self._parse_tokens(response, "authentication")
            attempts.append(
                f"api key in {style}: HTTP {response.status} "
                f"{_error_detail(response)}"
            )
        raise AuthError(
            "authentication failed. Each request shape tried:\n  "
            + "\n  ".join(attempts)
            + "\n\nRun `python3 -m informdirect authtest` to probe every "
              "plausible shape and see the full response to each."
        )

    def _refresh_tokens(self):
        settings = self._settings
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": settings.user_agent,
        }
        body = json.dumps({settings.refresh_token_field: self._refresh}).encode()
        response = self._transport.send(
            "POST", settings.resolved_refresh_url(), headers=headers, body=body,
            timeout=settings.timeout,
        )
        if not response.ok:
            raise AuthError(
                f"refresh failed: HTTP {response.status} {_error_detail(response)}"
            )
        return self._parse_tokens(response, "refresh")

    def _parse_tokens(self, response, what):
        payload = response.json()
        if not isinstance(payload, dict):
            raise AuthError(
                f"{what} endpoint returned a non-JSON body: {response.text[:200]!r}"
            )
        flat = {_norm(k): v for k, v in _walk(payload)}
        access = _first_of(flat, ("accesstoken", "token", "jwt", "idtoken"))
        if not access:
            raise AuthError(
                f"{what} response had no access token (keys: "
                + ", ".join(sorted(payload)) + ")"
            )
        refresh = _first_of(flat, ("refreshtoken", "refresh"))
        expires = _first_of(flat, ("expiresin", "expiresinseconds", "ttl"))
        try:
            expires_in = float(expires)
        except (TypeError, ValueError):
            expires_in = float(self._settings.access_token_ttl)
        return str(access), (str(refresh) if refresh else None), expires_in


def _walk(payload, depth=0):
    """Yield (key, value) for scalars at any depth - token may be nested."""
    if not isinstance(payload, dict) or depth > 3:
        return
    for key, value in payload.items():
        if isinstance(value, dict):
            yield from _walk(value, depth + 1)
        else:
            yield key, value


def _norm(value):
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _first_of(flat, names):
    for name in names:
        value = flat.get(name)
        if value not in (None, "", []):
            return value
    return None


def build_auth(settings, transport=None):
    if settings.auth_mode == "api_key":
        return ApiKeyAuth(settings)
    if settings.auth_mode == "oauth2":
        return OAuth2ClientCredentials(settings, transport=transport)
    return ApiKeyTokenAuth(settings, transport=transport)
