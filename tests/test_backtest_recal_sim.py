"""The #381 validation simulations (small, seeded runs; the full runs are recorded in fno_backtest/ADDENDUM.md)."""

from __future__ import annotations

import math

import numpy as np
import pytest

from finresearch.backtest import recal_sim as rs


def test_dm_size_rectangular_is_nearer_nominal_than_the_registered_bartlett_at_h_22():
    r = rs.dm_size(22, 500, 400, 2)
    assert r["bartlett_registered"] > r["rectangular_h_minus_1"]
    assert abs(r["rectangular_h_minus_1"] - 0.05) < abs(r["bartlett_registered"] - 0.05)


def test_simulated_series_have_the_stated_shape():
    rng = np.random.default_rng(1)
    for kind in ("iid", "ar1", "garch_t", "ar1_garch_t"):
        x = rs.simulate_series(kind, 4000, rng)
        assert len(x) == 4000 and abs(x.mean()) < 0.15
    ar = rs.simulate_series("ar1", 20000, rng)
    assert np.corrcoef(ar[1:], ar[:-1])[0, 1] == pytest.approx(0.3, abs=0.03)


def test_coverage_reports_every_method():
    out = rs.coverage(["iid"], 60, 20, 199, 3)["iid"]
    assert set(out) >= {"iid_percentile", "stationary_percentile", "stationary_bca", "hac_t"}
    assert all(
        0 <= out[m] <= 1 for m in ("iid_percentile", "stationary_percentile", "stationary_bca", "hac_t")
    )


def test_programme_plants_the_sharpe_in_strategy_0_only():
    base = rs.synthetic_base(20_000, 2, 4)
    x = rs.programme(base, 4, 2.0, 0.5, np.random.default_rng(5))
    sr = x.mean(axis=0) / x.std(axis=0, ddof=1) * math.sqrt(252)
    assert sr[0] == pytest.approx(2.0, abs=0.4)  # 79 years of days: se of the annual SR ~ 0.11
    assert all(abs(s) < 0.45 for s in sr[1:])


def test_rules_declare_a_strong_planted_strategy_and_count_errors():
    base = np.random.default_rng(0).standard_normal((570, 3)) * 0.01
    r = rs.dsr_rule_sim(base, 8, 6.0, 5, 100, 7)
    assert r["power"]["romano_wolf"] == 1.0 and r["power"]["dsr_v_null"] == 1.0
    assert set(r["fwer"]) == {"dsr_v_null", "dsr_v_cand", "dsr_max", "romano_wolf", "spa"}


def test_seller_returns_from_a_history():
    from datetime import date

    import fno_synth as fs

    x, keys = rs.seller_returns(fs.history(date(2023, 6, 1), date(2025, 1, 31)))
    assert keys and x.shape[1] == len(keys) and x.shape[0] > 50
    assert np.isfinite(x).all()
