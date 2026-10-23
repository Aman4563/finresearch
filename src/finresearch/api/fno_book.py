"""F&O positions, the paper book, exit rules, the margin approximation and the event strip (#308).

Read-only towards the broker: no route here (or anywhere) places, modifies or cancels an order. Paper trades and exit
rules are local rows; a paper trade can be closed but never deleted (every trade counts in the results)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from finresearch.db import session_scope

log = logging.getLogger(__name__)


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
    card_log_id: int | None = Field(
        None, ge=1, description="#383: the card_log row of the card it was taken from"
    )
    card_index: int = Field(0, ge=0, le=50, description="The card's index in that response (a plan card: 0)")


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


GROWW_MARGIN_SOURCE = "Groww margin calculator (POST /v1/margins/detail/orders, segment FNO, product NRML)"


async def _groww_margin(sym: str, expiry: date, spec: list[tuple[str, float, str, int, float | None]]) -> dict[str, Any]:  # fmt: skip
    """Groww's required margin for the basket, or why not (then the approximation is the figure). Never raises."""
    from finresearch.adapters import groww_market as gm
    from finresearch.portfolio.connectors.base import ConnectorError

    market = gm.MARKET
    if not market.token():
        return {"available": False, "reason": "Groww is not connected (or its session expired): the approximate "
                "estimate is the only figure"}  # fmt: skip
    try:
        inst = await market.instruments()
        if inst is None:
            raise ConnectorError("Groww's instruments file is not available")
        got = await market.basket_margin(gm.basket_orders(inst, sym, expiry, spec), "FNO")
    except ConnectorError as e:  # messages are redacted and safe to show
        why = str(e)
    except Exception as e:  # never let the calculator break the estimate
        why = type(e).__name__
    else:
        return {"available": True, "source": GROWW_MARGIN_SOURCE, "span": got["span_required"],
                "exposure": got["exposure_required"], "premium": got["option_buy_premium"],
                "charges": got["brokerage_and_charges"], "total": got["total_requirement"]}  # fmt: skip
    return {"available": False, "reason": f"Groww's margin calculator did not answer ({why}): the approximate "
            "estimate is the fallback"}  # fmt: skip


def _span_params(sym: str, day: date) -> dict[str, Any] | None:
    """The day's NSE SPAN scan ranges for `sym` (fnohist.span.params_for, #370), or None (the labelled assumptions).
    A database error never breaks the estimate."""
    try:
        from finresearch.fnohist import span

        with session_scope() as s:
            return span.params_for(s, sym, day)
    except Exception:
        log.warning("SPAN ranges not read; the assumed ranges are used", exc_info=True)
        return None


def _log_margin_check(sym: str, expiry: date, kind: str, spec: list[Any], est: Any, groww: dict[str, Any],
                      span_day: date | None) -> None:  # fmt: skip
    """One row per Groww answer: the estimate against Groww's figure, like for like (scan + exposure vs SPAN +
    exposure; premium and charges left out), for the P4.3 trigger (MF16, #370). Never breaks the answer."""
    try:
        from finresearch.db.models import MarginCheck

        if groww.get("span") is None or groww.get("exposure") is None:
            return  # an incomplete answer is not a comparison (unknown is never 0)
        g = float(groww["span"]) + float(groww["exposure"])
        with session_scope() as s:
            s.add(MarginCheck(symbol=sym, expiry=expiry, kind=kind, legs=[list(x) for x in spec],
                              approx_margin=round(est.scan_risk + est.exposure, 2), groww_margin=round(g, 2),
                              span_day=span_day))  # fmt: skip
    except Exception:
        log.warning("margin check not logged", exc_info=True)


def _check_card(s: Any, card_log_id: int, index: int, sym: str) -> None:
    """422 unless the card_log row is a plan or suggest response for `sym` holding a card at `index` (#383)."""
    from finresearch.db.models import CardLog
    from finresearch.fnobook.card_forecasts import _cards

    row = s.get(CardLog, card_log_id)
    if row is None or row.symbol.upper() != sym:
        raise HTTPException(422, f"card log {card_log_id} is not a {sym} card")
    if not 0 <= index < len(_cards(row.kind, row.response or {})):
        raise HTTPException(422, f"card log {card_log_id} ({row.kind}) has no card {index}")


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

    @app.get("/api/fno/review")
    def fno_review(month: str | None = None) -> dict[str, Any]:
        """The monthly post-trade review (#383): shortfall, adherence, exposure mix and the daily P&L explain of the
        paper and Groww F&O book (`month` = YYYY-MM, default this IST month). Reads stored rows only, writes nothing."""
        from finresearch.fnobook import review

        try:
            y, m = (int(x) for x in month.split("-")) if month else (today().year, today().month)
            if not 1 <= m <= 12 or not 2000 <= y <= 2100:
                raise ValueError
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM") from None
        with session_scope() as s:
            return review.month_view(s, y, m)

    @app.get("/api/fno/scenarios")
    async def fno_scenarios() -> dict[str, Any]:
        """The open F&O book's exposures, spot x vol x time grid, historical replays and reverse stress (#394).
        Analysis only; reads option chains, writes nothing."""
        from finresearch.fnobook.scenarios import view
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            capital = float(load_profile(s).fno_capital_inr)
        return await view(now(), sources=sources(), capital=capital)

    @app.get("/api/fno/paper")
    def fno_paper() -> dict[str, Any]:
        """Every paper trade and the book's results (closed trades, after costs), with the exit rules."""
        from finresearch.backtest import costs
        from finresearch.db.models import FnoExitRule
        from finresearch.fnobook.book import book_json
        from finresearch.fnobook.exits import rule_json

        with session_scope() as s:
            out = book_json(s, today())
            out["rules"] = [rule_json(r) for r in s.scalars(select(FnoExitRule).order_by(FnoExitRule.id))]
            # #382 (d): the measured median half-spread once enough quotes are recorded, else the assumption
            out["default_slippage_pct"], out["default_slippage_source"] = costs.paper_default(s, today())
        return out

    @app.post("/api/fno/paper", status_code=201)
    async def fno_paper_open(body: PaperIn) -> dict[str, Any]:
        """Paper-trade a strategy from the builder: fills at the live LTP (Groww, else the chain's last price) moved
        by the slippage, with entry charges. Only in market hours. Nothing is sent to a broker."""
        from finresearch.backtest import costs
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
        legs_in = [LegIn(x.right, float(x.strike), x.side, x.lots) for x in body.legs]
        with session_scope() as s:
            if body.card_log_id is not None:
                _check_card(s, body.card_log_id, body.card_index, sym)
            slip = (
                float(body.slippage_pct)
                if body.slippage_pct is not None
                else costs.paper_default(s, today())[0]
            )
            brokerage = float(load_profile(s).fno_brokerage_per_order_inr)
            try:
                st = book.record(s, symbol=sym, expiry=body.expiry, lot=lot, legs_in=legs_in, quotes=quotes,
                                 slippage_pct=slip, brokerage=brokerage, now=t, label=body.label,
                                 card=None if body.card_log_id is None else (body.card_log_id, body.card_index),
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
        """The basket's margin from Groww's margin calculator (`groww_calculator`, #316: the one calculator POST the
        read-only client may send) when Groww is connected and answers, and always the APPROXIMATE SPAN-style
        estimate (fincalc.margin_approx), labelled as such: the fallback, and the only figure without Groww."""
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
        t = service._t(body.expiry, now())  # #359: minutes to 15:30 IST on the expiry day; None once expired
        if t is None:
            raise HTTPException(
                422, f"the {body.expiry:%d-%b-%Y} contract has expired (15:30 IST): no margin estimate"
            )
        rinfo = await service._rate(t, None)  # #366/#385: FBIL's par yield at the expiry, labelled
        rate = rinfo["rate"]
        legs, spec = [], []
        for x in body.legs:
            k = float(x.strike)
            qty = x.lots * lot * (1 if x.side == "buy" else -1)
            if x.right == "future":
                legs.append(ma.MarginLeg("future", spot, qty, spot, None))
                spec.append(("future", k, x.side, abs(qty), None))
                continue
            price = service._chain_price(chain, x.right, k)
            spec.append((x.right, k, x.side, abs(qty), price))
            iv = positions.chain_iv(chain, x.right, k) or positions.implied(x.right, price, spot, k, t, rate)
            legs.append(ma.MarginLeg(x.right, k, qty, price or 0.0, iv))
        kind = "index" if events.is_index(sym) else "stock"
        span = _span_params(sym, today())
        try:
            est = ma.estimate(legs, spot, t, rate, kind, params=span and span["params"],
                              sources=span and span["sources"])  # fmt: skip
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        groww = await _groww_margin(sym, body.expiry, spec)
        if groww["available"]:
            _log_margin_check(sym, body.expiry, kind, spec, est, groww, span and span["day"])
        return {"symbol": sym, "expiry": body.expiry.isoformat(), "spot": spot, "lot_size": lot,
                **ma.to_json(est, kind), "groww_calculator": groww,
                "margin_source": "groww" if groww["available"] else "approximate", "rate": rinfo}  # fmt: skip

    @app.get("/api/fno/{symbol}/event-vol")
    def fno_event_vol(symbol: str) -> dict[str, Any]:
        """#391: implied event moves from the stored end-of-day term structure and the realised history by event
        type (point in time). Reads stored data only; informational, the card does not use it."""
        from finresearch.signals import event_vol

        with session_scope() as s:
            return event_vol.build(s, symbol, now())

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
