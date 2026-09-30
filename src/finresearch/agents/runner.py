"""Render a role into an AgentTask, prepare its sandboxed workspace, run it through the bridge, validate output."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from finresearch.agents.roles import ROLES, Role
from finresearch.agents.schemas import json_schema_for
from finresearch.bridge import AgentResult, AgentTask, BridgeRouter, Capability, build_router
from finresearch.config import get_settings
from finresearch.fincalc.dates import now_ist
from finresearch.mcp_server.config import write_mcp_config


class _Keep(dict):
    """format_map helper: unknown placeholders stay as-is instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class RunContext:
    run_id: int
    company_slug: str
    company_name: str
    nse_symbol: str | None = None
    bse_code: str | None = None  # a BSE-only listed stock (no NSE symbol): its BSE scrip code
    decision_deadline: str = "the issue closes (UPI mandate cut-off 5:00 PM IST on the last bidding day)"
    subject: str = "an Indian IPO"
    primary_source: str = "the offer documents (the RHP text layer)"
    documents: list[dict[str, Any]] = field(default_factory=list)  # from list_documents
    facts: dict[str, Any] = field(
        default_factory=dict
    )  # deterministic facts (issue info, dates, share maths)
    now: datetime | None = None

    def values(self) -> dict[str, str]:
        now = self.now or now_ist()
        today: date = now.date()
        return {
            "run_id": str(self.run_id),
            "company_name": self.company_name,
            "company_slug": self.company_slug,
            # a BSE-only stock reads "BSE:526433" wherever the prompts name the symbol: the equity tools accept it
            "nse_symbol": self.nse_symbol or (f"BSE:{self.bse_code}" if self.bse_code else "n/a"),
            "listing": self.listing(),
            "today": today.isoformat(),
            "now_ist": now.strftime("%Y-%m-%d %H:%M"),
            "news_from": (today - timedelta(days=30)).isoformat(),
            "decision_deadline": self.decision_deadline,
            "subject": self.subject,
            "primary_source": self.primary_source,
        }

    def listing(self) -> str:
        """Where the stock trades, as the prompts say it: "NSE INFY", or for a BSE-only stock its scrip code and how to
        read it with the equity tools."""
        if self.nse_symbol or not self.bse_code:
            return f"NSE {self.nse_symbol or 'n/a'}"
        return (f"BSE {self.bse_code}; BSE-only, not listed on NSE: pass \"BSE:{self.bse_code}\" as the symbol to the "
                "nse_* equity tools, which then read BSE")  # fmt: skip


class RoleOutputInvalid(Exception):
    pass


def render(role: Role, ctx: RunContext, **extra: str) -> tuple[str, str]:
    """Returns (system_prompt, prompt)."""
    vals = _Keep({**ctx.values(), "stream": role.name, "focus": "see the research plan below", **extra})
    house = resources.files("finresearch.agents.prompts").joinpath("_house_rules.md").read_text()
    system = house.format_map(vals) + "\n\n" + role.template_text().format_map(vals)
    if role.skills:
        system += "\n\nSkills available (load with the Skill tool when useful): " + ", ".join(role.skills)
    docs = (
        "\n".join(
            f"- document_id {d['document_id']}: {d['kind']} — {d['title']} ({d.get('pages')} pages, "
            f"{d.get('sections', 0)} sections)"
            for d in ctx.documents
        )
        or "- (none ingested yet — say so in open_questions)"
    )
    facts = json.dumps(ctx.facts, indent=1, default=str) if ctx.facts else "{}"
    prompt = (
        f"Company: {ctx.company_name} (slug {ctx.company_slug}, {ctx.listing()}). Run id: {ctx.run_id}.\n"
        f"Documents in the store:\n{docs}\n\nDeterministic facts already established:\n{facts}\n\n"
        f"Do the task described in your instructions, then return the structured result."
    )
    return system, prompt


def prepare_workspace(role: Role, ctx: RunContext) -> Path:
    """Per-role sandbox: data/runs/<run_id>/<role>/ with only this role's skills under .claude/skills."""
    ws = get_settings().runs_dir / str(ctx.run_id) / role.name
    skills_dst = ws / ".claude" / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    skills_dst.mkdir(parents=True)
    src = resources.files("finresearch.agents").joinpath("skills")
    for name in role.skills:
        s = Path(str(src.joinpath(name)))
        shutil.copytree(s, skills_dst / name)
    return ws


def build_task(role_name: str, ctx: RunContext, **extra: str) -> AgentTask:
    role = ROLES[role_name]
    system, prompt = render(role, ctx, **extra)
    return AgentTask(
        name=f"run{ctx.run_id}-{role.name}",
        prompt=prompt,
        system_prompt=system,
        json_schema=json_schema_for(role.output),
        model_class=role.model_class,
        effort=role.effort,
        capabilities={Capability.TOOLS} | ({Capability.WEB} if role.needs_web else set()),
        allowed_tools=role.tools,
        mcp_config=write_mcp_config(),
        max_turns=role.max_turns,
        timeout_s=role.timeout_s,
        run_dir=prepare_workspace(role, ctx),
        allow_degraded=False,  # research roles wait for Claude rather than degrade to local models
    )


async def run_role(
    role_name: str, ctx: RunContext, *, router: BridgeRouter | None = None, **extra: str
) -> tuple[BaseModel, AgentResult]:
    role = ROLES[role_name]
    task = build_task(role_name, ctx, **extra)
    result = await (router or build_router()).run(task)
    try:
        parsed = role.output.model_validate(result.structured_output)
    except ValidationError as e:
        raise RoleOutputInvalid(f"{role_name}: output failed validation: {e}") from e
    return parsed, result
