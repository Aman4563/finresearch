"""portfolio.optimise: synthetic holdings and returns (made-up names and ISINs); tax and charges worked by hand."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import numpy as np
import pytest

from finresearch.portfolio import optimise as op
from finresearch.portfolio import optimise_math as om
from finresearch.portfolio.rebalance import Lot, Position
from finresearch.portfolio.tax import HoldingTax

TODAY = date(2026, 10, 22)  # FY 2026-27: equity STCG 20 %, cess 4 %
SLAB = D("0.30")
NEED = om.TRAIN + om.TEST * om.MIN_WINDOWS


def pos(hid, price, lots, *, asset_type="stock", tax_class="equity", cls="Stocks", **kw):
    ht = HoldingTax(hid, f"Example {hid} Ltd", "Demat", f"INE000Z0{hid:04d}", tax_class, True, None)
    lots = [Lot(a, D(q), None if c is None else D(c)) for a, q, c in lots]
    value = None if price is None else D(price) * sum((x.quantity for x in lots), D(0))
    return Position(ht, asset_type, cls, value, None if price is None else D(price), lots, **kw)


def days(n):
    d, out = date(2023, 1, 2), []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def series(vols, n=NEED + 60, seed=4):
    ds = days(n)
    r = np.random.default_rng(seed).normal(0, 0.01, size=(n, len(vols))) * np.array(vols)
    return {
        hid: dict(zip(ds, r[:, i].tolist(), strict=True)) for i, hid in enumerate(range(1, len(vols) + 1))
    }


OLD = date(2023, 1, 1)  # long-term lots


def three(alpha_lots=None, **kw):
    """Example 1 is 3x as volatile and 60 % of the portfolio; 2 and 3 are calm."""
    return [pos(1, 100, alpha_lots or [(OLD, 600, 100)], **kw), pos(2, 100, [(OLD, 200, 100)]),
            pos(3, 100, [(OLD, 200, 100)])]  # fmt: skip


def run(ps, rets, **kw):
    kw.setdefault("max_position_pct", 100.0)
    return op.suggest(ps, rets, TODAY, [], SLAB, **kw)


def test_validated_min_variance_sells_the_volatile_holding():
    out = run(three(), series([3, 1, 1]))
    assert out["status"] == "suggested", out.get("message")
    assert out["validation"]["passed"]
    rows = {r["holding_id"]: r for r in out["holdings"]}
    assert rows[1]["target_pct"] < rows[1]["current_pct"] == 60.0
    assert sum(r["target_pct"] for r in rows.values()) == pytest.approx(100.0, abs=0.01)
    assert [s["holding_id"] for s in out["sells"]] == [1]
    assert {b["holding_id"] for b in out["buys"]} == {2, 3}
    assert "not investment advice" in out["disclaimer"] and "no order" in out["disclaimer"]


def test_short_term_gain_tax_by_hand():
    # Example 1 bought 1-Jun-2026 at 80, price 100: short-term. Per unit sold: gain 20 less STT 0.1 % of 100 = 19.9
    out = run(three([(date(2026, 6, 1), 600, 80)]), series([3, 1, 1]))
    s = out["sells"][0]
    units = D(str(s["units"]))
    assert s["tax"] == pytest.approx(float(units * D("19.9") * D("0.20") * D("1.04")), abs=0.02)
    assert s["charges"] == pytest.approx(float(units * 100 * D("0.001")), abs=0.01)
    assert out["totals"]["tax"] == pytest.approx(s["tax"], abs=0.01)


def test_not_validated_shows_no_trades_or_targets():
    # the volatile holding swaps after the first year: every fit on the past backs the wrong one
    rets = series([3, 1])
    ds = sorted(rets[1])
    for d in ds[om.TRAIN :]:
        rets[1][d], rets[2][d] = rets[1][d] / 3, rets[2][d] * 3
    out = run([pos(1, 100, [(OLD, 500, 100)]), pos(2, 100, [(OLD, 500, 100)])], rets)
    assert out["status"] == "not_validated", out["message"]
    assert out["sells"] == [] and out["buys"] == []
    assert all(r["target_pct"] is None for r in out["holdings"])
    assert out["validation"]["reasons"]


def test_too_little_common_history_is_not_a_plan():
    out = run([pos(1, 100, [(OLD, 500, 100)]), pos(2, 100, [(OLD, 500, 100)])], series([1, 1], n=NEED - 30))
    assert out["status"] == "too_few" and out["sells"] == [] and out["validation"] is None


def test_short_history_holding_is_fixed_not_dropped():
    rets = series([3, 1, 1])
    rets[3] = dict(list(rets[3].items())[-100:])  # a recent buy: 100 returns
    out = run(three(), rets)
    row = {r["holding_id"]: r for r in out["holdings"]}[3]
    assert row["status"] == "fixed" and row["target_pct"] == row["current_pct"] == 20.0
    assert "100 daily returns" in row["reason"]
    assert all(s["holding_id"] != 3 for s in out["sells"]) and all(b["holding_id"] != 3 for b in out["buys"])


def test_unpriced_holding_is_excluded_and_the_plan_incomplete():
    ps = [*three(), pos(4, None, [(OLD, 10, 100)])]
    out = run(ps, series([3, 1, 1]))
    assert out["complete"] is False
    assert [x["holding_id"] for x in out["excluded"]] == [4]


def test_locked_and_unknown_cost_units_set_lower_bounds():
    # Example 1: oldest lot has an unknown cost, so FIFO can sell nothing: its whole 60 % is a floor
    out = run(three([(OLD, 600, None)]), series([3, 1, 1]))
    row = {r["holding_id"]: r for r in out["holdings"]}[1]
    assert row["lo_pct"] == pytest.approx(60.0)
    assert out["sells"] == []
    assert any("unknown cost" in x["reason"] for x in out["skipped"])


def test_elss_units_in_lock_in_are_a_floor():
    lots = [(OLD, 300, 100), (date(2025, 1, 10), 300, 100)]  # the newer 300 unlock on 10-Jan-2028
    ps = three(lots, asset_type="mf", tax_class="equity", cls="Equity funds", elss=True, exit_load=(D(0), 0))
    out = run(ps, series([3, 1, 1]))
    row = {r["holding_id"]: r for r in out["holdings"]}[1]
    assert row["lo_pct"] == pytest.approx(30.0)
    assert row["target_pct"] >= 30.0 - 1e-6
    sold = sum(s["units"] for s in out["sells"] if s["holding_id"] == 1)
    assert sold <= 300


def test_fund_without_an_exit_load_is_flagged_not_zero():
    ps = three([(OLD, 600, 100)], asset_type="mf", tax_class="equity", cls="Equity funds")  # exit_load None
    out = run(ps, series([3, 1, 1]))
    s = out["sells"][0]
    assert s["exit_load"] is None
    assert "exit load" in " ".join(s["costs_unknown"])
    assert "exit load" in " ".join(out["totals"]["costs_unknown"])


def test_stock_trades_flag_brokerage_as_unknown():
    out = run(three(), series([3, 1, 1]))
    assert any("brokerage" in c for c in out["sells"][0]["costs_unknown"])
    assert any("brokerage" in c for b in out["buys"] for c in b["costs_unknown"])


def test_position_cap_binds():
    # unbounded, minimum variance puts about 47 % in each calm holding; the 40 % cap holds them there
    out = run(three(), series([3, 1, 1]), max_position_pct=40.0)
    assert out["status"] == "suggested", out["message"]
    rows = {r["holding_id"]: r for r in out["holdings"]}
    assert all(r["hi_pct"] == 40.0 for r in rows.values())
    assert rows[2]["target_pct"] == pytest.approx(40.0, abs=0.01) and rows[3]["target_pct"] == pytest.approx(
        40.0, abs=0.01
    )


def test_infeasible_cap_is_reported():
    out = run(three(), series([3, 1, 1]), max_position_pct=20.0)  # 3 x 20 % < 100 %
    assert out["status"] == "infeasible"
    assert out["sells"] == [] and "100 %" in out["message"]


def test_mean_variance_is_refused():
    with pytest.raises(ValueError):
        run(three(), series([3, 1, 1]), method="mean_variance")


# --------------------------------------------------------------------------- price history
class FakeFetch:
    def __init__(self, closes):
        self.closes, self.calls = closes, []

    async def __call__(self, inst, a, b):
        self.calls.append((inst.key, a, b))
        if inst.key == "NSE:FAIL":
            raise RuntimeError("exchange down")
        return {d: v for d, v in self.closes.get(inst.key, {}).items() if a <= d <= b}, True


async def test_load_returns_maps_instruments_to_holdings_and_isolates_failures(tmp_path):
    from finresearch.portfolio.history import History, PriceStore
    from finresearch.portfolio.history import Position as HPos

    ds = days(10)
    closes = {d: 100.0 * (1.01**i) for i, d in enumerate(ds)}
    h = History(days=ds, positions=[HPos("NSE:AAAA", "Example A", "stock", "equity", [1, 2], 10, 1, None, 10, None, None),
                                    HPos("NSE:FAIL", "Example F", "stock", "equity", [3], 10, 1, None, 10, None, None)])  # fmt: skip
    f = FakeFetch({"NSE:AAAA": closes})
    out = await op.load_returns(h, f, PriceStore(tmp_path / "ph"), ds[-1])
    assert out[1] == out[2] and len(out[1]) == 9 and out[1][ds[1]] == pytest.approx(0.01)
    assert out[3] == {}  # a failed read is no history, which fixes the holding, never a 0 return
    assert min(c[1] for c in f.calls if c[0] == "NSE:AAAA") <= date(
        ds[-1].year - op.HISTORY_YEARS, ds[-1].month, ds[-1].day
    )


def test_steps_of_one_holding_follow_fifo_order():
    # Example 1's oldest lot is a long-term gain and its newer lot a short-term loss: FIFO sells the gain lot first
    out = run(three([(OLD, 300, 50), (date(2026, 6, 1), 300, 150)]), series([3, 1, 1]))
    acquired = [lot["acquired"] for s in out["sells"] if s["holding_id"] == 1 for lot in s["lots"]]
    assert acquired[0] == OLD.isoformat()
    assert acquired == sorted(acquired)
