"""/api/portfolio/health: the data-health panel at the top of the Portfolio page (#219; portfolio.health).

It reads the same answers the page's own cards read, by calling the sibling routes in process: /api/portfolio (prices),
/api/lookthrough (fund coverage) and /api/portfolio/analytics/performance (history length). Each runs under a time
limit; one that fails or times out makes its row "unknown", which counts as 0 % in the overall figure.
Privacy: local only; nothing here calls an LLM.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute

from finresearch.db import session_scope

TIMEOUT_S = 45.0


def add_portfolio_health_routes(app: FastAPI) -> None:
    def endpoint(path: str):
        for r in app.routes:
            if isinstance(r, APIRoute) and r.path == path and "GET" in r.methods:
                return r.endpoint
        return None

    async def call(path: str, **kw: Any) -> tuple[dict[str, Any] | None, str | None]:
        fn = endpoint(path)
        if fn is None:
            return None, "not available"
        try:
            got = fn(**kw)
            if inspect.isawaitable(got):
                got = await asyncio.wait_for(got, TIMEOUT_S)
            return got, None
        except TimeoutError:
            return None, f"timed out after {TIMEOUT_S:.0f} s"
        except HTTPException as e:
            return None, str(e.detail)[:200]
        except Exception as e:  # a failed check is shown as unknown, never as complete
            return None, type(e).__name__

    @app.get("/api/portfolio/health")
    async def portfolio_health() -> dict[str, Any]:
        """Coverage of each input to the portfolio analysis, what each gap blocks and how to fix it, and an overall
        completeness figure with stated weights (portfolio.health)."""
        from datetime import date

        from finresearch.portfolio.health import compute

        snap, snap_error = await call("/api/portfolio", prices="live")
        if snap is None:
            raise HTTPException(503, f"the portfolio could not be valued ({snap_error})")
        (lt, lt_error), (perf, perf_error) = await asyncio.gather(
            call("/api/lookthrough", top=5), call("/api/portfolio/analytics/performance")
        )
        today = date.fromisoformat(snap["as_of"])  # the valuation's own day (the markets clock)
        with session_scope() as s:
            return compute(s, snap, today, lt=lt, lt_error=lt_error, perf=perf, perf_error=perf_error)
