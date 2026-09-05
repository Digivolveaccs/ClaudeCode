import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from informdirect.endpoints import EndpointMap
from informdirect.errors import ConfigError, EndpointNotConfigured
from informdirect.export import write_snapshot_json, write_tracker_csv
from informdirect.models import Company

REPO_ROOT = Path(__file__).resolve().parent.parent


def sample(number="01234567", name="Test Ltd"):
    return Company.from_api({
        "companyNumber": number, "companyName": name, "companyStatus": "Active",
        "incorporationDate": "2001-06-04", "nextAccountsMadeUpTo": "2025-03-31",
        "accountsDueDate": "2025-12-31",
    })


class ExportTest(unittest.TestCase):
    def test_csv_headers_and_iso_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_tracker_csv([sample()], Path(tmp) / "feed.csv")
            with path.open(encoding="utf-8-sig") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["Company Number"], "01234567")
            self.assertEqual(row["Next Accounts Made Up To"], "2025-03-31")
            self.assertEqual(row["Accounts Due Date"], "2025-12-31")
            self.assertEqual(row["Confirmation Statement Due Date"], "")

    def test_directory_target_writes_a_dated_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state" / "inform direct"
            path = write_tracker_csv([sample()], target)
            self.assertTrue(path.is_file())
            self.assertEqual(path.parent, target)
            self.assertIn(date.today().isoformat(), path.name)

    def test_rows_are_sorted_by_company_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_tracker_csv(
                [sample("09999999", "Z Ltd"), sample("01111111", "A Ltd")],
                Path(tmp) / "feed.csv")
            with path.open(encoding="utf-8-sig") as handle:
                numbers = [r["Company Number"] for r in csv.DictReader(handle)]
            self.assertEqual(numbers, ["01111111", "09999999"])

    def test_snapshot_json_keeps_the_raw_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_snapshot_json([sample()], Path(tmp) / "snap.json")
            payload = json.loads(path.read_text())
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["companies"][0]["raw"]["companyName"], "Test Ltd")

    def test_exported_csv_round_trips_back_into_companies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_tracker_csv([sample()], Path(tmp) / "feed.csv")
            with path.open(encoding="utf-8-sig") as handle:
                restored = [Company.from_api(r) for r in csv.DictReader(handle)]
            self.assertEqual(restored[0].company_number, "01234567")
            self.assertEqual(restored[0].accounts_due, date(2025, 12, 31))
            self.assertEqual(restored[0].accounts_next_made_up_to, date(2025, 3, 31))


class EndpointMapTest(unittest.TestCase):
    def test_shipped_map_declares_the_confirmed_operations(self):
        endpoints = EndpointMap.load(REPO_ROOT / "config" / "endpoints.json")
        for name in ("list_companies", "get_company", "add_company"):
            self.assertTrue(endpoints.has(name), name)
        self.assertFalse(endpoints.is_provisional,
                         "paths were verified against the live API")
        self.assertEqual(endpoints.resolve("add_company"),
                         ("POST", "/companies/add"))
        self.assertEqual(endpoints.resolve("get_company", company_id="01234567"),
                         ("GET", "/companies/01234567"))

    def test_remove_company_is_parked_as_unresolved(self):
        raw = json.loads((REPO_ROOT / "config" / "endpoints.json").read_text())
        self.assertIn("remove_company", raw["_unresolved"])
        self.assertNotIn("remove_company", raw["operations"])

    def test_officers_and_shareholders_are_parked_not_silently_wired_up(self):
        raw = json.loads((REPO_ROOT / "config" / "endpoints.json").read_text())
        parked = raw["_not_offered_by_the_api"]
        for name in ("list_officers", "list_shareholders", "list_filings"):
            self.assertIn(name, parked)
            self.assertNotIn(name, raw["operations"])

    def test_asking_for_a_parked_operation_fails_loudly(self):
        endpoints = EndpointMap.load(REPO_ROOT / "config" / "endpoints.json")
        with self.assertRaises(EndpointNotConfigured) as ctx:
            endpoints.get("list_officers")
        self.assertIn("list_companies", str(ctx.exception))

    def test_resolve_substitutes_and_quotes(self):
        endpoints = EndpointMap.from_dict({
            "operations": {"get": {"method": "get", "path": "/c/{company_id}/x"}}
        })
        self.assertEqual(endpoints.resolve("get", company_id="a b/c"),
                         ("GET", "/c/a%20b%2Fc/x"))

    def test_missing_path_parameter_is_reported(self):
        endpoints = EndpointMap.from_dict({
            "operations": {"get": {"method": "GET", "path": "/c/{company_id}"}}
        })
        with self.assertRaises(ConfigError) as ctx:
            endpoints.resolve("get")
        self.assertIn("company_id", str(ctx.exception))

    def test_unknown_operation_lists_the_known_ones(self):
        endpoints = EndpointMap.from_dict({
            "operations": {"a": {"method": "GET", "path": "/a"}}
        })
        with self.assertRaises(EndpointNotConfigured) as ctx:
            endpoints.get("b")
        self.assertIn("Known operations: a", str(ctx.exception))

    def test_malformed_maps_are_rejected(self):
        bad_cases = [
            {},
            {"operations": {}},
            {"operations": {"a": {"method": "GET"}}},
            {"operations": {"a": {"method": "GET", "path": "no-leading-slash"}}},
            {"operations": {"a": "not-an-object"}},
        ]
        for data in bad_cases:
            with self.subTest(data=data):
                with self.assertRaises(ConfigError):
                    EndpointMap.from_dict(data)

    def test_per_operation_pagination_overrides_the_default(self):
        endpoints = EndpointMap.from_dict({
            "pagination": {"style": "auto", "max_pages": 10},
            "operations": {"a": {"method": "GET", "path": "/a",
                                 "pagination": {"style": "cursor"}}},
        })
        self.assertEqual(endpoints.pagination["max_pages"], 10)
        self.assertEqual(endpoints.get("a").pagination["style"], "cursor")


if __name__ == "__main__":
    unittest.main()
