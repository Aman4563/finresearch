"""Corporate-action adjustment of F&O and equity histories (#369, roadmap P1.7).

Synthetic prices and lots only. Expected values are computed by hand from the issue's formulas:
F&O factor f = L_t / L_t-1, adjusted return r_t = ln(F_t x f / F_t-1)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from finresearch.fincalc import fno_history as fh

E = date(2026, 10, 27)
D = [date(2026, 9, 1) + timedelta(days=i) for i in range(4)]


def _futs(px):
    return [(d, E, p) for d, p in zip(D, px, strict=False)]


def _lots(seq):
    return {(d, E): lot for d, lot in zip(D, seq, strict=False)}


def test_bonus_lot_change_is_adjusted_not_dropped():
    # 1:1 bonus: NSE halves the open future (1010 -> 500) and doubles the lot (500 -> 1000): f = 2,
    # adjusted return ln(500 x 2 / 1010) = ln(1000 / 1010), a -1 % market move kept in RV
    adjusted: list = []
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0, 500.0]), lots=_lots([500, 500, 1000]),
                                   unverified=unverified, adjusted=adjusted)  # fmt: skip
    assert [d for d, _ in rets] == [D[1], D[2]]
    assert rets[1][1] == pytest.approx(math.log(1000 / 1010))
    assert unverified == []
    assert adjusted == [(D[2], E, pytest.approx(2.0), pytest.approx(math.log(500 / 1010)))]


def test_lot_cut_on_consolidation_is_adjusted():
    # 1-for-5 consolidation: price x5 (200 -> 1000), lot / 5 (2500 -> 500): f = 0.2, r = ln(1000 x 0.2 / 200) = 0
    rets = fh.front_future_returns(_futs([200.0, 1000.0]), lots=_lots([2500, 500]))
    assert rets == [(D[1], pytest.approx(0.0))]


def test_lot_revision_without_price_adjustment_keeps_raw_return():
    # NSE's periodic lot revision (no corporate action): lot 500 -> 750 but the price moves +1 %. Applying
    # f = 1.5 would invent a ln(1.5) jump; the raw return is the smaller, verified one
    adjusted: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0]), lots=_lots([500, 750]), adjusted=adjusted)
    assert rets == [(D[1], pytest.approx(math.log(1.01)))]
    assert adjusted == []


def test_lot_change_whose_factor_does_not_explain_the_jump_is_unverified():
    # lot doubles but the price fell 80 %: adjusted ln(0.2 x 2) = -0.92, raw -1.61, neither within 0.25
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 200.0]), lots=_lots([500, 1000]), unverified=unverified)
    assert rets == []
    assert unverified == [(D[1], E, pytest.approx(math.log(0.2)))]


@pytest.mark.parametrize("lots", [None, _lots([500, 500, 500])])
def test_unexplained_jump_is_unverified_and_excluded(lots):
    # a -50 % within-contract day with no lot change (or no lot field, pre-UDiFF days) is not a verified market
    # move: excluded from RV and reported, never silently kept
    unverified: list = []
    rets = fh.front_future_returns(_futs([1000.0, 1010.0, 505.0]), lots=lots, unverified=unverified)
    assert [d for d, _ in rets] == [D[1]]
    assert unverified == [(D[2], E, pytest.approx(math.log(0.5)))]


def test_ordinary_moves_pass_through_unchanged():
    rets = fh.front_future_returns(_futs([1000.0, 1100.0, 990.0]))
    assert [r for _, r in rets] == [pytest.approx(math.log(1.1)), pytest.approx(math.log(0.9))]
