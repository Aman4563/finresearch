"""Deterministic verification gate: checks that need no LLM, run on every claim before and after the verifiers.

Checks (results stored in claim.checks; a failing check never silently passes a claim):
* value_in_source  — a numeric claim's value (or its unit conversion: ₹ rupees/lakh/million/crore, % vs fraction)
                     appears among the numbers at its cited lines. Otherwise the figure is "derived" and must be
                     confirmed by a verifier (status needs_review if it was merely unverified).
* conflicts        — the same metric + period stated with different values by different claims (after unit
                     normalisation, >0.5% apart) -> both needs_review with a cross-reference, except a
                     deterministic exchange/AMFI fact (checks.source), which keeps its status.
* live_timestamp   — live market figures (subscription, GMP, prices) must carry a time (HH:MM) or an INTERIM label
                     while bidding is open, and web sources older than LIVE_MAX_AGE are stale.
* day_label        — "Day N" next to a date must match the real bidding calendar (weekends/holidays excluded);
                     a mismatch is a deterministic contradiction with the correct day recorded.

The publish gate (check_report) inspects a synthesized report: it may only cite usable claims, must not cite raw
document lines, and high-importance claims it relies on must be verified.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import Citation, Claim, Document
from finresearch.fincalc.dates import bidding_day_number, to_ist
from finresearch.ingest.text import read_lines

LINE_TOLERANCE = 2
REL_TOL = Decimal("0.0005")  # 0.05% — printed figures are rounded to 2 dp
CONFLICT_TOL = Decimal("0.005")  # 0.5% apart = genuine conflict
LIVE_RE = re.compile(r"subscri|\bgmp\b|grey[- ]market|kostak|sauda|times subscribed|share price|market price|\bcmp\b",
                     re.I)  # fmt: skip
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b|interim", re.I)
LIVE_MAX_AGE = timedelta(hours=24)
_LIVE_FIGURE_RE = re.compile(
    r"\d+(?:\.\d+)?\s?x\b|₹\s?\d|rs\.?\s?\d|\d+(?:\.\d+)?\s?%", re.I
)  # a live number is present
_NUM_RE = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?")
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}  # fmt: skip
_DATE_RE = re.compile(r"\b(\d{1,2})[-\s](jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[-\s,]*(\d{4})\b"
                      r"|\b(\d{4})-(\d{2})-(\d{2})\b", re.I)  # fmt: skip
_DAY_RE = re.compile(r"\bday[\s-]*(\d)\b", re.I)

# ₹ scale of each unit keyword, in rupees
_SCALE = [("crore", Decimal(10) ** 7), (" cr", Decimal(10) ** 7), ("million", Decimal(10) ** 6),
          (" mn", Decimal(10) ** 6), ("lakh", Decimal(10) ** 5), ("billion", Decimal(10) ** 9),
          ("thousand", Decimal(1000))]  # fmt: skip


# --------------------------------------------------------------------------- helpers
def _dec(tok: str) -> Decimal | None:
    t = tok.strip()
    neg = t.startswith("(") and t.endswith(")")
    try:
        v = Decimal(t.strip("()").replace(",", ""))
    except InvalidOperation:
        return None
    return -v if neg else v


def numbers_in(text: str) -> list[Decimal]:
    return [v for v in (_dec(m.group(0)) for m in _NUM_RE.finditer(text)) if v is not None]


FOREIGN_CURRENCY = ("usd", "us$", "$", "eur", "€", "gbp", "£", "jpy", "¥", "dollar", "euro")


def rupee_scale(unit: str | None) -> Decimal | None:
    u = f" {(unit or '').lower()}"
    if any(k in u for k in FOREIGN_CURRENCY):  # live INFY run 8: "USD million" was read as ₹ million
        return None
    if not any(
        k in u for k in ("inr", "rs", "₹", "rupee", "crore", "lakh", "million", " mn", " cr", "billion")
    ):
        return None
    for key, scale in _SCALE:
        if key in u:
            return scale
    return Decimal(1)


def candidate_forms(value: Decimal, unit: str | None) -> list[Decimal]:
    """The value as it might be printed in the source: same unit, other ₹ scales, or %/fraction."""
    forms = {value, abs(value)}
    u = (unit or "").lower()
    scale = rupee_scale(unit)
    if scale is not None:
        rupees = value * scale
        for _, s in _SCALE:
            forms.add(rupees / s)
        forms.add(rupees)
    if "%" in u or "percent" in u:
        forms.update({value / 100, value * 100})
    if u.strip() in {"fraction", "ratio"}:
        forms.add(value * 100)
    return [f for f in forms if f != 0] or [value]


def _matches(a: Decimal, b: Decimal) -> bool:
    if a == b:
        return True
    denom = max(abs(a), abs(b))
    return denom != 0 and abs(a - b) / denom <= REL_TOL


def cited_window(session: Session, cit: Citation, cache: dict[int, list[str]]) -> str:
    if cit.document_id is None or cit.line_start is None:
        return cit.quote or ""
    if cit.document_id not in cache:
        doc = session.get(Document, cit.document_id)
        cache[cit.document_id] = read_lines(doc.text_path) if doc and doc.text_path else []
    lines = cache[cit.document_id]
    a = max(1, cit.line_start - LINE_TOLERANCE)
    b = min(len(lines), (cit.line_end or cit.line_start) + LINE_TOLERANCE)
    return "\n".join(lines[a - 1 : b])


def _norm_key(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse_date(m: re.Match) -> date | None:
    try:
        if m.group(4):
            return date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        return date(int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1)))
    except (ValueError, KeyError):
        return None


# --------------------------------------------------------------------------- per-claim checks
@dataclass
class GateResult:
    checked: int = 0
    derived: list[int] = field(default_factory=list)
    conflicts: list[tuple[int, int]] = field(default_factory=list)
    live_flags: list[int] = field(default_factory=list)
    day_label_errors: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {"checked": self.checked, "derived": self.derived, "conflicts": self.conflicts,
                "live_flags": self.live_flags, "day_label_errors": self.day_label_errors}  # fmt: skip


# claims recorded by the pipeline from primary exchange/AMFI data, not by an agent (checks["source"])
DETERMINISTIC_SOURCES = {"nse_issue_info", "bse_issue_info", "nse_equity", "amfi", "nse_bonds"}


def is_deterministic(c: Claim) -> bool:
    return (c.checks or {}).get("source") in DETERMINISTIC_SOURCES


def _conflict_base(value: Decimal, unit: str | None) -> Decimal:
    """One form per quantity for conflict detection: rupee amounts in rupees, percentages as fractions
    (the same %/fraction equivalence candidate_forms accepts when matching the source)."""
    scale = rupee_scale(unit)
    if scale is not None:
        return value * scale
    u = (unit or "").lower()
    if "%" in u or "percent" in u:
        return value / 100
    return value


def _apart(a: Decimal, b: Decimal) -> bool:
    denom = max(abs(a), abs(b))
    return bool(denom) and abs(a - b) / denom > CONFLICT_TOL


def _conflicting(a: Claim, b: Claim) -> bool:
    va, vb = Decimal(a.value), Decimal(b.value)
    if not _apart(_conflict_base(va, a.unit), _conflict_base(vb, b.unit)):
        return False
    # a percentage next to a loosely spelt unit ("pct", "per cent"): the same figure unless the raw values differ
    loose = rupee_scale(a.unit) is None and rupee_scale(b.unit) is None
    return not loose or _apart(va, vb)


def _downgrade(c: Claim, note: str, *, force: bool = False) -> None:
    """unverified -> needs_review; with force, verified -> needs_review too. Never upgrades a claim and never
    overrides a contradiction."""
    if c.status == "unverified" or (force and c.status == "verified"):
        c.status = "needs_review"
    c.verifier_note = ((c.verifier_note + " | ") if c.verifier_note else "") + note


def run_gate(session: Session, run_id: int, *, stream: str | None = None, facts: dict[str, Any] | None = None,
             now: datetime | None = None) -> GateResult:  # fmt: skip
    """Run deterministic checks on a run's claims (optionally one stream) and record results on each claim."""
    now = now or datetime.now(UTC)
    facts = facts or {}
    q = select(Claim).where(Claim.run_id == run_id)
    if stream:
        q = q.where(Claim.stream == stream)
    claims = session.scalars(q.order_by(Claim.id)).all()
    res = GateResult()
    cache: dict[int, list[str]] = {}
    bidding_open = facts.get("issue_close") and date.fromisoformat(facts["issue_close"]) >= to_ist(now).date()
    issue_open = date.fromisoformat(facts["issue_open"]) if facts.get("issue_open") else None

    for c in claims:
        if c.status in ("unsupported", "contradicted"):
            continue
        res.checked += 1
        checks = dict(c.checks or {})
        text = f"{c.statement} {c.period or ''} {c.metric or ''}"

        # 1. value present at the cited lines (or a unit conversion of it)
        if c.claim_type == "numeric" and c.value is not None:
            windows = [cited_window(session, ct, cache) for ct in c.citations if ct.document_id]
            if windows:
                found = any(_matches(n, f) for w in windows for n in numbers_in(w)
                            for f in candidate_forms(Decimal(c.value), c.unit))  # fmt: skip
                checks["value_in_source"] = found
                if not found:
                    res.derived.append(c.id)
                    if c.status == "unverified":
                        _downgrade(
                            c, "gate: value not printed at cited lines (derived) — verifier must confirm"
                        )

        # 2. live figures need a timestamp while bidding is open; web sources must be fresh
        if LIVE_RE.search(text) and (c.claim_type == "numeric" or _LIVE_FIGURE_RE.search(c.statement)):
            flags = []
            if bidding_open and not TIME_RE.search(text):
                flags.append("no time/INTERIM label on a live figure")
            for ct in c.citations:
                if ct.url and ct.accessed_at and now - ct.accessed_at > LIVE_MAX_AGE:
                    flags.append(f"web source accessed {ct.accessed_at:%Y-%m-%d %H:%M} is older than 24h")
            checks["live_ok"] = not flags
            if flags:
                res.live_flags.append(c.id)
                _downgrade(c, "gate: " + "; ".join(flags), force=True)

        # 3. "Day N" must match the bidding calendar for any date in the same claim
        if issue_open:
            days = _DAY_RE.findall(c.statement)
            dates = [d for d in (_parse_date(m) for m in _DATE_RE.finditer(c.statement)) if d]
            if len(days) == 1 and len(dates) == 1 and dates[0] >= issue_open:
                from finresearch.adapters.nse_holidays import trading_holidays

                expected = bidding_day_number(issue_open, dates[0], trading_holidays())
                checks["day_label_ok"] = expected == int(days[0])
                if expected is not None and expected != int(days[0]):
                    res.day_label_errors.append(c.id)
                    c.status = "contradicted"
                    c.verifier_note = (f"gate: {dates[0]:%d-%b-%Y} is bidding Day {expected}, not Day {days[0]} "
                                       f"(issue opened {issue_open:%d-%b-%Y}; weekends/holidays excluded)")  # fmt: skip
        c.checks = checks

    # 4. cross-claim conflicts on the same metric + period (whole run, so streams are compared)
    all_claims = session.scalars(select(Claim).where(Claim.run_id == run_id, Claim.claim_type == "numeric",
                                                     Claim.value.is_not(None),
                                                     Claim.status.not_in(("unsupported", "contradicted")))).all()  # fmt: skip
    groups: dict[tuple[str, str], list[Claim]] = {}
    for c in all_claims:
        if c.metric and c.period:
            groups.setdefault((_norm_key(c.metric), _norm_key(c.period)), []).append(c)
    for items in groups.values():
        for i, a in enumerate(items):
            for b in items[i + 1 :]:
                if _conflicting(a, b):
                    res.conflicts.append((a.id, b.id))
                    for x, y in ((a, b), (b, a)):
                        chk = dict(x.checks or {})
                        chk.setdefault("conflict_with", [])
                        if y.id not in chk["conflict_with"]:
                            chk["conflict_with"].append(y.id)
                        x.checks = chk
                        if is_deterministic(x):
                            continue  # the agent side is reviewed; the exchange's own figure stands
                        _downgrade(
                            x,
                            f"gate: conflicts with claim {y.id} ({y.stream}: {y.value} {y.unit or ''})",
                            force=True,
                        )
    session.flush()
    return res


# --------------------------------------------------------------------------- corrections
def _parse_correct_value(text: str | None) -> Decimal | None:
    if not text:
        return None
    nums = numbers_in(text.split(";")[0])
    return nums[0] if len(nums) == 1 else None


def apply_correction(
    session: Session, claim: Claim, correct_value: str | None, evidence: str
) -> Claim | None:
    """Create a corrected claim linked to a contradicted one, re-using its citations, then re-check it.

    The correction becomes `verified` only if its value is printed at the cited lines (verifier and source agree);
    otherwise it stays `needs_review`. Non-numeric corrections are recorded as factual needs_review claims.
    """
    if not correct_value:
        return None
    new_val = _parse_correct_value(correct_value) if claim.claim_type == "numeric" else None
    corr = Claim(run_id=claim.run_id, stream=claim.stream, claim_type=claim.claim_type if new_val is not None
                 else "factual", statement=f"[correction of C{claim.id}] {correct_value}", metric=claim.metric,
                 value=new_val, unit=claim.unit, period=claim.period, importance=claim.importance,
                 status="needs_review", verifier_note=f"verifier evidence: {evidence[:1500]}",
                 corrects_claim_id=claim.id)  # fmt: skip
    session.add(corr)
    session.flush()
    for ct in claim.citations:
        session.add(Citation(claim_id=corr.id, document_id=ct.document_id, page_no=ct.page_no,
                             line_start=ct.line_start, line_end=ct.line_end, quote=ct.quote,
                             quote_found=ct.quote_found, url=ct.url, accessed_at=ct.accessed_at))  # fmt: skip
    session.flush()
    session.refresh(corr)
    if new_val is not None:
        windows = [cited_window(session, ct, {}) for ct in corr.citations if ct.document_id]
        found = any(
            _matches(n, f)
            for w in windows
            for n in numbers_in(w)
            for f in candidate_forms(new_val, corr.unit)
        )
        corr.checks = {"value_in_source": found}
        if found:
            corr.status = "verified"
    return corr


# --------------------------------------------------------------------------- publish gate
USABLE = {"verified"}
CAVEAT_OK = {"unverified", "needs_review"}
_CITE_RE = re.compile(r"\[C(\d+)\]")
_RAW_CITE_RE = re.compile(r"\[(?:RHP|DRHP|doc(?:ument)?)\s*L?\s*\d+", re.I)
_FIGURE_RE = re.compile(r"₹\s?\d|\d[\d,]*\.\d+|\d+(?:\.\d+)?\s?%|\d+(?:\.\d+)?x\b|\b\d{1,3}(?:,\d{2,3})+\b")


@dataclass
class ReportGate:
    ok: bool
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cited_claims: list[int] = field(default_factory=list)

    def revision_request(self) -> str:
        items = self.blocking + self.warnings[:30]
        return "REVISION REQUIRED — fix every item, then return the full report again:\n- " + "\n- ".join(
            items
        )


def check_report(session: Session, run_id: int, report_markdown: str) -> ReportGate:
    ids = sorted({int(x) for x in _CITE_RE.findall(report_markdown)})
    claims = {c.id: c for c in session.scalars(select(Claim).where(Claim.id.in_(ids)))} if ids else {}
    g = ReportGate(ok=True, cited_claims=ids)
    for i in ids:
        c = claims.get(i)
        if c is None or c.run_id != run_id:
            g.blocking.append(f"[C{i}] does not exist in this run's ledger — remove it or cite a real claim")
        elif c.status in ("contradicted", "unsupported"):
            fix = next(
                (x for x in session.scalars(select(Claim).where(Claim.corrects_claim_id == c.id))), None
            )
            hint = f"; use the correction [C{fix.id}] ({fix.statement[:80]})" if fix else ""
            g.blocking.append(f"[C{i}] is {c.status}: {c.verifier_note or ''}"[:300] + hint)
        elif c.importance == "high" and c.status not in USABLE:
            g.blocking.append(
                f"[C{i}] is high-importance but only '{c.status}' — verify it or drop the figure"
            )
        elif c.status in CAVEAT_OK:
            g.warnings.append(f"[C{i}] is '{c.status}' — keep only with an UNVERIFIED caveat")
        if (
            c is not None
            and (c.checks or {}).get("source_language") == "hi"
            and not c.checks.get("translation_marked")
        ):
            g.warnings.append(
                f"[C{i}] quotes a Hindi source: say in the report that the figure is translated"
            )
    raw = sorted(set(_RAW_CITE_RE.findall(report_markdown)))
    if raw:
        g.blocking.append(
            f"raw document citations {raw[:10]} — every figure must cite a ledger claim [C<id>]"
        )
    uncited = []
    for line in report_markdown.splitlines():
        if (
            _FIGURE_RE.search(line)
            and not _CITE_RE.search(line)
            and not line.lstrip().startswith(("#", "|---", ">"))
        ):
            uncited.append(line.strip()[:120])
    if uncited:
        g.warnings.append(f"{len(uncited)} lines contain figures without a [C<id>] citation, e.g.: "
                          + " || ".join(uncited[:5]))  # fmt: skip
    g.ok = not g.blocking
    return g
