"""Criteria v2 of the F&O backtest (#381): a separately registered SHADOW verdict that never changes a v1 status."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import fno_synth as fs
import pytest

from finresearch.backtest import criteria_v2, evaluate, prereg_v2


def test_criteria_v2_registration_hash_is_recorded():
    """criteria_v2.py is registered as committed; it covers v1's registration hash, so a v1 change also breaks it."""
    assert criteria_v2.registration_hash() == prereg_v2.HASH
    assert criteria_v2.registration()["base_registration"] == evaluate.prereg.HASH
    # adoption waits for #380 (P2.5) and the real-data simulation
    assert criteria_v2.CRITERIA_V2.adopted is False


def test_registration_changes_with_any_criterion():
    changed = replace(criteria_v2.CRITERIA_V2, dsr_rule="v_null")
    assert criteria_v2.registration_hash(changed) != prereg_v2.HASH


def _trade(i: int, net: float, margin: float | None = 100_000.0) -> dict:
    entry = date(2024, 7, 1) + timedelta(days=7 * i)
    return {"net": net, "entry_day": entry.isoformat(), "expiry": (entry + timedelta(days=30)).isoformat(),
            "margin": margin, "days_held": 20, "entry_costs": 0.0, "exit_costs": 0.0, "exercise_stt": 0.0}  # fmt: skip


def test_trade_part_uses_pnl_in_excess_of_the_collateral_yield():
    trades = [_trade(i, 1000.0 if i % 3 else -500.0) for i in range(40)]
    p = criteria_v2.trade_part(trades)
    forgone = 100_000 * 0.5 * 0.065 * 20 / 365  # cash half of the margin at 6.5 % for 20 days
    mean_net = sum(t["net"] for t in trades) / 40
    assert p["excess_expectancy"] == pytest.approx(mean_net - forgone)
    assert p["t_twoway"] is not None and p["ci_low"] is not None and p["ci_low"] < p["excess_expectancy"]
    unknown = criteria_v2.trade_part([*trades, _trade(41, 10.0, margin=None)])
    assert unknown["excess_expectancy"] is None and unknown["t_twoway"] is None  # one unknown margin: unknown


def test_evaluate_v1_verdicts_are_identical_with_and_without_the_shadow():
    h = fs.history(date(2023, 6, 1), date(2025, 1, 31))
    with_v2 = evaluate.evaluate(h, stock=None, source="synthetic")
    without = evaluate.evaluate(h, stock=None, source="synthetic", shadow_v2=False)
    for a, b in zip(with_v2["strategies"], without["strategies"], strict=True):
        assert "criteria_v2" not in b
        assert {k: v for k, v in a.items() if k != "criteria_v2"} == b
    judged = [r for r in with_v2["strategies"] if r.get("criteria_v2")]
    assert judged
    for r in judged:
        v2 = r["criteria_v2"]
        assert v2["status"] == "shadow_not_adopted" and v2["verdict"] in ("would_pass", "would_fail")
        assert {c["criterion"] for c in v2["criteria"]} >= {
            "OOS deflated Sharpe (v2)",
            "OOS max drawdown (% of peak)",
        }
        assert v2["dsr"]["rule"] == criteria_v2.CRITERIA_V2.dsr_rule
