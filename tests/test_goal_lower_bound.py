"""#286: a goal whose earmarked portfolio is partly priced runs on the priced part as a labelled lower bound.

The bound rests on one property of wealth.goals.plan: with the seed fixed, P(success) is non-decreasing and the SIP
needed non-increasing in the starting money. Proof from the code: the draws depend only on (seed, n, months) and the
weights only on the month (never on the pot), each weight is >= 0 and they sum to 1, so a month's growth factor
1 + R = sum_c w_c exp(x_c) is > 0; each path's final value V_M = start * prod(1 + R_m) + sum_m SIP_m prod_{k>m}(1 + R_k)
therefore rises with the start (and with the SIP) path by path, so the share of paths at or above the target cannot
fall. The bisection for the SIP then gives a ceiling to within its ₹1 resolution. The grid tests below check it.
Synthetic data only (invented names and ISINs).
"""

from __future__ import annotations

from decimal import Decimal as D
from itertools import pairwise

import numpy as np
import pytest
import test_unknowns
from test_unknowns import ORIGIN, TODAY, add

from finresearch.wealth.goals import (
    DEFAULT_ASSUMPTIONS,
    monthly_returns,
    plan,
    sip_path,
    terminal,
    weights_path,
)

client = test_unknowns.client  # the fixture: a test DB with the portfolio and wealth tables emptied

STARTS = [0.0, 5_000.0, 25_000.0, 100_000.0, 300_000.0, 1_000_000.0, 3_000_000.0, 10_000_000.0]
CASES = [  # (months, sip0, step-up %, target today, equity % or None = glide, gold %)
    (120, 5_000.0, 0.0, 2_500_000.0, None, 0.0),
    (60, 10_000.0, 10.0, 1_500_000.0, 80.0, 10.0),
    (24, 0.0, 0.0, 400_000.0, 100.0, 0.0),
    (300, 2_000.0, 5.0, 5_000_000.0, None, 15.0),
]


def _inf(x: float | None) -> float:
    return float("inf") if x is None else x


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("seed", [1, 20260930])
def test_success_is_non_decreasing_and_the_sip_needed_non_increasing_in_the_start(case, seed):
    months, sip0, step, target, eq, gold = case
    runs = [plan(start=s0, sip0=sip0, step_up_pct=step, months=months, target_today=target, inflation_pct=6.0,
                 equity_pct=eq, gold_pct=gold, assumptions=DEFAULT_ASSUMPTIONS, n=400, seed=seed) for s0 in STARTS]  # fmt: skip
    for lo, hi in pairwise(runs):
        for k in ("p_success", "p_haircut", "p_plus_year", "p_step_up_plus5"):
            assert getattr(lo, k) <= getattr(hi, k), (k, lo.start, hi.start)
        assert lo.p_ci[0] <= hi.p_ci[0] and lo.p_ci[1] <= hi.p_ci[1]
        for q in ("p10", "p50", "p90"):
            assert lo.terminal_pcts[q] <= hi.terminal_pcts[q]
        for a, b in zip(lo.fan, hi.fan, strict=True):
            assert a["p10"] <= b["p10"] and a["p50"] <= b["p50"] and a["p90"] <= b["p90"]
        # the bisection lands within ₹1 of the threshold, so a larger start needs at most ₹1 more than a smaller one
        assert _inf(hi.sip_for_75) <= _inf(lo.sip_for_75) + 1, (lo.start, lo.sip_for_75, hi.sip_for_75)
        assert _inf(hi.sip_for_90) <= _inf(lo.sip_for_90) + 1
    # the grid is not trivial: the chance moves from below 75 % to above 90 % across it
    assert runs[0].p_success < 0.75 and runs[-1].p_success > 0.9


def test_every_path_ends_higher_from_a_larger_start():
    months = 180
    w = weights_path(months, None, 10.0)
    rets = monthly_returns(months, 2000, DEFAULT_ASSUMPTIONS, w, seed=7)
    assert (1 + rets).min() > 0  # the growth factor the proof needs
    sips = sip_path(3_000.0, 5.0, months)
    finals = [terminal(s0, sips, rets) for s0 in STARTS]
    for a, b in pairwise(finals):
        assert np.all(a <= b)


# --------------------------------------------------------------------------- the goal on /wealth
def _value(priced: dict[str, float], history: dict[str, dict[str, float]] | None = None,
           source: str = "nse", as_of: str = "2026-10-05") -> None:  # fmt: skip
    """Value the portfolio the way the daily pass does (report.snapshot + record_snapshot with its gaps): holdings in
    `priced` at that price, every other one without a price; `history` = the pass's earlier days {day: {name: price}}."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding
    from finresearch.portfolio import cache
    from finresearch.portfolio.metrics import record_snapshot, valuation_gaps
    from finresearch.portfolio.report import snapshot
    from finresearch.portfolio.valuation import PriceInfo

    with session_scope() as s:
        ids = {h.name: h.id for h in s.scalars(select(PortfolioHolding))}
        if history:
            cache.write(s, cache.VALUATION, {"history": {d: {str(ids[n]): [p, None] for n, p in row.items()}
                                                         for d, row in history.items()}})  # fmt: skip
        prices = {i: PriceInfo(price=D(str(priced[n])), as_of=as_of, source=source) if n in priced
                  else PriceInfo(error="not priced") for n, i in ids.items()}  # fmt: skip
        snap = snapshot(s, prices, TODAY)
        by_asset = {r["label"]: r["value"] for r in snap["allocation"]["asset"]}
        record_snapshot(s, TODAY, snap["summary"]["value"] or 0.0, snap["invested"], by_asset, snap["complete"],
                        valuation_gaps(s, snap))  # fmt: skip


def _goal(c, **body) -> int:
    base = {"name": "Synthetic goal", "target_inr": "1000000", "target_date": "2036-10-05", "current_inr": "10000",
            "monthly_sip": "5000", "portfolio_pct": "50"}  # fmt: skip
    r = c.post("/api/wealth/goals", headers=ORIGIN, json={**base, **body})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _overview_goal(gid: int) -> dict:
    from finresearch.db import session_scope
    from finresearch.wealth.service import overview

    with session_scope() as s:
        return next(g for g in overview(s, TODAY)["goals"] if g["id"] == gid)


def _plan(gid: int) -> dict:
    from finresearch.db import session_scope
    from finresearch.wealth.service import goal_plan

    with session_scope() as s:
        return goal_plan(s, gid, TODAY, n=500)


def _two_holdings(c) -> None:
    add(c, day="2026-01-05", kind="buy", quantity="5", price="100")  # Example Alpha Ltd
    add(
        c,
        day="2026-01-05",
        kind="buy",
        quantity="10",
        price="40",
        name="Example Beta Ltd",
        isin="INE000Y01012",
    )


def test_a_partly_priced_portfolio_gives_a_labelled_lower_bound(client):
    from finresearch.db import session_scope
    from finresearch.wealth.goals import assumptions_from
    from finresearch.wealth.service import DEFAULT_ASSUMPTIONS as KEYS
    from finresearch.wealth.service import get_assumptions

    _two_holdings(client)
    # Alpha priced at ₹120 (5 × 120 = ₹600); Beta has no price today, ₹50 on the pass of 2 October (10 × 50 = ₹500)
    _value(
        {"Example Alpha Ltd": 120.0},
        history={"2026-10-02": {"Example Alpha Ltd": 118.0, "Example Beta Ltd": 50.0}},
    )
    gid = _goal(client)
    g = _overview_goal(gid)
    # 10,000 other savings + 50 % of the ₹600 priced = 10,300, at least
    assert (g["funded_now"], g["funded_complete"], g["funded_bound"]) == (10300.0, False, "lower")
    assert g["funded_unpriced"] == ["Example Beta Ltd"]
    assert g["funded_unpriced_share_pct"] == pytest.approx(
        500 / 1100 * 100, abs=0.01
    )  # 45.45 % of the earmark
    p = _plan(gid)
    assert (p["bound"], p["start"], p["unpriced"]) == ("lower", 10300.0, ["Example Beta Ltd"])
    with session_scope() as s:
        asm = get_assumptions(s)
    kw = {"sip0": 5000.0, "step_up_pct": 0.0, "months": 120, "target_today": 1_000_000.0, "inflation_pct": 6.0,
          "equity_pct": None, "gold_pct": 0.0, "assumptions": assumptions_from({k: asm[k] for k in KEYS}), "n": 500,
          "seed": asm["seed"]}  # fmt: skip
    lo, whole = plan(start=10300.0, **kw), plan(start=10300.0 + 250.0, **kw)  # 250 = 50 % of Beta's last ₹500
    assert p["p_success"] == lo.p_success <= whole.p_success
    assert p["sip_for_75"] == lo.sip_for_75 and _inf(whole.sip_for_75) <= _inf(lo.sip_for_75) + 1
    assert p["message"].startswith(f"At least {lo.p_success * 100:.1f} % on these assumptions, counting only the "
                                   "priced holdings")  # fmt: skip
    assert "Below 75 %" not in p["message"] and "not reachable" not in p["message"]


def test_an_unpriced_holding_with_no_known_price_leaves_its_share_unknown(client):
    _two_holdings(client)
    _value({"Example Alpha Ltd": 120.0})  # no earlier pass: Beta's last price is not known
    gid = _goal(client)
    g = _overview_goal(gid)
    assert (g["funded_now"], g["funded_bound"], g["funded_unpriced_share_pct"]) == (10300.0, "lower", None)
    assert _plan(gid)["bound"] == "lower"


def test_nothing_priced_stays_unknown_not_a_bound(client):
    _two_holdings(client)
    _value({}, history={"2026-10-02": {"Example Alpha Ltd": 118.0, "Example Beta Ltd": 50.0}})
    gid = _goal(client)
    g = _overview_goal(gid)
    assert g["funded_bound"] is None and g["funded_complete"] is False
    p = _plan(gid)
    assert p["bound"] is None and "p_success" not in p and "sip_for_75" not in p
    assert p["message"].startswith("Not simulated") and "no holding is priced" in p["message"]


def test_a_stale_price_is_not_a_lower_bound(client):
    _two_holdings(client)
    # Alpha valued at a statement price from June: it may be above today's, so ₹600 is not a floor
    _value({"Example Alpha Ltd": 120.0}, source="statement", as_of="2026-06-30")
    p = _plan(_goal(client))
    assert p["bound"] is None and "p_success" not in p and "old price" in p["message"]


def test_a_linked_asset_without_a_value_stays_not_simulated(client):
    _two_holdings(client)
    _value({"Example Alpha Ltd": 120.0}, history={"2026-10-02": {"Example Beta Ltd": 50.0}})
    r = client.post("/api/wealth/assets", headers=ORIGIN, json={  # starts in 2027: no value today
        "kind": "fd", "name": "Synthetic FD", "principal": "100000", "rate_pct": "7", "start_date": "2027-01-04"})  # fmt: skip
    assert r.status_code == 200, r.text
    gid = _goal(client, linked_asset_ids=[r.json()["id"]])
    assert _overview_goal(gid)["funded_bound"] is None
    p = _plan(gid)
    assert p["bound"] is None and "p_success" not in p and "a linked asset has no value" in p["message"]


@pytest.mark.parametrize("unpriced", [
    [{"name": "X", "units": 0.0, "last_price": 10.0}],  # a closed or short position: not a long holding
    [{"name": "X", "units": None, "last_price": None}],  # units unknown
    [{"name": "X", "units": 3.0, "last_price": -5.0}],  # a negative price was seen (a liability-like instrument)
])  # fmt: skip
def test_an_unpriced_item_that_could_be_negative_is_not_a_lower_bound(unpriced):
    from finresearch.wealth.service import _lower_bound

    gaps = {"priced": 1, "unpriced": unpriced, "stale": 0}
    assert _lower_bound(gaps, 600.0) == "a holding without a price could be worth less than ₹0"
    assert _lower_bound({**gaps, "unpriced": [{"name": "X", "units": 3.0, "last_price": 5.0}]}, 600.0) is None


def test_a_valuation_incomplete_only_for_cost_basis_is_complete_for_the_goal(client):
    add(client, day="2026-01-05", kind="buy", quantity="5", price="100")  # Example Alpha Ltd, cost known
    # a statement opening balance: its cost is unknown, its units and today's price are not
    add(client, day="2026-04-10", kind="opening", quantity="10", name="Example Beta Ltd", isin="INE000Y01012")
    _value({"Example Alpha Ltd": 120.0, "Example Beta Ltd": 50.0})  # 600 + 500 = ₹1,100, every holding priced
    from finresearch.db import session_scope
    from finresearch.portfolio import cache

    with session_scope() as s:
        gaps = cache.read(s, cache.GAPS)["gaps"]
    # the snapshot itself is incomplete (an unknown cost), which made #262 stop at "not simulated"
    assert (gaps["unknown_cost"], gaps["unpriced"], gaps["stale"], gaps["priced"]) == (1, [], 0, 2)
    gid = _goal(client)
    g = _overview_goal(gid)
    # 10,000 other savings + 50 % of ₹1,100 = 10,550, complete: the current value does not depend on cost
    assert (g["funded_now"], g["funded_complete"], g["funded_why"], g["funded_bound"]) == (
        10550.0,
        True,
        None,
        None,
    )
    p = _plan(gid)
    assert (p["funded_complete"], p["bound"], p["start"]) == (True, None, 10550.0) and 0 <= p[
        "p_success"
    ] <= 1
    assert not p["message"].startswith(("Not simulated", "At least"))
