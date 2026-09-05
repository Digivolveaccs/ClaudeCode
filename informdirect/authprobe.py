"""Work out the shape the authentication endpoint actually wants.

The endpoint is confirmed live (it answers HTTP 400, not 404) but the exact
request shape is not documented anywhere public. This tries every plausible
shape once and shows the full response to each, so one run either finds the
working one or hands over the server's own explanation of what is missing.

Read-only: it authenticates and nothing else. Nothing is added or removed.
"""

from __future__ import annotations

import json
import urllib.parse

from .transport import UrllibTransport

# Field names an API might expect the key under.
BODY_FIELDS = ("apiKey", "api_key", "key", "apikey", "ApiKey", "token",
               "clientSecret")

# Headers an API might expect the key in.
KEY_HEADERS = ("X-Api-Key", "ApiKey", "X-API-KEY", "Api-Key",
               "Ocp-Apim-Subscription-Key")


class Attempt:
    def __init__(self, label, method, url, headers, body, kind):
        self.label = label
        self.method = method
        self.url = url
        self.headers = headers
        self.body = body
        self.kind = kind
        self.status = None
        self.detail = ""
        self.token = None
        self.refresh = None
        self.error = None

    @property
    def ok(self):
        return self.token is not None

    def settings_hint(self):
        """What to put in config/settings.json if this is the one that worked."""
        if self.kind[0] == "json":
            return f'"api_key_in": "body", "api_key_field": "{self.kind[1]}"'
        if self.kind[0] == "form":
            return (f'"api_key_in": "body", "api_key_field": "{self.kind[1]}"'
                    "  (form-encoded - see note below)")
        if self.kind[0] == "header":
            return f'"api_key_in": "header", "api_key_header": "{self.kind[1]}"'
        if self.kind[0] == "bearer":
            return '"api_key_in": "header", "api_key_header": "Authorization"  ' \
                   "(value must be prefixed with 'Bearer ')"
        return "(query string - not supported directly; tell Claude)"


def build_attempts(url, api_key, user_agent="informdirect-authprobe"):
    base = {"Accept": "application/json", "User-Agent": user_agent}
    attempts = []

    for field in BODY_FIELDS:
        attempts.append(Attempt(
            f"JSON body  {{{field}}}", "POST", url,
            {**base, "Content-Type": "application/json"},
            json.dumps({field: api_key}).encode(),
            ("json", field),
        ))

    for field in ("apiKey", "api_key"):
        attempts.append(Attempt(
            f"form body  {field}=", "POST", url,
            {**base, "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode({field: api_key}).encode(),
            ("form", field),
        ))

    for header in KEY_HEADERS:
        attempts.append(Attempt(
            f"header     {header}", "POST", url, {**base, header: api_key}, None,
            ("header", header),
        ))

    attempts.append(Attempt(
        "header     Authorization: Bearer", "POST", url,
        {**base, "Authorization": f"Bearer {api_key}"}, None, ("bearer", None),
    ))

    # A raw JSON string body - some minimal APIs take just the key.
    attempts.append(Attempt(
        'raw body   "<key>"', "POST", url,
        {**base, "Content-Type": "application/json"},
        json.dumps(api_key).encode(), ("raw", None),
    ))

    attempts.append(Attempt(
        "query      ?apiKey=", "POST",
        url + ("&" if "?" in url else "?") + urllib.parse.urlencode(
            {"apiKey": api_key}),
        dict(base), None, ("query", "apiKey"),
    ))

    return attempts


def run(attempts, transport=None, timeout=30.0):
    transport = transport or UrllibTransport()
    for attempt in attempts:
        try:
            response = transport.send(attempt.method, attempt.url,
                                      headers=attempt.headers, body=attempt.body,
                                      timeout=timeout)
        except Exception as exc:                      # noqa: BLE001 - report it
            attempt.error = str(exc)
            continue
        attempt.status = response.status
        payload = response.json()
        if isinstance(payload, dict):
            flat = _flatten(payload)
            attempt.token = _pick(flat, ("accesstoken", "token", "jwt", "idtoken"))
            attempt.refresh = _pick(flat, ("refreshtoken", "refresh"))
        attempt.detail = _summarise(response, payload)
    return attempts


def _flatten(payload, depth=0):
    out = {}
    if not isinstance(payload, dict) or depth > 3:
        return out
    for key, value in payload.items():
        if isinstance(value, dict):
            out.update(_flatten(value, depth + 1))
        else:
            out.setdefault("".join(c for c in str(key).lower() if c.isalnum()), value)
    return out


def _pick(flat, names):
    for name in names:
        value = flat.get(name)
        if value not in (None, "", []):
            return str(value)
    return None


# Hosts a sandbox key might belong to, when the production host rejects it.
HOST_CANDIDATES = (
    "https://sandbox-api.informdirect.co.uk",
    "https://api-sandbox.informdirect.co.uk",
    "https://sandbox.informdirect.co.uk",
    "https://test-api.informdirect.co.uk",
    "https://api-test.informdirect.co.uk",
    "https://uat-api.informdirect.co.uk",
    "https://api.informdirect.co.uk",
)

# Paths the auth endpoint might live under on any of those hosts.
PATH_CANDIDATES = (
    "/authenticate",
    "/sandbox/authenticate",
    "/v1/authenticate",
    "/api/authenticate",
    "/api/v1/authenticate",
    "/auth/authenticate",
    "/token",
)


def analyse(attempts):
    """What the spread of status codes tells us about the request shape.

    A 401 means the request was understood and the credential refused; a 400
    means it was not understood at all. So a shape that draws 401 while the
    others draw 400 has found the right field - the problem has moved on to the
    key or the environment.
    """
    winners = [a for a in attempts if a.ok]
    if winners:
        return {"verdict": "working", "shape": winners[0]}

    understood = [a for a in attempts if a.status == 401]
    if understood:
        # Prefer a JSON body shape - it is what the docs describe.
        preferred = next((a for a in understood if a.kind[0] == "json"), understood[0])
        return {"verdict": "shape_ok_key_refused", "shape": preferred,
                "understood": understood}

    return {"verdict": "no_shape_understood", "shape": None}


def _attempt_for(url, api_key, shape, user_agent, label):
    built = build_attempts(url, api_key, user_agent)
    match = next((a for a in built if a.kind == shape.kind), None)
    if match is not None:
        match.label = label
    return match


def build_environment_attempts(api_key, shape, user_agent="informdirect-authprobe",
                               hosts=HOST_CANDIDATES, paths=PATH_CANDIDATES):
    """One attempt per host+path, all using the shape that got understood."""
    attempts = []
    for host in hosts:
        for path in paths:
            attempt = _attempt_for(host.rstrip("/") + path, api_key, shape,
                                   user_agent, f"{host.split('//')[1]}{path}")
            if attempt is not None:
                attempts.append(attempt)
    return attempts


def probe_environments(api_key, shape, user_agent="informdirect-authprobe",
                       hosts=HOST_CANDIDATES, paths=PATH_CANDIDATES,
                       transport=None, timeout=10.0, on_host=None):
    """Find the host that accepts the key, in two phases.

    Trying every host against every path is a slow way to learn that six of the
    hosts do not exist - a name that does not resolve can hang for the full
    timeout. So each host is tried once on the first path, and only hosts that
    actually answered are worth the remaining paths.
    """
    transport = transport or UrllibTransport()
    first_path, rest = paths[0], paths[1:]
    results = []
    live_hosts = []

    for host in hosts:
        attempt = _attempt_for(host.rstrip("/") + first_path, api_key, shape,
                               user_agent, f"{host.split('//')[1]}{first_path}")
        if attempt is None:
            continue
        run([attempt], transport=transport, timeout=timeout)
        results.append(attempt)
        if on_host:
            on_host(attempt)
        if attempt.ok:
            return results
        if attempt.status is not None:
            live_hosts.append(host)

    for host in live_hosts:
        for path in rest:
            attempt = _attempt_for(host.rstrip("/") + path, api_key, shape,
                                   user_agent, f"{host.split('//')[1]}{path}")
            if attempt is None:
                continue
            run([attempt], transport=transport, timeout=timeout)
            results.append(attempt)
            if on_host:
                on_host(attempt)
            if attempt.ok:
                return results

    return results


def _summarise(response, payload):
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, dict) and errors:
            parts = [f"{k}: {'; '.join(map(str, v)) if isinstance(v, list) else v}"
                     for k, v in list(errors.items())[:4]]
            title = payload.get("title") or "validation failed"
            return f"{title} -- {'; '.join(parts)}"
        for key in ("message", "detail", "error_description", "error", "title"):
            if payload.get(key):
                return str(payload[key])
        return json.dumps(payload)[:220]
    text = response.text.strip()
    return text[:220] if text else "(empty body)"
