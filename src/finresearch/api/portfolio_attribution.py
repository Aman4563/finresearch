"""GET /api/portfolio/analytics/liquidity and /api/portfolio/analytics/attribution (#348): days to liquidate each
holding from stored NSE volume (`portfolio.liquidity`) and Brinson-Fachler attribution by sector against the Nifty 50
(`portfolio.attribution`).

GETs that write nothing to the database: they reuse the analytics routes' value history (`app.state.portfolio_history`,
so `add_portfolio_analytics_routes` must be added first) and read the stored NSE bhavcopy (`equity_eod`, `index_eod`).
The attribution also reads the Nifty 50 constituents with NSE's free-float market caps through
`adapters.nse_index_weights` (PoliteClient; kept in memory for 12 hours, a failure never cached).

Test seam: `app.state.benchmark_source`, an async `() -> IndexWeights` used instead of NSE. Privacy: nothing here calls
an LLM.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Query

from finresearch.api.errors import public_error

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

    _ = public_error  # used by the attribution route
