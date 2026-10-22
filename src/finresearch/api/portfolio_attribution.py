"""GET /api/portfolio/analytics/liquidity and /api/portfolio/analytics/attribution (#348): days to liquidate each
holding from stored NSE volume (`portfolio.liquidity`) and Brinson-Fachler attribution by sector against the Nifty 50
(`portfolio.attribution`).

GETs that write nothing to the database: they reuse the analytics routes' value history (`app.state.portfolio_history`,
so `add_portfolio_analytics_routes` must be added first) and read the stored NSE bhavcopy (`equity_eod`, `index_eod`).
The attribution also reads the Nifty 50 member list and each member's free-float market cap from its NSE quote
(`adapters.nse_index_weights`; PoliteClient and the quote source, kept in memory for 12 hours; a failure, or a list
with a failed quote, is never kept as data).

Test seam: `app.state.benchmark_source`, an async `() -> IndexWeights` used instead of NSE. Privacy: nothing here calls
an LLM.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI, Query

from finresearch.api.errors import public_error

WEIGHTS_TTL_S = 12 * 3600
QUOTE_TTL_S = 12 * 3600
RESULT_TTL_S = 1800
LIQUIDITY_SOURCE = {"name": "NSE full bhavcopy with delivery (sec_bhavdata_full), stored daily (#373)",
                    "note": "NSE volume only; BSE volume is not collected."}  # fmt: skip


def add_portfolio_attribution_routes(app: FastAPI) -> None:
    @app.get("/api/portfolio/analytics/liquidity")
    async def liquidity(participation: float = Query(0.20, gt=0, le=1)) -> dict[str, Any]:
        """Days to sell each holding at `participation` of its average daily NSE volume (a convention)."""
        from finresearch.db import session_scope
        from finresearch.portfolio import liquidity as lq
        from finresearch.portfolio.analytics import PRIVACY
        from finresearch.signals.base import DISCLAIMER

        hist, today, _fp = await app.state.portfolio_history()
        positions = [p for p in hist.positions if p.value > 0]
        syms = [s for s in (lq.nse_symbol(p) for p in positions if p.asset_type != "mf") if s]
        with session_scope() as s:
            advs = lq.read_advs(s, syms, today)
            s.rollback()  # reads only
        out = lq.liquidity(positions, advs, participation=participation)
        return {**out, "as_of": today.isoformat(), "source": LIQUIDITY_SOURCE, "privacy": PRIVACY,
                "disclaimer": DISCLAIMER, "available": bool(positions),
                "reason": None if positions else (hist.reason or "no holdings")}  # fmt: skip

    from finresearch.api.markets import TtlCache

    cache = TtlCache()

    async def _quote(symbol: str):
        return await cache.get(
            ("quote", symbol), QUOTE_TTL_S, lambda: app.state.markets.get_quote(symbol, "NSE")
        )

    async def _weights() -> dict[str, Any]:
        """{"iw": IndexWeights, "unreachable": members whose quote failed}; a non-empty `unreachable` keeps the
        answer only briefly (TtlCache's degraded rule), so a failed quote is retried, never cached as data."""
        seam = getattr(app.state, "benchmark_source", None)
        if seam is not None:
            iw = await seam()
        else:
            from finresearch.adapters.nse_index_weights import NiftyWeightsClient

            async with NiftyWeightsClient() as c:
                iw = await c.nifty50(_quote)
        return {"iw": iw, "unreachable": list(getattr(iw, "unreachable", []) or [])}

    async def _sector_of(symbol: str) -> str | None:
        """NSE's sector level ("Information Technology") of a stock outside the index, from its quote (cached)."""
        try:
            q = await cache.get(
                ("quote", symbol), QUOTE_TTL_S, lambda: app.state.markets.get_quote(symbol, "NSE")
            )
        except Exception:
            return None
        return getattr(q, "sector", None) or None

    @app.get("/api/portfolio/analytics/attribution")
    async def attribution(period: Literal["1m", "3m", "6m"] = "3m") -> dict[str, Any]:
        """Brinson-Fachler attribution of today's holdings by sector against the Nifty 50 over the period."""
        from finresearch.config import get_settings
        from finresearch.portfolio import attribution as at
        from finresearch.portfolio.history import Fetcher, PriceStore

        hist, today, fp = await app.state.portfolio_history()
        try:
            got = await cache.get(("nifty50",), WEIGHTS_TTL_S, _weights)
        except Exception as e:
            return at.unavailable(
                period, f"Nifty 50 weights could not be read: {public_error(e, drop_query=True)}"
            )
        iw = got["iw"]
        if not hist.ok:
            return at.unavailable(period, hist.reason or "no value history yet")
        store = PriceStore(get_settings().state_dir / "portfolio_history")

        async def make():
            async with Fetcher(app.state.markets) as f:  # set by portfolio_history() above
                out = await at.load(hist, iw, today, period, f, store, _sector_of)
            return {**out, "unreachable": got["unreachable"]}  # non-empty: kept only briefly

        return await cache.get(("attribution", fp, today, period, iw.as_of), RESULT_TTL_S, make)
