"""Valuation triangulation (fincalc.valuation DCF / reverse DCF / grid / peers / EV bridge, fincalc.montecarlo)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from finresearch.fincalc import montecarlo, valuation


def test_dcf_golden_one_year():
    """CF0 100, g 10 %, r 12 %, g_T 4 %, n 1: CF1 = 110; TV = 110 × 1.04 / 0.08 = 1430; value = 1540 / 1.12 = 1375."""
    v = valuation.dcf(100, "0.10", "0.12", "0.04", years=1)
    assert v.pv_explicit.quantize(Decimal("0.0001")) == Decimal("98.2143")
    assert v.value.quantize(Decimal("0.0001")) == Decimal("1375.0000")
    assert v.terminal_share.quantize(Decimal("0.0001")) == Decimal("0.9286")


def test_dcf_equals_gordon_when_growth_is_constant():
    for years in (1, 5, 10):
        v = valuation.dcf(100, "0.05", "0.11", "0.05", years=years).value
        assert abs(v - Decimal(105) / Decimal("0.06")) < Decimal("1e-20")  # 1750


def test_dcf_golden_five_years_by_hand():
    # CF_t = 100 × 1.08^t, r = 12 %: Σ PV = 108/1.12 + 116.64/1.2544 + 125.9712/1.404928 + 136.048896/1.57351936
    # + 146.93280768/1.762341683 = 96.4286 + 92.9847 + 89.6638 + 86.4615 + 83.3736 = 448.9121
    # TV = 146.93280768 × 1.04 / 0.08 = 1910.1265; PV = 1083.8604; value = 1532.7725
    v = valuation.dcf(100, "0.08", "0.12", "0.04", years=5)
    assert v.pv_explicit.quantize(Decimal("0.001")) == Decimal("448.912")
    assert v.value.quantize(Decimal("0.01")) == Decimal("1532.77")


def test_dcf_rejects_r_not_above_terminal_growth():
    with pytest.raises(ValueError):
        valuation.dcf(100, "0.1", "0.05", "0.05")


def test_ev_bridge_with_ipo_proceeds():
    # EV 3000 + cash 129 + fresh issue 320 − debt 2342 − NCI 1 = 1106 (₹ Mn, Orient-like)
    assert valuation.ev_bridge(3000, cash=129, debt=2342, nci=1, fresh_issue_proceeds=320) == Decimal(1106)
    assert valuation.ev_bridge(100, leases=5, esop_value=2, non_operating_assets=7) == Decimal(100)


def test_reverse_dcf_recovers_the_growth_that_built_the_price():
    price = valuation.dcf_per_share(1000, "0.07", "0.12", "0.04", 100, net_debt=-500)
    g = valuation.reverse_dcf(price, 100, 1000, "0.12", "0.04", net_debt=-500)
    assert abs(g - Decimal("0.07")) < Decimal("0.000001")
    # IPO: proceeds and post-issue shares
    p2 = valuation.dcf_per_share(50, "0.15", "0.13", "0.05", 10, fresh_issue_proceeds=32, net_debt=221)
    g2 = valuation.reverse_dcf(p2, 10, 50, "0.13", "0.05", fresh_issue_proceeds=32, net_debt=221)
    assert abs(g2 - Decimal("0.15")) < Decimal("0.000001")


def test_reverse_dcf_is_none_for_negative_cash_flow_or_out_of_range():
    assert valuation.reverse_dcf(100, 10, -5, "0.12", "0.04") is None
    assert valuation.reverse_dcf(10**9, 1, 1, "0.12", "0.04") is None  # needs > 100 % growth a year


def test_dcf_grid_marks_invalid_cells():
    grid = valuation.dcf_grid(100, "0.05", 10, ["0.04", "0.12"], ["0.04", "0.05"])
    assert grid[0] == [None, None] and grid[1][0] is not None and grid[1][0] < grid[1][1]


def test_peer_stats_median_iqr_and_percentile():
    s = valuation.peer_stats([54.85, 46.69, 42.69, 48.99, 26.29, 33.47, -5], own="51.61")
    assert (
        s.n == 6 and s.median == Decimal("44.69") and s.q1 == Decimal("35.775") and s.q3 == Decimal("48.415")
    )
    assert s.percentile.quantize(Decimal("0.01")) == Decimal("83.33")  # 5 of 6 peers are cheaper
    with pytest.raises(ValueError):
        valuation.peer_stats([10, 12])


def test_triangulate_union_intersection_and_disagreement():
    b = valuation.triangulate({"dcf": (100, 200), "peers": (130, 230)})  # mids 150 vs 180: 20 %
    assert (b.low, b.high, b.intersection, b.disagree) == (100, 230, (130, 200), False)
    b2 = valuation.triangulate({"dcf": (50, 70), "peers": (150, 250)})
    assert b2.intersection is None and b2.disagree


def test_monte_carlo_zero_width_ranges_reproduce_the_deterministic_dcf():
    r = montecarlo.Range
    res = montecarlo.simulate_dcf(1000, 100, r(0.07, 0.07, 0.07), r(0.12, 0.12, 0.12), r(0.04, 0.04, 0.04),
                                  net_debt=-500, price=100, n=200)  # fmt: skip
    exact = float(valuation.dcf_per_share(1000, "0.07", "0.12", "0.04", 100, net_debt=-500))
    assert res.p5 == pytest.approx(exact) and res.p95 == pytest.approx(exact) and res.rejected == 0
    assert res.prob_above_price == (1.0 if exact > 100 else 0.0)


def test_monte_carlo_is_reproducible_by_seed_and_ordered():
    r = montecarlo.Range
    args = (
        1000,
        100,
        r(0.02, 0.06, 0.10, "hist"),
        r(0.11, 0.12, 0.14, "C1"),
        r(0.03, 0.04, 0.05, "assumption"),
    )
    a = montecarlo.simulate_dcf(*args, price=150, seed=7)
    b = montecarlo.simulate_dcf(*args, price=150, seed=7)
    c = montecarlo.simulate_dcf(*args, price=150, seed=8)
    assert a == b and a != c
    assert a.p5 < a.p25 < a.p50 < a.p75 < a.p95 and 0 <= a.prob_above_price <= 1
    assert sum(h["count"] for h in a.histogram) == a.n == 5000


def test_monte_carlo_rejects_draws_too_close_to_terminal_growth():
    r = montecarlo.Range
    res = montecarlo.simulate_dcf(
        100, 10, r(0.05, 0.05, 0.05), r(0.04, 0.06, 0.08), r(0.04, 0.045, 0.05), n=500
    )
    assert res.rejected > 0
    with pytest.raises(ValueError):
        r(0.1, 0.05, 0.2)


# --------------------------------------------------------------------------- ledger → report blocks, MCP tools
EVAL = __import__("pathlib").Path(__file__).parent / "fixtures" / "eval"


def _claims(name):
    import json

    return json.loads((EVAL / name).read_text())["claims"]


def test_triangulation_from_the_infosys_ledger():
    from finresearch.api.accuracy import triangulation_block

    t = triangulation_block(_claims("infosys-run9.json"), "stock_report")
    assert t["status"] == "ok" and t["cash_flow_basis"] == "FCF" and not t["missing"]
    names = {i["name"]: i for i in t["inputs"]}
    assert names["Price"]["value"] == 1003.2 and names["Discount rate"]["source"].startswith(
        "C"
    )  # ledger CoE
    assert names["Terminal growth"]["source"].startswith("ASSUMPTION")
    rd = t["reverse_dcf"]
    assert 0 < rd["implied_growth"] < 0.05  # the price implies ~2 % FCF growth vs 7–10 % history
    mc = t["monte_carlo"]
    assert mc["p5"] < mc["p50"] < mc["p95"] and sum(h["count"] for h in mc["histogram"]) == mc["n"]
    assert t == triangulation_block(
        _claims("infosys-run9.json"), "stock_report"
    )  # seeded: identical on re-render
    assert t["peers"]["n"] >= 3 and t["band"]["methods"]


def test_triangulation_for_the_orient_ipo_uses_labelled_proxies():
    from finresearch.api.accuracy import triangulation_block

    t = triangulation_block(_claims("orient-cables-run5.json"), "ipo_report")
    assert t["cash_flow_basis"] == "PAT (earnings proxy)" and "negative" in t["notes"][0]
    names = {i["name"] for i in t["inputs"]}
    assert "Fresh-issue proceeds (into the company)" in names and "Shares (post-issue)" in names
    disc = next(i for i in t["inputs"] if i["name"] == "Discount rate")
    assert disc["claim_id"] is None and "ASSUMPTION" in disc["source"]  # no rate in the ledger: said so


def test_triangulation_not_enough_inputs_and_other_kinds():
    from finresearch.api.accuracy import triangulation_block

    t = triangulation_block([{"id": 1, "metric": "price_band_upper", "value": "100", "unit": "INR per share",
                              "period": "offer", "status": "verified", "importance": "high"}], "ipo_report")  # fmt: skip
    assert t["status"] == "not_enough_inputs" and t["monte_carlo"] is None and len(t["missing"]) >= 2
    assert triangulation_block(_claims("mf-120505-run11.json"), "fund_report") is None


def test_insights_carry_accuracy_and_triangulation():
    from finresearch.api.insights import build_insights

    raw = _claims("infosys-run9.json")
    ins = build_insights(
        run_id=9, kind="stock_report", claims=raw, synthesis={}, report_markdown="[C1644] [C1646]"
    )
    acc = ins["accuracy"]
    assert acc["counts"]["pass"] >= 10 and acc["counts"]["fail"] == 0
    assert any(c["cited"] for c in acc["checks"]) and ins["triangulation"]["status"] == "ok"


def test_mcp_valuation_tools():
    import json

    from finresearch.mcp_server.server import reverse_dcf, valuation_monte_carlo

    price = valuation.dcf_per_share(1000, "0.07", "0.12", "0.04", 100)
    out = json.loads(reverse_dcf(str(price), "100", "1000", "0.12"))
    assert (
        abs(float(out["implied_growth"]) - 0.07) < 1e-6
        and len(out["implied_growth_at_discount_rate_minus_plus_1pp"]) == 3
    )
    rng = {"low": 0.05, "mode": 0.07, "high": 0.09, "source": "test"}
    a = json.loads(
        valuation_monte_carlo("1000", "100", rng, {"low": 0.11, "mode": 0.12, "high": 0.13}, price="150")
    )
    b = json.loads(
        valuation_monte_carlo("1000", "100", rng, {"low": 0.11, "mode": 0.12, "high": 0.13}, price="150")
    )
    assert a == b and a["p5"] < a["p95"] and "error" not in a
    assert "error" in json.loads(valuation_monte_carlo("1", "1", {"low": 1, "high": 0}, rng))


def test_mcp_identity_checks_tool(env, tmp_path):
    import json

    from finresearch.db import session_scope
    from finresearch.evals.replay import import_run
    from finresearch.mcp_server.server import identity_checks

    data = json.loads((EVAL / "orient-cables-run5.json").read_text())
    with session_scope() as s:
        run_id = import_run(s, data, slug_suffix="-" + tmp_path.name[-8:])
    out = json.loads(identity_checks(run_id))
    assert out["counts"]["fail"] == 1 and [c["family"] for c in out["checks"]] == ["growth"]
    assert json.loads(identity_checks(run_id, only_failing=False))["counts"]["pass"] >= 10
