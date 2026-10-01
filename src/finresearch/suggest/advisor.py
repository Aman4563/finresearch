"""Personal suggestion: a read-only advisor agent, then deterministic enforcement of rules and lot limits."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select

from finresearch.agents.roles import CALC, DOC_READ, LEDGER_READ
from finresearch.agents.schemas import json_schema_for
from finresearch.bridge import AgentTask, Capability, ModelClass
from finresearch.config import get_settings
from finresearch.db import session_scope
from finresearch.db.models import AgentStep, Decision, InvestorProfile, ResearchRun
from finresearch.fincalc.dates import now_ist
from finresearch.suggest.profile import UI_FIELDS, Profile, default_profile, normalize_stored_rules
from finresearch.suggest.rules import Inputs, gather, is_sme, lot_limits

log = logging.getLogger(__name__)
ADVISOR_TOOLS = [*DOC_READ, *CALC, *LEDGER_READ]
PROFILE_NAME = "default"
MAX_REPORT_CHARS = 50_000


class Suggestion(BaseModel):
    action: Literal["APPLY", "APPLY-CONDITIONAL", "SKIP"]
    category: Literal["retail", "shni", "bhni"]
    lots: int = Field(ge=0)
    conditions: list[str] = Field(
        default_factory=list, description="Exact checks and deadlines before bidding"
    )
    exit_plan: str
    watch: list[str] = Field(default_factory=list, description="Dates and figures to watch after bidding")
    rationale_markdown: str = Field(description="Why, citing [C<claim_id>] or metric names")
    confidence: Literal["low", "medium", "high"]


# --------------------------------------------------------------------------- profile store
def load_profile(session) -> Profile:
    row = session.scalar(select(InvestorProfile).where(InvestorProfile.name == PROFILE_NAME))
    if row is None:
        return default_profile()
    data, warnings = normalize_stored_rules(row.data or {})
    for w in warnings:
        log.warning("stored profile: %s", w)
    return Profile.model_validate(data)


def save_profile(session, profile: Profile) -> Profile:
    from datetime import UTC

    row = session.scalar(select(InvestorProfile).where(InvestorProfile.name == PROFILE_NAME))
    data = profile.model_dump(mode="json")
    if row is None:
        session.add(InvestorProfile(name=PROFILE_NAME, data=data))
    else:
        row.data = data
        row.updated_at = datetime.now(UTC)
    return profile


# --------------------------------------------------------------------------- enforcement
def enforce(s: Suggestion, inputs: Inputs, limits: dict[str, int | None], profile: Profile) -> dict[str, Any]:
    """Apply the investor's rules and lot limits to the agent's suggestion; the result is what the app shows."""
    notes: list[str] = []
    action, lots, conditions = s.action, s.lots, list(s.conditions)
    if s.category != profile.category:
        notes.append(f"category set to the profile's {profile.category!r} (agent suggested {s.category!r})")
    fired = [r for r in inputs.rules if r.rule.action == "skip" and r.status == "fired"]
    unknown = [r for r in inputs.rules if r.rule.action == "skip" and r.status == "unknown"]
    warnings = [r for r in inputs.rules if r.rule.action == "warn" and r.status == "fired"]
    if fired:
        action, lots = "SKIP", 0
        notes += [
            f"rule {r.rule.id} fired: {r.rule.metric} = {r.value:.4g} {r.rule.op} {r.rule.value}"
            for r in fired
        ]
    elif unknown and action == "APPLY":
        action = "APPLY-CONDITIONAL"
        notes.append("downgraded to APPLY-CONDITIONAL: some skip rules cannot be checked yet")
    for r in unknown if not fired else []:
        cond = (
            f"Check {r.rule.metric} {r.rule.op} {r.rule.value} is NOT true before bidding (rule {r.rule.id})"
        )
        if cond not in conditions:
            conditions.append(cond)
    caps = [x for x in (limits.get("by_capital"), limits.get("by_category")) if x is not None]
    if action != "SKIP":
        cap = min(caps) if caps else None
        if cap is not None and lots > cap:
            notes.append(f"lots reduced from {lots} to {cap} (capital and category limits)")
            lots = cap
        min_lots = limits.get("min_lots")
        if min_lots and lots < min_lots:
            if cap is not None and cap >= min_lots:
                notes.append(f"lots raised to the category minimum of {min_lots}")
                lots = min_lots
            else:
                action, lots = "SKIP", 0
                notes.append("capital does not cover the category's minimum application")
        if lots == 0 and action != "SKIP":
            action = "SKIP"
            notes.append("zero lots after limits: SKIP")
    return {"action": action, "lots": lots, "category": profile.category, "conditions": conditions,
            "warnings": [f"rule {r.rule.id}: {r.rule.description or r.rule.metric}" for r in warnings],
            "enforcement_notes": notes, "agent": s.model_dump(mode="json")}  # fmt: skip


# --------------------------------------------------------------------------- suggestion flow
def _synthesis(session, run_id: int) -> dict[str, Any]:
    st = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                                 AgentStep.status == "done")
                         .order_by(AgentStep.finished_at.desc().nulls_last(), AgentStep.id.desc())).first()  # fmt: skip
    return (st.output or {}) if st else {}


async def fetch_live(symbol: str | None):
    if not symbol:
        return None
    from finresearch.adapters.nse import NseClient

    try:
        async with NseClient() as nse:
            return await nse.ipo_detail(symbol)
    except Exception:
        return None  # rules on live metrics become "unknown", which the enforcement turns into conditions


async def suggest(run_id: int, *, router=None, live_detail=None, fetch=fetch_live) -> dict[str, Any]:
    from finresearch.bridge import build_router
    from finresearch.mcp_server.config import write_mcp_config
    from finresearch.suggest.rules import company_of
    from finresearch.verify.gate import check_report

    now = now_ist()
    with session_scope() as s:
        run = s.get(ResearchRun, run_id)
        if run is None:
            raise LookupError(f"unknown run {run_id}")
        if run.kind != "ipo_report":  # lots, categories and the UPI cut-off only exist for an IPO
            raise ValueError(f"run {run_id} is a {run.kind}; personal suggestions are for IPO reports only")
        co = company_of(s, run_id)
        symbol, company_id = (co.nse_symbol if co else None), (co.id if co else None)
        synth = _synthesis(s, run_id)
        if not synth.get("report_markdown"):
            raise LookupError(f"run {run_id} has no finished report")
    if live_detail is None:
        live_detail = await fetch(symbol)
    with session_scope() as s:
        profile = load_profile(s)
        gate_ok = check_report(s, run_id, synth["report_markdown"]).ok
        s.rollback()
        inputs = gather(s, run_id, profile, live_detail=live_detail, now=now, gate_ok=gate_ok)
        run = s.get(ResearchRun, run_id)
        issue_info = ((run.manifest or {}).get("facts") or {}).get("issue_info") or {}
        limits = lot_limits(profile, inputs.metrics["lot_cost"].value, sme=is_sme(issue_info, live_detail))
        system = _prompt(run_id, co, now, profile, inputs, limits, synth)

    ws = get_settings().runs_dir / str(run_id) / "advisor"
    ws.mkdir(parents=True, exist_ok=True)
    task = AgentTask(name=f"run{run_id}-advisor", prompt="Write the personal suggestion now.", system_prompt=system,
                     json_schema=json_schema_for(Suggestion), model_class=ModelClass.DEEP, effort="high",
                     capabilities={Capability.TOOLS}, allowed_tools=ADVISOR_TOOLS, mcp_config=write_mcp_config(),
                     max_turns=30, timeout_s=900, run_dir=ws, allow_degraded=False)  # fmt: skip
    result = await (router or build_router()).run(task)
    agent = Suggestion.model_validate(result.structured_output)
    final = enforce(agent, inputs, limits, profile)
    final["model"], final["tier"] = result.model, str(result.tier)
    with session_scope() as s:
        d = Decision(run_id=run_id, company_id=company_id, action=final["action"], lots=final["lots"],
                     category=final["category"], suggestion=final,
                     inputs={"profile": profile.model_dump(mode="json", exclude=UI_FIELDS), "limits": limits, **inputs.to_json(),
                             "at": now.isoformat()})  # fmt: skip
        s.add(d)
        s.flush()
        return decision_json(d)


def _prompt(run_id, co, now, profile: Profile, inputs: Inputs, limits, synth) -> str:
    tmpl = resources.files("finresearch.agents.prompts").joinpath("advisor.md").read_text()
    report = synth.get("report_markdown", "")
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n\n[report truncated for length]"
    verdict = {k: synth.get(k) for k in ("overall_verdict", "verdict_listing", "verdict_long_term", "confidence",
                                          "condition")}  # fmt: skip
    return tmpl.format(company_name=co.name if co else "?", nse_symbol=(co.stock_key if co else None) or "n/a",
                       run_id=run_id, now_ist=now.strftime("%H:%M"), today=now.date().isoformat(),
                       profile=json.dumps(profile.model_dump(mode="json", exclude=UI_FIELDS), indent=1),
                       metrics=json.dumps(inputs.to_json()["metrics"], indent=1),
                       rules=json.dumps(inputs.to_json()["rules"], indent=1), limits=json.dumps(limits),
                       verdict=json.dumps(verdict), report=report)  # fmt: skip


# --------------------------------------------------------------------------- journal
def record_outcome(d: Decision) -> dict[str, Any]:
    """Returns computed with fincalc from what the investor entered."""
    from finresearch.fincalc.ipo import listing_gain

    out: dict[str, Any] = {}
    if d.issue_price and d.listing_price:
        out["listing_gain_pct"] = str(round(listing_gain(d.issue_price, d.listing_price) * 100, 2))
    if d.issue_price and d.exit_price:
        out["exit_return_pct"] = str(round(listing_gain(d.issue_price, d.exit_price) * 100, 2))
    lot = (d.inputs or {}).get("metrics", {}).get("lot_size", {}).get("value")
    if d.allotted_lots and lot and d.issue_price and d.exit_price:
        shares = d.allotted_lots * int(float(lot))
        out["profit_inr"] = str(round((d.exit_price - d.issue_price) * shares, 2))
    if d.user_action and d.action:
        out["followed_suggestion"] = (d.user_action == "applied") == (d.action != "SKIP")
    return out


def decision_json(d: Decision) -> dict[str, Any]:
    return {"id": d.id, "run_id": d.run_id, "company_id": d.company_id, "action": d.action, "lots": d.lots,
            "category": d.category, "suggestion": d.suggestion, "inputs": d.inputs, "user_action": d.user_action,
            "applied_lots": d.applied_lots, "allotted_lots": d.allotted_lots,
            "issue_price": str(d.issue_price) if d.issue_price is not None else None,
            "listing_price": str(d.listing_price) if d.listing_price is not None else None,
            "exit_price": str(d.exit_price) if d.exit_price is not None else None,
            "exit_date": d.exit_date.isoformat() if d.exit_date else None, "outcome": d.outcome or {},
            "notes": d.notes, "created_at": d.created_at.isoformat() if d.created_at else None}  # fmt: skip
