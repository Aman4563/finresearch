"""Backtest statistics and the pre-registration guard (#307). Expected values are worked out by hand."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import fno_synth as fs
import pytest

from finresearch.backtest import evaluate, metrics, prereg, specs


def test_clustered_t_equals_the_ordinary_t_with_one_trade_per_expiry():
    # mean 15, deviations 85, -65, 15, -35: s^2 = 12900 / 3 = 4300, se = sqrt(4300 / 4) = 32.787
    assert metrics.clustered_t([100, -50, 30, -20], ["a", "b", "c", "d"]) == pytest.approx(
        15 / 32.78719, rel=1e-5
    )


def test_clustered_t_groups_trades_on_the_same_expiry():
    # mean 8; cluster residual sums: a (2 + 12) = 14, b -13, c -1; var = 3/2 * (196 + 169 + 1) / 16 = 34.3125
    assert metrics.clustered_t([10, 20, -5, 7], ["a", "a", "b", "c"]) == pytest.approx(8 / 34.3125**0.5)
    assert metrics.clustered_t([10, 20], ["a", "a"]) is None  # one expiry: no variance estimate


def test_bootstrap_ci_is_seeded_and_brackets_the_mean():
    vals, cl = [100.0, -50.0, 30.0, -20.0, 60.0, 10.0], list("abcdef")
    lo, hi = metrics.cluster_bootstrap(vals, cl, 4000, 307)
    assert lo < sum(vals) / 6 < hi
    assert metrics.cluster_bootstrap(vals, cl, 4000, 307) == (lo, hi)
    assert metrics.cluster_bootstrap([5.0, 5.0, 5.0], list("abc"), 500, 1) == (5.0, 5.0)


def test_trade_stats_and_cvar():
    nets = [-900.0] + [100.0] * 19  # 20 trades: the worst 5 % is the single worst trade
    trades = [{"net": x, "expiry": f"2024-{i % 12 + 1:02d}-28", "entry_costs": 10.0, "exit_costs": 5.0,
               "exercise_stt": 0.0, "margin": 50_000.0, "days_held": 73} for i, x in enumerate(nets)]  # fmt: skip
    s = metrics.trade_stats(trades)
    assert (s["trades"], s["wins"], s["losses"], s["win_rate"]) == (20, 19, 1, 0.95)
    assert (s["expectancy"], s["avg_win"], s["avg_loss"], s["worst_trade"], s["cvar_5"]) == (
        50.0,
        100.0,
        -900.0,
        -900.0,
        -900.0,
    )
    assert s["costs"] == 300.0
    # ₹1,000 net over 20 x 50,000 x 73/365 = 2,00,000 rupee-years of margin = 0.5 % a year
    assert s["return_on_margin_annual"] == pytest.approx(0.005)
    assert metrics.cvar([-5.0, -3.0, 1.0] + [2.0] * 37) == -4.0  # ceil(5 % x 40) = 2 worst


def test_equity_stats():
    d0 = date(2024, 1, 1)
    eq = [
        (d0, 100.0),
        (d0 + timedelta(days=1), 110.0),
        (d0 + timedelta(days=2), 99.0),
        (d0 + timedelta(days=365), 120.0),
    ]
    s = metrics.equity_stats(eq, 100.0)
    assert s["max_drawdown_pct"] == 11.0 and s["max_drawdown"] == 11.0
    assert s["cagr"] == pytest.approx(1.2 ** (365 / 365) - 1)
    r = [0.1, 99 / 110 - 1, 120 / 99 - 1]
    m = sum(r) / 3
    sd = (sum((x - m) ** 2 for x in r) / 2) ** 0.5
    assert s["sharpe"] == pytest.approx(m / sd * 252**0.5, abs=1e-3)
    down = (sum(min(x, 0) ** 2 for x in r) / 3) ** 0.5
    assert s["sortino"] == pytest.approx(m / down * 252**0.5, abs=1e-3)


def test_deflated_sharpe_against_hand_values():
    # N = 1: no selection, DSR = PSR = Phi(0.1 * sqrt(252) / sqrt(1 + 0.5 * 0.01)) = Phi(1.58355) = 0.943346
    assert metrics.deflated_sharpe(0.1, 253, 0.0, 3.0, 1, 0.0) == pytest.approx(0.943346, abs=2e-6)
    # N = 8, V = 0.0025: SR0 = 0.05 * (0.42278 * 1.15035 + 0.57722 * 1.68510) = 0.072951
    assert metrics.expected_max_sharpe(0.0025, 8) == pytest.approx(0.072951, abs=2e-6)
    # DSR = Phi((0.1 - 0.072956) * sqrt(252) / 1.002497) = Phi(0.42834) = 0.66579
    assert metrics.deflated_sharpe(0.1, 253, 0.0, 3.0, 8, 0.0025) == pytest.approx(0.66579, abs=5e-5)
    # negative skew and fat tails lower it
    assert metrics.deflated_sharpe(0.1, 253, -2.0, 12.0, 8, 0.0025) < 0.66


# --------------------------------------------------------------------------- pre-registration
def test_registration_hash_is_recorded():
    """specs.py is registered as committed: any edit must become a new, re-registered variant."""
    assert specs.registration_hash() == prereg.HASH


def test_out_of_sample_refused_after_a_spec_changes(monkeypatch):
    changed = tuple(replace(s, profit_take_pct=75.0) if s.key == "nifty_pcs_m" else s for s in specs.SPECS)
    monkeypatch.setattr(specs, "SPECS", changed)
    assert specs.registration_hash() != prereg.HASH
    h = fs.history(date(2023, 1, 2), date(2023, 3, 31))
    with pytest.raises(evaluate.PreregistrationError):
        evaluate.evaluate(h, stock=None, source="synthetic")
    rep = evaluate.evaluate(
        h, stock=None, source="synthetic", oos=False, spec_list=changed
    )  # in-sample: allowed
    assert rep["oos_evaluated"] is False
    assert {r["status"] for r in rep["strategies"]} <= {"in_sample_only", "no_data"}


def test_in_sample_run_never_reads_out_of_sample_days():
    h = fs.history(date(2023, 1, 2), date(2024, 9, 30))
    rep = evaluate.evaluate(h, stock=None, source="synthetic", oos=False)
    read = [r["max_day_read"] for r in rep["strategies"] if r.get("max_day_read")]
    assert read and max(read) <= specs.IS_END.isoformat()


def test_report_judges_every_criterion(monkeypatch):
    h = fs.history(date(2023, 6, 1), date(2025, 1, 31))
    rep = evaluate.evaluate(h, stock=None, source="synthetic")
    assert rep["registration_hash"] == prereg.HASH and rep["variants_tried"] == specs.VARIANTS_TRIED
    by = {r["key"]: r for r in rep["strategies"]}
    assert by["stock_cc_m"]["status"] == "no_data" and by["banknifty_ic_m"]["status"] == "no_data"
    dcs = by["nifty_dcs_m"]
    assert dcs["status"] == "fails" and len(dcs["criteria"]) == 8
    assert any(
        c["criterion"] == "OOS trades" and not c["pass"] for c in dcs["criteria"]
    )  # 7 months: < 30 trades
    assert dcs["windows"]["oos"]["trades"] == len(
        [t for t in dcs["trades"] if t["entry_day"] >= "2024-07-01"]
    )
