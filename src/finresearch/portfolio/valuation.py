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
import logging
import re
import time as _time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.disclosures.store import bse_only_isin
from finresearch.fincalc.dates import to_ist
from finresearch.fincalc.funds import xirr
from finresearch.fincalc.price import LAST_TRADED, OFFICIAL_CLOSE

log = logging.getLogger(__name__)
GROWW_CLOSE_PROVENANCE = (
    "Groww daily close"  # = adapters.groww_market.CLOSE_PROVENANCE (not imported: no cycle)
)

CAP_LIST = {
    "as_of": "2025-12-31",
    "large_min_cr": Decimal("105173.87"),
    "mid_min_cr": Decimal("34758.38"),
    "source": "https://www.amfiindia.com/Themes/Theme1/downloads/AverageMarketCapitalization31Dec2025.xlsx",
    "note": "AMFI list for the six months ended 31-Dec-2025 (the Jun-2026 list could not be verified)",
}
GROWW_CLOSE = (
    "groww_daily_close"  # PriceInfo.kind of a Groww daily close: never the exchange's official close
)
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
    note: str | None = None  # e.g. the symbol was renamed and the holding was priced under its new one
    kind: str | None = (
        None  # fincalc.price kind (last_traded | official_close | previous_close) or GROWW_CLOSE
    )
    close_pending: bool = False  # after the session, the exchange has not published its official close yet


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
    if listing is not None and not listing.nse_symbol and listing.bse_code and bse_only_isin(h.isin):
        # the ISIN is listed on BSE only: a stored "NSE symbol" (e.g. a broker's "NSE$") is no NSE listing, and NSE's
        # quote API can still answer for it with a price that is not this company's official close (#200)
        return listing.bse_code, "BSE", listing
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
    from datetime import UTC, datetime

    from finresearch.fincalc.price import price_view

    v = price_view(q, exchange=exch, now=datetime.now(UTC))  # an earlier day's quote is never "in session"
    price = v.price
    mcap = getattr(q, "market_cap", None)
    mcap_cr = (mcap / Decimal(10**7)) if mcap else None
    if mcap_cr is None and price and q.issued_shares:
        mcap_cr = price * q.issued_shares / Decimal(10**7)
    if mcap_cr is None and listing is not None:
        mcap_cr = listing.market_cap_cr

    origin = getattr(q, "provenance", None) or f"{exch} quote"  # "Groww LTP" / "Groww daily close" (#267)
    source, kind = f"{origin}: {v.label.lower()}", v.kind
    if origin.startswith("Groww") and v.kind == OFFICIAL_CLOSE:
        # Groww's daily candle close is not documented as the exchange's published close [U]: it is used only when
        # the official close is not available (fetch_prices) and says so (#283)
        from finresearch.adapters.groww_market import CLOSE_FALLBACK_LABEL

        source, kind = CLOSE_FALLBACK_LABEL, GROWW_CLOSE
    return PriceInfo(
        price,
        q.as_of.isoformat() if q.as_of else None,
        source,
        q.industry,
        mcap_cr,
        kind=kind,
        close_pending=v.kind == LAST_TRADED and v.session == "closing",
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
                       concurrency: int = QUOTE_CONCURRENCY, budget_s: float | None = None,
                       prefetch: Any = "groww") -> dict[int, PriceInfo]:  # fmt: skip
    """holding id -> PriceInfo, fetched concurrently: one AMFI NAVAll download prices every fund while the stock
    quotes run, at most `concurrency` at a time, one request per distinct instrument (a stock held in two accounts
    is quoted once). `quote(id, exchange)` should be cached and rate-limited by the caller (the API passes a shared
    polite client behind a 10-minute cache). `on_price(holding id, PriceInfo)` is called as each price arrives.

    `budget_s` bounds the whole call: whatever is still being fetched then is cancelled, and those rows fall back to
    their last statement price (or none) with a "did not answer in time" reason, so one slow source never holds the
    page. Nothing unfinished is cached (the caller's cache stores only completed fetches).

    `prefetch(items) -> {(symbol, exchange): quote}` is asked first for every stock at once: by default Groww's market
    data (`adapters.groww_market.Prefetch`, the user's paid API, #267), which answers only while the Groww connection is
    on and its session valid. A stock it does not answer takes the `quote` path (NSE, then BSE) as before; None turns
    the step off. A Groww-priced stock's industry comes from the last exchange quote seen (`_META`), or one exchange
    quote when there is none yet (unless `prefetch.backfill` is False)."""
    out: dict[int, PriceInfo] = {}
    if prefetch == "groww":
        from finresearch.adapters.groww_market import Prefetch

        prefetch = Prefetch()

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

    index: dict[str, dict[str, Any]] = {}  # the ISIN map, loaded at most once per call
    index_lock = asyncio.Lock()

    async def isin_index() -> dict[str, Any]:
        async with index_lock:
            if "map" not in index:
                try:
                    index["map"] = {r.isin.upper(): r for r in (await listings()).rows} if listings else {}
                except Exception:
                    log.warning("could not load the NSE/BSE listings for ISIN lookups", exc_info=True)
                    index["map"] = {}
            return index["map"]

    async def renamed(sym: str, members: list[tuple[Any, Any]]) -> tuple[Any, str, PriceInfo] | None:
        """NSE has no quote for `sym` (HTTP 404): when a holding carries an ISIN and today's NSE listing of that ISIN
        has another symbol (a rename such as ZOMATO -> ETERNAL keeps the ISIN), price it under the new symbol."""
        isin = next(((h.isin or "").upper() for h, _ in members if h.isin), "")
        if not isin or listings is None:
            return None
        listing = (await isin_index()).get(isin)
        new = getattr(listing, "nse_symbol", None)
        if listing is None or not new or new.upper() == sym.upper():
            return None
        try:
            p = price_from_quote(await quote(new, "NSE"), "NSE", listing)
        except Exception:
            return None
        p.note = (
            f"NSE symbol changed: {sym} is now {new} (matched by ISIN {isin}); update the holding's symbol"
        )
        return listing, new, p

    async def price_stocks() -> None:
        isin_map: dict[str, Any] = {}
        # every stock with an ISIN is matched to its listing: a BSE-only ISIN is priced on BSE even when the holding
        # carries an NSE-looking symbol (instrument_of)
        if listings is not None and any(h.isin for h in stocks):
            isin_map = await isin_index()
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
        pre: dict[tuple[str, str], Any] = {}
        if prefetch is not None and groups:
            try:
                pre = await prefetch(list(groups)) or {}
            except Exception as e:  # Groww failed as a whole: every stock takes the exchange path
                log.info("price prefetch failed (%s): using the exchanges", type(e).__name__)

        async def one(key: tuple[str, str], members: list[tuple[Any, Any]]) -> None:
            from finresearch.adapters.nse import NseNoQuote

            sym, exch = key
            err = None
            no_such_symbol = False
            async with sem:
                q = pre.get(key)
                # after the session Groww answers with its daily close: the exchange's official close comes first, and
                # the Groww close is only the fallback (#283); in session Groww's LTP is the price
                groww_close = q if getattr(q, "provenance", None) == GROWW_CLOSE_PROVENANCE else None
                memo = None
                if groww_close is not None:
                    q = memo = prefetch.official(key) if hasattr(prefetch, "official") else None
                try:
                    q = q if q is not None else await quote(sym, exch)
                except Exception as e:
                    q, err = None, f"no quote from {exch} ({type(e).__name__})"
                    no_such_symbol = exch == "NSE" and isinstance(e, NseNoQuote)
                p = price_from_quote(q, exch, members[0][1]) if q is not None else PriceInfo(error=err)
                if q is not None and q is pre.get(key):
                    await _fill_meta(p, key, quote, getattr(prefetch, "backfill", True))
                elif p.price is not None and p.industry:
                    _META[key] = (_time.monotonic() + META_TTL_S, p.industry, p.market_cap_cr)
                if no_such_symbol and (hit := await renamed(sym, members)) is not None:
                    p = hit[2]
                alt = bse_fallback(*members[0]) if exch == "NSE" else None
                # BSE when NSE has no price, or when NSE's session is over but its official close is not out yet
                # and BSE has published its own official close (the exchanges' closes are their own figures)
                if alt and (p.price is None or p.close_pending):
                    try:
                        q2 = await quote(alt, "BSE")
                        p2 = price_from_quote(q2, "BSE", members[0][1])
                        if p2.price is not None and (p.price is None or p2.kind == OFFICIAL_CLOSE):
                            p = p2
                    except Exception:  # keep the NSE result and its reason
                        pass
                if groww_close is not None:
                    p = after_close(key, p, q, groww_close, memo, members[0][1])
            for h, _listing in members:
                one_p = p if p.price is not None else broker_statement_price(
                    h, p.error or f"{exch} has no price for {sym}")  # fmt: skip
                if one_p.price is None and one_p.error is None:
                    one_p.error = f"{exch} has no price for {sym}"
                emit(h.id, one_p)

        def after_close(
            key: tuple[str, str], p: PriceInfo, q: Any, gq: Any, memo: Any, listing: Any
        ) -> PriceInfo:
            """The price after the session when Groww answered with its daily close `gq`: the exchange's official
            close when there is one (remembered until the next open, and compared with Groww's close of the same
            day on the same exchange), else Groww's close, labelled, unless the exchange already describes a later
            day (Groww has not posted today's candle yet: the exchange's last trade stays, "close not yet out")."""
            gday = to_ist(gq.as_of).date() if gq.as_of else None
            if p.kind == OFFICIAL_CLOSE:
                pday = date.fromisoformat(p.as_of[:10]) if p.as_of else None
                # not BSE's close standing in for NSE's: that one is not remembered, nor compared with Groww's NSE close
                same_exchange = q is not None and (p.source or "").startswith(f"{key[1]} quote")
                if memo is None and same_exchange and pday is not None and (gday is None or pday >= gday):
                    if hasattr(prefetch, "remember_official"):
                        prefetch.remember_official(key, q)
                    if pday == gday:
                        checks.append({"day": pday, "exchange": key[1], "symbol": key[0], "source": p.source,
                                       "groww_close": gq.close_price, "official_close": p.price})  # fmt: skip
                return p
            pday = date.fromisoformat(p.as_of[:10]) if p.price is not None and p.as_of else None
            if pday is not None and gday is not None and pday > gday:
                return p
            g = price_from_quote(gq, key[1], listing)
            g.industry, g.market_cap_cr = p.industry or g.industry, p.market_cap_cr or g.market_cap_cr
            g.note = p.note
            return g

        checks: list[dict[str, Any]] = []
        await asyncio.gather(*(one(k, m) for k, m in groups.items()))
        if checks and hasattr(prefetch, "record"):
            await prefetch.record(checks)

    work = asyncio.gather(price_funds(), price_stocks())
    if budget_s is None:
        await work
    else:
        try:
            await asyncio.wait_for(work, budget_s)
        except TimeoutError:
            late = f"no price within {budget_s:g} s (the exchange or AMFI is slow): retry shortly"
            for h in holdings:
                if h.id not in out and h.asset_type in ("mf", "stock"):
                    emit(
                        h.id,
                        _statement_price(h, late)
                        if h.asset_type == "mf"
                        else broker_statement_price(h, late),
                    )
    for h in holdings:
        if h.id not in out:
            emit(h.id, PriceInfo(error="no price source for this kind of holding"))
    return out


# industry and market cap per (symbol, exchange) from the last exchange quote: Groww's LTP carries neither, and they
# change rarely, so one exchange quote a day is enough for the sector and cap breakdowns of Groww-priced stocks
META_TTL_S = 24 * 3600.0
_META: dict[tuple[str, str], tuple[float, str | None, Decimal | None]] = {}


async def _fill_meta(p: PriceInfo, key: tuple[str, str], quote: Callable[[str, str], Awaitable[Any]],
                     backfill: bool) -> None:  # fmt: skip
    hit = _META.get(key)
    if (hit is None or hit[0] <= _time.monotonic()) and backfill:
        try:
            q = await quote(*key)
            hit = (_time.monotonic() + META_TTL_S, getattr(q, "industry", None), None)
            ex = price_from_quote(q, key[1])
            hit = (hit[0], hit[1], ex.market_cap_cr)
            _META[key] = hit
        except Exception:  # the price stands; only the sector/cap labels stay unknown
            return
    if hit is not None and hit[0] > _time.monotonic():
        p.industry = p.industry or hit[1]
        p.market_cap_cr = p.market_cap_cr or hit[2]


# A cold first load once took over 120 s (reproduced 1-Oct-2026 with one unknown symbol among 21 holdings): NSE's quote
# API answers an unknown symbol with a fast 404, but the batch then re-warmed from that symbol's quote PAGE, which NSE
# never answers (ReadTimeout), and the polite client retried the timeout 3 times at 30 s each. Now cookies come from
# a fixed, always-listed stock's page, a 404 is "no quote" (no re-warm), and every NSE call is bounded.
WARM_SYMBOL = "RELIANCE"  # any quote page's cookies serve every symbol; a NIFTY 50 stock's page always exists
NSE_TIMEOUT_S = 10.0  # per HTTP request on the interactive path (the polite client's default is 30 s)
NSE_RETRIES = 1  # one retry of a timeout / 5xx, not three
QUOTE_TIMEOUT_S = 20.0  # one instrument's whole quote (warm-up wait, re-warm, BSE's extra calls) at most
MAX_WARMS = 2  # the first warm-up plus one re-warm per batch, however many quotes are refused


class QuoteBatch:
    """Live quotes for one valuation over ONE NSE session and ONE BSE client, instead of a new client (and a fresh
    quote-page warm-up) per stock. NSE's quote page is fetched once for cookies; every quote then shares the client's
    host rate limit (adapters.http: 2 requests per second to nseindia.com), so concurrency overlaps network latency
    without raising the request rate. A refused quote (401/403/block page) re-warms the session (serialised, at most
    MAX_WARMS warm-ups per batch) and retries; NSE's "no quote" (404) and timeouts do not. Each quote is bounded by
    `quote_timeout` seconds."""

    def __init__(self, *, quote_timeout: float = QUOTE_TIMEOUT_S) -> None:
        self._nse: Any = None
        self._bse: Any = None
        self._http: list[Any] = []
        self._lock = asyncio.Lock()
        self._warm_gen = 0
        self._warms = 0
        self.quote_timeout = quote_timeout

    async def __aenter__(self) -> QuoteBatch:
        return self

    async def __aexit__(self, *exc: object) -> None:
        for c in self._http:
            await c.aclose()

    def _client(self, **kw: Any) -> Any:
        from finresearch.adapters.http import PoliteClient

        c = PoliteClient(timeout=NSE_TIMEOUT_S, max_retries=NSE_RETRIES, **kw)
        self._http.append(c)
        return c

    async def _warm(self, seen_gen: int) -> None:
        async with self._lock:
            if self._nse is None:
                from finresearch.adapters.nse import NseClient

                self._nse = NseClient(self._client())
            if self._warm_gen != seen_gen or self._warms >= MAX_WARMS:
                return  # someone re-warmed while we waited, or the batch has used its warm-ups
            self._warms += 1
            self._warm_gen += 1  # even when the warm-up fails: the next quote must not queue for another one
            try:
                await self._nse.warm_quote_session(WARM_SYMBOL)
            except Exception:  # the quote that follows fails (and says why) if the cookies are really missing
                log.warning("NSE quote-session warm-up failed", exc_info=True)

    async def quote(self, symbol: str, exchange: str = "NSE") -> Any:
        return await asyncio.wait_for(self._quote(symbol, exchange), self.quote_timeout)

    async def _quote(self, symbol: str, exchange: str) -> Any:
        if exchange == "BSE":
            async with self._lock:
                if self._bse is None:
                    from finresearch.adapters.bse import BSE_RATE, BseClient
                    from finresearch.adapters.bse_equity import BseEquity

                    self._bse = BseEquity(BseClient(self._client(host_rates={"nseindia.com": 2.0,
                                                                              "bseindia.com": BSE_RATE})))  # fmt: skip
            q = await self._bse.quote(symbol)
            if q is None:
                raise LookupError(f"BSE has no quote for scrip {symbol}")
            return q
        from finresearch.adapters.nse import NseError, NseNoQuote

        if self._warm_gen == 0:
            await self._warm(0)
        gen = self._warm_gen
        try:
            return await self._nse.quote(symbol, warm=False)
        except NseNoQuote:
            raise  # NSE answered "no such symbol": fresh cookies would not change that
        except NseError:  # refused (401/403/block page): the cookies may have expired
            await self._warm(gen)
            return await self._nse.quote(symbol, warm=False)


# A broker statement's close or a CAS statement's NAV is a last-resort price. Listed stocks trade and funds publish a NAV
# every business day, so a statement figure older than one trading week no longer stands for today's value (a week's
# move is routinely several per cent; the alert layer uses the same horizon, metrics.FRESH_DAYS). Beyond it the price
# is "stale": still shown with its date, but the holding no longer counts as priced for `complete`, the headline and
# the XIRR say so, and data health counts it (#238). Days are counted Monday-Friday without the exchange holiday list,
# so a holiday counts as a trading day: a price can turn stale a day early, never late.
# The other age limits answer different questions and stay separate: metrics.FRESH_DAYS (5 calendar days) is how old
# the daily pass's whole valuation may be for alerts; monitor.portfolio_daily flags every statement price, and any
# live price older than STALE_PRICE_DAYS (4 calendar days), for the alert inputs, stricter than this headline rule;
# history.STALE_DAYS (10 calendar days) is how far the reconstruction forward-fills a missing close before a day
# counts as incomplete.
STATEMENT_MAX_AGE_TRADING_DAYS = 5


def statement_stale(p: PriceInfo, today: date) -> str | None:
    """Why a statement price is too old to stand for today's value, else None (live prices are never stale here)."""
    from finresearch.fincalc.dates import business_days_between

    if p.price is None or "statement" not in (p.source or ""):
        return None
    if not p.as_of:
        return f"{p.source} with no date"
    try:
        day = date.fromisoformat(p.as_of[:10])
    except ValueError:
        return f"{p.source} with an unreadable date ({p.as_of})"
    age = business_days_between(day, today)
    if age <= STATEMENT_MAX_AGE_TRADING_DAYS:
        return None
    return (f"{p.source} of {day.isoformat()} is {age} trading days old (over "
            f"{STATEMENT_MAX_AGE_TRADING_DAYS})")  # fmt: skip


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
    except ValueError:
        # fixed wording, never the exception text (it reaches the API)
        if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
            return None, "needs at least one investment and one current value"
        return None, "no XIRR between -99.99 % and 1000 %"


NO_PURCHASE_DATE = "opening balance with an unknown purchase date"


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
            if t.price is None:
                return [], "opening balance with an unknown cost"
            if not (t.meta or {}).get("acquired"):  # e.g. a broker holdings baseline: average price, no date
                return [], NO_PURCHASE_DATE
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
