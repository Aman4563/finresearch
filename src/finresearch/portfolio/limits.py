"""One place for the personal limits every page uses (decisions #194 and #195).

1. Single-stock position limit (`position_limit`): the profile's max position % when set, else a default by risk
   appetite: low 5 %, medium 8 %, high 10 % (rules of thumb [W], the same values signals.stock has always used as
   CAPS). Used by the stock signal's sizing, the pre-trade checklist, /portfolio concentration and the
   "Position too big" alert text, so all of them flag the same holding. The result names the rule that applied,
   e.g. "5 % — your risk profile: low".

2. Rebalancing band (`band_pp`): a class is outside its band when |weight - target| exceeds the TIGHTER of an
   absolute band (default ±5 pp) and a relative band (default 25 % of the target), the "5/25" rule of thumb [W]
   (Vanguard's example rebalances at a 5-point drift: Jaconetti, Kinniry & Zilbering, "Best
   practices for portfolio rebalancing", Vanguard 2010, and
   https://investor.vanguard.com/investor-resources-education/portfolio-management/rebalancing-your-portfolio).
   A 0 % target (or a relative band of 0) uses the absolute band only. Both widths are settable on the profile
   (`rebalance_band_abs_pp`, `rebalance_band_rel_pct`). Used by /wealth (wealth.allocation) and the Rebalance card
   (portfolio.rebalance) so the two never disagree on a breach.

3. Fund / ETF position threshold (`FUND_LIMIT_PCT`, 25 %): the single-stock limit is about one company's risk, so it
   never applies to a diversified mutual fund or an ETF (one scheme already holds dozens of stocks under SEBI's
   diversification limits, e.g. at most 10 % of a scheme's NAV in the shares of one company: SEBI (Mutual Funds)
   Regulations 1996, Seventh Schedule, clause 11). A fund or ETF only counts as concentrated above 25 % of the
   portfolio, the same rule of thumb [W] as the sector limit. ETFs arrive as listed "stock" holdings, so `is_fund_like` also matches
   ETF symbols/names (BEES / ETF / IETF suffix or "ETF" in the name) [unverified heuristic].
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# single-stock cap (fraction of the portfolio) when the profile sets none; rule of thumb [W]
CAPS = {"low": 0.05, "medium": 0.08, "high": 0.10}
DEFAULT_RISK = "medium"
DEFAULT_ABS_PP = 5.0
DEFAULT_REL_PCT = 25.0
# a single fund or ETF above this share of the portfolio is concentrated; rule of thumb [W] (see 3. above)
FUND_LIMIT_PCT = 25.0
_ETF_SYMBOL = re.compile(r"(BEES|ETF|IETF)$")
_ETF_NAME = re.compile(r"\bETF\b|\bBEES\b", re.I)

# Sector labels that are not a business sector: funds and ETFs are not looked through here, so they are grouped by
# what they hold instead of NSE's industry for an ETF ("Mutual Fund Scheme - ETF", which would lump gold, silver,
# debt and index ETFs into one fake sector). These never count towards a sector-concentration flag or alert.
FUNDS_SECTOR = "Funds (no look-through)"
COMMODITY_SECTOR = "Commodities (gold/silver)"
DEBT_ETF_SECTOR = "Debt ETFs"
BONDS_SECTOR = "Bonds & NCDs"
UNCLASSIFIED_SECTOR = "Unclassified"
NOT_A_SECTOR = frozenset({FUNDS_SECTOR, COMMODITY_SECTOR, DEBT_ETF_SECTOR, BONDS_SECTOR, UNCLASSIFIED_SECTOR})
_COMMODITY = re.compile(r"\b(gold|silver)\b|GOLDBEES|SILVERBEES|GOLDETF|SILVERETF|GOLDIETF|SILVERIETF", re.I)
_DEBT = re.compile(
    r"\b(liquid|gilt|g-?sec|bond|debt|sdl|overnight|money market)\b|LIQUIDBEES|LIQUIDETF", re.I
)


def is_fund_like(asset_type: str | None, symbol: str | None = None, name: str | None = None) -> bool:
    """True for a mutual fund or an ETF (held as a listed share): these never count against the single-stock limit."""
    if asset_type == "mf":
        return True
    if asset_type != "stock":
        return False
    return bool(_ETF_SYMBOL.search((symbol or "").upper()) or _ETF_NAME.search(name or ""))


def sector_label(asset_type: str | None, sector: str | None, symbol: str | None = None, name: str | None = None,
                 tax_class: str | None = None, isin: str | None = None) -> str:  # fmt: skip
    """The sector a holding counts towards: a stock's NSE industry (or the user's own label), but funds, ETFs and
    Sovereign Gold Bonds are grouped by what they hold, never by NSE's "Mutual Fund Scheme - ETF" industry."""
    if asset_type == "mf":
        return FUNDS_SECTOR
    if tax_class == "sgb":
        return COMMODITY_SECTOR
    from finresearch.portfolio.tax import is_debt_security

    if asset_type == "stock" and is_debt_security(isin, name, symbol):
        return BONDS_SECTOR
    if is_fund_like(asset_type, symbol, name):
        text = f"{symbol or ''} {name or ''}"
        if _COMMODITY.search(text):
            return COMMODITY_SECTOR
        if tax_class == "debt_mf" or _DEBT.search(text):
            return DEBT_ETF_SECTOR
        return FUNDS_SECTOR
    return sector if sector and sector.strip(" -–") else UNCLASSIFIED_SECTOR  # BSE gives "-" when it has none


def is_real_sector(label: str | None) -> bool:
    """True for a business sector (counts towards sector concentration); False for the groupings above."""
    return bool(label) and label not in NOT_A_SECTOR and not label.startswith("Funds")


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
