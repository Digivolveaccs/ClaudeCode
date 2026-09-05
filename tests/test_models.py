import unittest
from datetime import date

from informdirect.models import (
    Company, Officer, Shareholder, clean_company_number, compute_percentages,
    flatten, parse_date, parse_number,
)


class HelpersTest(unittest.TestCase):
    def test_parse_date_formats(self):
        cases = {
            "2025-03-31": date(2025, 3, 31),
            "2025-03-31T00:00:00Z": date(2025, 3, 31),
            "2025-03-31T12:30:00+01:00": date(2025, 3, 31),
            "31/03/2025": date(2025, 3, 31),
            "31 Mar 2025": date(2025, 3, 31),
            date(2025, 3, 31): date(2025, 3, 31),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_date(raw), expected)

    def test_parse_date_rejects_rubbish(self):
        for raw in (None, "", "not a date", "n/a"):
            self.assertIsNone(parse_date(raw))

    def test_parse_number(self):
        self.assertEqual(parse_number("1,000"), 1000.0)
        self.assertEqual(parse_number("25%"), 25.0)
        self.assertEqual(parse_number(12), 12.0)
        self.assertIsNone(parse_number("many"))

    def test_clean_company_number(self):
        self.assertEqual(clean_company_number("1234567"), "01234567")
        self.assertEqual(clean_company_number(" 012 34567 "), "01234567")
        self.assertEqual(clean_company_number("sc123456"), "SC123456")
        self.assertEqual(clean_company_number(None), "")

    def test_flatten_registers_nested_and_prefixed_keys(self):
        table = flatten({"accounts": {"nextMadeUpTo": "2025-03-31"}})
        self.assertEqual(table["accountsnextmadeupto"], "2025-03-31")
        self.assertEqual(table["nextmadeupto"], "2025-03-31")


class CompanyTest(unittest.TestCase):
    def test_flat_camel_case_payload(self):
        company = Company.from_api({
            "id": "abc-1",
            "companyNumber": "1234567",
            "companyName": "Browns Garage (Haywards Heath) Ltd",
            "companyStatus": "Active",
            "incorporationDate": "2001-06-04",
            "nextAccountsMadeUpTo": "2025-03-31",
            "accountsDueDate": "2025-12-31",
        })
        self.assertEqual(company.company_number, "01234567")
        self.assertEqual(company.name, "Browns Garage (Haywards Heath) Ltd")
        self.assertEqual(company.accounts_next_made_up_to, date(2025, 3, 31))
        self.assertEqual(company.accounts_due, date(2025, 12, 31))
        self.assertEqual(company.next_deadline_year, 2025)
        self.assertTrue(company.is_active)

    def test_nested_companies_house_style_payload(self):
        company = Company.from_api({
            "company_number": "SC123456",
            "company_name": "Highland Widgets Ltd",
            "company_status": "active",
            "accounts": {"next_made_up_to": "2026-01-31",
                         "next_due": "2026-10-31",
                         "last_made_up_to": "2025-01-31"},
            "confirmation_statement": {"next_made_up_to": "2026-02-14",
                                       "next_due": "2026-02-28"},
        })
        self.assertEqual(company.company_number, "SC123456")
        self.assertEqual(company.accounts_next_made_up_to, date(2026, 1, 31))
        self.assertEqual(company.accounts_due, date(2026, 10, 31))
        self.assertEqual(company.accounts_last_made_up_to, date(2025, 1, 31))
        self.assertEqual(company.confirmation_due, date(2026, 2, 28))

    def test_alternative_spellings(self):
        company = Company.from_api({
            "registeredNumber": "00000042",
            "registeredName": "Alt Names Ltd",
            "yearEnd": "2025-09-30",
            "accountsFilingDeadline": "2026-06-30",
        })
        self.assertEqual(company.company_number, "00000042")
        self.assertEqual(company.accounts_next_made_up_to, date(2025, 9, 30))
        self.assertEqual(company.accounts_due, date(2026, 6, 30))

    def test_dissolved_and_insolvent_statuses(self):
        for status in ("Dissolved", "struck off", "Struck Off and Dissolved"):
            with self.subTest(status=status):
                self.assertTrue(Company.from_api({"status": status}).is_dissolved)
        liquidating = Company.from_api({"status": "In Liquidation"})
        self.assertTrue(liquidating.in_insolvency)
        self.assertFalse(liquidating.is_dissolved)
        self.assertFalse(Company.from_api({"status": "Active"}).is_dissolved)

    def test_id_falls_back_to_company_number(self):
        company = Company.from_api({"companyNumber": "1234567"})
        self.assertEqual(company.company_id, "01234567")

    def test_raw_payload_is_kept(self):
        payload = {"companyNumber": "1", "somethingNew": {"we": "did not expect"}}
        self.assertEqual(Company.from_api(payload).raw, payload)

    def test_empty_payload_is_harmless(self):
        company = Company.from_api({})
        self.assertEqual(company.company_number, "")
        self.assertIsNone(company.accounts_due)
        self.assertIsNone(company.next_deadline_year)


class OfficerTest(unittest.TestCase):
    def test_current_director(self):
        officer = Officer.from_api({
            "name": "M Hunt", "officerRole": "Director",
            "appointedOn": "2019-04-01", "resignedOn": None,
        })
        self.assertTrue(officer.is_director)
        self.assertTrue(officer.is_current)
        self.assertEqual(officer.appointed_on, date(2019, 4, 1))

    def test_resigned_secretary(self):
        officer = Officer.from_api({
            "forename": "Jo", "surname": "Bloggs", "role": "Secretary",
            "resignationDate": "2023-01-31",
        })
        self.assertEqual(officer.name, "Jo Bloggs")
        self.assertFalse(officer.is_director)
        self.assertFalse(officer.is_current)


class ShareholderTest(unittest.TestCase):
    def test_percentage_supplied_by_api(self):
        holder = Shareholder.from_api({"name": "M Hunt", "percentageHeld": "60"})
        self.assertEqual(holder.percentage, 60.0)
        self.assertFalse(holder.is_corporate)

    def test_percentages_computed_from_share_counts(self):
        holders = compute_percentages([
            Shareholder.from_api({"name": "A", "numberOfShares": 75}),
            Shareholder.from_api({"name": "B", "numberOfShares": 25}),
        ])
        self.assertEqual([h.percentage for h in holders], [75.0, 25.0])

    def test_corporate_detected_from_name_and_flag(self):
        self.assertTrue(Shareholder.from_api({"name": "Digivolve Holdings Ltd"}).is_corporate)
        self.assertTrue(Shareholder.from_api({"name": "X", "isCorporate": True}).is_corporate)
        self.assertFalse(
            Shareholder.from_api({"name": "Ltd Person", "shareholderType": "Individual"})
            .is_corporate)

    def test_zero_shares_does_not_divide_by_zero(self):
        holders = compute_percentages([Shareholder.from_api({"name": "A"})])
        self.assertIsNone(holders[0].percentage)


if __name__ == "__main__":
    unittest.main()
