#!/usr/bin/env python3
"""Turn the Inform Direct OpenAPI spec into config/endpoints.json.

The published spec was not reachable from the machine this package was written
on, so config/endpoints.json ships with provisional paths. Point this script at
the real spec once and the guesswork is gone:

    # from a downloaded file (SwaggerHub -> Export -> Download API -> JSON/YAML)
    python3 scripts/fetch_spec.py --spec ~/Downloads/informdirect.json --write

    # or straight from a URL, if your network can reach it
    python3 scripts/fetch_spec.py --spec https://.../openapi.json --write

Without --write it prints what it found and what it would change, so you can
check the mapping before it lands.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "config" / "endpoints.json"

# How to recognise each operation we need. Checked in order:
#   1. an operationId containing one of `op_ids`
#   2. a GET path whose last non-parameter segment matches `segment`, with the
#      right number of path parameters
WANTED = {
    "list_companies": {
        "segment": ("companies", "company", "portfolio"),
        "params": 0, "op_ids": ("listcompanies", "getcompanies", "searchcompanies"),
    },
    "get_company": {
        "segment": ("companies", "company"),
        "params": 1, "op_ids": ("getcompany", "companybyid", "getcompanybyid"),
    },
    "list_officers": {
        "segment": ("officers", "officer", "appointments", "directors"),
        "params": 1, "op_ids": ("listofficers", "getofficers", "companyofficers"),
    },
    "list_shareholders": {
        "segment": ("shareholders", "shareholder", "members", "shareholdings",
                    "shares"),
        "params": 1, "op_ids": ("listshareholders", "getshareholders",
                                "companyshareholders", "getshareholdings"),
    },
    "list_filings": {
        "segment": ("filings", "filing", "filinghistory", "submissions"),
        "params": 1, "op_ids": ("listfilings", "getfilings", "filinghistory"),
    },
}

PARAM_RE = re.compile(r"\{([^}]+)\}")


def norm(value):
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def parse_spec(text):
    """Parse JSON or YAML into a dict, or None if it is neither."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        import yaml
    except ImportError:
        return None
    try:
        loaded = yaml.safe_load(text)
    except Exception:
        return None
    return loaded if isinstance(loaded, dict) else None


def looks_like_spec(parsed):
    """True only for something that really is an OpenAPI/Swagger document.

    Servers do not reliably 404. This API answers a missing path with HTTP 200
    and an IIS "the resource you are looking for has been removed" body, so a
    successful fetch proves nothing on its own.
    """
    return (
        isinstance(parsed, dict)
        and isinstance(parsed.get("paths"), dict)
        and ("openapi" in parsed or "swagger" in parsed)
    )


def load_spec(source, headers=None):
    text = _read(source, headers=headers)
    parsed = parse_spec(text)
    if parsed is None:
        raise SystemExit(
            f"could not parse {source} as JSON or YAML. First 200 characters:\n"
            f"  {text[:200]!r}"
        )
    return parsed


# Where APIs commonly publish their spec, tried in order when given a bare host.
SPEC_CANDIDATES = (
    "/swagger/v1/swagger.json",
    "/swagger/v1/swagger.yaml",
    "/openapi.json",
    "/openapi/v1.json",
    "/swagger.json",
    "/v1/swagger.json",
    "/api-docs",
    "/api/swagger.json",
)

# Pages that embed the real spec URL when the guesses above miss. Swashbuckle
# (ASP.NET) puts it in swagger-ui-init.js; most Swagger UI builds put it in the
# HTML itself.
UI_CANDIDATES = (
    "/swagger/swagger-ui-init.js",
    "/swagger/index.html",
    "/swagger",
    "/",
)

_SPEC_URL_RE = re.compile(
    r"""["']([^"'\s]*(?:swagger|openapi)[^"'\s]*\.(?:json|yaml|yml))["']""",
    re.IGNORECASE,
)


def _read(source, headers=None):
    source = str(source)
    if source.startswith(("http://", "https://")):
        return _read_url(source, headers=headers)
    path = Path(source).expanduser()
    if not path.is_file():
        raise SystemExit(f"spec not found: {path}")
    return path.read_text(encoding="utf-8")


def _read_url(url, headers=None):
    """Fetch a spec from a URL, probing common locations for a bare host.

    Every candidate is parsed and checked before it is accepted, because a 2xx
    response is not evidence that a spec came back.
    """
    if url.rstrip("/").endswith((".json", ".yaml", ".yml")) or "?" in url:
        return _fetch(url, headers=headers)

    root = url.rstrip("/")
    tried = []

    for candidate in SPEC_CANDIDATES:
        body = _probe(root + candidate, tried, headers=headers)
        if body is not None:
            return body

    # Nothing at the usual paths - ask the Swagger UI where its spec lives.
    for ui_path in UI_CANDIDATES:
        try:
            page = _fetch(root + ui_path, headers=headers)
        except SystemExit as exc:
            tried.append(f"  {root}{ui_path}: {exc}")
            continue
        discovered = _spec_urls_in(page)
        if not discovered:
            tried.append(f"  {root}{ui_path}: no spec URL referenced")
            continue
        for reference in discovered:
            absolute = urllib.parse.urljoin(root + ui_path, reference)
            body = _probe(absolute, tried, headers=headers,
                          note=f" (referenced by {ui_path})")
            if body is not None:
                return body

    raise SystemExit(
        f"no OpenAPI spec found under {root}. Tried:\n" + "\n".join(tried)
        + "\n\nIf the spec needs authentication, pass it through:\n"
        "    --header 'Authorization: Bearer <token>'\n"
        "Otherwise download it from SwaggerHub (Export > Download API > JSON)\n"
        "and pass the file instead of the host."
    )


def _probe(url, tried, headers=None, note=""):
    """Fetch one candidate, accepting it only if it really is a spec."""
    try:
        body = _fetch(url, headers=headers)
    except SystemExit as exc:
        tried.append(f"  {url}{note}: {exc}")
        return None
    parsed = parse_spec(body)
    if looks_like_spec(parsed):
        print(f"found spec at {url}")
        return body
    if parsed is None:
        why = f"not JSON or YAML ({body.strip()[:60]!r})"
    else:
        why = "parsed, but has no 'paths' plus 'openapi'/'swagger'"
    tried.append(f"  {url}{note}: {why}")
    return None


def _spec_urls_in(text):
    """Spec URLs referenced by a Swagger UI page or its init script."""
    seen = []
    for match in _SPEC_URL_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen


def _fetch(url, headers=None):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json, application/yaml, */*",
                 **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"{exc.reason}") from exc


def server_base(spec):
    servers = spec.get("servers")
    if isinstance(servers, list) and servers:
        url = servers[0].get("url", "")
        variables = servers[0].get("variables") or {}
        for name, meta in variables.items():
            default = (meta or {}).get("default")
            if default is not None:
                url = url.replace("{" + name + "}", str(default))
        return url.rstrip("/")
    # Swagger 2.0
    host = spec.get("host")
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{spec.get('basePath', '').rstrip('/')}"
    return ""


def collect_operations(spec):
    """Every (method, path, operationId, summary) in the spec."""
    out = []
    for path, item in (spec.get("paths") or {}).items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(operation, dict):
                continue
            out.append({
                "method": method.upper(),
                "path": path,
                "operation_id": operation.get("operationId", ""),
                "summary": operation.get("summary") or operation.get("description", ""),
                "params": PARAM_RE.findall(path),
            })
    return sorted(out, key=lambda o: (o["path"], o["method"]))


def match(name, rule, operations):
    for candidate in operations:
        if candidate["method"] != "GET":
            continue
        op_id = norm(candidate["operation_id"])
        if op_id and any(want in op_id for want in rule["op_ids"]):
            return candidate

    best = None
    for candidate in operations:
        if candidate["method"] != "GET":
            continue
        if len(candidate["params"]) != rule["params"]:
            continue
        segments = [norm(s) for s in candidate["path"].split("/")
                    if s and not s.startswith("{")]
        if not segments or segments[-1] not in rule["segment"]:
            continue
        if best is None or len(candidate["path"]) < len(best["path"]):
            best = candidate
    return best


def rewrite_path(path, params):
    """Rename the single path parameter to company_id so our client can fill it."""
    if len(params) == 1:
        return path.replace("{" + params[0] + "}", "{company_id}")
    return path


def build_map(spec, existing):
    operations = collect_operations(spec)
    result = {}
    unmatched = []
    for name, rule in WANTED.items():
        found = match(name, rule, operations)
        if found is None:
            unmatched.append(name)
            if name in (existing.get("operations") or {}):
                result[name] = existing["operations"][name]
            continue
        result[name] = {
            "method": found["method"],
            "path": rewrite_path(found["path"], found["params"]),
            "description": (found["summary"] or "").strip()[:200],
            "operation_id": found["operation_id"],
        }
    return result, operations, unmatched


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--spec", required=True, help="OpenAPI file path or URL")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--write", action="store_true",
                        help="write the endpoint map (otherwise just report)")
    parser.add_argument("--list", action="store_true",
                        help="list every operation in the spec and exit")
    parser.add_argument("--header", action="append", default=[], metavar="H",
                        help="extra request header, e.g. "
                             "--header 'Authorization: Bearer <token>'. Repeatable.")
    args = parser.parse_args(argv)

    headers = {}
    for raw in args.header:
        if ":" not in raw:
            raise SystemExit(f"--header needs 'Name: value', got {raw!r}")
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()

    spec = load_spec(args.spec, headers=headers or None)
    if not looks_like_spec(spec):
        raise SystemExit(
            "that does not look like an OpenAPI/Swagger document (no 'paths' "
            "plus 'openapi'/'swagger')"
        )

    out_path = Path(args.out).expanduser()
    existing = {}
    if out_path.is_file():
        try:
            existing = json.loads(out_path.read_text())
        except json.JSONDecodeError:
            existing = {}

    if args.list:
        for op in collect_operations(spec):
            print(f"{op['method']:<6} {op['path']:<55} {op['operation_id']}")
        return 0

    base = server_base(spec)
    mapped, all_ops, unmatched = build_map(spec, existing)

    title = (spec.get("info") or {}).get("title", "(untitled)")
    version = (spec.get("info") or {}).get("version", "")
    print(f"spec        : {title} {version}")
    print(f"server base : {base or '(none declared)'}")
    print(f"operations  : {len(all_ops)} in spec, {len(mapped)} mapped")
    for name in sorted(mapped):
        entry = mapped[name]
        print(f"  {name:<20} {entry['method']:<5} {entry['path']}")
    if unmatched:
        print("\ncould not match automatically: " + ", ".join(unmatched))
        print("run with --list to see every path, then edit the map by hand.")

    if not args.write:
        print(f"\n(dry run - pass --write to update {out_path})")
        return 0

    document = {
        "_status": "confirmed" if not unmatched else "partial",
        "_spec_source": str(args.spec),
        "_spec_title": f"{title} {version}".strip(),
        "_server_base": base,
        "_generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pagination": existing.get("pagination") or {},
        "operations": mapped,
    }
    if not document["pagination"]:
        document.pop("pagination")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(document, indent=2) + "\n")
    print(f"\nwrote {out_path} (status: {document['_status']})")
    if base:
        print(f"set base_url to: {base}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
