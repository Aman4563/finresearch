"""F&O backtester (#307): fills, sizing, costs, expiry settlement with exercise STT, management rules and the
no-look-ahead guarantee. Synthetic history only (tests/fno_synth.py); expected values are worked out here by hand
from the stated rates and formulas, not by calling the code under test."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import fno_synth as fs
import pytest

from finresearch.backtest import engine, pricing, specs
from finresearch.backtest.history import Clocked, Day, LookAheadError, MemoryHistory, Quote
from finresearch.fincalc.fno_history import black76_price

BOOK = specs.BOOK
R = 0.065


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def charges(sell_value: float, buy_value: float, orders: int, stt_sell: float = 0.001) -> float:
    """fincalc.charges' rule written out: STT on sells, NSE 0.03553 % + SEBI ₹10/crore both sides, stamp 0.003 % on
    buys, ₹20 a order, GST 18 % on brokerage + exchange + SEBI (rates for a trade date from 1-Oct-2024 to 31-Mar-2026)."""
    turnover = sell_value + buy_value
    exch, sebi, brokerage = turnover * 0.0003553, turnover * 0.000001, 20.0 * orders
    return (
        sell_value * stt_sell
        + exch
        + sebi
        + buy_value * 0.00003
        + brokerage
        + 0.18 * (brokerage + exch + sebi)
    )


@pytest.mark.parametrize(
    ("ref", "contracts", "side", "expected"),
    [
        (100.0, 1500, "buy", 101.00),  # 1 % tier
        (100.0, 1500, "sell", 99.00),
        (2.0, 5, "buy", 2.16),  # 8 % of 2 = 0.16 > the 0.10 minimum
        (1.0, 0, "sell", 0.85),  # untraded: settlement price, 15 %
        (0.5, 1000, "sell", 0.40),  # 1 % = 0.005 < 0.10 minimum
        (0.1, 50, "sell", 0.05),  # never below one tick
        (250.0, 150, "buy", 255.00),  # 2 % tier
        (40.0, 12, "sell", 38.40),  # 4 % tier
    ],
)
def test_fill_slippage_tiers(ref, contracts, side, expected):
    assert pricing.fill(ref, side, contracts, BOOK) == pytest.approx(expected)


def test_stress_book_doubles_slippage():
    assert pricing.fill(100.0, "buy", 1500, replace(BOOK, slippage_mult=2.0)) == pytest.approx(102.0)


# --------------------------------------------------------------------------- one entry, worked out by hand
D0, EXP = date(2025, 1, 10), date(2025, 2, 13)  # 34 days
F, VOL, LOT = 20000.0, 0.15, 75


def clk(x: Day) -> Clocked:
    c = Clocked(MemoryHistory([x]))
    c.advance(x.day)
    return c


def put_day(contracts_short: int = 500, contracts_wing: int = 50) -> Day:
    x = Day(D0, "NIFTY", spot=None, iv30=15.0)
    x.futures[EXP] = F
    t = (EXP - D0).days / 365
    for k in range(18000, 20100, 100):
        for right in ("put", "call"):
            p = round(black76_price(right, F, float(k), t, R, VOL), 2)
            n = contracts_wing if k % 200 else contracts_short
            x.options[(EXP, right, float(k))] = Quote(EXP, right, float(k), p, p, n, 10_000, LOT)
    return x


def test_put_credit_entry_strikes_fills_size_and_costs():
    x = put_day()
    t = (EXP - D0).days / 365

    # the traded OTM put (>= 10 contracts) whose Black-76 |delta| = e^(-rT) N(-d1) is nearest 0.20
    def pdelta(k: float) -> float:
        d1 = (math.log(F / k) + VOL * VOL * t / 2) / (VOL * math.sqrt(t))
        return math.exp(-R * t) * ncdf(-d1)

    short_k = min((k for k in range(18000, 20001, 100)), key=lambda k: abs(pdelta(k) - 0.20))
    wing_k = short_k - 400  # 2 % of the 20,000 forward
    st = engine.SpecState(replace(specs.by_key("nifty_pcs_m"), iv_pct_min=None))
    p = engine.try_enter(st, x, "NIFTY", clk(x), [15.0], BOOK)
    assert p is not None, st.skips
    assert [(lg.strike, lg.side) for lg in p.legs] == [(short_k, "sell"), (wing_k, "buy")]
    s_ref, w_ref = x.options[(EXP, "put", float(short_k))].close, x.options[(EXP, "put", float(wing_k))].close
    s_fill = (
        round(s_ref - max(0.02 * s_ref, 0.10), 2)
        if short_k % 200 == 0
        else round(s_ref - max(0.04 * s_ref, 0.1), 2)
    )
    w_fill = (
        round(w_ref + max(0.02 * w_ref, 0.10), 2)
        if wing_k % 200 == 0
        else round(w_ref + max(0.04 * w_ref, 0.1), 2)
    )
    assert (p.legs[0].entry, p.legs[1].entry) == (pytest.approx(s_fill), pytest.approx(w_fill))
    credit = s_fill - w_fill
    one_lot_costs = charges(s_fill * LOT, w_fill * LOT, 2)
    lots = math.floor(1_000_000 * 0.05 / ((400 - credit) * LOT + one_lot_costs))
    assert p.lots == lots and p.lot == LOT
    assert p.premium == pytest.approx(credit * lots * LOT)
    assert p.max_loss == pytest.approx((400 - credit) * lots * LOT)
    assert p.entry_costs == pytest.approx(charges(s_fill * lots * LOT, w_fill * lots * LOT, 2))
    assert p.margin is not None and p.margin > 0


def test_entry_refused_without_lot_size_or_iv_or_liquidity():
    x = put_day()
    x2 = Day(
        D0,
        "NIFTY",
        options={k: replace(q, lot=None) for k, q in x.options.items()},
        futures=x.futures,
        iv30=15.0,
    )
    st = engine.SpecState(replace(specs.by_key("nifty_pcs_m"), iv_pct_min=None))
    assert engine.try_enter(st, x2, "NIFTY", clk(x2), [15.0], BOOK) is None
    assert st.skips == {"lot size unknown": 1}  # never a default lot
    x3 = Day(D0, "NIFTY", options=x.options, futures=x.futures, iv30=None)
    st = engine.SpecState(specs.by_key("nifty_pcs_m"))  # IV filter on, today's IV unknown
    assert engine.try_enter(st, x3, "NIFTY", clk(x3), [15.0] * 100, BOOK) is None
    assert st.skips == {"IV percentile unknown": 1}
    thin = put_day(contracts_short=5, contracts_wing=5)  # no strike traded 10 contracts
    st = engine.SpecState(replace(specs.by_key("nifty_pcs_m"), iv_pct_min=None))
    assert engine.try_enter(st, thin, "NIFTY", clk(thin), [15.0], BOOK) is None
    assert list(st.skips) == ["no traded put near 0.2 delta"]


# --------------------------------------------------------------------------- expiry settlement and management
def position(
    legs: list[engine.Leg], expiry: date, premium: float, entry_costs: float = 50.0
) -> engine.Position:
    return engine.Position("t", "NIFTY", expiry, date(2025, 1, 2), 30, 1, 75, legs, premium, None, None,
                           entry_costs, 15.0, 0.5)  # fmt: skip


@pytest.mark.parametrize(("expiry", "stt_rate"), [(date(2025, 2, 13), 0.00125), (date(2026, 5, 26), 0.0015)])
def test_expiry_settlement_charges_exercise_stt_on_itm_longs_only(expiry, stt_rate):
    legs = [engine.Leg("call", 20000.0, "buy", 75, 300.0, 0.15, 300.0),
            engine.Leg("call", 20400.0, "sell", 75, 120.0, 0.15, 120.0)]  # fmt: skip
    p = position(legs, expiry, -180 * 75)
    x = Day(expiry, "NIFTY", futures={expiry: 20250.0})  # the expiring future settles at the final price
    tr = engine.settle(p, x)
    # long 20000 C worth 250, short 20400 C worth 0: (250 - 300) x 75 + (120 - 0) x 75
    assert tr["gross"] == pytest.approx(5250.0)
    assert tr["exercise_stt"] == pytest.approx(
        250 * 75 * stt_rate, abs=0.005
    )  # Finance (No. 2) Act 2024 / Finance Bill 2026
    assert tr["net"] == pytest.approx(5250.0 - 50.0 - 250 * 75 * stt_rate, abs=0.01)
    assert tr["exit_costs"] == 0 and tr["reason"] == "expiry"
    # a short leg that finishes in the money pays no exercise STT (the writer is assigned)
    short_itm = position([engine.Leg("put", 20500.0, "sell", 75, 200.0, 0.15, 200.0)], expiry, 200 * 75)
    assert engine.settle(short_itm, x)["exercise_stt"] == 0


def test_settlement_price_falls_back_to_option_intrinsic_without_a_future():
    e = date(2023, 3, 2)  # a weekly expiry before the UDiFF file: no future, no underlying value
    x = Day(e, "NIFTY")
    x.options[(e, "call", 17300.0)] = Quote(e, "call", 17300.0, 150.0, 150.4, 900, 1, None)
    x.options[(e, "put", 17600.0)] = Quote(e, "put", 17600.0, 149.0, 149.6, 900, 1, None)
    assert pricing.settlement_price(x, e) == (pytest.approx(17450.4), "options_intrinsic")


def test_management_rules_fire_in_order():
    spec = specs.by_key("nifty_pcs_m")  # take 50 % of the credit, stop at 2x the credit, exit at 7 days
    e = date(2025, 2, 27)
    legs = lambda m1, m2: [engine.Leg("put", 19000.0, "sell", 75, 100.0, 0.15, m1),  # noqa: E731
                           engine.Leg("put", 18600.0, "buy", 75, 40.0, 0.15, m2)]  # fmt: skip
    credit = 60 * 75
    d = date(2025, 2, 3)
    assert engine.exit_reason(position(legs(70.0, 31.0), e, credit), spec, d, date(2025, 2, 4)) is None
    # gain = (100 - 45) - (40 - 15) = 30 a unit = 50 % of the 60 credit -> profit take
    assert engine.exit_reason(position(legs(45.0, 15.0), e, credit), spec, d, None) == "profit_take"
    # loss = (220 - 100) - (100 - 40) = 60... not 2x; at (260, 80): (160) - 40 = 120 a unit = 2x credit -> stop
    assert engine.exit_reason(position(legs(220.0, 100.0), e, credit), spec, d, None) is None
    assert engine.exit_reason(position(legs(260.0, 80.0), e, credit), spec, d, None) == "stop"
    assert engine.exit_reason(position(legs(70.0, 31.0), e, credit), spec, date(2025, 2, 20), None) == "dte"
    weekly = specs.by_key("nifty_ic_w")  # no DTE rule: out on the last session before expiry day
    tue = date(2025, 2, 25)
    assert engine.exit_reason(position(legs(70.0, 31.0), e, credit), weekly, tue, date(2025, 2, 26)) is None
    # Wednesday is a holiday: Tuesday's close is the last session before Thursday's expiry
    assert engine.exit_reason(position(legs(70.0, 31.0), e, credit), weekly, tue, e) == "before_expiry_day"


def test_full_run_exits_and_costs_are_consistent():
    h = fs.history(date(2022, 1, 3), date(2022, 12, 30))
    sp = [replace(s, iv_pct_min=None) for s in specs.SPECS if s.underlying == "NIFTY"]
    res = engine.run(sp, h, date(2022, 2, 1), date(2022, 11, 30), warmup_days=40)
    assert all(r["trades"] for r in res.values())
    for key, r in res.items():
        hold = specs.by_key(key).hold_to_expiry
        for t in r["trades"]:
            assert t["exit_day"] <= t["expiry"] if hold else t["exit_day"] < t["expiry"]
            assert t["net"] == pytest.approx(
                t["gross"] - t["entry_costs"] - t["exit_costs"] - t["exercise_stt"], abs=0.02
            )
            # defined risk holds up to the exit's slippage (at most 15 % of the price or ₹0.10 a unit per leg)
            slip = (
                0
                if t["reason"] == "expiry"
                else sum(x["qty"] * max(0.15 * x["exit"] / 0.85, 0.1) for x in t["legs"])
            )
            assert t["max_loss"] is not None and t["gross"] >= -t["max_loss"] - slip - 1e-6
            assert t["exercise_stt"] == 0 or t["reason"] == "expiry"
        assert not r["open_at_end"]  # entries stopped a month before the data ends
        # the equity curve ends at the capital plus every closed trade's net P&L (to the paisa per trade)
        assert r["equity"][-1][1] == pytest.approx(
            BOOK.capital + sum(t["net"] for t in r["trades"]), abs=0.01 * len(r["trades"])
        )


# --------------------------------------------------------------------------- no look-ahead
def test_clock_refuses_a_later_day():
    h = Clocked(fs.history(date(2022, 1, 3), date(2022, 1, 31)))
    h.advance(date(2022, 1, 10))
    assert h.day("NIFTY", date(2022, 1, 10)) is not None
    with pytest.raises(LookAheadError):
        h.day("NIFTY", date(2022, 1, 11))
    with pytest.raises(LookAheadError):
        h.lot("NIFTY", date(2022, 1, 27), date(2022, 1, 11))


def test_decisions_up_to_a_day_ignore_every_later_price():
    """Two histories identical to T, then one crashes 25 %: every trade closed by T, every entry made by T and the
    equity curve to T are identical; the runs differ only afterwards."""
    start, end, cut = date(2022, 1, 3), date(2022, 12, 30), date(2022, 7, 15)
    base = fs.path(start, end, seed=11)
    crash = {d: (s if d <= cut else s * 0.75) for d, s in base.items()}
    sp = [replace(s, iv_pct_min=None) for s in specs.SPECS if s.underlying == "NIFTY"]
    a = engine.run(sp, fs.history(start, end, prices=base), date(2022, 2, 1), end, warmup_days=40)
    b = engine.run(sp, fs.history(start, end, prices=crash), date(2022, 2, 1), end, warmup_days=40)
    differs = False
    for key in a:
        closed = lambda r: [t for t in r["trades"] if t["exit_day"] <= cut.isoformat()]  # noqa: E731
        assert closed(a[key]) == closed(b[key])
        entered = lambda r: [(t["entry_day"], t["legs"][0]["entry"], t["lots"]) for t in r["trades"]  # noqa: E731
                             if t["entry_day"] <= cut.isoformat()]  # fmt: skip
        assert entered(a[key]) == entered(b[key])
        assert [e for e in a[key]["equity"] if e[0] <= cut] == [e for e in b[key]["equity"] if e[0] <= cut]
        differs |= a[key]["equity"][-1] != b[key]["equity"][-1]
        assert a[key]["max_day_read"] <= end
    assert differs  # the crash after T did change the later results


def test_current_setup_gives_the_rule_legs_at_the_last_close():
    from finresearch.backtest import setup

    h = fs.history(date(2025, 1, 2), date(2025, 1, 15))
    spec = replace(specs.by_key("nifty_pcs_m"), iv_pct_min=None)
    got = setup.current_setup(spec, h)
    assert got["status"] == "entry" and got["as_of"] == "2025-01-15" and got["expiry"] == "2025-02-27"
    assert [lg["side"] for lg in got["legs"]] == ["sell", "buy"] and got["max_loss"] > 0
    late = setup.current_setup(spec, fs.history(date(2025, 1, 2), date(2025, 1, 31)))  # 27 days to Feb; Mar 27 - 45 days = Feb 10
    assert late["status"] == "wait" and "window opens on 10-Feb-2025" in late["reason"]
    assert setup.current_setup(replace(spec, underlying="BANKNIFTY"), h)["status"] == "no_data"
