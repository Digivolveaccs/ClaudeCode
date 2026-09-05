"""Exception hierarchy for the Inform Direct client."""

from __future__ import annotations


class InformDirectError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(InformDirectError):
    """Configuration is missing or contradictory."""


class EndpointNotConfigured(InformDirectError):
    """An operation was requested that config/endpoints.json does not define."""


class AuthError(InformDirectError):
    """Could not obtain or refresh an access token."""


class TransportError(InformDirectError):
    """The request never produced an HTTP response (DNS, TLS, timeout, reset)."""


class ApiError(InformDirectError):
    """The API returned a non-2xx response."""

    def __init__(self, status, message, *, body=None, headers=None, url=None):
        self.status = status
        self.message = message
        self.body = body
        self.headers = headers or {}
        self.url = url
        super().__init__(f"HTTP {status} for {url}: {message}")

    @property
    def request_id(self):
        for key in ("x-request-id", "x-correlation-id", "request-id"):
            if key in self.headers:
                return self.headers[key]
        return None


class BadRequestError(ApiError):
    """400 / 422 - the request was rejected as invalid."""


class NotFoundError(ApiError):
    """404 - resource (or endpoint path) does not exist.

    Add company returns this when the company number is not one Companies House
    knows, so the lookup behind the endpoint fails.
    """


class AlreadyLinkedError(ApiError):
    """422 - the company is already associated with this account.

    Not a failure for anything that adds companies in bulk: the desired state
    already holds.
    """


class ForbiddenError(ApiError):
    """403 - authenticated but not entitled to this resource."""


class RateLimitError(ApiError):
    """429 - throttled."""

    @property
    def retry_after(self):
        raw = self.headers.get("retry-after")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None


class ServerError(ApiError):
    """5xx - the API failed."""


def from_response(status, message, *, body=None, headers=None, url=None):
    """Map an HTTP status onto the most specific ApiError subclass."""
    if status == 422 and "already associated" in str(message).lower():
        cls = AlreadyLinkedError
    elif status in (400, 422):
        cls = BadRequestError
    elif status == 403:
        cls = ForbiddenError
    elif status == 404:
        cls = NotFoundError
    elif status == 429:
        cls = RateLimitError
    elif status >= 500:
        cls = ServerError
    else:
        cls = ApiError
    return cls(status, message, body=body, headers=headers, url=url)
