"""F&O risk maths (roadmap items 5 and 18): the dated charges table, real-world and risk-neutral probabilities, expected
value after costs, expected move, max loss vs capital, risk of ruin, IV rank/percentile and skew. Golden values are
hand-computed (see each comment)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import charges as ch
from finresearch.fincalc import options as o
from finresearch.fincalc import volatility as v

# a NIFTY bull call spread, one lot of 65: buy 22800 CE at ₹150, sell 23000 CE at ₹60
SPREAD = [ch.TradeLeg("option", "buy", 150, 65), ch.TradeLeg("option", "sell", 60, 65)]


def test_charges_on_a_sample_trade_match_a_hand_computation():
    c = ch.order_costs(SPREAD, date(2026, 9, 30))
    # buy value 9,750, sell value 3,900, turnover 13,650
    assert c.turnover == 13650 and c.orders == 2
    assert c.lines["stt"] == pytest.approx(5.85)  # 3,900 x 0.15 %
    assert c.lines["exchange"] == pytest.approx(4.849845)  # 13,650 x 0.03553 %
    assert c.lines["sebi"] == pytest.approx(0.01365)  # 13,650 x ₹10/crore
    assert c.lines["stamp"] == pytest.approx(0.2925)  # 9,750 x 0.003 %
    assert c.lines["brokerage"] == 40  # 2 orders x ₹20
    assert c.lines["gst"] == pytest.approx(8.0754291)  # 18 % x (40 + 4.849845 + 0.01365)
    assert c.total == pytest.approx(59.0814241)
    # the 2026 STT rows are dated and flagged unconfirmed; they can be overridden
    stt = next(r for r in c.rates if r.key == "stt_option_sell")
    assert (
        stt.rate == Decimal("0.0015")
        and stt.status == "unconfirmed"
        and stt.effective_from == date(2026, 4, 1)
    )
    assert ch.order_costs(SPREAD, date(2026, 9, 30), overrides={"stt_option_sell": "0.001"}).lines[
        "stt"
    ] == pytest.approx(3.9)
    assert ch.order_costs(SPREAD, date(2026, 9, 30), brokerage_per_order=0).lines["brokerage"] == 0


def test_the_rate_in_force_follows_the_trade_date():
    before = ch.order_costs(SPREAD, date(2025, 6, 1))
    assert before.lines["stt"] == pytest.approx(3.9)  # 0.1 % before 1-Apr-2026
    assert before.total == pytest.approx(57.1314241)
    assert ch.rates_as_of(date(2026, 3, 31))["stt_futures_sell"].rate == Decimal("0.0002")
    assert ch.rates_as_of(date(2026, 4, 1))["stt_futures_sell"].rate == Decimal("0.0005")
    # a long 22800 call settled at 23100: 300 x 65 intrinsic x 0.15 %
    assert ch.exercise_stt(300 * 65, date(2026, 10, 6)) == pytest.approx(29.25)
    fut = ch.order_costs([ch.TradeLeg("future", "sell", 22800, 65)], date(2026, 9, 30), brokerage_per_order=0)
    assert fut.lines["stt"] == pytest.approx(22800 * 65 * 0.0005) and fut.lines["stamp"] == 0
    assert [x.side for x in ch.flip(SPREAD)] == ["sell", "buy"]


def test_real_world_probability_is_n_d2_with_the_stated_drift():
    # S = K = 100, sigma 20 %, T = 1: d2* = (mu - 0.02) / 0.2
    assert o.prob_above(100, 100, 1, 0.2, drift=0.0) == pytest.approx(0.460172, abs=1e-6)  # N(-0.10)
    assert o.prob_above(100, 100, 1, 0.2, drift=0.05) == pytest.approx(0.559618, abs=1e-6)  # N(0.15)
    # a long 100 call bought for 5 profits above 105: the grid PoP equals the closed form
    call = [o.Leg("call", 100, 5, 1)]
    for mu in (0.0, 0.05, 0.12):
        assert o.probability_of_profit(call, 100, 1, 0.2, drift=mu) == pytest.approx(
            o.prob_above(100, 105, 1, 0.2, mu), abs=2e-4)  # fmt: skip
    assert o.probability_of_profit(call, 100, 1, 0.2, 0.05) == pytest.approx(
        o.prob_above(100, 105, 1, 0.2, 0.05), abs=2e-4
    )


def test_ev_at_fair_prices_is_minus_costs():
    s, t, r, vol = 100.0, 0.25, 0.05, 0.2
    legs = [o.Leg("call", 100, o.bs_price("call", s, 100, t, r, vol), 1),
            o.Leg("call", 110, o.bs_price("call", s, 110, t, r, vol), -1)]  # fmt: skip
    rn = o.outcome(legs, s, t, vol, drift=r, rate=r, entry_cost=0.3)
    assert rn.ev_gross == pytest.approx(0.0, abs=1e-4) and rn.ev == pytest.approx(-0.3, abs=1e-4)
    # a short straddle priced at 20 % vol, when the price really moves at 12 %: positive real-world EV (the variance
    # risk premium), and a higher chance of profit than the risk-neutral one
    straddle = [o.Leg("call", 100, o.bs_price("call", s, 100, t, r, vol), -1),
                o.Leg("put", 100, o.bs_price("put", s, 100, t, r, vol), -1)]  # fmt: skip
    rw = o.outcome(straddle, s, t, 0.12, drift=r, rate=r)
    assert rw.ev > 0 and rw.pop > o.outcome(straddle, s, t, vol, drift=r, rate=r).pop
    # quantiles are ordered and a capped spread never loses more than its debit
    q = rn.quantiles
    assert q["p10"] <= q["p25"] <= q["p50"] <= q["p75"] <= q["p90"]
    assert q["p10"] >= -(legs[0].premium - legs[1].premium) - 0.3 - 1e-9
    # exit costs (STT on exercised long calls) lower the EV by their discounted expectation
    xc = o.outcome(legs, s, t, vol, drift=r, rate=r, exit_cost=lambda p: 0.0015 * max(0.0, p - 100))
    assert xc.ev < rn.ev + 0.3 and xc.exit_cost > 0


def test_expected_move_realised_vol_ruin_and_capital_check():
    assert o.expected_move(100, 0.2, 0.25) == pytest.approx((10.0, 90.0, 110.0))  # 100 x 0.2 x 0.5
    # log returns ln(1.01), ln(99/101), ln(100/99): mean 0, sample var 0.00030002 -> x 252 -> sqrt
    assert o.realised_vol([100, 101, 99, 100]) == pytest.approx(0.274965, abs=1e-5)
    assert o.realised_vol([100, 101, 99, 100, 100], window=2) == pytest.approx(
        math.sqrt(((math.log(100 / 99) - math.log(100 / 99) / 2) ** 2 * 2) * 252), abs=1e-9)  # fmt: skip
    with pytest.raises(ValueError):
        o.realised_vol([100, 101])
    assert o.risk_of_ruin(0.1, 10) == pytest.approx((0.9 / 1.1) ** 10) and o.risk_of_ruin(0.0, 10) == 1.0
    ok = o.max_loss_check(-1950, 100000, 2)
    assert ok.within and ok.pct_of_capital == pytest.approx(1.95)
    assert not o.max_loss_check(-2500, 100000, 2).within and not o.max_loss_check(None, 100000, 2).within
    assert not o.max_loss_check(-100, 0, 2).within
    # a strategy that always loses 1 % of capital: a 5 % drawdown is certain within 10 repeats, impossible in 4
    dist = [(-1000.0, 1.0)]
    assert (
        o.drawdown_probability(dist, 100000, 0.05, 10) == 1.0
        and o.drawdown_probability(dist, 100000, 0.05, 4) == 0.0
    )


def test_iv_rank_percentile_and_skew():
    short = v.iv_stats([15.0] * 59)
    assert short.rank is None and short.status == "insufficient history (59 days; needs 60)"
    s = v.iv_stats([*range(10, 70), 30])  # 61 days, low 10, high 69, today 30
    assert s.n == 61 and s.rank == pytest.approx(20 / 59) and s.percentile == pytest.approx(20 / 60)
    assert v.iv_stats(list(range(300))).n == 252  # only the last 252 days count
    rows = [v.StrikeIv(k, 20.0, 25.0) for k in range(80, 125, 1)]
    assert v.skew_25d(rows, 100, 30 / 365, 0.065) == pytest.approx(5.0)
    assert v.skew_25d([v.StrikeIv(100, 20, 20)], 100, 30 / 365, 0.065) is None  # one strike spans no deltas
    assert v.atm_iv([v.StrikeIv(22750, 13.0, 14.0), v.StrikeIv(22800, 12.0, 13.0)], 22780.25) == (22800, 12.5)


def test_profit_intervals_are_exact_for_multi_leg_payoffs():
    straddle = [o.Leg("call", 100, 5, 1), o.Leg("put", 100, 5, 1)]
    assert o.profit_intervals(lambda p: o.payoff(straddle, p), [100]) == [(0.0, 90.0), (110.0, math.inf)]
    # P(S_T < 90) + P(S_T > 110), each a closed-form N(d2*) term
    want = 1 - o.prob_above(100, 90, 0.5, 0.25, 0.0) + o.prob_above(100, 110, 0.5, 0.25, 0.0)
    assert o.probability_of_profit(straddle, 100, 0.5, 0.25, drift=0.0) == pytest.approx(want, abs=1e-12)
    condor = [
        o.Leg("put", 90, 1, 1),
        o.Leg("put", 95, 2.5, -1),
        o.Leg("call", 105, 2.5, -1),
        o.Leg("call", 110, 1, 1),
    ]
    assert o.profit_intervals(lambda p: o.payoff(condor, p), [90, 95, 105, 110]) == [(92.0, 108.0)]
    grid = sum(w for p, w in o.lognormal_grid(100, 0.1, 0.2, 0.0, 20000) if o.payoff(condor, p) > 0)
    assert o.probability_of_profit(condor, 100, 0.1, 0.2, drift=0.0) == pytest.approx(grid, abs=2e-4)
