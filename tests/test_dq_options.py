"""Option-price screens on stored settles (#368, P1.6): no-arbitrage bounds, monotonicity and convexity in strike,
put-call parity and calendar total variance (dq.options). Pure tests on made-up chains (SYNTHIDX / SYNTHSTK, priced
with Black-76), hand-derived tolerances, a noisy clean day that must raise nothing, and one planted fault per kind with
the exact count it must produce. No database here (test_dq_options_db.py covers the stored-row wiring)."""

from __future__ import annotations

import math
from datetime import date

import pytest

from finresearch.fincalc import black76

EXP1, EXP2 = date(2026, 11, 10), date(2026, 12, 8)
NAMES = ("opt_bounds", "opt_strike_monotone", "opt_butterfly", "opt_parity", "opt_calendar")


def tick(p: float) -> float:
    return round(round(p / 0.05) * 0.05, 2)


def chain(symbol="SYNTHIDX", expiry=EXP1, dte=14, fwd=20000.0, strikes=None, sigma=0.15, skew=0.2, rate=0.07,
          future="same", jitter=0.0, edit=None):  # fmt: skip
    """A Black-76 chain at a skewed smile sigma(K) = sigma - skew ln(K/F); `jitter` prices strike i at a forward moved by
    +/- jitter (alternating sign: the trades behind two settles at different times); `edit` {(K, right): price}."""
    from finresearch.dq import options as op

    t = dte / 365
    df = math.exp(-rate * t)
    ks = strikes or [round(fwd * (0.925 + 0.005 * i), 2) for i in range(31)]
    out = []
    for i, k in enumerate(ks):
        f = fwd * (1 + jitter * (1 if i % 2 else -1))
        for right in ("call", "put"):
            vol = sigma - skew * math.log(k / fwd)
            out.append(op.Opt(k, right, tick(black76.price(right, f, k, t, df, vol))))
    out = [op.Opt(o.strike, o.right, (edit or {}).get((o.strike, o.right), o.price)) for o in out]
    return op.Chain(symbol, expiry, dte, tuple(out), fwd if future == "same" else future)


def counts(res) -> dict[str, int]:
    return {k: len(res.found[k]) for k in NAMES}


def test_delta_at_the_options_own_vol_and_one_without_a_vol():
    from finresearch.dq import options as op

    # F = K = 100, T = 1, sigma 20 %, DF 1: d1 = 0.1, call = 100 (N(0.1) - N(-0.1)) = 7.96557; N(0.1) = 0.5398278
    call = 100 * (0.5398278372770290 - 0.4601721627229710)
    assert op.option_delta(op.Opt(100.0, "call", call), 100.0, 1.0) == pytest.approx(0.5398278, abs=1e-6)
    assert op.option_delta(op.Opt(100.0, "put", call), 100.0, 1.0) == pytest.approx(0.4601722, abs=1e-6)
    # a call below its intrinsic value (10) has no implied volatility: the largest delta, 1, never a smaller one
    assert op.option_delta(op.Opt(90.0, "call", 5.0), 100.0, 1.0) == 1.0


def test_tolerance_is_two_ticks_plus_the_forward_noise_at_each_price_delta():
    from finresearch.dq import options as op

    # 0.10 + 0.005 x 1000 x (|1 x 0.5| + |-1 x 0.25|) = 0.10 + 3.75
    assert op.tolerance(1000.0, [(1.0, 0.5), (-1.0, 0.25)]) == pytest.approx(3.85)


@pytest.mark.parametrize("price,flagged", [(90.97, True), (90.99, False)])
def test_lower_bound_at_the_lowest_discount_factor_with_a_hand_derived_tolerance(price, flagged):
    from finresearch.dq import options as op

    # T = 73/365 = 0.2: the weakest lower bound is at DF = e^(-0.20 x 0.2) = 0.9607894 -> 96.07894 for a 900 call on
    # F = 1000; the price has no IV, so |delta| = 1 and tol = 0.10 + 0.005 x 1000 = 5.10. 90.97 is 5.10894 short.
    c = op.Chain("SYNTHSTK", EXP1, 73, (op.Opt(900.0, "call", price),), 1000.0)
    res = op.screen([c])
    assert (len(res.found["opt_bounds"]), res.tested["opt_bounds"]) == (1 if flagged else 0, 1)


@pytest.mark.parametrize("price,flagged", [(1006.0, False), (1010.0, True)])
def test_upper_bound_at_the_highest_discount_factor(price, flagged):
    from finresearch.dq import options as op

    # a put is worth at most DF x K; the weakest is DF = e^(+0.02 x 0.2) = 1.0040080 -> 1004.008; 1010 is 5.992 over
    c = op.Chain("SYNTHSTK", EXP1, 73, (op.Opt(1000.0, "put", price),), 1000.0)
    assert len(op.screen([c]).found["opt_bounds"]) == (1 if flagged else 0)


def test_noisy_clean_day_raises_nothing():
    """False-alarm guard: two expiries of a skewed Black-76 surface on ticks, each strike's settle taken at a forward
    0.25 % up or down (half the noise the screens forgive), the second expiry on a parity forward 0.1 % off its future."""
    from finresearch.dq import options as op

    near = chain(jitter=0.0025)
    far = chain(expiry=EXP2, dte=42, sigma=0.16, jitter=0.0025, future=20000.0 * 1.001)
    res = op.screen([near, far])
    assert counts(res) == dict.fromkeys(NAMES, 0), {k: res.found[k][:3] for k in NAMES}
    # 31 strikes x 2 rights x 2 expiries priced; OTM pairs and triples on each side of the ATM strike (20000: 15 below,
    # 15 above): 15 + 15 pairs and 14 + 14 triples per expiry; parity on the 20 strikes within 5 % of F (19,100 to
    # 21,000: e^-0.05 x 20,000 = 19,024.6, e^0.05 x 20,000 = 21,025.4) plus one basis test per expiry; one calendar pair
    assert res.tested == {"opt_bounds": 124, "opt_strike_monotone": 60, "opt_butterfly": 56, "opt_parity": 42,
                          "opt_calendar": 1}  # fmt: skip


def only(**kw) -> dict[str, int]:
    return {k: kw.get(k, 0) for k in NAMES}


def price_of(c, k, right) -> float:
    return next(o.price for o in c.options if o.strike == k and o.right == right)


def test_put_below_intrinsic_outside_the_parity_band_is_a_bounds_fault_only():
    from finresearch.dq import options as op

    # the 21,500 put is in the money by 1,500 (x DF >= 1,488) and outside the 5 % band: 1,000 is 488 short, far over
    # the largest tolerance 0.10 + 0.005 x 20,000 x 1 = 100.1; it is on the in-the-money side, so no strike sequence
    res = op.screen([chain(edit={(21500.0, "put"): 1000.0})])
    assert counts(res) == only(opt_bounds=1)
    assert "21500 PE" in res.found["opt_bounds"][0]


def test_far_call_above_its_lower_strike_is_a_monotonicity_fault_only():
    from finresearch.dq import options as op

    c = chain()
    bad = price_of(c, 21400.0, "call") + 500  # 500 over the next lower strike: tol <= 0.10 + 100 x 2 = 200.1
    res = op.screen([chain(edit={(21500.0, "call"): bad})])
    assert counts(res) == only(opt_strike_monotone=1)  # the top strike: no triple has it in the middle


def test_call_above_its_chord_is_a_convexity_fault_only():
    from finresearch.dq import options as op

    # a stock chain, F 1,000, strikes 50 apart: C(1,100) raised to C(1,050) keeps the calls non-increasing but puts
    # C(1,100) (C(1,050) - C(1,150)) / 2 over the chord of its neighbours, about 7 against a tolerance near 2
    kw = {"symbol": "SYNTHSTK", "dte": 28, "fwd": 1000.0, "strikes": [700.0 + 50 * i for i in range(13)],
          "sigma": 0.30, "skew": 0.1}  # fmt: skip
    c = chain(**kw)
    up = price_of(c, 1050.0, "call")
    assert (up - price_of(c, 1150.0, "call")) / 2 > 6  # the planted break, hand-checked against the fixture
    res = op.screen([chain(**kw, edit={(1100.0, "call"): up})])
    assert counts(res) == only(opt_butterfly=1)


def test_options_off_a_forward_one_percent_from_the_future_are_a_parity_fault_only():
    from finresearch.dq import options as op

    res = op.screen([chain(future=20200.0)])  # every option priced off 20,000; the future settles 1 % higher
    assert counts(res) == only(opt_parity=1)
    assert "basis" in res.found["opt_parity"][0]


def test_far_expiry_below_the_near_total_variance_is_a_calendar_fault_only():
    from finresearch.dq import options as op

    # sigma^2 T: near 0.25^2 x 14/365 = 0.002397, far 0.10^2 x 42/365 = 0.001151; slack 2 x 0.01 x (0.25 x 14/365 +
    # 0.10 x 42/365) = 0.000422, so the far expiry is 0.001247 short (DF range moves each sigma by well under 1 %)
    near = chain(sigma=0.25, skew=0.0)
    far = chain(expiry=EXP2, dte=42, sigma=0.10, skew=0.0)
    assert counts(op.screen([near, far])) == only(opt_calendar=1)
    assert counts(op.screen([near, chain(expiry=EXP2, dte=42, sigma=0.25, skew=0.0)])) == only()


def test_calendar_skips_an_expiry_inside_a_week():
    from finresearch.dq import options as op

    near = chain(dte=3, sigma=0.60, skew=0.0)  # 3 days out: ATM IVs this close to expiry are not compared
    res = op.screen([near, chain(expiry=EXP2, dte=42, sigma=0.10, skew=0.0)])
    assert res.tested["opt_calendar"] == 0 and counts(res) == only()


def test_call_put_label_swap_shows_in_every_strike_screen():
    from finresearch.dq import options as op

    # at 19,500 (in the band, put side) the in-the-money call and the out-of-the-money put trade places: the "call"
    # sits ~400 under intrinsic (bounds), the "put" breaks both of its put pairs (19,400 slope, 19,600 order) and the
    # 19,400/19,500/19,600 chord, and C - P at 19,500 is ~1,000 off the fitted line (parity)
    c = chain()
    swap = {(19500.0, "call"): price_of(c, 19500.0, "put"), (19500.0, "put"): price_of(c, 19500.0, "call")}
    res = op.screen([chain(edit=swap)])
    assert counts(res) == only(opt_bounds=1, opt_strike_monotone=2, opt_butterfly=1, opt_parity=1)


def test_nothing_testable_is_unknown_never_ok():
    from finresearch.dq import options as op

    lone = op.Chain("SYNTHSTK", EXP1, 14, (op.Opt(1000.0, "call", 20.0), op.Opt(1000.0, "put", 19.0)), None)
    for chains, why in (([], "no market-settled option"), ([lone], "no forward")):
        checks = op.to_checks(op.screen(chains))
        assert {c.check: c.status for c in checks} == dict.fromkeys(NAMES, "unknown")
        assert all(why in (c.note or "") and not c.critical and c.dataset == "fno_bhav" for c in checks)


def test_checks_carry_counts_and_examples():
    from finresearch.dq import options as op

    checks = {c.check: c for c in op.to_checks(op.screen([chain(edit={(21500.0, "put"): 1000.0})]))}
    b = checks["opt_bounds"]
    assert (b.status, b.value, b.threshold, b.critical) == ("warn", 1, op.NOISE, False)
    assert b.note.startswith("1 of 62 ") and "SYNTHIDX 10-Nov-2026 21500 PE" in b.note
    assert (checks["opt_butterfly"].status, checks["opt_butterfly"].value) == ("ok", 0)
