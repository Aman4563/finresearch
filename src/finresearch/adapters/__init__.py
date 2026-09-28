"""Source adapters: polite HTTP plus NSE (IPO subscription data) and SEBI (offer-document filings)."""

from finresearch.adapters.http import Fetched, FetchRecord, PoliteClient
from finresearch.adapters.nse import (
    CategorySubscription,
    DemandGraph,
    IpoDetail,
    IpoIssue,
    NseClient,
    NseError,
    PastIssue,
    SubscriptionSnapshot,
    rebase_times,
)
from finresearch.adapters.sebi import Filing, SebiClient, SebiError

__all__ = [
    "CategorySubscription",
    "DemandGraph",
    "FetchRecord",
    "Fetched",
    "Filing",
    "IpoDetail",
    "IpoIssue",
    "NseClient",
    "NseError",
    "PastIssue",
    "PoliteClient",
    "SebiClient",
    "SebiError",
    "SubscriptionSnapshot",
    "rebase_times",
]
