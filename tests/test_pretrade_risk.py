"""#399: the pre-trade check's marginal-risk line: the portfolio's volatility before and after the trade, the traded
holding's marginal contribution MCR_i = (Σw)_i / σ_p, and its correlation with the largest other holding. Synthetic
returns only; expected values from numpy."""

from __future__ import annotations

import math

import numpy as np
import pytest


def _mat(n=200, seed=5):
    rng = np.random.default_rng(seed)
    m = rng.normal(0.0, 0.01, n)
    a = 0.8 * m + rng.normal(0, 0.006, n)
    b = 1.3 * m + rng.normal(0, 0.012, n)
    c = rng.normal(0, 0.004, n)  # a low-volatility, uncorrelated holding
    return np.column_stack([a, b, c])


def test_trade_risk_matches_numpy():
    from finresearch.portfolio.analytics_math import trade_risk

    r = _mat()
    before, after = [5000.0, 3000.0, 2000.0], [5000.0, 4000.0, 2000.0]  # buy ₹1,000 of holding 1
    got = trade_risk(before, after, r.tolist(), 1, 0)
    cov = np.cov(r, rowvar=False, ddof=1)
    w = np.array(before) / sum(before)
    wa = np.array(after) / sum(after)
    sp = math.sqrt(w @ cov @ w * 252)
    assert got.sigma_before == pytest.approx(sp, rel=1e-9)
    assert got.sigma_after == pytest.approx(math.sqrt(wa @ cov @ wa * 252), rel=1e-9)
    assert got.mcr == pytest.approx((cov @ w)[1] * 252 / sp, rel=1e-9)
    assert got.correlation == pytest.approx(np.corrcoef(r[:, 1], r[:, 0])[0, 1], rel=1e-9)


@pytest.mark.parametrize("i", [0, 1, 2])
def test_new_money_raises_volatility_exactly_when_mcr_exceeds_it(i):
    """σ_p is homogeneous of degree 1 in w, so adding a little new money to i changes σ_p by about
    ε·(MCR_i - σ_p): the line's sign test."""
    from finresearch.portfolio.analytics_math import trade_risk

    r = _mat().tolist()
    before = [5000.0, 3000.0, 2000.0]
    after = list(before)
    after[i] += 10.0
    got = trade_risk(before, after, r, i, (i + 1) % 3)
    assert (got.sigma_after > got.sigma_before) == (got.mcr > got.sigma_before)


def test_trade_risk_refuses_too_few_days():
    from finresearch.portfolio.analytics_math import trade_risk

    with pytest.raises(ValueError):
        trade_risk([1.0, 1.0], [2.0, 1.0], [[0.01, 0.02], [0.0, 0.01]], 0, 1)
