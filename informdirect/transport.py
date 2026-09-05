"""Minimal HTTP transport.

Standard library only, so the package runs on a stock Python 3.9+ without a
pip install. `Transport` is a protocol - tests inject a fake, and anything with
a compatible `send()` can be swapped in.
"""

from __future__ import annotations

import json as _json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .errors import TransportError


@dataclass
class HttpResponse:
    status: int
    headers: dict = field(default_factory=dict)
    body: bytes = b""
    url: str = ""

    @property
    def text(self):
        charset = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self):
        """Parsed JSON body, or None if the body is empty or not JSON."""
        if not self.body:
            return None
        try:
            return _json.loads(self.text)
        except ValueError:
            return None

    @property
    def ok(self):
        return 200 <= self.status < 300


class UrllibTransport:
    """HTTP over the standard library. Honours HTTPS_PROXY/NO_PROXY via urllib."""

    def send(self, method, url, headers=None, body=None, timeout=30.0):
        request = urllib.request.Request(
            url, data=body, headers=dict(headers or {}), method=method.upper()
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    status=response.status,
                    headers=_lower(response.headers.items()),
                    body=response.read(),
                    url=response.geturl(),
                )
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a real response - hand it back for the client to map.
            return HttpResponse(
                status=exc.code,
                headers=_lower(exc.headers.items() if exc.headers else []),
                body=exc.read() if hasattr(exc, "read") else b"",
                url=url,
            )
        except urllib.error.URLError as exc:
            raise TransportError(f"{method} {url} failed: {exc.reason}") from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TransportError(f"{method} {url} timed out after {timeout}s") from exc
        except OSError as exc:
            raise TransportError(f"{method} {url} failed: {exc}") from exc


def _lower(items):
    return {str(k).lower(): v for k, v in items}
