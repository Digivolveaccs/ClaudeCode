"""Write the registry feed the Limited Companies Tracker already consumes.

The tracker matches its headers fuzzily, so these column names line up with the
manual "Your Portfolio -> export companies" download this replaces.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime
from pathlib import Path

COLUMNS = (
    ("Company Number", "company_number"),
    ("Company Name", "name"),
    ("Status", "status"),
    ("Incorporation Date", "incorporation_date"),
    ("Last Accounts Made Up To", "accounts_last_made_up_to"),
    ("Next Accounts Made Up To", "accounts_next_made_up_to"),
    ("Accounts Due Date", "accounts_due"),
    ("Confirmation Statement Made Up To", "confirmation_next_made_up_to"),
    ("Confirmation Statement Due Date", "confirmation_due"),
    ("Inform Direct Id", "company_id"),
)

FILENAME_TEMPLATE = "inform-direct-companies-{stamp}.csv"


def row_for(company):
    row = {}
    for header, attr in COLUMNS:
        row[header] = _fmt(getattr(company, attr, ""))
    return row


def write_tracker_csv(companies, path):
    """Write the feed CSV. `path` may be a file or a directory.

    Passing the tracker's `state/inform direct/` directory writes a dated file
    there, which is exactly what the daily run picks up (newest file wins).
    """
    target = Path(path).expanduser()
    if target.is_dir() or not target.suffix:
        target.mkdir(parents=True, exist_ok=True)
        target = target / FILENAME_TEMPLATE.format(stamp=date.today().isoformat())
    else:
        target.parent.mkdir(parents=True, exist_ok=True)

    companies = sorted(companies, key=lambda c: (c.company_number, c.name))
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=[h for h, _ in COLUMNS])
        writer.writeheader()
        for company in companies:
            writer.writerow(row_for(company))
    tmp.replace(target)
    return target


def write_snapshot_json(companies, path):
    """Full-fidelity snapshot: parsed fields plus each company's raw payload."""
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "retrieved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "count": len(companies),
        "companies": [
            {
                **{attr: _fmt(getattr(c, attr, "")) for _, attr in COLUMNS},
                "raw": c.raw,
            }
            for c in companies
        ],
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    return target


def _fmt(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)
