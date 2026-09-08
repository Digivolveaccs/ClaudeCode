"""The whole path: API payloads -> Company records -> feed CSV -> reconciliation."""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

from tests.support import FakeTransport, StubAuth, json_response

from informdirect.client import Client
from informdirect.config import Settings
from informdirect.endpoints import EndpointMap
from informdirect.export import write_tracker_csv
from informdirect.portfolio import Portfolio
from informdirect.reconcile import CREATE, SNAP, SUBMITTED, load_planner_csv, reconcile

ENDPOINTS = {
    "_status": "test",
    "pagination": {"style": "auto", "page_param": "page", "page_size_param": "pageSize"},
    "operations": {
        "list_companies": {"method": "GET", "path": "/companies"},
        "get_company": {"method": "GET", "path": "/companies/{company_id}"},
        "add_company": {"method": "POST", "path": "/companies/add"},
        "remove_company": {"method": "PUT", "path": "/companies/delete"},
        "list_officers": {"method": "GET", "path": "/companies/{company_id}/officers"},
        "list_shareholders": {"method": "GET",
                              "path": "/companies/{company_id}/shareholders"},
        "list_filings": {"method": "GET", "path": "/companies/{company_id}/filings"},
    },
}

PAGE_1 = {
    "items": [
        {"id": "id-1", "companyNumber": "1234567", "companyName": "Browns Garage Ltd",
         "companyStatus": "Active", "nextAccountsMadeUpTo": "2025-03-31",
         "accountsDueDate": "2025-12-31"},
        {"id": "id-2", "companyNumber": "07654321", "companyName": "PMC Floors Ltd",
         "companyStatus": "Active", "accounts": {"nextMadeUpTo": "2025-09-30",
                                                 "dueDate": "2026-06-30"}},
    ],
    "page": 1, "totalPages": 2,
}
PAGE_2 = {
    "items": [
        {"id": "id-3", "companyNumber": "00112233", "companyName": "Old Co Ltd",
         "companyStatus": "Dissolved"},
        {"id": "id-4", "companyNumber": "05550001", "companyName": "New Client Ltd",
         "companyStatus": "Active", "nextAccountsMadeUpTo": "2025-06-30",
         "accountsDueDate": "2026-03-31"},
    ],
    "page": 2, "totalPages": 2,
}

PLANNER_CSV = """Company Number,Client Name,Year End,Accounts Due Date,Job Stage,Notes
1234567,Browns Garage Ltd,30/03/2025,30/12/2025,3-In progress,
07654321,PMC Floors Ltd,30/09/2024,30/06/2025,4-Queries,
00112233,Old Co Ltd,31/12/2024,30/09/2025,2-Records in,
,Helping Hands,,,1-Not started,Charity 1122334
"""


ALL_COMPANIES = PAGE_1["items"] + PAGE_2["items"]


def routed(method, url, headers=None, body=None):
    # GET /companies/<id> - one company by Inform Direct id or company number.
    path = url.split("?", 1)[0]
    if "/companies/" in path and path.count("/companies/") == 1:
        tail = path.rsplit("/companies/", 1)[1]
        if "/" not in tail:
            wanted = tail.lstrip("0")
            for record in ALL_COMPANIES:
                if record["id"] == tail or record["companyNumber"].lstrip("0") == wanted:
                    return json_response(record)
            return json_response({"message": "not found"}, status=404)
    if "/officers" in url:
        return json_response([{"name": "M Hunt", "role": "Director",
                               "appointedOn": "2019-04-01"}])
    if "/shareholders" in url:
        return json_response([{"name": "M Hunt", "numberOfShares": 60},
                              {"name": "Digivolve Holdings Ltd",
                               "numberOfShares": 40}])
    if "page=2" in url:
        return json_response(PAGE_2)
    if url.rstrip("/").endswith("/companies") or "pageSize" in url:
        return json_response(PAGE_1)
    return json_response({})


def make_portfolio():
    settings = Settings.load(base_url="https://api.example.com/v1",
                             auth_mode="api_key", api_key="k", page_size=2,
                             backoff_base=0.0)
    client = Client(settings, transport=FakeTransport(handler=routed),
                    auth=StubAuth(),
                    endpoints=EndpointMap.from_dict(ENDPOINTS, source="test"),
                    sleep=lambda _: None)
    return Portfolio(client=client)


class EndToEndTest(unittest.TestCase):
    def test_portfolio_paginates_and_parses_mixed_payload_shapes(self):
        companies = make_portfolio().companies()
        self.assertEqual(len(companies), 4)
        by_number = {c.company_number: c for c in companies}
        self.assertEqual(by_number["01234567"].accounts_due, date(2025, 12, 31))
        # Nested "accounts" object parses the same as the flat camelCase one.
        self.assertEqual(by_number["07654321"].accounts_due, date(2026, 6, 30))
        self.assertEqual(by_number["07654321"].accounts_next_made_up_to,
                         date(2025, 9, 30))
        self.assertTrue(by_number["00112233"].is_dissolved)

    def test_dissolved_can_be_excluded(self):
        companies = make_portfolio().companies(include_dissolved=False)
        self.assertNotIn("00112233", {c.company_number for c in companies})

    def test_close_company_view(self):
        portfolio = make_portfolio()
        company = portfolio.find_by_number("1234567")
        view = portfolio.close_company_view(company)
        self.assertEqual(view["directors"][0].name, "M Hunt")
        holders = {s.name: s for s in view["shareholders"]}
        self.assertEqual(holders["M Hunt"].percentage, 60.0)
        self.assertTrue(holders["Digivolve Holdings Ltd"].is_corporate)

    def test_feed_csv_then_reconcile_against_the_planner(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            companies = make_portfolio().companies()
            feed = write_tracker_csv(companies, tmp / "feed.csv")
            self.assertTrue(feed.is_file())

            planner = tmp / "planner.csv"
            planner.write_text(PLANNER_CSV)
            rows = load_planner_csv(planner)
            self.assertEqual(len(rows), 4)

            result = reconcile(companies, rows)
            by_number = {}
            for action in result.actions:
                by_number.setdefault(action.company_number, []).append(action.kind)

            # Same deadline year, wrong dates -> snap to the registry.
            self.assertIn(SNAP, by_number["01234567"])
            # Registry has moved to 2026 -> the 2025 row was filed, 2026 needs a row.
            self.assertIn(SUBMITTED, by_number["07654321"])
            self.assertIn(CREATE, by_number["07654321"])
            # Dissolved -> flagged, never rolled forward.
            self.assertEqual(by_number["00112233"], ["flag_dissolved"])
            # In the portfolio but not the planner -> create a row.
            self.assertEqual(by_number["05550001"], [CREATE])
            # Charity row handled without a registry match.
            self.assertEqual(by_number[""], ["charity_verify"])

    def test_reconcile_via_the_cli_against_a_snapshot(self):
        from informdirect.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            companies = make_portfolio().companies()
            feed = write_tracker_csv(companies, tmp / "feed.csv")
            planner = tmp / "planner.csv"
            planner.write_text(PLANNER_CSV)
            report = tmp / "report.csv"

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                code = main(["reconcile", "--planner", str(planner),
                             "--snapshot", str(feed), "--out", str(report),
                             "--json"])

            self.assertEqual(code, 2, "exceptions present -> exit 2")
            payload = json.loads(buffer.getvalue().split("\n\nreport ->")[0])
            self.assertEqual(payload["summary"]["registry_count"], 4)
            self.assertEqual(payload["summary"]["row_count"], 4)
            self.assertIn("flag_dissolved", payload["summary"]["counts"])

            with report.open(encoding="utf-8-sig") as handle:
                report_rows = list(csv.DictReader(handle))
            self.assertEqual(len(report_rows), len(payload["actions"]))

    def test_cli_reconcile_exits_zero_when_there_is_nothing_to_escalate(self):
        from informdirect.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            feed = tmp / "feed.csv"
            feed.write_text(
                "Company Number,Company Name,Status,Next Accounts Made Up To,"
                "Accounts Due Date\n"
                "01234567,Browns Garage Ltd,Active,2025-03-31,2025-12-31\n")
            planner = tmp / "planner.csv"
            planner.write_text(
                "Company Number,Client Name,Year End,Accounts Due Date,Job Stage,Notes\n"
                "01234567,Browns Garage Ltd,2025-03-31,2025-12-31,3-In progress,\n")

            with redirect_stdout(io.StringIO()):
                code = main(["reconcile", "--planner", str(planner),
                             "--snapshot", str(feed)])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()


class CheckCommandTest(unittest.TestCase):
    """`check` must say plainly whether the planner's fields are present."""

    def _run(self, page):
        from informdirect.cli import main
        from unittest import mock

        portfolio = make_portfolio()
        portfolio.client.transport = FakeTransport(
            handler=lambda *a: json_response(page))
        buffer = io.StringIO()
        with mock.patch("informdirect.cli._portfolio", return_value=portfolio):
            with redirect_stdout(buffer):
                code = main(["check"])
        return code, buffer.getvalue()

    def test_the_live_shape_is_reported_as_working_with_its_limits_stated(self):
        """Number and name only - which is what the API actually returns."""
        code, output = self._run({"Companies": [
            {"CompanyNumber": "01234567", "Name": "SANDBOX ONE LIMITED",
             "PublicUrl": "/c/x"}]})
        self.assertEqual(code, 0, "this is the expected shape, not a failure")
        self.assertIn("not returned by this API", output)
        self.assertIn("cannot drive the deadline feed", output)
        self.assertIn("membership", output)

    def test_dates_appearing_later_would_be_reported_as_a_change(self):
        code, output = self._run({"Companies": [
            {"CompanyNumber": "01234567", "Name": "A Ltd",
             "companyStatus": "Active", "accountsDueDate": "2025-12-31"}]})
        self.assertEqual(code, 0)
        self.assertIn("returned fields it was not expected to", output)
        self.assertIn("accounts due date", output)

    def test_a_missing_company_name_is_still_a_real_problem(self):
        code, output = self._run({"Companies": [{"CompanyNumber": "01234567"}]})
        self.assertEqual(code, 2)
        self.assertIn("company name", output)
        self.assertIn("diagnose", output)


class ParkedOperationsTest(unittest.TestCase):
    """Officers/shareholders are absent from the documented API - degrade, don't crash."""

    def _portfolio_without_people(self):
        endpoints = {
            "_status": "test",
            "operations": {
                "list_companies": {"method": "GET", "path": "/companies"},
                "get_company": {"method": "GET", "path": "/companies/{company_id}"},
            },
        }
        settings = Settings.load(base_url="https://api.example.com/v1",
                                 auth_mode="api_key", api_key="k", page_size=2,
                                 backoff_base=0.0)
        client = Client(settings, transport=FakeTransport(handler=routed),
                        auth=StubAuth(),
                        endpoints=EndpointMap.from_dict(endpoints, source="test"),
                        sleep=lambda _: None)
        return Portfolio(client=client)

    def test_close_company_view_reports_unavailable_instead_of_raising(self):
        portfolio = self._portfolio_without_people()
        view = portfolio.close_company_view(portfolio.find_by_number("1234567"))
        self.assertEqual(view["company"].company_number, "01234567")
        self.assertIsNone(view["directors"])
        self.assertIsNone(view["shareholders"])
        self.assertIn("directors", view["unavailable"])
        self.assertIn("list_companies", view["unavailable"]["directors"])

    def test_none_and_empty_are_different_answers(self):
        # The full portfolio DOES serve officers, so [] vs None stays meaningful.
        view = make_portfolio().close_company_view("1234567")
        self.assertEqual([o.name for o in view["directors"]], ["M Hunt"])
        self.assertEqual(view["unavailable"], {})

    def test_company_command_says_so_rather_than_crashing(self):
        from informdirect.cli import main
        from unittest import mock

        buffer = io.StringIO()
        with mock.patch("informdirect.cli._portfolio",
                        return_value=self._portfolio_without_people()):
            with redirect_stdout(buffer):
                code = main(["company", "1234567"])
        self.assertEqual(code, 0)
        self.assertIn("not available from this API", buffer.getvalue())


class VerifyCommandTest(unittest.TestCase):
    """The four-endpoint sandbox run Inform Direct require before a production key."""

    def _run(self, argv, handler=None):
        from informdirect.cli import main
        from unittest import mock

        portfolio = make_portfolio()
        portfolio.client.settings.api_key = "sandbox-abc123XYZ"
        if handler is not None:
            portfolio.client.transport = FakeTransport(handler=handler)
        buffer = io.StringIO()
        with mock.patch("informdirect.cli._portfolio", return_value=portfolio):
            with redirect_stdout(buffer):
                code = main(argv)
        return code, buffer.getvalue()

    def test_without_confirm_the_mutating_endpoints_are_skipped(self):
        code, output = self._run(["verify"])
        self.assertEqual(code, 2, "skipped endpoints are not a pass")
        self.assertIn("Add company", output)
        self.assertIn("needs --confirm", output)
        self.assertIn("Skipped:", output)
        self.assertNotIn("email support@informdirect.co.uk", output)

    def test_confirm_requires_a_company_number(self):
        code, _ = self._run(["verify", "--confirm"])
        self.assertEqual(code, 1)

    def test_full_run_passes_and_prints_the_production_request_details(self):
        seen = []

        def handler(method, url, headers=None, body=None):
            seen.append((method, url.split("?")[0]))
            if method == "POST":
                return json_response({"companyNumber": "05550001",
                                      "companyName": "New Client Ltd"})
            if method == "PUT":
                return json_response({"Message": "Company deleted."})
            return routed(method, url, headers, body)

        code, output = self._run(
            ["verify", "--confirm", "--company-number", "05550001",
             ], handler=handler)

        self.assertEqual(code, 0)
        for name in ("Authenticate", "Get companies", "Add company",
                     "Get company", "Remove company"):
            self.assertIn(name, output)
        self.assertNotIn("FAIL", output)
        self.assertIn("All four endpoints returned successful", output)
        # The support email needs the last 6 of the key, and nothing more of it.
        self.assertIn("...123XYZ", output)
        self.assertNotIn("sandbox-abc123XYZ", output)
        self.assertIn(("POST", "https://api.example.com/v1/companies/add"), seen)
        self.assertIn(("PUT", "https://api.example.com/v1/companies/delete"), seen)

    def test_keep_skips_removal(self):
        def handler(method, url, headers=None, body=None):
            if method == "POST":
                return json_response({"companyNumber": "05550001"})
            if method == "PUT":
                raise AssertionError("--keep must not delete")
            return routed(method, url, headers, body)

        code, output = self._run(
            ["verify", "--confirm", "--keep", "--company-number", "05550001"],
            handler=handler)
        self.assertEqual(code, 2)
        self.assertIn("--keep was passed", output)

    def test_a_failing_endpoint_is_reported_and_fails_the_run(self):
        def handler(method, url, headers=None, body=None):
            if method == "POST":
                return json_response({"message": "company already linked"}, 400)
            if method == "PUT":
                return json_response({"Message": "Company deleted."})
            return routed(method, url, headers, body)

        code, output = self._run(
            ["verify", "--confirm", "--company-number", "05550001"],
            handler=handler)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", output)
        self.assertIn("company already linked", output)
        self.assertIn("1 endpoint(s) failed: Add company", output)


class RedactedCheckTest(unittest.TestCase):
    """--redact keeps field coverage but drops anything client-identifying."""

    def _run(self, argv):
        from unittest import mock

        from informdirect.cli import main

        portfolio = make_portfolio()
        buffer = io.StringIO()
        with mock.patch("informdirect.cli._portfolio", return_value=portfolio):
            with redirect_stdout(buffer):
                code = main(argv)
        return code, buffer.getvalue()

    def test_redact_withholds_names_and_numbers(self):
        _, output = self._run(["check", "--redact"])
        self.assertIn("field coverage", output)
        self.assertIn("company details withheld", output)
        self.assertNotIn("Browns Garage", output)
        self.assertNotIn("01234567", output)
        self.assertNotIn("PMC Floors", output)

    def test_without_redact_the_sample_is_shown(self):
        _, output = self._run(["check"])
        self.assertIn("Browns Garage", output)

    def test_coverage_counts_survive_redaction(self):
        _, output = self._run(["check", "--redact"])
        self.assertIn("company number", output)
        self.assertIn("accounts due date", output)
        self.assertRegex(output, r"\d+/\d+")


class DiagnoseTest(unittest.TestCase):
    """Tell a missing field apart from a renamed one, without guessing."""

    def _run(self, list_payload, detail_payload, argv=None):
        from unittest import mock

        from informdirect.cli import main

        def handler(method, url, headers=None, body=None):
            path = url.split("?", 1)[0]
            if path.rstrip("/").endswith("/companies"):
                return json_response(list_payload)
            if detail_payload is None:
                return json_response({"message": "nope"}, status=404)
            return json_response(detail_payload)

        portfolio = make_portfolio()
        portfolio.client.transport = FakeTransport(handler=handler)
        buffer = io.StringIO()
        with mock.patch("informdirect.cli._portfolio", return_value=portfolio):
            with redirect_stdout(buffer):
                code = main(argv or ["diagnose"])
        return code, buffer.getvalue()

    def test_the_detail_endpoint_supplying_the_dates_is_visible(self):
        code, output = self._run(
            {"items": [{"id": "id-1", "companyNumber": "01234567",
                        "companyName": "Sandbox One Limited"}]},
            {"id": "id-1", "companyNumber": "01234567",
             "companyStatus": "Active",
             "nextAccountsMadeUpTo": "2025-03-31",
             "accountsDueDate": "2025-12-31"},
        )
        self.assertEqual(code, 0)
        self.assertIn("list endpoint", output)
        self.assertIn("detail endpoint", output)
        # The concept map must show where each field actually lives.
        self.assertIn("accountsDueDate (detail)", output)
        self.assertIn("companyNumber (both)", output)

    def test_a_renamed_date_field_is_surfaced_under_the_right_concept(self):
        code, output = self._run(
            {"items": [{"id": "x", "companyNumber": "01234567"}]},
            {"id": "x", "accountingReferenceDate": "2025-03-31",
             "filingDate": "2025-12-31"},
        )
        self.assertIn("accountingReferenceDate", output)
        self.assertRegex(output, r"accounts year end\s+accountingReferenceDate")
        self.assertRegex(output, r"accounts deadline\s+filingDate")

    def test_genuinely_absent_dates_are_reported_as_nothing(self):
        code, output = self._run(
            {"items": [{"id": "x", "companyNumber": "01234567",
                        "companyName": "Sandbox One Limited"}]},
            {"id": "x", "companyNumber": "01234567",
             "companyName": "Sandbox One Limited"},
        )
        self.assertRegex(output, r"accounts deadline\s+-- nothing --")
        self.assertRegex(output, r"accounts year end\s+-- nothing --")

    def test_nested_fields_are_reported_with_their_path(self):
        code, output = self._run(
            {"items": [{"id": "x"}]},
            {"id": "x", "accounts": {"nextMadeUpTo": "2025-03-31",
                                     "dueDate": "2025-12-31"}},
        )
        self.assertIn("accounts.nextMadeUpTo", output)
        self.assertIn("accounts.dueDate", output)

    def test_a_failing_detail_call_does_not_lose_the_list_findings(self):
        code, output = self._run(
            {"items": [{"id": "x", "companyNumber": "01234567"}]}, None)
        self.assertEqual(code, 0)
        self.assertIn("list endpoint", output)
        self.assertIn("failed:", output)
        self.assertIn("companyNumber (list)", output)

    def test_empty_list_is_reported_rather_than_crashing(self):
        code, output = self._run({"items": []}, None)
        self.assertEqual(code, 2)
        self.assertIn("no companies", output)

    def test_out_writes_the_same_report_to_a_file(self):
        import tempfile
        from pathlib import Path as _P

        with tempfile.TemporaryDirectory() as tmp:
            path = _P(tmp) / "diag.txt"
            code, output = self._run(
                {"items": [{"id": "x", "companyNumber": "01234567"}]},
                {"id": "x", "accountsDueDate": "2025-12-31"},
                argv=["diagnose", "--out", str(path)])
            self.assertEqual(code, 0)
            written = path.read_text()
            self.assertIn("accountsDueDate", written)
            self.assertIn("list endpoint", written)


class VerifyProductionGuardTest(unittest.TestCase):
    """verify --confirm adds and removes a company; not on production by accident."""

    def _run(self, base_url, argv_extra):
        import contextlib
        from unittest import mock

        from informdirect.cli import main

        settings = Settings.load(base_url=base_url, auth_mode="api_key",
                                 api_key="k", backoff_base=0.0)
        calls = []

        def handler(method, url, headers=None, body=None):
            calls.append(method)
            if method == "POST":
                return json_response({"Message": "added"}, status=201)
            if method == "PUT":
                return json_response({"Message": "Company deleted."})
            return routed(method, url, headers, body)

        portfolio = make_portfolio()
        portfolio.client.settings = settings
        portfolio.client.transport = FakeTransport(handler=handler)

        out, err = io.StringIO(), io.StringIO()
        with mock.patch("informdirect.cli._portfolio", return_value=portfolio):
            with mock.patch("informdirect.cli._settings", return_value=settings):
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(["verify", "--company-number", "05251849",
                                 *argv_extra])
        return code, out.getvalue() + err.getvalue(), calls

    def test_refused_on_production_without_live(self):
        code, output, calls = self._run("https://api.informdirect.co.uk",
                                        ["--confirm"])
        self.assertEqual(code, 1)
        self.assertNotIn("POST", calls, "nothing may be added")
        self.assertNotIn("PUT", calls, "nothing may be removed")
        self.assertIn("--live", output)

    def test_allowed_on_production_with_live(self):
        code, output, calls = self._run("https://api.informdirect.co.uk",
                                        ["--confirm", "--live"])
        self.assertIn("POST", calls)
        self.assertIn("PUT", calls)

    def test_the_environment_is_named_in_the_output(self):
        code, output, _ = self._run("https://api.informdirect.co.uk", [])
        self.assertIn("PRODUCTION", output)
        code, output, _ = self._run("https://sandbox-api.informdirect.co.uk", [])
        self.assertIn("sandbox", output)
