"""/api/portfolio/analytics/*: performance vs NIFTYBEES, risk, concentration and costs of the personal portfolio.

Every route is a GET that reads the local database and public price history, except PUT .../settings, which stores
the user's business-group corrections and exit loads in `portfolio_setting` ("analytics"). The daily value history is
rebuilt from the transactions (`portfolio.history`), cached in memory for 30 minutes per transaction set and on disk
per instrument, so only the first build after new transactions reads prices from NSE/BSE/AMFI. The GETs write
nothing to the database (#260): the canonical value history the alerts, brief, digest and net worth read
(`portfolio.series`, #239) is stored by the monitor (daily pass, and as soon as the transactions change), and the
performance payload reconciles the "as shown" snapshots against this build.

Privacy: personal data stays in the local database; nothing here calls an LLM.

Test seams: `app.state.markets` (a `MarketSources` with fake NSE/AMFI) and `app.state.analytics_sources` (an
`AnalyticsSources` with a fake TER table). The risk-free rate is signals.rates' (#479: owner-entered else the RBI
repo-rate proxy, no network); tests set `rates.OWNER`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from finresearch.db import session_scope

log = logging.getLogger(__name__)

HISTORY_TTL_S = 1800
SETTINGS_KEY = "analytics"


@dataclass
class AnalyticsSources:
    ter: Callable[[date], Awaitable[dict]] | None = None  # month -> {ter_key(name): SchemeTer}
    index_series: Callable[[str, date, date], dict] | None = None  # (symbol, start, end) -> closes (#345)


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
            # read-only (#260): the canonical series the alerts read (portfolio.series) is stored by the monitor
            return hist

        return await cache.get(key, HISTORY_TTL_S, make), today, fp

    app.state.portfolio_history = history  # shared with the behaviour report (api/journal.py)

    def _risk_free(rf: float | None, hist: Any):
        """The user's figure (one annual rate), else each month's own rate over the realised window and the
        factsheet-style rate on its last day (#479: signals.rates; no FBIL, no network)."""
        from finresearch.portfolio.analytics import RiskFree, realised_window
        from finresearch.signals import rates

        if rf is not None:
            return RiskFree(rf / 100, f"your figure: {rf:g} % a year", None, None, user_set=True)
        win = realised_window(hist)
        if win is None:
            return RiskFree(None, "not needed: no history yet", None, None)
        start, end = win
        months, why = rates.months_for(start, end)
        period = rates.accrual_fn(months)
        acc = period(start, end)
        annual = None if acc is None or end <= start else (1 + acc) ** (365 / (end - start).days) - 1
        return RiskFree(annual, f"{rates.SOURCE_LABEL}: {rates.MONTHLY_LABEL}", end.isoformat(),
                        "signals.rates (#479)", period=period, factsheet=rates.factsheet(end),
                        detail=rates.monthly_json(months, why))  # fmt: skip

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

        hist, today, fp = await history()
        out = perf(hist)
        if hist.ok:  # the "as shown" snapshots against this (canonical) series, day by day (#239)
            from finresearch.portfolio import series

            with session_scope() as s:
                out["reconciliation"] = series.reconcile_db(s, series.from_history(hist, fp, today), today)
        return out

    @app.get("/api/portfolio/analytics/risk")
    async def risk(
        rf: float | None = Query(None, ge=0, le=20, description="risk-free rate, % a year"),
        assume_beta1: bool = Query(
            False, description="hypothetical fall: beta 1 for short-history equity (#423)"
        ),
    ) -> dict[str, Any]:
        """Realised volatility, beta and tracking error vs NIFTYBEES, max drawdown and recovery, historical VaR/CVaR,
        Sharpe/Sortino (risk-free, #479: each day's own month's average 91-day rate, owner-entered else the RBI
        repo-rate proxy, with the factsheet-style pair beside it; `rf` is one annual rate you give), today's mix over
        the last year with each holding's risk contribution, and stress scenarios."""
        from finresearch.portfolio.analytics import hypothetical_fall, stress_scenarios
        from finresearch.portfolio.analytics import risk as risk_payload
        from finresearch.portfolio.history import Fetcher

        hist, today, fp = await history()
        if not hist.ok:
            from finresearch.portfolio.analytics import RiskFree

            return risk_payload(hist, RiskFree(0.0, "not needed: no history yet", None, None), {}, [])
        rfree = _risk_free(rf, hist)
        sectors = await _sectors(hist) if hist.ok else {}

        async def make_stress():
            async with Fetcher(src()) as f:
                return await stress_scenarios(hist, f, store(), today)

        stress = await cache.get(("stress", fp, today), HISTORY_TTL_S, make_stress) if hist.ok else []
        # the hypothetical needs no fetch and depends on the option, so it is computed per request, outside the cache
        if hist.positions:
            stress = [*stress, hypothetical_fall(hist, assume_beta1=assume_beta1)]
        from finresearch.portfolio.var_ledger import backtest

        with session_scope() as s:  # read only: the monitor logs and resolves the VaR forecasts (#399)
            vl = backtest(s)
            s.rollback()
        return risk_payload(hist, rfree, sectors, stress, var_ledger=vl)

    @app.get("/api/portfolio/analytics/stress-replay")
    async def stress_replay() -> dict[str, Any]:
        """Historical stress replay (#345): today's holdings replayed through Sep-Nov 2008, May-Aug 2013, Feb-Mar 2020,
        3-5 Jun 2024 and Feb-Mar 2026. Loss, worst day, peak-to-trough, recovery and the holdings that drive it;
        holdings with no price path are listed as unmapped. The first call reads each holding's prices for the windows
        (cached on disk for good)."""
        from finresearch.portfolio import stress_replay as sr
        from finresearch.portfolio.history import Fetcher

        hist, today, fp = await history()
        if not hist.ok:
            return {"available": False, "reason": hist.reason or "no value history yet", "scenarios": [],
                    "method": sr.METHOD}  # fmt: skip

        async def make():
            async with Fetcher(src()) as f:
                return await sr.load(hist, f, store(), today, index_series=seams().index_series)

        return {"available": True, **await cache.get(("replay", fp, today), HISTORY_TTL_S, make)}

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
