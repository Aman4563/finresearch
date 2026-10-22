"""The stored daily joint P&L's arithmetic (#397 (b), fincalc.joint_pnl): the actual P&L of both books between two
evenings and its explained parts. Pure; synthetic numbers only, every expected value worked by hand."""

from __future__ import annotations

from datetime import date

import pytest

from finresearch.fincalc import joint_pnl as jp

D0, D1 = date(2026, 10, 20), date(2026, 10, 21)
EXP = date(2026, 10, 27)


def _m(key: str, price: float | None, *, qty: int | None = -75, right: str = "put", strike: float = 25000.0,
       expiry: date = EXP, spot: float | None = 25500.0, iv: float | None = 0.12, t: float = 0.02,
       symbol: str = "NIFTY") -> jp.Mark:  # fmt: skip
    return jp.Mark(key, symbol, expiry, right, strike, qty, price, spot, iv, t)


# --------------------------------------------------------------------------- the book's actual P&L
def test_fno_actual_is_signed_qty_times_the_settle_change_of_carried_legs():
    prev = [_m("paper_leg:1", 100.0), _m("paper_leg:2", 40.0, qty=75, strike=24500.0)]
    cur = [_m("paper_leg:1", 130.0), _m("paper_leg:2", 52.0, qty=75, strike=24500.0)]
    got, why = jp.fno_actual(prev, cur, D1)
    # -75 x (130 - 100) + 75 x (52 - 40) = -2250 + 900
    assert got == pytest.approx(-1350.0) and why == []


def test_fno_actual_of_an_empty_book_is_exactly_zero():
    assert jp.fno_actual([], [], D1) == (0.0, [])


def test_a_leg_that_expired_at_the_previous_close_carries_nothing():
    prev = [_m("paper_leg:1", 3.0, expiry=D0), _m("paper_leg:2", 100.0)]
    cur = [_m("paper_leg:2", 90.0)]
    got, why = jp.fno_actual(prev, cur, D1)
    assert got == pytest.approx(-75 * (90.0 - 100.0)) and why == []


@pytest.mark.parametrize(
    ("prev", "cur", "needle"),
    [
        ([_m("paper_leg:1", 100.0)], [], "changed"),  # closed during the day
        ([], [_m("paper_leg:1", 100.0)], "changed"),  # opened during the day
        ([_m("paper_leg:1", 100.0)], [_m("paper_leg:1", 100.0, qty=-150)], "changed"),
        ([_m("groww:X", 100.0, qty=None)], [_m("groww:X", 90.0, qty=None)], "long or short"),
        ([_m("paper_leg:1", None)], [_m("paper_leg:1", 90.0)], "price"),
        ([_m("paper_leg:1", 100.0)], [_m("paper_leg:1", None)], "price"),
    ],
)
def test_fno_actual_is_unknown_with_a_reason_never_zero(prev, cur, needle):
    got, why = jp.fno_actual(prev, cur, D1)
    assert got is None and why and needle in " ".join(why)


# --------------------------------------------------------------------------- the holdings' actual P&L
def test_holdings_actual_is_the_value_change_less_the_days_net_money_in():
    # value 1,00,000 -> 1,12,000 with 10,000 bought that day: the market made 2,000
    got, why = jp.holdings_actual(jp.SeriesDay(D0, 100_000.0, 80_000.0, True),
                                  jp.SeriesDay(D1, 112_000.0, 90_000.0, True))  # fmt: skip
    assert got == pytest.approx(2_000.0) and why == []


@pytest.mark.parametrize(("c0", "c1"), [(False, True), (True, False)])
def test_holdings_actual_on_a_stale_priced_day_is_unknown(c0, c1):
    got, why = jp.holdings_actual(jp.SeriesDay(D0, 1.0, 1.0, c0), jp.SeriesDay(D1, 2.0, 1.0, c1))
    assert got is None and "stale" in why[0]


def test_holdings_actual_without_a_series_day_is_unknown():
    got, why = jp.holdings_actual(None, jp.SeriesDay(D1, 2.0, 1.0, True))
    assert got is None and why


# --------------------------------------------------------------------------- the holdings' explained P&L
def test_own_move_is_each_previous_evening_value_times_its_own_return():
    snap = [jp.Snap("NSE:FAKEEQ", 100_000.0, 1.2), jp.Snap("MF:999", 50_000.0, None)]
    got, why = jp.own_move(snap, {"NSE:FAKEEQ": -0.02, "MF:999": 0.001})
    assert got == pytest.approx(100_000 * -0.02 + 50_000 * 0.001) and why == []


def test_own_move_with_a_missing_return_is_unknown_and_names_it():
    got, why = jp.own_move([jp.Snap("NSE:FAKEEQ", 1.0, 1.0)], {"NSE:FAKEEQ": None})
    assert got is None and "NSE:FAKEEQ" in why[0]


def test_beta_move_is_the_previous_evenings_d_holdings_times_niftys_move():
    assert jp.beta_move(150_000.0, -0.02) == pytest.approx(-3_000.0)
    assert jp.beta_move(None, -0.02) is None
