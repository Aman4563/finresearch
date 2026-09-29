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
CESS = Decimal("0.04")  # health and education cess on income tax


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
        from finresearch.fincalc.valuation import market_cap

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
        quote = None
        if q is not None:
            last = q.last_price or q.close_price
            change = (last - q.previous_close) if last is not None and q.previous_close else None
            mcap = market_cap(q.issued_shares, last) if q.issued_shares and last else None
            pos = None
            if last and q.week52_high and q.week52_low and q.week52_high > q.week52_low:
                pos = (last - q.week52_low) / (q.week52_high - q.week52_low)
            quote = {"symbol": q.symbol or sym, "company": q.company, "industry": q.industry, "status": q.status,
                     "listing_date": q.listing_date.isoformat() if q.listing_date else None,
                     "as_of": q.as_of.isoformat() if q.as_of else None, "last_price": _s(last), "open": _s(q.open),
                     "previous_close": _s(q.previous_close), "change": _s(change),
                     "change_pct": _f(change / q.previous_close * 100, 4) if change is not None else None,
                     "week52_high": _s(q.week52_high), "week52_low": _s(q.week52_low),
                     "week52_position": _f(pos, 4), "issued_shares": _s(q.issued_shares),
                     "market_cap": _s(mcap.quantize(Decimal(1))) if mcap is not None else None}  # fmt: skip
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
        bars: dict[date, Any] = {}
        async with s.open_equity() as eq:
            # NSE answers a range with only its latest ~70 trading days, so walk backwards from the end until a
            # request reaches the start (or returns nothing new)
            hi, partial = end, False
            for _ in range(HISTORY_MAX_REQUESTS):
                if hi < start:
                    break
                lo = max(start, hi - timedelta(days=HISTORY_WINDOW_DAYS))
                try:
                    got = await _retry(lambda lo=lo, hi=hi: eq.history(sym, lo, hi))
                except Exception:
                    if not bars:
                        raise
                    partial = True  # keep what came back; the page says the history is shorter
                    break
                got = [x for x in got if lo <= x.day <= hi]
                for x in got:
                    bars[x.day] = x
                if not got and lo == start:
                    break
                earliest = min((x.day for x in got), default=lo)
                # a window answered in full (or empty: e.g. before listing) moves to the previous window
                hi = (
                    lo - timedelta(days=1)
                    if earliest <= lo + timedelta(days=4)
                    else earliest - timedelta(days=1)
                )
        rows = [bars[d] for d in sorted(bars) if bars[d].close]
        points = [(b.day, b.close) for b in rows]
        last = rows[-1] if rows else None
        return {"symbol": sym, "days": days, "source": f"https://www.nseindia.com/get-quotes/equity?symbol={sym}",
                "bars": [{"date": b.day.isoformat(), "close": _f(b.close, 4), "open": _f(b.open, 4), "high": _f(b.high, 4),
                          "low": _f(b.low, 4), "volume": _f(b.volume, 0)} for b in rows],
                "partial": partial, "week52_high": _s(last.week52_high) if last else None, "week52_low": _s(last.week52_low) if last else None,
                "stats": _series_stats(points)}  # fmt: skip

    @app.get("/api/stocks/{symbol}/results")
    async def stock_results(symbol: str, quarters: int = Query(6, ge=1, le=8)) -> dict[str, Any]:
        """Revenue, profit and EPS for the latest quarters, read from each results filing's XBRL (consolidated when
        the company files both). Values are in rupees."""
        sym = _symbol(symbol)
        return await cache.get(("results", sym, quarters), 12 * 3600, lambda: _results(sym, quarters))

    async def _results(sym: str, quarters: int) -> dict[str, Any]:
        from finresearch.adapters.xbrl import parse_results_xbrl

        s = src()
        errors: list[str] = []
        out = []
        async with s.open_equity() as eq:
            filings = await eq.results(sym, "Quarterly")
            best: dict[date, Any] = {}
            for f in filings:
                if not f.period_to or not _official(f.xbrl):
                    continue
                cur = best.get(f.period_to)
                better = cur is None or (f.consolidated and not cur.consolidated) or (
                    f.consolidated == cur.consolidated and (f.filed_at or datetime.min.replace(tzinfo=UTC))
                    > (cur.filed_at or datetime.min.replace(tzinfo=UTC)))  # fmt: skip
                if better:
                    best[f.period_to] = f
            for end in sorted(best, reverse=True)[:quarters]:
                f = best[end]
                try:
                    x = parse_results_xbrl(await eq.fetch_bytes(f.xbrl))
                except Exception as e:
                    errors.append(f"{end}: {type(e).__name__}: {e}"[:200])
                    continue
                p = x.quarter
                if p is None:
                    continue
                facts = p.facts
                profit = facts.get("profit_attributable_to_owners", facts.get("profit_for_period"))
                revenue = facts.get("revenue_from_operations")
                out.append({"period_start": (p.start or f.period_from).isoformat() if (p.start or f.period_from) else None,
                            "period_end": (p.end or end).isoformat(), "consolidated": f.consolidated,
                            "audited": f.audited, "filed_at": f.filed_at.isoformat() if f.filed_at else None,
                            "revenue": _f(revenue, 2), "total_income": _f(facts.get("total_income"), 2),
                            "profit": _f(profit, 2), "eps": _f(facts.get("eps_basic"), 4),
                            "margin": _f(profit / revenue, 6) if profit is not None and revenue else None,
                            "xbrl": f.xbrl})  # fmt: skip
        out.sort(key=lambda r: r["period_end"])
        return {"symbol": sym, "unit": "INR (EPS: INR per share)", "quarters": out, "errors": errors,
                "source": "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"}  # fmt: skip

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

    # ------------------------------------------------------------------ listed bonds
    @app.get("/api/bonds/{isin}/analytics")
    async def bond_analytics(isin: str, freq: int = 1, basis: str = "dirty",
                             tax_slab_pct: Annotated[Decimal | None, Query(ge=0, le=50)] = None,
                             settlement: date | None = None) -> dict[str, Any]:  # fmt: skip
        """YTM, current yield, accrued interest, durations, convexity and after-tax yield for a bond in NSE's
        capital-market list, with its cash-flow schedule and price-yield curve. NSE's CM-segment bond prices are
        dirty (they include accrued interest) unless `basis=clean`. The coupon frequency (`freq`: 1, 2, 4 or 12)
        must come from the offer document; the tax slab defaults to the profile's (4% cess added)."""
        from finresearch.fincalc import bonds as b

        code = isin.strip().upper()
        if not ISIN_RE.match(code):
            raise HTTPException(422, f"{isin!r} is not an ISIN")
        if freq not in (1, 2, 4, 12):
            raise HTTPException(422, "freq must be 1, 2, 4 or 12")
        if basis not in ("dirty", "clean"):
            raise HTTPException(422, "basis must be 'dirty' or 'clean'")
        rows = [x for x in await bond_rows() if x.isin.upper() == code]
        if not rows:
            raise HTTPException(404, f"{code} is not in NSE's list of traded bonds")
        bond = max(rows, key=lambda x: x.traded_value or 0)
        if tax_slab_pct is None:
            tax_slab_pct = await asyncio.to_thread(_profile_slab)
        tax_rate = tax_slab_pct / 100 * (1 + CESS)
        head = {"bond": bond.model_dump(mode="json"), "warnings": bond.warnings, "freq": freq, "basis": basis,
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
        after = b.after_tax_ytm(clean, s_day, bond.maturity, coupon, freq, tax_rate, face=face)
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
                                    "yield taxes each coupon at the slab rate plus 4% cess and treats the gap between "
                                    "face value and the clean price as a capital gain or loss at redemption"}  # fmt: skip
        return {**head, "analytics": analytics, "error": None}


def _profile_slab() -> Decimal:
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import load_profile

    try:
        with session_scope() as s:
            return load_profile(s).tax_slab_pct
    except Exception:
        return Decimal(30)
