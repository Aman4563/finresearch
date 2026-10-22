"""The Nifty 50's constituents with NSE's free-float market capitalisation, the benchmark weights of the Brinson
attribution (#348, portfolio.attribution).

Source: NSE (National Stock Exchange of India), https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%2050, the
JSON behind NSE's "Live Equity Market" index page. One GET through the shared NSE budget (adapters.http), kept in
memory by the caller; never stored in the database or committed. Tests use hand-made synthetic payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


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


INDEX = "NIFTY 50"
API_URL = "https://www.nseindia.com/api/equity-stockIndices"
WARMUP_URL = "https://www.nseindia.com/market-data/live-equity-market"


def _num(v: Any) -> float | None:
    from finresearch.adapters.nse import parse_num

    d = parse_num(v)
    return None if d is None or d <= 0 else float(d)


def parse(data: Any, *, url: str, index: str = INDEX) -> IndexWeights:
    """The members of the index payload (the row for the index itself is not one). A member without a positive
    `ffmc` keeps None (unknown), never 0."""
    from finresearch.adapters.nse import parse_nse_timestamp

    if not isinstance(data, dict) or str(data.get("name", "")).strip().upper() != index:
        raise IndexWeightsError(f"not the {index} index payload")
    members = []
    for r in data.get("data") or []:
        sym = str(r.get("symbol") or "").strip()
        if not sym or sym.upper() == index or r.get("priority") == 1:
            continue
        meta = r.get("meta") or {}
        members.append(IndexMember(symbol=sym, name=str(meta.get("companyName") or sym).strip(),
                                   isin=(str(meta.get("isin") or "").strip() or None), sector=_sector(r, meta),
                                   ffmc=_num(r.get("ffmc")), last_price=_num(r.get("lastPrice"))))  # fmt: skip
    if not members:
        raise IndexWeightsError(f"the {index} payload has no constituents")
    return IndexWeights(index, parse_nse_timestamp(data.get("timestamp")), url, None, members)


class NiftyWeightsClient:
    """One GET of the index payload through the shared NSE budget (NseClient's cookie warm-up and block checks)."""

    def __init__(self, client: Any = None) -> None:
        from finresearch.adapters.nse import NseClient

        self.nse = NseClient(client, warmup_url=WARMUP_URL)

    async def __aenter__(self) -> NiftyWeightsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.nse.aclose()

    async def nifty50(self) -> IndexWeights:
        from finresearch.adapters.nse import NseError

        try:
            data, resp = await self.nse.get_json(API_URL, {"index": INDEX})
        except NseError as e:
            raise IndexWeightsError(str(e)) from e
        iw = parse(data, url=str(resp.record.url))
        iw.fetched_at = resp.record.fetched_at
        return iw


def _sector(row: dict[str, Any], meta: dict[str, Any]) -> str | None:
    """The member's NSE classification as the payload gives it (`meta.industry`); None when absent (unknown)."""
    v = str(meta.get("industry") or "").strip()
    return v or None
