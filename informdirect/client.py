"""HTTP client: auth, retries, error mapping and pagination.

Everything above this layer works in terms of named operations and plain dicts.
"""

from __future__ import annotations

import json as _json
import random
import time
import urllib.parse

from . import errors
from .auth import build_auth
from .config import Settings
from .endpoints import EndpointMap
from .transport import UrllibTransport

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

# Keys an API might use for the array of results, most specific first.
ITEM_KEYS = (
    "items", "data", "results", "records", "content", "value", "companies",
    "officers", "shareholders", "filings",
)
NEXT_URL_KEYS = ("nextPageUrl", "nextLink", "@odata.nextLink", "next", "nextPage")
CURSOR_KEYS = ("nextCursor", "continuationToken", "cursor", "nextToken")
PAGE_KEYS = ("page", "pageNumber", "currentPage", "pageIndex")
TOTAL_PAGE_KEYS = ("totalPages", "pageCount", "totalPageCount")
TOTAL_KEYS = ("total", "totalCount", "totalItems", "totalRecords", "count")


class Client:
    def __init__(self, settings=None, *, transport=None, auth=None, endpoints=None,
                 sleep=time.sleep):
        self.settings = (settings or Settings.load()).validate()
        self.transport = transport or UrllibTransport()
        self.auth = auth if auth is not None else build_auth(self.settings, self.transport)
        self.endpoints = endpoints or EndpointMap.load(self.settings.endpoints_path)
        self._sleep = sleep

    # -- low level -------------------------------------------------------- #

    def request(self, method, path, *, params=None, json_body=None, headers=None,
                absolute=False):
        url = path if absolute else self.settings.base_url + path
        if params:
            query = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}, doseq=True
            )
            if query:
                url += ("&" if "?" in url else "?") + query

        body = None
        base_headers = {
            "Accept": "application/json",
            "User-Agent": self.settings.user_agent,
        }
        if json_body is not None:
            body = _json.dumps(json_body).encode("utf-8")
            base_headers["Content-Type"] = "application/json"
        base_headers.update(headers or {})

        attempts = self.settings.max_retries + 1
        reauthorised = False
        last_exc = None

        for attempt in range(attempts):
            send_headers = dict(base_headers)
            self.auth.apply(send_headers)
            try:
                response = self.transport.send(
                    method, url, headers=send_headers, body=body,
                    timeout=self.settings.timeout,
                )
            except errors.TransportError as exc:
                last_exc = exc
                if attempt + 1 < attempts:
                    self._sleep(self._backoff(attempt))
                    continue
                raise

            if response.ok:
                return response

            # A single 401 usually means the token expired early - refresh once.
            if response.status == 401 and not reauthorised and self.auth.invalidate():
                reauthorised = True
                continue

            if response.status in RETRY_STATUSES and attempt + 1 < attempts:
                self._sleep(self._backoff(attempt, response))
                continue

            raise errors.from_response(
                response.status,
                _message(response),
                body=response.text[:2000],
                headers=response.headers,
                url=url,
            )

        raise last_exc or errors.TransportError(f"{method} {url} exhausted retries")

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def call(self, operation, *, path_params=None, params=None, json_body=None):
        """Invoke a named operation from the endpoint map."""
        method, path = self.endpoints.resolve(operation, **(path_params or {}))
        return self.request(method, path, params=params, json_body=json_body)

    # -- pagination ------------------------------------------------------- #

    def paginate(self, operation, *, path_params=None, params=None):
        """Yield every item across all pages of a list operation.

        Handles page-number, offset/limit, cursor and next-URL styles, and falls
        back to "keep asking until a short page comes back" when the API gives
        no explicit paging metadata.
        """
        op = self.endpoints.get(operation)
        method, path = op.render(**(path_params or {}))
        cfg = {**self.endpoints.pagination, **op.pagination}
        style = cfg.get("style", "auto")
        page_size = self.settings.page_size
        max_pages = int(cfg.get("max_pages", 500))

        query = dict(params or {})
        page = int(cfg.get("first_page", 1))
        offset = 0
        cursor = None
        next_url = None
        seen = 0
        previous = None

        for page_index in range(max_pages):
            if next_url:
                response = self.request(method, next_url, absolute=True)
            else:
                call_params = dict(query)
                if style in ("auto", "page"):
                    call_params.setdefault(cfg["page_size_param"], page_size)
                    if page_index or style == "page":
                        call_params[cfg["page_param"]] = page
                if style == "offset":
                    call_params[cfg["limit_param"]] = page_size
                    call_params[cfg["offset_param"]] = offset
                if style == "cursor" and cursor:
                    call_params[cfg["cursor_param"]] = cursor
                response = self.request(method, path, params=call_params)

            payload = response.json()
            items = extract_items(payload)

            # If the API ignores our paging parameters it will hand back the same
            # page for ever. Stop on a repeat rather than duplicating records.
            fingerprint = _fingerprint(items)
            if items and fingerprint == previous:
                return
            previous = fingerprint

            for item in items:
                yield item
            seen += len(items)

            next_url = _next_url(payload, response)
            cursor = _first(payload, CURSOR_KEYS)
            page += 1
            offset += len(items) or page_size

            if next_url:
                continue
            if style == "cursor" or (style == "auto" and cursor):
                if not cursor:
                    return
                continue
            if _is_last_page(payload, seen, len(items), page_size):
                return
        # Ran out of page budget rather than pages - say so loudly.
        raise errors.InformDirectError(
            f"{operation}: stopped after {max_pages} pages ({seen} items). "
            "Raise pagination.max_pages in the endpoint map if that is genuinely short."
        )

    # -- helpers ---------------------------------------------------------- #

    def _backoff(self, attempt, response=None):
        if response is not None:
            retry_after = response.headers.get("retry-after")
            try:
                return min(float(retry_after), self.settings.backoff_cap)
            except (TypeError, ValueError):
                pass
        delay = self.settings.backoff_base * (2 ** attempt)
        delay = min(delay, self.settings.backoff_cap)
        return delay * (0.5 + random.random() / 2.0)  # jitter


def extract_items(payload):
    """Pull the list of records out of a response body of unknown shape."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ITEM_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    lists = [v for v in payload.values() if isinstance(v, list)]
    if len(lists) == 1:
        return lists[0]
    return []


def _next_url(payload, response):
    link = response.headers.get("link")
    if link:
        url = _link_header_next(link)
        if url:
            return url
    if isinstance(payload, dict):
        for key in NEXT_URL_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        links = payload.get("links") or payload.get("_links")
        if isinstance(links, dict):
            nxt = links.get("next")
            if isinstance(nxt, str) and nxt.startswith("http"):
                return nxt
            if isinstance(nxt, dict):
                href = nxt.get("href")
                if isinstance(href, str) and href.startswith("http"):
                    return href
    return None


def _link_header_next(header):
    for part in header.split(","):
        segments = part.split(";")
        if len(segments) < 2:
            continue
        url = segments[0].strip().strip("<>")
        for segment in segments[1:]:
            if segment.strip().lower().replace('"', "") == "rel=next":
                return url
    return None


def _first(payload, keys):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _is_last_page(payload, seen, page_len, page_size):
    if page_len == 0:
        return True
    if isinstance(payload, dict):
        page = _as_int(_first(payload, PAGE_KEYS))
        total_pages = _as_int(_first(payload, TOTAL_PAGE_KEYS))
        if page is not None and total_pages is not None:
            return page >= total_pages
        total = _as_int(_first(payload, TOTAL_KEYS))
        if total is not None:
            return seen >= total
    # No metadata: a short page means the end.
    return page_len < page_size


def _fingerprint(items):
    try:
        return _json.dumps(items, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(items)


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _message(response):
    payload = response.json()
    if isinstance(payload, dict):
        for key in ("message", "error_description", "detail", "title", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        errs = payload.get("errors")
        if isinstance(errs, list) and errs:
            return "; ".join(str(e) for e in errs[:3])
    text = response.text.strip()
    return text[:200] if text else "(no response body)"
