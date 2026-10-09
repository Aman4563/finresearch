"""Evidence grades for claims and citations (#242). Deterministic: computed from the stored citation checks.

    A  document quote verified at its cited lines (quote_found on a document citation)
    B  web quote verified against a stored snapshot of the page (fetch_page / exchange tool text, snapshot_sha256);
       also exchange/AMFI facts the pipeline itself fetched and recorded (verify.gate.DETERMINISTIC_SOURCES)
    C  web citation whose quote was not checked (no stored snapshot)
    D  computed by fincalc: save_claim re-ran the call, it reproduced the value, and its input claims are cited
    U  unsupported: a checked citation that failed, or a free-text calculation note nobody re-ran

A claim's grade is the best of its citations' (A, B, D are "checked"; C is weaker; U worst). The publish gate
requires A, B or D for every high-importance claim a report cites (verify.gate.check_report).
"""

from __future__ import annotations

import re
from typing import Any

from finresearch.db.models import Citation, Claim

STRONG = frozenset("ABD")
_RANK = {"A": 0, "B": 1, "D": 2, "C": 3, "U": 4}
LABEL = {
    "A": "document quote verified at its lines",
    "B": "web quote verified against a stored copy of the page",
    "C": "web source, quote not verified",
    "D": "computed by fincalc from cited inputs",
    "U": "unsupported",
}
_HTTP = re.compile(r"^https?://", re.I)


def citation_grade(ct: Citation) -> tuple[str, str]:
    """(grade, honest label) for one citation."""
    if (
        ct.document_id is not None or ct.line_start is not None
    ):  # a line span is a document citation (a replayed
        if ct.quote_found:  # run's citations keep their lines and check but not the document row)
            return "A", LABEL["A"]
        return "U", "document quote not found at the cited lines"
    if ct.computation:
        if ct.quote_found:
            return "D", LABEL["D"]
        return "U", f"fincalc check failed: {(ct.computation or {}).get('detail') or 'not reproduced'}"
    if ct.url and _HTTP.match(ct.url):
        if ct.snapshot_sha256:
            if ct.quote_found:
                return "B", LABEL["B"]
            return "U", "quote not found on the stored copy of the page"
        return "C", LABEL["C"]
    if ct.url:  # "fincalc: ..." typed by an agent before #242: nothing re-ran it
        return "U", "calculation note, not re-checked"
    return "U", "citation without a source"


def claim_grade(c: Claim) -> dict[str, Any]:
    """{"grade", "label", "citations": [grade per citation]} for a claim."""
    from finresearch.verify.gate import is_deterministic

    per = [citation_grade(ct) for ct in c.citations]
    if is_deterministic(c):
        return {"grade": "B", "label": "exchange/AMFI data fetched and recorded by the pipeline",
                "citations": [g for g, _ in per]}  # fmt: skip
    if not per:
        return {"grade": "U", "label": "no citation", "citations": []}
    g, label = min(per, key=lambda x: _RANK[x[0]])
    return {"grade": g, "label": label, "citations": [x for x, _ in per]}


def citation_json(ct: Citation) -> dict[str, Any]:
    g, label = citation_grade(ct)
    return {
        "grade": g,
        "grade_label": label,
        "snapshot_sha256": ct.snapshot_sha256,
        "computation": ct.computation,
    }
