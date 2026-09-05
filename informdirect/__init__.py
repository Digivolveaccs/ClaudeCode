"""Inform Direct API client for the Digivolve planner and job reconciliation.

    from informdirect import Portfolio
    portfolio = Portfolio()
    for company in portfolio.iter_companies():
        print(company.company_number, company.accounts_due)
"""

from .client import Client
from .config import Settings
from .endpoints import EndpointMap
from .errors import (
    AlreadyLinkedError, ApiError, AuthError, ConfigError, EndpointNotConfigured,
    ForbiddenError, InformDirectError, NotFoundError, RateLimitError, ServerError,
    TransportError,
)
from .models import Company, Officer, Shareholder
from .portfolio import Portfolio

__version__ = "1.0.0"

__all__ = [
    "AlreadyLinkedError", "ApiError", "AuthError", "Client", "Company", "ConfigError", "EndpointMap",
    "EndpointNotConfigured", "ForbiddenError", "InformDirectError", "NotFoundError",
    "Officer", "Portfolio", "RateLimitError", "ServerError", "Settings",
    "Shareholder", "TransportError", "__version__",
]
