"""One place for the two personal limits every page uses (decisions #194 and #195).

1. Single-stock position limit (`position_limit`): the profile's max position % when set, else a default by risk
   appetite: low 5 %, medium 8 %, high 10 % (rules of thumb [W], the same values signals.stock has always used as
   CAPS). Used by the stock signal's sizing, the pre-trade checklist, /portfolio concentration and the
   "Position too big" alert text, so all of them flag the same holding. The result names the rule that applied,
   e.g. "5 % — your risk profile: low".

2. Rebalancing band (`band_pp`): a class is outside its band when |weight - target| exceeds the TIGHTER of an
   absolute band (default ±5 pp) and a relative band (default 25 % of the target), the "5/25" rule of thumb [W]
   (Swedroe's 5/25 rule; Vanguard's example rebalances at a 5-point drift: Jaconetti, Kinniry & Zilbering, "Best
   practices for portfolio rebalancing", Vanguard 2010, and
   https://investor.vanguard.com/investor-resources-education/portfolio-management/rebalancing-your-portfolio).
   A 0 % target (or a relative band of 0) uses the absolute band only. Both widths are settable on the profile
   (`rebalance_band_abs_pp`, `rebalance_band_rel_pct`). Used by /wealth (wealth.allocation) and the Rebalance card
   (portfolio.rebalance) so the two never disagree on a breach.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# single-stock cap (fraction of the portfolio) when the profile sets none; rule of thumb [W]
CAPS = {"low": 0.05, "medium": 0.08, "high": 0.10}
DEFAULT_RISK = "medium"
DEFAULT_ABS_PP = 5.0
DEFAULT_REL_PCT = 25.0


@dataclass(frozen=True)
class PositionLimit:
    pct: float  # percent of the portfolio, e.g. 8.0
    source: str  # "profile" (the user's max position) | "risk" (default by risk appetite)
    risk: str  # the profile's risk appetite (low | medium | high)
    rule: str  # what the UI shows, e.g. "5 % — your risk profile: low"

    def to_json(self) -> dict[str, Any]:
        return {"pct": self.pct, "source": self.source, "risk": self.risk, "rule": self.rule}


def position_limit(profile: Any) -> PositionLimit:
    """The single-stock limit in percent: the profile's max position if set, else 5 / 8 / 10 % by risk appetite."""
    risk = getattr(profile, "risk_appetite", None) or DEFAULT_RISK
    if risk not in CAPS:
        risk = DEFAULT_RISK
    explicit = getattr(profile, "max_position_pct", None)
    if explicit:
        pct = float(explicit)
        return PositionLimit(pct, "profile", risk, f"{pct:g} % — your profile's max position")
    pct = round(CAPS[risk] * 100, 6)
    return PositionLimit(pct, "risk", risk, f"{pct:g} % — your risk profile: {risk}")


def band_pp(target_pct: float, abs_pp: float = DEFAULT_ABS_PP, rel_pct: float = DEFAULT_REL_PCT) -> float:
    """The band half-width in pp: the tighter of the absolute and relative bands (0 % target: absolute only)."""
    if target_pct <= 0 or rel_pct <= 0:
        return abs_pp
    return min(abs_pp, rel_pct / 100 * target_pct)


def outside_band(weight_pct: float, target_pct: float, abs_pp: float = DEFAULT_ABS_PP,
                 rel_pct: float = DEFAULT_REL_PCT) -> bool:  # fmt: skip
    return abs(weight_pct - target_pct) > band_pp(target_pct, abs_pp, rel_pct)


def bands(profile: Any) -> tuple[float, float]:
    """(abs_pp, rel_pct) from the profile, else the 5 / 25 defaults."""
    a = getattr(profile, "rebalance_band_abs_pp", None)
    r = getattr(profile, "rebalance_band_rel_pct", None)
    return (float(a) if a is not None else DEFAULT_ABS_PP, float(r) if r is not None else DEFAULT_REL_PCT)


def band_rule(abs_pp: float, rel_pct: float) -> str:
    """The band in words, for the UI."""
    if rel_pct <= 0:
        return f"±{abs_pp:g} pp (absolute band only)"
    return (f"the tighter of ±{abs_pp:g} pp and {rel_pct:g} % of the target (a 0 % target uses ±{abs_pp:g} pp)")  # fmt: skip
