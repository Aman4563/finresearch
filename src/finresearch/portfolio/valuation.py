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
async def fetch_prices(holdings: Sequence[Any], *, quote: Callable[[str, str], Awaitable[Any]],
                       scheme_rows: Callable[[], Awaitable[list]] | None,
                       listings: Callable[[], Awaitable[Any]] | None = None) -> dict[int, PriceInfo]:  # fmt: skip
    """holding id -> PriceInfo. `quote(id, exchange)` is MarketSources.get_quote; `scheme_rows` gives AMFI NAVAll."""
    out: dict[int, PriceInfo] = {}
    funds = [h for h in holdings if h.asset_type == "mf"]
    if funds and scheme_rows is not None:
        try:
            rows = await scheme_rows()
        except Exception as e:
            rows, err = [], f"AMFI NAVs unavailable ({type(e).__name__})"
        else:
            err = None
        by_code = {r.code: r for r in rows}
        by_isin = {i: r for r in rows for i in (r.isin_growth, r.isin_reinvest) if i}
        for h in funds:
            r = by_code.get(h.scheme_code or "") or by_isin.get(h.isin or "")
            if r is not None and r.nav is not None:
                out[h.id] = PriceInfo(r.nav, r.day.isoformat() if r.day else None, "AMFI NAVAll", category=r.category,
                                      scheme_code=r.code)  # fmt: skip
            else:
                out[h.id] = _statement_price(h, err or "not found in AMFI's NAV file (set the scheme code)")
    for h in funds:
        out.setdefault(h.id, _statement_price(h, "AMFI NAVs not loaded"))
    isin_map: dict[str, Any] = {}
    stocks = [h for h in holdings if h.asset_type == "stock"]
    if listings is not None and any(not h.nse_symbol and not h.bse_code and h.isin for h in stocks):
        try:
            idx = await listings()
            isin_map = {r.isin.upper(): r for r in idx.rows}
        except Exception:
            isin_map = {}
    for h in stocks:  # one at a time: the polite clients rate-limit anyway
        sym, exch = h.nse_symbol, "NSE"
        listing = isin_map.get((h.isin or "").upper())
        if not sym and listing is not None:
            sym, exch = (listing.nse_symbol, "NSE") if listing.nse_symbol else (listing.bse_code, "BSE")
        if not sym and h.bse_code:
            sym, exch = h.bse_code, "BSE"
        if not sym:
            out[h.id] = PriceInfo(error="no NSE symbol or BSE code: set one to price this holding")
            continue
        try:
            q = await quote(sym, exch)
        except Exception as e:
            out[h.id] = PriceInfo(error=f"no quote from {exch} ({type(e).__name__})")
            continue
        price = q.last_price or q.close_price or q.previous_close
        mcap = getattr(q, "market_cap", None)
        mcap_cr = (mcap / Decimal(10**7)) if mcap else None
        if mcap_cr is None and price and q.issued_shares:
            mcap_cr = price * q.issued_shares / Decimal(10**7)
        if mcap_cr is None and listing is not None:
            mcap_cr = listing.market_cap_cr
        out[h.id] = PriceInfo(
            price, q.as_of.isoformat() if q.as_of else None, f"{exch} quote", q.industry, mcap_cr
        )
        await asyncio.sleep(0)
    for h in holdings:
        out.setdefault(h.id, PriceInfo(error="no price source for this kind of holding"))
    return out


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
    flows: list[tuple[date, Decimal]] = []
    for t in txns:
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
        elif t.kind == "sell" and gross is not None:
            flows.append((t.day, gross - (t.charges or 0)))
        elif t.kind == "dividend" and not reinvest and t.amount:
            flows.append((t.day, abs(t.amount)))
    if value:
        flows.append((today, value))
    return flows, None
