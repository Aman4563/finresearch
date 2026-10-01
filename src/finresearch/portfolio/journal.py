"""The decision journal for every buy and sell (feature #5, behavioural guardrails): drafts for newly imported trades,
planned trades matched to the trades that follow, review reminders and the outcome review.

Personal data: the investor's own reasons live only in the local `trade_note` table. Nothing here calls an LLM, and
no agent, advisor prompt or MCP tool imports this module. Review reminders are alerts, which may be forwarded to the
investor's phone (ntfy / Telegram): their text names the instrument only, never the thesis.

Drafts. Each pass looks at transactions added since the last pass (a cursor over `portfolio_txn.id` kept in
`portfolio_setting` "journal:cursor"), so no import path needs to call it. One draft per holding, side and day (a
day of partial fills is one decision). Left out, because they are not decisions taken that day: corporate actions
(`source` nse_actions), opening balances, dividend reinvestments, SIP instalments (CAS `PURCHASE_SIP`) and trades
older than DRAFT_WINDOW_DAYS when they are imported (the reasons are long forgotten; asking would only nag).

Planned trades. A note written from the pre-trade checklist (`planned`) becomes the entry of the trade that follows:
same holding (or instrument), same side, traded within MATCH_DAYS of the planned day.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, PortfolioHolding, PortfolioSetting, PortfolioTxn, TradeNote

CURSOR_KEY = "journal:cursor"
DRAFT_WINDOW_DAYS = 30
MATCH_DAYS = 7
STATUSES = ("draft", "planned", "active", "reviewed", "cancelled")
VERDICTS = ("right", "wrong", "mixed", "too_early")
OPEN = ("planned", "active")  # notes whose review date raises a reminder
ALERT_KIND = "journal_review"
PRIVACY = "Your journal stays in the local database; it is never sent to an LLM or put in a forwarded alert."


def _dec(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def _f(x: Decimal | None) -> float | None:
    return None if x is None else float(x)


def instrument_of(h: PortfolioHolding) -> str:
    from finresearch.portfolio.behaviour import group_key

    return group_key(h)


def keys_of(h: PortfolioHolding) -> set[str]:
    """Every key a planned note may name this holding by (a plan made from a symbol matches a trade imported with an
    ISIN)."""
    out = {h.ikey, instrument_of(h)}
    if h.isin:
        out.add(f"ISIN:{h.isin.upper()}")
    if h.nse_symbol:
        out.add(f"NSE:{h.nse_symbol.upper()}")
    if h.bse_code:
        out.add(f"BSE:{h.bse_code}")
    if h.scheme_code:
        out.add(f"MF:{h.scheme_code}")
    return out


def is_decision(t: PortfolioTxn) -> bool:
    """A buy or sell the investor chose that day (not an SIP instalment, a reinvested dividend or a corporate action)."""
    m = t.meta or {}
    return (t.kind in ("buy", "sell") and t.source != "nse_actions" and not m.get("reinvest")
            and not m.get("statement_opening") and m.get("cas_type_txn") != "PURCHASE_SIP")  # fmt: skip


def _cursor(s: Session) -> int | None:
    row = s.get(PortfolioSetting, CURSOR_KEY)
    return int((row.value or {}).get("txn_id", 0)) if row else None


def _set_cursor(s: Session, v: int) -> None:
    row = s.get(PortfolioSetting, CURSOR_KEY)
    if row is None:
        s.add(PortfolioSetting(key=CURSOR_KEY, value={"txn_id": v}))
    else:
        row.value = {"txn_id": v}


def _fill(s: Session, n: TradeNote, txns: list[PortfolioTxn]) -> None:
    """Attach `txns` to `n` and set its quantity and average gross price over every transaction it now covers."""
    ids = sorted({*[int(i) for i in n.txn_ids or []], *[t.id for t in txns]})
    n.txn_ids = ids
    rows = list(s.scalars(select(PortfolioTxn).where(PortfolioTxn.id.in_(ids)))) if ids else []
    rows = list({t.id: t for t in [*rows, *txns]}.values())
    q = sum((t.quantity or Decimal(0) for t in rows), Decimal(0))
    gross = [abs(t.amount) if t.amount is not None else (t.quantity * t.price if t.quantity and t.price else None)
             for t in rows]  # fmt: skip
    if q > 0:
        n.quantity = q
        n.price = (
            sum((g for g in gross if g is not None), Decimal(0)) / q
            if all(g is not None for g in gross)
            else None
        )


def sync_drafts(s: Session, today: date) -> dict[str, int]:
    """Create drafts for new decision trades and attach trades to planned notes. Idempotent."""
    cursor = _cursor(s) or 0
    top = s.scalar(select(func.max(PortfolioTxn.id))) or 0
    if top <= cursor:
        if _cursor(s) is None:
            _set_cursor(s, top)
        return {"drafts": 0, "matched": 0}
    rows = s.scalars(select(PortfolioTxn).where(PortfolioTxn.id > cursor).order_by(PortfolioTxn.id)).all()
    groups: dict[tuple[int, date, str], list[PortfolioTxn]] = defaultdict(list)
    for t in rows:
        if is_decision(t) and t.day >= today - timedelta(days=DRAFT_WINDOW_DAYS):
            groups[(t.holding_id, t.day, t.kind)].append(t)
    covered = {int(i) for ids in s.scalars(select(TradeNote.txn_ids)) for i in (ids or [])}
    drafts = matched = 0
    for (hid, day, side), txns in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2])):
        txns = [t for t in txns if t.id not in covered]
        if not txns:
            continue
        h = s.get(PortfolioHolding, hid)
        if h is None:
            continue
        inst = instrument_of(h)
        keys = keys_of(h)
        notes = s.scalars(select(TradeNote).where(TradeNote.side == side, TradeNote.status != "cancelled")
                          .order_by(TradeNote.id)).all()  # fmt: skip
        # a draft or entry already made for this holding, side and day (partial fills imported later)
        same = next((n for n in notes if n.holding_id == hid and n.trade_day == day and n.txn_ids), None)
        plan = next((n for n in notes if n.status == "planned" and not n.txn_ids
                     and (n.holding_id == hid or (n.instrument and n.instrument in keys))
                     and n.trade_day is not None and abs((n.trade_day - day).days) <= MATCH_DAYS), None)  # fmt: skip
        if same is not None:
            _fill(s, same, txns)
        elif plan is not None:
            plan.holding_id, plan.instrument, plan.trade_day = hid, inst, day
            plan.quantity = None
            _fill(s, plan, txns)
            plan.status = "active" if plan.thesis else "draft"
            plan.updated_at = func.now()
            matched += 1
        else:
            n = TradeNote(status="draft", source="auto", side=side, asset_type=h.asset_type, instrument=inst,
                          name=h.name[:300], holding_id=hid, trade_day=day, txn_ids=[], checklist={}, outcome={})  # fmt: skip
            s.add(n)
            _fill(s, n, txns)
            drafts += 1
        covered.update(t.id for t in txns)
    _set_cursor(s, top)
    s.flush()
    return {"drafts": drafts, "matched": matched}


# --------------------------------------------------------------------------- review reminders
def due(s: Session, today: date) -> list[TradeNote]:
    return list(s.scalars(select(TradeNote).where(TradeNote.status.in_(OPEN), TradeNote.review_on.is_not(None),
                                                  TradeNote.review_on <= today).order_by(TradeNote.review_on)))  # fmt: skip


def review_step(s: Session, now: datetime) -> dict[str, int]:
    """Raise one reminder per note when its review date (IST) arrives. The message names the instrument only."""
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    out = sync_drafts(s, today)
    n = 0
    for note in due(s, today):
        if note.review_alerted_on == note.review_on:
            continue
        verb = "bought" if note.side == "buy" else "sold"
        when = f" on {note.trade_day:%d %b %Y}" if note.trade_day else ""
        s.add(Alert(kind=ALERT_KIND, level="warn",
                    message=f"Journal review due: {note.name} ({verb}{when}). Did it go as you expected?",
                    data={"note_id": note.id, "path": "/journal", "review_on": note.review_on.isoformat()}))  # fmt: skip
        note.review_alerted_on = note.review_on
        n += 1
    s.flush()
    return {**out, "reminders": n}


# --------------------------------------------------------------------------- outcome figures
def outcome_figures(s: Session, note: TradeNote, today: date) -> dict[str, Any]:
    """What happened since the trade, from stored data only: the holding's latest valued price (the monitor's daily
    pass) against the entry price. For a sale the move since selling: positive = the price rose after you sold."""
    from finresearch.portfolio import cache

    out: dict[str, Any] = {"as_of": today.isoformat()}
    if note.trade_day:
        out["days_since_trade"] = (today - note.trade_day).days
        if note.expected_holding_days:
            out["expected_holding_days"] = note.expected_holding_days
    val = cache.read(s, cache.VALUATION)
    h = (val.get("holdings") or {}).get(str(note.holding_id)) if note.holding_id else None
    px = _dec(h.get("price")) if h else None
    if px is None or note.price is None or note.price <= 0:
        out["return_pct"] = None
        out["why"] = ("no current price: the holding was sold or the daily valuation has not run" if px is None
                      else "the entry price is unknown")  # fmt: skip
        return out
    r = (px / note.price - 1) * 100
    out |= {"price_now": float(px), "price_as_of": h.get("price_as_of"), "entry_price": float(note.price),
            ("return_pct" if note.side == "buy" else "move_since_sale_pct"): round(float(r), 2)}  # fmt: skip
    if note.side == "sell":
        out["return_pct"] = None
    return out


# --------------------------------------------------------------------------- JSON
def note_json(n: TradeNote, live: tuple[set[int], set[int]] | None = None) -> dict[str, Any]:
    """`live`: (transaction ids, holding ids) that still exist; links to deleted ones read as absent."""
    ids = [int(i) for i in n.txn_ids or []]
    hid = n.holding_id
    if live is not None:
        ids = [i for i in ids if i in live[0]]
        hid = hid if hid in live[1] else None
    return {"id": n.id, "status": n.status, "source": n.source, "side": n.side, "asset_type": n.asset_type,
            "instrument": n.instrument, "name": n.name, "holding_id": hid, "txn_ids": ids,
            "trade_day": n.trade_day.isoformat() if n.trade_day else None, "quantity": _f(n.quantity),
            "price": _f(n.price), "thesis": n.thesis, "expected_holding_days": n.expected_holding_days,
            "invalidation": n.invalidation, "confidence_pct": n.confidence_pct,
            "review_on": n.review_on.isoformat() if n.review_on else None, "checklist": n.checklist or {},
            "outcome_verdict": n.outcome_verdict, "outcome_notes": n.outcome_notes, "outcome": n.outcome or {},
            "reviewed_at": n.reviewed_at.isoformat() if n.reviewed_at else None,
            "created_at": n.created_at.isoformat() if n.created_at else None,
            "updated_at": n.updated_at.isoformat() if n.updated_at else None}  # fmt: skip
