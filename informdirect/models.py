"""Typed records built from API payloads of not-yet-confirmed shape.

Field names are matched fuzzily (case, punctuation and nesting insensitive)
against a list of aliases, the same approach the Limited Companies Tracker
already uses for spreadsheet headers. Anything unrecognised stays available on
`.raw`, so no data is lost even when a field name is a surprise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

DISSOLVED_STATUSES = (
    "dissolved", "struckoff", "struckoffanddissolved", "removed", "closed",
    "inactive", "ceased",
)
LIQUIDATION_STATUSES = (
    "liquidation", "inliquidation", "administration", "inadministration",
    "receivership", "voluntaryarrangement",
)

_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y",
)


def norm_key(value):
    return _NON_ALNUM.sub("", str(value).lower())


def flatten(payload, _prefix="", _out=None, _depth=0):
    """Normalised-key lookup table for a nested payload.

    A nested value is registered under both its own key and parent+key, so
    {"accounts": {"nextMadeUpTo": x}} is findable as `nextmadeupto` and
    `accountsnextmadeupto`.
    """
    out = {} if _out is None else _out
    if not isinstance(payload, dict) or _depth > 4:
        return out
    for key, value in payload.items():
        nkey = norm_key(key)
        combined = _prefix + nkey
        if isinstance(value, dict):
            flatten(value, combined, out, _depth + 1)
        else:
            out.setdefault(combined, value)
            if _prefix:
                out.setdefault(nkey, value)
    return out


def pick(table, aliases):
    for alias in aliases:
        value = table.get(norm_key(alias))
        if value not in (None, "", []):
            return value
    return None


def parse_date(value):
    if value in (None, "", []):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    iso = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(iso).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    return None


def parse_number(value):
    if value in (None, "", []):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "").replace("£", "")
    try:
        return float(text)
    except ValueError:
        return None


def clean_company_number(value):
    """Companies House numbers are 8 chars, zero-padded, letters uppercased."""
    if value in (None, ""):
        return ""
    text = re.sub(r"[^0-9A-Za-z]", "", str(value)).upper()
    if not text:
        return ""
    if text.isdigit():
        return text.zfill(8)
    return text


@dataclass
class Company:
    company_id: str = ""
    company_number: str = ""
    name: str = ""
    status: str = ""
    jurisdiction: str = ""
    incorporation_date: date = None
    accounts_next_made_up_to: date = None
    accounts_due: date = None
    accounts_last_made_up_to: date = None
    confirmation_next_made_up_to: date = None
    confirmation_due: date = None
    raw: dict = field(default_factory=dict)

    ALIASES = {
        "company_id": ("id", "companyId", "informDirectId", "guid", "key", "uid"),
        "company_number": (
            "companyNumber", "registeredNumber", "registrationNumber",
            "companiesHouseNumber", "crn", "number",
        ),
        "name": ("companyName", "registeredName", "legalName", "name"),
        "status": ("companyStatus", "status", "state", "registerStatus"),
        "jurisdiction": ("jurisdiction", "country", "countryOfOrigin"),
        "incorporation_date": (
            "incorporationDate", "dateOfIncorporation", "incorporatedOn",
            "dateIncorporated",
        ),
        "accounts_next_made_up_to": (
            "nextAccountsMadeUpTo", "accountsNextMadeUpTo", "accountsNextMadeUpToDate",
            "nextAccountsPeriodEnd", "accountingReferenceDate",
            "nextAccountingReferenceDate", "accountsMadeUpTo", "yearEnd",
            "financialYearEnd", "accountsNextPeriodEndOn",
        ),
        "accounts_due": (
            "accountsDueDate", "nextAccountsDue", "accountsNextDue", "accountsDue",
            "nextAccountsDueDate", "accountsFilingDeadline", "accountsNextDueOn",
            "accountsDeadline",
        ),
        "accounts_last_made_up_to": (
            "lastAccountsMadeUpTo", "accountsLastMadeUpTo", "accountsLastMadeUpToDate",
            "lastAccountsPeriodEnd", "accountsLastMadeUpOn",
        ),
        "confirmation_next_made_up_to": (
            "nextConfirmationStatementMadeUpTo", "confirmationStatementMadeUpTo",
            "confirmationStatementNextMadeUpTo", "confirmationStatementNextMadeUpToOn",
            "annualReturnMadeUpTo",
        ),
        "confirmation_due": (
            "confirmationStatementDueDate", "nextConfirmationStatementDue",
            "confirmationStatementNextDue", "confirmationStatementDue",
            "confirmationStatementNextDueOn",
        ),
    }

    DATE_FIELDS = (
        "incorporation_date", "accounts_next_made_up_to", "accounts_due",
        "accounts_last_made_up_to", "confirmation_next_made_up_to",
        "confirmation_due",
    )

    @classmethod
    def from_api(cls, payload):
        table = flatten(payload)
        kwargs = {}
        for name, aliases in cls.ALIASES.items():
            value = pick(table, aliases)
            if name in cls.DATE_FIELDS:
                kwargs[name] = parse_date(value)
            elif value is None:
                kwargs[name] = ""
            else:
                kwargs[name] = str(value).strip()
        kwargs["company_number"] = clean_company_number(kwargs.get("company_number"))
        if not kwargs.get("company_id"):
            kwargs["company_id"] = kwargs["company_number"]
        kwargs["raw"] = payload if isinstance(payload, dict) else {}
        return cls(**kwargs)

    @property
    def status_key(self):
        return norm_key(self.status)

    @property
    def is_dissolved(self):
        key = self.status_key
        return any(marker in key for marker in DISSOLVED_STATUSES)

    @property
    def in_insolvency(self):
        key = self.status_key
        return any(marker in key for marker in LIQUIDATION_STATUSES)

    @property
    def is_active(self):
        return not self.is_dissolved

    @property
    def next_deadline_year(self):
        """Calendar year of the next accounts deadline - the tracker's key."""
        if self.accounts_due:
            return self.accounts_due.year
        return None

    def label(self):
        return f"{self.company_number} {self.name}".strip()


@dataclass
class Officer:
    name: str = ""
    role: str = ""
    appointed_on: date = None
    resigned_on: date = None
    date_of_birth: str = ""
    nationality: str = ""
    raw: dict = field(default_factory=dict)

    ALIASES = {
        "name": ("name", "fullName", "officerName", "displayName"),
        "role": ("role", "officerRole", "position", "appointmentType", "type"),
        "appointed_on": ("appointedOn", "appointmentDate", "dateAppointed", "appointed"),
        "resigned_on": ("resignedOn", "resignationDate", "dateResigned", "ceasedOn",
                        "terminatedOn", "resigned"),
        "date_of_birth": ("dateOfBirth", "dob", "birthDate"),
        "nationality": ("nationality",),
    }
    DATE_FIELDS = ("appointed_on", "resigned_on")

    @classmethod
    def from_api(cls, payload):
        table = flatten(payload)
        kwargs = {}
        for name, aliases in cls.ALIASES.items():
            value = pick(table, aliases)
            if name in cls.DATE_FIELDS:
                kwargs[name] = parse_date(value)
            else:
                kwargs[name] = "" if value is None else str(value).strip()
        if not kwargs["name"]:
            first = pick(table, ("forename", "firstName", "forenames", "givenName"))
            last = pick(table, ("surname", "lastName", "familyName"))
            kwargs["name"] = " ".join(p for p in (first, last) if p).strip()
        kwargs["raw"] = payload if isinstance(payload, dict) else {}
        return cls(**kwargs)

    @property
    def is_current(self):
        return self.resigned_on is None

    @property
    def is_director(self):
        return "director" in norm_key(self.role)


@dataclass
class Shareholder:
    name: str = ""
    share_class: str = ""
    shares_held: float = None
    percentage: float = None
    is_corporate: bool = False
    raw: dict = field(default_factory=dict)

    ALIASES = {
        "name": ("name", "shareholderName", "memberName", "fullName", "holderName"),
        "share_class": ("shareClass", "class", "classOfShares", "shareType"),
        "shares_held": ("sharesHeld", "numberOfShares", "shares", "quantity",
                        "numberHeld", "holding"),
        "percentage": ("percentage", "percentageHeld", "shareholdingPercentage",
                       "percentageOfShares", "percent"),
    }

    @classmethod
    def from_api(cls, payload):
        table = flatten(payload)
        kwargs = {
            "name": str(pick(table, cls.ALIASES["name"]) or "").strip(),
            "share_class": str(pick(table, cls.ALIASES["share_class"]) or "").strip(),
            "shares_held": parse_number(pick(table, cls.ALIASES["shares_held"])),
            "percentage": parse_number(pick(table, cls.ALIASES["percentage"])),
        }
        corporate = pick(table, ("isCorporate", "corporate", "isCompany", "entityType",
                                 "shareholderType", "type"))
        kwargs["is_corporate"] = _looks_corporate(corporate, kwargs["name"])
        kwargs["raw"] = payload if isinstance(payload, dict) else {}
        return cls(**kwargs)


_CORPORATE_SUFFIXES = (
    "limited", "ltd", "plc", "llp", "lp", "incorporated", "inc", "holdings",
    "group", "company", "trustees", "cic",
)


def _looks_corporate(flag, name):
    if isinstance(flag, bool):
        return flag
    if flag not in (None, ""):
        key = norm_key(flag)
        if key in ("true", "yes", "1", "corporate", "company", "corporateentity"):
            return True
        if key in ("false", "no", "0", "individual", "person", "natural"):
            return False
    key = norm_key(name)
    return any(key.endswith(suffix) for suffix in _CORPORATE_SUFFIXES)


def compute_percentages(shareholders):
    """Fill in `percentage` from share counts when the API does not supply it."""
    total = sum(s.shares_held or 0.0 for s in shareholders)
    if total <= 0:
        return shareholders
    for holder in shareholders:
        if holder.percentage is None and holder.shares_held is not None:
            holder.percentage = round(holder.shares_held / total * 100.0, 6)
    return shareholders
