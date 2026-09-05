"""High-level portfolio access - the layer the planner and reconciler call."""

from __future__ import annotations

from .client import Client
from .errors import EndpointNotConfigured, NotFoundError
from .models import (
    Company, Officer, Shareholder, clean_company_number, compute_percentages,
)


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
        payload = _unwrap(response.json())
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

    # -- membership -------------------------------------------------------- #

    def add_company(self, company_number, *, auth_code=None, extra=None):
        """Link one company to the account.

        Confirmed live: POST /companies/add with {"CompanyNumber": "..."}
        returns 201. Without an authentication code the API says so explicitly
        ("Company added with no authentication code."), so `auth_code` carries
        the Companies House code when you have it - Inform Direct needs it
        before it can file for the company.

        One company per call: a payload carrying a list is refused with 429
        ("not meant for bulk uploading"). Re-adding a company already on the
        account raises AlreadyLinkedError (HTTP 422).
        """
        body = {"CompanyNumber": clean_company_number(company_number)}
        if auth_code:
            body["AuthenticationCode"] = auth_code
        if extra:
            body.update(extra)
        response = self.client.call("add_company", json_body=body)
        payload = _unwrap(response.json())
        return Company.from_api(payload) if payload else None

    def remove_company(self, company):
        """Unlink one company from the account. Returns True on success.

        Confirmed live: PUT /companies/delete with {"CompanyNumber": "..."}
        returns 200 {"Message": "Company deleted."}. The verb is PUT, not
        DELETE - DELETE on that path answers 405 with "allow: PUT".

        A company that is not on the account raises NotFoundError.
        """
        number = company.company_number if isinstance(company, Company) else company
        self.client.call("remove_company",
                         json_body={"CompanyNumber": clean_company_number(number)})
        return True

    # -- officers and shares ---------------------------------------------- #
    # Not part of the Integration API as documented - see the
    # "_not_offered_by_the_api" note in config/endpoints.json. The code stays
    # because it is written and tested, and starts working the moment those
    # operations are moved into the map.

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
        """Everything the SA return needs about one company, in one call set.

        Officers and shareholders are not part of the Integration API as
        documented, so those two come back as None (not []) with the reason in
        `unavailable`, rather than raising. An empty list means "the API
        answered and there are none"; None means "the API cannot tell us".
        """
        record = company if isinstance(company, Company) else self.get_company(company)
        if record is None:
            return None

        view = {"company": record, "directors": None, "shareholders": None,
                "unavailable": {}}
        for key, fetch in (("directors", self.directors),
                           ("shareholders", self.shareholders)):
            try:
                view[key] = fetch(record)
            except EndpointNotConfigured as exc:
                view["unavailable"][key] = str(exc)
        return view


def _unwrap(payload):
    """Pull the single record out of whatever envelope it arrived in.

    The live API answers GET /companies/{n} with the same {"Companies": [...]}
    envelope it uses for the list, so a detail fetch has to be unwrapped too.
    """
    if not isinstance(payload, dict):
        return payload
    for key in ("Companies", "companies", "company", "data", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value[0] if value else None
        if isinstance(value, dict):
            return value
    return payload


def _id_of(company):
    if isinstance(company, Company):
        return company.company_id or company.company_number
    return company
