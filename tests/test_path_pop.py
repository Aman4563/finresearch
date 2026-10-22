"""P3.5 (#388): probabilities that match the trade plan's exits. A Monte Carlo of the card's own exit rules: forward
paths on the stored session calendar, each leg repriced by Black-76 at its origin IV moved by a spot-vol rule, and
every path run through `fnobook.card_forecasts.simulate` (the card ledger's exit replay, i.e. `backtest.engine.manage`)
so there is one implementation of the exit rules. Synthetic inputs only; expected values worked out here."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from finresearch.fincalc import black76 as b76

D0, EXP = date(2025, 1, 10), date(2025, 2, 13)


def test_forward_paths_are_a_martingale_with_the_registered_total_variance():
    from finresearch.fnobook import path_pop as pp

    times = np.array([3, 4, 5, 34]) / 365  # calendar years from the origin close to each session
    f = pp.forward_paths(20000.0, 0.15, times, 200_000, np.random.default_rng(1))
    assert f.shape == (200_000, 4)
    end = np.log(f[:, -1] / 20000.0)
    se = 0.15 * math.sqrt(34 / 365) / math.sqrt(200_000)
    # E[ln F_T] = -sigma^2 T / 2 and var = sigma^2 T (zero-drift forward: S drifts at r, as A1.6)
    assert end.mean() == pytest.approx(-0.5 * 0.15**2 * 34 / 365, abs=4 * se)
    assert end.var() == pytest.approx(0.15**2 * 34 / 365, rel=0.01)
    assert f[:, -1].mean() == pytest.approx(20000.0, rel=4 * se)
    again = pp.forward_paths(20000.0, 0.15, times, 10, np.random.default_rng(1))
    assert np.array_equal(again, pp.forward_paths(20000.0, 0.15, times, 10, np.random.default_rng(1)))


def test_a_leg_is_repriced_by_black76_at_its_origin_iv_moved_by_the_spot_vol_rule():
    from finresearch.fnobook import path_pop as pp

    leg = pp.PathLeg("put", 19800.0, "sell", entry=60.0, iv=0.14, contracts=1500)
    tau, df = 30 / 365, math.exp(-0.065 * 30 / 365)
    # no move: the origin IV; beta = -0.5 and F down 2 %: sigma = 0.14 - 0.5 x ln(0.98) = 0.150101
    assert pp.leg_price(leg, 20000.0, 20000.0, tau, 0.065, -0.5) == pytest.approx(
        b76.price("put", 20000.0, 19800.0, tau, df, 0.14), abs=1e-9)
    sig = 0.14 - 0.5 * math.log(0.98)
    assert pp.leg_price(leg, 19600.0, 20000.0, tau, 0.065, -0.5) == pytest.approx(
        b76.price("put", 19600.0, 19800.0, tau, df, sig), abs=1e-9)
    # a shift below the solver's floor is floored there, never negative
    assert pp.leg_price(leg, 30000.0, 20000.0, tau, 0.065, -5.0) == pytest.approx(
        b76.price("put", 30000.0, 19800.0, tau, df, b76.SIGMA_LO), abs=1e-9)
    assert pp.leg_price(leg, 19000.0, 20000.0, 0.0, 0.065, 0.0) == pytest.approx(800.0)  # intrinsic at expiry


def test_a_mark_is_rounded_to_the_tick_and_never_below_one():
    from finresearch.fnobook import path_pop as pp

    assert pp.tick(12.3449) == 12.35 and pp.tick(12.32) == 12.3 and pp.tick(0.004) == 0.05
