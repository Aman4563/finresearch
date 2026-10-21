"""Save and read backtest reports (#307): fno_backtest_run + one fno_backtest_result per strategy."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from finresearch.db.models import FnoBacktestResult, FnoBacktestRun

RUN_KEYS = ("version", "registration_hash", "registered_hash", "oos_evaluated", "source", "variants_tried",
            "stock_rule_symbol")  # fmt: skip


def save(s: Session, report: dict[str, Any]) -> int:
    run = FnoBacktestRun(**{k: report[k] for k in RUN_KEYS},
                         detail={k: v for k, v in report.items() if k not in RUN_KEYS and k != "strategies"})  # fmt: skip
    s.add(run)
    s.flush()
    for r in report["strategies"]:
        s.add(FnoBacktestResult(run_id=run.id, key=r["key"], name=r["name"], role=r["role"], status=r["status"],
                                spec_hash=r["spec_hash"], underlying=r.get("underlying"),
                                payload={k: v for k, v in r.items() if k != "trades"},
                                trades=r.get("trades") or []))  # fmt: skip
    return run.id


def latest_run(s: Session) -> FnoBacktestRun | None:
    """The newest same-print run that evaluated out of sample, else the newest same-print run. A next-print run
    (#382: detail.fill) is a sensitivity, never the evidence the cards, the Strategy Lab and the page read."""
    fill = FnoBacktestRun.detail["fill"].astext
    q = (select(FnoBacktestRun).where(or_(fill.is_(None), fill == "same_print"))
         .order_by(FnoBacktestRun.created_at.desc(), FnoBacktestRun.id.desc()))  # fmt: skip
    return (
        s.scalars(q.where(FnoBacktestRun.oos_evaluated.is_(True)).limit(1)).first()
        or s.scalars(q.limit(1)).first()
    )


def run_json(s: Session, run: FnoBacktestRun, trades: bool = False) -> dict[str, Any]:
    rows = s.scalars(
        select(FnoBacktestResult).where(FnoBacktestResult.run_id == run.id).order_by(FnoBacktestResult.id)
    )
    out = {"id": run.id, "created_at": run.created_at.isoformat() if run.created_at else None,
           **{k: getattr(run, k) for k in RUN_KEYS}, **run.detail, "strategies": []}  # fmt: skip
    for r in rows:
        out["strategies"].append(
            {**r.payload, **({"trades": r.trades} if trades else {"n_trades": len(r.trades)})}
        )
    return out


def result(s: Session, run_id: int, key: str) -> FnoBacktestResult | None:
    return s.scalars(select(FnoBacktestResult).where(FnoBacktestResult.run_id == run_id,
                                                     FnoBacktestResult.key == key)).first()  # fmt: skip
