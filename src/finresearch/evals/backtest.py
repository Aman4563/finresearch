"""Back-test: each IPO report's verdict against what happened at listing.

Outcomes come from the monitor (NSE's listing-day open, stored on the watch) or from the decision journal. A
listing verdict is a hit when APPLY met a positive listing gain or AVOID/SKIP met a flat or negative one;
APPLY-CONDITIONAL is scored as conditional (its condition decides, and it is reported, not counted).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import AgentStep, Company, Decision, ResearchRun, Watch


@dataclass
class BacktestRow:
    company: str
    run_id: int
    verdict: str
    listing_gain_pct: Decimal | None
    source: str | None
    result: str  # hit | miss | conditional | pending


def _verdict(session: Session, run_id: int) -> str | None:
    st = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                                 AgentStep.status == "done")
                         .order_by(AgentStep.finished_at.desc().nulls_last(), AgentStep.id.desc())).first()  # fmt: skip
    return (st.output or {}).get("overall_verdict") if st else None


def _outcome(session: Session, company_id: int, run_id: int) -> tuple[Decimal | None, str | None]:
    from finresearch.fincalc.ipo import listing_gain
    from finresearch.monitor.jobs import _upper_band

    w = session.scalar(select(Watch).where(Watch.company_id == company_id))
    if w and (w.meta or {}).get("listing_open"):
        upper = _upper_band(session, w)
        if upper:
            return listing_gain(
                upper, Decimal(w.meta["listing_open"])
            ) * 100, "NSE listing-day open (monitor)"
    d = session.scalars(select(Decision).where(Decision.run_id == run_id, Decision.listing_price.isnot(None),
                                               Decision.issue_price.isnot(None))).first()  # fmt: skip
    if d:
        return listing_gain(d.issue_price, d.listing_price) * 100, "decision journal"
    return None, None


def classify(verdict: str, gain: Decimal | None) -> str:
    if gain is None:
        return "pending"
    v = verdict.upper()
    if v.startswith("APPLY-CONDITIONAL"):
        return "conditional"
    if v.startswith("APPLY"):
        return "hit" if gain > 0 else "miss"
    if v.startswith(("AVOID", "SKIP")):
        return "hit" if gain <= 0 else "miss"
    return "conditional"


def backtest(session: Session) -> list[BacktestRow]:
    rows = []
    for run, co in session.execute(select(ResearchRun, Company).join(Company, Company.id == ResearchRun.company_id)
                                   .where(ResearchRun.kind == "ipo_report", ResearchRun.status == "done")
                                   .order_by(ResearchRun.id)):  # fmt: skip
        verdict = _verdict(session, run.id)
        if not verdict:
            continue
        gain, source = _outcome(session, co.id, run.id)
        rows.append(BacktestRow(co.name, run.id, verdict, gain, source, classify(verdict, gain)))
    return rows


def markdown(rows: list[BacktestRow]) -> str:
    scored = [r for r in rows if r.result in ("hit", "miss")]
    hits = sum(r.result == "hit" for r in scored)
    lines = ["### Back-test: report verdicts against listing outcomes", "",
             f"Scored: {len(scored)} · hits: {hits} · pending: {sum(r.result == 'pending' for r in rows)} · "
             f"conditional: {sum(r.result == 'conditional' for r in rows)}", "",
             "| company | run | verdict | listing gain | source | result |", "|---|---|---|---|---|---|"]  # fmt: skip
    for r in rows:
        gain = f"{r.listing_gain_pct:+.2f}%" if r.listing_gain_pct is not None else "—"
        lines.append(f"| {r.company} | {r.run_id} | {r.verdict} | {gain} | {r.source or '—'} | {r.result} |")
    return "\n".join(lines) + "\n"
