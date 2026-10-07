"""Agreement between a claim and the blind second verifier's own reading of its source (#243).

The second verifier is never shown the claimed figure (orchestrator.base._blind_claims_text), so agreement is
decided here, deterministically: a figure agrees when both sides are within the gate's conflict tolerance (0.5 %)
after unit normalisation (₹ rupee/lakh/million/crore, % vs fraction), and the verifier did not say the rest of the
statement is unsupported. A claim without a figure agrees only on an explicit "yes". No finding is a disagreement.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from finresearch.db.models import Claim


def parse_figure(s: str | None) -> Decimal | None:
    """'1,234.56' -> 1234.56; '(512.40)' / '-512.40' -> -512.40; anything else None."""
    if s is None:
        return None
    t = re.sub(r"[\s,₹]|rs\.?|inr", "", str(s), flags=re.I)
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()").rstrip("%x")
    try:
        v = Decimal(t)
    except InvalidOperation:
        return None
    return -v if neg else v


def agrees(c: Claim, finding: Any | None) -> tuple[bool, str]:
    """(agrees, why) for one claim and the blind verifier's finding (agents.schemas.BlindFinding) or None."""
    from finresearch.verify.gate import _apart, _conflict_base

    if finding is None:
        return False, "the second verifier returned no finding for this claim"
    if finding.supports_statement == "no":
        return False, f"source does not support the statement: {finding.evidence[:300]}"
    if c.claim_type == "numeric" and c.value is not None:
        got = parse_figure(finding.derived_value)
        if got is None:
            return False, f"could not re-derive the figure from the cited source: {finding.evidence[:300]}"
        mine = _conflict_base(Decimal(c.value), c.unit)
        theirs = _conflict_base(got, finding.derived_unit or c.unit)
        if _apart(mine, theirs):
            return False, (f"re-derived {finding.derived_value} {finding.derived_unit or c.unit or ''} from the source, "
                           f"not {c.value.normalize()} {c.unit or ''}: {finding.evidence[:300]}")  # fmt: skip
        return True, f"re-derived the same figure ({finding.derived_value}) independently"
    if finding.supports_statement == "yes":
        return True, "the source supports the statement"
    return False, f"could not confirm the statement: {finding.evidence[:300]}"
