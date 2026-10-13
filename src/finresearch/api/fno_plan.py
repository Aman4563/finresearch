"""F&O trade plans and the market-context panel (#317): POST /api/fno/plan (a card for any strategy), GET
/api/fno/{symbol}/suggest (the library recipes on today's chain) and GET /api/fno/{symbol}/context.

Analysis only: no route here places, modifies or cancels an order. The card's one-click actions use the paper book
and exit-rule routes (api/fno_book.py)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from finresearch.db import session_scope


class PlanLegIn(BaseModel):
    right: Literal["call", "put", "future"]
    strike: Decimal = Field(gt=0)
    side: Literal["buy", "sell"]
    lots: int = Field(1, ge=1, le=100)


class PlanIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    expiry: date
    legs: list[PlanLegIn] = Field(min_length=1, max_length=8)


async def _events(sym: str, today: date) -> list[tuple[date, str]] | None:
    """Results / RBI dates before expiry (fnobook.events); None when they cannot be read (unknown, never "none")."""
    from finresearch.fnobook import events, sources

    try:
        return await events.event_dates(sym, today, sources.SOURCES)
    except Exception:
        return None


def add_fno_plan_routes(app: FastAPI, fno: Callable[[], Any]) -> None:
    """`fno` = the app's option-chain client factory (Groww first, else NSE; tests pass a fake)."""

    async def inputs(f: Any, sym: str, expiry: date, today: date) -> Any:
        from finresearch.fnobook import events
        from finresearch.signals import fno as fs
        from finresearch.signals import tradeplan as tpl
        from finresearch.signals.rates import risk_free
        from finresearch.suggest.advisor import load_profile

        closes = await fs.underlying_closes(f, sym, today)
        series, src = fs.iv_series_info(sym)
        with session_scope() as s:
            p = load_profile(s)
        rate = (await risk_free(max((expiry - today).days, 0.5) / 365))["rate"]
        strategies, head = tpl.backtest_rows()
        return tpl.Inputs(today=today, closes=closes, iv_series=[v for _, v, _ in series], iv_src=src,
                          iv_as_of=series[-1][0] if series else None, capital=float(p.fno_capital_inr),
                          max_loss_pct=float(p.fno_max_loss_pct), brokerage=float(p.fno_brokerage_per_order_inr),
                          defined_risk_only=p.fno_defined_risk_only, rate=rate, strategies=strategies,
                          backtest_head=head, paper=tpl.paper_trades(), events=await _events(sym, today),
                          is_index=events.is_index(sym))  # fmt: skip

    @app.post("/api/fno/plan")
    async def fno_plan(body: PlanIn) -> dict[str, Any]:
        """The trade-plan card for the legs (their lots are the ratio; the profile's budget sets the size)."""
        from finresearch.adapters.nse_fno import lot_size_for
        from finresearch.fincalc.dates import now_ist
        from finresearch.signals import fno as fs
        from finresearch.signals import tradeplan as tpl

        sym, today = body.symbol.strip().upper(), now_ist().date()
        try:
            fs.check_expiry(body.expiry, today)
        except fs.ExpiredContract as e:
            raise HTTPException(422, str(e)) from e
        async with fno() as f:
            chain = await f.option_chain(sym, body.expiry)
            try:
                lot = lot_size_for(await f.lot_sizes(), sym, body.expiry)
            except Exception:
                lot = None
            if not lot:
                raise HTTPException(422, f"no NSE lot size for {sym} {body.expiry:%b-%y}")
            if chain.underlying is None or not chain.rows:
                raise HTTPException(422, f"no option chain for {sym} {body.expiry}")
            x = await inputs(f, sym, body.expiry, today)
        legs = [fs.LegIn(y.right, float(y.strike), y.side, y.lots) for y in body.legs]
        try:
            card = tpl.build(chain, lot, legs, x)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {**card, "risk_notice": fs.risk_notice()}

    @app.get("/api/fno/{symbol}/suggest")
    async def fno_suggest(symbol: str) -> dict[str, Any]:
        """Each non-overlay pre-registered recipe applied to today's chain, as a trade-plan card."""
        from finresearch.adapters.nse_fno import lot_size_for
        from finresearch.fincalc.dates import now_ist
        from finresearch.signals import fno as fs
        from finresearch.signals import tradeplan as tpl

        sym, today = symbol.strip().upper(), now_ist().date()
        async with fno() as f:
            expiries, _ = await f.contract_info(sym)
            expiries = fs.live_expiries(expiries, today)
            if not expiries:
                raise HTTPException(422, f"no live F&O expiries for {sym}")
            try:
                lots = await f.lot_sizes()
            except Exception:
                lots = {}

            async def get_chain(e: date) -> Any:
                try:
                    return await f.option_chain(sym, e)
                except Exception:
                    return None

            async def lot_for(e: date) -> int | None:
                return lot_size_for(lots, sym, e)

            x = await inputs(f, sym, expiries[0], today)
            cards = await tpl.suggest(sym, expiries, get_chain, lot_for, x)
        return {"symbol": sym, "today": today.isoformat(), "cards": cards, "risk_notice": fs.risk_notice(),
                "note": "The library's pre-registered recipes (backtest.specs) applied to today's chain, each with its "
                        "own out-of-sample result. A recipe shown here is not a recommendation."}  # fmt: skip

    @app.get("/api/fno/{symbol}/context")
    async def fno_context(symbol: str, expiry: date) -> dict[str, Any]:
        """The market-context panel for the underlying: every indicator with what it measures and its evidence."""
        from finresearch.fincalc.dates import now_ist
        from finresearch.fnobook import events, sources
        from finresearch.signals import fno as fs
        from finresearch.signals import fno_context as ctx
        from finresearch.signals.rates import risk_free

        sym, today = symbol.strip().upper(), now_ist().date()
        try:
            fs.check_expiry(expiry, today)
        except fs.ExpiredContract as e:
            raise HTTPException(422, str(e)) from e
        async with fno() as f:
            chain = await f.option_chain(sym, expiry)
            # NSE's ~90 daily closes only when the stored bhavcopy history is too short (no needless request)
            closes = (
                await fs.underlying_closes(f, sym, today) if ctx.needs_network_closes(sym, today) else None
            )
            try:
                exp = fs.live_expiries((await f.contract_info(sym))[0], today)
            except Exception:
                exp = [expiry]
        try:
            strip = await events.strip(sym, today, sources.SOURCES, exp)
        except Exception:
            strip = None
        rate = (await risk_free(max((expiry - today).days, 0.5) / 365))["rate"]
        return ctx.build(chain, today=today, rate=rate, network_closes=closes, events=strip)
