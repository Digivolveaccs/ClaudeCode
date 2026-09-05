"""Command line entry point:  python3 -m informdirect <command>

    check        prove the credentials work and report field coverage
    verify       exercise all four endpoints (the gate for a production key)
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

SAMPLE_SIZE = 25

# (label, Company attribute, is it required for the planner feed)
FIELD_CHECKS = (
    ("company number", "company_number", True),
    ("company name", "name", True),
    ("status", "status", True),
    ("next accounts made up to", "accounts_next_made_up_to", True),
    ("accounts due date", "accounts_due", True),
    ("last accounts made up to", "accounts_last_made_up_to", False),
    ("confirmation statement due", "confirmation_due", False),
    ("incorporation date", "incorporation_date", False),
)


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

    sub.add_parser("check", help="prove credentials work and report field coverage")
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

    authtest = sub.add_parser(
        "authtest",
        help="probe every plausible shape for the authentication request")
    authtest.add_argument("--auth-url",
                          help="override the endpoint to probe")
    authtest.add_argument("--no-host-probe", action="store_true",
                          help="stop after the shape probe; do not try other hosts")

    verify = sub.add_parser(
        "verify",
        help="exercise all four endpoints - the gate for a production key")
    verify.add_argument("--company-number",
                        help="a company to add then remove (required with --confirm)")
    verify.add_argument("--auth-code",
                        help="Companies House authentication code for that company")
    verify.add_argument("--confirm", action="store_true",
                        help="actually run Add company and Remove company; without "
                             "it only the read-only endpoints are exercised")
    verify.add_argument("--keep", action="store_true",
                        help="skip Remove company, leaving the added company linked")

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
    if args.command == "authtest":
        return _cmd_authtest(args)
    if args.command == "verify":
        return _cmd_verify(args)
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
    """Prove the credentials work, then report which fields the API returns.

    Inform Direct describes the Integration API as returning "high-level company
    details". Whether that includes the accounts year end and deadline decides
    whether the planner feed and reconciliation can run off the API at all, so
    this reports it rather than leaving it to be discovered later.
    """
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
        if len(sample) >= SAMPLE_SIZE:
            break

    if not sample:
        print("companies : none returned - check the key's entitlements",
              file=sys.stderr)
        return EXIT_EXCEPTIONS

    print(f"companies : reachable, sampled {len(sample)}")
    for company in sample[:3]:
        print(f"  {company.company_number or '(no number)':<10} "
              f"{company.name[:38]:<38} due {company.accounts_due or '-'}")

    print("\nfield coverage across the sample:")
    missing_critical = []
    for label, attr, critical in FIELD_CHECKS:
        present = sum(1 for c in sample if getattr(c, attr, None) not in (None, ""))
        mark = "ok  " if present == len(sample) else ("some" if present else "NONE")
        flag = " (needed by the planner)" if critical else ""
        print(f"  [{mark}] {label:<32} {present}/{len(sample)}{flag}")
        if critical and not present:
            missing_critical.append(label)

    if missing_critical:
        print("\nThe API returned nothing for: " + ", ".join(missing_critical) + ".")
        print("Either those fields are not part of 'high-level company details',")
        print("or they are named something the parser does not recognise yet.")
        print("Run `companies --json <file>` and look at a company's `raw` payload:")
        print("if the data is there under another name, add that name to the")
        print("aliases in informdirect/models.py and it will start mapping.")
        return EXIT_EXCEPTIONS

    print("\nEverything the planner feed and reconciliation need is present.")
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
            "directors": _people(view["directors"]),
            "shareholders": _people(view["shareholders"]),
            "unavailable": view["unavailable"],
        }, indent=2))
        return EXIT_OK

    print(f"{company.company_number}  {company.name}")
    print(f"  status        : {company.status}")
    print(f"  incorporated  : {company.incorporation_date or '-'}")
    print(f"  next year end : {company.accounts_next_made_up_to or '-'}")
    print(f"  accounts due  : {company.accounts_due or '-'}")
    print(f"  CS due        : {company.confirmation_due or '-'}")
    if view["directors"] is None:
        print("  directors     : not available from this API "
              "(see _not_offered_by_the_api in config/endpoints.json)")
    else:
        print("  directors:")
        for officer in view["directors"]:
            print(f"    {officer.name} ({officer.role}) appointed "
                  f"{officer.appointed_on or '-'}")

    if view["shareholders"] is None:
        print("  shareholders  : not available from this API")
    else:
        print("  shareholders:")
        for holder in view["shareholders"]:
            pct = f"{holder.percentage:.2f}%" if holder.percentage is not None else "-"
            kind = "corporate" if holder.is_corporate else "individual"
            print(f"    {holder.name} {pct} ({holder.shares_held or '-'} "
                  f"{holder.share_class or 'shares'}, {kind})")
    return EXIT_OK


def _cmd_authtest(args):
    """Find the shape the authentication endpoint wants, then the environment.

    A 401 means the request was understood and the credential refused; a 400
    means it was not understood. That split identifies the right field name, and
    once the shape is settled a 401 points at the key or the host instead - so
    the same shape is then tried against the likely sandbox hosts.
    """
    from . import authprobe

    settings = _settings(args).validate()
    url = args.auth_url or settings.resolved_auth_url()
    if not settings.api_key:
        print("no api_key set - nothing to probe with", file=sys.stderr)
        return EXIT_ERROR

    print(f"probing {url}")
    print(f"key     ...{settings.api_key[-6:]}\n")

    attempts = authprobe.run(
        authprobe.build_attempts(url, settings.api_key, settings.user_agent),
        timeout=settings.timeout,
    )
    _print_attempts(attempts)

    result = authprobe.analyse(attempts)
    verdict = result["verdict"]

    if verdict == "working":
        best = result["shape"]
        print("\nAuthentication works. Put this in config/settings.json:")
        print(f"  {best.settings_hint()}")
        if not best.refresh:
            print("  note: no refresh token came back - /refresh needs one, so "
                  "check the response")
        return EXIT_OK

    if verdict == "no_shape_understood":
        print("\nEvery shape returned 400 - none of them was understood.")
        print("The endpoint may not be /authenticate. Paste this output to Claude.")
        return EXIT_EXCEPTIONS

    # 401 somewhere: the field name is settled, the credential is not accepted here.
    shape = result["shape"]
    print(f"\nRequest shape identified: {shape.label.strip()}")
    print("  401 means the request was understood and the key refused;")
    print("  400 means the field name was wrong. So the shape is right and the")
    print("  problem is the key or the environment.")
    print(f"  settings:  {shape.settings_hint()}")

    if args.no_host_probe:
        return EXIT_EXCEPTIONS

    print("\nTrying that shape against the likely sandbox hosts and paths...")
    env_attempts = authprobe.run(
        authprobe.build_environment_attempts(
            settings.api_key, shape, settings.user_agent),
        timeout=min(settings.timeout, 10.0),
    )

    interesting = [a for a in env_attempts if a.ok or a.status in (401, 400, 403)]
    if interesting:
        _print_attempts(interesting)
    else:
        print("  (no host answered - they do not exist)")

    working = [a for a in env_attempts if a.ok]
    if working:
        print(f"\nThis one authenticated: {working[0].label}")
        print("  set INFORMDIRECT_BASE_URL to that host (minus the path).")
        return EXIT_OK

    print("\nNo sandbox host accepted the key either. That leaves:")
    print("  - the key needs activating, or was regenerated")
    print("  - the sandbox host is one not guessed here")
    print("\nBoth are questions for support@informdirect.co.uk. Suggested wording:")
    print('  "Our sandbox key (ending ' + settings.api_key[-6:] + ') returns 401')
    print(f'   from POST {url} with body {{\"apiKey\": \"...\"}}.')
    print('   Is there a separate sandbox base URL, or does the key need activating?"')
    return EXIT_EXCEPTIONS


def _print_attempts(attempts):
    for attempt in attempts:
        if attempt.error:
            print(f"  [err ] {attempt.label:<34} {attempt.error}")
            continue
        mark = "WORKS" if attempt.ok else f"{attempt.status}"
        print(f"  [{mark:^5}] {attempt.label:<34} {attempt.detail}")


def _cmd_verify(args):
    """Exercise the four endpoints Inform Direct requires before granting a
    production key: Add company, Remove company, Get company, Get companies.

    Add and Remove mutate the account, so they only run with --confirm. Without
    it this exercises the read-only half and tells you what it skipped.
    """
    if args.confirm and not args.company_number:
        print("--confirm needs --company-number: a company to add and then remove.",
              file=sys.stderr)
        return EXIT_ERROR

    portfolio = _portfolio(args)
    client = portfolio.client
    results = []

    def step(name, fn, *, skipped=None):
        if skipped:
            results.append((name, "skip", skipped))
            return None
        try:
            detail = fn()
        except errors.InformDirectError as exc:
            results.append((name, "FAIL", str(exc)))
            return None
        results.append((name, "pass", detail))
        return detail

    def do_authenticate():
        if not hasattr(client.auth, "token"):
            return "static key - no token exchange"
        client.auth.token()
        return "access token obtained"

    step("Authenticate", do_authenticate)

    listed = []

    def do_list():
        for company in portfolio.iter_companies():
            listed.append(company)
            if len(listed) >= 5:
                break
        return f"{len(listed)} company(ies) returned"

    step("Get companies", do_list)

    added = None
    if args.confirm:
        def do_add():
            nonlocal added
            added = portfolio.add_company(args.company_number,
                                          auth_code=args.auth_code)
            return f"linked {args.company_number}"
        step("Add company", do_add)
    else:
        step("Add company", None, skipped="needs --confirm (it changes the account)")

    target = args.company_number or (listed[0].company_number if listed else None)

    def do_get():
        record = portfolio.get_company(target)
        if record is None:
            raise errors.InformDirectError(f"no company returned for {target!r}")
        return f"{record.company_number} {record.name}".strip()

    if target:
        step("Get company", do_get)
    else:
        step("Get company", None, skipped="no company number to look up")

    if args.confirm and not args.keep:
        step("Remove company",
             lambda: f"unlinked {args.company_number}"
             if portfolio.remove_company(args.company_number) else "")
    elif args.keep:
        step("Remove company", None, skipped="--keep was passed")
    else:
        step("Remove company", None, skipped="needs --confirm (it changes the account)")

    print()
    for name, status, detail in results:
        print(f"  [{status:^4}] {name:<16} {detail}")

    failed = [n for n, st, _ in results if st == "FAIL"]
    skipped = [n for n, st, _ in results if st == "skip"]

    if failed:
        print(f"\n{len(failed)} endpoint(s) failed: " + ", ".join(failed))
        return EXIT_ERROR
    if skipped:
        print("\nSkipped: " + ", ".join(skipped))
        print("Inform Direct validate all four endpoints before enabling a "
              "production key, so re-run with --confirm against sandbox.")
        return EXIT_EXCEPTIONS

    print("\nAll four endpoints returned successful authenticated responses.")
    print("To request production access, email support@informdirect.co.uk with:")
    print("  - your organisation name")
    print(f"  - last 6 of the sandbox key used here: "
          f"...{client.settings.api_key[-6:] if client.settings.api_key else '??????'}")
    print("  - last 6 of the production key you want activated")
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


def _people(records):
    if records is None:
        return None
    return [{k: _iso(v) for k, v in vars(r).items() if k != "raw"} for r in records]


def _iso(value):
    return value.isoformat() if isinstance(value, date) else value
