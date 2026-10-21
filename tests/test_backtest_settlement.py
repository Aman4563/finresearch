"""#352 (P0.2) backtest settlement: on an expiry's last session every expiring option row carries one identical
settle, the underlying's final settlement price. Settle there, cross-checked against the expiring future or the
underlying (< 1 bp) when one exists; otherwise None (unknown, never a guess). The fixture is cut from NSE's legacy
bhavcopy of 13-Jan-2022 (BANKNIFTY weekly expiry: no same-expiry future, no underlying column), where the old
fallback (median of K ± settle) gave 3,680.05 against the true 38,469.95."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from test_fno_bhav_store import clean, zipped  # noqa: F401  (fixture)

from finresearch.backtest import engine, pricing
from finresearch.backtest.history import Day, Quote

FX = Path(__file__).parent / "fixtures" / "fno_bhav"
LEGACY_2022 = (FX / "legacy_fo13JAN2022_banknifty_sample.csv").read_text()
D, E = date(2022, 1, 13), date(2022, 1, 13)
S_TRUE = 38469.95  # every expiring BANKNIFTY option row's SETTLE_PR on 13-Jan-2022


def _day_2022():
    from finresearch.backtest.dbhistory import DbHistory
    from finresearch.db import session_scope
    from finresearch.fnohist import store

    with session_scope() as s:
        store.load_day(s, D, "legacy", "l", zipped(LEGACY_2022), [])
    return DbHistory().day("BANKNIFTY", D)


def test_2022_legacy_weekly_settles_at_the_uniform_option_settle(clean):  # noqa: F811
    x = _day_2022()
    assert E not in x.futures and x.spot is None  # the old code's two first choices are both missing
    assert pricing.settlement_price(x, E) == (S_TRUE, "uniform option settle")


def test_2022_legacy_weekly_trade_settles_without_options_intrinsic(clean):  # noqa: F811
    x = _day_2022()
    legs = [engine.Leg("call", 38500.0, "sell", 25, 120.0, 0.15, 120.0),
            engine.Leg("call", 39000.0, "buy", 25, 40.0, 0.15, 40.0)]  # fmt: skip
    p = engine.Position("t", "BANKNIFTY", E, date(2022, 1, 10), 3, 1, 25, legs, 80 * 25, 420 * 25, None, 0.0,
                        None, None, None)  # fmt: skip
    tr = engine.settle(p, x)
    # 38,469.95 is below both strikes: both calls expire worthless, the credit is kept: (120 − 40) × 25
    assert tr["gross"] == pytest.approx(2000.0) and tr["exercise_stt"] == 0
    assert not any("options_intrinsic" in f for f in p.flags)


def _opt(right, k, settle, e=E):
    return Quote(e, right, k, None, settle, 0, 10, None)


def test_non_uniform_settles_without_a_cross_check_are_unknown():
    x = Day(E, "NIFTY")
    x.options[(E, "call", 17300.0)] = _opt("call", 17300.0, 150.4)
    x.options[(E, "put", 17600.0)] = _opt("put", 17600.0, 149.6)
    assert pricing.settlement_price(x, E) is None  # never the median of K ± settle


def test_uniform_settle_is_cross_checked_against_the_future_to_one_basis_point():
    x = Day(E, "NIFTY", futures={E: 18000.0})
    for k in (17900.0, 18100.0):
        x.options[(E, "call", k)] = _opt("call", k, 18000.0)
    assert pricing.settlement_price(x, E) == (18000.0, "uniform option settle")
    x.futures[E] = 18000.0 * (1 + 0.00011)  # 1.1 bp away: the two disagree, so unknown
    assert pricing.settlement_price(x, E) is None
    x.futures[E] = 18000.0 * (1 + 0.00009)  # 0.9 bp: accepted
    assert pricing.settlement_price(x, E) == (18000.0, "uniform option settle")


def test_without_option_settles_the_expiring_future_or_underlying_is_used():
    x = Day(E, "NIFTY", futures={E: 18000.0})
    assert pricing.settlement_price(x, E) == (18000.0, "future")
    y = Day(E, "NIFTY", spot=18010.0)
    assert pricing.settlement_price(y, E) == (18010.0, "underlying")
    assert pricing.settlement_price(Day(E, "NIFTY"), E) is None
    # options of another expiry never set this expiry's settlement
    z = Day(E, "NIFTY")
    z.options[(date(2022, 1, 20), "call", 18000.0)] = _opt("call", 18000.0, 18000.0, date(2022, 1, 20))
    assert pricing.settlement_price(z, E) is None
