"""Time-series OLS with Newey-West (HAC) standard errors, for factor exposures (#348).

    y_t = a + Σ_k b_k·f_k,t + e_t, estimated by least squares; the covariance of the estimates is the Newey & West
    (1987) estimator with Bartlett weights w_l = 1 - l/(L+1), so serially correlated and heteroskedastic residuals
    (daily returns of a portfolio with stale fund NAVs and thin trading are both) do not shrink the errors.

Lag rule: L = floor(4·(n/100)^(2/9)) (Newey & West 1994, "Automatic lag selection in covariance matrix estimation").
The covariance carries the n/(n-k) small-sample factor, as statsmodels' HAC does by default (`use_correction=True`);
`tests/test_factor_math.py` pins both against statsmodels 0.15.0. Floats via numpy: these are descriptive statistics
for a page, not report figures (those come from fincalc in Decimal).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

Z95 = 1.959963984540054  # two-sided 95 % normal quantile: intervals are asymptotic, as Newey-West errors are


@dataclass(frozen=True)
class OlsResult:
    coef: list[float]  # intercept first, then one per factor column
    se: list[float]
    t: list[float | None]  # None when the error is 0 (an exact fit)
    r2: float
    adj_r2: float
    resid_sd: float  # per period
    n: int
    lags: int


def newey_west_lags(n: int) -> int:
    return math.floor(4 * (n / 100) ** (2 / 9))


def ols_hac(y: Sequence[float], X: Sequence[Sequence[float]], lags: int | None = None) -> OlsResult:
    """OLS of `y` on a constant and the columns of `X` (rows are periods) with Newey-West standard errors.
    ValueError when there are not more observations than coefficients or the factors are collinear."""
    Y = np.asarray(y, dtype=float)
    F = np.asarray(X, dtype=float)
    n = len(Y)
    if F.ndim != 2 or F.shape[0] != n:
        raise ValueError("y and X must have the same number of rows")
    A = np.column_stack([np.ones(n), F])
    k = A.shape[1]
    if n <= k + 1:
        raise ValueError(f"needs more than {k + 1} observations; have {n}")
    if np.linalg.matrix_rank(A) < k:
        raise ValueError("the factor columns are collinear: exposures are not identified")
    L = newey_west_lags(n) if lags is None else lags
    XtX_inv = np.linalg.inv(A.T @ A)
    b = XtX_inv @ A.T @ Y
    e = Y - A @ b
    u = A * e[:, None]  # score per period
    S = u.T @ u
    for lag in range(1, min(L, n - 1) + 1):
        w = 1 - lag / (L + 1)
        G = u[lag:].T @ u[:-lag]
        S += w * (G + G.T)
    cov = XtX_inv @ S @ XtX_inv * (n / (n - k))
    se = np.sqrt(np.clip(np.diag(cov), 0, None))
    ss_res = float(e @ e)
    ss_tot = float(((Y - Y.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else float("nan")
    adj = 1 - (1 - r2) * (n - 1) / (n - k)
    scale = max(float(np.abs(b).max()), 1.0)
    t = [float(c / s) if s > 1e-12 * scale else None for c, s in zip(b, se, strict=True)]
    return OlsResult(coef=[float(x) for x in b], se=[float(x) for x in se], t=t, r2=r2, adj_r2=adj,
                     resid_sd=math.sqrt(ss_res / (n - k)), n=n, lags=L)  # fmt: skip
