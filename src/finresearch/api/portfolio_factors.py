"""GET /api/portfolio/analytics/factors: the portfolio's and each holding's exposures to IIMA's Indian market, size,
value and momentum factors (#348; `portfolio.factor_exposures`).

A GET that writes nothing to the database. It reuses the analytics routes' value history
(`app.state.portfolio_history`, so `add_portfolio_analytics_routes` must be added first), reads the factor file through
`adapters.iima_factors` (PoliteClient, disk-cached per release; kept in memory for 12 hours, a failure never cached)
and each holding's closes over the factor window through the analytics PriceStore.

Test seam: `app.state.factor_source`, an async `() -> FactorSeries` used instead of IIMA. Privacy: nothing here calls
an LLM.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from finresearch.api.errors import public_error

FACTORS_TTL_S = 12 * 3600
RESULT_TTL_S = 1800


def add_portfolio_factor_routes(app: FastAPI) -> None:
    from finresearch.api.markets import TtlCache

    cache = TtlCache()

    async def _factor_series():
        seam = getattr(app.state, "factor_source", None)
        if seam is not None:
            return await seam()
        from finresearch.adapters.iima_factors import IimaFactorsClient

        async with IimaFactorsClient() as c:
            return await c.latest_daily()

    @app.get("/api/portfolio/analytics/factors")
    async def factors() -> dict[str, Any]:
        """Time-series regressions (Newey-West errors) of daily excess returns on IIMA's four factors, over the last
        three years the factor file covers; too little overlap gives a reason, never a 0 exposure."""
        from finresearch.config import get_settings
        from finresearch.portfolio import factor_exposures as fx
        from finresearch.portfolio.history import Fetcher, PriceStore

        hist, today, fp = await app.state.portfolio_history()
        try:
            fs = await cache.get(("iima",), FACTORS_TTL_S, _factor_series)
        except Exception as e:
            return fx.exposures(hist, None, {}, source_error=public_error(e, drop_query=True))
        if not hist.ok:
            return fx.exposures(hist, fs, {})
        store = PriceStore(get_settings().state_dir / "portfolio_history")

        async def make():
            async with Fetcher(app.state.markets) as f:  # set by portfolio_history() above
                return await fx.load(hist, fs, f, store, today)

        return await cache.get(("factors", fp, today, fs.release, fs.last), RESULT_TTL_S, make)
