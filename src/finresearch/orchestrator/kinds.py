"""Research kinds: ResearchRun.kind → the pipeline that researches it."""

from __future__ import annotations

from finresearch.orchestrator.base import PipelineConfig, ResearchPipeline
from finresearch.orchestrator.ipo import IpoPipeline
from finresearch.orchestrator.stock import StockPipeline

KINDS: dict[str, type[ResearchPipeline]] = {IpoPipeline.kind: IpoPipeline, StockPipeline.kind: StockPipeline}


def kind_of(run_id: int) -> str:
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun

    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None:
            raise ValueError(f"unknown run {run_id}")
        return run.kind


def pipeline_for(run_id: int, config: PipelineConfig | None = None, **kw) -> ResearchPipeline:
    kind = kind_of(run_id)
    if kind not in KINDS:
        raise ValueError(f"run {run_id} has kind {kind!r}, which has no research pipeline")
    return KINDS[kind](run_id, config=config, **kw)
