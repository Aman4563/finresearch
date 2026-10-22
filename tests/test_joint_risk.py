"""One risk view across the investment portfolio and the F&O book (#397, fincalc.joint_risk). Synthetic holdings and
legs only. Expected values are computed by hand or independently (statistics.NormalDist), never by the code under
test."""

from __future__ import annotations

import math
from datetime import date, datetime
from statistics import NormalDist

import pytest

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc import joint_risk as jr
from finresearch.fincalc.dates import IST

N = NormalDist()
NOW = datetime(2026, 10, 22, 15, 30, tzinfo=IST)
EXP = date(2026, 11, 26)
T35 = 35 / 365
R = 0.065
F = 25000.0
AT = bs.horizon_clock(NOW, 1)


def _b76(right: str, f: float, k: float, t: float, v: float) -> float:
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    c = math.exp(-R * t) * (f * N.cdf(d1) - k * N.cdf(d1 - v * math.sqrt(t)))
    return c if right == "call" else c - math.exp(-R * t) * (f - k)


MARKET = bs.Market(NOW, R, {("NIFTY", EXP): F}, nifty=24900.0)
SHORT_PUT = bs.BookLeg("NIFTY", EXP, "put", 24000.0, -75, _b76("put", F, 24000, T35, 0.15), "mid")
SHORT_FUT = bs.BookLeg("NIFTY", EXP, "future", 0.0, -75, None, None)
EQUITY = jr.Holding("NSE:FAKEEQ", "Fake Equity Ltd", 1_000_000.0, 1.2, "beta 1.20 (250 days)")
FUND = jr.Holding(
    "MF:999", "Fake Liquid Fund", 200_000.0, None, "only 30 days of prices, needs 120 for a beta"
)


def test_long_equity_plus_short_put_loses_more_at_minus_10_than_either_alone():
    eq = jr.losses([EQUITY], jr.Book([], MARKET), AT, moves=(-0.10,))[0]
    fo = jr.losses([], jr.Book([SHORT_PUT], MARKET), AT, moves=(-0.10,))[0]
    both = jr.losses([EQUITY], jr.Book([SHORT_PUT], MARKET), AT, moves=(-0.10,))[0]
    assert eq.total == pytest.approx(-120_000.0)  # 1,000,000 x 1.2 x -10 %
    assert fo.total is not None and fo.total < 0
    assert abs(both.total) > abs(eq.total) and abs(both.total) > abs(fo.total)
    assert both.total == pytest.approx(eq.total + fo.total)
    assert both.fno_share == pytest.approx(fo.total / both.total)


def test_d_total_adds_beta_weighted_holdings_and_the_book_nifty_rs_delta():
    d = jr.d_total([EQUITY], jr.Book([SHORT_FUT], MARKET))
    # a future's delta to its own forward is 1: -75 x 25,000 = -1,875,000; holdings 1.2 x 1,000,000
    assert d.holdings == pytest.approx(1_200_000.0)
    assert d.fno == pytest.approx(-1_875_000.0)
    assert d.total == pytest.approx(-675_000.0) and d.complete


def test_a_holding_without_a_beta_makes_both_figures_incomplete_never_zero():
    d = jr.d_total([EQUITY, FUND], jr.Book([], MARKET))
    assert d.total is None and d.holdings is None and not d.complete
    assert any("Fake Liquid Fund" in r for r in d.reasons)
    (lo,) = jr.losses([EQUITY, FUND], jr.Book([], MARKET), AT, moves=(-0.10,))
    assert lo.total is None and lo.holdings is None and lo.fno == 0.0
    assert any("Fake Liquid Fund" in r for r in lo.reasons)


def test_an_unpriced_leg_or_an_unknown_book_makes_the_figures_incomplete():
    no_mark = bs.BookLeg("NIFTY", EXP, "put", 24000.0, -75, None, None)
    d = jr.d_total([EQUITY], jr.Book([no_mark], MARKET))
    assert d.total is None and d.fno is None and d.holdings == pytest.approx(1_200_000.0)
    assert any("no price" in r for r in d.reasons)
    unknown = jr.Book(None, MARKET, "Groww is connected but its F&O positions have not been read yet")
    (lo,) = jr.losses([EQUITY], unknown, AT, moves=(-0.10,))
    assert lo.total is None and lo.fno is None and lo.holdings == pytest.approx(-120_000.0)
    assert any("Groww" in r for r in lo.reasons)
    assert jr.d_total([EQUITY], unknown).total is None


def test_an_empty_book_contributes_exactly_zero():
    d = jr.d_total([EQUITY], jr.Book([], MARKET))
    assert d.fno == 0.0 and d.total == pytest.approx(1_200_000.0) and d.complete


def test_fno_share_is_unknown_when_the_joint_figure_is_not_a_loss():
    (up,) = jr.losses([EQUITY], jr.Book([], MARKET), AT, moves=(0.05,))
    assert up.total == pytest.approx(60_000.0) and up.fno_share is None and up.share_reason


def test_the_hedge_trigger_fires_at_a_25_percent_fno_share_of_the_minus_10_loss():
    small = jr.Holding("NSE:FAKE2", "Fake Two Ltd", 50_000.0, 1.0, "beta 1.00")
    big = jr.hedge_trigger(jr.losses([small], jr.Book([SHORT_PUT], MARKET), AT))
    assert big.fired is True and big.share >= 0.25
    quiet = jr.hedge_trigger(jr.losses([EQUITY], jr.Book([], MARKET), AT))
    assert quiet.fired is False and quiet.share == 0.0
    unknown = jr.hedge_trigger(jr.losses([FUND], jr.Book([], MARKET), AT))
    assert unknown.fired is None and unknown.reasons


def test_an_underlying_without_a_registered_beta_leaves_the_book_term_unknown():
    stock = bs.BookLeg("FAKESTK", EXP, "future", 0.0, -500, None, None)
    m = bs.Market(NOW, R, {("FAKESTK", EXP): 1500.0}, nifty=24900.0)
    d = jr.d_total([EQUITY], jr.Book([stock], m))
    assert d.fno is None and d.total is None and any("FAKESTK" in r for r in d.reasons)
    (lo,) = jr.losses([EQUITY], jr.Book([stock], m), AT, moves=(-0.10,))
    assert lo.fno is None and lo.total is None and any("FAKESTK" in r for r in lo.reasons)


def test_d_total_does_not_need_niftys_level():
    m = bs.Market(NOW, R, {("NIFTY", EXP): F}, nifty=None)
    assert jr.d_total([], jr.Book([SHORT_FUT], m)).total == pytest.approx(-1_875_000.0)
