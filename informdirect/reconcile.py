"""Job reconciliation: registry truth vs the planner's rows.

Implements the rules the Limited Companies Tracker already applies, but sourced
from the API rather than a hand-downloaded CSV:

  row deadline year == registry next-due year -> snap year end + deadline
  row deadline year  < registry next-due year -> that period was filed
  registry next-due year has no row            -> create one
  dissolved / struck off                       -> flag, never roll forward

Plus two the tracker needs in practice: planner rows with no registry match, and
charity rows (not at Companies House) which are verified separately.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import clean_company_number, norm_key, parse_date

SNAP = "snap_dates"
SUBMITTED = "mark_submitted"
CREATE = "create_row"
DISSOLVED = "flag_dissolved"
INSOLVENT = "flag_insolvent"
UNMATCHED = "flag_unmatched"
AHEAD = "flag_row_ahead_of_registry"
CHARITY = "charity_verify"
OK = "ok"

SUBMITTED_STAGE = "8-Submitted"

# Headers the planner might use, matched the same fuzzy way as the tracker.
ROW_ALIASES = {
    "company_number": ("company number", "companynumber", "number", "crn",
                       "registered number", "registration number"),
    "company_name": ("company name", "client", "client name", "name", "company"),
    "year_end": ("year end", "yearend", "ye", "period end", "accounts period end",
                 "made up to", "accounting date"),
    "deadline": ("deadline", "accounts due", "accounts due date", "due date",
                 "filing deadline", "ch deadline"),
    "stage": ("stage", "status", "job stage", "job status", "action status"),
    "notes": ("notes", "note", "comments", "comment"),
}


@dataclass
class PlannerRow:
    company_number: str = ""
    company_name: str = ""
    year_end: date = None
    deadline: date = None
    stage: str = ""
    notes: str = ""
    row_number: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def deadline_year(self):
        return self.deadline.year if self.deadline else None

    @property
    def is_charity(self):
        return "charity" in norm_key(self.notes)

    @property
    def charity_number(self):
        match = re.search(r"charity\s*([0-9]{5,8})", self.notes or "", re.I)
        return match.group(1) if match else ""

    @property
    def is_submitted(self):
        return "submitted" in norm_key(self.stage)


@dataclass
class Action:
    kind: str
    company_number: str
    company_name: str = ""
    reason: str = ""
    row_number: int = 0
    current: dict = field(default_factory=dict)
    proposed: dict = field(default_factory=dict)

    @property
    def needs_human(self):
        return self.kind in (DISSOLVED, INSOLVENT, UNMATCHED, AHEAD, CHARITY)

    def as_dict(self):
        return {
            "kind": self.kind,
            "company_number": self.company_number,
            "company_name": self.company_name,
            "reason": self.reason,
            "row_number": self.row_number,
            "current": {k: _iso(v) for k, v in self.current.items()},
            "proposed": {k: _iso(v) for k, v in self.proposed.items()},
        }


@dataclass
class ReconResult:
    actions: list = field(default_factory=list)
    matched: int = 0
    registry_count: int = 0
    row_count: int = 0

    def of_kind(self, *kinds):
        return [a for a in self.actions if a.kind in kinds]

    @property
    def exceptions(self):
        return [a for a in self.actions if a.needs_human]

    @property
    def automatic(self):
        return [a for a in self.actions if not a.needs_human and a.kind != OK]

    def counts(self):
        tally = {}
        for action in self.actions:
            tally[action.kind] = tally.get(action.kind, 0) + 1
        return dict(sorted(tally.items()))

    def summary(self):
        lines = [
            f"registry companies : {self.registry_count}",
            f"planner rows       : {self.row_count}",
            f"matched            : {self.matched}",
        ]
        for kind, count in self.counts().items():
            lines.append(f"{kind:<19}: {count}")
        return "\n".join(lines)


def reconcile(companies, rows):
    """Compare a registry snapshot against planner rows and return the actions."""
    result = ReconResult(registry_count=len(companies), row_count=len(rows))
    by_number = {}
    for company in companies:
        if company.company_number:
            by_number.setdefault(company.company_number, company)

    rows_by_number = {}
    for row in rows:
        rows_by_number.setdefault(row.company_number, []).append(row)

    for number, group in rows_by_number.items():
        company = by_number.get(number)

        charity_rows = [r for r in group if r.is_charity]
        for row in charity_rows:
            result.actions.append(Action(
                kind=CHARITY,
                company_number=number,
                company_name=row.company_name,
                row_number=row.row_number,
                reason="Charity Commission entity - verify on the charity register, "
                       "not Companies House.",
                current={"charity_number": row.charity_number,
                         "deadline": row.deadline},
            ))
        group = [r for r in group if not r.is_charity]
        if not group:
            continue

        if company is None:
            for row in group:
                result.actions.append(Action(
                    kind=UNMATCHED,
                    company_number=number,
                    company_name=row.company_name,
                    row_number=row.row_number,
                    reason="No company with this number in the Inform Direct "
                           "portfolio. Check the number, or add it to the portfolio.",
                    current={"deadline": row.deadline, "stage": row.stage},
                ))
            continue

        result.matched += 1

        if company.is_dissolved:
            for row in group:
                result.actions.append(Action(
                    kind=DISSOLVED,
                    company_number=number,
                    company_name=company.name or row.company_name,
                    row_number=row.row_number,
                    reason=f"Registry status {company.status!r} - do not roll forward.",
                    current={"stage": row.stage, "deadline": row.deadline},
                ))
            continue

        if company.in_insolvency:
            result.actions.append(Action(
                kind=INSOLVENT,
                company_number=number,
                company_name=company.name,
                row_number=group[0].row_number,
                reason=f"Registry status {company.status!r} - check before working "
                       "the job.",
                current={"stage": group[0].stage},
            ))

        registry_year = company.next_deadline_year
        if registry_year is None:
            for row in group:
                result.actions.append(Action(
                    kind=UNMATCHED,
                    company_number=number,
                    company_name=company.name,
                    row_number=row.row_number,
                    reason="Registry has no accounts due date for this company.",
                    current={"deadline": row.deadline},
                ))
            continue

        covered = False
        for row in group:
            row_year = row.deadline_year
            if row_year is None:
                result.actions.append(Action(
                    kind=SNAP,
                    company_number=number,
                    company_name=company.name,
                    row_number=row.row_number,
                    reason="Row has no deadline - take both dates from the registry.",
                    current={"year_end": row.year_end, "deadline": row.deadline},
                    proposed={"year_end": company.accounts_next_made_up_to,
                              "deadline": company.accounts_due},
                ))
                covered = True
                continue

            if row_year == registry_year:
                covered = True
                if _dates_agree(row, company):
                    result.actions.append(Action(
                        kind=OK,
                        company_number=number,
                        company_name=company.name,
                        row_number=row.row_number,
                        reason="Row already agrees with the registry.",
                        current={"year_end": row.year_end, "deadline": row.deadline},
                    ))
                else:
                    result.actions.append(Action(
                        kind=SNAP,
                        company_number=number,
                        company_name=company.name,
                        row_number=row.row_number,
                        reason="Registry is the authority on year end and deadline.",
                        current={"year_end": row.year_end, "deadline": row.deadline},
                        proposed={"year_end": company.accounts_next_made_up_to,
                                  "deadline": company.accounts_due},
                    ))
            elif row_year < registry_year:
                if row.is_submitted:
                    result.actions.append(Action(
                        kind=OK,
                        company_number=number,
                        company_name=company.name,
                        row_number=row.row_number,
                        reason="Earlier period, already marked submitted.",
                        current={"stage": row.stage, "deadline": row.deadline},
                    ))
                else:
                    result.actions.append(Action(
                        kind=SUBMITTED,
                        company_number=number,
                        company_name=company.name,
                        row_number=row.row_number,
                        reason=f"Registry has moved on to {registry_year}, so the "
                               f"{row_year} period was filed.",
                        current={"stage": row.stage, "deadline": row.deadline},
                        proposed={"stage": SUBMITTED_STAGE},
                    ))
            else:
                covered = True
                result.actions.append(Action(
                    kind=AHEAD,
                    company_number=number,
                    company_name=company.name,
                    row_number=row.row_number,
                    reason=f"Row deadline is {row_year} but the registry expects "
                           f"{registry_year} next. Check the registry feed is current.",
                    current={"deadline": row.deadline},
                    proposed={"deadline": company.accounts_due},
                ))

        if not covered:
            result.actions.append(Action(
                kind=CREATE,
                company_number=number,
                company_name=company.name,
                reason=f"No planner row for the {registry_year} accounts deadline.",
                proposed={"year_end": company.accounts_next_made_up_to,
                          "deadline": company.accounts_due,
                          "company_name": company.name},
            ))

    # Live companies in the portfolio with no planner row at all.
    for number, company in by_number.items():
        if number in rows_by_number or company.is_dissolved:
            continue
        if company.accounts_due is None:
            continue
        result.actions.append(Action(
            kind=CREATE,
            company_number=number,
            company_name=company.name,
            reason="In the Inform Direct portfolio but absent from the planner.",
            proposed={"year_end": company.accounts_next_made_up_to,
                      "deadline": company.accounts_due,
                      "company_name": company.name},
        ))

    result.actions.sort(key=lambda a: (a.kind, a.company_number))
    return result


# -- planner row loading --------------------------------------------------- #

def load_planner_csv(path):
    """Read planner rows from a CSV, matching headers fuzzily."""
    target = Path(path).expanduser()
    with target.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        return rows_from_dicts(reader)


def rows_from_dicts(records):
    rows = []
    for index, record in enumerate(records, start=2):
        table = {norm_key(k): v for k, v in record.items() if k}
        row = PlannerRow(
            company_number=clean_company_number(_pick(table, "company_number")),
            company_name=str(_pick(table, "company_name") or "").strip(),
            year_end=parse_date(_pick(table, "year_end")),
            deadline=parse_date(_pick(table, "deadline")),
            stage=str(_pick(table, "stage") or "").strip(),
            notes=str(_pick(table, "notes") or "").strip(),
            row_number=index,
            raw=dict(record),
        )
        if not row.company_number and not row.company_name:
            continue
        rows.append(row)
    return rows


def _pick(table, field_name):
    for alias in ROW_ALIASES[field_name]:
        value = table.get(norm_key(alias))
        if value not in (None, ""):
            return value
    return None


def _dates_agree(row, company):
    return (
        row.deadline == company.accounts_due
        and (company.accounts_next_made_up_to is None
             or row.year_end == company.accounts_next_made_up_to)
    )


def _iso(value):
    return value.isoformat() if isinstance(value, date) else value


def write_report_csv(result, path):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    headers = ["kind", "company_number", "company_name", "row_number", "reason",
               "current", "proposed"]
    with target.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for action in result.actions:
            data = action.as_dict()
            writer.writerow([
                data["kind"], data["company_number"], data["company_name"],
                data["row_number"] or "", data["reason"],
                _kv(data["current"]), _kv(data["proposed"]),
            ])
    return target


def _kv(mapping):
    return "; ".join(f"{k}={v}" for k, v in mapping.items() if v not in (None, ""))
