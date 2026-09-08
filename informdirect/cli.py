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

# The Add company endpoint refuses bulk use (429), so calls are paced.
ADD_COMPANY_PAUSE = 2.0

OK_KIND = "ok"

# (label, Company attribute, expectation)
#   "supplied"  - the API returns it, and its absence is a real problem
#   "absent"    - confirmed against the live API as not returned at all
FIELD_CHECKS = (
    ("company number", "company_number", "supplied"),
    ("company name", "name", "supplied"),
    ("status", "status", "absent"),
    ("next accounts made up to", "accounts_next_made_up_to", "absent"),
    ("accounts due date", "accounts_due", "absent"),
    ("last accounts made up to", "accounts_last_made_up_to", "absent"),
    ("confirmation statement due", "confirmation_due", "absent"),
    ("incorporation date", "incorporation_date", "absent"),
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

    check = sub.add_parser(
        "check", help="prove credentials work and report field coverage")
    check.add_argument("--redact", action="store_true",
                       help="omit company names and numbers, leaving only counts "
                            "- for output that will be shared or committed")
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

    diagnose = sub.add_parser(
        "diagnose",
        help="dump the raw payloads so the real field names can be read off")
    diagnose.add_argument("--company",
                          help="company to fetch in detail (default: the first "
                               "one the list returns)")
    diagnose.add_argument("--out", help="also write the report to this file")

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
                        help="(unused - the live Add company endpoint takes only "
                             "CompanyNumber; kept so older commands still parse)")
    verify.add_argument("--confirm", action="store_true",
                        help="actually run Add company and Remove company; without "
                             "it only the read-only endpoints are exercised")
    verify.add_argument("--live", action="store_true",
                        help="permit the mutating steps against the production "
                             "host (they are refused there otherwise)")
    verify.add_argument("--keep", action="store_true",
                        help="skip Remove company, leaving the added company linked")

    member = sub.add_parser(
        "membership",
        help="compare the Inform Direct portfolio against the planner's companies")
    member.add_argument("--planner", required=True, help="planner rows CSV")
    member.add_argument("--out", help="write the full report to this CSV")
    member.add_argument("--json", action="store_true", dest="as_json")
    member.add_argument("--add", action="store_true",
                        help="link the companies that are missing from Inform "
                             "Direct (needs --confirm)")
    member.add_argument("--confirm", action="store_true",
                        help="actually perform the additions")
    member.add_argument("--auth-code",
                        help="Companies House authentication code to send with "
                             "each addition")
    member.add_argument("--live", action="store_true",
                        help="permit additions against the production host "
                             "(refused there otherwise)")
    member.add_argument("--limit", type=int, default=25,
                        help="most companies to add in one run (default 25)")

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


def _require_live_ack(settings, what, acknowledged=False):
    """Refuse to change a real portfolio without --live being passed.

    Add and remove are not reversible in the sense that matters: removing a
    company the practice files for, or adding one it does not, is a real change
    to a live account. Sandbox needs no ceremony; production does.
    """
    if not settings.is_production or acknowledged:
        return True
    print(
        f"\nRefusing to {what} against PRODUCTION ({settings.base_url}) "
        "without --live.\n"
        "  This changes the real Inform Direct portfolio.\n"
        "  Re-run with --live if that is genuinely what you want, or point\n"
        "  INFORMDIRECT_BASE_URL at https://sandbox-api.informdirect.co.uk.",
        file=sys.stderr,
    )
    return False


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
    if args.command == "diagnose":
        return _cmd_diagnose(args)
    if args.command == "authtest":
        return _cmd_authtest(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "membership":
        return _cmd_membership(args)
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
    print(f"environment: {client.settings.environment_name()}")
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
    if getattr(args, "redact", False):
        print("  (company details withheld - --redact)")
    else:
        for company in sample[:3]:
            print(f"  {company.company_number or '(no number)':<10} "
                  f"{company.name[:38]:<38} due {company.accounts_due or '-'}")

    print("\nfield coverage across the sample:")
    missing_supplied = []
    unexpectedly_present = []
    for label, attr, expectation in FIELD_CHECKS:
        present = sum(1 for c in sample if getattr(c, attr, None) not in (None, ""))
        mark = "ok  " if present == len(sample) else ("some" if present else "none")
        note = "" if expectation == "supplied" else "  (not returned by this API)"
        print(f"  [{mark}] {label:<32} {present}/{len(sample)}{note}")
        if expectation == "supplied" and not present:
            missing_supplied.append(label)
        if expectation == "absent" and present:
            unexpectedly_present.append(label)

    if unexpectedly_present:
        print("\nGood news - the API returned fields it was not expected to: "
              + ", ".join(unexpectedly_present) + ".")
        print("That changes what the planner can do. Tell Claude, and the")
        print("deadline reconciliation can be pointed at the API after all.")
        return EXIT_OK

    if missing_supplied:
        print("\nThe API returned nothing for: " + ", ".join(missing_supplied) + ".")
        print("Those are fields it does normally supply, so something is wrong -")
        print("run `diagnose` to see the raw payload.")
        return EXIT_EXCEPTIONS

    print("\nWorking as expected. Note what that means:")
    print("  the API supplies a company number, a name and a portal link, and")
    print("  nothing else - no status, year end, deadline or confirmation date.")
    print("  So it can answer WHICH companies are linked (`membership`), but it")
    print("  cannot drive the deadline feed. That still needs the portfolio")
    print("  export. See the README.")
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


# Concepts the planner needs, and the words that hint at each in a field name.
CONCEPT_HINTS = (
    ("company number", ("number", "crn", "registration")),
    ("company name", ("name",)),
    ("status", ("status", "state")),
    ("accounts year end", ("madeup", "periodend", "yearend", "accountingreference",
                           "arddate", "accountsdate")),
    ("accounts deadline", ("due", "deadline", "filingdate")),
    ("confirmation statement", ("confirmation", "annualreturn", "cs")),
    ("incorporation", ("incorporat",)),
    ("identifier", ("id", "guid", "key", "uid")),
)


def _cmd_diagnose(args):
    """Show what the API actually returns, field by field.

    The list endpoint and the detail endpoint often carry different fields - a
    list of companies is commonly a summary, with the dates only on the detail
    record. This fetches both and reports the keys in each, so a missing field
    can be told apart from a renamed one without guessing.
    """
    portfolio = _portfolio(args)
    lines = []

    def emit(text=""):
        print(text)
        lines.append(text)

    emit("=== list endpoint (Get companies) ===")
    listed = []
    for raw in portfolio.client.paginate("list_companies"):
        listed.append(raw)
        if len(listed) >= 3:
            break

    if not listed:
        emit("  returned no companies - nothing to inspect")
        return EXIT_EXCEPTIONS

    emit(f"  {len(listed)} record(s) sampled")
    emit("  raw payload of the first record:")
    emit(_indent(json.dumps(listed[0], indent=2, default=str)))
    list_keys = _all_keys(listed[0])
    emit(f"  keys: {', '.join(sorted(list_keys)) or '(none)'}")

    target = args.company or _first_identifier(listed[0])
    emit()
    emit(f"=== detail endpoint (Get company) for {target!r} ===")
    detail_keys = set()
    if not target:
        emit("  no usable identifier in the list record - cannot fetch detail")
    else:
        try:
            response = portfolio.client.call("get_company",
                                             path_params={"company_id": target})
            payload = response.json()
            emit("  raw payload:")
            emit(_indent(json.dumps(payload, indent=2, default=str)))
            detail_keys = _all_keys(payload)
            emit(f"  keys: {', '.join(sorted(detail_keys)) or '(none)'}")
        except errors.InformDirectError as exc:
            emit(f"  failed: {exc}")

    emit()
    emit("=== what maps to what the planner needs ===")
    combined = {**{k: "list" for k in list_keys},
                **{k: ("both" if k in list_keys else "detail") for k in detail_keys}}
    for concept, hints in CONCEPT_HINTS:
        matches = [f"{k} ({where})" for k, where in sorted(combined.items())
                   if any(hint in _norm_key(k) for hint in hints)]
        emit(f"  {concept:<24} {', '.join(matches) if matches else '-- nothing --'}")

    extra = sorted(k for k in combined
                   if not any(hint in _norm_key(k)
                              for _, hints in CONCEPT_HINTS for hint in hints))
    if extra:
        emit(f"  {'(unmatched fields)':<24} {', '.join(extra)}")

    if args.out:
        from pathlib import Path as _Path

        target_path = _Path(args.out).expanduser()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("\n".join(lines) + "\n")
        print(f"\nwritten to {target_path}")

    return EXIT_OK


def _all_keys(payload, prefix="", depth=0):
    """Every key in a payload, dotted for nesting."""
    found = set()
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict) or depth > 3:
        return found
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if isinstance(value, (dict, list)):
            nested = _all_keys(value, f"{path}.", depth + 1)
            found |= nested or {path}
        else:
            found.add(path)
    return found


def _first_identifier(record):
    from .models import clean_company_number, flatten

    table = flatten(record)
    for name in ("id", "companyid", "informdirectid", "guid", "uid", "key"):
        if table.get(name) not in (None, ""):
            return str(table[name])
    for name in ("companynumber", "registerednumber", "number"):
        if table.get(name) not in (None, ""):
            return clean_company_number(table[name])
    return None


def _norm_key(value):
    return "".join(c for c in str(value).lower() if c.isalnum())


def _indent(text, prefix="    "):
    return "\n".join(prefix + line for line in text.splitlines())


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
    print("(hosts that do not resolve are dropped after one try)\n")
    env_attempts = authprobe.probe_environments(
        settings.api_key, shape, settings.user_agent,
        timeout=min(settings.timeout, 8.0),
        on_host=lambda a: _print_attempts([a]),
    )

    if not any(a.status is not None for a in env_attempts):
        print("\n  (no host answered - none of them exist)")

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

    settings = _settings(args)
    if args.confirm and not _require_live_ack(
            settings, "add and remove a company", args.live):
        return EXIT_ERROR

    portfolio = _portfolio(args)
    client = portfolio.client
    print(f"environment: {client.settings.environment_name()}")
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
            added = portfolio.add_company(args.company_number)
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


def _cmd_membership(args):
    """Which planner companies are linked to Inform Direct, and which are not.

    The API returns a company number, a name and a portal link - nothing else -
    so it cannot answer deadline questions. Membership is what it can answer,
    and it is worth answering: a client missing from the portfolio is one
    nobody is filing for.
    """
    import time

    from .reconcile import NOT_IN_INFORMDIRECT, reconcile_membership

    rows = load_planner_csv(args.planner)
    portfolio = _portfolio(args)
    companies = portfolio.companies()
    result = reconcile_membership(companies, rows)

    if args.as_json:
        print(json.dumps({
            "summary": {"in_inform_direct": result.registry_count,
                        "planner_rows": result.row_count,
                        "matched": result.matched,
                        "counts": result.counts()},
            "actions": [a.as_dict() for a in result.actions],
        }, indent=2))
    else:
        print(f"in Inform Direct : {result.registry_count}")
        print(f"planner rows     : {result.row_count}")
        print(f"linked and agreed: {result.matched}")
        for kind, count in result.counts().items():
            print(f"{kind:<17}: {count}")
        for action in result.actions:
            if action.kind == OK_KIND:
                continue
            print(f"\n  [{action.kind}] {action.company_number} "
                  f"{action.company_name[:40]}")
            print(f"      {action.reason}")

    missing = result.of_kind(NOT_IN_INFORMDIRECT)
    if args.add and missing and args.confirm:
        if not _require_live_ack(portfolio.client.settings,
                                 f"link {len(missing)} company(ies)", args.live):
            return EXIT_ERROR
    if args.add and missing:
        if not args.confirm:
            print(f"\n{len(missing)} company(ies) would be linked. Re-run with "
                  "--confirm to do it.")
        else:
            print(f"\nLinking {min(len(missing), args.limit)} of "
                  f"{len(missing)} company(ies)...")
            added = already = failed = 0
            for action in missing[:args.limit]:
                label = f"{action.company_number} {action.company_name[:40]}"
                try:
                    portfolio.add_company(action.company_number,
                                          auth_code=args.auth_code)
                    added += 1
                    print(f"  added     {label}")
                except errors.AlreadyLinkedError:
                    # The desired state already holds - not a failure.
                    already += 1
                    print(f"  already   {label}")
                except errors.NotFoundError:
                    failed += 1
                    print(f"  NOT FOUND {label} - Companies House does not know "
                          "this number")
                except errors.RateLimitError:
                    print(f"  RATE LIMIT at {label} - stopping. The endpoint "
                          "refuses bulk use; re-run later or with a smaller "
                          "--limit.")
                    break
                except errors.InformDirectError as exc:
                    failed += 1
                    print(f"  FAILED    {label}: {exc}")
                # The add endpoint refuses bulk use, so pace the calls.
                time.sleep(ADD_COMPANY_PAUSE)
            print(f"\nadded {added}, already linked {already}, failed {failed}")
            if added and not args.auth_code:
                print("Companies were added without a Companies House "
                      "authentication code; Inform Direct needs one before it "
                      "can file for them (--auth-code).")

    if args.out:
        print(f"\nreport -> {write_report_csv(result, args.out)}")

    return EXIT_EXCEPTIONS if result.exceptions or missing else EXIT_OK


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
