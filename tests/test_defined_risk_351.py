"""#351 (P0.1): the defined-risk gate needs BOTH payoff slopes bounded. A naked short put, a put ratio spread and a long
future + short call have a finite loss only because a price cannot fall below zero; they are not defined risk.

Slopes are recomputed here from first principles (not from fincalc): above every strike the payoff moves by
Σq(calls) + Σq(futures) per point, below every strike by Σq(futures) − Σq(puts)."""

from __future__ import annotations

import json
import random
from datetime import date

import pytest
from test_fno_signal import FNO, chain

from finresearch.adapters.nse_fno import parse_index_history
from finresearch.fincalc import options as o
from finresearch.signals import fno as fs


def _slopes(legs):
    up = sum(x.qty for x in legs if x.right in ("call", "future"))
    down = sum(x.qty for x in legs if x.right == "future") - sum(x.qty for x in legs if x.right == "put")
    return up, down


def test_random_baskets_are_defined_only_when_both_slopes_are_bounded():
    rng = random.Random(351)
    spot = 22780.0
    strikes = [spot + 100 * i for i in range(-15, 16)]
    seen = {"defined": 0, "down": 0, "up": 0}
    for _ in range(10_000):
        legs = []
        for _ in range(rng.randint(1, 8)):
            right = rng.choice(("call", "put", "future"))
            qty = rng.choice((-3, -2, -1, 1, 2, 3)) * 65
            if right == "future":
                legs.append(o.Leg("future", rng.choice(strikes), 0.0, qty))
            else:
                legs.append(o.Leg(right, rng.choice(strikes), rng.uniform(5, 400), qty))
        up, down = _slopes(legs)
        prof = o.profile(legs, spot)
        if up < 0 or down > 0:
            assert prof.defined_risk is False, legs
            seen["up" if up < 0 else "down"] += 1
        else:
            assert prof.defined_risk is True, legs
            seen["defined"] += 1
            # the max loss really is the worst case: nothing at a price of zero or far above is lower
            assert prof.max_loss is not None
            assert o.payoff(legs, 0.0) >= prof.max_loss - 1e-6
            assert o.payoff(legs, 20 * spot) >= prof.max_loss - 1e-6
    assert min(seen.values()) > 100  # the generator reaches every class


@pytest.mark.parametrize(
    "legs",
    [
        [o.Leg("put", 22600, 120.0, -65)],  # naked short put
        [o.Leg("put", 22600, 120.0, 65), o.Leg("put", 22400, 70.0, -130)],  # put ratio spread
        [o.Leg("future", 22780, 0.0, 65), o.Leg("call", 23000, 60.0, -65)],  # long future + short call
    ],
    ids=["short_put", "put_ratio", "future_short_call"],
)
def test_loss_bounded_only_by_a_price_of_zero_is_not_defined_risk(legs):
    prof = o.profile(legs, 22780.0)
    # the max loss stays the grid minimum (today's number: the loss at a price of zero), but it is not defined risk
    assert prof.max_loss == pytest.approx(o.payoff(legs, 0.0))
    assert prof.defined_risk is False


def _analyse(legs: str, capital: float = 10_000_000, pct: float = 20):
    raw = json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text())
    closes = [float(c) for _, c in parse_index_history(raw)]
    return fs.analyse(chain(), 65, fs.parse_legs(legs), today=date(2026, 9, 29), closes=closes, iv_series=[],
                      capital=capital, max_loss_pct=pct, brokerage=20)  # fmt: skip


@pytest.mark.parametrize("legs", ["sell:put:22600:1", "buy:put:22600:1,sell:put:22400:2",
                                  "buy:future:22780:1,sell:call:23000:1"])  # fmt: skip
def test_the_gate_fails_them_at_any_capital(legs):
    for capital, pct in ((10_000_000, 20), (100_000_000, 2)):  # ₹1 crore at 20%, ₹10 crore at 2%
        a = _analyse(legs, capital, pct)
        assert a["capital_check"]["within"] is True  # the budget is not what stops them
        _, factors, failed = fs.assess(a, defined_risk_only=True)
        assert "Defined risk" in failed
        dr = next(f for f in factors if f.name == "Defined risk")
        assert str(dr.value).startswith("bounded only by a price of zero: ₹")
    # a profile that allows undefined risk lets the gate pass, as today
    assert "Defined risk" not in fs.assess(_analyse(legs), defined_risk_only=False)[2]


def test_spreads_and_condors_stay_defined_risk():
    for preset in ("iron_condor", "bull_call", "bear_put"):
        a = fs.analyse(chain(), 65, fs.preset_legs(chain(), preset), today=date(2026, 9, 29), closes=[],
                       iv_series=[], capital=1_000_000, max_loss_pct=2, brokerage=20)  # fmt: skip
        assert a["defined_risk"] is True and "Defined risk" not in fs.assess(a)[2]


def test_the_position_book_applies_the_same_rule():
    from finresearch.fnobook import positions as pos

    def leg(right, strike, qty, avg):
        return {"right": right, "strike": strike, "qty": qty, "avg_price": avg, "direction_known": True}

    short_put = [leg("put", 22600.0, -65, 120.0)]
    ml, why = pos.max_loss(short_put, 22780.0)
    assert ml == pytest.approx(-(22600 - 120) * 65)  # the loss at a price of zero, as before
    assert why == "bounded only by a price of zero: not defined risk"
    spread = [leg("put", 22600.0, -65, 120.0), leg("put", 22400.0, 65, 70.0)]
    assert pos.max_loss(spread, 22780.0) == (pytest.approx(-(200 - 50) * 65), "")
    covered = [leg("future", None, 65, 22780.0), leg("call", 23000.0, -65, 60.0)]
    assert pos.max_loss(covered, 22780.0)[1] == "bounded only by a price of zero: not defined risk"
