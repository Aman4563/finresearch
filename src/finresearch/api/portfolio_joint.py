"""GET /api/portfolio/analytics/joint-risk and POST .../joint-risk/pretrade (#397): one risk view across the investment
portfolio and the F&O book (`portfolio.joint_risk`, `fincalc.joint_risk`).

Both write nothing to the database: the POST only carries the candidate legs (a body), it stores nothing. They reuse
the analytics routes' value history (`app.state.portfolio_history`, so `add_portfolio_analytics_routes` must be added
first), read option chains for the open book (and the candidate), stored index closes and `fno_mark`, and fill only
the market-data price caches (the holdings' prices on the replay days).

Test seams: `app.state.fno_sources` (the option-chain sources, default `fnobook.sources.SOURCES`) and
`app.state.analytics_sources.index_series`. Analysis only: nothing here places, modifies or cancels an order.
Privacy: both books are personal data; nothing here calls an LLM.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, Query
from pydantic import BaseModel, Field, model_validator

REPLAY_TTL_S = 1800


class JointLegIn(BaseModel):
    underlying: str = Field(min_length=1, max_length=30)
    expiry: date
    right: Literal["call", "put", "future"]
    strike: Decimal = Field(ge=0)  # ignored for a future
    qty: int = Field(ge=-100_000, le=100_000)  # signed units (lots x lot size): + long, - short

    @model_validator(mode="after")
    def _check(self) -> JointLegIn:
        if self.qty == 0:
            raise ValueError("qty must not be 0")
        if self.right != "future" and self.strike <= 0:
            raise ValueError("an option leg needs a strike above 0")
        return self


class PretradeIn(BaseModel):
    legs: list[JointLegIn] = Field(min_length=1, max_length=8)


def add_portfolio_joint_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    from finresearch.api.markets import TtlCache

    cache = TtlCache()

    def now() -> datetime:
        from finresearch.fincalc.dates import now_ist

        return clock() if clock else now_ist()

    def sources() -> Any:
        from finresearch.fnobook import sources as src

        return getattr(app.state, "fno_sources", None) or src.SOURCES

    def holidays() -> set[date]:
        from finresearch.adapters.groww_market import _holidays

        return _holidays()

    async def _base(assume_beta1: bool) -> tuple[Any, list[Any], date, str]:
        from finresearch.portfolio import joint_risk as jl

        hist, today, fp = await app.state.portfolio_history()
        return hist, jl.holdings(hist, assume_beta1=assume_beta1), today, fp

    @app.get("/api/portfolio/analytics/joint-risk")
    async def joint_risk(
        assume_beta1: bool = Query(False, description="beta 1 for short-history equity (#423), labelled"),
    ) -> dict[str, Any]:
        """D_total (holdings' beta-weighted value plus the book's NIFTY-equivalent ₹ delta), L(m) on both books with
        F&O's share and the hedge-calculator trigger, the single-day historical replays on both books and the
        forward test's day count. Unknown parts leave the combined figures None with the reasons."""
        from finresearch.config import get_settings
        from finresearch.db import session_scope
        from finresearch.fincalc import book_scenarios as bs
        from finresearch.portfolio import joint_risk as jl
        from finresearch.portfolio.history import Fetcher, PriceStore
        from finresearch.signals.base import DISCLAIMER

        hist, hs, today, fp = await _base(assume_beta1)
        t, hol = now(), holidays()
        b, _, notes = await jl.book(t, sources=sources())
        seams = getattr(app.state, "analytics_sources", None)
        index_series = getattr(seams, "index_series", None)

        async def make() -> dict[date, Any]:
            store = PriceStore(get_settings().state_dir / "portfolio_history")
            async with Fetcher(app.state.markets) as f:  # set by portfolio_history() above
                return await jl.replay_moves(hist, f, store, today, index_series=index_series)

        moves = await cache.get(("joint-replay", fp, today), REPLAY_TTL_S, make) if hs else {}
        with session_scope() as s:
            fwd = jl.forward_status(s, today)
        out = jl.view(hs, b, notes, bs.horizon_clock(t, 1, hol), moves, fwd, hol)
        return {"as_of": t.isoformat(), "assume_beta1": assume_beta1, "disclaimer": DISCLAIMER,
                "privacy": "Computed on this machine from your local database; never sent to an LLM.", **out}  # fmt: skip

    @app.post("/api/portfolio/analytics/joint-risk/pretrade")
    async def joint_pretrade(
        body: PretradeIn, assume_beta1: bool = Query(False, description="as on the GET")
    ) -> dict[str, Any]:
        """How D_total and L(-10 %) change if the legs were added to today's book. Stores nothing."""
        from finresearch.fincalc import book_scenarios as bs
        from finresearch.fincalc import joint_risk as jr
        from finresearch.portfolio import joint_risk as jl

        _, hs, _, _ = await _base(assume_beta1)
        t = now()
        cand = [bs.BookLeg(x.underlying.strip().upper(), x.expiry, x.right,
                           0.0 if x.right == "future" else float(x.strike), x.qty, None, None, "candidate")
                for x in body.legs]  # fmt: skip
        b, priced, notes = await jl.book(t, sources=sources(), candidate=cand)
        p = jr.pretrade(hs, b, priced, bs.horizon_clock(t, 1, holidays()))
        return {"as_of": t.isoformat(), "notes": notes, **jl.pretrade_json(p)}
