"""The override log (#376 (d), evals/experiments/card_ledger/ADDENDUM.md B1.2): the decisions the owner records on
logged cards (POST /api/fno/overrides), append-only (db.models.CARD_OVERRIDE_APPEND_ONLY).

Never inferred: a card with no recorded decision has none (never "skipped"), and nothing here reads the paper book or
broker positions. An override is trading a WAIT, AVOID or NO PROVEN EDGE card, or skipping an ENTER card; a card
whose verdict class is unknown is counted and left out. Personal: never imported by a prompt-building or agent-tool
package (tests/test_privacy_llm.py)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import CardLog, CardOverride

DECISIONS = ("traded", "skipped")
CLASSES = ("ENTER", "NO PROVEN EDGE", "WAIT", "AVOID")
NOTE_MAX = 500


def verdict_class(action: str | None, qualifier: str | None) -> str | None:
    """ENTER, NO PROVEN EDGE (PAPER, or an ENTER qualified "no proven edge"), WAIT or AVOID; None when unknown."""
    a = (action or "").upper()
    if a in ("PAPER", "NO PROVEN EDGE") or (a == "ENTER" and "no proven edge" in (qualifier or "").lower()):
        return "NO PROVEN EDGE"
    return a if a in ("ENTER", "WAIT", "AVOID") else None


def is_override(verdict: str | None, decision: str) -> bool | None:
    if verdict is None:
        return None
    return decision == "skipped" if verdict == "ENTER" else decision == "traded"


def _json(r: CardOverride) -> dict[str, Any]:
    return {"id": r.id, "created_at": r.created_at.isoformat() if r.created_at else None,
            "card_log_id": r.card_log_id, "card_index": r.card_index, "decision": r.decision, "verdict": r.verdict,
            "override": is_override(r.verdict, r.decision), "note": r.note}  # fmt: skip


def record(
    s: Session, *, card_log_id: int, card_index: int, decision: str, note: str | None = None
) -> dict[str, Any]:
    """Append one decision; ValueError (422) for an unknown card or decision."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    cards = s.scalar(select(CardLog.cards).where(CardLog.id == card_log_id))
    if cards is None or not 0 <= card_index < len(cards):
        raise ValueError(f"no logged card {card_log_id}#{card_index}")
    c = cards[card_index] or {}
    note = (note or "").strip()[:NOTE_MAX] or None
    r = CardOverride(card_log_id=card_log_id, card_index=card_index, decision=decision,
                     verdict=verdict_class(c.get("action"), c.get("qualifier")), note=note)  # fmt: skip
    s.add(r)
    s.flush()
    s.refresh(r)
    return _json(r)


def history(s: Session) -> list[dict[str, Any]]:
    """Every recorded decision, oldest first (read only)."""
    return [_json(r) for r in s.scalars(select(CardOverride).order_by(CardOverride.id))]


def latest(s: Session) -> list[dict[str, Any]]:
    """The latest decision per card (B1.2), in card order."""
    by: dict[tuple[int, int], dict[str, Any]] = {}
    for d in history(s):
        by[(d["card_log_id"], d["card_index"])] = d
    return [by[k] for k in sorted(by)]


def rate(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Override rate = overrides / decisions with a known class; None (unknown) with none."""
    known = [d for d in decisions if d["override"] is not None]
    n = sum(1 for d in known if d["override"])
    return {"decisions": len(known), "overrides": n, "unknown_class": len(decisions) - len(known),
            "rate": n / len(known) if known else None}  # fmt: skip
