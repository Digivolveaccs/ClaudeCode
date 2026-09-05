"""Command line entry point:  python3 -m informdirect <command>

    check        prove the credentials and the endpoint map work
    companies    export the portfolio as the tracker's registry feed CSV
    company      show one company, its directors and its shareholders
    reconcile    compare the registry against planner rows
    config       print the resolved settings (secrets masked)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from . import errors
from .config import Settings
from .endpoints import EndpointMap
from .export import write_snapshot_json, write_tracker_csv
from .portfolio import Portfolio
from .reconcile import load_planner_csv, reconcile, write_report_csv

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_EXCEPTIONS = 2


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python3 -m informdirect",
        description="Inform Direct API client for the planner and job reconciliation.",
    )
    parser.add_argument("--config", help="path to a settings JSON file")
    parser.add_argument("--base-url", dest="base_url")
    parser.add_argument("--endpoints", dest="endpoints_path",
                        help="path to an endpoint map JSON file")
    parser.add_argument("--no-token-cache", action="store_true",
                        help="do not read or write the on-disk token cache")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="verify credentials and endpoints")
    sub.add_parser("config", help="print resolved settings, secrets masked")

    companies = sub.add_parser("companies", help="export the registry feed")
    companies.add_argument("--out", required=True,
                           help="CSV file, or a directory to write a dated file into")
    companies.add_argument("--json", dest="json_out",
                           help="also write a full snapshot (parsed + raw) here")
    companies.add_argument("--include-dissolved", action="store_true")

    company = sub.add_parser("company", help="show one company in detail")
    company.add_argument("number", help="company number or Inform Direct id")
    company.add_argument("--json", action="store_true", dest="as_json")

    rec = sub.add_parser("reconcile", help="compare registry against planner rows")
    rec.add_argument("--planner", required=True, help="planner rows CSV")
    rec.add_argument("--snapshot", help="use this companies CSV/JSON instead of the API")
    rec.add_argument("--out", help="write the full action report to this CSV")
    rec.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except errors.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except errors.AuthError as exc:
        print(f"authentication failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except errors.ApiError as exc:
        print(f"API error: {exc}", file=sys.stderr)
        if exc.request_id:
            print(f"request id: {exc.request_id}", file=sys.stderr)
        return EXIT_ERROR
    except errors.InformDirectError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


def _settings(args):
    overrides = {}
    if getattr(args, "base_url", None):
        overrides["base_url"] = args.base_url
    if getattr(args, "endpoints_path", None):
        overrides["endpoints_path"] = args.endpoints_path
    if getattr(args, "no_token_cache", False):
        overrides["cache_tokens"] = False
    return Settings.load(config_path=args.config, **overrides)


def _portfolio(args):
    settings = _settings(args)
    portfolio = Portfolio(settings=settings)
    if portfolio.client.endpoints.is_provisional:
        print(
            "warning: endpoint paths are still marked provisional in "
            f"{portfolio.client.endpoints.source} - confirm them against the "
            "OpenAPI spec (scripts/fetch_spec.py).",
            file=sys.stderr,
        )
    return portfolio


def _dispatch(args):
    if args.command == "config":
        return _cmd_config(args)
    if args.command == "check":
        return _cmd_check(args)
    if args.command == "companies":
        return _cmd_companies(args)
    if args.command == "company":
        return _cmd_company(args)
    if args.command == "reconcile":
        return _cmd_reconcile(args)
    return EXIT_ERROR


def _cmd_config(args):
    settings = _settings(args)
    print(json.dumps(settings.redacted(), indent=2))
    try:
        settings.validate()
        print("\nsettings look complete.")
    except errors.ConfigError as exc:
        print(f"\nincomplete: {exc}")
        return EXIT_ERROR
    endpoints = EndpointMap.load(settings.endpoints_path)
    print(f"endpoint map: {endpoints.source} (status: {endpoints.status})")
    print("operations: " + ", ".join(endpoints.names()))
    return EXIT_OK


def _cmd_check(args):
    portfolio = _portfolio(args)
    client = portfolio.client
    print(f"base url  : {client.settings.base_url}")
    print(f"auth      : {client.auth.describe()}")
    if hasattr(client.auth, "token"):
        client.auth.token()
        print("token     : obtained")
    sample = []
    for company in portfolio.iter_companies():
        sample.append(company)
        if len(sample) >= 3:
            break
    print(f"companies : reachable, first {len(sample)} record(s):")
    for company in sample:
        print(f"  {company.company_number or '(no number)':<10} "
              f"{company.name[:40]:<40} due {company.accounts_due or '-'}")
    if not sample:
        print("  (portfolio returned no companies - check entitlements)")
        return EXIT_EXCEPTIONS
    return EXIT_OK


def _cmd_companies(args):
    portfolio = _portfolio(args)
    companies = portfolio.companies(include_dissolved=args.include_dissolved)
    csv_path = write_tracker_csv(companies, args.out)
    print(f"{len(companies)} companies -> {csv_path}")
    if args.json_out:
        json_path = write_snapshot_json(companies, args.json_out)
        print(f"snapshot -> {json_path}")
    missing = [c for c in companies if c.accounts_due is None and not c.is_dissolved]
    if missing:
        print(f"warning: {len(missing)} live companies have no accounts due date:",
              file=sys.stderr)
        for company in missing[:10]:
            print(f"  {company.label()}", file=sys.stderr)
    return EXIT_OK


def _cmd_company(args):
    portfolio = _portfolio(args)
    view = portfolio.close_company_view(args.number)
    if view is None:
        print(f"no company found for {args.number!r}", file=sys.stderr)
        return EXIT_ERROR
    company = view["company"]
    if args.as_json:
        print(json.dumps({
            "company": {k: _iso(v) for k, v in vars(company).items() if k != "raw"},
            "directors": [{k: _iso(v) for k, v in vars(o).items() if k != "raw"}
                          for o in view["directors"]],
            "shareholders": [{k: _iso(v) for k, v in vars(s).items() if k != "raw"}
                             for s in view["shareholders"]],
        }, indent=2))
        return EXIT_OK

    print(f"{company.company_number}  {company.name}")
    print(f"  status        : {company.status}")
    print(f"  incorporated  : {company.incorporation_date or '-'}")
    print(f"  next year end : {company.accounts_next_made_up_to or '-'}")
    print(f"  accounts due  : {company.accounts_due or '-'}")
    print(f"  CS due        : {company.confirmation_due or '-'}")
    print("  directors:")
    for officer in view["directors"] or []:
        print(f"    {officer.name} ({officer.role}) appointed "
              f"{officer.appointed_on or '-'}")
    print("  shareholders:")
    for holder in view["shareholders"] or []:
        pct = f"{holder.percentage:.2f}%" if holder.percentage is not None else "-"
        kind = "corporate" if holder.is_corporate else "individual"
        print(f"    {holder.name} {pct} ({holder.shares_held or '-'} "
              f"{holder.share_class or 'shares'}, {kind})")
    return EXIT_OK


def _cmd_reconcile(args):
    rows = load_planner_csv(args.planner)
    if args.snapshot:
        companies = _load_snapshot(args.snapshot)
    else:
        companies = _portfolio(args).companies()

    result = reconcile(companies, rows)

    if args.as_json:
        print(json.dumps({
            "summary": {
                "registry_count": result.registry_count,
                "row_count": result.row_count,
                "matched": result.matched,
                "counts": result.counts(),
            },
            "actions": [a.as_dict() for a in result.actions],
        }, indent=2))
    else:
        print(result.summary())
        automatic = result.automatic
        if automatic:
            print(f"\n-- {len(automatic)} change(s) the planner can apply --")
            for action in automatic:
                print(f"  [{action.kind}] {action.company_number} "
                      f"{action.company_name[:34]}")
                print(f"      {action.reason}")
                if action.proposed:
                    print("      -> " + "; ".join(
                        f"{k}={_iso(v)}" for k, v in action.proposed.items()
                        if v not in (None, "")))
        if result.exceptions:
            print(f"\n-- {len(result.exceptions)} exception(s) for a human --")
            for action in result.exceptions:
                print(f"  [{action.kind}] {action.company_number} "
                      f"{action.company_name[:34]}: {action.reason}")

    if args.out:
        print(f"\nreport -> {write_report_csv(result, args.out)}")

    return EXIT_EXCEPTIONS if result.exceptions else EXIT_OK


def _load_snapshot(path):
    """Read companies from a previously exported CSV or JSON snapshot."""
    import csv as _csv
    from pathlib import Path

    from .models import Company

    target = Path(path).expanduser()
    if target.suffix.lower() == ".json":
        payload = json.loads(target.read_text())
        records = payload.get("companies", payload) if isinstance(payload, dict) else payload
        return [Company.from_api(r.get("raw") or r) for r in records]
    with target.open(newline="", encoding="utf-8-sig") as handle:
        return [Company.from_api(row) for row in _csv.DictReader(handle)]


def _iso(value):
    return value.isoformat() if isinstance(value, date) else value
