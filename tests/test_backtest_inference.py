"""Inference recalibration (#381, P2.6). Every expected value is worked out by hand in the comment beside it."""

from __future__ import annotations

import pytest

from finresearch.backtest import evaluate


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
