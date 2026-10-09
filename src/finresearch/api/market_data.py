"""/api/connections/groww/market-data: what the Groww Trade API is supplying to the app and today's call budget (#267).

Counts and labels only: no token, key, secret or account detail is ever part of this payload (tests scan it)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def add_market_data_routes(app: FastAPI) -> None:
    @app.get("/api/connections/groww/market-data")
    async def groww_market_data() -> dict[str, Any]:
        """Whether Groww's market data is in use now, which data it supplies (and which it does not), when it last
        answered for each kind, and today's Groww calls by rate category against Groww's published limits."""
        from finresearch.adapters import groww_market

        return groww_market.MARKET.status()
