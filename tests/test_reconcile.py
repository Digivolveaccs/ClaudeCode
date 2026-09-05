import unittest
from datetime import date

from informdirect.models import Company
from informdirect.reconcile import (
    AHEAD, CHARITY, CREATE, DISSOLVED, INSOLVENT, OK, SNAP, SUBMITTED, UNMATCHED,
    PlannerRow, reconcile, rows_from_dicts,
)


def company(number="01234567", name="Test Ltd", status="Active",
            year_end="2025-03-31", due="2025-12-31"):
    return Company.from_api({
        "companyNumber": number, "companyName": name, "companyStatus": status,
        "nextAccountsMadeUpTo": year_end, "accountsDueDate": due,
    })


def row(number="01234567", name="Test Ltd", year_end="2025-03-31",
        deadline="2025-12-31", stage="3-In progress", notes="", row_number=2):
    return PlannerRow(
        company_number=number, company_name=name,
        year_end=date.fromisoformat(year_end) if year_end else None,
        deadline=date.fromisoformat(deadline) if deadline else None,
        stage=stage, notes=notes, row_number=row_number,
    )


def kinds(result):
    return sorted({a.kind for a in result.actions})


class ReconcileTest(unittest.TestCase):
    def test_agreeing_row_is_ok(self):
        result = reconcile([company()], [row()])
        self.assertEqual(kinds(result), [OK])
        self.assertEqual(result.matched, 1)
        self.assertEqual(result.exceptions, [])

    def test_same_deadline_year_but_wrong_dates_snaps_to_registry(self):
        result = reconcile([company()], [row(year_end="2025-03-30",
                                             deadline="2025-12-30")])
        action = result.of_kind(SNAP)[0]
        self.assertEqual(action.proposed["year_end"], date(2025, 3, 31))
        self.assertEqual(action.proposed["deadline"], date(2025, 12, 31))
        self.assertEqual(action.current["deadline"], date(2025, 12, 30))

    def test_row_with_no_deadline_takes_registry_dates(self):
        result = reconcile([company()], [row(year_end=None, deadline=None)])
        self.assertEqual(kinds(result), [SNAP])

    def test_earlier_deadline_year_means_that_period_was_filed(self):
        result = reconcile([company()], [row(year_end="2024-03-31",
                                             deadline="2024-12-31")])
        submitted = result.of_kind(SUBMITTED)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(submitted[0].proposed["stage"], "8-Submitted")
        # ...and the current year still has no row, so one is created.
        self.assertEqual(len(result.of_kind(CREATE)), 1)

    def test_earlier_year_already_marked_submitted_is_left_alone(self):
        result = reconcile(
            [company()],
            [row(year_end="2024-03-31", deadline="2024-12-31", stage="8-Submitted")],
        )
        self.assertEqual(result.of_kind(SUBMITTED), [])
        self.assertEqual(len(result.of_kind(CREATE)), 1)

    def test_both_years_present_marks_old_and_keeps_new(self):
        result = reconcile([company()], [
            row(year_end="2024-03-31", deadline="2024-12-31", row_number=2),
            row(row_number=3),
        ])
        self.assertEqual(len(result.of_kind(SUBMITTED)), 1)
        self.assertEqual(len(result.of_kind(OK)), 1)
        self.assertEqual(result.of_kind(CREATE), [])

    def test_company_absent_from_planner_creates_a_row(self):
        result = reconcile([company()], [])
        created = result.of_kind(CREATE)
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].proposed["deadline"], date(2025, 12, 31))

    def test_dissolved_company_is_flagged_and_never_rolled_forward(self):
        result = reconcile([company(status="Dissolved")], [row()])
        self.assertEqual(kinds(result), [DISSOLVED])
        self.assertEqual(result.of_kind(CREATE, SNAP, SUBMITTED), [])
        self.assertEqual(len(result.exceptions), 1)

    def test_dissolved_company_with_no_row_is_not_created(self):
        result = reconcile([company(status="Struck off")], [])
        self.assertEqual(result.actions, [])

    def test_company_in_liquidation_is_flagged_but_still_processed(self):
        result = reconcile([company(status="In Liquidation")], [row()])
        self.assertIn(INSOLVENT, kinds(result))
        self.assertIn(OK, kinds(result))

    def test_planner_row_missing_from_the_portfolio(self):
        result = reconcile([], [row(number="09999999")])
        self.assertEqual(kinds(result), [UNMATCHED])
        self.assertEqual(result.matched, 0)

    def test_row_ahead_of_the_registry_is_flagged_not_snapped(self):
        result = reconcile([company()], [row(year_end="2026-03-31",
                                             deadline="2026-12-31")])
        self.assertEqual(kinds(result), [AHEAD])
        self.assertIn("registry feed is current", result.actions[0].reason)

    def test_charity_rows_skip_the_registry(self):
        result = reconcile([], [row(number="", name="Helping Hands",
                                    notes="Charity 1122334")])
        action = result.of_kind(CHARITY)[0]
        self.assertEqual(action.current["charity_number"], "1122334")
        self.assertEqual(result.of_kind(UNMATCHED), [])

    def test_registry_without_a_due_date_is_flagged(self):
        result = reconcile([company(due=None)], [row()])
        self.assertEqual(kinds(result), [UNMATCHED])

    def test_registry_without_a_due_date_and_no_row_creates_nothing(self):
        result = reconcile([company(due=None)], [])
        self.assertEqual(result.actions, [])

    def test_number_formats_are_normalised_on_both_sides(self):
        result = reconcile([company(number="1234567")],
                           [row(number="01234567")])
        self.assertEqual(kinds(result), [OK])

    def test_summary_and_counts(self):
        result = reconcile([company(), company(number="09999999", name="Two Ltd")],
                           [row()])
        self.assertEqual(result.registry_count, 2)
        self.assertEqual(result.row_count, 1)
        self.assertEqual(result.counts()[CREATE], 1)
        self.assertIn("registry companies : 2", result.summary())


class RowLoadingTest(unittest.TestCase):
    def test_headers_are_matched_fuzzily(self):
        rows = rows_from_dicts([{
            "Company Number": "1234567",
            "Client Name": "Test Ltd",
            "Year End": "31/03/2025",
            "Accounts Due Date": "2025-12-31",
            "Job Stage": "3-In progress",
            "Notes": "",
        }])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].company_number, "01234567")
        self.assertEqual(rows[0].year_end, date(2025, 3, 31))
        self.assertEqual(rows[0].deadline, date(2025, 12, 31))
        self.assertEqual(rows[0].row_number, 2)

    def test_blank_rows_are_dropped(self):
        rows = rows_from_dicts([{"Company Number": "", "Company Name": ""},
                                {"Company Number": "1234567"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].row_number, 3)

    def test_charity_detection_and_submitted_stage(self):
        rows = rows_from_dicts([{"Company Name": "X", "Notes": "Charity 123456",
                                 "Stage": "8-Submitted"}])
        self.assertTrue(rows[0].is_charity)
        self.assertTrue(rows[0].is_submitted)
        self.assertEqual(rows[0].charity_number, "123456")


if __name__ == "__main__":
    unittest.main()
