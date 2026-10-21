"""The append-only F&O card log (#360, P0.10): every plan, suggest and strategy-check response appends one row with
its inputs, each card's verdict, failed gates, PoP and EV per model, evidence label and the engine's git SHA.

Forward-only data: the cards the owner actually saw, with their live chain inputs, cannot be rebuilt later. Rows are
never updated or deleted (a database trigger refuses both, db.models.CARD_LOG_APPEND_ONLY). Resolution and scoring
are P2.1's; `verdict_counts` gives the release report its before/after verdict counts. Personal (the profile's F&O
budget): this module is never imported by a prompt-building or agent-tool package."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import date, datetime
from functools import lru_cache
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db import session_scope
from finresearch.db.models import CardLog

VERDICTS = ("ENTER", "NO PROVEN EDGE", "WAIT", "AVOID")
NO_VERDICT = "no verdict"  # a strategy check runs no gates: its verdict is unknown, never counted as one


@lru_cache(maxsize=1)
def engine_sha() -> str | None:
    """The full commit SHA of the checkout this process runs from; None when it is not a git checkout (unknown)."""
    from finresearch.config import REPO_ROOT

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and len(sha) == 40 else None


def _json(x: Any) -> Any:
    """JSON-safe copy (Decimal, date and datetime as strings) so the row holds exactly what was served."""
    return json.loads(json.dumps(x, default=str))


def _ev_check(card: dict[str, Any]) -> float | None:
    """The real-world EV after costs from the card's gate list (None when the card has none: unknown)."""
    for c in card.get("checks") or []:
        if str(c.get("name", "")).startswith("EV after costs"):
            return c.get("value")
    return None


def _card(c: dict[str, Any]) -> dict[str, Any]:
    p = c.get("probability") or {}
    return {"title": c.get("title"), "action": c.get("action"), "qualifier": c.get("qualifier"),
            "label": c.get("label"), "failed": c.get("failed"), "spec_key": c.get("spec_key"),
            "probability": {"real_world": p.get("real_world"), "risk_neutral": p.get("risk_neutral")},
            # a plan card shows the real-world EV gate only; its risk-neutral EV is not computed there (None)
            "ev": {"real_world": _ev_check(c), "risk_neutral": None},
            "evidence": c.get("evidence")}  # fmt: skip


def summarise(kind: str, response: dict[str, Any]) -> list[dict[str, Any]]:
    """One summary per card in the response. Total: a missing field is None, never a guess."""
    if kind == "suggest":
        return [_card(c) for c in response.get("cards") or []]
    if kind == "strategy":
        an = response.get("analysis") or {}
        rw, rn = an.get("real_world") or {}, an.get("risk_neutral") or {}
        return [{"title": f"{response.get('symbol')} {response.get('expiry')} · strategy check", "action": None,
                 "qualifier": None, "label": None, "failed": None, "spec_key": None,
                 "probability": {"real_world": rw.get("pop"), "risk_neutral": rn.get("pop")},
                 "ev": {"real_world": rw.get("ev"), "risk_neutral": rn.get("ev")}, "evidence": None}]  # fmt: skip
    return [_card(response)]


def row(kind: str, *, symbol: str, expiry: date | None, inputs: dict[str, Any], cards: list[dict[str, Any]],
        response: dict[str, Any], created_at: datetime | None = None) -> CardLog:  # fmt: skip
    r = CardLog(kind=kind, symbol=symbol, expiry=expiry, engine_sha=engine_sha(), inputs=_json(inputs),
                cards=_json(cards), response=_json(response))  # fmt: skip
    if created_at is not None:
        r.created_at = created_at
    return r


def append(kind: str, *, symbol: str, expiry: date | None, request: Any, chains: list[Any], response: dict[str, Any],
           plan_inputs: Any = None) -> int:  # fmt: skip
    """Log one response; returns the row id. `chains` = the live option chains it was built on (pydantic models),
    `plan_inputs` = signals.tradeplan.Inputs (every attribute is kept, so new fields are logged too) or a dict."""
    inputs = {"request": request, "chains": [c.model_dump(mode="json") for c in chains if c is not None],
              "plan": plan_inputs if plan_inputs is None or isinstance(plan_inputs, dict) else dict(vars(plan_inputs))}  # fmt: skip
    with session_scope() as s:
        r = row(
            kind,
            symbol=symbol,
            expiry=expiry,
            inputs=inputs,
            cards=summarise(kind, response),
            response=response,
        )
        s.add(r)
        s.flush()
        return r.id


def bucket(action: str | None, qualifier: str | None) -> str:
    """ENTER, NO PROVEN EDGE (an ENTER qualified "no proven edge"), WAIT, AVOID, or `no verdict`."""
    if action is None:
        return NO_VERDICT
    if action.upper() == "NO PROVEN EDGE" or (
        action == "ENTER" and "no proven edge" in (qualifier or "").lower()
    ):
        return "NO PROVEN EDGE"
    return action


def verdict_counts(s: Session, split: datetime) -> dict[str, Any]:
    """Card verdict counts before and after `split` (a release's deploy time): the release script's P0 effect."""
    out: dict[str, Any] = {"before": Counter(), "after": Counter(), "responses": Counter()}
    for created, cards in s.execute(select(CardLog.created_at, CardLog.cards)):
        side = "before" if created < split else "after"
        out["responses"][side] += 1
        for c in cards:
            out[side][bucket(c.get("action"), c.get("qualifier"))] += 1
    return {
        "before": {k: out["before"].get(k, 0) for k in (*VERDICTS, NO_VERDICT)} | dict(out["before"]),
        "after": {k: out["after"].get(k, 0) for k in (*VERDICTS, NO_VERDICT)} | dict(out["after"]),
        "responses": {"before": out["responses"]["before"], "after": out["responses"]["after"]},
    }
