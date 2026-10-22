"""Range and realised-variance estimators, losses and HAR (P3.7, #390; fincalc/realvol.py). Expected values are
computed independently: by hand from the published formulas (one bar O 100, H 110, L 95, C 105, previous close 98,
evaluated in a separate Python session and pinned here), by exact recovery on noise-free synthetic series, or by the
estimators' known unbiasedness on simulated Brownian paths."""

import math
from datetime import date, timedelta

import numpy as np
import pytest

from finresearch.fincalc import realvol as rv

BAR = rv.Bar(date(2025, 1, 2), 100.0, 110.0, 95.0, 105.0, 98.0)


# ----------------------------------------------------------------------------------------------- one bar
def test_parkinson_garman_klass_rogers_satchell_match_the_published_formulas_by_hand() -> None:
    # Parkinson 1980: (ln H/L)^2 / (4 ln 2); Garman-Klass 1980: 0.5 (ln H/L)^2 - (2 ln 2 - 1)(ln C/O)^2;
    # Rogers-Satchell 1991: ln(H/C) ln(H/O) + ln(L/C) ln(L/O)
    assert rv.parkinson(BAR) == pytest.approx(0.007751809156810511, rel=1e-12)
    assert rv.garman_klass(BAR) == pytest.approx(0.00982672327557351, rel=1e-12)
    assert rv.rogers_satchell(BAR) == pytest.approx(0.009567441357749173, rel=1e-12)
    assert rv.overnight(BAR) == pytest.approx(math.log(100 / 98))
    assert rv.close_to_close(BAR) == pytest.approx(math.log(105 / 98))
    assert rv.daily_variance(BAR) == pytest.approx(math.log(100 / 98) ** 2 + 0.009567441357749173)


@pytest.mark.parametrize(
    ("bar", "why"),
    [
        (rv.Bar(date(2025, 1, 2), 100.0, 110.0, 95.0, 0.0, 98.0), "close"),  # a zero close is unknown, not dropped
        (rv.Bar(date(2025, 1, 2), None, 110.0, 95.0, 105.0, 98.0), "open"),
        (rv.Bar(date(2025, 1, 2), 100.0, 104.0, 95.0, 105.0, 98.0), "high"),  # H below C
        (rv.Bar(date(2025, 1, 2), 100.0, 110.0, 101.0, 105.0, 98.0), "low"),  # L above O
        (rv.Bar(date(2025, 1, 2), 100.0, 110.0, 95.0, 105.0, None), "previous close"),
    ],
)
def test_an_invalid_bar_is_unknown_with_a_reason(bar: rv.Bar, why: str) -> None:
    assert why in (rv.bar_problem(bar, need_prev=True) or "")
    assert rv.rogers_satchell(bar) is None or why == "previous close"
    assert rv.daily_variance(bar) is None
    assert rv.yang_zhang([BAR, bar, BAR]) is None  # a window holding an invalid bar is unknown


def test_yang_zhang_k_and_the_window_estimator_match_yang_and_zhang_2000() -> None:
    # k = 0.34 / (1.34 + (n + 1) / (n - 1)) (Yang and Zhang 2000, J. Business 73, 477-491, eq. 9 with alpha = 1.34)
    assert rv.yz_k(5) == pytest.approx(0.11971830985915495, rel=1e-12)
    assert rv.yz_k(22) == pytest.approx(0.1396167383652718, rel=1e-12)
    bars = [rv.Bar(date(2025, 1, 2 + i), o, h, lo, c, p) for i, (o, h, lo, c, p) in enumerate(
        [(100, 103, 99, 102, 101), (102.5, 104, 100, 101, 102), (100.5, 101, 97, 98, 101), (98.2, 99.5, 97.5, 99, 98)])]  # fmt: skip
    on = [math.log(b.open / b.prev_close) for b in bars]
    oc = [math.log(b.close / b.open) for b in bars]
    rs = [math.log(b.high / b.close) * math.log(b.high / b.open) + math.log(b.low / b.close) * math.log(b.low / b.open)
          for b in bars]  # fmt: skip
    k = 0.34 / (1.34 + 5 / 3)
    want = np.var(on, ddof=1) + k * np.var(oc, ddof=1) + (1 - k) * np.mean(rs)
    assert rv.yang_zhang(bars) == pytest.approx(want, rel=1e-12)
    assert rv.yang_zhang(bars[:1]) is None  # needs two bars
    cc = [math.log(b.close / b.prev_close) for b in bars]
    assert rv.close_to_close_variance(bars) == pytest.approx(np.var(cc, ddof=1), rel=1e-12)


def test_range_estimators_are_unbiased_on_simulated_brownian_bars_and_rogers_satchell_ignores_drift() -> None:
    rng = np.random.default_rng(390)
    sigma2, steps, days = 1e-4, 2000, 1500

    def bars(mu: float) -> list[rv.Bar]:
        inc = rng.normal(mu / steps, math.sqrt(sigma2 / steps), (days, steps))
        paths = np.concatenate([np.zeros((days, 1)), np.cumsum(inc, axis=1)], axis=1)
        d0 = date(2020, 1, 1)
        return [rv.Bar(d0 + timedelta(i), 100.0, 100 * math.exp(p.max()), 100 * math.exp(p.min()),
                       100 * math.exp(p[-1]), 100.0) for i, p in enumerate(paths)]  # fmt: skip

    flat = bars(0.0)
    for f in (rv.parkinson, rv.garman_klass, rv.rogers_satchell):
        # a discrete path under-reads the range by about 0.58 sqrt(1/steps) (Broadie and Glasserman 1997): about 2-4 % low at 2,000 steps
        assert np.mean([f(b) for b in flat]) == pytest.approx(sigma2, rel=0.06), f.__name__
    drift = bars(0.01)  # a 1 % daily drift: 100x the variance's square root
    assert np.mean([rv.rogers_satchell(b) for b in drift]) == pytest.approx(sigma2, rel=0.06)
    assert np.mean([rv.parkinson(b) for b in drift]) > 1.3 * sigma2  # Parkinson reads the drift as variance
