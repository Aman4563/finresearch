"""Export a finished run's ledger to a text fixture and replay it offline.

A fixture holds everything the gold scorer and the publish gate need: the claims with their citation checks,
the step statistics and the synthesis (verdict and report). Documents are not included: citations keep their
page, lines, quote and whether the quote was found. Replaying imports the fixture into a database with fresh claim
ids and rewrites the report's [C#] citations to match, so CI can score a real run without Claude or PDFs.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import AgentStep, Citation, Claim, Company, Document, ResearchRun

FORMAT = 1
STEP_FIELDS = ("key", "stage", "role", "status", "tier", "model", "num_turns", "duration_s", "five_hour_before",
               "five_hour_after")  # fmt: skip
SYNTH_FIELDS = ("overall_verdict", "verdict_listing", "verdict_long_term", "confidence", "condition",
                "report_markdown")  # fmt: skip


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def export_run(session: Session, run_id: int) -> dict[str, Any]:
    run = session.get(ResearchRun, run_id)
    if run is None:
        raise LookupError(f"unknown run {run_id}")
    co = session.get(Company, run.company_id)
    titles = dict(session.execute(select(Document.id, Document.title)).all())
    claims = []
    for c in session.scalars(select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)):
        claims.append({"id": c.id, "stream": c.stream, "statement": c.statement, "claim_type": c.claim_type,
                       "metric": c.metric, "value": str(c.value) if c.value is not None else None, "unit": c.unit,
                       "period": c.period, "importance": c.importance, "status": c.status,
                       "verifier_note": c.verifier_note, "checks": c.checks or {},
                       "corrects_claim_id": c.corrects_claim_id,
                       "citations": [{"document_title": titles.get(x.document_id), "page_no": x.page_no,
                                      "line_start": x.line_start, "line_end": x.line_end, "quote": x.quote,
                                      "quote_found": x.quote_found, "url": x.url,
                                      "accessed_at": _dt(x.accessed_at)} for x in c.citations]})  # fmt: skip
    steps = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id).order_by(AgentStep.id)).all()
    synth = next((s for s in sorted(steps, key=lambda x: (x.finished_at or datetime.min.astimezone(), x.id),
                                    reverse=True) if s.stage == "synthesis" and s.status == "done"), None)  # fmt: skip
    return {
        "format": FORMAT,
        "company": {"slug": co.slug, "name": co.name, "nse_symbol": co.nse_symbol} if co else None,
        "run": {
            "id": run.id,
            "kind": run.kind,
            "status": run.status,
            "final_gate": (run.manifest or {}).get("final_gate"),
        },
        "steps": [{f: getattr(s, f) for f in STEP_FIELDS} for s in steps],
        "synthesis": {"step": synth.key, **{f: (synth.output or {}).get(f) for f in SYNTH_FIELDS}}
        if synth
        else None,
        "claims": claims,
    }


def import_run(session: Session, data: dict[str, Any], *, slug_suffix: str = "") -> int:
    """Load a fixture as a new run; returns the new run id. Claim ids are remapped everywhere."""
    if data.get("format") != FORMAT:
        raise ValueError(f"unsupported fixture format {data.get('format')!r}")
    from finresearch.ingest.documents import get_or_create_company

    c = data["company"]
    co = get_or_create_company(session, c["slug"] + slug_suffix, c["name"])
    co.nse_symbol = c.get("nse_symbol")
    run = ResearchRun(company_id=co.id, kind=data["run"]["kind"], status=data["run"]["status"],
                      manifest={"final_gate": data["run"].get("final_gate"), "replayed_from": data["run"]["id"]})  # fmt: skip
    session.add(run)
    session.flush()
    ids: dict[int, int] = {}
    for x in data["claims"]:
        claim = Claim(run_id=run.id, stream=x["stream"], statement=x["statement"], claim_type=x["claim_type"],
                      metric=x["metric"], value=Decimal(x["value"]) if x["value"] is not None else None,
                      unit=x["unit"], period=x["period"], importance=x["importance"], status=x["status"],
                      verifier_note=x["verifier_note"], checks=x["checks"])  # fmt: skip
        session.add(claim)
        session.flush()
        ids[x["id"]] = claim.id
        for ct in x["citations"]:
            session.add(Citation(claim_id=claim.id, page_no=ct["page_no"], line_start=ct["line_start"],
                                 line_end=ct["line_end"], quote=ct["quote"], quote_found=ct["quote_found"],
                                 url=ct["url"], accessed_at=datetime.fromisoformat(ct["accessed_at"])
                                 if ct["accessed_at"] else None))  # fmt: skip
    for x in data["claims"]:
        if x["corrects_claim_id"] in ids:
            session.get(Claim, ids[x["id"]]).corrects_claim_id = ids[x["corrects_claim_id"]]
    final = (data.get("synthesis") or {}).get("step")
    base = datetime.now().astimezone()
    for i, st in enumerate(data["steps"]):
        is_final = st["key"] == final
        out = remap_synthesis(data["synthesis"], ids) if is_final else {}
        # keep the original order; the final synthesis is the latest finished step, as in the live run
        finished = base + timedelta(seconds=len(data["steps"]) + 1 if is_final else i)
        session.add(AgentStep(run_id=run.id, output=out, finished_at=finished, **st))
    session.flush()
    return run.id


def remap_synthesis(synth: dict[str, Any], ids: dict[int, int]) -> dict[str, Any]:
    synth = {k: v for k, v in synth.items() if k != "step"}

    def sub(m: re.Match) -> str:
        old = int(m.group(1))
        return f"[C{ids.get(old, old + 10_000_000)}]"  # an unknown claim stays unknown after the remap

    return {**synth, "report_markdown": re.sub(r"\[C(\d+)\]", sub, synth.get("report_markdown") or "")}
