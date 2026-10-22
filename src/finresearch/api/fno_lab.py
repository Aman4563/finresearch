"""The F&O Strategy Lab (#318): define strategies from building blocks, backtest them in a background worker on the
stored F&O history (read only) with a locked out-of-sample window and a global variants count, compare them with the
pre-registered library, and send a tested version to paper trading. Nothing here places an order."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from finresearch.db import session_scope
from finresearch.stratlab.spec import StrategySpec

log = logging.getLogger(__name__)


def _history() -> Any:
    from finresearch.backtest.dbhistory import DbHistory

    return DbHistory()


HISTORY: Callable[[], Any] = _history  # tests swap in a synthetic history


class StrategyIn(BaseModel):
    name: str = Field("", max_length=120)
    spec: StrategySpec


class VersionIn(BaseModel):
    spec: StrategySpec
    note: str = Field("", max_length=200)


class RunIn(BaseModel):
    oos: bool = Field(
        False,
        description="False: in-sample only; True: out of sample and the forward holdout. Every run is a "
        "logged trial and counts in N (#380)",
    )


def blocks() -> dict[str, Any]:
    from dataclasses import asdict

    from finresearch.backtest import specs
    from finresearch.stratlab import jobs, spec, split

    return {"structures": spec.STRUCTURES, "trends": spec.TRENDS, "weekdays": list(spec.WEEKDAYS),
            "max_dte": spec.MAX_DTE, "defaults": StrategySpec.model_json_schema()["properties"],
            "criteria": asdict(specs.CRITERIA), "run_cap_minutes": int(jobs.RUN_CAP.total_seconds() // 60),
            "min_span_days": split.MIN_SPAN_DAYS, "split_rule": split.__doc__,
            "results_note": spec.RESULTS_NOTE, "deflated_sharpe_source": specs.DEFLATED_SHARPE_SOURCE}  # fmt: skip


def _underlyings(s: Any) -> list[str]:
    from finresearch.db.models import FnoContract

    return sorted(set(s.scalars(select(FnoContract.symbol).distinct())))


def add_fno_lab_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    from finresearch.stratlab import store

    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    def refuse(e: Exception, code: int = 422) -> HTTPException:
        return HTTPException(code, str(e))

    @app.get("/api/fno/lab")
    def fno_lab() -> dict[str, Any]:
        """Every lab strategy with its versions and runs, the variants count and the building blocks."""
        from finresearch.backtest import specs
        from finresearch.db.models import LabStrategy
        from finresearch.stratlab import jobs

        with session_scope() as s:
            jobs.recover_stale(s, now())
            rows = [
                store.strategy_json(s, st)
                for st in s.scalars(select(LabStrategy).order_by(LabStrategy.id.desc()))
            ]
            lab, trials, vault = store.lab_variants(s), store.lab_trials(s), store.vault_reads_in_n(s)
            n = store.n_trials(s)
            unders = _underlyings(s)
        # n (#380): the library's VARIANTS_TRIED + every spec run in the lab (in sample too) + vault reads in N
        return {"strategies": rows, "underlyings": unders, "blocks": blocks(),
                "variants": {"library": specs.VARIANTS_TRIED, "lab": lab, "trials": trials, "vault_reads": vault,
                             "n": n}}  # fmt: skip

    @app.get("/api/fno/lab/family")
    def fno_lab_family() -> dict[str, Any]:
        """PBO, SPA and Romano-Wolf over every spec run in the lab (#380, stratlab.family). Reads only."""
        from finresearch.stratlab import family

        with session_scope() as s:
            return family.statistics(family.matrix(s))

    @app.get("/api/fno/lab/versions/{version_id}/promotion")
    def fno_lab_promotion(version_id: int) -> dict[str, Any]:
        """The forward-incubation gate of one version (#380, stratlab.promotion): the only path to lifting
        'no proven edge'. Reads only."""
        from finresearch.stratlab import promotion

        with session_scope() as s:
            try:
                return promotion.judge(s, version_id)
            except store.LabError as e:
                raise HTTPException(404, str(e)) from e

    @app.post("/api/fno/lab/strategies", status_code=201)
    def fno_lab_create(body: StrategyIn) -> dict[str, Any]:
        """A new strategy: its in-sample / out-of-sample split is locked now (stratlab.split) and never changes."""
        h = HISTORY()
        with session_scope() as s:
            try:
                st = store.create_strategy(s, body.name, body.spec, h, now=now())
            except store.LabError as e:
                raise refuse(e) from e
            s.flush()
            s.refresh(st)
            return store.strategy_json(s, st)

    @app.post("/api/fno/lab/strategies/{strategy_id}/versions", status_code=201)
    def fno_lab_version(strategy_id: int, body: VersionIn) -> dict[str, Any]:
        """Save a changed spec as the next version (same split); an unchanged spec returns its existing version."""
        from finresearch.db.models import LabStrategy

        with session_scope() as s:
            st = s.get(LabStrategy, strategy_id)
            if st is None:
                raise HTTPException(404, f"no lab strategy {strategy_id}")
            try:
                v = store.add_version(s, st, body.spec, body.note, now=now())
            except store.LabError as e:
                raise refuse(e) from e
            s.refresh(st)
            return {"version_id": v.id, "version": v.version, "strategy": store.strategy_json(s, st)}

    @app.post("/api/fno/lab/versions/{version_id}/runs", status_code=202)
    def fno_lab_run(version_id: int, body: RunIn) -> dict[str, Any]:
        """Queue a backtest and start its background worker; poll GET /api/fno/lab/runs/{id} for progress."""
        from finresearch.db.models import LabRun
        from finresearch.stratlab import jobs

        with session_scope() as s:
            try:
                r = jobs.submit(s, version_id, body.oos, now())
            except store.LabError as e:
                raise refuse(e, 409 if "already" in str(e) else 422) from e
            rid = r.id
        try:
            pid = jobs.LAUNCHER(rid)
        except Exception as e:
            log.warning("lab worker for run %s did not start", rid, exc_info=True)
            with session_scope() as s:
                r = s.get(LabRun, rid)
                r.status, r.error, r.finished_at = (
                    "failed",
                    f"the worker did not start: {type(e).__name__}",
                    now(),
                )
            raise HTTPException(503, "the background worker could not be started") from e
        with session_scope() as s:
            r = s.get(LabRun, rid)
            if pid and r.pid is None:
                r.pid = pid
            return store.run_json(r)

    @app.get("/api/fno/lab/runs/{run_id}")
    def fno_lab_run_view(run_id: int) -> dict[str, Any]:
        """Status and progress; when done, the result (no trades), the library beside it and today's bar."""
        from finresearch.db.models import LabRun, LabStrategy, LabVersion
        from finresearch.stratlab import evaluate as ev
        from finresearch.stratlab import jobs

        with session_scope() as s:
            jobs.recover_stale(s, now())
            r = s.get(LabRun, run_id)
            if r is None:
                raise HTTPException(404, f"no lab run {run_id}")
            out = store.run_json(r, full=True)
            v = s.get(LabVersion, r.version_id)
            st = s.get(LabStrategy, v.strategy_id)
            out["strategy"] = {
                "id": st.id,
                "name": st.name,
                "version": v.version,
                "underlying": st.underlying,
            }
            if r.status == "done":
                out["library"] = store.library_comparison(s)
                n, var = store.n_trials(s), ev.trials_variance(store.trial_srs(s))
                oos = ((r.result or {}).get("windows") or {}).get("oos")
                out["bar_now"] = {"n_trials": n, "deflated_sharpe": ev.deflated(oos, n, var) if oos else None,
                                  "note": "The deflated Sharpe recomputed with today's variants count: it falls as "
                                          "more variants are tried, even for a run made earlier."}  # fmt: skip
            return out

    @app.get("/api/fno/lab/runs/{run_id}/trades")
    def fno_lab_run_trades(run_id: int) -> dict[str, Any]:
        from finresearch.db.models import LabRun

        with session_scope() as s:
            r = s.get(LabRun, run_id)
            if r is None:
                raise HTTPException(404, f"no lab run {run_id}")
            return {"run_id": run_id, "status": r.status, "trades": r.trades or []}

    @app.post("/api/fno/lab/versions/{version_id}/paper", status_code=201)
    def fno_lab_paper_start(version_id: int) -> dict[str, Any]:
        """Send a tested version to paper trading: signals from the next stored close on (stratlab.paper)."""
        from finresearch.stratlab import paper

        with session_scope() as s:
            try:
                r = paper.create_rule(s, version_id, now())
            except store.LabError as e:
                raise refuse(e, 409 if "already" in str(e) else 422) from e
            return rule_json(s, r, today())

    @app.post("/api/fno/lab/paper/{rule_id}/stop")
    def fno_lab_paper_stop(rule_id: int) -> dict[str, Any]:
        from finresearch.stratlab import paper

        with session_scope() as s:
            try:
                r = paper.stop_rule(s, rule_id, now())
            except store.LabError as e:
                raise HTTPException(404, str(e)) from e
            return rule_json(s, r, today())

    @app.get("/api/fno/lab/paper")
    def fno_lab_paper() -> dict[str, Any]:
        """Every paper rule set with its signals, their paper trades and the closed trades' results."""
        from finresearch.db.models import LabPaperRule

        with session_scope() as s:
            rules = s.scalars(select(LabPaperRule).order_by(LabPaperRule.id.desc()))
            return {"rules": [rule_json(s, r, today()) for r in rules]}

    def today() -> date:
        from finresearch.fincalc.dates import to_ist

        return to_ist(now()).date()


def rule_json(s: Any, r: Any, today: date) -> dict[str, Any]:
    from finresearch.db.models import LabPaperSignal, LabStrategy, LabVersion, PaperStrategy
    from finresearch.fincalc import paper as pp

    v = s.get(LabVersion, r.version_id)
    st = s.get(LabStrategy, v.strategy_id)
    sigs = list(
        s.scalars(select(LabPaperSignal).where(LabPaperSignal.rule_id == r.id).order_by(LabPaperSignal.day))
    )
    rows, closed = [], []
    for g in sigs:
        p = s.get(PaperStrategy, g.paper_id) if g.paper_id else None
        if p is not None and p.status == "closed" and p.net_pnl is not None:
            closed.append(float(p.net_pnl))
        rows.append({"id": g.id, "day": g.day.isoformat(), "status": g.status, "expiry": g.expiry.isoformat(),
                     "lots": g.lots, "lot": g.lot, "legs": g.legs, "premium_at_signal": float(g.premium),
                     "paper_id": g.paper_id, "paper_status": p.status if p else None,
                     "mtm_gross": None if p is None or p.mtm_gross is None else float(p.mtm_gross),
                     "net_pnl": None if p is None or p.net_pnl is None else float(p.net_pnl),
                     "exit_reason": g.exit_reason, "note": g.note})  # fmt: skip
    res = pp.results(closed)
    return {"id": r.id, "version_id": v.id, "version": v.version, "strategy_id": st.id, "strategy": st.name,
            "underlying": st.underlying, "status": r.status, "created_at": r.created_at.isoformat(),
            "start_after": r.start_after.isoformat(), "last_day": r.last_day.isoformat() if r.last_day else None,
            "note": r.note, "signals": rows,
            "results": {"closed": res.closed, "wins": res.wins, "total_net": round(res.total, 2),
                        "expectancy": None if res.expectancy is None else round(res.expectancy, 2),
                        "max_drawdown": round(res.max_drawdown, 2)},
            "method": "Signals come from each stored close after the rule set was created; each is filled on a later "
                      "session at live prices with the paper book's slippage and charges, and closed by the "
                      "version's rules at live prices. Missed signals are kept. Paper fills are kinder than real ones."}  # fmt: skip
