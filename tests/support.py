"""Test doubles - no network is touched anywhere in this suite."""

from __future__ import annotations

import json

from informdirect.transport import HttpResponse


def json_response(payload, status=200, headers=None):
    base = {"content-type": "application/json"}
    base.update({k.lower(): v for k, v in (headers or {}).items()})
    return HttpResponse(
        status=status,
        headers=base,
        body=json.dumps(payload).encode("utf-8"),
        url="",
    )


class FakeTransport:
    """Returns queued responses and records every request it was given."""

    def __init__(self, responses=None, handler=None):
        self.queue = list(responses or [])
        self.handler = handler
        self.calls = []

    def send(self, method, url, headers=None, body=None, timeout=None):
        self.calls.append({
            "method": method, "url": url, "headers": dict(headers or {}),
            "body": body, "timeout": timeout,
        })
        if self.handler is not None:
            return self.handler(method, url, headers, body)
        if not self.queue:
            raise AssertionError(f"no queued response for {method} {url}")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        response = item
        response.url = url
        return response

    @property
    def urls(self):
        return [call["url"] for call in self.calls]


class StubAuth:
    """Auth that adds a fixed header and counts invalidations."""

    def __init__(self, token="stub-token"):
        self.token_value = token
        self.invalidations = 0

    def apply(self, headers):
        headers["Authorization"] = "Bearer " + self.token_value
        return headers

    def invalidate(self):
        self.invalidations += 1
        self.token_value = f"{self.token_value}+{self.invalidations}"
        return True

    def describe(self):
        return "stub"
