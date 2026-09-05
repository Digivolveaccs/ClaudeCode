"""High-level portfolio access - the layer the planner and reconciler call."""

from __future__ import annotations

from .client import Client
from .errors import NotFoundError
from .models import Company, Officer, Shareholder, compute_percentages


class Portfolio:
    def __init__(self, client=None, **client_kwargs):
        self.client = client or Client(**client_kwargs)

    # -- companies -------------------------------------------------------- #

    def iter_companies(self, *, include_dissolved=True, params=None):
        for raw in self.client.paginate("list_companies", params=params):
            company = Company.from_api(raw)
            if not include_dissolved and company.is_dissolved:
                continue
            yield company

    def companies(self, **kwargs):
        return list(self.iter_companies(**kwargs))

    def get_company(self, company_id):
        """Fetch one company by Inform Direct id or company number.

        Falls back to a portfolio scan when a direct lookup 404s, because the id
        the API accepts on the path may not be the company number.
        """
        try:
            response = self.client.call("get_company", path_params={"company_id": company_id})
        except NotFoundError:
            return self.find_by_number(company_id)
        payload = response.json()
        if isinstance(payload, dict):
            inner = payload.get("company") or payload.get("data")
            if isinstance(inner, dict):
                payload = inner
        return Company.from_api(payload) if payload else None

    def find_by_number(self, company_number):
        from .models import clean_company_number

        wanted = clean_company_number(company_number)
        if not wanted:
            return None
        for company in self.iter_companies():
            if company.company_number == wanted:
                return company
        return None

    # -- officers and shares ---------------------------------------------- #

    def officers(self, company, *, current_only=False):
        officers = [
            Officer.from_api(raw)
            for raw in self.client.paginate(
                "list_officers", path_params={"company_id": _id_of(company)}
            )
        ]
        if current_only:
            officers = [o for o in officers if o.is_current]
        return officers

    def directors(self, company, *, current_only=True):
        return [
            o for o in self.officers(company, current_only=current_only)
            if o.is_director
        ]

    def shareholders(self, company):
        holders = [
            Shareholder.from_api(raw)
            for raw in self.client.paginate(
                "list_shareholders", path_params={"company_id": _id_of(company)}
            )
        ]
        return compute_percentages(holders)

    def filings(self, company):
        return list(
            self.client.paginate(
                "list_filings", path_params={"company_id": _id_of(company)}
            )
        )

    # -- close company check (used by the SA data run) --------------------- #

    def close_company_view(self, company):
        """Everything the SA return needs about one company, in one call set."""
        record = company if isinstance(company, Company) else self.get_company(company)
        if record is None:
            return None
        return {
            "company": record,
            "directors": self.directors(record),
            "shareholders": self.shareholders(record),
        }


def _id_of(company):
    if isinstance(company, Company):
        return company.company_id or company.company_number
    return company
