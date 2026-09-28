"""Score a research run against a gold set of verified key facts.

A gold fact is found when a usable claim (verified / unverified / needs_review) in the run's ledger matches the
fact's pattern and period and carries the fact's value within tolerance, after unit normalisation (₹ rupee scales,
%/fraction). A fact is contradicted by the report when the report cites a claim that matches the fact's pattern and
period but states a different value. Live market figures are deliberately not part of gold sets.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.config import REPO_ROOT
from finresearch.db.models import AgentStep, Claim, ResearchRun
from finresearch.verify.gate import rupee_scale

GOLD_DIR = REPO_ROOT / "evals" / "gold"
USABLE = ("verified", "unverified", "needs_review")
RELEASE_RECALL = 0.90
# claims about other contexts that share keywords with gold facts (draft offer, lower band, peers, dilution)
DEFAULT_EXCLUDE = (
    r"\bdrhp\b|lower band|floor price|\b258\b|\bpeer|polycab|\bkei\b|finolex|rr kabel|sterlite|"
    r"kissht|onemi|bajaj|sbi card|paytm|pb fintech|post[- ]dilution|diluted at|waca|"
    r"weighted average cost"
)

# a claim about a component of the gold metric (segment revenue, export revenue, ...) never contradicts the total
DERIVED_VARIANTS = (
    r"adjusted",
    r"normali[sz]",
    r"excluding",
    r"\bex[- ]",
    r"impact",
    r"footnote",
    r"\bbefore\b",
    r"\binterim\b",
    r"\bfinal\b",
    r"standalone",
    r"implied",
    r"\bttm\b",
    r"partial",
)
DEFAULT_COMPONENT = r"segment|export|domestic|geograph|product|region|channel|category"


def load_gold(company: str, gold_dir: Path | None = None) -> dict[str, Any]:
    path = (gold_dir or GOLD_DIR) / f"{company}.json"
    if not path.exists():
        raise FileNotFoundError(f"no gold set for {company!r} at {path}")
    return json.loads(path.read_text())


def _to_gold_unit(value: Decimal, unit: str | None, gold_unit: str) -> Decimal | None:
    g, u = gold_unit.lower(), (unit or "").lower()
    gs, us = rupee_scale(gold_unit), rupee_scale(unit)
    if gs is not None and gs != 1:  # rupee amounts (million, crore, ...)
        return value * us / gs if us is not None else None
    if "%" in g:
        if "fraction" in u or "ratio" in u:
            return value * 100
        return value if ("%" in u or "percent" in u or not u) else None
    if g == "shares":
        return value if (not u or "share" in u or u in {"count", "number", "nos"}) else None
    if g == "inr":  # per-share rupee values
        return value if (us == 1 or not u or "per share" in u) else None
    return value


def _within(a: Decimal, b: Decimal, tol: float) -> bool:
    if b == 0:
        return abs(a) <= Decimal(str(tol))
    return abs(a - b) / abs(b) <= Decimal(str(tol))


@dataclass
class FactResult:
    id: str
    label: str
    importance: str
    found: bool = False
    verified: bool = False
    claim_ids: list[int] = field(default_factory=list)
    contradicted_in_report: list[int] = field(default_factory=list)


@dataclass
class EvalResult:
    company: str
    run_id: int
    facts: list[FactResult]
    verdict_expected: dict[str, Any]
    verdict_actual: dict[str, Any]
    stats: dict[str, Any]

    @property
    def recall(self) -> float:
        return sum(f.found for f in self.facts) / len(self.facts) if self.facts else 0.0

    @property
    def verified_recall(self) -> float:
        return sum(f.verified for f in self.facts) / len(self.facts) if self.facts else 0.0

    @property
    def high_recall(self) -> float:
        hi = [f for f in self.facts if f.importance == "high"]
        return sum(f.found for f in hi) / len(hi) if hi else 0.0

    @property
    def contradicted(self) -> list[FactResult]:
        return [f for f in self.facts if f.contradicted_in_report]

    @property
    def verdict_agrees(self) -> bool | None:
        a, e = self.verdict_actual, self.verdict_expected
        if not e:  # no reference verdict (stock gold sets are facts only)
            return None
        if "any_of" in e:  # stock reports: one verdict out of an acceptable set
            return (a.get("overall") or "").upper() in {v.upper() for v in e["any_of"]}
        listing_ok = a.get("overall", "").startswith(e["listing"]) or a.get("listing", "").upper().startswith(
            e["listing"]
        )
        long_ok = any(x in (a.get("long_term") or "").upper() for x in e["long_term"])
        return listing_ok and long_ok

    @property
    def passes_release_bar(self) -> bool:
        return self.recall >= RELEASE_RECALL and not self.contradicted

    def markdown(self) -> str:
        s = self.stats
        lines = [
            f"### Gold-set evaluation: {self.company}, run {self.run_id}",
            "",
            "| metric | value |",
            "|---|---|",
            f"| key-fact recall | **{self.recall:.0%}** ({sum(f.found for f in self.facts)}/{len(self.facts)}) |",
            f"| verified recall | {self.verified_recall:.0%} |",
            f"| high-importance recall | {self.high_recall:.0%} |",
            f"| gold facts contradicted by the report | **{len(self.contradicted)}** |",
            f"| verdict agrees with the manual report | "
            f"{'n/a (no reference verdict)' if self.verdict_agrees is None else 'yes' if self.verdict_agrees else 'no'} "
            f"({self.verdict_actual.get('overall')}) |",
            f"| citation validity (cited claims with a found quote or URL) | {s['citation_validity']:.0%} |",
            f"| claims: total / verified / contradicted / unsupported | {s['claims_total']} / {s['claims_verified']} / "
            f"{s['claims_contradicted']} / {s['claims_unsupported']} |",
            f"| publish gate | {'passed' if s.get('gate_ok') else 'blocked'} |",
            f"| steps / turns / time | {s['steps']} / {s['turns']} / {s['minutes']:.0f} min |",
            f"| Max 5-hour window used (sum of step deltas) | {s['five_hour_used']:.0%} |",
            f"| release bar (≥{RELEASE_RECALL:.0%} recall, 0 contradicted) | {'PASS' if self.passes_release_bar else 'FAIL'} |",
            "",
            "| fact | importance | found | verified | claims | contradicted in report |",
            "|---|---|---|---|---|---|",
        ]
        for f in self.facts:
            lines.append(f"| {f.label} | {f.importance} | {'✓' if f.found else '✗'} | {'✓' if f.verified else ''} | "
                         f"{', '.join(f'C{i}' for i in f.claim_ids[:4])} | "
                         f"{', '.join(f'C{i}' for i in f.contradicted_in_report)} |")  # fmt: skip
        return "\n".join(lines) + "\n"


def evaluate(
    session: Session, run_id: int, gold: dict[str, Any], report_markdown: str | None = None
) -> EvalResult:
    claims = session.scalars(select(Claim).where(Claim.run_id == run_id)).all()
    cited = {int(x) for x in re.findall(r"\[C(\d+)\]", report_markdown or "")}
    results = []
    for fact in gold["facts"]:
        pat = re.compile("|".join(fact["patterns"]), re.I)
        per = re.compile(fact["period"], re.I) if fact.get("period") else None
        gv = Decimal(str(fact["value"]))
        fr = FactResult(fact["id"], fact["label"], fact.get("importance", "normal"))
        excl = re.compile(fact.get("exclude") or DEFAULT_EXCLUDE, re.I)
        # a fact can exclude claims by their period field alone (quarterly figures vs an annual fact), without
        # dropping annual claims whose statement merely mentions a quarter
        per_excl = re.compile(fact["period_exclude"], re.I) if fact.get("period_exclude") else None
        # an exclusion is lifted when the claim also names this context (e.g. "consolidated ... (standalone ₹1,146 cr)")
        keep = re.compile(fact["exclude_unless"], re.I) if fact.get("exclude_unless") else None
        component = re.compile(fact.get("component") or DEFAULT_COMPONENT, re.I)
        # derived variants (adjusted, normalised, before-exceptional, interim/final parts) never contradict the
        # reported figure, unless the fact's own pattern names that variant
        pattern_text = " ".join(fact["patterns"])
        variants = [v for v in DERIVED_VARIANTS if not re.search(v, pattern_text, re.I)]
        derived = re.compile("|".join(variants), re.I) if variants else None
        for c in claims:
            metric = (c.metric or "").replace("_", " ")
            text = f"{metric} {c.statement}"
            context = f"{metric} {c.period or ''}"
            if c.value is None or not pat.search(text):
                continue
            # an exclusion in the claim's own metric or period always applies; one found only in the statement is
            # lifted when the statement also names the wanted context (live INFY run 9: a "standalone_net_profit"
            # claim whose statement said "consolidated" for contrast)
            if excl.search(context) or (excl.search(c.statement) and not (keep and keep.search(c.statement))):
                continue
            if per_excl and per_excl.search(c.period or ""):
                continue
            if per and not per.search(f"{c.period or ''} {c.statement}"):
                continue
            v = _to_gold_unit(Decimal(c.value), c.unit, fact["unit"])
            if v is None:
                continue
            tol = fact.get("tolerance", 0.005)
            match = _within(v, gv, tol) or (fact["unit"] != "%" and _within(-v, gv, tol))
            if match and c.status in USABLE:
                fr.found = True
                fr.verified = fr.verified or c.status == "verified"
                fr.claim_ids.append(c.id)
            elif (
                not match
                and c.id in cited
                and c.status in USABLE
                and pat.search(metric)
                and not component.search(metric)
                and not (derived and derived.search(metric))
                and (per is None or per.search(c.period or ""))
            ):
                # a contradiction needs the claim's own metric and period fields to name the gold fact
                fr.contradicted_in_report.append(c.id)
        results.append(fr)

    # run statistics
    cited_rows = session.scalars(select(Claim).where(Claim.id.in_(cited))).all() if cited else []
    valid = sum(1 for c in cited_rows if any(x.quote_found or x.url for x in c.citations))
    counts = dict(session.execute(select(Claim.status, func.count()).where(Claim.run_id == run_id)
                                  .group_by(Claim.status)).all())  # fmt: skip
    steps = session.scalars(
        select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.status == "done")
    ).all()
    used = sum(max(0.0, (s.five_hour_after or 0) - (s.five_hour_before or 0)) for s in steps
               if s.five_hour_after is not None and s.five_hour_before is not None)  # fmt: skip
    run = session.get(ResearchRun, run_id)
    synth = next((s.output for s in sorted(steps, key=lambda x: (x.finished_at or x.id, x.id), reverse=True)
                  if s.stage == "synthesis"), {})  # fmt: skip
    stats = {
        "citation_validity": valid / len(cited_rows) if cited_rows else 0.0,
        "claims_total": sum(counts.values()),
        "claims_verified": counts.get("verified", 0),
        "claims_contradicted": counts.get("contradicted", 0),
        "claims_unsupported": counts.get("unsupported", 0),
        "gate_ok": (run.manifest or {}).get("final_gate", {}).get("ok"),
        "steps": len(steps),
        "turns": sum(s.num_turns or 0 for s in steps),
        "minutes": sum(s.duration_s or 0 for s in steps) / 60,
        "five_hour_used": used,
    }
    actual = {"overall": synth.get("overall_verdict") or synth.get("verdict", ""),
              "listing": synth.get("verdict_listing", ""),
              "long_term": synth.get("verdict_long_term", "")}  # fmt: skip
    return EvalResult(gold["company"], run_id, results, gold.get("verdict") or {}, actual, stats)
