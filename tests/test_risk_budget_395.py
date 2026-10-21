"""P4.2 (#395): the common-shock and margin-utilisation terms of the one sizing rule, and the Kelly ceiling on real
money. Expected values are hand arithmetic, written out next to each assertion, never the code under test."""

from __future__ import annotations

import math

import pytest

from finresearch.fincalc import tradeplan as tp

M3, M5, M10, P5 = -0.03, -0.05, -0.10, 0.05


def test_the_registered_shock_moves_and_limits_395():
    assert tp.SHOCK_MOVES == (M3, M5, M10, P5)
    assert tp.SHOCK_BUDGET_PCT == 6.0 and tp.MARGIN_UTIL_MAX == 0.5


# --------------------------------------------------------------------------- the common-shock term
BOOK = {M3: -1000.0, M5: -2000.0, M10: -5000.0, P5: 500.0}
ONE = {M3: -300.0, M5: -600.0, M10: -1500.0, P5: 100.0}


def test_shock_lots_is_the_tightest_move_395():
    # limit 20,000: −3 %: (20,000 − 1,000) / 300 = 63.3 → 63; −5 %: 18,000 / 600 = 30; −10 %: 15,000 / 1,500 = 10;
    # +5 %: the new trade gains, no bound → 10
    n, why = tp.shock_lots(BOOK, ONE, 20_000)
    assert n == 10 and "-10%" in why


def test_shock_lots_floors_at_zero_when_one_lot_does_not_fit_395():
    # limit 6,000 at −10 %: (6,000 − 5,000) / 1,500 = 0.67 → 0
    assert tp.shock_lots(BOOK, ONE, 6_000)[0] == 0


def test_a_book_already_beyond_the_shock_budget_takes_nothing_new_even_a_hedge_395():
    book = {**BOOK, P5: -7000.0}  # the open book alone loses ₹7,000 at +5 %, beyond ₹6,000
    n, why = tp.shock_lots(book, {M3: 10.0, M5: 10.0, M10: 10.0, P5: 2000.0}, 6_000)
    assert n == 0 and "already" in why


def test_a_trade_that_loses_in_no_move_is_not_bounded_by_the_shock_term_395():
    n, _ = tp.shock_lots(BOOK, {M3: 1.0, M5: 2.0, M10: 3.0, P5: 0.0}, 6_000)
    assert n == math.inf


@pytest.mark.parametrize(
    "book,one",
    [
        (None, ONE),  # the open book's shock is unknown
        (BOOK, None),
        ({**BOOK, M5: None}, ONE),  # one move unknown
        (BOOK, {**ONE, P5: None}),
        ({M3: 0.0, M5: 0.0, M10: 0.0}, ONE),  # a registered move missing: unknown, never "no loss"
    ],
)
def test_an_unknown_shock_is_never_room_395(book, one):
    n, why = tp.shock_lots(book, one, 1e9)
    assert n is None and "unknown" in why


# --------------------------------------------------------------------------- the margin-utilisation term
def test_margin_utilisation_is_the_larger_of_the_two_ratios_395():
    # (100,000 + 40,000) / (300,000 + 200,000) = 0.28; (140,000) x 0.5 / 300,000 = 0.2333 → 0.28
    assert tp.margin_utilisation(100_000, 40_000, 300_000, 200_000) == pytest.approx(0.28)
    # cash-bound: 140,000 / 500,000 = 0.28; 140,000 x 0.5 / 100,000 = 0.70 → 0.70
    assert tp.margin_utilisation(100_000, 40_000, 100_000, 400_000) == pytest.approx(0.70)


def test_margin_lots_395():
    # capacity 0.5 x min(300,000 + 200,000, 300,000 / 0.5) = 250,000; (250,000 − 100,000) / 40,000 = 3.75 → 3
    assert tp.margin_lots(100_000, 40_000, 300_000, 200_000)[0] == 3
    # cash-bound: 0.5 x min(500,000, 100,000 / 0.5 = 200,000) = 100,000; (100,000 − 50,000) / 40,000 = 1.25 → 1
    assert tp.margin_lots(50_000, 40_000, 100_000, 400_000)[0] == 1
    # already beyond u_max: 0, never negative
    assert tp.margin_lots(300_000, 40_000, 300_000, 200_000)[0] == 0


@pytest.mark.parametrize("args", [(None, 40_000, 3e5, 2e5), (1e5, None, 3e5, 2e5), (1e5, 4e4, None, 2e5),
                                  (1e5, 4e4, 3e5, None)])  # fmt: skip
def test_unknown_margin_or_funds_is_never_room_395(args):
    n, why = tp.margin_lots(*args)
    assert n is None and "unknown" in why


def test_no_cash_means_no_margin_room_395():
    assert tp.margin_lots(0, 40_000, 0, 500_000)[0] == 0
