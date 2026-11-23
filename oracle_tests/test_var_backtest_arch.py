"""portfolio/analytics_math: Kupiec's proportion-of-failures test, Christoffersen's independence test and the
chi-square tail, against arch.univariate.backtest and scipy.stats.

Conventions pinned first:
* arch takes the returns and the VaR as a POSITIVE loss and counts an exception when return < -VaR; the module takes
  the counts (n days, x exceptions) or the 0/1 hit sequence. The test builds a return series with exactly the wanted
  hits (a hit is -2 against a VaR of 1, anything else 0), so both sides see the same exceptions;
* p is the tail probability (0.05 for a 95 % VaR) on both sides;
* both statistics are likelihood ratios with one degree of freedom, and 0 x ln 0 = 0 (no exception, only
  exceptions, no exception after an exception).

Tolerance: these are sums of a handful of x ln y terms, so float rounding only. The likelihood ratio subtracts two
log-likelihoods of size n, hence an absolute 1e-10 next to the relative 1e-10.
"""

from __future__ import annotations

import itertools

import numpy as np
from arch.univariate.backtest import christoffersen_independence, kupiec_pof
from scipy import stats

from finresearch.portfolio import analytics_math as am


def returns_with(hits) -> tuple[np.ndarray, np.ndarray]:
    h = np.asarray(hits, dtype=bool)
    return np.where(h, -2.0, 0.0), np.ones(len(h))


def close(a: float, b: float) -> float:
    """|a - b| in units of the tolerance 1e-10 (relative) + 1e-10 (absolute)."""
    return abs(a - b) / (1e-10 * abs(b) + 1e-10)


def test_kupiec_matches_arch(oracle):
    """Every count from none to all on short samples, and Fibonacci-spaced counts up to n on long ones: the edges
    x = 0 and x = n are where 0 x ln 0 lives."""
    worst_lr, worst_p, n_cases = (0.0, None), (0.0, None), 0
    for n, p in itertools.product([20, 50, 250, 500, 1000], [0.01, 0.025, 0.05, 0.10]):
        xs = (
            range(n + 1) if n <= 50 else sorted({0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, n // 2, n - 1, n})
        )
        for x in xs:
            n_cases += 1
            ref = kupiec_pof(*returns_with([1] * x + [0] * (n - x)), p=p)
            lr, pv = am.kupiec(n, x, p)
            worst_lr = max(
                worst_lr, (close(lr, float(ref.stat)), (n, x, p, lr, float(ref.stat))), key=lambda t: t[0]
            )
            worst_p = max(
                worst_p, (close(pv, float(ref.pval)), (n, x, p, pv, float(ref.pval))), key=lambda t: t[0]
            )
    oracle.check("analytics_math.kupiec (LR)", "arch kupiec_pof", grid=n_cases, max_diff=worst_lr[0], limit=1.0,
                 why="x (1e-10 relative + 1e-10 absolute): float rounding", worst=worst_lr[1])  # fmt: skip
    oracle.check("analytics_math.kupiec (p-value)", "arch kupiec_pof", grid=n_cases, max_diff=worst_p[0], limit=1.0,
                 why="x (1e-10 relative + 1e-10 absolute): float rounding", worst=worst_p[1])  # fmt: skip


def hit_sequences():
    rng = np.random.default_rng(503)
    for n, rate in itertools.product([50, 250, 1000], [0.01, 0.05, 0.15, 0.30]):
        for _ in range(8):
            yield (rng.random(n) < rate).astype(int).tolist()
        # clustered exceptions: a hit is followed by another with probability 0.6
        seq, prev = [], 0
        for _ in range(n):
            prev = int(rng.random() < (0.6 if prev else rate))
            seq.append(prev)
        yield seq
    yield [0] * 60  # no exception
    yield [1] * 60  # only exceptions
    yield [0, 1] * 30  # never two in a row, never two calm days in a row
    yield [0] * 59 + [1]  # one exception, on the last day (it has no successor)
    yield [1] + [0] * 59  # one exception, on the first day
    yield [0] * 20 + [1, 1] + [0] * 38  # one pair
    yield [1, 0, 0] * 20  # isolated exceptions (n11 = 0)
    yield [1, 1, 0] * 20


def test_christoffersen_independence_matches_arch(oracle):
    worst, n_cases = (0.0, None), 0
    for hits in hit_sequences():
        n_cases += 1
        ref = float(christoffersen_independence(*returns_with(hits)).stat)
        got = am.christoffersen_ind(hits)
        worst = max(worst, (close(got, ref), (len(hits), sum(hits), got, ref)), key=lambda t: t[0])
    oracle.check("analytics_math.christoffersen_ind (LR)", "arch christoffersen_independence", grid=n_cases,
                 max_diff=worst[0], limit=1.0, why="x (1e-10 relative + 1e-10 absolute): float rounding",
                 worst=worst[1])  # fmt: skip


def test_chi2_sf_matches_scipy_into_the_far_tail(oracle):
    """The module's closed forms (erfc for 1 degree of freedom, exp for 2) against scipy's regularised gamma, from
    0 out to where the tail leaves double precision (about x = 1400). Relative: a p-value of 1e-200 must still be a
    p-value of 1e-200, not 0."""
    worst, n_cases = (0.0, None), 0
    xs = [
        0.0,
        1e-12,
        1e-6,
        0.01,
        0.5,
        1.0,
        2.706,
        3.841,
        6.635,
        10.83,
        20.0,
        50.0,
        100.0,
        300.0,
        700.0,
        1000.0,
        1400.0,
    ]
    for df, x in itertools.product([1, 2], xs):
        ref = float(stats.chi2.sf(x, df))
        if ref < 1e-300:
            continue
        n_cases += 1
        worst = max(worst, (abs(am.chi2_sf(x, df) / ref - 1), (df, x, ref)), key=lambda t: t[0])
    oracle.check("analytics_math.chi2_sf (df 1, 2)", "scipy.stats.chi2.sf", grid=n_cases, max_diff=worst[0],
                 limit=1e-12, why="relative (float rounding of erfc / exp)", worst=worst[1])  # fmt: skip
