"""The collectable form of the VRP (P3.6, #389; vrp/collect.py): a short ATM straddle (and a short 25-delta put) held
from one monthly expiry to the next, 1 lot, after dated charges and slippage, un-hedged, band-hedged in whole futures
lots and fractionally hedged (the benchmark). Expected values are computed here by hand."""

import math
from dataclasses import replace
from datetime import date, timedelta

import pytest

from finresearch.backtest.specs import BOOK
from finresearch.fincalc import black76 as b76
from finresearch.fincalc import charges
from finresearch.vrp import collect as co

E0, E1 = date(2024, 1, 25), date(2024, 2, 29)
RATES = {"stt_option_sell": 0.001, "stt_option_exercise": 0.00125, "stt_futures_sell": 0.0002,
         "exchange_option": 0.0005, "exchange_futures": 0.00002, "sebi_fee": 0.000001, "stamp_option_buy": 0.00003,
         "stamp_futures_buy": 0.00002, "gst": 0.18}  # fmt: skip
FREE = dict.fromkeys(RATES, 0.0)
NOSLIP = replace(BOOK, slippage_tiers=((0, 0.0),), min_slippage=0.0)


def straddle(marks, final=1080.0, lot=50):
    return co.Trade(E0, E1, 1000.0, lot, tuple(marks), final, ("call", "put"))


def test_unhedged_straddle_after_slippage_and_charges_by_hand() -> None:
    t = straddle([co.Mark(E0, 1001.0, 100.0, 90.0, 2000, 2000)])
    r = co.pnl(t, "none", rate=0.065, overrides=RATES, brokerage=20.0)
    fills = (100 - 1.0, 90 - 0.9)  # the 1 % tier at >= 1,000 contracts
    value = sum(fills) * 50
    stt, exch, sebi = value * 0.001, value * 0.0005, value * 0.000001
    costs = stt + exch + sebi + 40 + (40 + exch + sebi) * 0.18
    assert r.reason is None
    assert r.gross == pytest.approx(value - 80 * 50)
    assert r.costs == pytest.approx(costs)
    assert r.net == pytest.approx(value - 80 * 50 - costs)
    assert r.slippage == pytest.approx((1.0 + 0.9) * 50)


def test_the_dated_and_the_2026_charges() -> None:
    t = co.Trade(date(2022, 6, 30), date(2022, 7, 28), 15800.0, 50, (co.Mark(date(2022, 6, 30), 15790.0, 300.0,
                 310.0, 5000, 5000),), 15600.0, ("call", "put"))  # fmt: skip
    dated = co.pnl(t, "none", rate=0.065)
    today = co.pnl(t, "none", rate=0.065, overrides=co.collectable_today_overrides())
    legs = [charges.TradeLeg("option", "sell", 297.0, 50), charges.TradeLeg("option", "sell", 306.9, 50)]
    assert dated.costs == pytest.approx(charges.order_costs(legs, date(2022, 6, 30)).total)
    o26 = {k: v.rate for k, v in charges.rates_as_of(co.COLLECTABLE_TODAY).items()}
    assert today.costs == pytest.approx(charges.order_costs(legs, date(2022, 6, 30), overrides=o26).total)
    assert today.costs > dated.costs  # STT on option sales rose from 0.05 % to 0.15 % (2026 rate unconfirmed)


def test_unknowns_are_counted_never_zero() -> None:
    m = co.Mark(E0, 1001.0, 100.0, 90.0, 2000, 2000)
    assert co.pnl(straddle([m], lot=None), "none", rate=0.065).reason == "no dated lot size"
    old = co.Trade(date(2008, 1, 31), date(2008, 2, 28), 5000.0, 50, (replace(m, day=date(2008, 1, 31)),), 5100.0,
                   ("call", "put"))  # fmt: skip
    assert "charges" in co.pnl(old, "none", rate=0.065).reason
    assert "settlement" in co.pnl(straddle([m], final=None), "none", rate=0.065, overrides=RATES).reason
    gap = [m, replace(m, day=E0 + timedelta(1), call=None, put=None)]
    assert (
        co.pnl(straddle(gap), "none", rate=0.065, overrides=RATES).reason is None
    )  # un-hedged needs no marks
    assert "IV" in co.pnl(straddle(gap), "band", rate=0.065, overrides=RATES).reason


def _marks(path):
    """Marks at Black-76 prices with a 20 % volatility along a futures path, one per calendar day from E0."""
    out = []
    for i, f in enumerate(path):
        d = E0 + timedelta(i)
        t = (E1 - d).days / 365
        df = math.exp(-0.065 * t)
        out.append(co.Mark(d, f, b76.price("call", f, 1000, t, df, 0.2), b76.price("put", f, 1000, t, df, 0.2), 5000,
                           5000))  # fmt: skip
    return out


def _delta(f, d):
    t = (E1 - d).days / 365
    df = math.exp(-0.065 * t)
    return b76.forward_delta("call", f, 1000, t, df, 0.2) + b76.forward_delta("put", f, 1000, t, df, 0.2)


def test_band_hedge_trades_whole_lots_only_when_the_net_delta_reaches_half_a_lot() -> None:
    path = [1000.0, 1010.0, 1100.0, 1105.0, 1000.0]
    marks = _marks(path)
    r = co.pnl(straddle(marks, final=990.0), "band", rate=0.065, overrides=FREE, brokerage=0.0, book=NOSLIP,
               fut_slippage=0.0)  # fmt: skip
    h, hs = 0, []
    for mk in marks:
        x = _delta(mk.fut, mk.day)
        if abs(h - x) >= 0.5:
            h = math.floor(x + 0.5)
        hs.append(h)
    assert r.hedges == tuple(hs) and any(hs) and all(isinstance(x, int) for x in hs)
    hedge = sum(hs[i] * (path[i + 1] - path[i]) for i in range(len(path) - 1)) + hs[-1] * (990.0 - path[-1])
    unhedged = co.pnl(
        straddle(marks, final=990.0), "none", rate=0.065, overrides=FREE, brokerage=0.0, book=NOSLIP
    )
    assert r.hedge_pnl == pytest.approx(hedge * 50)
    assert r.net == pytest.approx(unhedged.net + hedge * 50)
    frac = co.pnl(straddle(marks, final=990.0), "fractional", rate=0.065, overrides=FREE, brokerage=0.0, book=NOSLIP,
                  fut_slippage=0.0)  # fmt: skip
    xs = [_delta(mk.fut, mk.day) for mk in marks]
    exp = sum(xs[i] * (path[i + 1] - path[i]) for i in range(len(path) - 1)) + xs[-1] * (990.0 - path[-1])
    assert frac.hedge_pnl == pytest.approx(exp * 50)


def test_hedge_trades_pay_futures_charges_and_slippage() -> None:
    marks = _marks([1000.0, 1100.0])
    r = co.pnl(straddle(marks, final=1100.0), "band", rate=0.065, overrides=RATES, brokerage=20.0, book=NOSLIP,
               fut_slippage=0.5)  # fmt: skip
    base = co.pnl(
        straddle(marks, final=1100.0), "none", rate=0.065, overrides=RATES, brokerage=20.0, book=NOSLIP
    )
    (n,) = [x for x in r.hedges if x]  # one buy of n lots on day 2 at 1100 + 0.5, settled at 1100
    value = (1100.5) * n * 50
    exch, sebi = value * 0.00002, value * 0.000001
    fut_costs = exch + sebi + value * 0.00002 + 20 + (20 + exch + sebi) * 0.18
    assert r.costs == pytest.approx(base.costs + fut_costs)
    assert r.hedge_pnl == pytest.approx(-0.5 * n * 50)


def test_the_25_delta_put_is_the_market_put_nearest_minus_a_quarter() -> None:
    f, t = 1000.0, 35 / 365
    df = math.exp(-0.065 * t)
    puts = [(k, b76.price("put", f, k, t, df, 0.2 + (1000 - k) / 2000), 500) for k in range(800, 1001, 10)]
    k = co.delta_put_strike(puts, f, t, 0.065)
    d = {x: b76.forward_delta("put", f, x, t, df, 0.2 + (1000 - x) / 2000) for x, _, _ in puts}
    assert k == min(d, key=lambda x: abs(d[x] + 0.25))
    assert (
        co.delta_put_strike([(900.0, 1.0, 2)], f, t, 0.065) is None
    )  # too few contracts: not a market price


def test_summary_counts_unknowns_crash_losses_and_negative_months() -> None:
    rs = [co.Result(date(2008, 10, 30), -5000.0, -4800.0, 200.0, 0.0, 0.0, (), None, None, None),
          co.Result(date(2009, 1, 29), 2000.0, 2100.0, 100.0, 0.0, 0.0, (), None, None, None),
          co.Result(date(2010, 1, 28), 1000.0, 1100.0, 100.0, 0.0, 0.0, (), 100_000.0, 600.0, 400.0),
          co.Result(date(2011, 1, 27), None, None, None, 0.0, 0.0, (), None, None, None, "no dated lot size")]  # fmt: skip
    s = co.summarise(rs, draws=200, seed=389)
    assert s["n"] == 3 and s["unknown"] == 1 and s["unknown_reasons"] == {"no dated lot size": 1}
    assert s["mean_net"] == pytest.approx(-2000 / 3) and s["share_negative"] == pytest.approx(1 / 3)
    assert s["crash"]["2008-09..2009-03"] == {"windows": 2, "net": -3000.0, "worst": -5000.0}
    assert s["crash"]["2020-02..2020-06"] == {"windows": 0, "net": None, "worst": None}
    assert s["margin"]["n"] == 1 and s["margin"]["mean_net_after_carry"] == pytest.approx(400.0)
    assert set(s["ci"]) >= {"lo", "hi", "draws"} and s["ci"]["draws"] == 200
