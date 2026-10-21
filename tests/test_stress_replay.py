"""Historical stress replay on today's holdings (#345): synthetic fixtures only, expected values worked by hand."""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest

from finresearch.portfolio import stress_replay as sr

D = date
W = sr.Window("t", "Test fall", D(2020, 2, 3), D(2020, 2, 7), "synthetic")
BASE = D(2020, 1, 31)
DAYS = [
    D(2020, 2, 3),
    D(2020, 2, 4),
    D(2020, 2, 5),
    D(2020, 2, 6),
    D(2020, 2, 7),
    D(2020, 2, 10),
    D(2020, 2, 11),
]
INDEX = dict(zip([BASE, *DAYS], [100, 98, 95, 97, 90, 92, 99, 101], strict=True))


def test_the_five_dated_windows_of_the_issue():
    got = {w.id: (w.start, w.end) for w in sr.WINDOWS}
    assert got == {
        "gfc2008": (D(2008, 9, 1), D(2008, 11, 30)),
        "taper2013": (D(2013, 5, 1), D(2013, 8, 31)),
        "covid2020": (D(2020, 2, 1), D(2020, 3, 31)),
        "election2024": (D(2024, 6, 3), D(2024, 6, 5)),
        "febmar2026": (D(2026, 2, 1), D(2026, 3, 31)),
    }


def test_market_days_start_from_the_last_close_before_the_window():
    base, days = sr.market_days(INDEX, W, D(2020, 2, 11))
    assert base == BASE and days == DAYS
    base, days = sr.market_days(INDEX, W, D(2020, 2, 7))  # the recovery horizon cuts the path
    assert days == DAYS[:5]


def test_market_days_refuse_a_gap_or_a_missing_base():
    gap = {d: v for d, v in INDEX.items() if d not in DAYS[:4]}
    gap = {**gap, D(2020, 1, 30): 99}
    gap.pop(BASE)  # 30-Jan -> 7-Feb is 8 calendar days: the backfill has a hole, never read as "no move"
    assert isinstance(sr.market_days(gap, W, D(2020, 2, 11)), str)
    late = {d: v for d, v in INDEX.items() if d != BASE}
    assert "before" in sr.market_days(late, W, D(2020, 2, 11))
    short = {d: v for d, v in INDEX.items() if d <= D(2020, 2, 5)}  # data stops inside the window
    assert isinstance(sr.market_days(short, W, D(2020, 2, 11)), str)


def test_own_path_chains_closes_and_applies_an_in_window_split():
    closes = {BASE: 100.0, DAYS[0]: 90.0, DAYS[1]: 45.0, DAYS[2]: 45.0, DAYS[3]: 40.5, DAYS[4]: 45.0}
    r, why = sr.own_returns(closes, {DAYS[1]: 2.0}, BASE, DAYS[:5], W.end)
    assert why is None
    assert r == pytest.approx(
        {DAYS[0]: -0.10, DAYS[1]: 0.0, DAYS[2]: 0.0, DAYS[3]: -0.10, DAYS[4]: 45 / 40.5 - 1}
    )


def test_own_path_rejects_an_unrecorded_split_instead_of_a_fake_crash():
    closes = {BASE: 100.0, DAYS[0]: 100.0, DAYS[1]: 50.0, DAYS[2]: 50.0, DAYS[3]: 50.0, DAYS[4]: 50.0}
    r, why = sr.own_returns(closes, {}, BASE, DAYS[:5], W.end)
    assert r is None and "split" in why


def test_own_path_needs_a_price_at_the_start_and_through_the_window():
    closes = {d: 10.0 for d in DAYS}
    r, why = sr.own_returns(closes, {}, BASE, DAYS, W.end)  # listed after the window began
    assert r is None and "start" in why
    thin = {BASE: 10.0, DAYS[4]: 9.0}
    r, why = sr.own_returns(thin, {}, BASE, DAYS[:5], W.end)
    assert r is None and "window" in why


def test_own_path_stops_after_the_window_where_prices_end():
    closes = {BASE: 10.0, **{d: 10.0 for d in DAYS[:5]}}
    r, why = sr.own_returns(closes, {}, BASE, DAYS, W.end)
    assert why is None and sorted(r) == DAYS[:5]  # 10/11-Feb have no close: the path ends, never ffilled


def index_returns():
    return sr.level_returns(INDEX, BASE, DAYS)


def test_index_and_beta_paths():
    r = index_returns()
    assert r[DAYS[0]] == pytest.approx(-0.02) and r[DAYS[3]] == pytest.approx(90 / 97 - 1)
    assert sr.beta_returns(0.5, r)[DAYS[1]] == pytest.approx(0.5 * (95 / 98 - 1))


def test_duration_path_is_minus_duration_times_yield_change():
    y = {BASE: 0.070, DAYS[0]: 0.071, DAYS[1]: 0.073, DAYS[2]: 0.072, DAYS[3]: 0.075, DAYS[4]: 0.075}
    r, why = sr.duration_returns(6.0, y, BASE, DAYS[:5], W.end)
    assert why is None
    assert r == pytest.approx(
        {DAYS[0]: -0.006, DAYS[1]: -0.012, DAYS[2]: 0.006, DAYS[3]: -0.018, DAYS[4]: 0.0}
    )
    r, why = sr.duration_returns(6.0, {BASE: 0.07}, BASE, DAYS[:5], W.end)
    assert r is None and "yield" in why


def test_beta_estimate_matches_cov_over_var():
    days = [D(2025, 1, 1) + timedelta(days=i) for i in range(200)]
    m = {d: 100 * (1 + 0.01 * ((i % 7) - 3)) for i, d in enumerate(days)}
    a = {d: 50 * (1 + 0.02 * ((i % 7) - 3)) for i, d in enumerate(days)}
    import numpy as np

    rm = np.array([m[days[i]] / m[days[i - 1]] - 1 for i in range(1, 200)])
    ra = np.array([a[days[i]] / a[days[i - 1]] - 1 for i in range(1, 200)])
    want = np.cov(ra, rm, ddof=1)[0, 1] / np.var(rm, ddof=1)
    got = sr.estimate_beta(a, None, m, None, days[-1])
    assert got is not None and got[0] == pytest.approx(want) and got[1] == 199
    assert sr.estimate_beta({d: a[d] for d in days[-50:]}, None, m, None, days[-1]) is None  # under 120 days


def golden():
    """A (own closes, ₹1,000), B (beta 0.5 × index, ₹2,000), C (no path, ₹500). Hand values:
    A levels 1, .9, .9, .8, .84 | .92, 1.04; B levels .99, .9748469, .9851085, .9495639, .9601146 | .9966407, 1.0067078;
    value 3000 -> 2980, 2849.694, 2870.217, 2699.128, 2760.229 | 2913.281, 3053.416."""
    a_closes = dict(zip([BASE, *DAYS], [50, 50, 45, 45, 40, 42, 46, 52], strict=True))
    a, _ = sr.own_returns(a_closes, {}, BASE, DAYS, W.end)
    legs = [
        sr.Leg("NSE:AAA", "AAA Ltd", 1000.0, "own prices", a),
        sr.Leg("NSE:BBB", "BBB Ltd", 2000.0, "beta 0.50 × Nifty 50", sr.beta_returns(0.5, index_returns())),
    ]
    missing = [sr.Missing("MF:999", "Test Debt Fund", 500.0, "no own NAVs for the window; not equity")]
    return sr.replay(W, BASE, DAYS, index_returns(), legs, missing, "Nifty 50 (index_eod)")


def test_replay_loss_worst_day_drawdown_and_recovery_by_hand():
    p = golden().payload()
    assert p["available"] and p["base"] == "2020-01-31"
    assert (
        p["mapped_value"] == 3000
        and p["unmapped_value"] == 500
        and p["mapped_share"] == pytest.approx(3000 / 3500, abs=1e-4)
    )
    v_end = 840 + 2000 * 0.99 * (1 + 0.5 * (95 / 98 - 1)) * (1 + 0.5 * (97 / 95 - 1)) * (
        1 + 0.5 * (90 / 97 - 1)
    ) * (1 + 0.5 * (92 / 90 - 1))
    assert p["loss"]["inr"] == pytest.approx(v_end - 3000, abs=0.5)
    assert p["loss"]["pct"] == pytest.approx(v_end / 3000 - 1, abs=1e-4)
    assert p["worst_day"]["date"] == "2020-02-06" and p["worst_day"]["pct"] == pytest.approx(
        2699.128 / 2870.217 - 1, abs=1e-4
    )
    dd = p["drawdown"]
    assert (dd["peak"], dd["trough"]) == ("2020-01-31", "2020-02-06")
    assert dd["pct"] == pytest.approx(2699.128 / 3000 - 1, abs=1e-4)
    assert p["recovery"]["date"] == "2020-02-11" and p["recovery"]["days"] == 5
    assert p["index"]["move"] == pytest.approx(-0.08) and p["index"]["drawdown"] == pytest.approx(-0.10)


def test_contributors_add_up_to_the_fall_and_unmapped_is_not_zero():
    p = golden().payload()
    c = {x["key"]: x["inr"] for x in p["contributors"]}
    assert c["NSE:AAA"] == pytest.approx(-200, abs=0.5) and c["NSE:BBB"] == pytest.approx(-100.87, abs=0.5)
    assert sum(c.values()) == pytest.approx(p["drawdown"]["inr"], abs=1)
    assert p["contributors"][0]["key"] == "NSE:AAA"
    u = p["unmapped"][0]
    assert u["key"] == "MF:999" and u["value"] == 500 and u["reason"]
    assert "MF:999" not in {x["key"] for x in p["positions"]}  # never shown as a 0 % move


def test_not_recovered_is_said_not_hidden():
    days = DAYS[:6]  # the horizon ends on 10-Feb, still below the peak
    r = sr.level_returns(INDEX, BASE, days)
    rep = sr.replay(
        W, BASE, days, r, [sr.Leg("NSE:BBB", "BBB", 1000.0, "beta 1.00", sr.beta_returns(1.0, r))], [], "x"
    )
    rec = rep.payload()["recovery"]
    assert rec["date"] is None and rec["days"] is None and "10-Feb-2020" in rec["note"]


def test_nothing_mapped_is_unavailable_with_the_list():
    rep = sr.replay(W, BASE, DAYS, index_returns(), [], [sr.Missing("X", "X", 10.0, "why")], "x")
    p = rep.payload()
    assert p["available"] is False and p["unmapped"][0]["key"] == "X" and p["loss"] is None


# --------------------------------------------------------------------------- mapping hierarchy
RECENT_DAYS = [D(2026, 1, 1) + timedelta(days=i) for i in range(200)]
MKT_RECENT = {d: 100 * (1 + 0.01 * ((i % 5) - 2)) for i, d in enumerate(RECENT_DAYS)}


def twice_market():  # a holding whose recent daily returns are exactly 2 × the market's: beta 2
    out, lv = {RECENT_DAYS[0]: 10.0}, 10.0
    for a, b in pairwise(RECENT_DAYS):
        lv *= 1 + 2 * (MKT_RECENT[b] / MKT_RECENT[a] - 1)
        out[b] = lv
    return out


def ctx(**kw):
    base = dict(
        base=BASE,
        days=DAYS,
        end=W.end,
        market=index_returns(),
        market_label="Nifty 50",
        market_recent=MKT_RECENT,
        market_factors=None,
        as_of=RECENT_DAYS[-1],
        categories={},
        yields=None,
    )
    return sr.Context(**{**base, **kw})


def hd(**kw):
    base = dict(
        key="NSE:AAA",
        name="AAA Ltd",
        value=1000.0,
        asset_type="stock",
        tax_class="equity",
        closes={},
        factors={},
        recent={},
        category=None,
        mod_duration=None,
    )
    return sr.HoldingData(**{**base, **kw})


def test_own_prices_win():
    closes = dict(zip([BASE, *DAYS], [50, 50, 45, 45, 40, 42, 46, 52], strict=True))
    g = sr.map_holding(hd(closes=closes, recent=twice_market()), ctx())
    assert isinstance(g, sr.Leg) and g.how == "own prices" and g.returns[DAYS[1]] == pytest.approx(-0.1)


def test_equity_without_window_prices_uses_its_beta():
    g = sr.map_holding(hd(recent=twice_market()), ctx())
    assert isinstance(g, sr.Leg) and g.how.startswith("beta 2.00 × Nifty 50")
    assert g.returns[DAYS[0]] == pytest.approx(2 * -0.02)


def test_equity_with_too_short_a_history_is_unmapped_not_beta_one():
    short = {d: v for d, v in twice_market().items() if d >= RECENT_DAYS[-60]}
    m = sr.map_holding(hd(recent=short), ctx())
    assert isinstance(m, sr.Missing) and "beta" in m.reason and m.value == 1000


def test_debt_fund_without_navs_is_unmapped_not_zero():
    m = sr.map_holding(
        hd(
            key="MF:9",
            asset_type="mf",
            tax_class="debt",
            recent=twice_market(),
            category="Debt Scheme - Gilt Fund",
        ),
        ctx(),
    )
    assert isinstance(m, sr.Missing) and "not equity" in m.reason


def test_fund_uses_its_category_index_with_its_beta_to_it():
    cat = {"NIFTY MIDCAP 150": (sr.level_returns(INDEX, BASE, DAYS), MKT_RECENT)}
    g = sr.map_holding(
        hd(key="MF:7", asset_type="mf", recent=twice_market(), category="Equity Scheme - Mid Cap Fund"),
        ctx(categories=cat),
    )
    assert (
        isinstance(g, sr.Leg) and "NIFTY MIDCAP 150" in g.how and g.returns[DAYS[0]] == pytest.approx(-0.04)
    )


def test_fund_category_without_window_data_falls_back_to_market_beta():
    cat = {"NIFTY MIDCAP 150": (None, MKT_RECENT)}
    g = sr.map_holding(
        hd(key="MF:7", asset_type="mf", recent=twice_market(), category="Equity Scheme - Mid Cap Fund"),
        ctx(categories=cat),
    )
    assert isinstance(g, sr.Leg) and "Nifty 50" in g.how


def test_bond_uses_duration_where_yields_exist_else_unmapped():
    y = {BASE: 0.07, **{d: 0.07 + 0.001 * (i + 1) for i, d in enumerate(DAYS)}}
    g = sr.map_holding(hd(key="NSE:GS1", tax_class="debt", mod_duration=5.0), ctx(yields=y))
    assert isinstance(g, sr.Leg) and "duration" in g.how and g.returns[DAYS[0]] == pytest.approx(-0.005)
    m = sr.map_holding(hd(key="NSE:GS1", tax_class="debt", mod_duration=5.0), ctx())
    assert isinstance(m, sr.Missing) and "yield" in m.reason


# --------------------------------------------------------------------------- loading (fake sources, synthetic book)
def _weekdays(a, b):
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if (a + timedelta(days=i)).weekday() < 5]


ELEC = _weekdays(D(2024, 5, 20), D(2024, 7, 31))
NIFTY_2024 = {d: 100.0 + (3 if d == D(2024, 6, 3) else -3 if d == D(2024, 6, 4) else 0 if d < D(2024, 6, 5) else 1)
              for d in ELEC}  # fmt: skip
NOW_DAYS = [D(2026, 3, 1) + timedelta(days=i) for i in range(200)]
NOW_MKT = {d: 100 * (1 + 0.01 * ((i % 5) - 2)) for i, d in enumerate(NOW_DAYS)}


def synthetic_history():
    from finresearch.portfolio.history import Benchmark, History, Position

    lv, bbb = 10.0, {NOW_DAYS[0]: 10.0}
    for a, b in pairwise(NOW_DAYS):
        lv *= 1 + 2 * (NOW_MKT[b] / NOW_MKT[a] - 1)
        bbb[b] = lv
    pos = [Position("NSE:AAA", "AAA Ltd", "stock", "equity", [1], 10, 100, None, 1000.0, None, "AAA"),
           Position("NSE:BBB", "BBB Ltd", "stock", "equity", [2], 10, 100, None, 2000.0, None, "BBB"),
           Position("MF:9", "Test Liquid Fund", "mf", "debt", [3], 10, 50, None, 500.0, None, None,
                    scheme=type("S", (), {"code": "9", "category": "Debt Scheme - Liquid Fund"})())]  # fmt: skip
    bench = Benchmark(NOW_DAYS, [NOW_MKT[d] for d in NOW_DAYS], NOW_MKT, {}, {})
    return History(days=NOW_DAYS, value=[1.0] * 200, index=[1.0] * 200, positions=pos,
                   closes={"NSE:BBB": bbb, "NSE:AAA": {}}, factors={}, benchmark=bench)  # fmt: skip


async def fake_fetch(inst, a, b):
    if inst.key == "NSE:AAA":
        return {d: 50.0 * (0.9 if d == D(2024, 6, 4) else 1.0) for d in ELEC if a <= d <= b}, True
    return {}, True


def fake_index(sym, a, b):
    src = {"NIFTY": NIFTY_2024, "INDIA VIX": {d: 16.0 + (10 if d == D(2024, 6, 4) else 0) for d in ELEC}}.get(
        sym, {}
    )
    return {d: v for d, v in src.items() if a <= d <= b}


async def test_load_replays_every_window_and_says_why_one_is_missing(tmp_path):
    from finresearch.portfolio.history import PriceStore

    out = await sr.load(
        synthetic_history(), fake_fetch, PriceStore(tmp_path), D(2026, 10, 22), index_series=fake_index
    )
    by = {s["id"]: s for s in out["scenarios"]}
    assert list(by) == [w.id for w in sr.WINDOWS]
    e = by["election2024"]
    assert e["available"] and e["base"] == "2024-05-31" and "index_eod" in e["index_source"]
    how = {p["key"]: p["how"] for p in e["positions"]}
    assert how["NSE:AAA"] == "own prices" and how["NSE:BBB"].startswith("beta 2.00")
    assert e["unmapped"][0]["key"] == "MF:9" and e["mapped_share"] == pytest.approx(3000 / 3500, abs=1e-4)
    assert e["worst_day"]["date"] == "2024-06-04"
    # AAA -10 % (₹-100) and BBB 2 × (97/103 - 1) (₹-246.99) on 4-Jun, after +6 % on 3-Jun
    assert e["worst_day"]["inr"] == pytest.approx(-100 + 2000 * 1.06 * 2 * (97 / 103 - 1), abs=0.5)
    assert e["vix"] == {"start": 16.0, "peak": 26.0, "peak_date": "2024-06-04"}
    g = by["gfc2008"]  # no index_eod rows before 2013 and the fake NIFTYBEES has none: unavailable, never 0
    assert g["available"] is False and "NIFTYBEES" in g["reason"] and "index_eod" in g["reason"]


def test_a_bond_without_a_duration_says_why():
    m = sr.map_holding(hd(key="NSE:NCD1", tax_class="debt", bond=True), ctx())
    assert isinstance(m, sr.Missing) and "duration" in m.reason and "yield" in m.reason


def test_market_days_refuse_a_window_with_no_close_inside():
    """A backfill hole over a short window (closes before and after it, none inside) is unavailable, never a replay of
    zero days (which would also crash the payload)."""
    w = sr.Window("e", "Short", D(2024, 6, 3), D(2024, 6, 5), "synthetic")
    level = {D(2024, 5, 31): 100.0, D(2024, 6, 6): 101.0, D(2024, 6, 7): 102.0}
    why = sr.market_days(level, w, D(2024, 6, 30))
    assert isinstance(why, str) and "inside" in why


async def test_one_failing_window_does_not_take_the_others_down(tmp_path):
    from finresearch.portfolio.history import PriceStore

    def flaky(sym, a, b):
        if a.year == 2013:
            raise RuntimeError("synthetic failure")
        return fake_index(sym, a, b)

    out = await sr.load(
        synthetic_history(), fake_fetch, PriceStore(tmp_path), D(2026, 10, 22), index_series=flaky
    )
    by = {s["id"]: s for s in out["scenarios"]}
    assert by["taper2013"]["available"] is False and "RuntimeError" in by["taper2013"]["reason"]
    assert by["election2024"]["available"] is True
