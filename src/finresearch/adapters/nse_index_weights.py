"""The Nifty 50's members with NSE's free-float market capitalisation: the benchmark weights of the Brinson
attribution (#348, portfolio.attribution).

Sources (both NSE, checked 22-Oct-2026 after the close with one request each; structure only, nothing kept):
- the member list: https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv, columns `Company Name,
  Industry,Symbol,Series,ISIN Code` (50 rows). `Industry` is NSE's sector level ("Information Technology"), the same
  field as a quote's `secInfo.sector`, so holdings outside the index are classified on the same level;
- each member's free-float market cap: the quote API's `tradeInfo.ffmc` (rupees, at `tradeInfo.lastPrice`), read
  through the caller's `quote(symbol)` (adapters.nse.Quote.ffmc), so the app's quote cache and NSE budget apply.

NSE's `/api/equity-stockIndices` (the index page's JSON) answered HTTP 404 on 22-Oct-2026 and is not used. NSE
publishes no index weights directly: a member's weight is its share of the members' free-float market cap (the index
is free-float market-cap weighted; NSE Indices' capping is not applied). `ffmc` and `last_price` come from the same quote, so
during market hours both are live and attribution.Member.mcap_end rescales the cap to the stored close. A member whose quote fails keeps
`ffmc=None` with a reason (unknown, never 0) and is listed in `unreachable`. Nothing is stored in the database or
committed (owner decision #362: personal use only); tests use hand-made synthetic files.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

INDEX = "NIFTY 50"
LIST_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
LIST_TTL_S = 86400  # the list changes at the semi-annual reconstitution
HEADER = ["Company Name", "Industry", "Symbol", "Series", "ISIN Code"]


class IndexWeightsError(RuntimeError):
    pass


@dataclass(frozen=True)
class Constituent:
    symbol: str
    name: str
    sector: str | None
    isin: str | None


@dataclass(frozen=True)
class IndexMember:
    symbol: str
    name: str
    isin: str | None
    sector: str | None
    ffmc: float | None  # free-float market cap in rupees at `last_price` (None: unknown)
    last_price: float | None
    reason: str | None = None


@dataclass
class IndexWeights:
    index: str
    as_of: datetime | None
    url: str
    fetched_at: datetime | None
    members: list[IndexMember]
    unreachable: list[str] = field(default_factory=list)  # members whose quote failed


def _is_list(text: str) -> bool:
    return [c.strip() for c in text.lstrip("﻿").split("\n", 1)[0].split(",")] == HEADER


def parse_constituents(text: str) -> list[Constituent]:
    if not _is_list(text):
        raise IndexWeightsError("not NSE's index constituent list (unexpected header)")
    out = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        sym = (r.get("Symbol") or "").strip()
        if sym:
            out.append(Constituent(sym, (r.get("Company Name") or sym).strip(), (r.get("Industry") or "").strip() or None,
                                   (r.get("ISIN Code") or "").strip() or None))  # fmt: skip
    if not out:
        raise IndexWeightsError("NSE's index constituent list has no constituents")
    return out


def _pos(v: Any) -> float | None:
    return None if v is None or v <= 0 else float(v)


async def weights(members: Sequence[Constituent], quote: Callable[[str], Awaitable[Any]], *, url: str,
                  index: str = INDEX) -> IndexWeights:  # fmt: skip
    """Each member with the free-float market cap and price of its quote. A failed quote is that member's reason."""
    out, failed = [], []
    for c in members:
        try:
            q = await quote(c.symbol)
            ffmc, last, why = _pos(getattr(q, "ffmc", None)), _pos(getattr(q, "last_price", None)), None
            if ffmc is None:
                why = "NSE gave no free-float market cap"
        except Exception as e:
            ffmc, last, why = None, None, f"its NSE quote failed: {type(e).__name__}"
            failed.append(c.symbol)
        out.append(IndexMember(c.symbol, c.name, c.isin, c.sector, ffmc, last, why))
    return IndexWeights(index, None, url, None, out, failed)


class NiftyWeightsClient:
    """Reads the member list through PoliteClient (disk-cached for a day when it is the list, never a block page)."""

    def __init__(self, client: Any = None) -> None:
        from finresearch.adapters.http import PoliteClient

        self._own = client is None
        self.http = client or PoliteClient()

    async def __aenter__(self) -> NiftyWeightsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own:
            await self.http.aclose()

    async def nifty50(self, quote: Callable[[str], Awaitable[Any]]) -> IndexWeights:
        got = await self.http.get(LIST_URL, cache_ttl=LIST_TTL_S, cache_if=lambda f: _is_list(f.text))
        if not got.ok:
            raise IndexWeightsError(f"NSE index constituent list HTTP {got.status}")
        iw = await weights(parse_constituents(got.text), quote, url=LIST_URL)
        iw.fetched_at = iw.as_of = got.record.fetched_at
        return iw
