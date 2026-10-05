"""Allocation across the whole balance sheet and a glide-path rule of thumb.

Classes: Equity, Debt, Gold, Real estate, Cash, Other. Mapping: FD/RD/EPF/PPF -> Debt; NPS split by its equity share
(E) with the rest in Debt; gold and SGB -> Gold; savings/cash -> Cash; the portfolio's classes from its latest
snapshot (stocks and equity funds -> Equity, debt funds -> Debt, gold & international funds and SGBs -> Gold; the
international part of that portfolio bucket is equity in reality, a stated approximation).

Glide path (a rule of thumb [W], never a recommendation): equity % = clamp(100 - age, 20, 80), then -10 for a low
and +10 for a high risk appetite; gold 10 % (a common diversifier rule of thumb [W]); the rest debt and cash. Bands
("5/25" rule of thumb [W], portfolio.limits.band_pp, shared with the Rebalance card): a class is outside its band when
|weight - target| exceeds the TIGHTER of ±5 pp and 25 % of the target (a 0 % target: ±5 pp only); both widths are
settable on the profile. (Before #195 this used the looser max(5 pp, 25 %), so /wealth and the Rebalance card could
disagree on the same drift.)
Real estate is not part of the comparison (illiquid, self-valued); the comparison is over financial assets.
"""

from __future__ import annotations

from typing import Any

from finresearch.portfolio.limits import DEFAULT_ABS_PP, DEFAULT_REL_PCT, band_pp

CLASSES = ("Equity", "Debt", "Gold", "Cash", "Real estate", "Other")
FINANCIAL = ("Equity", "Debt", "Gold", "Cash", "Other")

PORTFOLIO_CLASS = {
    "Stocks": "Equity",
    "Equity funds": "Equity",
    "Debt funds": "Debt",
    "Gold & international funds": "Gold",
    "Sovereign Gold Bonds": "Gold",
    "Other": "Other",
}
KIND_CLASS = {
    "fd": "Debt",
    "rd": "Debt",
    "epf": "Debt",
    "ppf": "Debt",
    "gold": "Gold",
    "sgb": "Gold",
    "real_estate": "Real estate",
    "cash": "Cash",
    "other": "Other",
}
RISK_TILT = {"low": -10, "medium": 0, "high": 10}
GOLD_RULE_PCT = 10.0


def split_asset(
    kind: str, value: float, *, asset_class: str | None = None, equity_pct: float | None = None
) -> dict[str, float]:
    """One asset's value by class. NPS without an equity share is assumed 50 % equity (stated in the UI)."""
    if kind == "nps":
        e = 50.0 if equity_pct is None else max(0.0, min(100.0, equity_pct))
        return {"Equity": value * e / 100, "Debt": value * (100 - e) / 100}
    if kind == "other" and asset_class in CLASSES:
        return {asset_class: value}
    return {KIND_CLASS.get(kind, "Other"): value}


def glide_equity_pct(age: int, risk: str = "medium") -> float:
    """clamp(100 - age, 20, 80) + risk tilt, kept inside 0..100 (rule of thumb)."""
    base = min(max(100 - age, 20), 80)
    return float(min(max(base + RISK_TILT.get(risk, 0), 0), 100))


def glide_target(
    age: int | None, risk: str = "medium", equity_override: float | None = None
) -> dict[str, float] | None:
    """Target % over financial assets: equity by the rule (or the user's own equity target), gold 10 %, rest debt
    (cash counts with debt). None without an age and without an override."""
    if equity_override is not None:
        e = float(equity_override)
    elif age is not None:
        e = glide_equity_pct(age, risk)
    else:
        return None
    g = min(GOLD_RULE_PCT, 100 - e)
    return {"Equity": e, "Gold": g, "Debt + cash": round(100 - e - g, 6)}


def compare(weights: dict[str, float], target: dict[str, float], abs_pp: float = DEFAULT_ABS_PP,
            rel_pct: float = DEFAULT_REL_PCT) -> list[dict[str, Any]]:  # fmt: skip
    """Weight vs target per class (Debt and Cash compared together), with the 5/25 band and a status."""
    w = {
        "Equity": weights.get("Equity", 0.0),
        "Gold": weights.get("Gold", 0.0),
        "Debt + cash": weights.get("Debt", 0.0) + weights.get("Cash", 0.0),
    }
    rows = []
    for k, t in target.items():
        d = w.get(k, 0.0) - t
        b = band_pp(t, abs_pp, rel_pct)
        rows.append(
            {
                "label": k,
                "weight_pct": round(w.get(k, 0.0), 2),
                "target_pct": round(t, 2),
                "drift_pp": round(d, 2),
                "band_pp": round(b, 2),
                "outside_band": abs(d) > b,
            }
        )
    return rows


def weights(by_class: dict[str, float], classes: tuple[str, ...]) -> dict[str, float]:
    total = sum(v for k, v in by_class.items() if k in classes)
    if total <= 0:
        return {}
    return {k: by_class.get(k, 0.0) / total * 100 for k in classes if by_class.get(k, 0.0)}
