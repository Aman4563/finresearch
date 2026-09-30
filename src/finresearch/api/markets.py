"""Read-only market data for the dashboard's stock, fund and bond pages: NSE quotes, price history, shareholding,
corporate actions, announcements and results; AMFI NAV history with returns, rolling returns, risk and SIP outcomes;
listed-bond yields, durations, cash flows and the price-yield curve. All arithmetic is fincalc's.

Every route is a GET that only reads public sources (and the stored profile's tax slab). Results are cached in
memory per app so moving around the dashboard does not hammer NSE or AMFI.

Test seam: set ``app.state.markets`` to a `MarketSources` with fakes before the first request.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query

from finresearch.fincalc.dates import add_years, today_ist

SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")
NSE_ARCHIVE_HOSTS = ("nsearchives.nseindia.com",)
HISTORY_MAX_REQUESTS = 40  # NSE returns at most ~70 rows (the latest) per historical-trade request
HISTORY_WINDOW_DAYS = 360  # and refuses (HTTP 404) a range longer than a year
RF_DEFAULT = Decimal("0.065")  # the page shows it and lets the viewer change it
SIP_DEFAULT = Decimal(10000)
CESS = Decimal("0.04")
SHP_XBRL_TTL_S = (
    30 * 86400
)  # a filing's XBRL URL carries its filing id and never changes  # health and education cess on income tax


# --------------------------------------------------------------------------- sources (live by default)
@dataclass
class MarketSources:
    """Where market data comes from. Every field is optional; missing ones use the live NSE / AMFI clients."""

    equity: Callable[[], Any] | None = None  # () -> async context manager with NseEquity's methods
    quote: Callable[[str], Awaitable[Any]] | None = None  # symbol -> nse.Quote
    nav_history: Callable[[Any, date, date], Awaitable[list]] | None = None  # (SchemeNav, start, end) -> NAVs
    navs_on: Callable[[date], Awaitable[dict]] | None = None  # day -> {scheme code: SchemeNav} (all AMCs)
    today: Callable[[], date] = today_ist

    def open_equity(self):
        if self.equity is not None:
            return self.equity()
        from finresearch.adapters.nse_equity import NseEquity

        return NseEquity()

    async def get_quote(self, symbol: str):
        if self.quote is not None:
            return await self.quote(symbol)
        from finresearch.adapters.nse import NseClient

        async with NseClient() as nse:
            return await nse.quote(symbol)

    async def get_nav_history(self, scheme, start: date, end: date) -> list:
        if self.nav_history is not None:
            return await self.nav_history(scheme, start, end)
        from finresearch.adapters.amfi import AmfiClient
        from finresearch.config import get_settings

        today = self.today()
        probe = today - timedelta(days=3 if today.weekday() == 0 else 1)
        async with AmfiClient(cache_dir=get_settings().state_dir) as amfi:
            return await amfi.scheme_history(scheme, start, end, probe)

    async def get_navs_on(self, day: date) -> dict:
        if self.navs_on is not None:
            return await self.navs_on(day)
        from finresearch.adapters.amfi import AmfiClient

        async with AmfiClient() as amfi:
            return await amfi.navs_on(day)


@dataclass
class TtlCache:
    """A tiny in-memory cache with per-entry expiry, plus a lock per key so concurrent requests fetch once."""

    entries: dict[Any, tuple[float, Any]] = field(default_factory=dict)
    locks: dict[Any, asyncio.Lock] = field(default_factory=dict)

    async def get(self, key: Any, ttl: float, make: Callable[[], Awaitable[Any]]) -> Any:
        hit = self.entries.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self.entries.get(key)
            if hit and hit[0] > time.time():
                return hit[1]
            value = await make()
            self.entries[key] = (time.time() + ttl, value)
            if len(self.entries) > 500:  # drop expired entries now and then
                now = time.time()
                for k in [k for k, (at, _) in self.entries.items() if at < now]:
                    self.entries.pop(k, None)
            return value


# --------------------------------------------------------------------------- helpers
def _s(v: Any) -> str | None:
    """Decimal -> plain string (no exponent), None stays None."""
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral() else format(v, "f")
    return str(v)


def _f(v: Any, digits: int = 6) -> float | None:
    return None if v is None else round(float(v), digits)


def _symbol(symbol: str) -> str:
    sym = symbol.strip().upper()
    if not SYMBOL_RE.match(sym):
        raise HTTPException(422, f"{symbol!r} is not an NSE symbol")
    return sym


def _quarter_end(d: date) -> bool:
    """Quarterly patterns are dated the last day of Mar/Jun/Sep/Dec; other dates are event filings (buybacks...)."""
    return d.month in (3, 6, 9, 12) and (d + timedelta(days=1)).day == 1


def _official(url: str | None) -> bool:
    from urllib.parse import urlsplit

    if not url:
        return False
    p = urlsplit(url)
    return p.scheme == "https" and (p.hostname or "") in NSE_ARCHIVE_HOSTS and p.port in (None, 443)


async def _retry(make: Callable[[], Awaitable[Any]], wait_s: float = 1.5) -> Any:
    """One retry after a short wait: NSE sometimes answers a burst of requests with a stray 404."""
    try:
        return await make()
    except Exception:
        await asyncio.sleep(wait_s)
        return await make()


def _histogram(values: list[float], bins: int = 14) -> list[dict[str, float]]:
    """Equal-width buckets over the values (as fractions); each bucket has its range and count."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi - lo < 1e-9:
        return [{"from": lo, "to": hi, "count": len(values)}]
    width = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(bins - 1, int((v - lo) / width))] += 1
    return [{"from": round(lo + i * width, 6), "to": round(lo + (i + 1) * width, 6), "count": c}
            for i, c in enumerate(counts)]  # fmt: skip


def _series_stats(points: list[tuple[date, Decimal]]) -> dict[str, Any]:
    """Return, annualised volatility and the largest fall (with its dates) for a dated price/NAV series."""
    from finresearch.fincalc import market

    if len(points) < 3:
        return {}
    values = [v for _, v in points]
    dd = market.max_drawdown(values)
    return {"return": _f(market.price_return(values[0], values[-1])),
            "annualised_volatility": _f(market.annualised_volatility(values)),
            "max_drawdown": _f(dd.max_drawdown), "drawdown_peak": points[dd.peak_index][0].isoformat(),
            "drawdown_trough": points[dd.trough_index][0].isoformat(), "points": len(points)}  # fmt: skip


def _pct_rank(sorted_vals: list[float], x: float) -> float:
    """Share of values at or below x (0..1)."""
    return sum(1 for v in sorted_vals if v <= x) / len(sorted_vals) if sorted_vals else 0.0


def _quantile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    pos = (len(sorted_vals) - 1) * q
    i, frac = int(pos), pos - int(pos)
    nxt = sorted_vals[min(i + 1, len(sorted_vals) - 1)]
    return round(sorted_vals[i] + (nxt - sorted_vals[i]) * frac, 6)


# --------------------------------------------------------------------------- routes
def add_market_routes(app: FastAPI, *, bond_rows: Callable[[], Awaitable[list]],
                      scheme_rows: Callable[[], Awaitable[list]]) -> None:  # fmt: skip
    """Register the /api/stocks/{symbol}/..., /api/funds/{code}/... and /api/bonds/{isin}/analytics routes."""
    cache = TtlCache()

    def src() -> MarketSources:
        s = getattr(app.state, "markets", None)
        if s is None:
            s = app.state.markets = MarketSources()
        return s

    # ------------------------------------------------------------------ stocks
    @app.get("/api/stocks/{symbol}/overview")
    async def stock_overview(symbol: str) -> dict[str, Any]:
        """Quote, 52-week range, market cap, shareholding trend, corporate actions and announcements. Each part is
        fetched separately; a part NSE refuses is listed under `errors` and the rest still comes back."""
        sym = _symbol(symbol)
        return await cache.get(("overview", sym), 600, lambda: _overview(sym))

    async def _overview(sym: str) -> dict[str, Any]:
        s = src()
        today = s.today()
        errors: list[str] = []

        async def part(name: str, make: Callable[[], Awaitable[Any]], default: Any) -> Any:
            try:
                return await make()
            except Exception as e:  # one refused section must not blank the whole page
                errors.append(f"{name}: {type(e).__name__}: {e}"[:240])
                return default

        q = await part("quote", lambda: s.get_quote(sym), None)
        async with s.open_equity() as eq:
            holding = await part("shareholding", lambda: eq.shareholding(sym), [])
            actions = await part("corporate actions", lambda: eq.corporate_actions(sym), [])
            anns = await part("announcements", lambda: eq.announcements(sym), [])
        quote = quote_json(q, sym) if q is not None else None
        year_ago = today - timedelta(days=365)
        ttm_dps = sum((a.dividend_per_share for a in actions
                       if a.dividend_per_share and a.ex_date and year_ago < a.ex_date <= today), Decimal(0))  # fmt: skip
        last_px = Decimal(quote["last_price"]) if quote and quote["last_price"] else None
        acts = sorted(actions, key=lambda a: a.ex_date or date.min, reverse=True)
        anns = sorted(anns, key=lambda a: a.at or datetime.min.replace(tzinfo=UTC), reverse=True)
        return {"symbol": sym, "fetched_at": datetime.now(UTC).isoformat(),
                "source": f"https://www.nseindia.com/get-quotes/equity?symbol={sym}", "quote": quote,
                "dividends": {"ttm_per_share": _s(ttm_dps) if ttm_dps else None,
                              "ttm_yield": _f(ttm_dps / last_px) if ttm_dps and last_px else None},
                "shareholding": [{"as_of": h.as_of.isoformat() if h.as_of else None, "promoter_pct": _f(h.promoter_pct, 4),
                                  "public_pct": _f(h.public_pct, 4), "employee_trusts_pct": _f(h.employee_trusts_pct, 4),
                                  "xbrl": h.xbrl} for h in holding[:12]],
                "corporate_actions": [{"subject": a.subject, "ex_date": a.ex_date.isoformat() if a.ex_date else None,
                                       "record_date": a.record_date.isoformat() if a.record_date else None,
                                       "dividend_per_share": _s(a.dividend_per_share),
                                       "upcoming": bool(a.ex_date and a.ex_date >= today)} for a in acts[:20]],
                "announcements": [{"at": a.at.isoformat() if a.at else None, "category": a.category, "text": a.text[:400],
                                   "attachment": a.attachment,
                                   "results_period_end": a.results_period_end.isoformat() if a.results_period_end else None}
                                  for a in anns[:15]],
                "errors": errors}  # fmt: skip

    @app.get("/api/stocks/{symbol}/history")
    async def stock_history(symbol: str, days: int = Query(365, ge=7, le=1830)) -> dict[str, Any]:
        """Daily closes (and OHLC, volume) for the last `days` calendar days with return, volatility and max
        drawdown. NSE answers ~70 trading days per request, so longer ranges take several requests (cached)."""
        sym = _symbol(symbol)
        return await cache.get(("history", sym, days), 1800, lambda: _history(sym, days))

    async def _history(sym: str, days: int) -> dict[str, Any]:
        s = src()
        end = s.today()
        start = end - timedelta(days=days)
        from finresearch.adapters.nse_equity import walk_history

        async with s.open_equity() as eq:
            got, partial = await walk_history(lambda lo, hi: _retry(lambda: eq.history(sym, lo, hi)), start, end,
                                              max_requests=HISTORY_MAX_REQUESTS, window_days=HISTORY_WINDOW_DAYS)  # fmt: skip
        bars = {b.day: b for b in got}
        rows = [bars[d] for d in sorted(bars) if bars[d].close]
        points = [(b.day, b.close) for b in rows]
        last = rows[-1] if rows else None
        return {"symbol": sym, "days": days, "source": f"https://www.nseindia.com/get-quotes/equity?symbol={sym}",
                "bars": [{"date": b.day.isoformat(), "close": _f(b.close, 4), "open": _f(b.open, 4), "high": _f(b.high, 4),
                          "low": _f(b.low, 4), "volume": _f(b.volume, 0)} for b in rows],
                "partial": partial, "week52_high": _s(last.week52_high) if last else None, "week52_low": _s(last.week52_low) if last else None,
                "stats": _series_stats(points)}  # fmt: skip

    @app.get("/api/stocks/{symbol}/results")
    async def stock_results(symbol: str, quarters: int = Query(8, ge=1, le=12)) -> dict[str, Any]:
        """Quarterly (and, where a March quarter is in range, annual) results read from each filing's XBRL:
        revenue, other income, expenses, PBT, tax, net profit and EPS, consolidated when the company files both.
        Quarters since Mar-2025 come from NSE's Integrated Filing (Financials) index, older ones from NSE's
        Financial Results index. Values are in rupees."""
        sym = _symbol(symbol)
        return await cache.get(("results", sym, quarters), 12 * 3600, lambda: _results(sym, quarters))

    async def _results(sym: str, quarters: int) -> dict[str, Any]:
        async with src().open_equity() as eq:
            return await results_from_nse(eq, sym, quarters)

    @app.get("/api/stocks/{symbol}/shareholding")
    async def stock_shareholding(symbol: str, quarters: int = Query(8, ge=1, le=12)) -> dict[str, Any]:
        """Shareholder categories (promoter, FPI, mutual funds, insurers, banks, other DIIs, individuals, bodies
        corporate, others) per quarter, read from each quarter's filed shareholding-pattern XBRL. Percentages are the
        filed ones: of total shares excluding shares underlying depository receipts (SCRR basis)."""
        sym = _symbol(symbol)
        return await cache.get(
            ("shareholding", sym, quarters), 12 * 3600, lambda: _shareholding(sym, quarters)
        )

    async def _shareholding(sym: str, quarters: int) -> dict[str, Any]:
        async with src().open_equity() as eq:
            return await shareholding_from_nse(eq, sym, quarters)

    # ------------------------------------------------------------------ mutual funds
    async def _scheme(code: str):
        code = code.strip()
        if not code.isdigit() or len(code) > 8:
            raise HTTPException(422, f"{code!r} is not an AMFI scheme code")
        scheme = next((x for x in await scheme_rows() if x.code == code), None)
        if scheme is None:
            raise HTTPException(404, f"scheme {code} is not in AMFI's NAV file")
        return scheme

    async def _navs(scheme, years: int) -> list[tuple[date, Decimal]]:
        async def make() -> list[tuple[date, Decimal]]:
            end = src().today()
            # a few extra days so an N-year trailing return finds a NAV on or before its start date
            hist = await src().get_nav_history(scheme, add_years(end, -years) - timedelta(days=10), end)
            by_day = {h.day: h.nav for h in hist if h.day and h.nav}
            return sorted(by_day.items())

        return await cache.get(("navs", scheme.code, years), 6 * 3600, make)

    def _scheme_json(x) -> dict[str, Any]:
        return {"scheme_code": x.code, "name": x.name, "plan": x.plan, "option": x.option, "category": x.category,
                "amc": x.amc, "nav": _s(x.nav), "nav_date": x.day.isoformat() if x.day else None,
                "isin": x.isin_growth}  # fmt: skip

    @app.get("/api/funds/{code}/analytics")
    async def fund_analytics(code: str, years: Annotated[int, Query(ge=1, le=10)] = 5,
                             rf: Annotated[Decimal, Query(ge=0, le=0.2)] = RF_DEFAULT) -> dict[str, Any]:  # fmt: skip
        """NAV history with trailing 1/3/5-year returns, 1- and 3-year rolling-return distributions, volatility,
        max drawdown, Sharpe and Sortino (risk-free rate `rf`, a fraction you choose)."""
        from finresearch.fincalc import funds

        scheme = await _scheme(code)
        navs = await _navs(scheme, years)
        out: dict[str, Any] = {"scheme": _scheme_json(scheme), "years": years,
                               "source": "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx",
                               "navs": [{"date": d.isoformat(), "nav": _f(v, 4)} for d, v in navs],
                               "trailing": {}, "rolling": {}, "risk": {}}  # fmt: skip
        if len(navs) < 3:
            return out
        out["trailing"] = {f"{y}y": _f(funds.trailing_return(navs, y)) for y in (1, 3, 5)}
        span = (navs[-1][0] - navs[0][0]).days
        out["trailing"]["since_start"] = _f(funds.annualised_return(navs[0][1], navs[-1][1], navs[0][0], navs[-1][0])) \
            if span >= 365 else None  # fmt: skip
        for y in (1, 3):
            series = funds.rolling_return_series(navs, y, step_days=7)
            vals = sorted(float(r) for _, r in series)
            if not vals:
                out["rolling"][f"{y}y"] = None
                continue
            out["rolling"][f"{y}y"] = {"count": len(vals), "min": _f(vals[0]), "p25": _quantile(vals, 0.25),
                                       "median": _quantile(vals, 0.5), "p75": _quantile(vals, 0.75), "max": _f(vals[-1]),
                                       "share_positive": round(sum(1 for v in vals if v > 0) / len(vals), 4),
                                       "share_above_rf": round(sum(1 for v in vals if v > float(rf)) / len(vals), 4),
                                       "histogram": _histogram(vals),
                                       "series": [{"date": d.isoformat(), "return": _f(r)} for d, r in series]}  # fmt: skip
        risk = _series_stats(navs)
        risk["risk_free_annual"] = _f(rf)
        for name, fn in (("sharpe", funds.sharpe_ratio), ("sortino", funds.sortino_ratio)):
            try:
                risk[name] = _f(fn(navs, rf), 4)
            except ValueError:
                risk[name] = None
        out["risk"] = risk
        return out

    @app.get("/api/funds/{code}/sip")
    async def fund_sip(code: str, amount: Annotated[Decimal, Query(gt=0, le=10_000_000)] = SIP_DEFAULT,
                       years: Annotated[int, Query(ge=1, le=10)] = 5,
                       day: Annotated[int, Query(ge=1, le=28)] = 1) -> dict[str, Any]:  # fmt: skip
        """What a monthly SIP of `amount` over the last `years` years would be worth today from the fund's actual
        NAVs (XIRR from fincalc), month by month, against investing the same total as one lump sum on day one."""
        from finresearch.fincalc import funds

        scheme = await _scheme(code)
        navs = await _navs(scheme, max(years, 5) if years <= 5 else 10)
        if len(navs) < 3:
            raise HTTPException(422, "not enough NAV history for a SIP")
        end = navs[-1][0]
        start = max(add_years(end, -years), navs[0][0])
        o = funds.sip_outcome(navs, amount, start, end, day)
        # month-by-month path, same fills as sip_outcome: the first NAV on or after each due date
        path, units, invested = [], Decimal(0), Decimal(0)
        y, m = start.year, start.month
        while True:
            due = date(y, m, day)
            if due > end:
                break
            if due >= start:
                fill = next(((d, v) for d, v in navs if d >= due), None)
                if fill is None:
                    break
                units += amount / fill[1]
                invested += amount
                path.append(
                    {
                        "date": fill[0].isoformat(),
                        "invested": _f(invested, 2),
                        "value": _f(units * fill[1], 2),
                    }
                )
            m += 1
            if m > 12:
                y, m = y + 1, 1
        path.append({"date": end.isoformat(), "invested": _f(o.invested, 2), "value": _f(o.value, 2)})
        first = next((d, v) for d, v in navs if d >= start)
        lump_units = o.invested / first[1]
        lump_value = lump_units * navs[-1][1]
        lump_cagr = (
            funds.annualised_return(first[1], navs[-1][1], first[0], end)
            if (end - first[0]).days > 0
            else None
        )
        for row in path:  # lump-sum value on the same dates
            nav = funds.nav_on_or_before(navs, date.fromisoformat(row["date"]))
            row["lump_sum"] = _f(lump_units * nav[1], 2) if nav else None
        return {"scheme_code": scheme.code, "amount": _s(amount), "years": years, "day": day,
                "start": first[0].isoformat(), "end": end.isoformat(),
                "sip": {"invested": _f(o.invested, 2), "value": _f(o.value, 2), "units": _f(o.units, 4),
                        "gain": _f(o.value - o.invested, 2), "xirr": _f(o.xirr), "instalments": o.instalments},
                "lump_sum": {"invested": _f(o.invested, 2), "value": _f(lump_value, 2),
                             "gain": _f(lump_value - o.invested, 2), "cagr": _f(lump_cagr)},
                "path": path}  # fmt: skip

    @app.get("/api/funds/{code}/peers")
    async def fund_peers(code: str) -> dict[str, Any]:
        """Where the scheme's 1/3/5-year returns sit among direct-growth schemes of its SEBI category (point to
        point from AMFI NAVs on the same dates). Slow the first time: it reads every scheme's NAV on three dates."""
        scheme = await _scheme(code)
        return await cache.get(("peers", scheme.code), 12 * 3600, lambda: _peers(scheme))

    async def _peers(me) -> dict[str, Any]:
        from finresearch.fincalc import funds

        rows = await scheme_rows()
        peers = [x for x in rows if x.category == me.category and x.is_direct_growth and x.nav and x.day]
        if me.code not in {p.code for p in peers} and me.nav and me.day:
            peers.append(me)
        anchor = me.day or src().today()
        out: dict[str, Any] = {
            "category": me.category,
            "peers": len(peers),
            "as_of": anchor.isoformat(),
            "periods": {},
        }
        for y in (1, 3, 5):
            snap = await src().get_navs_on(add_years(anchor, -y))
            rets: dict[str, float] = {}
            for p in peers:
                old = snap.get(p.code)
                if old and old.nav and old.day and p.day and p.day > old.day:
                    rets[p.code] = float(funds.annualised_return(old.nav, p.nav, old.day, p.day))
            vals = sorted(rets.values())
            mine = rets.get(me.code)
            out["periods"][f"{y}y"] = {"count": len(vals), "scheme": _f(mine) if mine is not None else None,
                                       "p25": _quantile(vals, 0.25), "median": _quantile(vals, 0.5),
                                       "p75": _quantile(vals, 0.75), "best": _f(vals[-1]) if vals else None,
                                       "rank": (sum(1 for v in vals if v > mine) + 1) if mine is not None else None,
                                       "percentile": round(_pct_rank(vals, mine), 4) if mine is not None else None}  # fmt: skip
        return out

    @app.get("/api/funds/{code}/consistency")
    async def fund_consistency(code: str) -> dict[str, Any]:
        """The fund signal's evidence: the scheme's 1- and 3-year returns at each quarter end against its category
        (median, quartiles, percentile), downside capture, quarter-end drawdown, R², TER and category fit inputs.
        Slow the first time (one AMFI all-scheme NAV snapshot per quarter end, kept on disk afterwards)."""
        from finresearch.signals.fund import analyse

        try:
            return await analyse(code)
        except LookupError as e:
            raise HTTPException(404, str(e)) from e
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

    # ------------------------------------------------------------------ listed bonds
    @app.get("/api/bonds/{isin}/analytics")
    async def bond_analytics(isin: str, freq: int | None = None, basis: str = "dirty",
                             tax_slab_pct: Annotated[Decimal | None, Query(ge=0, le=50)] = None,
                             settlement: date | None = None) -> dict[str, Any]:  # fmt: skip
        """YTM, current yield, accrued interest, durations, convexity and after-tax yield for a bond in NSE's
        capital-market list, with its cash-flow schedule and price-yield curve. NSE's CM-segment bond prices are
        dirty (they include accrued interest) unless `basis=clean`. The coupon frequency (`freq`: 1, 2, 4 or 12) must
        come from the offer document: without `freq`, a verified `coupon_frequency` claim from the bond's own research
        is used, else yearly is assumed and flagged (`freq_source`). The tax slab defaults to the profile's (4% cess
        added)."""
        from finresearch.fincalc import bonds as b

        code = isin.strip().upper()
        if not ISIN_RE.match(code):
            raise HTTPException(422, f"{isin!r} is not an ISIN")
        if freq is not None and freq not in (1, 2, 4, 12):
            raise HTTPException(422, "freq must be 1, 2, 4 or 12")
        if basis not in ("dirty", "clean"):
            raise HTTPException(422, "basis must be 'dirty' or 'clean'")
        rows = [x for x in await bond_rows() if x.isin.upper() == code]
        if not rows:
            raise HTTPException(404, f"{code} is not in NSE's list of traded bonds")
        bond = max(rows, key=lambda x: x.traded_value or 0)
        if freq is not None:
            freq_source = {"kind": "chosen"}
        else:
            known = await asyncio.to_thread(_verified_frequency, code)
            freq, freq_source = (known[0], known[1]) if known else (1, {"kind": "assumed"})
        if tax_slab_pct is None:
            tax_slab_pct = await asyncio.to_thread(_profile_slab)
        tax_rate = tax_slab_pct / 100 * (1 + CESS)
        head = {"bond": bond.model_dump(mode="json"), "warnings": bond.warnings, "freq": freq,
                "freq_source": freq_source, "basis": basis,
                "tax_slab_pct": _s(tax_slab_pct), "tax_rate": _f(tax_rate),
                "source": "https://www.nseindia.com/market-data/bonds-traded-in-capital-market"}  # fmt: skip
        if any("partly" in w for w in bond.warnings):
            return {**head, "analytics": None, "error": bond.warnings[0]}
        price = bond.last_price or bond.close
        if not (price and bond.coupon_pct is not None and bond.maturity and bond.face_value):
            return {
                **head,
                "analytics": None,
                "error": "NSE's list lacks the price, coupon, maturity or face value",
            }
        s_day = settlement or src().today()
        if bond.maturity <= s_day:
            return {**head, "analytics": None, "error": f"the bond matured on {bond.maturity}"}
        coupon, face = bond.coupon_pct / 100, bond.face_value
        ai = b.accrued_interest(s_day, bond.maturity, coupon, freq, face)
        clean = price - ai if basis == "dirty" else price
        dirty = price if basis == "dirty" else price + ai
        if clean <= 0:
            return {
                **head,
                "analytics": None,
                "error": "the clean price would be negative: check the price basis",
            }
        y = b.ytm(clean, s_day, bond.maturity, coupon, freq, face)
        d = b.duration(y, s_day, bond.maturity, coupon, freq, face)
        after = b.after_tax_ytm(clean, s_day, bond.maturity, coupon, freq, tax_rate, face=face, accrued=ai)
        flows = b.cash_flows(s_day, bond.maturity, coupon, freq, face)
        per = face * coupon / freq
        yf = float(y)
        curve = []
        for bp in range(-300, 301, 10):
            yy = yf + bp / 10000
            if yy <= -0.5:
                continue
            dp = b.dirty_price(yy, s_day, bond.maturity, coupon, freq, face)
            curve.append({"bp": bp, "yield": round(yy, 6), "dirty": _f(dp, 4), "clean": _f(dp - ai, 4)})
        base_dirty = float(b.dirty_price(y, s_day, bond.maturity, coupon, freq, face))
        sensitivity = []
        for bp in (-200, -100, -50, -25, 25, 50, 100, 200):
            new = float(b.dirty_price(yf + bp / 10000, s_day, bond.maturity, coupon, freq, face))
            dy = bp / 10000
            estimate = -float(d.modified) * dy + 0.5 * float(d.convexity) * dy * dy
            sensitivity.append({"bp": bp, "price": round(new, 4), "change_pct": round((new / base_dirty - 1) * 100, 4),
                                "duration_estimate_pct": round(estimate * 100, 4)})  # fmt: skip
        total_coupons = sum((cf.amount for cf in flows), Decimal(0)) - face
        analytics = {"settlement": s_day.isoformat(), "price": _s(price), "clean_price": _f(clean, 4),
                     "dirty_price": _f(dirty, 4), "accrued_interest": _f(ai, 4), "ytm": _f(y),
                     "current_yield": _f(b.current_yield(clean, coupon, face)), "after_tax_ytm": _f(after),
                     "macaulay_duration": _f(d.macaulay, 4), "modified_duration": _f(d.modified, 4),
                     "convexity": _f(d.convexity, 4), "years_to_maturity": round((bond.maturity - s_day).days / 365.25, 3),
                     "premium_pct": _f((clean / face - 1) * 100, 4), "coupon_per_payment": _f(per, 4),
                     "cash_flows": [{"date": cf.day.isoformat(), "coupon": _f(per, 4),
                                     "principal": _f(face if cf.day == bond.maturity else 0, 4),
                                     "coupon_after_tax": _f(per * (1 - tax_rate), 4), "periods": _f(cf.periods, 6)}
                                    for cf in flows],
                     "totals": {"coupons": _f(total_coupons, 2), "principal": _f(face, 2),
                                "received": _f(total_coupons + face, 2), "paid_today": _f(dirty, 2)},
                     "curve": curve, "sensitivity": sensitivity,
                     "conventions": "accrued interest Actual/Actual (SEBI); discounting by coupon periods; after-tax "
                                    "yield pays the dirty price, taxes each coupon at the slab rate plus 4% cess and "
                                    "treats the gap between face value and the clean price as a capital gain or loss "
                                    "at redemption"}  # fmt: skip
        return {**head, "analytics": analytics, "error": None}


# what stands in for revenue, by taxonomy: Ind AS companies and NBFCs, banks, life insurers, general insurers
REVENUE_BASES = ("revenue_from_operations", "interest_earned", "net_premium_income", "premium_earned")
RESULT_SOURCE_RANK = {"nse_integrated_filing": 0, "nse_financial_results": 1}


async def _xbrl(eq: Any, url: str) -> bytes:
    """A filing's XBRL (its URL carries the filing id and never changes, so it is cached on disk); a cached block
    page that does not parse is fetched again."""
    import xml.etree.ElementTree as ET

    data = await eq.fetch_bytes(url, cache_ttl=SHP_XBRL_TTL_S)
    try:
        ET.fromstring(data)
    except ET.ParseError:
        data = await eq.fetch_bytes(url, cache_ttl=0)
    return data


async def results_from_nse(
    eq: Any, sym: str, quarters: int, *, annual_facts: dict[date, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Quarterly and annual results from each filing's XBRL (see the /api/stocks/{symbol}/results route). When
    `annual_facts` is given it also receives, per fiscal year end, the full-year P&L and cash-flow facts merged with
    the year-end balance sheet (Decimal, rupees) and the filing's basis: the inputs of fincalc.forensic."""
    from finresearch.adapters.nse_equity import INTEGRATED_PAGE, RESULTS_PAGE
    from finresearch.adapters.xbrl import parse_results_xbrl

    errors: list[str] = []
    filings: list[Any] = []
    out: list[dict[str, Any]] = []
    annual: dict[date, dict[str, Any]] = {}
    if hasattr(eq, "integrated_filings"):
        try:
            filings += [f.as_result_filing() for f in await eq.integrated_filings(sym)]
        except Exception as e:
            errors.append(f"integrated filing index: {type(e).__name__}: {e}"[:200])
    if len({f.period_to for f in filings if f.period_to and _official(f.xbrl)}) < quarters:
        try:  # the older index holds the quarters before integrated filing began (up to Dec-2024)
            filings += await eq.results(sym, "Quarterly")
        except Exception as e:
            errors.append(f"financial results index: {type(e).__name__}: {e}"[:200])
    ranked = _rank_result_filings(filings)
    for end in sorted(ranked, reverse=True)[:quarters]:
        for f in ranked[end]:  # consolidated first; the next candidate stands in when a file fails
            try:
                x = parse_results_xbrl(await _xbrl(eq, f.xbrl))
            except Exception as e:
                errors.append(f"{end} {f.xbrl}: {type(e).__name__}: {e}"[:200])
                continue
            if x.quarter is None or not x.quarter.facts:
                errors.append(f"{end} {f.xbrl}: no current-quarter facts")
                continue
            out.append(_result_row(f, x.quarter, end))
            y = x.year_to_date
            if y and y.start and y.end and (y.end - y.start).days >= 360 and y.end not in annual:
                annual[y.end] = _result_row(f, y, y.end, annual=True)
                bs = x.balance_sheet
                if annual_facts is not None and y.end not in annual_facts:
                    annual_facts[y.end] = {"facts": {**y.facts, **(bs.facts if bs and bs.end == y.end else {})},
                                           "consolidated": f.consolidated, "xbrl": f.xbrl,
                                           "company_type": x.company_type,
                                           "revenue_basis": next((k for k in REVENUE_BASES if k in y.facts), None),
                                           "filed_at": f.filed_at}  # fmt: skip
            break
    out.sort(key=lambda r: r["period_end"])
    _add_growth(out, annual=False)
    years = sorted(annual.values(), key=lambda r: r["period_end"])
    _add_growth(years, annual=True)
    latest = out[-1] if out else None
    sources = [{"name": "NSE Integrated Filing (Financials)", "url": INTEGRATED_PAGE,
                "note": "quarters from Mar-2025, when SEBI moved results into integrated filing"},
               {"name": "NSE Financial Results", "url": RESULTS_PAGE, "note": "quarters up to Dec-2024"}]  # fmt: skip
    return {"symbol": sym, "unit": "INR (EPS: INR per share)", "quarters": out, "annual": years, "errors": errors,
            "as_of": datetime.now(UTC).isoformat(), "sources": sources,
            "latest_quarter": None if latest is None else {
                k: latest[k] for k in ("label", "period_end", "filed_at", "source", "source_url", "consolidated",
                                       "xbrl", "ixbrl")},
            "source": latest["source_url"] if latest else INTEGRATED_PAGE}  # fmt: skip


async def shareholding_from_nse(eq: Any, sym: str, quarters: int) -> dict[str, Any]:
    """Shareholder categories per quarter from each filed pattern's XBRL (the /shareholding route's payload)."""
    from xml.etree.ElementTree import ParseError

    from finresearch.adapters.shp_xbrl import CATEGORIES, GROUPS, parse_shareholding_xbrl

    errors: list[str] = []
    out: list[dict[str, Any]] = []
    rows = await eq.shareholding(sym)
    best: dict[date, Any] = {}  # one filing per quarter end: the latest submission (a revision replaces it)
    for h in rows:
        if not h.as_of or not _quarter_end(h.as_of) or not _official(h.xbrl):
            continue
        cur = best.get(h.as_of)
        if cur is None or (h.submitted or date.min) > (cur.submitted or date.min):
            best[h.as_of] = h
    for end in sorted(best, reverse=True)[:quarters]:
        h = best[end]
        try:
            try:
                p = parse_shareholding_xbrl(await eq.fetch_bytes(h.xbrl, cache_ttl=SHP_XBRL_TTL_S))
            except ParseError:  # a cached block page: fetch it again
                p = parse_shareholding_xbrl(await eq.fetch_bytes(h.xbrl, cache_ttl=0))
        except Exception as e:
            errors.append(f"{end}: {type(e).__name__}: {e}"[:200])
            continue
        if not p.split:
            errors.append(f"{end}: {'; '.join(p.warnings) or 'no category rows'}")
            continue
        if p.as_of and p.as_of != end:
            errors.append(f"{end}: the filing is dated {p.as_of}; skipped")
            continue
        out.append({"as_of": end.isoformat(), "submitted": h.submitted.isoformat() if h.submitted else None,
                    "xbrl": h.xbrl, "taxonomy": p.taxonomy,
                    "categories": {k: _f(v, 4) for k, v in p.split.items()},
                    "groups": {k: _f(v, 4) for k, v in p.groups.items()},
                    "shareholders": {k: int(p.holders[k]) for k in ("total", "public", "retail", "hni")
                                     if k in p.holders},
                    "dr_pct_of_total_shares": _f(p.dr_pct_of_total, 4), "warnings": p.warnings})  # fmt: skip
    out.sort(key=lambda r: r["as_of"])
    return {"symbol": sym, "fetched_at": datetime.now(UTC).isoformat(),
            "basis": "% of total shares excluding shares underlying depository receipts "
                                     "(SCRR 1957 basis, as filed)",
            "category_labels": [{"key": k, "label": lbl, "group": g} for k, lbl, g in CATEGORIES],
            "group_labels": [{"key": k, "label": lbl} for k, lbl in GROUPS],
            "quarters": out, "errors": errors,
            "source": "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"}  # fmt: skip


def _rank_result_filings(filings: list[Any]) -> dict[date, list[Any]]:
    """Candidate filings per quarter end, best first: consolidated before standalone, integrated filing before the
    older results index, then the latest filed (a revision replaces the original). Only NSE archive XBRL links."""
    by_end: dict[date, list[Any]] = {}
    for f in filings:
        if f.period_to and _official(f.xbrl):
            by_end.setdefault(f.period_to, []).append(f)
    floor = datetime.min.replace(tzinfo=UTC)
    for fs in by_end.values():
        fs.sort(key=lambda f: (not f.consolidated, RESULT_SOURCE_RANK.get(getattr(f, "source", ""), 9),
                               -(f.filed_at or floor).timestamp()))  # fmt: skip
    return by_end


def _result_row(f: Any, p: Any, end: date, *, annual: bool = False) -> dict[str, Any]:
    """One period's figures from a parsed XBRL period (fincalc does the margins)."""
    from finresearch.adapters.nse_equity import INTEGRATED_PAGE, RESULTS_PAGE
    from finresearch.fincalc.dates import fiscal_quarter_label, fiscal_year

    facts = p.facts
    basis = next((k for k in REVENUE_BASES if k in facts), None)
    revenue = facts.get(basis) if basis else None
    insurer = basis in ("net_premium_income", "premium_earned")
    expenses = facts.get("total_expenses")
    if expenses is None and "expenditure_excluding_provisions" in facts:
        expenses = facts["expenditure_excluding_provisions"] + facts.get("provisions", Decimal(0))
    tax = facts.get("tax_expense", facts.get("tax_shareholders_account", facts.get("provision_for_tax")))
    if insurer:  # the revenue account's income and expenses are policyholders' money, not the company's P&L
        expenses = None
    owners = facts.get("profit_attributable_to_owners", facts.get("profit_for_period"))
    pbt = facts.get("profit_before_tax")
    period_end = p.end or end
    start = p.start or (None if annual else f.period_from)
    source = getattr(f, "source", "nse_financial_results")
    return {"label": f"FY{fiscal_year(period_end) % 100:02d}" if annual else fiscal_quarter_label(period_end),
            "period_start": start.isoformat() if start else None, "period_end": period_end.isoformat(),
            "consolidated": f.consolidated, "audited": f.audited,
            "filed_at": f.filed_at.isoformat() if f.filed_at else None, "revised": getattr(f, "revised", False),
            "source": source, "source_url": INTEGRATED_PAGE if source == "nse_integrated_filing" else RESULTS_PAGE,
            "bank": basis == "interest_earned", "revenue_basis": basis, "revenue": _f(revenue, 2),
            "other_income": None if insurer else _f(facts.get("other_income"), 2),
            "total_income": None if insurer else _f(facts.get("total_income"), 2), "total_expenses": _f(expenses, 2),
            "exceptional_items": _f(facts.get("exceptional_items"), 2), "profit_before_tax": _f(pbt, 2),
            "tax": _f(tax, 2), "net_profit": _f(facts.get("profit_for_period"), 2),
            "profit": _f(owners, 2), "eps": _f(facts.get("eps_basic"), 4), "eps_diluted": _f(facts.get("eps_diluted"), 4),
            "margin": _f(owners / revenue, 6) if owners is not None and revenue else None,
            "pbt_margin": _f(pbt / revenue, 6) if pbt is not None and revenue else None,
            "xbrl": f.xbrl, "ixbrl": getattr(f, "ixbrl", None)}  # fmt: skip


def _add_growth(rows: list[dict[str, Any]], *, annual: bool) -> None:
    """QoQ (previous quarter) and YoY (same period a year earlier) change in revenue, profit and EPS, only between
    periods reported on the same basis (consolidated vs standalone). Fractions; None when not comparable."""
    from finresearch.fincalc.growth import pct_change

    by_end = {r["period_end"]: r for r in rows}
    for r in rows:
        end = date.fromisoformat(r["period_end"])
        start = date.fromisoformat(r["period_start"]) if r["period_start"] else None
        prev = by_end.get((start - timedelta(days=1)).isoformat()) if start and not annual else None
        year_ago = by_end.get(add_years(end, -1).isoformat())
        g: dict[str, float | None] = {}
        for key in ("revenue", "profit", "eps"):
            for tag, other in (("qoq", prev), ("yoy", year_ago)):
                if annual and tag == "qoq":
                    continue
                ok = other is not None and other["consolidated"] == r["consolidated"]
                g[f"{key}_{tag}"] = _f(pct_change(other[key], r[key]), 6) if ok else None
        r["growth"] = g


def _verified_frequency(isin: str) -> tuple[int, dict[str, Any]] | None:
    """The coupon frequency verified in the bond's latest research run (a `coupon_frequency` claim), if any."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Claim, Company, ResearchRun

    with session_scope() as s:
        rows = s.execute(select(Claim.id, Claim.run_id, Claim.value)
                         .join(ResearchRun, ResearchRun.id == Claim.run_id)
                         .join(Company, Company.id == ResearchRun.company_id)
                         .where(Company.slug == f"bond-{isin.lower()}", ResearchRun.kind == "bond_report",
                                Claim.metric == "coupon_frequency", Claim.status == "verified", Claim.value.is_not(None))
                         .order_by(ResearchRun.id.desc(), Claim.id)).all()  # fmt: skip
    for claim_id, run_id, value in rows:
        if value is not None and value == int(value) and int(value) in (1, 2, 4, 12):
            return int(value), {"kind": "verified", "claim_id": claim_id, "run_id": run_id}
    return None


def quote_json(q, sym: str) -> dict[str, Any]:
    """An NSE quote as the stock pages show it: last price, day change, 52-week position and market cap."""
    from finresearch.fincalc.valuation import market_cap

    last = q.last_price or q.close_price
    change = (last - q.previous_close) if last is not None and q.previous_close else None
    mcap = market_cap(q.issued_shares, last) if q.issued_shares and last else None
    pos = None
    if last and q.week52_high and q.week52_low and q.week52_high > q.week52_low:
        pos = (last - q.week52_low) / (q.week52_high - q.week52_low)
    return {"symbol": q.symbol or sym, "company": q.company, "industry": q.industry, "status": q.status,
            "listing_date": q.listing_date.isoformat() if q.listing_date else None,
            "as_of": q.as_of.isoformat() if q.as_of else None, "last_price": _s(last), "open": _s(q.open),
            "previous_close": _s(q.previous_close), "change": _s(change),
            "change_pct": _f(change / q.previous_close * 100, 4) if change is not None else None,
            "week52_high": _s(q.week52_high), "week52_low": _s(q.week52_low),
            "week52_position": _f(pos, 4), "issued_shares": _s(q.issued_shares),
            "market_cap": _s(mcap.quantize(Decimal(1))) if mcap is not None else None}  # fmt: skip


def _profile_slab() -> Decimal:
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import load_profile

    try:
        with session_scope() as s:
            return load_profile(s).tax_slab_pct
    except Exception:
        return Decimal(30)
