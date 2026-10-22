"""The Nifty 50's constituents with NSE's free-float market capitalisation, the benchmark weights of the Brinson
attribution (#348, portfolio.attribution).

Source: NSE (National Stock Exchange of India), https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050, the
JSON behind NSE's "Live Equity Market" index page. One GET through the shared NSE budget (adapters.http), kept in
memory by the caller; never stored in the database or committed. Tests use hand-made synthetic payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


class IndexWeightsError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexMember:
    symbol: str
    name: str
    isin: str | None
    sector: str | None
    ffmc: float | None  # free-float market cap in rupees at `last_price` (None: NSE gave none, unknown)
    last_price: float | None


@dataclass
class IndexWeights:
    index: str
    as_of: datetime | None
    url: str
    fetched_at: datetime | None
    members: list[IndexMember]
