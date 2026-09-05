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


def load_spec(source):
    text = _read(source)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import yaml
    except ImportError:
        raise SystemExit(
            "spec is not JSON and PyYAML is not installed. Either export the spec "
            "as JSON, or: pip3 install pyyaml"
        )
    return yaml.safe_load(text)


def _read(source):
    if str(source).startswith(("http://", "https://")):
        request = urllib.request.Request(
            source, headers={"Accept": "application/json, application/yaml, */*"}
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")
    path = Path(source).expanduser()
    if not path.is_file():
        raise SystemExit(f"spec not found: {path}")
    return path.read_text(encoding="utf-8")


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
    args = parser.parse_args(argv)

    spec = load_spec(args.spec)
    if not isinstance(spec, dict) or "paths" not in spec:
        raise SystemExit("that does not look like an OpenAPI/Swagger document")

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
