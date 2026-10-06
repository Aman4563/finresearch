"""/api/portfolio/analytics/*: performance vs NIFTYBEES, risk, concentration and costs of the personal portfolio.

Every route is a GET that reads the local database and public price history, except PUT .../settings, which stores
the user's business-group corrections and exit loads in `portfolio_setting` ("analytics"). The daily value history is
rebuilt from the transactions (`portfolio.history`), cached in memory for 30 minutes per transaction set and on disk
per instrument, so only the first build after new transactions reads prices from NSE/BSE/AMFI.

Privacy: personal data stays in the local database; nothing here calls an LLM.

Test seams: `app.state.markets` (a `MarketSources` with fake NSE/AMFI) and `app.state.analytics_sources` (an
`AnalyticsSources` with a fake TER table and risk-free curve).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from finresearch.db import session_scope

HISTORY_TTL_S = 1800
SETTINGS_KEY = "analytics"


@dataclass
class AnalyticsSources:
    ter: Callable[[date], Awaitable[dict]] | None = None  # month -> {ter_key(name): SchemeTer}
    curve: Callable[[], Awaitable[Any]] | None = None  # () -> fbil.ParCurve


class ExitLoad(BaseModel):
    pct: float = Field(ge=0, le=10)
    days: int = Field(ge=0, le=3650)


class AnalyticsSettings(BaseModel):
    groups: dict[str, str | None] | None = None  # instrument key ("NSE:INFY") -> group ("" / null = no group)
    exit_loads: dict[str, ExitLoad | None] | None = None  # holding id -> load


def get_settings_row(s) -> dict[str, Any]:
    from finresearch.db.models import PortfolioSetting

    row = s.get(PortfolioSetting, SETTINGS_KEY)
    v = dict(row.value or {}) if row else {}
    return {"groups": dict(v.get("groups") or {}), "exit_loads": dict(v.get("exit_loads") or {})}


def add_portfolio_analytics_routes(app: FastAPI, *, scheme_rows: Callable[[], Awaitable[list]]) -> None:
    from finresearch.api.markets import MarketSources, TtlCache

    cache = TtlCache()

    def src() -> MarketSources:
        s = getattr(app.state, "markets", None)
        if s is None:
            s = app.state.markets = MarketSources(listings=getattr(app.state, "listings", None))
        return s

    def seams() -> AnalyticsSources:
        return getattr(app.state, "analytics_sources", None) or AnalyticsSources()

    def store():
        from finresearch.config import get_settings
        from finresearch.portfolio.history import PriceStore

        return PriceStore(get_settings().state_dir / "portfolio_history")

    def _load() -> list[Any]:
        from finresearch.portfolio.history import holdings_from
        from finresearch.portfolio.report import load

        with session_scope() as s:
            return holdings_from(load(s))

    async def _schemes() -> tuple[dict[str, Any], str | None]:
        try:
            rows = await cache.get(("navall",), 6 * 3600, scheme_rows)
        except Exception as e:
            return {}, f"AMFI's NAV file could not be read ({type(e).__name__}): funds are left out"
        out: dict[str, Any] = {}
        for r in rows:
            out[r.code] = r
            for i in (r.isin_growth, r.isin_reinvest):
                if i:
                    out[f"ISIN:{i.upper()}"] = r
        return out, None

    async def history():
        from finresearch.portfolio.history import Fetcher, build, fingerprint

        hs = _load()
        today = src().today()
        fp = fingerprint(hs)
        key = ("history", fp, today)

        async def make():
            schemes, note = await _schemes() if any(h.asset_type == "mf" for h in hs) else ({}, None)
            isin_map = None
            if src().listings is not None and any(h.asset_type == "stock" and not h.nse_symbol and not h.bse_code
                                                  and h.isin for h in hs):  # fmt: skip
                try:
                    isin_map = {r.isin.upper(): r for r in (await src().listings()).rows}
                except Exception:
                    isin_map = None
            async with Fetcher(src()) as f:
                hist = await build(
                    hs, fetch=f, store=store(), schemes=schemes, today=today, isin_map=isin_map
                )
            if note:
                hist.warnings.append(note)
            return hist

        return await cache.get(key, HISTORY_TTL_S, make), today, fp

    app.state.portfolio_history = history  # shared with the behaviour report (api/journal.py)

    async def _risk_free(rf: float | None):
        from finresearch.portfolio.analytics import RiskFree, risk_free_from_curve

        if rf is not None:
            return RiskFree(rf / 100, f"your figure: {rf:g} % a year", None, None, user_set=True)

        async def make():
            if seams().curve is not None:
                return await seams().curve()
            from finresearch.adapters.fbil import FALLBACK_CURVE, FbilClient

            try:
                async with FbilClient() as c:
                    return await c.latest_par_curve()
            except Exception:
                return FALLBACK_CURVE

        return risk_free_from_curve(await cache.get(("fbil",), 12 * 3600, make))

    async def _sectors(hist) -> dict[str, str]:
        """Sector per stock position: the user's own label on the holding, else NSE's industry from the quote (cached
        for 12 hours; one quote per stock)."""
        from finresearch.db.models import PortfolioHolding

        out: dict[str, str] = {}
        with session_scope() as s:
            own = {h.id: h.sector for h in s.query(PortfolioHolding).all() if h.sector}
        todo = []
        for p in hist.positions:
            label = next((own[i] for i in p.holding_ids if i in own), None)
            if label:
                out[p.key] = label
            elif p.asset_type == "stock":
                todo.append(p)
        if not todo:
            return out
        sources = src()
        from finresearch.portfolio.valuation import QuoteBatch

        async with QuoteBatch() as batch:
            for p in todo:
                exch, sym = p.key.split(":", 1)

                async def live(sym=sym, exch=exch):
                    if (exch == "NSE" and sources.quote is not None) or (
                        exch == "BSE" and sources.bse_quote is not None
                    ):
                        return await sources.get_quote(sym, exch)
                    return await batch.quote(sym, exch)

                try:
                    q = await cache.get(("industry", exch, sym), 12 * 3600, live)
                    if getattr(q, "industry", None):
                        out[p.key] = q.industry
                except Exception:
                    continue
        return out

    # ------------------------------------------------------------------ routes
    @app.get("/api/portfolio/analytics/performance")
    async def performance() -> dict[str, Any]:
        """The daily value history against the same cash flows put into NIFTYBEES (total return): TWR, window XIRR,
        direct-index-equivalent value and XIRR, ₹ difference and the Kaplan–Schoar PME."""
        from finresearch.portfolio.analytics import performance as perf

        hist, _, _ = await history()
        return perf(hist)

    @app.get("/api/portfolio/analytics/risk")
    async def risk(
        rf: float | None = Query(None, ge=0, le=20, description="risk-free rate, % a year"),
    ) -> dict[str, Any]:
        """Realised volatility, beta and tracking error vs NIFTYBEES, max drawdown and recovery, historical VaR/CVaR,
        Sharpe/Sortino (risk-free: FBIL 3-month par yield unless `rf` is given), today's mix over the last year with
        each holding's risk contribution, and stress scenarios."""
        from finresearch.portfolio.analytics import risk as risk_payload
        from finresearch.portfolio.analytics import stress_scenarios
        from finresearch.portfolio.history import Fetcher

        hist, today, fp = await history()
        if not hist.ok:
            from finresearch.portfolio.analytics import RiskFree

            return risk_payload(hist, RiskFree(0.0, "not needed: no history yet", None, None), {}, [])
        rfree = await _risk_free(rf)
        sectors = await _sectors(hist) if hist.ok else {}

        async def make_stress():
            async with Fetcher(src()) as f:
                return await stress_scenarios(hist, f, store(), today)

        stress = await cache.get(("stress", fp, today), HISTORY_TTL_S, make_stress) if hist.ok else []
        return risk_payload(hist, rfree, sectors, stress)

    @app.get("/api/portfolio/analytics/concentration")
    async def concentration() -> dict[str, Any]:
        """Single-stock, sector and business-group weights with your limits (portfolio.limits.position_limit: the
        profile's max position, else 5 / 8 / 10 % by risk appetite; sector and group rules of thumb), HHI and the
        effective number of holdings."""
        from finresearch.portfolio.analytics import concentration as conc
        from finresearch.portfolio.limits import position_limit
        from finresearch.suggest.advisor import load_profile

        hist, _, _ = await history()
        sectors = await _sectors(hist) if hist.ok else {}
        with session_scope() as s:
            st = get_settings_row(s)
            lim = position_limit(load_profile(s))
        return conc(hist, sectors, st["groups"], lim, coverage=_coverage(hist))

    def _coverage(hist: Any) -> dict[str, Any] | None:
        """Which held funds are looked through (#214), from the same positions and values as the figures here."""
        from decimal import Decimal

        from finresearch.portfolio import lookthrough as lt

        try:
            pos = [p for p in hist.positions if p.value > 0]
            funds = [lt.FundValue(getattr(p.scheme, "code", None), getattr(p.scheme, "name", None) or p.name,
                                  Decimal(str(p.value)), getattr(p.scheme, "amc", None))
                     for p in pos if p.asset_type == "mf"]  # fmt: skip
            total = sum((Decimal(str(p.value)) for p in pos), Decimal(0))
            return lt.coverage(total, funds, lt.PortfolioStore(), src().today())
        except Exception:
            return None

    @app.get("/api/portfolio/analytics/costs")
    async def costs() -> dict[str, Any]:
        """Weighted expense ratio (AMFI's monthly TER file) and, for regular plans, the cost of switching to the direct
        plan (tax, exit load, stamp duty) before the yearly saving and the break-even."""
        from finresearch.fincalc.dates import fiscal_year
        from finresearch.portfolio.analytics import FundLots
        from finresearch.portfolio.analytics import costs as costs_payload
        from finresearch.portfolio.report import disposal_rows, holding_tax, load
        from finresearch.portfolio.tax import gains_of
        from finresearch.suggest.advisor import load_profile

        hist, today, _ = await history()
        table, note = None, None
        if any(p.asset_type == "mf" for p in hist.positions):
            month = today.replace(day=1)

            async def make():
                if seams().ter is not None:
                    return await seams().ter(month)
                from finresearch.adapters.amfi import AmfiClient

                async with AmfiClient() as amfi:
                    try:
                        got = await amfi.ter(month)
                    except Exception:
                        got = {}
                    # early in a month the new file can be missing or empty: use the previous month's
                    return got or await amfi.ter((month - timedelta(days=1)).replace(day=1))

            try:
                table = await cache.get(("ter", month), 12 * 3600, make)
                note = "AMFI total expense ratio file (monthly, latest row per scheme)"
                if not table:
                    note = "AMFI's TER file had no rows for this month or the last"
            except Exception as e:
                note = f"{type(e).__name__}"
        with session_scope() as s:
            data = load(s)
            fy = fiscal_year(today)
            base_gains = gains_of([r for r in disposal_rows(data) if r.fy == fy])
            lots = {}
            for h in data.holdings:
                if h.asset_type != "mf":
                    continue
                open_lots = [(lot.acquired, lot.open_quantity, lot.cost_per_unit) for lot in data.lots.get(h.id, [])
                             if lot.open_quantity > Decimal("0.0005")]  # fmt: skip
                lots[h.id] = FundLots(h.id, holding_tax(h), open_lots)
            slab = Decimal(str(load_profile(s).tax_slab_pct)) / 100
            st = get_settings_row(s)
        return costs_payload(hist, table, note, lots, base_gains, slab, st["exit_loads"], today)

    @app.get("/api/portfolio/analytics/settings")
    def get_settings_route() -> dict[str, Any]:
        from finresearch.portfolio.analytics import GROUP_SOURCE, GROUP_SYMBOLS

        with session_scope() as s:
            return {**get_settings_row(s), "seed_groups": sorted(GROUP_SYMBOLS), "group_source": GROUP_SOURCE}

    @app.put("/api/portfolio/analytics/settings")
    def put_settings(body: AnalyticsSettings) -> dict[str, Any]:
        """Merge business-group corrections (instrument key -> group, "" for none, null to drop the correction) and
        exit loads (holding id -> {pct, days}, null to clear)."""
        from finresearch.db.models import PortfolioSetting

        with session_scope() as s:
            cur = get_settings_row(s)
            for k, v in (body.groups or {}).items():
                if len(k) > 60:
                    raise HTTPException(422, "instrument key too long")
                if v is None:
                    cur["groups"].pop(k, None)
                else:
                    cur["groups"][k] = v.strip()[:60]
            for k, v in (body.exit_loads or {}).items():
                if not k.isdigit():
                    raise HTTPException(422, "exit loads are keyed by holding id")
                if v is None:
                    cur["exit_loads"].pop(k, None)
                else:
                    cur["exit_loads"][k] = v.model_dump()
            row = s.get(PortfolioSetting, SETTINGS_KEY)
            if row is None:
                s.add(PortfolioSetting(key=SETTINGS_KEY, value=cur))
            else:
                row.value = cur
        return cur
