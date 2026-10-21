"""OLS with Newey-West (HAC) standard errors for factor exposures (#348). Reference values come from statsmodels 0.15.0
(`OLS(...).fit(cov_type="HAC", cov_kwds={"maxlags": 5, "use_correction": True})`), run once outside the repo on the
same seeded data; statsmodels is not a dependency."""

from __future__ import annotations

import random

import pytest

from finresearch.portfolio import factor_math as fm


def _data(seed: int = 7, n: int = 300) -> tuple[list[float], list[list[float]]]:
    g = random.Random(seed)
    X, y, e = [], [], 0.0
    for _ in range(n):
        f = [g.gauss(0, 0.01) for _ in range(4)]
        e = 0.5 * e + g.gauss(0, 0.004)  # AR(1) noise: plain OLS errors would be too small
        X.append(f)
        y.append(0.0002 + 0.9 * f[0] + 0.3 * f[1] - 0.2 * f[2] + 0.1 * f[3] + e)
    return y, X


def test_newey_west_lag_rule():
    # floor(4·(n/100)^(2/9)), Newey & West (1994)
    assert fm.newey_west_lags(300) == 5
    assert fm.newey_west_lags(100) == 4
    assert fm.newey_west_lags(1000) == 6


def test_ols_matches_statsmodels_hac():
    y, X = _data()
    r = fm.ols_hac(y, X)
    assert r.n == 300 and r.lags == 5
    want_coef = [0.0005538857, 0.9214756022, 0.2913701764, -0.1906622565, 0.1032474943]
    want_se = [0.0003587134, 0.0250355196, 0.0231665427, 0.0189968378, 0.017242675]
    assert r.coef == pytest.approx(want_coef, abs=1e-9)
    assert r.se == pytest.approx(want_se, rel=1e-7)
    assert r.r2 == pytest.approx(0.8509599014, abs=1e-9)
    assert r.adj_r2 == pytest.approx(0.8489390187, abs=1e-9)
    assert r.t[1] == pytest.approx(0.9214756022 / 0.0250355196, rel=1e-7)


def test_exact_fit_recovers_betas():
    g = random.Random(3)
    X = [[g.gauss(0, 0.01) for _ in range(4)] for _ in range(150)]
    y = [0.0001 + 1.1 * a - 0.4 * b + 0.25 * c + 0.0 * d for a, b, c, d in X]
    r = fm.ols_hac(y, X)
    assert r.coef == pytest.approx([0.0001, 1.1, -0.4, 0.25, 0.0], abs=1e-12)
    assert max(r.se) < 1e-12
    assert r.r2 == pytest.approx(1.0)


def test_refuses_too_few_rows_and_collinear_factors():
    with pytest.raises(ValueError, match="observations"):
        fm.ols_hac([0.01] * 5, [[0.01, 0.02, 0.0, 0.01]] * 5)
    g = random.Random(1)
    X = [[x, 2 * x, g.gauss(0, 1), g.gauss(0, 1)] for x in (g.gauss(0, 1) for _ in range(50))]
    with pytest.raises(ValueError, match="collinear"):
        fm.ols_hac([g.gauss(0, 1) for _ in range(50)], X)
