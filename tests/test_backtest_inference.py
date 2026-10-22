"""Inference recalibration (#381, P2.6). Every expected value is worked out by hand in the comment beside it."""

from __future__ import annotations

from itertools import pairwise

import pytest

from finresearch.backtest import evaluate, inference, metrics


def _win(daily_sr: float | None) -> dict:
    return {"trades": 40, "expectancy": 10.0, "t_stat": 0.5, "ci_low": -5.0, "max_drawdown_pct": 5.0,
            "daily_sr": daily_sr, "days": 253, "skew": 0.0, "kurtosis": 3.0}  # fmt: skip


def _row(key: str, daily_sr: float | None) -> dict:
    return {"key": key, "role": "candidate", "status": None,
            "windows": {"is": _win(daily_sr), "oos": _win(daily_sr), "oos_stress": _win(daily_sr)}}  # fmt: skip


def test_a_zero_daily_sharpe_enters_the_trials_variance():
    # SRs 0, 0.1, -0.1: mean 0, sample variance (0 + 0.01 + 0.01) / 2 = 0.01. Dropping the 0 (the old truthiness
    # test) gives (0.01 + 0.01) / 1 = 0.02.
    rows = [_row("a", 0.0), _row("b", 0.1), _row("c", -0.1)]
    evaluate._finish(rows, True, evaluate.CRITERIA)
    assert rows[0]["windows"]["oos"]["sr_trials_variance"] == pytest.approx(0.01)


# --------------------------------------------------------------------------- deflated Sharpe (Bailey & López de Prado)
def test_deflated_sharpe_reproduces_the_papers_numerical_example():
    # Bailey & López de Prado (2014), "A numerical example": annualised SR 2.5 over 5 years of daily returns
    # (250 a year, T = 1,250), N = 100, V[{SR}] = 1/2 annualised, skew -3, kurtosis 10 -> DSR 0.9004; with N = 46,
    # 0.9505; with Normal returns and N = 88, 0.9505 (eq. 1 for SR0, eq. 2 for DSR).
    sr, v = 2.5 / 250**0.5, 0.5 / 250
    assert metrics.deflated_sharpe(sr, 1250, -3.0, 10.0, 100, v) == pytest.approx(0.9004, abs=5e-5)
    assert metrics.deflated_sharpe(sr, 1250, -3.0, 10.0, 46, v) == pytest.approx(0.9505, abs=5e-5)
    assert metrics.deflated_sharpe(sr, 1250, 0.0, 3.0, 88, v) == pytest.approx(0.9505, abs=5e-5)


def test_sr_variance_is_the_dsr_denominator_over_t_minus_1():
    # (1 - g3 SR + (g4 - 1)/4 SR^2) / (T - 1): SR 0.1, g3 -1, g4 6 -> (1 + 0.1 + 1.25 * 0.01) / 99 = 0.0112374
    assert inference.sr_variance(0.1, 100, -1.0, 6.0) == pytest.approx(1.1125 / 99)
    assert inference.sr_variance(0.0, 570, 0.0, 3.0) == pytest.approx(1 / 569)  # V_null
    assert inference.sr_variance(0.1, 1, 0.0, 3.0) is None  # T < 2: unknown


def _bar(t: int, n: int, v: float) -> float:
    """The annualised Sharpe at which DSR reaches 0.95 (Normal returns, so the denominator is ~1)."""
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if metrics.deflated_sharpe(mid, t, 0.0, 3.0, n, v) < 0.95 else (lo, mid)
    return lo * 252**0.5


def test_v_null_bars_quoted_in_the_issue():
    # V_null = 1/(T-1). T = 570, N = 8: SR0 = sqrt(1/569) * 1.45880 = 0.061156 daily, plus 1.645/sqrt(569) = 0.06896
    # -> 0.1301 daily = 2.07 annualised ("about 2.0-2.2"); T = 3,400: 0.85 (N = 8) and 1.01 (N = 30).
    assert _bar(570, 8, 1 / 569) == pytest.approx(2.07, abs=0.02)
    assert _bar(3400, 8, 1 / 3399) == pytest.approx(0.85, abs=0.02)
    assert _bar(3400, 30, 1 / 3399) == pytest.approx(1.01, abs=0.02)


def test_v_cand_from_the_published_sharpes_gives_the_issues_bar():
    # RESULTS.md OOS Sharpes (annualised) -1.00, 0.24, 0.10, 0.44, -2.62, 1.07: sd of the daily SRs 0.0833, and with
    # N = 8 the bar is about 3.0-3.3 annualised at T ~ 570 (the issue's "mis-scaled" figure)
    srs = [x / 252**0.5 for x in (-1.00, 0.24, 0.10, 0.44, -2.62, 1.07)]
    m = sum(srs) / 6
    v = sum((x - m) ** 2 for x in srs) / 5
    assert v**0.5 == pytest.approx(0.0833, abs=5e-4)
    assert 3.0 <= _bar(570, 8, v) <= 3.3


def test_dsr_v2_rules_and_trial_count():
    srs = [x / 252**0.5 for x in (-1.00, 0.24, 0.10, 0.44, -2.62, 1.07)]
    w = {"daily_sr": 0.1, "days": 571, "skew": 0.0, "kurtosis": 3.0}  # T = 570 daily returns
    cand = inference.dsr_v2(w, srs, rule="v_cand")
    null = inference.dsr_v2(w, srs, rule="v_null")
    both = inference.dsr_v2(w, srs, rule="max")
    assert cand["n_trials"] == null["n_trials"] == 6  # N = the trials whose SR enters V
    assert null["v_used"] == pytest.approx(1 / 569)
    assert cand["v_used"] == pytest.approx(0.0833**2, rel=0.02)
    assert both["v_used"] == max(cand["v_used"], null["v_used"])
    # same N and T: DSR falls as V rises
    assert null["dsr"] > cand["dsr"] and both["dsr"] == cand["dsr"]
    assert cand["dsr"] == pytest.approx(metrics.deflated_sharpe(0.1, 570, 0.0, 3.0, 6, cand["v_used"]))
    assert inference.dsr_v2({**w, "skew": None}, srs, rule="max")["dsr"] is None  # unknown moments: unknown
    assert inference.dsr_v2(w, srs[:1], rule="v_cand")["dsr"] is None  # one trial: V_cand unknown


# --------------------------------------------------------------------------- Lo (2002) and the posterior Sharpe
def test_lo_eta_matches_the_ar1_closed_form():
    # Lo (2002) eq. 22: AR(1) with rho = 0.2, q = 12 -> sqrt(12) * (1 + 2*0.2/0.8 * (1 - (1 - 0.2^12)/(12*0.8)))^-1/2
    # = 3.4641 / sqrt(1.447917) = 2.879 ("3.46 in the IID case and 2.88", p. 41); eq. 20 with rho_k = 0.2^k is the same
    rhos = [0.2**k for k in range(1, 12)]
    assert inference.lo_eta(rhos, 12) == pytest.approx(2.8789, abs=1e-4)
    assert inference.lo_eta([0.0] * 11, 12) == pytest.approx(12**0.5)
    assert inference.lo_eta([-0.6] * 11, 12) is None  # q + 2 sum (q - k) rho_k <= 0: unknown


def test_lo_sharpe_uses_the_sample_autocorrelations_up_to_the_lag_cap():
    # returns 1, -1, 1, -1 ...: rho_1 = -(n-1)/n with gamma_k / gamma_0 over T (Lo eq. A17); with max_lag 1 only rho_1
    # enters: eta = q / sqrt(q + 2 (q - 1) rho_1)
    r = [0.01 + (0.01 if i % 2 else -0.01) for i in range(100)]
    rho1 = -99 / 100
    q = 2  # q + 2 (q - 1) rho_1 = 0.02 > 0
    lo = inference.lo_sharpe(r, q=q, max_lag=1)
    sr = 0.01 / (sum((x - 0.01) ** 2 for x in r) / 99) ** 0.5
    assert lo["rho"] == [pytest.approx(rho1)]
    assert lo["eta"] == pytest.approx(q / (q + 2 * (q - 1) * rho1) ** 0.5)
    assert lo["sharpe"] == pytest.approx(sr * lo["eta"])
    assert inference.lo_sharpe([0.01], q=252)["sharpe"] is None


def test_posterior_sharpe_reproduces_the_issue_example():
    # SR-hat 1.0 annualised over 2.3 years (T = 580 daily returns), tau = 0.5, Normal returns: se^2 per day
    # (1 + 0.5 SR_d^2) / T with SR_d = 1/sqrt(252), x 252 = 0.43524 a year -> 1.0 * 0.25 / (0.25 + 0.43524) = 0.3648
    p = inference.posterior_sharpe(1 / 252**0.5, 580, 0.0, 3.0, tau=0.5)
    assert p["se2_annual"] == pytest.approx(252 * (1 + 0.5 / 252) / 580)
    assert p["posterior"] == pytest.approx(0.3648, abs=5e-4)
    assert p["shrink"] == pytest.approx(0.25 / (0.25 + p["se2_annual"]))
    assert inference.posterior_sharpe(None, 580, 0.0, 3.0, tau=0.5)["posterior"] is None


# --------------------------------------------------------------------------- two-way clustering (Cameron, Gelbach & Miller)
def test_twoway_t_by_hand():
    # values 10, 20, -5, 7 (mean 8, residuals 2, 12, -13, -1); expiry a a b c; entry month m1 m2 m2 m3.
    # CGM (2011) eq. 2.15 with c = G/(G-1) per component: expiry 3/2 (14^2 + 13^2 + 1^2)/16 = 34.3125; month
    # 3/2 (2^2 + 1^2 + 1^2)/16 = 0.5625; intersection (4 singletons) 4/3 (4 + 144 + 169 + 1)/16 = 26.5;
    # V = 34.3125 + 0.5625 - 26.5 = 8.375, t = 8 / sqrt(8.375) = 2.76437
    r = inference.twoway_clustered_t([10, 20, -5, 7], ["a", "a", "b", "c"], ["m1", "m2", "m2", "m3"])
    assert r["var"] == pytest.approx(8.375) and r["t"] == pytest.approx(2.76437, abs=1e-5)
    assert r["fallback"] is False


def test_twoway_t_with_identical_clusterings_is_the_one_way_t():
    vals, cl = [10.0, 20.0, -5.0, 7.0, 3.0], ["a", "a", "b", "c", "c"]
    assert inference.twoway_clustered_t(vals, cl, cl)["t"] == pytest.approx(metrics.clustered_t(vals, cl))


def test_twoway_t_falls_back_to_the_larger_one_way_variance_when_not_positive():
    # values 3, 0, 0, 1 (mean 1, residuals 2, -1, -1, 0); expiry a a b b: 2 (1 + 1)/16 = 0.25; month x y x y:
    # 2 (1 + 1)/16 = 0.25; intersection 4/3 (4 + 1 + 1)/16 = 0.5 -> V = 0: CGM's rule of thumb, max one-way = 0.25
    r = inference.twoway_clustered_t([3, 0, 0, 1], ["a", "a", "b", "b"], ["x", "y", "x", "y"])
    assert r["fallback"] is True and r["var"] == pytest.approx(0.25) and r["t"] == pytest.approx(2.0)
    assert inference.twoway_clustered_t([1, 2], ["a", "a"], ["x", "y"])["t"] is None  # one expiry: unknown


# --------------------------------------------------------------------------- stationary bootstrap and BCa
def test_stationary_bootstrap_indices():
    import numpy as np

    rng = np.random.default_rng(1)
    idx = inference.stationary_indices(50, 1.0, 200, rng)
    assert idx.shape == (200, 50) and idx.min() >= 0 and idx.max() < 50
    one_block = inference.stationary_indices(7, 1e12, 3, np.random.default_rng(2))
    for row in one_block:  # p ~ 0: one block from a random start, wrapping round the circle
        assert all((b - a) % 7 == 1 for a, b in pairwise(row))
    # mean block length ~ L: the share of steps that continue the previous index is 1 - 1/L
    idx = inference.stationary_indices(400, 5.0, 200, np.random.default_rng(3))
    cont = ((idx[:, 1:] - idx[:, :-1]) % 400 == 1).mean()
    assert cont == pytest.approx(0.8, abs=0.01)


def test_bca_acceleration_and_levels_by_hand():
    # jackknife values 1, 2, 3, 10: mean 4, d = 3, 2, 1, -6; a = sum d^3 / (6 (sum d^2)^1.5) = -180 / (6 * 50^1.5)
    assert inference.acceleration([1.0, 2.0, 3.0, 10.0]) == pytest.approx(-180 / (6 * 50**1.5))
    # z0 = 0, a = 0: the percentile levels
    assert inference.bca_levels(0.0, 0.0, 0.95) == pytest.approx((0.025, 0.975))
    # z0 = Phi^-1(0.3) = -0.524401, a = 0: alpha1 = Phi(2 z0 - 1.959964) = Phi(-3.008766) = 0.0013117,
    # alpha2 = Phi(2 z0 + 1.959964) = Phi(0.911162) = 0.818895 (table: 0.81859 + 0.1162 x 0.00262)
    lo, hi = inference.bca_levels(-0.524401, 0.0, 0.95)
    assert lo == pytest.approx(0.0013117, abs=2e-7) and hi == pytest.approx(0.818895, abs=2e-6)
    # a = 0.1, z0 = 0: alpha1 = Phi(-1.959964 / 1.1959964) = Phi(-1.638770) = 0.05063 (table: 0.05155 - 0.877 x 0.00105)
    assert inference.bca_levels(0.0, 0.1, 0.95)[0] == pytest.approx(0.05063, abs=5e-6)


def test_bca_with_no_bias_and_no_skew_is_the_percentile_interval():
    import numpy as np

    boot = np.array([-3.0, -2.0, -1.0, 1.0, 2.0, 3.0])  # half below theta-hat 0: z0 = 0
    lo, hi = inference.bca_interval(0.0, boot, [-1.0, 0.0, 1.0], 0.5)  # symmetric jackknife: a = 0
    assert (lo, hi) == (pytest.approx(np.quantile(boot, 0.25)), pytest.approx(np.quantile(boot, 0.75)))
    assert (
        inference.bca_interval(0.0, np.zeros(5) + 1.0, [0.0, 1.0], 0.95) is None
    )  # every draw above: z0 infinite


def test_cluster_bca_is_seeded_and_brackets_the_mean():
    vals = [100.0, -50.0, 30.0, -20.0, 60.0, 10.0, -5.0, 45.0, 12.0, -30.0]
    cl = [
        "2024-01",
        "2024-01",
        "2024-02",
        "2024-03",
        "2024-03",
        "2024-04",
        "2024-05",
        "2024-06",
        "2024-06",
        "2024-07",
    ]
    r = inference.cluster_bca(vals, cl, 2000, 307)
    assert r["lo"] < sum(vals) / len(vals) < r["hi"] and r["block"] == 2  # ceil(7^(1/3)) = 2 expiries
    assert inference.cluster_bca(vals, cl, 2000, 307) == r
    assert inference.cluster_bca([5.0, 5.0, 5.0], ["a", "b", "c"], 500, 1)["lo"] == 5.0  # degenerate: a point
    assert inference.cluster_bca([5.0], ["a"], 500, 1)["lo"] is None


# --------------------------------------------------------------------------- drawdown, collateral, opportunity cost
def test_drawdown_on_peak_equity():
    from datetime import date, timedelta

    d = [date(2024, 7, 1) + timedelta(days=i) for i in range(4)]
    # 100 -> 110 -> 99 -> 120: the fall 11 from the peak 110 is 10 % of it (11 % of capital 100)
    assert inference.drawdown_peak_pct(
        list(zip(d, [100.0, 110.0, 99.0, 120.0], strict=True))
    ) == pytest.approx(10.0)
    # a window that opens below the capital: 80 -> 90 -> 72 is 20 % of the peak 90 (18 % of capital 100)
    assert inference.drawdown_peak_pct(list(zip(d[:3], [80.0, 90.0, 72.0], strict=True))) == pytest.approx(
        20.0
    )
    assert inference.drawdown_peak_pct([(d[0], 100.0)]) is None


def test_collateral_yield_forgone_per_trade():
    # margin 1,00,000, half of it cash earning nothing, 6.5 % a year for 30 days: 100000 x 0.5 x 0.065 x 30/365
    t = {"net": 1000.0, "margin": 100_000.0, "days_held": 30}
    f = 100_000 * 0.5 * 0.065 * 30 / 365
    assert inference.excess_net(t, rate=0.065, cash_share=0.5) == pytest.approx(1000 - f)
    assert inference.excess_net({**t, "days_held": 0}, rate=0.065, cash_share=0.5) == pytest.approx(
        1000 - f / 30
    )  # a same-day trade holds its margin for one day
    assert inference.excess_net({**t, "margin": None}, rate=0.065, cash_share=0.5) is None  # unknown margin


def test_opportunity_cost_after_tax_by_hand():
    from datetime import date
    from decimal import Decimal

    # Rs 10 lakh, index TRI 100 -> 115 from 1-Jul-2024 to 9-Oct-2026 (long-term listed equity sold after
    # 23-Jul-2024: 12.5 %, plus 4 % cess): gain 1,50,000, tax 19,500, after tax 1,30,500. F&O net Rs 50,000 at a 30 %
    # slab: tax 15,600, after tax 34,400. Excess over holding the index: -96,100.
    o = inference.opportunity_cost(50_000.0, 1_000_000.0, (date(2024, 7, 1), 100.0), (date(2026, 10, 9), 115.0),
                                   Decimal("0.30"), trades=40)  # fmt: skip
    assert o["index_tax_rate"] == pytest.approx(0.125)
    assert o["fno_after_tax"] == pytest.approx(34_400.0) and o["index_after_tax"] == pytest.approx(130_500.0)
    assert o["excess_after_tax"] == pytest.approx(-96_100.0)
    assert o["index_per_trade_after_tax"] == pytest.approx(130_500.0 / 40)
    # an F&O loss carries no tax credit here (set-off against other business income is not modelled)
    loss = inference.opportunity_cost(-20_000.0, 1_000_000.0, (date(2024, 7, 1), 100.0),
                                      (date(2026, 10, 9), 115.0), Decimal("0.30"), trades=40)  # fmt: skip
    assert loss["fno_after_tax"] == -20_000.0
    # short-term (held 8 months, sold after 23-Jul-2024): 20 %
    st = inference.opportunity_cost(0.0, 1_000_000.0, (date(2025, 7, 1), 100.0), (date(2026, 3, 2), 110.0),
                                    Decimal("0.30"), trades=10)  # fmt: skip
    assert st["index_tax_rate"] == pytest.approx(0.20)
    assert st["index_after_tax"] == pytest.approx(100_000 - 100_000 * 0.20 * 1.04)
    unknown = inference.opportunity_cost(50_000.0, 1_000_000.0, None, None, Decimal("0.30"), trades=40)
    assert unknown["excess_after_tax"] is None and unknown["reason"]
