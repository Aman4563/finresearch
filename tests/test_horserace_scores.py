"""Scoring maths of the card horse race (#377). Expected values are computed by hand (in comments), not by the code
under test."""

import math

import pytest

from finresearch.horserace import scores as sc

UNIFORM_DOTS = [
    (i + 0.5) / 20 for i in range(20)
]  # the 20 dots of U(0, 1): its quantile function is q(u) = u


def test_chi2_sf_hits_the_textbook_5pct_critical_values() -> None:
    # chi-square 95 % critical values: 3.8415 (1 df), 5.9915 (2 df), 7.8147 (3 df)
    assert sc.chi2_sf(3.841458820694124, 1) == pytest.approx(0.05, abs=1e-9)
    assert sc.chi2_sf(5.991464547107979, 2) == pytest.approx(0.05, abs=1e-9)
    assert sc.chi2_sf(7.814727903251178, 3) == pytest.approx(0.05, abs=1e-9)
    assert sc.chi2_sf(0.0, 3) == 1.0


def test_murphy_decomposition_two_bins() -> None:
    # p = .05,.05 (bin 0) and .95,.95 (bin 9); o = 0,1,1,1. o-bar = .75, UNC = .1875
    # REL = (2(.05-.5)^2 + 2(.95-1)^2)/4 = .1025; RES = (2(.5-.75)^2 + 2(1-.75)^2)/4 = .0625; BS = .91/4 = .2275
    m = sc.murphy([0.05, 0.05, 0.95, 0.95], [0, 1, 1, 1], bins=10)
    assert m["brier"] == pytest.approx(0.2275)
    assert m["rel"] == pytest.approx(0.1025)
    assert m["res"] == pytest.approx(0.0625)
    assert m["unc"] == pytest.approx(0.1875)
    assert m["residual"] == pytest.approx(0.0, abs=1e-12)


def test_murphy_reports_the_within_bin_residual() -> None:
    # p = .01 and .09 share bin 0: the bin-mean forecast .05 hides spread, so BS != REL - RES + UNC
    m = sc.murphy([0.01, 0.09], [0, 1], bins=10)
    assert m["brier"] == pytest.approx((0.01**2 + 0.91**2) / 2)
    assert m["residual"] != pytest.approx(0.0, abs=1e-6)


def test_kupiec_zero_lr_at_nominal_rate() -> None:
    r = sc.kupiec(10, 100, 0.10)
    assert r["lr"] == pytest.approx(0.0, abs=1e-12) and r["p"] == pytest.approx(1.0)


def test_kupiec_twice_the_nominal_rate() -> None:
    # LR = 2[80 ln(.8/.9) + 20 ln 2] = 2(-9.422642852510677 + 13.862943611198906) = 8.880601517376458
    r = sc.kupiec(20, 100, 0.10)
    assert r["lr"] == pytest.approx(8.880601517376458)
    assert r["p"] == pytest.approx(0.002883, abs=2e-6)  # erfc(sqrt(8.8806 / 2))


def test_kupiec_no_hits_is_finite() -> None:
    # x = 0: LR = -2 n ln(1 - p) = -200 ln .9 = 21.07210313156526
    assert sc.kupiec(0, 100, 0.10)["lr"] == pytest.approx(21.07210313156526)


def test_christoffersen_independence() -> None:
    # transitions of 0011001100: n00=3 n01=2 n10=2 n11=2; lnL0 = 5 ln(5/9) + 4 ln(4/9) = -6.1826541893,
    # lnL1 = 3 ln .6 + 2 ln .4 + 4 ln .5 = -6.1376470574, LR = 0.0900142638
    r = sc.christoffersen([0, 0, 1, 1, 0, 0, 1, 1, 0, 0])
    assert r["lr"] == pytest.approx(0.0900142638, abs=1e-9)
    assert (r["n00"], r["n01"], r["n10"], r["n11"]) == (3, 2, 2, 2)


def test_quantile_function_of_uniform_dots_is_identity_with_extended_ends() -> None:
    q = sc.quantile_fn(UNIFORM_DOTS)
    for u in (0.0, 0.01, 0.1, 0.5, 0.97, 1.0):
        assert q(u) == pytest.approx(u)


def test_crps_of_uniform_matches_closed_form() -> None:
    # CRPS(U(0,1), y) = y^3/3 + (1-y)^3/3; at y = .3: .009 + .114333... = .1233333
    assert sc.crps(UNIFORM_DOTS, 0.3) == pytest.approx(0.1233333333, abs=1e-6)
    # outside the support, y = 2: E|X - 2| - E|X - X'|/2 = 1.5 - 1/6 = 1.3333333
    assert sc.crps(UNIFORM_DOTS, 2.0) == pytest.approx(4 / 3, abs=1e-6)


def test_pit_interpolates_and_clips() -> None:
    assert sc.pit(UNIFORM_DOTS, 0.3) == pytest.approx(0.3)
    assert sc.pit(UNIFORM_DOTS, -1.0) == pytest.approx(0.001)
    assert sc.pit(UNIFORM_DOTS, 5.0) == pytest.approx(0.999)


def test_var_es_of_scaled_uniform() -> None:
    dots = [-100 + 200 * (i + 0.5) / 20 for i in range(20)]  # U(-100, 100): q(u) = -100 + 200u
    var, es = sc.var_es(dots, 0.10)
    assert var == pytest.approx(-80.0)  # q(.1)
    assert es == pytest.approx(-90.0, abs=1e-6)  # (1/.1) int_0^.1 (-100 + 200u) du = -100 + 10 = -90
    assert math.isfinite(var)


def test_pit_with_a_flat_stretch_interpolates_elsewhere_and_takes_the_top_of_the_mass() -> None:
    # a credit spread's dots: 10 losses rising 0..9 then a max-profit plateau at 10 (dots 10..19)
    dots = [float(i) for i in range(10)] + [10.0] * 10
    # between dots 2 (u=.125) and 3 (u=.175): y = 2.5 -> u = .15
    assert sc.pit(dots, 2.5) == pytest.approx(0.15)
    # at the plateau the right-continuous CDF is the top of the mass; the extended end is also 10, so F(10) = 1,
    # clipped to .999
    assert sc.pit(dots, 10.0) == pytest.approx(0.999)
    # a plateau inside: dots 0..9 then 10 x5 then 11..15; F(10) = top of the 10s = u of dot 14 = .725
    inner = [float(i) for i in range(10)] + [10.0] * 5 + [float(i) for i in range(11, 16)]
    assert sc.pit(inner, 10.0) == pytest.approx(0.725)


def test_berkowitz_hand_example() -> None:
    # z = 0, 1, 2, 4: OLS of (1, 2, 4) on (0, 1, 2) gives c = 5/6, rho = 1.5, residuals 1/6, -1/3, 1/6, sigma^2 = 1/18;
    # L1 = -1.5 ln(2 pi / 18) - 1.5, L0 = -1.5 ln(2 pi) - (1 + 4 + 16) / 2, LR = 2 (L1 - L0)
    l1 = -1.5 * math.log(2 * math.pi / 18) - 1.5
    l0 = -1.5 * math.log(2 * math.pi) - 10.5
    norm_cdf = [
        0.5,
        0.8413447460685429,
        0.9772498680518208,
        0.9999683287581669,
    ]  # Phi(0), Phi(1), Phi(2), Phi(4)
    r = sc.berkowitz(norm_cdf)
    assert r["lr"] == pytest.approx(2 * (l1 - l0), rel=1e-6)
    assert r["rho"] == pytest.approx(1.5, rel=1e-6)
    assert r["p"] == pytest.approx(sc.chi2_sf(r["lr"], 3))


def test_berkowitz_needs_enough_points() -> None:
    assert sc.berkowitz([0.3, 0.6])["p"] is None


def test_acerbi_szekely_z2_hand_example() -> None:
    dots = [-100 + 200 * (i + 0.5) / 20 for i in range(20)]  # VaR(.1) = -80, ES = -90
    # only -95 breaches: Z2 = 1 - (-95) / (2 x .1 x -90) = 1 - 95/18 = -4.277777...
    r = sc.acerbi_szekely_z2([dots, dots], [-95.0, 50.0], alpha=0.10, sims=2000, seed=1)
    assert r["z2"] == pytest.approx(1 - 95 / 18)
    assert r["excluded"] == 0 and r["n"] == 2
    assert 0.0 <= r["p"] <= 0.2  # a breach 5/18 x larger than the tail mean is rare under the model
    assert r == sc.acerbi_szekely_z2(
        [dots, dots], [-95.0, 50.0], alpha=0.10, sims=2000, seed=1
    )  # deterministic


def test_acerbi_szekely_excludes_events_with_a_non_negative_tail_mean() -> None:
    pos = [10.0 + i for i in range(20)]  # every quantile is a profit: ES >= 0, the ratio is undefined
    r = sc.acerbi_szekely_z2([pos], [5.0], alpha=0.10, sims=100, seed=1)
    assert r["excluded"] == 1 and r["n"] == 0 and r["z2"] is None


def test_ols_clustered_hand_example() -> None:
    # y = a + b x on x = 0..3, y = 0, 1, 1, 3, clusters AABB: a = -0.1, b = 0.9; CR1 V = 3 x [[.045, -.015],
    # [-.015, .005]] -> se(a) = sqrt(.135), se(b) = sqrt(.015); t(1 df) 97.5 % = 12.706204736
    r = sc.ols_clustered([0, 1, 2, 3], [0, 1, 1, 3], ["A", "A", "B", "B"])
    assert r["a"] == pytest.approx(-0.1) and r["b"] == pytest.approx(0.9)
    assert r["se_a"] == pytest.approx(math.sqrt(0.135)) and r["se_b"] == pytest.approx(math.sqrt(0.015))
    assert r["clusters"] == 2 and r["t_crit"] == pytest.approx(12.706204736, rel=1e-6)
    assert r["ci_b"][0] == pytest.approx(0.9 - 12.706204736 * math.sqrt(0.015), rel=1e-6)


def test_ev_calibration_verdict() -> None:
    r = sc.ev_calibration([0, 1, 2, 3], [0, 1, 1, 3], ["A", "A", "B", "B"])
    assert r["calibrated"] is True  # CI(b) = 0.9 +- 1.556 holds 1, CI(a) holds 0
    assert sc.ev_calibration([1, 2], [1, 2], ["A", "B"])["calibrated"] is None  # too few: unknown, never True


def test_gate_information_delta() -> None:
    # passes: 10, 20 (expiry A, B); fails: -5, 5 (A, B): delta = 15 - 0 = 15
    r = sc.gate_information([True, True, False, False], [10.0, 20.0, -5.0, 5.0], ["A", "B", "A", "B"])
    assert r["delta"] == pytest.approx(15.0) and r["n_pass"] == 2 and r["n_fail"] == 2


def test_multinomial_log_score() -> None:
    # observed reasons get p = .5 and .25: mean -ln p = (ln 2 + ln 4) / 2
    probs = [{"target": 0.5, "stop": 0.5}, {"target": 0.75, "stop": 0.25}]
    assert sc.multinomial_log_score(probs, ["target", "stop"]) == pytest.approx(
        (math.log(2) + math.log(4)) / 2
    )


def test_band_coverage_binomial() -> None:
    # 10 of 10 inside a 50 % band: two-sided exact p = 2 x .5^10 = .001953125
    r = sc.band_coverage([0.0] * 10, [1.0] * 10, [0.5] * 10, nominal=0.5)
    assert r["inside"] == 10 and r["p"] == pytest.approx(0.001953125)
