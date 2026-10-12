"""F&O positions, the paper book, exit rules, the margin approximation and the event strip (#308).

Read-only towards the broker: no route here (or anywhere) places, modifies or cancels an order. Paper trades and exit
rules are local rows; a paper trade can be closed but never deleted (every trade counts in the results)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from finresearch.db import session_scope


class PaperLegIn(BaseModel):
    right: Literal["call", "put", "future"]
    strike: Decimal = Field(
        gt=0, description="Strike (a future: any positive number; it fills at the live price)"
    )
    side: Literal["buy", "sell"]
    lots: int = Field(1, ge=1, le=100)


class PaperIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    expiry: date
    legs: list[PaperLegIn] = Field(min_length=1, max_length=8)
    slippage_pct: Decimal | None = Field(
        None, ge=0, le=20, description="Default: fnobook.book.DEFAULT_SLIPPAGE_PCT"
    )
    label: str = Field("", max_length=120)


class ExitRuleIn(BaseModel):
    paper_id: int | None = None
    position_key: str | None = Field(
        None, max_length=60, description='A Groww position group: "NIFTY:2026-10-27"'
    )
    kind: Literal["profit", "stop", "dte", "delta", "event"]
    threshold: Decimal = Field(ge=0, le=10000)

    @model_validator(mode="after")
    def _one_target(self) -> ExitRuleIn:
        if (self.paper_id is None) == (self.position_key is None):
            raise ValueError("give exactly one of paper_id or position_key")
        if self.kind == "delta" and not 0 < self.threshold <= 1:
            raise ValueError("a delta threshold is between 0 and 1 (e.g. 0.30)")
        return self


class MarginIn(BaseModel):
    symbol: str = Field(min_length=1, max_length=30)
    expiry: date
    legs: list[PaperLegIn] = Field(min_length=1, max_length=8)


def add_fno_book_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    def today() -> date:
        from finresearch.fincalc.dates import to_ist

        return to_ist(now()).date()

    def sources() -> Any:
        from finresearch.fnobook import sources as src

        return src.SOURCES

    @app.get("/api/fno/positions")
    async def fno_positions() -> dict[str, Any]:
        """The Groww F&O positions read at the last sync or in-session poll, priced with Groww's LTP now."""
        from finresearch.fnobook.service import positions_view

        return await positions_view(now())

    @app.get("/api/fno/paper")
    def fno_paper() -> dict[str, Any]:
        """Every paper trade and the book's results (closed trades, after costs), with the exit rules."""
        from finresearch.db.models import FnoExitRule
        from finresearch.fnobook.book import DEFAULT_SLIPPAGE_PCT, book_json
        from finresearch.fnobook.exits import rule_json

        with session_scope() as s:
            out = book_json(s, today())
            out["rules"] = [rule_json(r) for r in s.scalars(select(FnoExitRule).order_by(FnoExitRule.id))]
        out["default_slippage_pct"] = DEFAULT_SLIPPAGE_PCT
        return out

    @app.post("/api/fno/paper", status_code=201)
    async def fno_paper_open(body: PaperIn) -> dict[str, Any]:
        """Paper-trade a strategy from the builder: fills at the live LTP (Groww, else the chain's last price) moved
        by the slippage, with entry charges. Only in market hours. Nothing is sent to a broker."""
        from finresearch.fnobook import book, service
        from finresearch.signals.fno import ExpiredContract, LegIn, check_expiry
        from finresearch.suggest.advisor import load_profile

        sym = body.symbol.strip().upper()
        t = now()
        try:
            check_expiry(body.expiry, today())
        except ExpiredContract as e:
            raise HTTPException(422, str(e)) from e
        try:
            lot = await sources().lot_size(sym, body.expiry)
        except Exception:
            lot = None
        if not lot:
            raise HTTPException(422, f"no NSE lot size for {sym} {body.expiry:%b-%y}: not recorded")
        try:
            quotes, chain = await service.quotes(
                sym, body.expiry, [(x.right, float(x.strike)) for x in body.legs], t
            )
        except service.OutOfSession as e:
            raise HTTPException(409, str(e)) from e
        slip = float(body.slippage_pct) if body.slippage_pct is not None else book.DEFAULT_SLIPPAGE_PCT
        legs_in = [LegIn(x.right, float(x.strike), x.side, x.lots) for x in body.legs]
        with session_scope() as s:
            brokerage = float(load_profile(s).fno_brokerage_per_order_inr)
            try:
                st = book.record(s, symbol=sym, expiry=body.expiry, lot=lot, legs_in=legs_in, quotes=quotes,
                                 slippage_pct=slip, brokerage=brokerage, now=t, label=body.label,
                                 spot=float(chain.underlying) if chain is not None and chain.underlying else None)  # fmt: skip
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return book.strategy_json(st, today())

    @app.post("/api/fno/paper/{paper_id}/close")
    async def fno_paper_close(paper_id: int) -> dict[str, Any]:
        """Square off an open paper trade at the live prices, with square-off charges. Only in market hours."""
        from finresearch.db.models import PaperStrategy
        from finresearch.fnobook import book, service

        with session_scope() as s:
            st = s.get(PaperStrategy, paper_id)
            if st is None:
                raise HTTPException(404, f"no paper trade {paper_id}")
            if st.status != book.OPEN:
                raise HTTPException(409, f"paper trade {paper_id} is {st.status}")
            sym, exp, legs = st.symbol, st.expiry, [(x.right, float(x.strike)) for x in st.legs]
        try:
            quotes, _ = await service.quotes(sym, exp, legs, now())
        except service.OutOfSession as e:
            raise HTTPException(409, str(e)) from e
        with session_scope() as s:
            st = s.get(PaperStrategy, paper_id, with_for_update=True)
            try:
                book.square_off(st, [q.price for q in quotes], now())
            except ValueError as e:
                raise HTTPException(409, str(e)) from e
            return book.strategy_json(st, today())

    @app.post("/api/fno/exit-rules", status_code=201)
    def fno_exit_rule_add(body: ExitRuleIn) -> dict[str, Any]:
        """Arm an exit rule on a paper trade or a Groww position group. It raises one alert when it triggers."""
        from finresearch.db.models import FnoExitRule, PaperStrategy
        from finresearch.fnobook.exits import rule_json

        with session_scope() as s:
            if body.paper_id is not None:
                st = s.get(PaperStrategy, body.paper_id)
                if st is None or st.status != "open":
                    raise HTTPException(422, f"paper trade {body.paper_id} is not open")
            r = FnoExitRule(paper_id=body.paper_id, position_key=body.position_key, kind=body.kind,
                            threshold=body.threshold, status="armed", created_at=now())  # fmt: skip
            s.add(r)
            s.flush()
            return rule_json(r)

    @app.post("/api/fno/exit-rules/{rule_id}/off")
    def fno_exit_rule_off(rule_id: int) -> dict[str, Any]:
        """Turn an armed rule off (kept, with its history; a fired rule stays fired)."""
        from finresearch.db.models import FnoExitRule
        from finresearch.fnobook.exits import rule_json

        with session_scope() as s:
            r = s.get(FnoExitRule, rule_id)
            if r is None:
                raise HTTPException(404, f"no exit rule {rule_id}")
            if r.status == "armed":
                r.status = "off"
            return rule_json(r)

    @app.post("/api/fno/margin")
    async def fno_margin(body: MarginIn) -> dict[str, Any]:
        """An APPROXIMATE SPAN-style margin for the basket (fincalc.margin_approx), labelled as such: Groww's basket
        margin calculator is a POST, which the read-only Groww client never sends."""
        from finresearch.fincalc import margin_approx as ma
        from finresearch.fnobook import events, positions, service

        sym = body.symbol.strip().upper()
        chain = await service._chain(sources(), sym, body.expiry)
        try:
            lot = await sources().lot_size(sym, body.expiry)
        except Exception:
            lot = None
        if chain is None or not chain.underlying or not lot:
            raise HTTPException(
                422, f"no option chain or lot size for {sym} {body.expiry}: no margin estimate"
            )
        spot = float(chain.underlying)
        t = service._t(body.expiry, today())
        legs = []
        for x in body.legs:
            k = float(x.strike)
            qty = x.lots * lot * (1 if x.side == "buy" else -1)
            if x.right == "future":
                legs.append(ma.MarginLeg("future", spot, qty, spot, None))
                continue
            price = service._chain_price(chain, x.right, k)
            iv = positions.chain_iv(chain, x.right, k) or positions.implied(
                x.right, price, spot, k, t, service.RATE
            )
            legs.append(ma.MarginLeg(x.right, k, qty, price or 0.0, iv))
        kind = "index" if events.is_index(sym) else "stock"
        try:
            est = ma.estimate(legs, spot, t, service.RATE, kind)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        return {"symbol": sym, "expiry": body.expiry.isoformat(), "spot": spot, "lot_size": lot,
                **ma.to_json(est, kind), "groww_calculator": {"available": False, "reason": "POST "
                "/v1/margins/detail/orders is not a read-only GET; the app never sends it"}}  # fmt: skip

    @app.get("/api/fno/{symbol}/events")
    async def fno_events(symbol: str) -> dict[str, Any]:
        """The event strip: the next results date (stocks), RBI policy dates (when a source exists) and expiries."""
        from finresearch.fnobook import events
        from finresearch.signals.fno import live_expiries

        sym = symbol.strip().upper()
        try:
            exp = live_expiries(await sources().expiries(sym), today())
        except Exception:
            exp = []
        return await events.strip(sym, today(), sources(), exp)
