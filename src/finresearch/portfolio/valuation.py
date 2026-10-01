"""Valuation: live prices (NSE/BSE quotes, AMFI NAVs), P&L, XIRR per holding and overall, allocation and dividends.

Prices come from the app's polite, cached clients (api.markets.MarketSources) — one AMFI NAVAll file for every fund
and one quote per listed stock, fetched one at a time. A price that cannot be fetched degrades that row ("no quote")
and never fails the page; a fund falls back to the NAV printed on its last imported statement, with that date.

Market-cap buckets follow SEBI's definition (circular 6-Oct-2017: large = top 100 companies by full market cap,
mid = 101-250, small = the rest) with the cut-offs of AMFI's latest list we could verify: six months ended
31-Dec-2025 (rank 100 Dr. Reddy's ₹1,05,173.87 cr, rank 250 Global Health ₹34,758.38 cr; AMFI xlsx
AverageMarketCapitalization31Dec2025, read 30-Sep-2026). The Jun-2026 list could not be fetched, so buckets near a
cut-off may be stale. Market cap = last price × issued shares (NSE quote), else BSE's published market cap.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fincalc.funds import xirr

CAP_LIST = {
    "as_of": "2025-12-31",
    "large_min_cr": Decimal("105173.87"),
    "mid_min_cr": Decimal("34758.38"),
    "source": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx",
    "note": "AMFI list for the six months ended 31-Dec-2025 (the Jun-2026 list could not be verified)",
}
MIN_XIRR_DAYS = 60  # annualising a return over a few weeks is misleading: show the absolute return instead


@dataclass
class PriceInfo:
    price: Decimal | None = None
    as_of: str | None = None
    source: str | None = None
    industry: str | None = None
    market_cap_cr: Decimal | None = None
    category: str | None = None
    scheme_code: str | None = None
    error: str | None = None


def cap_bucket(market_cap_cr: Decimal | None) -> str:
    if market_cap_cr is None:
        return "Unclassified"
    if market_cap_cr >= CAP_LIST["large_min_cr"]:
        return "Large cap"
    if market_cap_cr >= CAP_LIST["mid_min_cr"]:
        return "Mid cap"
    return "Small cap"


def fund_cap_bucket(category: str | None, tax_class: str) -> str:
    c = (category or "").lower()
    if tax_class != "equity":
        return "Not equity"
    for key, label in (("large & mid", "Large & mid (fund)"), ("large cap", "Large cap"), ("mid cap", "Mid cap"),
                       ("small cap", "Small cap")):  # fmt: skip
        if key in c:
            return label
    return "Multi / flexi (fund)"


ASSET_LABEL = {"equity": "Equity", "debt_mf": "Debt funds", "other_mf": "Gold & international funds",
               "sgb": "Sovereign Gold Bonds", "other": "Other"}  # fmt: skip


def asset_label(asset_type: str, tax_class: str) -> str:
    if tax_class == "equity":
        return "Equity funds" if asset_type == "mf" else "Stocks"
    return ASSET_LABEL.get(tax_class, "Other")


# --------------------------------------------------------------------------- prices
QUOTE_CONCURRENCY = (
    4  # parallel quote tasks; the shared polite client still spaces NSE requests (2 per second)
)
OnPrice = Callable[[int, "PriceInfo"], Any]


def instrument_of(h: Any, isin_map: dict[str, Any] | None = None) -> tuple[str | None, str, Any]:
    """(quote id, exchange, listing) for a stock holding: its NSE symbol, else the listing's, else its BSE code."""
    listing = (isin_map or {}).get((h.isin or "").upper())
    sym, exch = h.nse_symbol, "NSE"
    if sym and re.fullmatch(
        r"\d{5,7}(\.0)?", sym
    ):  # a BSE scrip code stored as the symbol (older tradebook imports)
        return sym.split(".")[0], "BSE", listing
    if not sym and listing is not None:
        sym, exch = (listing.nse_symbol, "NSE") if listing.nse_symbol else (listing.bse_code, "BSE")
    if not sym and h.bse_code:
        sym, exch = h.bse_code, "BSE"
    return sym, exch, listing


def bse_fallback(h: Any, listing: Any) -> str | None:
    """The BSE code to try when NSE has no price for a stock (BSE-only trading, suspended on NSE)."""
    return h.bse_code or (getattr(listing, "bse_code", None) if listing is not None else None)


def broker_statement_price(h: Any, why: str) -> PriceInfo:
    """Last resort for a stock no exchange prices (unlisted shares, an NCD without trades): the closing price on the
    broker's last holdings statement, with its date and the reason the live price is missing."""
    sp = (h.meta or {}).get("statement_price") or {}
    if sp.get("price"):
        return PriceInfo(Decimal(sp["price"]), sp.get("day"), f"{sp.get('source') or 'broker'} statement close",
                         error=why)  # fmt: skip
    return PriceInfo(error=why)


def price_from_quote(q: Any, exch: str, listing: Any = None) -> PriceInfo:
    """The holding's price is the display price (fincalc.price): the last trade in session, the official close after
    it, the previous close when there has been no trade."""
    from finresearch.fincalc.price import price_view

    v = price_view(q, exchange=exch)
    price = v.price
    mcap = getattr(q, "market_cap", None)
    mcap_cr = (mcap / Decimal(10**7)) if mcap else None
    if mcap_cr is None and price and q.issued_shares:
        mcap_cr = price * q.issued_shares / Decimal(10**7)
    if mcap_cr is None and listing is not None:
        mcap_cr = listing.market_cap_cr
    return PriceInfo(
        price,
        q.as_of.isoformat() if q.as_of else None,
        f"{exch} quote: {v.label.lower()}",
        q.industry,
        mcap_cr,
    )


def fund_prices(funds: Sequence[Any], rows: list, err: str | None) -> dict[int, PriceInfo]:
    by_code = {r.code: r for r in rows}
    by_isin = {i: r for r in rows for i in (r.isin_growth, r.isin_reinvest) if i}
    out = {}
    for h in funds:
        r = by_code.get(h.scheme_code or "") or by_isin.get(h.isin or "")
        if r is not None and r.nav is not None:
            out[h.id] = PriceInfo(r.nav, r.day.isoformat() if r.day else None, "AMFI NAVAll", category=r.category,
                                  scheme_code=r.code)  # fmt: skip
        else:
            out[h.id] = _statement_price(h, err or "not found in AMFI's NAV file (set the scheme code)")
    return out


async def fetch_prices(holdings: Sequence[Any], *, quote: Callable[[str, str], Awaitable[Any]],
                       scheme_rows: Callable[[], Awaitable[list]] | None,
                       listings: Callable[[], Awaitable[Any]] | None = None, on_price: OnPrice | None = None,
                       concurrency: int = QUOTE_CONCURRENCY) -> dict[int, PriceInfo]:  # fmt: skip
    """holding id -> PriceInfo, fetched concurrently: one AMFI NAVAll download prices every fund while the stock
    quotes run, at most `concurrency` at a time, one request per distinct instrument (a stock held in two accounts
    is quoted once). `quote(id, exchange)` should be cached and rate-limited by the caller (the API passes a shared
    polite client behind a 10-minute cache). `on_price(holding id, PriceInfo)` is called as each price arrives."""
    out: dict[int, PriceInfo] = {}

    def emit(hid: int, p: PriceInfo) -> None:
        out[hid] = p
        if on_price is not None:
            on_price(hid, p)

    funds = [h for h in holdings if h.asset_type == "mf"]
    stocks = [h for h in holdings if h.asset_type == "stock"]

    async def price_funds() -> None:
        if not funds:
            return
        rows, err = [], "AMFI NAVs not loaded"
        if scheme_rows is not None:
            try:
                rows, err = await scheme_rows(), None
            except Exception as e:
                err = f"AMFI NAVs unavailable ({type(e).__name__})"
        for hid, p in fund_prices(funds, rows, err).items():
            emit(hid, p)

    async def price_stocks() -> None:
        isin_map: dict[str, Any] = {}
        if listings is not None and any(not h.nse_symbol and not h.bse_code and h.isin for h in stocks):
            try:
                isin_map = {r.isin.upper(): r for r in (await listings()).rows}
            except Exception:
                isin_map = {}
        groups: dict[tuple[str, str], list[tuple[Any, Any]]] = {}
        for h in stocks:
            sym, exch, listing = instrument_of(h, isin_map)
            if not sym:
                emit(
                    h.id,
                    broker_statement_price(h, "no NSE symbol or BSE code: set one to price this holding"),
                )
            else:
                groups.setdefault((sym, exch), []).append((h, listing))
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(key: tuple[str, str], members: list[tuple[Any, Any]]) -> None:
            sym, exch = key
            err = None
            async with sem:
                try:
                    q = await quote(sym, exch)
                except Exception as e:
                    q, err = None, f"no quote from {exch} ({type(e).__name__})"
                p = price_from_quote(q, exch, members[0][1]) if q is not None else PriceInfo(error=err)
                alt = bse_fallback(*members[0]) if exch == "NSE" else None
                if p.price is None and alt:
                    try:
                        q2 = await quote(alt, "BSE")
                        p2 = price_from_quote(q2, "BSE", members[0][1])
                        if p2.price is not None:
                            p = p2
                    except Exception:  # keep the NSE result and its reason
                        pass
            for h, _listing in members:
                one_p = p if p.price is not None else broker_statement_price(
                    h, p.error or f"{exch} has no price for {sym}")  # fmt: skip
                if one_p.price is None and one_p.error is None:
                    one_p.error = f"{exch} has no price for {sym}"
                emit(h.id, one_p)

        await asyncio.gather(*(one(k, m) for k, m in groups.items()))

    await asyncio.gather(price_funds(), price_stocks())
    for h in holdings:
        if h.id not in out:
            emit(h.id, PriceInfo(error="no price source for this kind of holding"))
    return out


class QuoteBatch:
    """Live quotes for one valuation over ONE NSE session and ONE BSE client, instead of a new client (and a fresh
    quote-page warm-up) per stock. NSE's quote page is fetched once for cookies; every quote then shares the client's
    host rate limit (adapters.http: 2 requests per second to nseindia.com), so concurrency overlaps network latency
    without raising the request rate. A refused quote re-warms the session once (serialised) and retries."""

    def __init__(self) -> None:
        self._nse: Any = None
        self._bse: Any = None
        self._lock = asyncio.Lock()
        self._warm_gen = 0

    async def __aenter__(self) -> QuoteBatch:
        return self

    async def __aexit__(self, *exc: object) -> None:
        for c in (self._nse, self._bse):
            if c is not None:
                await c.__aexit__(None, None, None)

    async def _warm(self, symbol: str, seen_gen: int) -> None:
        async with self._lock:
            if self._nse is None:
                from finresearch.adapters.nse import NseClient

                self._nse = await NseClient().__aenter__()
            if self._warm_gen == seen_gen:  # nobody re-warmed while we waited
                await self._nse.warm_quote_session(symbol)
                self._warm_gen += 1

    async def quote(self, symbol: str, exchange: str = "NSE") -> Any:
        if exchange == "BSE":
            async with self._lock:
                if self._bse is None:
                    from finresearch.adapters.bse_equity import BseEquity

                    self._bse = await BseEquity().__aenter__()
            q = await self._bse.quote(symbol)
            if q is None:
                raise LookupError(f"BSE has no quote for scrip {symbol}")
            return q
        if self._warm_gen == 0:
            await self._warm(symbol, 0)
        gen = self._warm_gen
        try:
            return await self._nse.quote(symbol, warm=False)
        except Exception:
            await self._warm(symbol, gen)
            return await self._nse.quote(symbol, warm=False)


def _statement_price(h: Any, why: str) -> PriceInfo:
    sn = (h.meta or {}).get("statement_nav") or {}
    if sn.get("nav"):
        return PriceInfo(
            Decimal(sn["nav"]), sn.get("day"), f"{sn.get('source') or 'CAS'} statement NAV", error=why
        )
    return PriceInfo(error=why)


# --------------------------------------------------------------------------- XIRR
def xirr_or_reason(flows: list[tuple[date, Decimal]], today: date) -> tuple[float | None, str | None]:
    if not flows:
        return None, "no cash flows"
    first = min(d for d, _ in flows)
    if (today - first).days < MIN_XIRR_DAYS:
        return None, f"held under {MIN_XIRR_DAYS} days: see the absolute return"
    try:
        return float(xirr(flows)), None
    except ValueError as e:
        return None, str(e)


def cash_flows(
    txns: Sequence[Any], value: Decimal | None, today: date
) -> tuple[list[tuple[date, Decimal]], str | None]:
    """Investor cash flows for XIRR: buys out (amount + charges), sales and paid-out dividends in, the current value
    in today. Reinvested dividends are neither (the units were bought with the dividend). An opening balance with no
    cost makes XIRR impossible."""
    from finresearch.portfolio.lots import superseded_openings

    flows: list[tuple[date, Decimal]] = []
    skip = superseded_openings(txns)  # openings the lots ignore: earlier rows already hold that history
    for i, t in enumerate(txns):
        if i in skip:
            continue
        reinvest = bool((t.meta or {}).get("reinvest"))
        gross = (
            abs(t.amount)
            if t.amount is not None
            else ((t.quantity or 0) * (t.price or 0) if t.price else None)
        )
        if t.kind == "opening":
            if t.price is None or not (t.meta or {}).get("acquired"):
                return [], "opening balance with an unknown cost"
            flows.append((date.fromisoformat(t.meta["acquired"]), -(t.quantity * t.price + (t.charges or 0))))
        elif t.kind == "buy" and not reinvest:
            if gross is None:
                return [], "a purchase without an amount"
            flows.append((t.day, -(gross + (t.charges or 0))))
        elif t.kind == "sell":
            if (
                gross is None
            ):  # dropping it would leave the units' value out of the flows: XIRR would read a loss
                return [], "a sale without an amount"
            flows.append((t.day, gross - (t.charges or 0)))
        elif t.kind == "dividend" and not reinvest and t.amount:
            flows.append((t.day, abs(t.amount)))
    if value:
        flows.append((today, value))
    return flows, None
