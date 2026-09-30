"""Seeded Monte Carlo DCF: a fair-value distribution instead of a point estimate (roadmap §C.7).

Method (Damodaran, "Probabilistic Approaches: Scenario Analysis, Decision Trees and Simulations"): draw the
uncertain inputs — stage-1 growth, discount rate, terminal growth — from bounded triangular distributions, value
each draw with ``valuation.dcf`` and report P5 / P50 / P95 and P(value > price). His warnings apply and are shown
with the output: "garbage in, garbage out" (every input range carries its source), choosing the distribution is the
hardest step, and risk must not be double counted (the discount rate is not also risk-adjusted here).

Inputs are drawn independently. Growth and the discount rate are positively correlated in reality; drawing them
independently widens the distribution rather than narrowing it, which is the conservative direction for a
personal "is the price demanding?" check. A draw with r − g_T < ``min_spread`` is rejected and redrawn (the
terminal value explodes as r → g_T) and the rejections are counted.

Reproducible: ``random.Random(seed)``; the same inputs and seed give the same numbers on every run.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from finresearch.fincalc.numbers import Num, to_decimal


@dataclass(frozen=True)
class Range:
    """A triangular input range (fractions for rates) with where it came from ("C1590", "assumption: ...")."""

    low: float
    mode: float
    high: float
    source: str = ""

    def __post_init__(self) -> None:
        if not self.low <= self.mode <= self.high:
            raise ValueError(f"need low <= mode <= high, got {self.low}, {self.mode}, {self.high}")

    def draw(self, rng: random.Random) -> float:
        return self.mode if self.low == self.high else rng.triangular(self.low, self.high, self.mode)


@dataclass(frozen=True)
class McResult:
    n: int
    seed: int
    p5: float
    p25: float
    p50: float
    p75: float
    p95: float
    mean: float
    prob_above_price: float | None
    rejected: int
    histogram: list[dict[str, float]] = field(default_factory=list)  # [{lo, hi, count}]


def _dcf_float(cf: float, g: float, r: float, gt: float, years: int) -> float:
    pv, cft, disc = 0.0, cf, 1.0
    for _ in range(years):
        cft *= 1 + g
        disc *= 1 + r
        pv += cft / disc
    return pv + cft * (1 + gt) / (r - gt) / disc


def _pct(xs: list[float], q: float) -> float:
    pos = (len(xs) - 1) * q
    i = int(pos)
    return xs[i] if i + 1 >= len(xs) else xs[i] + (xs[i + 1] - xs[i]) * (pos - i)


def simulate_dcf(cash_flow: Num, shares: Num, growth: Range, discount_rate: Range, terminal_growth: Range, *,
                 years: int = 10, net_debt: Num = 0, fresh_issue_proceeds: Num = 0, price: Num | None = None,
                 n: int = 5000, seed: int = 20260930, bins: int = 24, min_spread: float = 0.01) -> McResult:  # fmt: skip
    """Per-share fair-value distribution: (DCF − net debt + fresh-issue proceeds) / shares for ``n`` draws.

    With zero-width ranges every draw equals ``valuation.dcf_per_share`` (the golden test)."""
    if n < 100:
        raise ValueError("n must be >= 100")
    if (
        discount_rate.high - terminal_growth.low < min_spread
        and discount_rate.low - terminal_growth.high < min_spread
    ):
        raise ValueError("discount rate range must exceed terminal growth range")
    cf, sh = float(to_decimal(cash_flow)), float(to_decimal(shares))
    if sh <= 0:
        raise ValueError("shares must be > 0")
    bridge = float(to_decimal(fresh_issue_proceeds)) - float(to_decimal(net_debt))
    rng = random.Random(seed)
    vals: list[float] = []
    rejected = 0
    while len(vals) < n:
        g, r, gt = growth.draw(rng), discount_rate.draw(rng), terminal_growth.draw(rng)
        if r - gt < min_spread:
            rejected += 1
            if rejected > 50 * n:
                raise ValueError("almost every draw has r - g_T below the minimum spread")
            continue
        vals.append((_dcf_float(cf, g, r, gt, years) + bridge) / sh)
    vals.sort()
    pr = None if price is None else sum(1 for v in vals if v > float(to_decimal(price))) / n
    lo, hi = (
        _pct(vals, 0.01),
        _pct(vals, 0.99),
    )  # the histogram spans P1–P99; the tails are counted at the ends
    hist: list[dict[str, float]] = []
    if hi > lo:
        width = (hi - lo) / bins
        counts = [0] * bins
        for v in vals:
            counts[min(bins - 1, max(0, math.floor((v - lo) / width)))] += 1
        hist = [{"lo": lo + i * width, "hi": lo + (i + 1) * width, "count": c} for i, c in enumerate(counts)]
    return McResult(n=n, seed=seed, p5=_pct(vals, 0.05), p25=_pct(vals, 0.25), p50=_pct(vals, 0.5),
                    p75=_pct(vals, 0.75), p95=_pct(vals, 0.95), mean=sum(vals) / n, prob_above_price=pr,
                    rejected=rejected, histogram=hist)  # fmt: skip
