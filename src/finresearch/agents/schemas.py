"""Typed outputs for every research role. Claude returns them via --json-schema; Pydantic validates them again.

Claims themselves live in the ledger (saved through the MCP `save_claim` tool); role outputs reference them by id
so the synthesizer and renderer can trace every number back to a citation.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field

Impact = Literal["positive", "negative", "neutral", "mixed"]


class Finding(BaseModel):
    finding: str
    impact: Impact
    claim_ids: list[int] = Field(
        default_factory=list, description="Ledger claim ids that support this finding"
    )


class StreamReport(BaseModel):
    """Output of one research stream."""

    summary: str = Field(description="8-15 sentence summary of what the stream established")
    section_markdown: str = Field(description="Report section in markdown; cite claims inline as [C<id>]")
    key_findings: list[Finding]
    red_flags: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, description="What could not be verified and why")
    claim_ids: list[int] = Field(default_factory=list, description="All claims this stream saved")


class StreamFocus(BaseModel):
    stream: str
    questions: list[str] = Field(description="Specific questions this stream must answer")
    leads: list[str] = Field(
        default_factory=list, description="Pointers: sections, lines, facts worth checking"
    )


class ResearchPlan(BaseModel):
    """Planner output: what each stream should dig into for THIS company."""

    company_one_liner: str
    critical_questions: list[str] = Field(description="The questions that decide apply / avoid")
    streams: list[StreamFocus]


class ClaimVerdict(BaseModel):
    claim_id: int
    verdict: Literal["verified", "contradicted", "needs_review"]
    correct_value: str | None = None
    evidence: str = Field(description="What the verifier checked, with document lines or URL + timestamp")


class VerificationReport(BaseModel):
    verdicts: list[ClaimVerdict]
    cross_stream_conflicts: list[str] = Field(default_factory=list)
    summary: str


class CasePoint(BaseModel):
    point: str
    weight: Literal["high", "medium", "low"]
    claim_ids: list[int] = Field(default_factory=list)


class CaseReport(BaseModel):
    """Bull or bear case, argued only from verified claims."""

    thesis: str
    points: list[CasePoint]
    listing_view: str
    long_term_view: str
    strongest_counterargument: str


class Scenario(BaseModel):
    name: Literal["bear", "base", "bull"]
    horizon: Literal["listing", "12m"]
    price_low: float | None = None
    price_high: float | None = None
    rationale: str
    likelihood: Literal["less likely", "most likely", "plausible"]


class Synthesis(BaseModel):
    verdict_listing: str
    verdict_long_term: str
    overall_verdict: Literal[
        "APPLY", "APPLY (listing gains only)", "APPLY (long term)", "APPLY-CONDITIONAL", "AVOID", "NEUTRAL"
    ]
    confidence: Literal["low", "medium", "high"]
    condition: str | None = Field(
        default=None, description="For APPLY-CONDITIONAL: the exact check and deadline"
    )
    executive_summary: str
    reasons_for: list[CasePoint]
    reasons_against: list[CasePoint]
    scenarios: list[Scenario]
    action_checklist: list[str]
    report_markdown: str = Field(description="Full 12-section report; every figure cites [C<id>]")


class Gap(BaseModel):
    description: str
    stream: str = Field(description="Which stream should fill it")
    task: str = Field(description="Concrete follow-up instruction for that stream")
    severity: Literal["high", "medium", "low"]


class CriticReport(BaseModel):
    gaps: list[Gap]
    stale_or_unverified: list[str] = Field(default_factory=list)
    ready_to_publish: bool


DocKindName = Literal[
    "RHP",
    "DRHP",
    "ADDENDUM",
    "ABRIDGED_PROSPECTUS",
    "PRICE_BAND_AD",
    "ANCHOR_ALLOCATION",
    "ANNUAL_REPORT",
    "FINANCIAL_STATEMENTS",
    "INDUSTRY_REPORT",
    "OTHER",
]


class DiscoveredDocument(BaseModel):
    url: str = Field(description="Direct link to the PDF (or ZIP of PDFs), exactly as seen on found_on")
    kind: DocKindName
    title: str
    fiscal_year: str | None = None
    found_on: str = Field(description="The page URL where this link was seen")


class DiscoveryResult(BaseModel):
    ir_pages: list[str] = Field(description="Investor-relations / IPO-document pages visited")
    documents: list[DiscoveredDocument]
    notes: list[str] = Field(default_factory=list, description="What could not be found and where you looked")


def json_schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic JSON schema with $refs inlined (the CLI's --json-schema validator and models do best with a
    flat, self-contained schema)."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: inline(v) for k, v in node.items()}
        if isinstance(node, list):
            return [inline(v) for v in node]
        return node

    return inline(schema)
