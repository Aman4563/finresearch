"""Trade-plan arithmetic (#317, fincalc.tradeplan): strategy shapes, strikes by delta, lots from the budget, limit
guidance, stop/target levels and the verdict rule. Expected values are computed independently in the tests
(statistics.NormalDist, hand arithmetic), never by calling the code under test."""

from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from finresearch.fincalc import options as o
from finresearch.fincalc import tradeplan as tp

N = NormalDist()
L = tp.PlanLeg


def test_classify_shapes():
    assert tp.classify([L("put", 19500, "sell"), L("put", 19300, "buy")]) == "put_credit"
    assert tp.classify([L("call", 20500, "sell"), L("call", 20700, "buy")]) == "call_credit"
    assert tp.classify([L("call", 20000, "buy"), L("call", 20400, "sell")]) == "debit_call"
    assert tp.classify([L("put", 20000, "buy"), L("put", 19600, "sell")]) == "debit_put"
    ic = [L("put", 19500, "sell"), L("put", 19300, "buy"), L("call", 20500, "sell"), L("call", 20700, "buy")]
    assert tp.classify(ic) == "iron_condor"
    assert tp.classify([L("call", 20000, "buy"), L("put", 20000, "buy")]) == "long_straddle"
    assert tp.classify([L("call", 20500, "sell"), L("put", 19500, "sell")]) == "short_strangle"
    assert tp.classify([L("put", 19500, "sell")]) == "short_put"
    assert tp.classify([L("future", 20000, "buy")]) == "long_future"
    assert tp.classify([L("put", 19500, "sell", 2), L("put", 19300, "buy", 1)]) == "custom"  # a ratio spread
    assert tp.is_credit("iron_condor") and not tp.is_credit("debit_call")


def _bs_delta(right: str, s: float, k: float, t: float, r: float, v: float) -> float:
    d1 = (math.log(s / k) + (r + v * v / 2) * t) / (v * math.sqrt(t))
    return N.cdf(d1) if right == "call" else N.cdf(d1) - 1


def test_pick_by_delta_matches_an_independent_black_scholes():
    spot, t, r, v = 20000.0, 30 / 365, 0.065, 0.15
    quotes = [
        tp.ChainQuote(k, right, 15.0, 10.0, oi=100)
        for k in range(18000, 22050, 50)
        for right in ("call", "put")
    ]
    got = tp.pick_by_delta(quotes, "put", 0.20, spot, t, r)
    # independent: the OTM put strike on the 50-point grid whose |delta| is nearest 0.20
    want = min(
        (k for k in range(18000, 20001, 50)),
        key=lambda k: abs(abs(_bs_delta("put", spot, k, t, r, v)) - 0.20),
    )
    assert got is not None and got.strike == want
    # closed form: |put delta| = 0.20 at K = S·exp(−(N⁻¹(0.8)·σ√t − (r + σ²/2)t)) ≈ 19,410: the grid strike is within 50
    k_star = spot * math.exp(-(N.inv_cdf(0.8) * v * math.sqrt(t) - (r + v * v / 2) * t))
    assert abs(got.strike - k_star) <= 50
    call = tp.pick_by_delta(quotes, "call", 0.20, spot, t, r)
    assert call is not None and call.strike > spot
    # untraded strikes are never picked
    dead = [tp.ChainQuote(q.strike, q.right, q.iv, q.last, oi=0, volume=0) for q in quotes]
    assert tp.pick_by_delta(dead, "put", 0.20, spot, t, r) is None
    w = tp.wing(quotes, "put", got.strike, got.strike - 400)
    assert w is not None and w.strike == got.strike - 400


def test_size_from_budget():
    s = tp.size(-1500.0, 60.0, 4000.0, proven=True)
    assert (s.multiple, s.suggested) == (2, 2)  # floor(4000 / 1560)
    s = tp.size(-1500.0, 60.0, 4000.0, proven=False)
    assert (s.multiple, s.suggested) == (2, 1)  # half without a proven edge
    s = tp.size(-1500.0, 60.0, 1000.0, proven=False)
    assert (s.multiple, s.suggested) == (0, 0) and "above your" in s.reason
    s = tp.size(None, 60.0, 4000.0, proven=False)
    assert s.suggested == 0 and s.risk_per_multiple is None  # unlimited loss: no size, not a number


def test_limit_guidance_mid_and_far_side():
    legs = [L("put", 19500, "sell"), L("put", 19300, "buy")]
    q = [
        tp.ChainQuote(19500, "put", 15, 60, bid=59.0, ask=61.0, oi=1),
        tp.ChainQuote(19300, "put", 16, 25, bid=24.0, ask=26.0, oi=1),
    ]
    g = tp.limit_guidance(legs, q, 75)
    assert g["status"] == "ok"
    assert g["net_mid"] == pytest.approx(60 - 25)  # sell at the mid 60, buy at the mid 25
    assert g["net_fill_now"] == pytest.approx(59 - 26)  # sell at the bid, buy at the ask
    assert g["legs"][0]["limit_to"] == 59.0 and g["legs"][1]["limit_to"] == 26.0
    # slippage (#324): filling now gives up |59 − 60| + |26 − 25| = 2/share; × 1 lot × 75 = ₹150
    assert [x["slippage"] for x in g["legs"]] == [1.0, 1.0] and g["slippage_inr"] == pytest.approx(150.0)
    assert not tp.needs_bidask(q, ["put", "put"])
    q[1] = tp.ChainQuote(19300, "put", 16, 25, bid=None, ask=None, oi=1)
    assert tp.needs_bidask(q, ["put", "put"]) and not tp.needs_bidask([None], ["future"])
    g = tp.limit_guidance(legs, q, 75)
    assert g["status"] == "unknown" and g["net_mid"] is None  # never the last trade in place of a quote
    assert g["slippage_inr"] is None  # unknown, never 0


def test_stop_and_target_levels_for_a_put_credit_spread():
    lot = 75
    legs = [o.Leg("put", 19500, 60.0, -lot), o.Leg("put", 19300, 25.0, lot)]
    plan = tp.ExitPlan(50.0, 200.0, 7, True, False, "test")
    # credit 35/share = ₹2,625; max loss (200 − 35) × 75 = ₹12,375
    lv = tp.levels(legs, [0.15, 0.16], 20000.0, 30 / 365, 0.065, plan, -12375.0)
    assert lv["credit"] and lv["entry_net"] == pytest.approx(2625.0)
    stop, target = lv["stop"], lv["target"]
    assert stop["rupees"] == pytest.approx(5250.0)  # 200% of the credit
    assert stop["premium_per_share"] == pytest.approx(105.0)  # buy back at 35 + 70
    assert target["rupees"] == pytest.approx(1312.5) and target["premium_per_share"] == pytest.approx(17.5)
    # at expiry: (35 − (19500 − S)) × 75 = −5250 → S = 19,395; nothing above spot
    assert stop["underlying_at_expiry"]["below"] == pytest.approx(19395.0, abs=0.05)
    assert stop["underlying_at_expiry"]["above"] is None
    # now (today's IVs): an independent Black-Scholes P&L at the returned level is the stop
    s_now = stop["underlying_now"]["below"]
    assert s_now is not None and 19000 < s_now < 20000

    def bs_put(s, k, v, t=30 / 365, r=0.065):
        d1 = (math.log(s / k) + (r + v * v / 2) * t) / (v * math.sqrt(t))
        return k * math.exp(-r * t) * N.cdf(-(d1 - v * math.sqrt(t))) - s * N.cdf(-d1)

    pnl = (-(bs_put(s_now, 19500, 0.15) - 60.0) + (bs_put(s_now, 19300, 0.16) - 25.0)) * lot
    assert pnl == pytest.approx(-5250.0, abs=1.0)


def test_stop_beyond_max_loss_is_capped():
    legs = [
        o.Leg("put", 19500, 150.0, -75),
        o.Leg("put", 19400, 100.0, 75),
    ]  # credit 50, width 100: max loss 50/share
    lv = tp.levels(
        legs, [0.15, 0.15], 20000.0, 30 / 365, 0.065, tp.ExitPlan(50.0, 200.0, 7, True, False, "t"), -3750.0
    )
    assert lv["stop"]["rupees"] == pytest.approx(3750.0) and "maximum loss" in lv["notes"][0]


def test_verdict_rules():
    assert tp.verdict(["Max loss within your F&O budget"], "proven") == ("AVOID", None)
    assert tp.verdict([], "negative") == ("AVOID", None)  # a failed backtest overrides passing checks
    assert tp.verdict(["IV context fits the strategy"], "none") == ("WAIT", None)
    assert tp.verdict([], "proven") == ("ENTER", None)
    assert tp.verdict([], "none") == ("ENTER", tp.NO_EDGE)
    assert tp.verdict([], "unproven") == ("ENTER", tp.NO_EDGE)


def test_evidence_level_and_text():
    fail = {"status": "fails", "windows": {"oos": {"expectancy": -1789.4, "t_stat": -1.31, "trades": 42,
                                                   "from": "2024-07-01", "to": "2026-10-09"}}}  # fmt: skip
    assert tp.evidence_level(fail) == "negative"
    assert tp.evidence_text(fail, "negative") == (
        "FAILED out-of-sample: expectancy ₹-1,789/trade, t=-1.3, 42 trades (2024-07-01 → 2026-10-09)."
    )
    pos = {"status": "fails", "windows": {"oos": {"expectancy": 120.0, "t_stat": 0.4, "trades": 35}}}
    assert tp.evidence_level(pos) == "unproven"
    assert tp.evidence_level({"status": "candidate"}) == "proven"
    assert tp.evidence_level({"status": "overlay_reported"}) == "none"
    assert tp.evidence_level(None) == "none"
