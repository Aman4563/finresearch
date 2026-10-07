"""Typed outputs for every research role. Claude returns them via --json-schema; Pydantic validates them again.

Claims themselves live in the ledger (saved through the MCP `save_claim` tool); role outputs reference them by id
so the synthesizer and renderer can trace every number back to a citation.
"""

from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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


class BlindFinding(BaseModel):
    """The independent second verifier's reading of one claim (#243). It is never told the claimed figure, the
    claim's status or the first verifier's reasoning, so `derived_value` is its own reading of the cited source."""

    claim_id: int
    derived_value: str | None = Field(
        default=None,
        description="The figure for this metric and period as YOU read or computed it from the cited "
        "source, digits only (e.g. '1234.56'); null if the claim has no figure or the source does not give one",
    )
    derived_unit: str | None = Field(
        default=None, description="Unit of derived_value as printed (e.g. 'INR million')"
    )
    supports_statement: Literal["yes", "no", "cannot_tell"] = Field(
        description="Does the cited source support the statement, figures masked as [N]?"
    )
    evidence: str = Field(description="Where you read it: document lines or URL, and what is printed there")


class BlindVerificationReport(BaseModel):
    findings: list[BlindFinding]
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


class StockScenario(BaseModel):
    name: Literal["bear", "base", "bull"]
    horizon: Literal["3m", "12m", "3y"]
    price_low: float | None = None
    price_high: float | None = None
    rationale: str
    likelihood: Literal["less likely", "most likely", "plausible"]


class StockSynthesis(BaseModel):
    # A research view, not a call (#241): stock calls are informational until a pre-registered model shows an edge
    # (signals.stock.CALLS_ENABLED). A stored output from before the switch (BUY…AVOID) still loads, mapped to its view.
    verdict: Literal["FAVOURABLE", "MIXED", "UNFAVOURABLE"] = Field(
        description="Research view: FAVOURABLE / MIXED / UNFAVOURABLE (informational, no validated edge; not a call)"
    )
    horizon: str = Field(description="Holding period the view is for, e.g. '3-5 years'")
    confidence: Literal["low", "medium", "high"]
    condition: str | None = Field(
        default=None, description="What would change the view, with the datum to watch"
    )
    entry_zone: str | None = Field(
        default=None,
        description="Price range the valuation discusses (fincalc-backed): context, never an instruction "
        "to buy or sell",
    )

    @field_validator("verdict", mode="before")
    @classmethod
    def _legacy_verdict(cls, v: object) -> object:
        legacy = {"BUY": "FAVOURABLE", "ACCUMULATE": "FAVOURABLE", "HOLD": "MIXED", "REDUCE": "UNFAVOURABLE",
                  "AVOID": "UNFAVOURABLE"}  # fmt: skip
        return legacy.get(v.strip().upper(), v.strip().upper()) if isinstance(v, str) else v

    executive_summary: str
    reasons_for: list[CasePoint]
    reasons_against: list[CasePoint]
    scenarios: list[StockScenario]
    action_checklist: list[str]
    report_markdown: str = Field(description="Full report; every figure cites [C<id>]")


class FundSynthesis(BaseModel):
    verdict: Literal["INVEST", "SIP ONLY", "HOLD", "SWITCH", "AVOID"]
    suits: str = Field(description="Who the scheme suits: horizon and risk appetite")
    confidence: Literal["low", "medium", "high"]
    condition: str | None = Field(
        default=None, description="What would change the verdict, with the datum to watch"
    )
    executive_summary: str
    reasons_for: list[CasePoint]
    reasons_against: list[CasePoint]
    alternatives: list[str] = Field(
        default_factory=list, description="Other schemes worth comparing, with reasons"
    )
    action_checklist: list[str]
    report_markdown: str = Field(description="Full report; every figure cites [C<id>]")


class BondSynthesis(BaseModel):
    verdict: Literal["BUY", "BUY BELOW PRICE", "HOLD", "AVOID"]
    price_or_yield: str | None = Field(default=None, description="Price or YTM at which the verdict applies")
    suits: str = Field(description="Who the bond suits: horizon, tax slab and risk appetite")
    confidence: Literal["low", "medium", "high"]
    condition: str | None = Field(
        default=None, description="What would change the verdict, with the datum to watch"
    )
    executive_summary: str
    reasons_for: list[CasePoint]
    reasons_against: list[CasePoint]
    action_checklist: list[str]
    report_markdown: str = Field(description="Full report; every figure cites [C<id>]")


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
