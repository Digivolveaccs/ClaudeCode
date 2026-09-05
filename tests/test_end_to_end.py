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


def routed(method, url, headers=None, body=None):
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
