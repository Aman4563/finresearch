"""Model-free implied variance (P3.6, #389; fincalc/mfiv.py) against the two published worked examples: Cboe's VIX
sample calculation (27-Sep-2022, methodology v6.0) from its raw bid/ask quotes, and NSE's India VIX white paper
example; plus a flat-volatility check, the sparse-strike refusals, the common-range truncation and the 30-day
combination when both terms are beyond 30 days (the weight on the far term turns negative)."""

import json
import math
from pathlib import Path

import pytest

from finresearch.fincalc import black76 as b76
from finresearch.fincalc import mfiv

CBOE = json.loads((Path(__file__).parent / "fixtures/vix/cboe_2022-09-27.json").read_text())
# Cboe's sample: T1 = 34,484 / 525,600, T2 = 44,954 / 525,600; R1 = 0.031664 %, R2 = 0.028797 % (Appendix 3)
T1, T2 = 34_484 / 525_600, 44_954 / 525_600
R1, R2 = 0.00031664, 0.00028797


def cboe_strikes(term: str) -> list[mfiv.Strike]:
    """Mid prices; a zero bid is 'no market price' (None), as Cboe excludes it."""
    out = []
    for k, cb, ca, pb, pa in CBOE[term]:
        out.append(mfiv.Strike(k, (cb + ca) / 2 if cb > 0 else None, (pb + pa) / 2 if pb > 0 else None))
    return out


def test_cboe_forward_k0_and_selection_match_the_published_example() -> None:
    near = mfiv.term_variance(cboe_strikes("near"), T1, R1, min_per_side=1, max_gap=1.0)
    nxt = mfiv.term_variance(cboe_strikes("next"), T2, R2, min_per_side=1, max_gap=1.0)
    # F1 = 1965 + e^(RT)(21.05 - 23.15) = 1962.89996; F2 = 1962.40006 (the paper rounds R to 0.000317 / 0.000288)
    assert near.forward == pytest.approx(1962.89996, abs=1e-4)
    assert nxt.forward == pytest.approx(1962.40006, abs=1e-4)
    assert near.k0 == 1960 and nxt.k0 == 1960
    ks = [k for k, _ in near.strip]
    # puts: 1360 and 1365 have no bid, so the walk stops there (1350 and 1355 are never considered)
    assert ks[0] == 1370 and 1365 not in ks and 1355 not in ks
    # calls: 2120 and 2150 skipped singly, the walk stops at 2175 + 2200 (2225 is never considered)
    assert ks[-1] == 2125 and 2120 not in ks and 2225 not in ks
    assert dict(near.strip)[1960] == pytest.approx((24.25 + 21.30) / 2)  # K0: the mean of the call and put
    assert (len(near.strip), len(nxt.strip)) == (146, 122)


def test_cboe_term_variances_and_vix_match_the_published_values() -> None:
    near = mfiv.term_variance(cboe_strikes("near"), T1, R1, min_per_side=1, max_gap=1.0)
    nxt = mfiv.term_variance(cboe_strikes("next"), T2, R2, min_per_side=1, max_gap=1.0)
    # published: sigma1^2 = 0.019267 - 0.00003337 = 0.019233906; sigma2^2 = 0.019441 - 0.00001753 = 0.019423884
    assert near.value == pytest.approx(0.019233906, abs=1e-9)
    assert nxt.value == pytest.approx(0.019423884, abs=1e-9)
    v = mfiv.thirty_day(near.value, 34_484, nxt.value, 44_954)
    assert 100 * math.sqrt(v) == pytest.approx(13.93, abs=0.005)  # published VIX = 100 x 0.13927842 = 13.93
    # with the published single-term values the combination is exact
    assert math.sqrt(mfiv.thirty_day(0.019233906, 34_484, 0.019423884, 44_954)) == pytest.approx(
        0.13927842, abs=5e-8
    )


# NSE, India VIX white paper (2014), section 5: the near-month Q(K) after the spline step; F1 = 5129 (the near
# future), K0 = 5100, T1 = (510 + 930 + 11,520) / 525,600, R1 = 3.90 %; published sigma1^2 = 0.072979.
NSE_NEAR = [(3800, 0.45), (3900, 0.6), (4000, 0.78), (4100, 0.96), (4200, 1.1), (4300, 1.23), (4400, 1.95),
            (4500, 3.38), (4600, 4.48), (4700, 7.85), (4800, 13.3), (4900, 22.58), (5000, 40.45)]  # fmt: skip
NSE_NEAR_CALLS = [(5200, 34.88), (5300, 11.53), (5400, 3.63), (5500, 1.83), (5600, 1.18)]
NSE_NEXT = [(4000, 6.20), (4100, 7.83), (4200, 10.35), (4300, 13.78), (4400, 18.38), (4500, 23.68), (4600, 32.65),
            (4700, 43.00), (4800, 61.75), (4900, 85.53), (5000, 116.50)]  # fmt: skip
NSE_NEXT_CALLS = [(5200, 120.93), (5300, 80.05), (5400, 48.30), (5500, 30.50), (5600, 17.88), (5700, 9.45)]


def _nse(puts, k0_call, k0_put, calls) -> list[mfiv.Strike]:
    return ([mfiv.Strike(k, None, p) for k, p in puts] + [mfiv.Strike(5100, k0_call, k0_put)]
            + [mfiv.Strike(k, c, None) for k, c in calls])  # fmt: skip


def test_india_vix_white_paper_term_variances() -> None:
    near = mfiv.term_variance(
        _nse(NSE_NEAR, 79.05, 74.45, NSE_NEAR_CALLS), 12_960 / 525_600, 0.039, forward=5129
    )
    nxt = mfiv.term_variance(
        _nse(NSE_NEXT, 171.30, 157.00, NSE_NEXT_CALLS), 53_280 / 525_600, 0.0465, forward=5115
    )
    assert near.k0 == 5100 and dict(near.strip)[5100] == pytest.approx(76.75)
    # the paper sums contributions rounded to 6 decimals (0.000916, 0.003600), worth up to 0.5e-6 x 19 x 2/T1
    # = 7.7e-4 and 0.5e-6 x 18 x 2/T2 = 1.8e-4; ours land within 1e-5 and 5e-5
    assert near.value == pytest.approx(0.072979, abs=1e-4)
    assert nxt.value == pytest.approx(0.070942, abs=1e-4)


def test_flat_volatility_strip_recovers_sigma_squared() -> None:
    f, t, r, sig = 20_000.0, 30 / 365, 0.065, 0.18
    df = math.exp(-r * t)
    strikes = [mfiv.Strike(k, b76.price("call", f, k, t, df, sig), b76.price("put", f, k, t, df, sig))
               for k in range(12_000, 30_001, 50)]  # fmt: skip
    out = mfiv.term_variance(strikes, t, r, forward=f)
    assert out.value == pytest.approx(sig * sig, rel=2e-3)
    # truncated at +/- 5 % the strip misses the wings: it understates, and the flat extension restores it
    narrow = [s for s in strikes if abs(math.log(s.strike / f)) <= 0.05]
    cut = mfiv.term_variance(narrow, t, r, forward=f)
    assert cut.value < 0.9 * sig * sig
    ext = mfiv.term_variance(narrow, t, r, forward=f, extend_flat=True)
    assert ext.value == pytest.approx(sig * sig, rel=5e-3)


def test_sparse_strikes_are_unknown_with_the_reason() -> None:
    f, t, r = 100.0, 30 / 365, 0.065
    df = math.exp(-r * t)
    p = lambda right, k: b76.price(right, f, k, t, df, 0.2)  # noqa: E731
    two = [mfiv.Strike(k, p("call", k), p("put", k)) for k in (98, 100, 102)]
    out = mfiv.term_variance(two, t, r, forward=f)
    assert out.value is None and "per side" in out.reason
    gappy = [
        mfiv.Strike(k, p("call", k), p("put", k)) for k in (70, 80, 90, 94, 96, 98, 100, 102, 104, 106, 120)
    ]
    out = mfiv.term_variance(gappy, t, r, forward=f)
    assert out.value is None and "gap" in out.reason
    no_k0 = [mfiv.Strike(k, p("call", k), None if k == 100 else p("put", k)) for k in range(90, 111, 2)]
    assert "K0" in mfiv.term_variance(no_k0, t, r, forward=100.5).reason
    assert mfiv.term_variance([], t, r).reason is not None


def test_common_range_truncates_and_refuses_an_uncovered_range() -> None:
    f, t, r = 100.0, 30 / 365, 0.065
    df = math.exp(-r * t)
    strikes = [mfiv.Strike(k, b76.price("call", f, k, t, df, 0.2), b76.price("put", f, k, t, df, 0.2))
               for k in range(80, 121)]  # fmt: skip
    out = mfiv.term_variance(strikes, t, r, forward=f, band=0.10)
    assert out.k_lo >= 100 * math.exp(-0.10) - 1e-9 and out.k_hi <= 100 * math.exp(0.10) + 1e-9
    assert out.value < mfiv.term_variance(strikes, t, r, forward=f).value
    out = mfiv.term_variance(strikes, t, r, forward=f, band=0.30)  # strikes reach only +/- ~20 %
    assert out.value is None and "common range" in out.reason


def test_thirty_day_extrapolates_when_both_terms_exceed_thirty_days() -> None:
    # at a monthly expiry's close the near month is often 31-35 days out: the registered formula is linear in total
    # variance through both points, so the weight on the far term is negative (stated in the PREREG)
    n1, n2 = 33 * 1440, 63 * 1440
    v = mfiv.thirty_day(0.04, n1, 0.05, n2)
    w1, w2 = (n1 / 525_600) * 0.04, (n2 / 525_600) * 0.05
    expect = (w1 * (n2 - 43_200) / (n2 - n1) + w2 * (43_200 - n1) / (n2 - n1)) * 525_600 / 43_200
    assert v == pytest.approx(expect) and (43_200 - n1) < 0
    assert mfiv.thirty_day(None, n1, 0.05, n2) is None
    assert mfiv.thirty_day(0.04, n2, 0.05, n2) is None  # one expiry twice: no interpolation
    assert mfiv.eod_minutes(9) == 12_960  # India VIX's T1 example: 510 + 930 + 11,520
