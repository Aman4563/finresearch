"""F&O trade plans and the market-context panel (#317): POST /api/fno/plan (a card for any strategy), GET
/api/fno/{symbol}/suggest (the library recipes on today's chain) and GET /api/fno/{symbol}/context.

Analysis only: no route here places, modifies or cancels an order. The card's one-click actions use the paper book
and exit-rule routes (api/fno_book.py)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from finresearch.db import session_scope
from finresearch.fnobook import card_log


class PlanLegIn(BaseModel):
    right: Literal["call", "put", "future"]
    strike: Decimal = Field(gt=0)
    side: Literal["buy", "sell"]
    lots: int = Field(1, ge=1, le=100)


class PlanIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    expiry: date
    legs: list[PlanLegIn] = Field(min_length=1, max_length=8)


async def _events(sym: str, today: date, expiry: date) -> list[tuple[date, str]] | None:
    """Results and tier-1 macro dates before expiry (fnobook.events); None when they cannot be read (unknown, never
    "none")."""
    from finresearch.fnobook import events, sources

    try:
        return await events.event_dates(sym, today, sources.SOURCES, until=expiry)
    except Exception:
        return None


def _calendar(today: date) -> tuple[list[Any], list[Any]] | None:
    """The event calendar (#367) for the plan's tier-1 check: events from the day before `today` on (an after-close
    release lands on the next session) and every coverage row, so the card log (#360) keeps what the check read and
    no more. None when it cannot be read (the check reads unknown)."""
    from finresearch.eventcal import store

    try:
        with session_scope() as s:
            ev, cov = store.load(s)
    except Exception:
        return None
    return [e for e in ev if e.day >= today - timedelta(days=1)], cov


async def plan_inputs(f: Any, sym: str, expiry: date, today: date, now: datetime | None = None) -> Any:
    """Everything a trade-plan card reads besides the chain (signals.tradeplan.Inputs): closes, the IV series, the
    profile's F&O budget, the rate, the saved backtest, the paper book and the event dates. Also used by #327.
    `now` = the live clock T is measured from (#359); None = 15:30 IST on `today`."""
    from finresearch.fnobook import events
    from finresearch.signals import fno as fs
    from finresearch.signals import fno_context
    from finresearch.signals import tradeplan as tpl
    from finresearch.signals.rates import risk_free
    from finresearch.suggest.advisor import load_profile

    closes = await fs.underlying_closes(f, sym, today)
    series, src = fs.iv_series_info(sym)
    with session_scope() as s:
        p = load_profile(s)
    from finresearch.fincalc.dates import session_clock, years_to_expiry

    rate = (await risk_free(years_to_expiry(expiry, session_clock(today, now))))[
        "rate"
    ]  # tenor to the minute (#359)
    strategies, head = tpl.backtest_rows()
    try:  # #357: the reliability line; an unreadable ledger leaves calibration unknown
        from finresearch.signals.reliability import scored_events

        with session_scope() as s:
            scored: int | None = scored_events(s, "fno", tpl.PLAN_METHOD)
    except Exception:
        scored = None
    return tpl.Inputs(today=today, closes=closes, iv_series=[v for _, v, _ in series], iv_src=src,
                      iv_as_of=series[-1][0] if series else None, capital=float(p.fno_capital_inr),
                      max_loss_pct=float(p.fno_max_loss_pct), brokerage=float(p.fno_brokerage_per_order_inr),
                      defined_risk_only=p.fno_defined_risk_only, rate=rate, strategies=strategies,
                      backtest_head=head, paper=tpl.paper_trades(), events=await _events(sym, today, expiry),
                      is_index=events.is_index(sym), book=await tpl.load_book(today),
                      iv_points=fno_context.iv_rank_input(sym)[0], scored=scored, now=now,
                      calendar=_calendar(today))  # fmt: skip


def add_fno_plan_routes(app: FastAPI, fno: Callable[[], Any]) -> None:
    """`fno` = the app's option-chain client factory (Groww first, else NSE; tests pass a fake)."""

    inputs = plan_inputs

    async def bidask_for(f: Any, sym: str, expiry: date) -> tuple[Any, str] | None:
        """NSE's chain for the bid/ask the plan's chain lacks (#324; cached in signals.bidask), with its label."""
        from finresearch.fincalc.dates import now_ist
        from finresearch.signals import bidask as ba

        now = now_ist()
        c = await ba.nse_chain(f, sym, expiry, now)
        return None if c is None else (c, ba.label(c, now.date(), now))

    @app.post("/api/fno/plan")
    async def fno_plan(body: PlanIn) -> dict[str, Any]:
        """The trade-plan card for the legs (their lots are the ratio; the profile's budget sets the size)."""
        from finresearch.adapters.nse_fno import lot_size_for
        from finresearch.fincalc.dates import now_ist
        from finresearch.signals import bidask as ba
        from finresearch.signals import fno as fs
        from finresearch.signals import tradeplan as tpl

        now = now_ist()
        sym, today = body.symbol.strip().upper(), now.date()
        legs = [fs.LegIn(y.right, float(y.strike), y.side, y.lots) for y in body.legs]
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
            x = await inputs(f, sym, body.expiry, today, now)
            # the plan's chain has no bid/ask for a leg (Groww's has none): NSE's, once, cached (#324)
            need = tpl.needs_bidask(chain, legs)
            fb = await bidask_for(f, sym, body.expiry) if need else None
        try:
            card = tpl.build(chain, lot, legs, x, bidask=fb, bidask_note=ba.FAILED_NOTE if need else None)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        out = {**card, "risk_notice": fs.risk_notice()}
        out["card_log_id"] = card_log.append("plan", symbol=sym, expiry=body.expiry, request=body.model_dump(mode="json"),
                                             chains=[chain], response=out, plan_inputs=x)  # #360  # fmt: skip
        return out

    @app.get("/api/fno/{symbol}/suggest")
    async def fno_suggest(symbol: str) -> dict[str, Any]:
        """Each non-overlay pre-registered recipe applied to today's chain, as a trade-plan card."""
        from finresearch.adapters.nse_fno import lot_size_for
        from finresearch.fincalc.dates import now_ist
        from finresearch.signals import fno as fs
        from finresearch.signals import tradeplan as tpl

        now = now_ist()
        sym, today = symbol.strip().upper(), now.date()
        async with fno() as f:
            expiries, _ = await f.contract_info(sym)
            expiries = fs.live_expiries(expiries, today)
            if not expiries:
                raise HTTPException(422, f"no live F&O expiries for {sym}")
            try:
                lots = await f.lot_sizes()
            except Exception:
                lots = {}

            seen: list[Any] = []  # the chains the cards were built on, for the card log (#360)

            async def get_chain(e: date) -> Any:
                try:
                    seen.append(await f.option_chain(sym, e))
                except Exception:
                    return None
                return seen[-1]

            async def lot_for(e: date) -> int | None:
                return lot_size_for(lots, sym, e)

            async def get_bidask(e: date) -> tuple[Any, str] | None:
                return await bidask_for(f, sym, e)

            x = await inputs(f, sym, expiries[0], today, now)
            cards = await tpl.suggest(sym, expiries, get_chain, lot_for, x, get_bidask)
        out = {"symbol": sym, "today": today.isoformat(), "cards": cards, "risk_notice": fs.risk_notice(),
               "note": "The library's pre-registered recipes (backtest.specs) applied to today's chain, each with its "
                       "own out-of-sample result. A recipe shown here is not a recommendation."}  # fmt: skip
        out["card_log_id"] = card_log.append("suggest", symbol=sym, expiry=None, request={"symbol": sym},
                                             chains=seen, response=out, plan_inputs=x)  # #360  # fmt: skip
        for i, c in enumerate(
            cards
        ):  # #383: a paper trade taken from a card names it (shortfall against its mids)
            c["card_log_id"], c["card_index"] = out["card_log_id"], i
        return out

    @app.get("/api/fno/{symbol}/context")
    async def fno_context(symbol: str, expiry: date | None = None) -> dict[str, Any]:
        """The market-context panel for the underlying: every indicator with what it measures and its evidence.
        Without `expiry` (#324): the nearest live expiry that is not today (a same-day expiry's chain is mostly
        decay), or today's when it is the only one; the response states which (`expiry`, `expiry_defaulted`)."""
        from finresearch.fincalc.dates import now_ist
        from finresearch.fnobook import events, sources
        from finresearch.signals import fno as fs
        from finresearch.signals import fno_context as ctx
        from finresearch.signals.rates import risk_free

        now = now_ist()
        sym, today = symbol.strip().upper(), now.date()
        if expiry is not None:
            try:
                fs.check_expiry(expiry, today)
            except fs.ExpiredContract as e:
                raise HTTPException(422, str(e)) from e
        defaulted = expiry is None
        async with fno() as f:
            exp: list[date] | None = None
            try:
                exp = fs.live_expiries((await f.contract_info(sym))[0], today)
            except Exception as e:
                if defaulted:
                    raise HTTPException(
                        422, f"no expiry given and NSE's expiry list for {sym} is unavailable"
                    ) from e
            if expiry is None:
                if not exp:
                    raise HTTPException(422, f"no live F&O expiries for {sym}")
                expiry = next((e for e in exp if e > today), exp[0])
            chain = await f.option_chain(sym, expiry)
            # NSE's ~90 daily closes only when the stored bhavcopy history is too short (no needless request)
            closes = (
                await fs.underlying_closes(f, sym, today) if ctx.needs_network_closes(sym, today) else None
            )
            exp = [expiry] if exp is None else exp
        try:
            strip = await events.strip(sym, today, sources.SOURCES, exp)
        except Exception:
            strip = None
        from finresearch.fincalc.dates import years_to_expiry

        rate = (await risk_free(years_to_expiry(expiry, now)))["rate"]  # tenor to the minute (#359)
        return {**ctx.build(chain, today=today, rate=rate, network_closes=closes, events=strip, now=now),
                "expiry": expiry.isoformat(), "expiry_defaulted": defaulted,
                "expiry_note": ("No expiry given: the nearest that is not today's." if defaulted else None)}  # fmt: skip
