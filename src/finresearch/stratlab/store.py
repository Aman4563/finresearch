"""Strategy Lab rows (#318): strategies with their locked split, versions, the global variants counter and views."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.backtest import specs
from finresearch.db.models import LabRun, LabStrategy, LabVariant, LabVersion
from finresearch.stratlab import split as sp
from finresearch.stratlab.spec import StrategySpec, describe, spec_hash


class LabError(ValueError):
    """A request the lab refuses (shown to the user as is)."""


def create_strategy(s: Session, name: str, spec: StrategySpec, history: Any) -> LabStrategy:
    """A new strategy with its split locked now from the stored history of the underlying, and version 1."""
    try:
        got = sp.lock(*sp.span(history, spec.underlying))
    except sp.SplitError as e:
        raise LabError(str(e)) from e
    st = LabStrategy(name=name.strip()[:120] or describe(spec)[:120], underlying=spec.underlying,
                     is_start=got.is_start, is_end=got.is_end, oos_start=got.oos_start, data_first=got.data_first,
                     data_last=got.data_last, split_rule=got.rule[:300])  # fmt: skip
    s.add(st)
    s.flush()
    add_version(s, st, spec, "first version")
    return st


def add_version(s: Session, st: LabStrategy, spec: StrategySpec, note: str = "") -> LabVersion:
    """A new version; the same spec as an existing version of this strategy returns that version (no new variant)."""
    if spec.underlying != st.underlying:
        raise LabError(
            f"the strategy's split is locked on {st.underlying}: another underlying is a new strategy"
        )
    h = spec_hash(spec)
    old = s.scalars(
        select(LabVersion).where(LabVersion.strategy_id == st.id, LabVersion.spec_hash == h)
    ).first()
    if old is not None:
        return old
    n = s.scalar(select(func.max(LabVersion.version)).where(LabVersion.strategy_id == st.id)) or 0
    v = LabVersion(strategy_id=st.id, version=n + 1, spec=spec.model_dump(), spec_hash=h, note=note[:200])
    s.add(v)
    s.flush()
    return v


def split_of(st: LabStrategy) -> sp.Split:
    return sp.Split(st.is_start, st.is_end, st.oos_start, st.data_first, st.data_last, st.split_rule)


# --------------------------------------------------------------------------- the variants counter
def lab_variants(s: Session) -> int:
    return s.scalar(select(func.count()).select_from(LabVariant)) or 0


def n_trials(s: Session) -> int:
    return specs.VARIANTS_TRIED + lab_variants(s)


def library_srs(s: Session) -> list[float]:
    """Out-of-sample daily Sharpe ratios of the library's latest out-of-sample evaluation (none before one is saved)."""
    from finresearch.backtest import store as bstore

    run = bstore.latest_run(s)
    if run is None or not run.oos_evaluated:
        return []
    out = []
    for r in bstore.run_json(s, run)["strategies"]:
        sr = ((r.get("windows") or {}).get("oos") or {}).get("daily_sr")
        if sr is not None:
            out.append(float(sr))
    return out


def trial_srs(s: Session) -> list[float]:
    return library_srs(s) + [
        float(x) for x in s.scalars(select(LabVariant.daily_sr).where(LabVariant.daily_sr.is_not(None)))
    ]


def oos_versions(s: Session, strategy_id: int) -> int:
    """Versions of this strategy counted as variants (evaluated out of sample)."""
    return s.scalar(select(func.count()).select_from(LabVariant).join(LabVersion, LabVariant.version_id == LabVersion.id)
                    .where(LabVersion.strategy_id == strategy_id)) or 0  # fmt: skip


# --------------------------------------------------------------------------- views
def _iso(x: Any) -> str | None:
    return x.isoformat() if x is not None else None


def run_json(r: LabRun, full: bool = False) -> dict[str, Any]:
    out = {"id": r.id, "version_id": r.version_id, "oos": r.oos, "status": r.status, "progress": round(r.progress, 4),
           "progress_note": r.progress_note, "created_at": _iso(r.created_at), "started_at": _iso(r.started_at),
           "finished_at": _iso(r.finished_at), "error": r.error, "n_trials": r.n_trials, "counted": r.counted,
           "n_trades": len(r.trades or [])}  # fmt: skip
    if full:
        out["result"] = r.result or None
    else:
        res = r.result or {}
        w = res.get("windows") or {}
        out["summary"] = {"status": res.get("status"), "is": _brief(w.get("is")), "oos": _brief(w.get("oos")),
                          "warnings": len(res.get("warnings") or [])}  # fmt: skip
    return out


def _brief(w: dict[str, Any] | None) -> dict[str, Any] | None:
    if not w:
        return None
    return {k: w.get(k) for k in ("trades", "expectancy", "win_rate", "sharpe", "max_drawdown_pct", "deflated_sharpe",
                                  "total_net")}  # fmt: skip


def strategy_json(s: Session, st: LabStrategy) -> dict[str, Any]:
    from finresearch.db.models import LabPaperRule

    vids = [v.id for v in st.versions]
    runs = (
        list(s.scalars(select(LabRun).where(LabRun.version_id.in_(vids)).order_by(LabRun.id.desc())))
        if vids
        else []
    )
    rules = list(s.scalars(select(LabPaperRule).where(LabPaperRule.version_id.in_(vids)))) if vids else []
    counted = (
        set(s.scalars(select(LabVariant.version_id).where(LabVariant.version_id.in_(vids))))
        if vids
        else set()
    )
    return {
        "id": st.id, "name": st.name, "underlying": st.underlying, "created_at": _iso(st.created_at),
        "split": {"is": [st.is_start.isoformat(), st.is_end.isoformat()], "oos_from": st.oos_start.isoformat(),
                  "data": [st.data_first.isoformat(), st.data_last.isoformat()], "rule": st.split_rule},
        "oos_versions": len(counted),
        "versions": [{"id": v.id, "version": v.version, "created_at": _iso(v.created_at), "spec": v.spec,
                      "spec_hash": v.spec_hash, "note": v.note, "describe": describe(StrategySpec.model_validate(v.spec)),
                      "counted_variant": v.id in counted,
                      "runs": [run_json(r) for r in runs if r.version_id == v.id],
                      "paper_rules": [{"id": p.id, "status": p.status} for p in rules if p.version_id == v.id]}
                     for v in st.versions],
    }  # fmt: skip


def library_comparison(s: Session) -> dict[str, Any] | None:
    """The pre-registered strategies' latest evaluation, side by side with a lab result."""
    from finresearch.backtest import store as bstore

    run = bstore.latest_run(s)
    if run is None:
        return None
    rows = []
    for r in bstore.run_json(s, run)["strategies"]:
        w = r.get("windows") or {}
        rows.append({"key": r["key"], "name": r["name"], "underlying": r.get("underlying"), "status": r["status"],
                     "is": _brief(w.get("is")), "oos": _brief(w.get("oos"))})  # fmt: skip
    return {"run_id": run.id, "oos_evaluated": run.oos_evaluated, "variants_tried": run.variants_tried,
            "windows": run.detail.get("windows"), "strategies": rows}  # fmt: skip
