"""The F&O backtest evidence (#307): the pre-registered specs and criteria, the latest saved evaluation and each
candidate's current setup. Read-only; runs are made with `finresearch fno backtest run`."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException

# #354 h: the hash is over the registered spec's parameters, so "matches code" overstated it; matches_code stays one
# release as a deprecated alias of matches_spec
HASH_COVERS = (
    "spec parameters only (specs.registration()): the engine code that runs them is hashed separately on each saved "
    "run (code_hash and engine_hash, #379), and a run whose code differs from this install says so"
)


def registration() -> dict[str, Any]:
    from finresearch.backtest import prereg, specs

    return {
        "version": specs.SPEC_VERSION, "hash": prereg.HASH, "registered_on": prereg.REGISTERED_ON,
        "matches_spec": specs.registration_hash() == prereg.HASH, "hash_covers": HASH_COVERS,
        "matches_code": specs.registration_hash() == prereg.HASH, "note": prereg.NOTE,
        "variants_tried": specs.VARIANTS_TRIED, "specs": [asdict(s) for s in specs.SPECS],
        "book": asdict(specs.BOOK), "criteria": asdict(specs.CRITERIA),
        "windows": {"is": [specs.IS_START.isoformat(), specs.IS_END.isoformat()],
                    "oos_from": specs.OOS_START.isoformat()},
        "doc": "evals/experiments/fno_backtest/PREREG.md", "deflated_sharpe_source": specs.DEFLATED_SHARPE_SOURCE,
    }  # fmt: skip


def add_fno_backtest_routes(app: FastAPI) -> None:
    @app.get("/api/fno/backtests")
    def fno_backtests() -> dict[str, Any]:
        """The registration, the latest saved evaluation (None before the first run) and the candidates' setups."""
        from finresearch.signals.fno import backtest_evidence, risk_notice

        return {"registration": registration(), "run": backtest_evidence(), "risk_notice": risk_notice()}

    @app.get("/api/fno/backtests/{run_id}/{key}/trades")
    def fno_backtest_trades(run_id: int, key: str) -> dict[str, Any]:
        """Every trade of one strategy in one saved run (entry, exit, legs, costs, net P&L, exit reason)."""
        from finresearch.backtest import store
        from finresearch.db import session_scope

        with session_scope() as s:
            r = store.result(s, run_id, key)
            if r is None:
                raise HTTPException(404, f"no strategy {key!r} in backtest run {run_id}")
            return {"run_id": run_id, "key": key, "name": r.name, "status": r.status, "trades": r.trades}
