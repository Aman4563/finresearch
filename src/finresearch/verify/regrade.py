"""Re-check grade-D (fincalc) citations saved before #265's argument binding (#285). Deterministic: fincalc is run
again on the stored call and `bind_args` on the cited input claims as stored; no LLM, no network.

A citation's grade is fixed when its claim is saved, so a citation saved before #265 is still grade D without its
arguments ever being tied to the cited claims. `regrade` re-runs the save-time check (`claims._computed_citation`)
on each such citation: one that still holds stays D and gains `bindings`/`constants`/`args_bound`; one that fails
becomes U (quote_found false) with the reason in `computation.detail`. Each report whose gate result changes is
listed, the gate re-evaluated read-only the way a re-render does (`verify.gate.check_report` on the latest
synthesis). Claim statuses are never changed.

Dry run (the default) issues no INSERT/UPDATE/DELETE. `apply=True` writes every change in the caller's transaction
and stamps each changed citation's `computation["regrade"]` with who, when, why and the full previous
`quote_found`/`computation` (restore those two fields to revert). Applying twice changes nothing the second time.
"""

from __future__ import annotations

import getpass
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import AgentStep, Citation, Claim

log = logging.getLogger("finresearch.evidence.regrade")
DEFAULT_REASON = "#285: re-check grade-D fincalc citations saved before #265 argument binding"


@dataclass
class CitationResult:
    run_id: int
    claim_id: int
    citation_id: int
    importance: str
    grade: str  # "D" (still holds) or "U"
    reason: str
    changed: bool  # the stored row differs from the re-check (False: already up to date, only with all_d)
    new: tuple[bool, dict[str, Any]] = field(repr=False, default=(False, {}))


@dataclass
class GateChange:
    run_id: int
    before_ok: bool | None  # None: the run has no finished report
    after_ok: bool | None
    new_blocking: list[str] = field(default_factory=list)

    @property
    def changes(self) -> bool:
        return self.before_ok != self.after_ok


@dataclass
class RegradeResult:
    citations: list[CitationResult]
    gates: list[GateChange]
    applied: bool

    @property
    def changed(self) -> list[CitationResult]:
        return [c for c in self.citations if c.changed]


def _is_grade_d(ct: Citation) -> bool:
    from finresearch.verify.evidence import citation_grade

    return citation_grade(ct)[0] == "D"


def _recheck(session: Session, ct: Citation, claim: Claim) -> tuple[bool, dict[str, Any], str]:
    """(quote_found, computation, reason) the save-time check gives for this stored citation today."""
    from finresearch.mcp_server.claims import _computed_citation
    from finresearch.mcp_server.server import run_fincalc

    old = ct.computation or {}
    spec = {"function": old.get("function"), "args": old.get("args") or {}}
    if old.get("result_key") is not None:
        spec["result_key"] = old["result_key"]
    c: dict[str, Any] = {"fincalc": spec, "inputs": old.get("inputs") or []}
    if old.get("constants"):
        c["constants"] = old["constants"]
    try:
        new, _ = _computed_citation(session, claim.run_id, c, claim.value, claim.unit, run_fincalc)
    except ValueError as e:  # the stored call no longer runs or is malformed: it proves nothing
        detail = f"fincalc citation could not be re-checked: {e}"[:1000]
        return False, {**{k: v for k, v in old.items() if k != "regrade"}, "args_bound": False,
                       "detail": detail}, detail  # fmt: skip
    comp = dict(new.computation or {})
    return bool(new.quote_found), comp, str(comp.get("detail") or "")


def _gate(session: Session, run_id: int) -> tuple[bool | None, list[str]]:
    from finresearch.verify.gate import check_report

    st = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                                 AgentStep.status == "done")
                         .order_by(AgentStep.finished_at.desc(), AgentStep.id.desc())).first()  # fmt: skip
    md = (st.output or {}).get("report_markdown") if st else None
    if not md:
        return None, []
    g = check_report(session, run_id, md)
    return g.ok, list(g.blocking)


def regrade(session: Session, *, all_d: bool = False, apply: bool = False, reason: str = DEFAULT_REASON,
            who: str | None = None) -> RegradeResult:  # fmt: skip
    """Re-check stored grade-D citations: those without a binding result (`args_bound` absent), or every grade-D
    citation with `all_d`. Without `apply` nothing is written (the caller should still roll back). With `apply`, the
    changes are made in the session; the caller commits them in one transaction."""
    rows = session.scalars(select(Citation).where(Citation.computation.is_not(None), Citation.quote_found.is_(True))
                           .order_by(Citation.id)).all()  # fmt: skip
    todo = [ct for ct in rows if _is_grade_d(ct) and (all_d or "args_bound" not in (ct.computation or {}))]
    # 1. every re-check and the gates before any change (reads only: nothing is pending, so nothing autoflushes)
    results: list[CitationResult] = []
    for ct in todo:
        claim = session.get(Claim, ct.claim_id)
        found, comp, why = _recheck(session, ct, claim)
        stored = {k: v for k, v in (ct.computation or {}).items() if k != "regrade"}
        results.append(CitationResult(run_id=claim.run_id, claim_id=claim.id, citation_id=ct.id,
                                      importance=claim.importance, grade="D" if found else "U", reason=why,
                                      changed=(found, comp) != (bool(ct.quote_found), stored), new=(found, comp)))  # fmt: skip
    runs = sorted({r.run_id for r in results if r.changed and r.grade == "U"})
    before = {run_id: _gate(session, run_id) for run_id in runs}
    # 2. the changes in memory, the gates again, then write or discard
    by_id = {ct.id: ct for ct in todo}
    now, who = datetime.now(UTC).isoformat(timespec="seconds"), who or getpass.getuser()
    with session.no_autoflush:
        for r in results:
            if not r.changed:
                continue
            ct = by_id[r.citation_id]
            found, comp = r.new
            stamp = {"at": now, "by": who, "why": reason,
                     "before": {"quote_found": ct.quote_found, "computation": ct.computation}}  # fmt: skip
            ct.quote_found = found
            # a new dict: a JSONB column sees a change on assignment only
            ct.computation = {**comp, "regrade": stamp}
        gates = []
        for run_id in runs:
            (b_ok, b_block), (a_ok, a_block) = before[run_id], _gate(session, run_id)
            gates.append(GateChange(run_id=run_id, before_ok=b_ok, after_ok=a_ok,
                                    new_blocking=[x for x in a_block if x not in b_block]))  # fmt: skip
    if apply:
        session.flush()
        for r in results:
            if r.changed:
                log.info("evidence regrade: run %s claim C%s citation %s -> %s by %s (%s): %s", r.run_id,
                         r.claim_id, r.citation_id, r.grade, who, reason, r.reason[:300])  # fmt: skip
    else:
        session.rollback()  # drop the in-memory changes; nothing was flushed
    return RegradeResult(citations=results, gates=gates, applied=apply)
