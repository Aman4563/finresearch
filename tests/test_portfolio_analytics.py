"""Portfolio analytics (WP-A, issue #139): golden values for every formula, the value history on a synthetic portfolio
with a split, a dividend and a fund, and the API with fake NSE/AMFI sources (offline; no personal data)."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from finresearch.portfolio import analytics_math as am
from finresearch.portfolio.lots import Event

# --------------------------------------------------------------------------- formulas (hand-computed goldens)


def test_twr_removes_flows():
    # 100 in on day 0; +10 %; 100 more in (value 220 = 110 grown 9.09 % + 100); then -9.09 %
    r = am.twr_returns([100, 110, 220, 200], [100, 0, 100, 0])
    assert r[0] is None
    assert r[1] == pytest.approx(0.10)
    assert r[2] == pytest.approx(120 / 110 - 1)
    assert r[3] == pytest.approx(200 / 220 - 1)
    assert am.chain(r)[-1] == pytest.approx(1.1 * 120 / 110 * 200 / 220)
    # a deposit that only adds its own amount is not a return
    assert am.twr_returns([1000, 1500], [1000, 500])[1] == pytest.approx(0.0)
    # a paid-out dividend (outflow) is a return: value falls by 50, the flow is -50
    assert am.twr_returns([1000, 950], [1000, -50])[1] == pytest.approx(0.0)


def test_twr_skips_days_after_a_full_exit():
    r = am.twr_returns([100, 0, 50, 55], [100, -100, 50, 0])
    assert r[1] == pytest.approx(0.0) and r[2] is None and r[3] == pytest.approx(0.10)
    assert am.chain(r) == pytest.approx([1.0, 1.0, 1.0, 1.1])
    assert am.defined_tail(r) == 2


def test_volatility_beta_tracking_error():
    rb = [0.01, -0.01, 0.02, -0.02]
    # sample variance = (0.0001 + 0.0001 + 0.0004 + 0.0004) / 3
    assert am.annualised_vol(rb) == pytest.approx(math.sqrt(0.001 / 3) * math.sqrt(252))
    assert am.beta([2 * x for x in rb], rb) == pytest.approx(2.0)
    assert am.beta([2 * x + 0.001 for x in rb], rb) == pytest.approx(2.0)
    assert am.tracking_error([x + 0.001 for x in rb], rb) == pytest.approx(0.0, abs=1e-12)
    # TE of 2x the benchmark = stdev(rb)·√252
    assert am.tracking_error([2 * x for x in rb], rb) == pytest.approx(am.annualised_vol(rb))


def test_drawdown_and_recovery():
    dd = am.drawdown_recovery([1, 1.2, 0.9, 1.0, 1.25, 1.1])
    assert dd.depth == pytest.approx(0.9 / 1.2 - 1)  # -25 %
    assert (dd.peak, dd.trough, dd.recovered) == (1, 2, 4)
    assert dd.current == pytest.approx(1.1 / 1.25 - 1)
    never = am.drawdown_recovery([1, 1.2, 0.9, 1.0])
    assert never.recovered is None
    flat = am.drawdown_recovery([1, 1.1, 1.2])
    assert flat.depth == 0 and flat.recovered is None


def test_historical_var_cvar_linear_quantile():
    r = [x / 100 for x in range(-10, 10)]  # -0.10 ... 0.09, n = 20
    var, cvar = am.historical_var(r)
    # position (n-1)·0.05 = 0.95 between -0.10 and -0.09
    assert var == pytest.approx(0.10 - 0.95 * 0.01)
    assert cvar == pytest.approx(0.10)
    assert am.horizon_returns([1, 2, 4, 8], 2) == pytest.approx([3.0, 3.0])


def test_sharpe_sortino_reuse_fincalc():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
    idx = [1.0, 1.01, 1.0, 1.02, 1.01]
    r = [idx[i] / idx[i - 1] - 1 for i in range(1, 5)]
    mean, sd = sum(r) / 4, math_stdev(r)
    s, so = am.sharpe_sortino(idx, days, 0.05)
    assert s == pytest.approx((mean * 252 - 0.05) / (sd * math.sqrt(252)))
    rf = 0.05 / 252
    dd = math.sqrt(sum(min(0, x - rf) ** 2 for x in r) / 4)
    assert so == pytest.approx((mean - rf) * 252 / (dd * math.sqrt(252)))


def math_stdev(xs):
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def test_risk_contributions():
    r = [[0.01, 0.01], [-0.01, 0.01], [0.01, -0.01], [-0.01, -0.01]]  # equal variance, zero covariance
    rc, vol = am.risk_contributions([0.5, 0.5], r)
    assert rc == pytest.approx([0.5, 0.5])
    var = 0.0004 / 3  # sample variance of each column
    assert vol == pytest.approx(math.sqrt(0.5 * var * 252))
    rc2, _ = am.risk_contributions([0.75, 0.25], r)
    assert rc2 == pytest.approx([0.5625 / 0.625, 0.0625 / 0.625])  # 90 % / 10 %
    assert sum(rc2) == pytest.approx(1.0)


def test_concentration_formulas():
    assert am.hhi([50, 30, 20]) == pytest.approx(0.38)
    assert am.n_effective([50, 30, 20]) == pytest.approx(1 / 0.38)
    assert am.top_share([50, 30, 20], 2) == pytest.approx(0.8)
    assert am.n_effective([1, 1, 1, 1]) == pytest.approx(4)


def test_direct_index_equivalent_of_the_benchmark_itself_is_exact():
    d = [date(2025, 1, 1), date(2025, 4, 1), date(2025, 9, 1), date(2026, 1, 1)]
    level = dict(zip(d, [100.0, 125.0, 110.0, 120.0], strict=True))
    flows = [(d[0], 1000.0), (d[1], 500.0), (d[2], -300.0)]
    units = 1000 / 100 + 500 / 125 - 300 / 110
    eq = am.direct_index_equivalent(flows, level, d[3], units * 120)
    assert eq.value == pytest.approx(units * 120) and not eq.clamped
    assert eq.pme == pytest.approx(1.0)  # the portfolio IS the benchmark: PME 1, ₹ alpha 0
    assert am.investor_xirr(flows, d[3], units * 120) == pytest.approx(
        am.investor_xirr(flows, d[3], eq.value)
    )
    # KS-PME by hand: FV(in) = 1000·1.2 + 500·0.96 = 1680; FV(out) = 300·120/110
    better = am.direct_index_equivalent(flows, level, d[3], 1500.0)
    assert better.pme == pytest.approx((300 * 120 / 110 + 1500) / 1680)


def test_die_clamps_outflows_beyond_the_holding_and_xirr():
    d0, d1 = date(2025, 1, 1), date(2026, 1, 1)
    eq = am.direct_index_equivalent([(d0, 100.0), (d1, -500.0)], {d0: 1.0, d1: 1.1}, d1, 0.0)
    assert eq.clamped and eq.units == 0
    assert am.investor_xirr([(d0, 100.0)], d1, 110.0) == pytest.approx(0.10, abs=1e-6)


def test_total_return_index_dividend_split_and_weekend_ex_date():
    d = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
    tr = am.total_return_index(d, [100, 102, 97, 99], {d[2]: 5.0})
    assert tr == pytest.approx([1, 1.02, 1.02 * 102 / 102, 1.02 * 99 / 97])
    # a 10 -> 1 split: the close drops tenfold, the units multiply tenfold: no return
    assert am.total_return_index(d[:2], [100, 10.5], {}, {d[1]: 10.0})[-1] == pytest.approx(1.05)
    # an ex-date on a Sunday counts on Monday
    mon = [date(2026, 1, 2), date(2026, 1, 5)]
    assert am.total_return_index(mon, [100, 95], {date(2026, 1, 4): 5.0})[-1] == pytest.approx(1.0)


def test_break_even():
    assert am.break_even_years(1000, 250) == 4
    assert am.break_even_years(1000, 0) is None
    assert am.break_even_years(-5, 100) == 0


# --------------------------------------------------------------------------- value history (synthetic portfolio)
DAYS = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]
CLOSES = {
    "NSE:AAA": [100, 110, 55, 60],  # 1:2 split (face value 10 -> 5) ex 7-Jan
    "NSE:BBB": [190, 200, 210, 205],
    "MF:111": [9.8, 9.9, 10.0, 10.1],
    "NSE:NIFTYBEES": [100, 101, 102, 103],
}


class FakeFetch:
    def __init__(self, closes=None, actions=None):
        self.closes = closes or CLOSES
        self.calls: list[tuple[str, date, date]] = []
        self._actions = actions or []

    async def __call__(self, inst, a, b):
        self.calls.append((inst.key, a, b))
        c = self.closes.get(inst.key, [])
        return {d: float(v) for d, v in zip(DAYS, c, strict=False) if a <= d <= b}, True

    async def actions(self, symbol):
        return self._actions


def ev(i, day, kind, q=None, price=None, amount=None, **meta):
    return Event(i, day, kind, D(str(q)) if q is not None else None, D(str(price)) if price is not None else None,
                 D(str(amount)) if amount is not None else None, D(0), True, meta)  # fmt: skip


def holding(i, name, asset_type, events, **kw):
    from finresearch.portfolio.history import HoldingIn

    return HoldingIn(i, name, "Test", asset_type, kw.get("ikey", f"K{i}"), None, kw.get("nse"), None, kw.get("code"),
                     None, kw.get("tax_class", "equity"), {}, events)  # fmt: skip


SCHEME = SimpleNamespace(code="111", name="Test Flexi Cap Fund - Regular Plan - Growth", plan="Regular", amc="X",
                         isin_growth=None, isin_reinvest=None)  # fmt: skip


def synthetic():
    a = holding(1, "AAA Ltd", "stock", [ev(1, DAYS[0], "buy", 10, 100, 1000),
                                        ev(2, DAYS[2], "split", **{"from": "10", "to": "5"})], nse="AAA")  # fmt: skip
    b = holding(2, "BBB Ltd", "stock", [ev(3, DAYS[1], "buy", 5, 200, 1000), ev(4, DAYS[3], "dividend", amount=50)],
                nse="BBB")  # fmt: skip
    c = holding(3, "Test Flexi Cap Fund", "mf", [ev(5, DAYS[2], "buy", 100, 10, 1000)], code="111")
    return [a, b, c]


def build(hs, tmp_path, fetch=None, today=DAYS[-1]):
    from finresearch.portfolio.history import PriceStore, build

    return asyncio.run(build(hs, fetch=fetch or FakeFetch(), store=PriceStore(tmp_path / "ph"),
                             schemes={"111": SCHEME}, today=today))  # fmt: skip


def test_history_split_dividend_and_fund_match_hand_values(tmp_path):
    h = build(synthetic(), tmp_path)
    assert h.ok and h.days == DAYS
    # d0 A 1000; d1 A 1100 + B 1000; d2 A 20×55 + B 1050 + C 1000; d3 A 1200 + B 1025 + C 1010
    assert h.value == pytest.approx([1000, 2100, 3150, 3235])
    assert h.flow == pytest.approx([1000, 1000, 1000, -50])  # dividend paid out = outflow
    assert h.initial == pytest.approx(1000)
    r = h.returns
    assert r[1] == pytest.approx(0.10)
    assert r[2] == pytest.approx(2150 / 2100 - 1)  # the split day adds nothing for AAA (1100 -> 1100)
    assert r[3] == pytest.approx(3285 / 3150 - 1)  # the dividend counts as return
    assert h.index[-1] == pytest.approx(1.1 * 2150 / 2100 * 3285 / 3150)
    assert h.invested == pytest.approx([1000, 2000, 3000, 2950])
    assert [p.key for p in h.positions] == ["NSE:AAA", "NSE:BBB", "MF:111"]
    assert h.positions[0].units == pytest.approx(20)
    assert h.benchmark is not None and h.benchmark.level == pytest.approx([1, 1.01, 1.02, 1.03])
    assert not h.warnings  # the split is recorded, so the 50 % price drop is not flagged


def test_unrecorded_split_is_flagged(tmp_path):
    hs = synthetic()
    hs[0].events = [e for e in hs[0].events if e.kind != "split"]
    h = build(hs, tmp_path)
    assert any("AAA Ltd" in w and "no split or bonus recorded" in w for w in h.warnings)


def test_opening_balance_moves_the_start_and_counts_in_kind(tmp_path):
    hs = synthetic()
    hs.append(
        holding(4, "BBB via CAS", "stock", [ev(6, DAYS[1], "opening", 2, statement_opening=True)], nse="BBB")
    )
    h = build(hs, tmp_path)
    assert h.days[0] == DAYS[1] and "opening balance" in h.start_reason
    # day 0 (6-Jan): A's 10 units held before + A's none; flows: B buy 1000; opening counted in kind
    assert h.initial == pytest.approx(10 * 110 + 1000 + 2 * 200)
    assert h.returns[1] == pytest.approx((1100 + 1050 + 2 * 210 + 1000 - 1000) / (1100 + 1000 + 400) - 1)


def test_price_cache_fetches_only_missing_ranges(tmp_path):
    from finresearch.portfolio.history import PriceStore

    store, f = PriceStore(tmp_path / "ph"), FakeFetch()
    inst = SimpleNamespace(key="NSE:AAA")
    today = date(2026, 3, 1)
    got = asyncio.run(store.get("NSE:AAA", DAYS[0], DAYS[-1], lambda a, b: f(inst, a, b), today))
    assert len(got) == 4 and len(f.calls) == 1
    asyncio.run(store.get("NSE:AAA", DAYS[0], DAYS[-1], lambda a, b: f(inst, a, b), today))
    assert len(f.calls) == 1  # final range: served from disk
    asyncio.run(
        store.get("NSE:AAA", DAYS[0] - timedelta(days=10), DAYS[-1], lambda a, b: f(inst, a, b), today)
    )
    assert f.calls[-1][1:] == (DAYS[0] - timedelta(days=10), DAYS[0] - timedelta(days=1))


def test_holding_without_a_price_source_is_excluded_and_named(tmp_path):
    hs = synthetic()
    hs.append(holding(5, "Unlisted Co", "stock", [ev(7, DAYS[0], "buy", 1, 10, 10)]))
    h = build(hs, tmp_path)
    assert any("Unlisted Co" in x for x in h.excluded)
    assert h.value[0] == pytest.approx(1000)


def test_benchmark_actions_parse_units_and_split():
    from finresearch.portfolio.history import parse_benchmark_actions

    acts = [SimpleNamespace(ex_date=date(2019, 12, 19), subject="Face Value Split (Sub-Division) - From Rs 10/- To Rs 1/-",
                            dividend_per_share=None),
            SimpleNamespace(ex_date=date(2015, 2, 18), subject="Dividend - Rs 8 Per Unit", dividend_per_share=None)]  # fmt: skip
    divs, factors = parse_benchmark_actions(acts)
    assert divs == {date(2015, 2, 18): 8.0}
    assert factors == {date(2019, 12, 19): 10.0}


# --------------------------------------------------------------------------- payloads
def test_performance_payload_benchmark_is_itself(tmp_path):
    """A portfolio that holds only NIFTYBEES, bought with the same flows: PME 1, ₹ alpha 0, TWR gap 0."""
    from finresearch.portfolio.analytics import performance

    hs = [holding(1, "Nippon ETF Nifty BeES", "stock", [ev(1, DAYS[0], "buy", 10, 100, 1000),
                                                        ev(2, DAYS[2], "buy", 5, 102, 510)], nse="NIFTYBEES")]  # fmt: skip
    p = performance(build(hs, tmp_path))
    b = p["summary"]["benchmark"]
    assert b["pme"]["value"] == pytest.approx(1.0)
    assert b["alpha_inr"] == pytest.approx(0.0, abs=0.01)
    assert b["twr_gap_pp"] == pytest.approx(0.0, abs=1e-6)
    assert b["early"] and b["verdict"] is None  # under a year: no ahead/behind wording
    assert p["series"][-1]["die_value"] == pytest.approx(p["series"][-1]["value"])
    assert p["disclaimer"].startswith("Personal research")


def test_risk_payload_guards_short_history(tmp_path):
    from finresearch.portfolio.analytics import RiskFree, risk

    p = risk(build(synthetic(), tmp_path), RiskFree(0.065, "test", None, None), {})
    real = p["realised"]
    assert real["volatility"]["value"] is None and "needs 60" in real["volatility"]["reason"]
    assert real["var_1d"]["reason"].startswith("needs 250") and real["sharpe"]["value"] is None
    assert real["max_drawdown"]["value"] is not None  # needs no minimum
    assert "needs 120 common" in p["hypothetical"]["reason"]


def long_series(n=300, seed=7):
    import random

    rnd = random.Random(seed)
    days, d = [], date(2025, 1, 1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    bench, a, b = [100.0], [50.0], [200.0]
    for _ in range(n - 1):
        m = rnd.gauss(0.0004, 0.01)
        bench.append(bench[-1] * (1 + m))
        a.append(a[-1] * (1 + 1.5 * m + rnd.gauss(0, 0.008)))
        b.append(b[-1] * (1 + 0.5 * m + rnd.gauss(0, 0.004)))
    return days, bench, a, b


def test_risk_payload_matches_numpy_on_a_long_series(tmp_path):
    import numpy as np

    from finresearch.portfolio.analytics import RiskFree, risk

    days, bench, a, b = long_series()
    global DAYS
    old = DAYS
    try:
        DAYS = days  # FakeFetch reads the module-level calendar
        f = FakeFetch({"NSE:AAA": a, "NSE:BBB": b, "NSE:NIFTYBEES": bench})
        hs = [holding(1, "AAA", "stock", [ev(1, days[0], "buy", 100, 50, 5000)], nse="AAA"),
              holding(2, "BBB", "stock", [ev(2, days[0], "buy", 25, 200, 5000)], nse="BBB")]  # fmt: skip
        h = build(hs, tmp_path, fetch=f, today=days[-1])
        p = risk(h, RiskFree(0.065, "test", None, None), {"NSE:AAA": "IT", "NSE:BBB": "Banks"})
    finally:
        DAYS = old
    v = np.array(h.value)
    r = v[1:] / v[:-1] - 1
    rb = np.array(bench[1:]) / np.array(bench[:-1]) - 1
    real = p["realised"]
    assert real["volatility"]["value"] == pytest.approx(r.std(ddof=1) * math.sqrt(252), abs=1e-4)
    assert real["beta"]["value"] == pytest.approx(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1), abs=1e-4)
    assert real["var_1d"]["value"] == pytest.approx(-np.quantile(r, 0.05), abs=1e-4)
    assert real["var_1d"]["inr"] == pytest.approx(-np.quantile(r, 0.05) * v[-1], abs=1)
    hyp = p["hypothetical"]
    assert sum(x["risk_share"] for x in hyp["positions"]) == pytest.approx(1.0, abs=1e-3)
    assert {x["sector"] for x in hyp["sectors"]} == {"IT", "Banks"}
    # AAA (beta 1.5 plus more noise) carries more than its weight of the risk
    top = hyp["positions"][0]
    assert top["key"] == "NSE:AAA" and top["risk_share"] > top["weight"]


def test_stress_scenario_uses_own_moves_and_proxies():
    from finresearch.portfolio.analytics import SCENARIOS, scenario_result
    from finresearch.portfolio.history import Position

    sc = SCENARIOS[0]
    bd = [date(2020, 1, 1) + timedelta(days=i) for i in range(0, 100, 5)]
    bench = {d: 100 - min(i, 18 - i) * 4.0 for i, d in enumerate(bd)}  # falls 36 % to bd[9], then recovers
    pos = [Position("NSE:AAA", "AAA", "stock", "equity", [1], 10, 10, None, 1000, None, "AAA"),
           Position("NSE:NEW", "New IPO", "stock", "equity", [2], 10, 10, None, 1000, None, "NEW"),
           Position("MF:9", "Liquid", "mf", "debt_mf", [3], 10, 10, None, 1000, None, None)]  # fmt: skip
    own = {"NSE:AAA": {bd[0]: 200.0, bd[9]: 100.0}}
    res = scenario_result(sc, bench, {}, {}, pos, own)
    assert res["available"] and res["peak"] == bd[0].isoformat() and res["trough"] == bd[9].isoformat()
    assert res["benchmark_move"] == pytest.approx(64 / 100 - 1)
    moves = {x["key"]: (x["move"], x["how"]) for x in res["positions"]}
    assert moves["NSE:AAA"] == (-0.5, "own prices")
    assert moves["NSE:NEW"][0] == pytest.approx(-0.36) and "proxy" in moves["NSE:NEW"][1]
    assert moves["MF:9"][0] == 0 and "not modelled" in moves["MF:9"][1]
    assert res["inr"] == pytest.approx(-500 - 360)


def test_concentration_flags_and_status(tmp_path):
    from finresearch.portfolio.analytics import concentration

    h = build(synthetic(), tmp_path)
    c = concentration(h, {"NSE:AAA": "IT", "NSE:BBB": "IT"}, {"NSE:BBB": "Tata"}, None)
    w = {x["key"]: x["weight_pct"] for x in c["positions"]}
    assert w["NSE:AAA"] == pytest.approx(1200 / 3235 * 100, abs=0.01)
    kinds = {f["kind"] for f in c["flags"]}
    assert kinds == {"stock", "sector", "group"}  # 37 % stock, 69 % IT, 32 % "Tata" (your map)
    assert all("you should" not in f["text"].lower() for f in c["flags"])
    assert c["n_effective"] == pytest.approx(1 / sum((x / 3235) ** 2 for x in (1200, 1025, 1010)), abs=0.01)
    assert c["funds_note"] and "not looked through" in c["funds_note"]
    calm = concentration(h, {"NSE:AAA": "IT", "NSE:BBB": "Banks"}, {}, 50)
    assert {f["kind"] for f in calm["flags"]} == {
        "sector"
    }  # IT 37 % > 25 %; 37 % stock is inside your 50 % limit
    assert calm["limits"]["stock_source"] == "your profile's max position"


def test_group_seed_map():
    from finresearch.portfolio.analytics import group_of

    p = SimpleNamespace(key="NSE:TCS", asset_type="stock", nse_symbol="TCS", name="Tata Consultancy Services")
    assert group_of(p, {}) == ("Tata", "starting map [unverified]")
    assert group_of(p, {"NSE:TCS": ""}) == (None, "your map")
    q = SimpleNamespace(key="NSE:XYZ", asset_type="stock", nse_symbol="XYZ", name="Godrej Something Ltd")
    assert group_of(q, {})[0] == "Godrej"


@dataclass
class Ter:
    name: str
    category: str | None
    day: date
    regular: D | None
    direct: D | None


def test_costs_switch_shows_tax_load_and_break_even(tmp_path):
    from finresearch.adapters.amfi import ter_key
    from finresearch.portfolio.analytics import FundLots, costs
    from finresearch.portfolio.tax import HoldingTax

    hs = [
        holding(3, "Test Flexi Cap Fund", "mf", [ev(5, date(2025, 1, 6), "buy", 5000, 10, 50000)], code="111")
    ]
    closes = {"MF:111": [10, 10, 10, 20], "NSE:NIFTYBEES": [100, 101, 102, 103]}
    days = [date(2025, 1, 6), date(2025, 6, 2), date(2025, 12, 1), date(2026, 1, 8)]
    global DAYS
    old = DAYS
    try:
        DAYS = days
        h = build(hs, tmp_path, fetch=FakeFetch(closes), today=days[-1])
    finally:
        DAYS = old
    table = {
        ter_key("Test Flexi Cap Fund"): Ter(
            "Test Flexi Cap Fund", None, date(2026, 1, 1), D("1.60"), D("0.60")
        )
    }
    ht = HoldingTax(3, "Test Flexi Cap Fund", "Test", None, "equity", False, None)
    lots = {3: FundLots(3, ht, [(date(2025, 1, 6), D(5000), D(10))])}
    out = costs(h, table, "test", lots, [], D("0.30"), {"3": {"pct": 1, "days": 365}}, days[-1])
    assert out["weighted_ter_pct"] == pytest.approx(1.6) and out["suppressed"] == 0
    sw = out["switches"][0]
    # value 1,00,000; gain 50,000 long-term (> 12 months) inside the ₹1.25 lakh exemption: tax 0
    assert (
        sw["costs"]["tax"] == 0 and sw["costs"]["exit_load"] == 0
    )  # the lot is older than the 365-day load period
    assert sw["costs"]["stamp"] == pytest.approx(100000 * 0.00005, abs=1)
    assert sw["yearly_saving"] == pytest.approx(1000)
    assert sw["break_even_years"] == pytest.approx(0.0, abs=0.05)
    assert sw["ten_year_difference"] == pytest.approx(100000 * (1.094**10 - 1.084**10), abs=1)
    # the same fund at a fifth of the size saves 200 a year: trivial, suppressed and counted
    small = {3: FundLots(3, ht, [(date(2025, 1, 6), D(1000), D(10))])}
    hs[0].events = [ev(5, date(2025, 1, 6), "buy", 1000, 10, 10000)]
    try:
        DAYS = days
        h2 = build(hs, tmp_path / "small", fetch=FakeFetch(closes), today=days[-1])
    finally:
        DAYS = old
    out2 = costs(h2, table, "test", small, [], D("0.30"), {}, days[-1])
    assert out2["switches"] == [] and out2["suppressed"] == 1 and "no action needed" in out2["status"]


def test_costs_suppresses_trivial_and_reports_unknown_load(tmp_path):
    from finresearch.adapters.amfi import ter_key
    from finresearch.portfolio.analytics import FundLots, costs
    from finresearch.portfolio.tax import HoldingTax

    hs = [holding(3, "Test Flexi Cap Fund", "mf", [ev(5, DAYS[0], "buy", 10000, 10, 100000)], code="111")]
    h = build(hs, tmp_path, fetch=FakeFetch({"MF:111": [10, 10, 10, 10.5], "NSE:NIFTYBEES": [1, 1, 1, 1]}))
    table = {
        ter_key("Test Flexi Cap Fund"): Ter(
            "Test Flexi Cap Fund", None, date(2026, 1, 1), D("1.60"), D("0.60")
        )
    }
    ht = HoldingTax(3, "Test Flexi Cap Fund", "Test", None, "equity", False, None)
    lots = {3: FundLots(3, ht, [(DAYS[0], D(10000), D(10))])}
    out = costs(h, table, "test", lots, [], D("0.30"), {}, DAYS[-1])
    sw = out["switches"][0]
    # value 1,05,000; STCG 5,000 at 20 % + 4 % cess = 1,040; saving 1 % a year = 1,050
    assert sw["costs"]["tax"] == pytest.approx(1040)
    assert sw["costs"]["exit_load_known"] is False and "exit load unknown" in sw["text"]
    assert sw["yearly_saving"] == pytest.approx(1050)
    total = sw["costs"]["total"]
    assert sw["break_even_years"] == pytest.approx(round(total / 1050, 1))
    assert sw["text"].index("costs about") < sw["text"].index("saves about")  # costs before the benefit


# --------------------------------------------------------------------------- API (fake sources, test database)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}


class FakeEquity:
    def __init__(self, closes):
        self.closes = closes

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def history(self, sym, start, end, series="EQ"):
        c = self.closes.get(f"NSE:{sym}", [])
        return [
            SimpleNamespace(day=d, close=D(str(v)))
            for d, v in zip(DAYS, c, strict=False)
            if start <= d <= end
        ]

    async def corporate_actions(self, sym):
        return []


@pytest.fixture
def api(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.adapters.amfi import SchemeNav
    from finresearch.adapters.fbil import FALLBACK_CURVE
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.api.portfolio_analytics import AnalyticsSources
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting"))  # fmt: skip
    scheme = SchemeNav("111", "Test Flexi Cap Fund - Regular Plan - Growth", "Regular", "Growth", None, None, D(10),
                       DAYS[-1], "Equity Scheme - Flexi Cap Fund", "X AMC")  # fmt: skip

    async def rows():
        return [scheme]

    async def navs(sch, start, end):
        return [SimpleNamespace(day=d, nav=D(str(v))) for d, v in zip(DAYS, CLOSES["MF:111"], strict=True)
                if start <= d <= end]  # fmt: skip

    async def quote(sym):
        return SimpleNamespace(industry={"AAA": "IT - Software", "BBB": "Banks"}.get(sym))

    async def curve():
        return FALLBACK_CURVE

    async def ter(month):
        from finresearch.adapters.amfi import ter_key

        return {
            ter_key("Test Flexi Cap Fund"): Ter("Test Flexi Cap Fund", None, DAYS[0], D("1.60"), D("0.60"))
        }

    app = create_app(nav_all=rows)
    app.state.markets = MarketSources(equity=lambda: FakeEquity(CLOSES), nav_history=navs, quote=quote,
                                      today=lambda: DAYS[-1])  # fmt: skip
    app.state.analytics_sources = AnalyticsSources(ter=ter, curve=curve)
    with TestClient(app) as c:
        for body in ({"asset_type": "stock", "name": "AAA Ltd", "nse_symbol": "AAA", "day": "2026-01-05", "kind": "buy",
                      "quantity": "10", "price": "100", "amount": "1000"},
                     {"asset_type": "stock", "name": "BBB Ltd", "nse_symbol": "BBB", "day": "2026-01-06", "kind": "buy",
                      "quantity": "5", "price": "200", "amount": "1000"},
                     {"asset_type": "mf", "name": "Test Flexi Cap Fund", "scheme_code": "111", "day": "2026-01-07",
                      "kind": "buy", "quantity": "10000", "price": "10", "amount": "100000"}):  # fmt: skip
            assert c.post("/api/portfolio/transactions", headers=ORIGIN, json=body).status_code == 201
        yield c


def test_api_routes_end_to_end(api):
    p = api.get("/api/portfolio/analytics/performance").json()
    assert p["available"] and p["summary"]["value"] == pytest.approx(10 * 60 + 5 * 205 + 10000 * 10.1)
    assert p["summary"]["benchmark"]["symbol"] == "NIFTYBEES"
    r = api.get("/api/portfolio/analytics/risk").json()
    assert r["risk_free"]["fallback"] and "FBIL" in r["risk_free"]["label"]
    assert r["realised"]["volatility"]["reason"]
    assert {s["id"] for s in r["stress"]} >= {"covid2020", "gfc2008", "beta20"}
    r2 = api.get("/api/portfolio/analytics/risk?rf=7").json()
    assert r2["risk_free"]["rate"] == pytest.approx(0.07) and r2["risk_free"]["user_set"]
    c = api.get("/api/portfolio/analytics/concentration").json()
    assert {x["sector"] for x in c["positions"]} == {"IT - Software", "Banks", "Funds (not looked through)"}
    k = api.get("/api/portfolio/analytics/costs").json()
    assert k["available"] and k["funds"][0]["plan"] == "Regular" and k["switches"]
    hid = k["switches"][0]["holding_ids"][0]
    put = api.put("/api/portfolio/analytics/settings", headers=ORIGIN,
                  json={"groups": {"NSE:AAA": "My group"}, "exit_loads": {str(hid): {"pct": 1, "days": 365}}})  # fmt: skip
    assert put.status_code == 200
    c2 = api.get("/api/portfolio/analytics/concentration").json()
    assert next(x for x in c2["positions"] if x["key"] == "NSE:AAA")["group"] == "My group"
    k2 = api.get("/api/portfolio/analytics/costs").json()
    assert k2["switches"][0]["costs"]["exit_load_known"] and k2["switches"][0]["costs"][
        "exit_load"
    ] == pytest.approx(1010)


def test_api_empty_portfolio(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(
            text(
                "TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import"
            )
        )
    with TestClient(create_app()) as c:
        for path in ("performance", "risk", "concentration", "costs"):
            j = c.get(f"/api/portfolio/analytics/{path}").json()
            assert j["available"] is False and j["reason"] and j["disclaimer"]
