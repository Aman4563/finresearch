"""The monthly re-validation of the backtest-backed models (#379): the registered library (backtest/specs.py) replayed
read-only on the stored history, its trades reported per regime split, and the result APPENDED to model_revalidation.

    splits = (.., 19-Nov-2024], [20-Nov-2024, 31-Aug-2025], [1-Sep-2025, ..)   (regimes.SPLIT_DAYS, issue #379)
    a trade belongs to the split of the session it was decided at (its decision day, else its entry day, as
    backtest.evaluate.window); per split: backtest.metrics.trade_stats (expectancy, expiry-clustered t, bootstrap CI)

What it never does: save a backtest run (backtest.store.save), touch fno_backtest_run/fno_backtest_result (the evidence
the cards, the Strategy Lab and the backtest page read), set a status or create a candidate. The issue calls the
regime-split re-run "a post hoc ADDENDUM analysis that can create no candidate", so a row carries statistics only.
The replay refuses to run when specs.py differs from its registration (evaluate.require_registered). Equity
statistics (drawdown, Sharpe) are not split: they need the unthinned equity path, which the report does not keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.fincalc.dates import to_ist
from finresearch.inventory import provenance, regimes


@dataclass(frozen=True)
class Split:
    key: str
    lo: date | None  # first day, None = from the start of the history
    hi: date | None  # last day, None = to the end of the history


def splits() -> list[Split]:
    days = regimes.SPLIT_DAYS
    out = [Split(f"before_{days[0].isoformat()}", None, days[0] - timedelta(days=1))]
    for a, b in zip(days, days[1:], strict=False):
        end = b - timedelta(days=1)
        out.append(Split(f"{a.isoformat()}_to_{end.isoformat()}", a, end))
    out.append(Split(f"from_{days[-1].isoformat()}", days[-1], None))
    return out


def _day(t: dict[str, Any]) -> date:
    return date.fromisoformat(t.get("decision_day") or t["entry_day"])


def split_rows(rep: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per (strategy, split) of an evaluate-style report: statistics only, never a status from the replay."""
    from finresearch.backtest import metrics
    from finresearch.backtest.specs import CRITERIA

    rows = []
    for st in rep["strategies"]:
        key = f"recipe:{st['key']}"
        for sp in splits():
            row = {"model_key": key, "split": sp.key, "split_from": sp.lo, "split_to": sp.hi, "metrics": None}
            if st.get("status") == "no_data" or st.get("trades") is None:
                why = "; ".join(st.get("reasons") or []) or "no stored history for this recipe"
                rows.append({**row, "status": "no_data", "reason": why[:300]})
                continue
            tr = [
                t
                for t in st["trades"]
                if (sp.lo is None or _day(t) >= sp.lo) and (sp.hi is None or _day(t) <= sp.hi)
            ]
            if not tr:
                rows.append({**row, "status": "no_data", "reason": "no trade was entered in this split"})
                continue
            stats = metrics.trade_stats(tr, CRITERIA.bootstrap_draws, CRITERIA.bootstrap_seed)
            rows.append({**row, "status": "computed", "reason": None, "metrics": stats})
    return rows


def run(history: Any, *, slot: str, source: str, now: datetime, stock: str | None = None,
        data: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Replay, split and append (a row already stored for the slot is kept: a retried slot appends nothing).
    `data`: provenance.data_hash of the history read, when the caller can compute it (None = unknown)."""
    from finresearch.backtest import evaluate
    from finresearch.db import session_scope
    from finresearch.db.models import ModelRevalidation

    stamp = provenance.code_stamp()
    rep = evaluate.evaluate(history, stock=stock, source=source, oos=True)
    tags = regimes.regime_state(to_ist(now).date())
    common = {"slot": slot, "code_hash": stamp["code_hash"], "engine_hash": stamp["engine_hash"],
              "data_hash": (data or {}).get("data_hash"), "registration_hash": rep["registration_hash"],
              "regime_tags": tags, "source": source[:200]}  # fmt: skip
    rows = [{**common, **r} for r in split_rows(rep)]
    added = 0
    with session_scope() as s:
        for r in rows:
            ins = insert(ModelRevalidation).values(**r).on_conflict_do_nothing(
                index_elements=["slot", "model_key", "split"])  # fmt: skip
            added += len(s.execute(ins.returning(ModelRevalidation.id)).all())
    return {"slot": slot, "rows": added, "models": len(rep["strategies"]), "splits": len(splits())}


def latest_by_model(s: Session) -> dict[str, dict[str, Any]]:
    """Per model, the newest slot's rows by split (read only)."""
    from finresearch.db.models import ModelRevalidation

    out: dict[str, dict[str, Any]] = {}
    q = select(ModelRevalidation).order_by(ModelRevalidation.created_at.desc(), ModelRevalidation.id.desc())
    for r in s.scalars(q):
        m = out.setdefault(r.model_key, {"slot": r.slot, "created_at": r.created_at.isoformat(), "splits": {},
                                         "code_hash": r.code_hash, "regime_tags": r.regime_tags})  # fmt: skip
        if r.slot == m["slot"] and r.split not in m["splits"]:
            m["splits"][r.split] = {"from": r.split_from.isoformat() if r.split_from else None,
                                    "to": r.split_to.isoformat() if r.split_to else None, "status": r.status,
                                    "reason": r.reason, "metrics": r.metrics}  # fmt: skip
    return out
