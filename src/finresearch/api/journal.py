"""/api/journal/*: the decision journal for every buy and sell, the pre-trade checklist, and /api/portfolio/behaviour
(the behaviour report). Feature #5, behavioural guardrails.

Privacy: the investor's own trades and reasons, local database only; nothing here calls an LLM and no agent or MCP
tool reads it. Unsafe routes sit behind the app's CSRF/Origin guard. The IPO decisions (/api/decisions) are unchanged.

Test seams: `app.state.journal_today` (a date) and `app.state.journal_signal` (an async (asset, code) -> Signal used
by the checklist for an instrument the daily pass has no signal for; default: the app's own signal provider).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from finresearch.db import session_scope

Status = Literal["draft", "planned", "active", "reviewed", "cancelled"]
Verdict = Literal["right", "wrong", "mixed", "too_early"]


def _strip(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


class NoteIn(BaseModel):
    """A new entry: a manual note for a trade, or a planned trade (status "planned", usually with its checklist)."""

    side: Literal["buy", "sell"]
    status: Literal["planned", "active", "draft"] = "active"
    holding_id: int | None = None
    asset_type: Literal["stock", "mf", "other"] = "stock"
    name: str | None = Field(None, max_length=300)
    instrument: str | None = Field(None, max_length=60)
    trade_day: date | None = None
    quantity: Decimal | None = Field(None, gt=0)
    price: Decimal | None = Field(None, gt=0)
    thesis: str | None = Field(None, max_length=4000)
    expected_holding_days: int | None = Field(None, ge=1, le=36500)
    invalidation: str | None = Field(None, max_length=2000)
    confidence_pct: int | None = Field(None, ge=0, le=100)
    review_on: date | None = None
    checklist: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check(self) -> NoteIn:
        if self.holding_id is None and not (self.name or "").strip():
            raise ValueError("choose a holding or enter the instrument's name")
        return self


class NoteUpdate(BaseModel):
    status: Status | None = None
    thesis: str | None = Field(None, max_length=4000)
    expected_holding_days: int | None = Field(None, ge=1, le=36500)
    invalidation: str | None = Field(None, max_length=2000)
    confidence_pct: int | None = Field(None, ge=0, le=100)
    review_on: date | None = None
    trade_day: date | None = None
    quantity: Decimal | None = Field(None, gt=0)
    price: Decimal | None = Field(None, gt=0)
    outcome_verdict: Verdict | None = None
    outcome_notes: str | None = Field(None, max_length=4000)


class PlanIn(BaseModel):
    side: Literal["buy", "sell"]
    quantity: Decimal = Field(gt=0)
    price: Decimal = Field(gt=0)
    day: date | None = None
    holding_id: int | None = None
    asset_type: Literal["stock", "mf", "other"] = "stock"
    name: str = Field("", max_length=300)
    nse_symbol: str | None = Field(None, max_length=30)
    bse_code: str | None = Field(None, max_length=20)
    scheme_code: str | None = Field(None, max_length=20)
    isin: str | None = Field(None, max_length=12)
    charges: Decimal = Field(Decimal(0), ge=0)

    @model_validator(mode="after")
    def _check(self) -> PlanIn:
        if self.holding_id is None and not (self.name or self.nse_symbol or self.scheme_code or self.isin):
            raise ValueError("choose a holding or name the instrument")
        if self.side == "sell" and self.holding_id is None:
            raise ValueError("a sale needs the holding it sells from")
        return self


def add_journal_routes(app: FastAPI, *, clock: Callable[[], datetime] | None = None) -> None:
    from finresearch.fincalc.dates import IST, today_ist

    def today() -> date:
        override = getattr(app.state, "journal_today", None)
        if override is not None:
            return override
        return clock().astimezone(IST).date() if clock else today_ist()

    def _note(s, note_id: int):
        from finresearch.db.models import TradeNote

        n = s.get(TradeNote, note_id)
        if n is None:
            raise HTTPException(404, f"unknown journal entry {note_id}")
        return n

    def _live(s) -> tuple[set[int], set[int]]:
        from sqlalchemy import select

        from finresearch.db.models import PortfolioHolding, PortfolioTxn

        return set(s.scalars(select(PortfolioTxn.id))), set(s.scalars(select(PortfolioHolding.id)))

    # ------------------------------------------------------------------ entries
    @app.get("/api/journal/notes")
    def notes(status: Status | None = None, holding_id: int | None = None) -> dict[str, Any]:
        """Every entry, newest trade first, after drafting entries for trades imported since the last look."""
        from sqlalchemy import select

        from finresearch.db.models import TradeNote
        from finresearch.portfolio.journal import PRIVACY, due, note_json, sync_drafts

        with session_scope() as s:
            synced = sync_drafts(s, today())
            q = select(TradeNote)
            if status:
                q = q.where(TradeNote.status == status)
            if holding_id is not None:
                q = q.where(TradeNote.holding_id == holding_id)
            rows = s.scalars(q.order_by(TradeNote.trade_day.desc().nulls_first(), TradeNote.id.desc())).all()
            live = _live(s)
            due_ids = [n.id for n in due(s, today())]
            return {"notes": [note_json(n, live) for n in rows], "due": due_ids, "synced": synced,
                    "today": today().isoformat(), "privacy": PRIVACY}  # fmt: skip

    @app.get("/api/journal/summary")
    def summary() -> dict[str, Any]:
        from sqlalchemy import func, select

        from finresearch.db.models import Decision, TradeNote
        from finresearch.portfolio.journal import due, sync_drafts

        with session_scope() as s:
            sync_drafts(s, today())
            by = dict(s.execute(select(TradeNote.status, func.count()).group_by(TradeNote.status)).all())
            return {"by_status": {k: int(by.get(k, 0)) for k in ("draft", "planned", "active", "reviewed", "cancelled")},
                    "due": len(due(s, today())), "ipo_decisions": s.scalar(select(func.count()).select_from(Decision)) or 0}  # fmt: skip

    @app.post("/api/journal/notes", status_code=201)
    def add_note(body: NoteIn) -> dict[str, Any]:
        from finresearch.db.models import PortfolioHolding, TradeNote
        from finresearch.portfolio.journal import instrument_of, note_json

        with session_scope() as s:
            h = None
            if body.holding_id is not None:
                h = s.get(PortfolioHolding, body.holding_id)
                if h is None:
                    raise HTTPException(404, f"unknown holding {body.holding_id}")
            status = body.status if body.status != "active" or _strip(body.thesis) else "draft"
            n = TradeNote(status=status, source="pretrade" if body.checklist else "manual", side=body.side,
                          asset_type=h.asset_type if h else body.asset_type,
                          instrument=(instrument_of(h) if h else body.instrument), name=(h.name if h else body.name or "")[:300],
                          holding_id=h.id if h else None, txn_ids=[], trade_day=body.trade_day or today(),
                          quantity=body.quantity, price=body.price, thesis=_strip(body.thesis),
                          expected_holding_days=body.expected_holding_days, invalidation=_strip(body.invalidation),
                          confidence_pct=body.confidence_pct, review_on=body.review_on, checklist=body.checklist or {},
                          outcome={})  # fmt: skip
            s.add(n)
            s.flush()
            return note_json(n)

    @app.patch("/api/journal/notes/{note_id}")
    def patch_note(note_id: int, body: NoteUpdate) -> dict[str, Any]:
        """Edit an entry. Writing a thesis turns a draft into an active entry; recording a verdict reviews it (with the
        figures since the trade, from stored data). A new review date re-arms the reminder."""
        from finresearch.portfolio.journal import note_json, outcome_figures

        fields = body.model_dump(exclude_unset=True)
        with session_scope() as s:
            n = _note(s, note_id)
            for k in ("thesis", "invalidation", "outcome_notes"):
                if k in fields:
                    fields[k] = _strip(fields[k])
            for k, v in fields.items():
                if k != "status":
                    setattr(n, k, v)
            if fields.get("status"):
                n.status = fields["status"]
            elif n.status == "draft" and n.thesis:
                n.status = "active"
            if fields.get("outcome_verdict"):
                n.status = "reviewed"
                n.reviewed_at = datetime.now(UTC)
                n.outcome = outcome_figures(s, n, today())
            if "review_on" in fields:
                n.review_alerted_on = None
            n.updated_at = datetime.now(UTC)
            return note_json(n, _live(s))

    @app.delete("/api/journal/notes/{note_id}")
    def delete_note(note_id: int) -> dict[str, Any]:
        with session_scope() as s:
            s.delete(_note(s, note_id))
            return {"deleted": note_id}

    # ------------------------------------------------------------------ pre-trade checklist
    @app.post("/api/journal/pretrade")
    async def pretrade(body: PlanIn) -> dict[str, Any]:
        """The checklist for a planned trade. Computes and stores nothing; record it with POST /api/journal/notes."""
        from finresearch.portfolio.pretrade import Plan, checklist

        live = getattr(app.state, "journal_signal", None)
        if live is None:
            from finresearch.monitor.portfolio_daily import _live_signal as live
        p = Plan(side=body.side, quantity=body.quantity, price=body.price, day=body.day or today(),
                 holding_id=body.holding_id, asset_type=body.asset_type, name=body.name,
                 nse_symbol=body.nse_symbol, bse_code=body.bse_code, scheme_code=body.scheme_code,
                 isin=body.isin, charges=body.charges)  # fmt: skip
        with session_scope() as s:
            try:
                out = await checklist(s, p, live_signal=live)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            s.rollback()  # read-only
            return out

    # ------------------------------------------------------------------ behaviour report
    @app.get("/api/portfolio/behaviour")
    async def behaviour(
        fy: int | None = Query(
            None, ge=2000, le=2100, description="financial year by its end year: 2027 = FY 2026-27"
        ),
        start: Annotated[date | None, Query(alias="from")] = None,
        end: Annotated[date | None, Query(alias="to")] = None,
    ) -> dict[str, Any]:
        """Turnover, holding periods, the disposition effect (Odean 1998 PGR/PLR), trading frequency vs returns and
        the cost of churn, for a financial year (default: the current one) or a date range."""
        from finresearch.fincalc.dates import fiscal_year
        from finresearch.portfolio.behaviour import build_report
        from finresearch.portfolio.report import load
        from finresearch.suggest.advisor import load_profile

        t = today()
        if start or end:
            if not (start and end) or end < start:
                raise HTTPException(422, "give both from and to, with from on or before to")
            lo, hi = start, min(end, t)
        else:
            y = fy if fy is not None else fiscal_year(t)
            lo, hi = date(y - 1, 4, 1), min(date(y, 3, 31), t)
            if lo > t:
                raise HTTPException(422, "that financial year has not started")
        hist, hist_note = None, None
        get_hist = getattr(app.state, "portfolio_history", None)
        if get_hist is not None:
            try:
                hist, _, _ = await get_hist()
            except Exception as e:  # the report still has its price-free sections; say why prices are missing
                hist_note = f"the value history could not be built ({type(e).__name__})"
        with session_scope() as s:
            data = load(s)
            slab = Decimal(str(load_profile(s).tax_slab_pct)) / 100
            out = build_report(data, hist, lo, hi, t, slab)
        if hist_note:
            out["warnings"].insert(0, hist_note)
        return out
