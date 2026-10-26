"""P4.2 Stage 2 (#395): one candidate's lots under the book's limits, composed from the one sizing formula
(fincalc.tradeplan.size, shock_lots) with the month stop and the card's Kelly zero. Hand arithmetic beside each case:
capital ₹10 lakh, 5 % trade budget = ₹50,000, B_book 8 % = ₹80,000, B_shock 6 % = ₹60,000, u_max 0.5 of ₹10 lakh
all cash = ₹5,00,000 of margin, L_month 6 % = ₹60,000."""

from __future__ import annotations

import math

import pytest

from finresearch.riskbudget import limits

M5 = -0.05
CARD = (-0.03, -0.05, -0.10, 0.05)
# one lot: max loss ₹12,000 + entry costs ₹500 = ₹12,500 risk → the trade budget allows floor(50,000 / 12,500) = 4
BASE = dict(ml_one=12_000.0, costs_one=500.0, open_risk=0.0, margin_used=0.0, im_one=40_000.0,
            l_book={M5: 0.0}, l_one={M5: -10_000.0}, mtd=0.0)  # fmt: skip


def sized(**kw):
    return limits.limit_lots(**{**BASE, **kw})


def test_with_room_everywhere_the_trade_budget_stands() -> None:
    # book (80,000 − 0) / 12,500 = 6.4 → 6; margin (500,000 − 0) / 40,000 = 12.5 → 12; shock 60,000 / 10,000 = 6
    assert sized() == limits.Sized(4, None, "")


@pytest.mark.parametrize(
    ("kw", "lots", "binding"),
    [
        ({"open_risk": 50_000.0}, 2, "book"),  # (80,000 − 50,000) / 12,500 = 2.4 → 2
        ({"open_risk": 70_000.0}, 0, "book"),  # 10,000 / 12,500 = 0.8 → 0
        ({"margin_used": 450_000.0}, 1, "margin"),  # (500,000 − 450,000) / 40,000 = 1.25 → 1
        (
            {"l_book": {M5: -30_000.0}, "l_one": {M5: -20_000.0}},
            1,
            "shock",
        ),  # (60,000 − 30,000) / 20,000 = 1.5 → 1
        ({"l_book": {M5: -60_000.0}}, 0, "shock"),  # the book alone is at −B_shock: nothing new
        ({"mtd": -60_000.0}, 0, "month"),  # at −6 % of capital the month is stopped
    ],
)
def test_the_tightest_term_sets_the_lots_and_is_named(kw, lots, binding) -> None:
    got = sized(**kw)
    assert (got.lots, got.binding) == (lots, binding) and got.reason


def test_a_month_just_above_the_stop_still_trades() -> None:
    assert sized(mtd=-59_999.0).lots == 4


def test_the_registered_move_only_needs_its_own_value_and_the_card_needs_all_four() -> None:
    # B registers m = −5 % alone; the card rule takes every move, so a missing one is unknown (never room)
    assert sized(moves=(M5,)).lots == 4
    got = sized(moves=CARD)
    assert (got.lots, got.binding) == (0, "shock") and "unknown" in got.reason
    one = {-0.03: -6_000.0, M5: -10_000.0, -0.10: -25_000.0, 0.05: 3_000.0}
    # −10 %: (60,000 − 0) / 25,000 = 2.4 → 2 is the tightest move
    assert sized(moves=CARD, l_book=dict.fromkeys(CARD, 0.0), l_one=one).lots == 2


def test_a_trade_that_gains_at_the_shock_is_not_bounded_by_it() -> None:
    assert sized(l_one={M5: 1_500.0}).lots == 4


@pytest.mark.parametrize(
    ("kw", "binding"),
    [({"open_risk": None}, "book"), ({"margin_used": None}, "margin"), ({"im_one": None}, "margin"),
     ({"l_book": None}, "shock"), ({"l_one": {M5: None}}, "shock")],
)  # fmt: skip
def test_an_unknown_term_is_never_room(kw, binding) -> None:
    got = sized(**kw)
    assert (got.lots, got.binding) == (0, binding) and "unknown" in got.reason


@pytest.mark.parametrize(("mu_lo", "lots"), [(None, 0), (-1.0, 0), (0.0, 0), (250.0, 4)])
def test_the_card_rules_kelly_ceiling_is_zero_while_mu_lo_is_not_above_zero(mu_lo, lots) -> None:
    got = sized(kelly=True, mu_lo=mu_lo)
    assert got.lots == lots and (got.binding == "kelly") == (lots == 0)


def test_without_the_kelly_term_mu_lo_is_not_read() -> None:
    assert sized(mu_lo=None).lots == 4


def test_a_trade_above_its_own_budget_is_left_to_the_engines_refusal() -> None:
    # one lot risks 60,000 > 50,000: the trade budget gives 0 and no book term is blamed
    assert sized(ml_one=59_500.0) == limits.Sized(0, None, "")


def test_kelly_mu_lo_needs_enough_closed_trades_and_reads_the_lower_bound() -> None:
    # 29 trades: unknown; with 30 the percentile CI over whole expiries is computed (here every expiry nets ₹1,000
    # per trade, so every resample's mean, and the lower bound, is exactly 1,000)
    rows = [{"net": 1000.0, "expiry": f"2024-{1 + i % 6:02d}-25"} for i in range(30)]
    assert limits.kelly_mu_lo(rows[:29]) is None
    assert limits.kelly_mu_lo(rows) == pytest.approx(1000.0)
    assert math.isfinite(limits.kelly_mu_lo([{**r, "net": r["net"] - 2000.0 * (i % 2)}
                                             for i, r in enumerate(rows)]))  # fmt: skip
