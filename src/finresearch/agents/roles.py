"""Research roles: model tier, effort, tool allow-list, output schema and prompt template for each agent.

Tool policy (least privilege): every role reads documents and uses fincalc through the FinResearch MCP server; only
roles whose job needs the open web get WebSearch/WebFetch; only research streams may write claims; bull/bear,
synthesizer and critic read the ledger but never write to it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from importlib import resources

from pydantic import BaseModel

from finresearch.agents.schemas import (
    CaseReport,
    CriticReport,
    DiscoveryResult,
    ResearchPlan,
    StreamReport,
    Synthesis,
    VerificationReport,
)
from finresearch.bridge.types import ModelClass

MCP = "mcp__finresearch__"
DOC_READ = [f"{MCP}{t}" for t in ("list_documents", "list_sections", "read_lines_tool", "read_section",
                                   "grep_document", "search_documents", "extract_table")]  # fmt: skip
CALC = [f"{MCP}fincalc_functions", f"{MCP}fincalc_call"]
LEDGER_WRITE = [f"{MCP}save_claim", f"{MCP}list_claims"]
LEDGER_READ = [f"{MCP}list_claims"]
MARKET = [f"{MCP}{t}" for t in ("nse_ipo_detail", "nse_current_issues", "sebi_filings", "sebi_resolve_pdf")]
WEB = ["WebSearch", "WebFetch"]
SKILL = ["Skill"]

STREAMS = ("financials", "business", "risks", "valuation", "news30", "demand", "major")


@dataclass(frozen=True)
class Role:
    name: str
    template: str  # file in agents/prompts
    output: type[BaseModel]
    model_class: ModelClass
    tools: list[str]
    effort: str = "medium"
    max_turns: int = 60
    timeout_s: float = 2400
    needs_web: bool = False
    writes_claims: bool = False
    skills: list[str] = field(
        default_factory=lambda: ["ipo-deep-research", "rhp-navigator", "indian-fin-glossary"]
    )

    def template_text(self) -> str:
        return resources.files("finresearch.agents.prompts").joinpath(self.template).read_text()

    def prompt_hash(self) -> str:
        house = resources.files("finresearch.agents.prompts").joinpath("_house_rules.md").read_text()
        return hashlib.sha256((house + self.template_text()).encode()).hexdigest()[:12]


def _stream(name: str, *, web: bool, market: bool = False, docs: bool = True, turns: int = 80) -> Role:
    tools = [*(DOC_READ if docs else [f"{MCP}search_documents", f"{MCP}read_lines_tool"]), *CALC, *LEDGER_WRITE,
             *(MARKET if market else []), *(WEB if web else []), *SKILL]  # fmt: skip
    return Role(name=name, template=f"stream_{name}.md", output=StreamReport, model_class=ModelClass.STANDARD,
                tools=tools, effort="high" if name == "financials" else "medium", max_turns=turns,
                timeout_s=3600, needs_web=web, writes_claims=True)  # fmt: skip


ROLES: dict[str, Role] = {
    "planner": Role("planner", "planner.md", ResearchPlan, ModelClass.DEEP, [*DOC_READ, *MARKET, *CALC, *SKILL],
                    effort="high", max_turns=30, timeout_s=1200),
    "financials": _stream("financials", web=False, turns=100),
    "business": _stream("business", web=True),
    "risks": _stream("risks", web=True),
    "valuation": _stream("valuation", web=True, market=True),
    "news30": _stream("news30", web=True, docs=False),
    "demand": _stream("demand", web=True, market=True),
    "major": _stream("major", web=True),
    "verifier": Role("verifier", "verifier.md", VerificationReport, ModelClass.DEEP,
                     [*DOC_READ, *CALC, *LEDGER_READ, *WEB], effort="high", max_turns=60, timeout_s=2400,
                     needs_web=True, skills=["indian-fin-glossary", "rhp-navigator"]),
    "bull": Role("bull", "case_bull.md", CaseReport, ModelClass.DEEP, [*LEDGER_READ, *DOC_READ],
                 effort="medium", max_turns=25, timeout_s=1200, skills=["indian-fin-glossary"]),
    "bear": Role("bear", "case_bear.md", CaseReport, ModelClass.DEEP, [*LEDGER_READ, *DOC_READ],
                 effort="medium", max_turns=25, timeout_s=1200, skills=["indian-fin-glossary"]),
    "synthesizer": Role("synthesizer", "synthesizer.md", Synthesis, ModelClass.DEEP,
                        [*LEDGER_READ, *DOC_READ, *CALC, *SKILL], effort="high", max_turns=60, timeout_s=3000,
                        skills=["report-writer", "indian-fin-glossary"]),
    "discovery": Role("discovery", "discovery.md", DiscoveryResult, ModelClass.STANDARD,
                      [f"{MCP}list_documents", *WEB], effort="medium", max_turns=40, timeout_s=1500, needs_web=True,
                      skills=[]),
    "critic": Role("critic", "critic.md", CriticReport, ModelClass.DEEP, [*LEDGER_READ, *DOC_READ],
                   effort="medium", max_turns=25, timeout_s=1200, skills=["ipo-deep-research"]),
}  # fmt: skip
