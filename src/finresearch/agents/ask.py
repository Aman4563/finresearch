"""Ask Claude about a finished report: a read-only agent grounded in the run's claim ledger and documents.

Each conversation is one Claude Code session, continued with --resume, working in its own directory under the
run. Every answer is checked deterministically before it is stored: cited claims must exist in the run and be
usable, cited document lines must exist, and figures without a citation are flagged.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from finresearch.agents.roles import CALC, DOC_READ, LEDGER_READ
from finresearch.agents.schemas import json_schema_for
from finresearch.bridge import AgentTask, Capability, ModelClass
from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import (
    AgentStep,
    Claim,
    Company,
    Conversation,
    ConversationMessage,
    Document,
    ResearchRun,
)
from finresearch.fincalc.dates import now_ist

ASK_TOOLS = [*DOC_READ, *CALC, *LEDGER_READ]
CLAIM_CITE = re.compile(r"\[C(\d+)\]")
LINE_CITE = re.compile(r"\[D(\d+):L(\d+)(?:-(\d+))?\]")
FIGURE = re.compile(r"₹\s?\d|\d[\d,]*\.\d+|\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?x\b|\b\d{1,3}(?:,\d{2,3})+\b")
USABLE = ("verified", "unverified", "needs_review")
MAX_REPORT_CHARS = 60_000


class AskAnswer(BaseModel):
    answer_markdown: str = Field(description="The answer, citing [C<claim_id>] or [D<document_id>:L<a>-<b>]")
    cited_claims: list[int] = Field(default_factory=list, description="Claim ids cited in the answer")
    needs_new_research: bool = Field(
        default=False, description="True when the question cannot be answered from the ledger and documents"
    )


def check_answer(session, run_id: int, text: str) -> dict[str, Any]:
    """Deterministic checks on an answer (never trusts the model's own list of citations)."""
    from finresearch.verify.gate import loose_cite_ids

    canonical = {int(x) for x in CLAIM_CITE.findall(text)}
    # "(C12)" / bare "C123" are shown as claim links too (api.insights.norm_cites): check those that are this run's
    loose = loose_cite_ids(text) - canonical
    if loose:
        loose = set(session.scalars(select(Claim.id).where(Claim.id.in_(loose), Claim.run_id == run_id)))
    ids = sorted(canonical | loose)
    claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.id.in_(ids)))} if ids else {}
    unknown = [i for i in ids if i not in claims or claims[i].run_id != run_id]
    unusable = [
        i for i in ids if i in claims and claims[i].run_id == run_id and claims[i].status not in USABLE
    ]
    bad_lines = []
    for doc_id, a, b in LINE_CITE.findall(text):
        d = session.get(Document, int(doc_id))
        first, last = int(a), int(b or a)
        n = _line_count(d.text_path) if d and d.text_path else 0
        if not d or first < 1 or last < first or last > n:
            bad_lines.append(f"D{doc_id}:L{a}-{b or a}")
    uncited = [sentence[:120] for sentence in _sentences(text) if _has_uncited_figure(sentence)]
    return {
        "cited_claims": ids,
        "unknown_claims": unknown,
        "unusable_claims": unusable,
        "bad_line_citations": bad_lines,
        "uncited_figure_lines": uncited[:20],
        "ok": not (unknown or unusable or bad_lines or uncited),
    }


ANY_CITE = re.compile(r"\[C\d+\]|\[D\d+:L\d+(?:-\d+)?\]")


def _sentences(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", "|---", ">")):
            continue
        out += [x.strip() for x in re.split(r"(?<=[.;!?])\s+", line) if x.strip()]
    return out


def _has_uncited_figure(sentence: str) -> bool:
    """A figure is cited when a citation follows it in the same sentence; text after the last citation is not."""
    tail = ANY_CITE.split(sentence)[-1]
    return bool(FIGURE.search(tail))


def _line_count(path: str) -> int:
    from finresearch.ingest.text import read_lines

    p = Path(path)
    return len(read_lines(p)) if p.exists() else 0


def _latest_report(session, run_id: int) -> str:
    st = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                                 AgentStep.status == "done")
                         .order_by(AgentStep.finished_at.desc().nulls_last(), AgentStep.id.desc())).first()  # fmt: skip
    return ((st.output or {}).get("report_markdown") if st else None) or "(no report yet)"


def system_prompt(session, run_id: int) -> str:
    run = session.get(ResearchRun, run_id)
    co = session.get(Company, run.company_id) if run and run.company_id else None
    now = now_ist()
    report = _latest_report(session, run_id)
    if len(report) > MAX_REPORT_CHARS:
        report = (
            report[:MAX_REPORT_CHARS] + "\n\n[report truncated here for length; read the ledger for the rest]"
        )
    tmpl = resources.files("finresearch.agents.prompts").joinpath("ask.md").read_text()
    return tmpl.format(company_name=co.name if co else "?", nse_symbol=(co.stock_key if co else None) or "n/a",
                       run_id=run_id, today=now.date().isoformat(), now_ist=now.strftime("%H:%M"), report=report)  # fmt: skip


def workspace(run_id: int, conversation_id: int) -> Path:
    ws = get_settings().runs_dir / str(run_id) / "ask" / str(conversation_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def ask(
    run_id: int, question: str, *, conversation_id: int | None = None, router=None
) -> dict[str, Any]:
    """Answer a question about a run; returns the stored user and assistant messages."""
    from finresearch.bridge import build_router
    from finresearch.mcp_server.config import write_mcp_config

    question = question.strip()
    if not question:
        raise ValueError("empty question")
    with session_scope() as s:
        if s.get(ResearchRun, run_id) is None:
            raise LookupError(f"unknown run {run_id}")
        if conversation_id is None:
            conv = Conversation(run_id=run_id, title=question[:200])
            s.add(conv)
            s.flush()
        else:
            conv = s.get(Conversation, conversation_id)
            if conv is None or conv.run_id != run_id:
                raise LookupError(f"unknown conversation {conversation_id} for run {run_id}")
        conv_id, session_id = conv.id, conv.session_id
        s.add(ConversationMessage(conversation_id=conv_id, role="user", content=question))
        system = system_prompt(s, run_id)

    task = AgentTask(name=f"run{run_id}-ask{conv_id}", prompt=question, system_prompt=system,
                     json_schema=json_schema_for(AskAnswer), model_class=ModelClass.STANDARD, effort="medium",
                     capabilities={Capability.TOOLS}, allowed_tools=ASK_TOOLS, mcp_config=write_mcp_config(),
                     max_turns=30, timeout_s=600, run_dir=workspace(run_id, conv_id),
                     resume_session_id=session_id, allow_degraded=False)  # fmt: skip
    result = await (router or build_router()).run(task)
    answer = AskAnswer.model_validate(result.structured_output)

    with session_scope() as s:
        checks = check_answer(s, run_id, answer.answer_markdown)
        checks["needs_new_research"] = answer.needs_new_research
        msg = ConversationMessage(conversation_id=conv_id, role="assistant", content=answer.answer_markdown,
                                  checks=checks, tier=str(result.tier), model=result.model,
                                  num_turns=result.num_turns, duration_s=result.duration_s)  # fmt: skip
        s.add(msg)
        conv = s.get(Conversation, conv_id)
        conv.session_id = result.session_id or conv.session_id
        conv.updated_at = datetime.now(UTC)
        s.flush()
        return {"conversation_id": conv_id, "message": message_json(msg)}


def message_json(m: ConversationMessage) -> dict[str, Any]:
    return {"id": m.id, "role": m.role, "content": m.content, "checks": m.checks or {}, "tier": m.tier,
            "model": m.model, "num_turns": m.num_turns, "duration_s": m.duration_s,
            "created_at": m.created_at.isoformat() if m.created_at else None}  # fmt: skip
