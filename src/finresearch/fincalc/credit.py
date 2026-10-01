"""Credit risk for bonds and NCDs: rating grades, historical default rates and expected loss.

Expected loss per year = PD_annual x LGD, where PD_annual = 1 - (1 - CDR_3y)**(1/3) turns a 3-year cumulative default
rate into a constant yearly rate (docs/dev/RESEARCH_ROADMAP.md §D.4). The default rates are CRISIL's historical
averages, a base rate for the rating grade and not a forecast for any issuer.
"""

from __future__ import annotations

import re
from decimal import Decimal

from finresearch.fincalc.numbers import Num, to_decimal

CDR_SOURCE = (
    "https://www.crisilratings.com/content/dam/crisil/our-analysis/publications/default-study/"
    "crisil-ratings-annual-default-and-ratings-transition-study-fy-2026.pdf"
)
CDR_AS_OF = "FY2016-FY2026 averages, CRISIL Ratings Annual Default and Ratings Transition Study FY2026"
# Average cumulative default rates by grade over 1, 2 and 3 years (fractions), Table 1 of the study (monthly static
# pools; re-read 1-Oct-2026, all five rows match). CRISIL does not publish a 5-year CDR. Table 1 also gives B
# (8.60 / 17.68 / 27.28 %) and C (24.84 / 42.71 / 56.05 %); they are left out on purpose: a sub-BB bond gets no signal.
CDR: dict[str, tuple[Decimal, Decimal, Decimal]] = {
    "AAA": (Decimal("0"), Decimal("0"), Decimal("0")),
    "AA": (Decimal("0.0002"), Decimal("0.0007"), Decimal("0.0014")),
    "A": (Decimal("0.0007"), Decimal("0.0033"), Decimal("0.0058")),
    "BBB": (Decimal("0.0043"), Decimal("0.0115"), Decimal("0.0197")),
    "BB": (Decimal("0.028"), Decimal("0.0598"), Decimal("0.097")),
}
GRADES = ("SOV", "AAA", "AA", "A", "BBB", "BB", "B", "C", "D")  # best first
INVESTMENT_GRADE = ("SOV", "AAA", "AA", "A", "BBB")
# Loss given default: share of the money lost when an issuer defaults. Assumptions, not measured (§D.4 marks them
# [U]): 60 % for senior unsecured debt, 40 % for secured.
LGD_UNSECURED = Decimal("0.60")
LGD_SECURED = Decimal("0.40")

_GRADE_RE = re.compile(r"(?<![A-Z])(AAA|AA|BBB|BB|A|B|C|D|SOV)([+-]?)(?![0-9A-Z])")


def base_grade(rating: str | None) -> str | None:
    """The lowest long-term grade in a rating text, without +/- or outlook: 'AA/Stable, AA+/Negative' -> 'AA',
    '[ICRA]AA+' -> 'AA', 'CRISIL AAA/stable/' -> 'AAA'. Short-term ratings (A1+) and unknown texts give None."""
    if not rating:
        return None
    found = []
    for part in rating.upper().split(","):
        part = re.sub(
            r"\b(CRISIL|ICRA|CARE|IND|INDIA|FITCH|BWR|BRICKWORK|ACUITE|SMERA|IVR|INFOMERICS)\b", " ", part
        )
        m = _GRADE_RE.search(part)
        if m:
            found.append(m.group(1))
    return max(found, key=GRADES.index) if found else None


def cumulative_default_rate(grade: str, years: float) -> Decimal | None:
    """Probability of default within `years` for a grade: CRISIL's 1/2/3-year CDR (linear in between, and from zero
    below one year), and beyond three years a constant annual rate from the 3-year CDR. SOV is zero; grades below BB
    (B, C, D) and unknown grades give None: B and C are in CRISIL's table but deliberately not used here (D is in
    default already)."""
    if years < 0:
        raise ValueError("years must be >= 0")
    if grade == "SOV":
        return Decimal(0)
    if grade not in CDR:
        return None
    c1, c2, c3 = (float(x) for x in CDR[grade])
    if years <= 1:
        v = c1 * years
    elif years <= 2:
        v = c1 + (c2 - c1) * (years - 1)
    elif years <= 3:
        v = c2 + (c3 - c2) * (years - 2)
    else:
        v = 1 - (1 - float(annual_pd(grade))) ** years
    return Decimal(str(round(v, 10)))


def annual_pd(grade: str) -> Decimal:
    """Constant yearly default probability implied by the 3-year CDR: 1 - (1 - CDR_3)**(1/3)."""
    if grade == "SOV":
        return Decimal(0)
    c3 = float(CDR[grade][2])
    return Decimal(str(round(1 - (1 - c3) ** (1 / 3), 10)))


def expected_loss(grade: str, lgd: Num = LGD_UNSECURED) -> Decimal:
    """Expected credit loss per year as a yield deduction, PD_annual x LGD. BBB: 1 - (1 - 0.0197)^(1/3) = 0.6610 %
    a year, x 0.60 = 0.397 pp."""
    loss = to_decimal(lgd)
    if not 0 <= loss <= 1:
        raise ValueError("lgd must be between 0 and 1")
    return annual_pd(grade) * loss


def next_lower(grade: str) -> str | None:
    """One grade worse (AA -> A), for a downgrade stress test; None below BB."""
    i = GRADES.index(grade)
    nxt = GRADES[i + 1] if i + 1 < len(GRADES) else None
    return nxt if nxt in CDR else None
