"""WP-E prediction experiments (#147): calibration policy, IPO calibration walk-forward, point-in-time stock universe.
Golden values are hand-computed; synthetic data is seeded; everything is offline."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from statistics import NormalDist

import numpy as np
import pytest

from finresearch.evals import calibration_policy as cp
from finresearch.evals import ipo_calibration as ic
from finresearch.evals import stock_backtest as bt
from finresearch.evals import stock_universe as su
from finresearch.fincalc import signals as sg


# --------------------------------------------------------------------------- calibration policy (§2.6)
def test_policy_tiers_at_the_boundaries_and_effective_n():
    assert [cp.policy_tier(n) for n in (0, 49, 50, 99, 100, 999, 1000, 5000)] == [
        "base_rate", "base_rate", "shrink", "shrink", "platt", "platt", "isotonic", "isotonic"]  # fmt: skip
    assert cp.effective_n(600, 12) == 50 and cp.policy_tier(cp.effective_n(600, 12)) == "shrink"
    assert cp.policy_tier(cp.effective_n(599, 12)) == "base_rate"
    with pytest.raises(ValueError):
        cp.effective_n(10, 0)


def test_base_rate_tier_returns_the_base_rate_and_shrink_is_hand_computed():
    cal = cp.fit_policy([0.9, 0.8, 0.2], [1, 0, 0])
    assert cal.tier == "base_rate" and cal.apply([0.9, 0.1]).tolist() == pytest.approx([1 / 3, 1 / 3])
    p = [0.9] * 75
    y = [1] * 37 + [0] * 38  # base rate 37/75
    cal = cp.fit_policy(p, y, k=75)
    assert cal.tier == "shrink" and cal.params["w"] == pytest.approx(0.5)
    assert cal.apply(0.9)[0] == pytest.approx(0.5 * 0.9 + 0.5 * 37 / 75)
    # PREREG golden: w = 0.5, p = 0.9, p̄ = 0.5 → 0.7
    c2 = cp.Calibrator("shrink", 75, 75, 0.5, {"w": 0.5})
    assert c2.apply(0.9)[0] == pytest.approx(0.7)
    assert c2.to_json()["description"].startswith("50–99")
    with pytest.raises(ValueError):
        cp.fit_policy([0.5], [1, 0])


def _distorted(n: int, fn, seed: int = 7):
    rng = np.random.default_rng(seed)
    p = 1 / (1 + np.exp(-rng.uniform(-3, 3, n)))
    y = (rng.uniform(size=n) < fn(p)).astype(float)
    return p, y


def test_platt_recovers_a_known_distortion_and_improves_brier():
    p, y = _distorted(20_000, lambda q: 1 / (1 + np.exp(-(0.5 * cp.logit(q) + 0.3))))
    a, b = cp.fit_platt(p, y)
    assert a == pytest.approx(0.5, abs=0.05) and b == pytest.approx(0.3, abs=0.05)
    raw = float(np.mean((p - y) ** 2))
    cal = float(np.mean((cp.apply_platt(p, a, b) - y) ** 2))
    assert cal < raw
    policy = cp.fit_policy(p[:500], y[:500])
    assert policy.tier == "platt" and set(policy.params) == {"a", "b"}


def test_temperature_recovers_t_2():
    p, y = _distorted(20_000, lambda q: 1 / (1 + np.exp(-cp.logit(q) / 2)), seed=11)
    t = cp.fit_temperature(p, y)
    assert t == pytest.approx(2.0, abs=0.15)
    assert cp.apply_temperature([0.5], 3.0)[0] == pytest.approx(0.5)
    assert cp.apply_temperature([0.9], 1.0)[0] == pytest.approx(0.9)


def test_pav_matches_the_hand_computed_case_and_is_monotone():
    knots, values = cp.fit_isotonic([0.1, 0.2, 0.3, 0.4, 0.5], [1, 0, 1, 0, 1])
    assert values == pytest.approx([0.5, 0.5, 1.0])
    assert knots == pytest.approx([0.15, 0.35, 0.5])
    # PAV blocks {0.1,0.2}→0.5, {0.3,0.4}→0.5, {0.5}→1; between block centres the map is linear (0.4 → 2/3)
    fitted = cp.apply_isotonic([0.1, 0.15, 0.35, 0.4, 0.5, 0.9], knots, values)
    assert fitted.tolist() == pytest.approx([0.5, 0.5, 0.5, 2 / 3, 1.0, 1.0])
    p, y = _distorted(3000, lambda q: q**2, seed=3)
    cal = cp.fit_policy(p, y)
    assert cal.tier == "isotonic"
    grid = cal.apply(np.linspace(0, 1, 101))
    assert np.all(np.diff(grid) >= -1e-12)


def test_blend_picks_the_brier_optimal_lambda_on_the_grid():
    y = [1, 0, 1, 0]
    assert cp.fit_blend([1, 0, 1, 0], [0.5] * 4, y) == 1.0  # the perfect forecast wins
    assert cp.fit_blend([0, 1, 0, 1], [0.5] * 4, y) == 0.0  # the anti-forecast loses
    assert cp.fit_blend([0.5] * 4, [0.5] * 4, y) == 0.0  # ties go to the reference
    assert cp.log_loss([0.5, 0.5], [1, 0]) == pytest.approx(math.log(2))


# --------------------------------------------------------------------------- IPO calibration walk-forward
def _ipo_rows(seed: int = 5):
    rng = random.Random(seed)
    rows = []
    for year in range(2016, 2023):
        for i in range(30):
            qib = math.exp(rng.uniform(-1, 5))
            z = 1.2 * (math.log1p(qib) - 2.5) + rng.gauss(0, 1)
            gain = rng.random() < 1 / (1 + math.exp(-z))
            rows.append({"series": "EQ", "listing_date": date(year, 1 + i % 12, 10).isoformat(), "qib_times": qib,
                         "nii_times": qib * 0.8, "retail_times": math.sqrt(qib), "total_times": qib * 0.9,
                         "public_book_cr": rng.uniform(100, 3000), "ofs_share": rng.choice([None, 0.3, 0.6]),
                         "nifty_ret20_close": rng.gauss(0, 0.03), "ipo_count_90d": rng.randint(2, 20),
                         "post_2022": year >= 2022, "return_open": (0.1 if gain else -0.05)})  # fmt: skip
    return rows


def test_oof_forecasts_use_only_earlier_years_and_live_parity_blanks_two_features():
    rows = _ipo_rows()
    oof = ic.oof_forecasts(rows)
    assert sorted({o.year for o in oof}) == list(range(2017, 2023))  # 2016 has no training data
    assert all(0 < o.p_model < 1 and 0 < o.p_table < 1 for o in oof)
    live = ic.oof_forecasts(rows, live_parity=True)
    assert len(live) == len(oof) and any(a.p_model != b.p_model for a, b in zip(oof, live, strict=True))
    f = ic._blank_live_gaps({"ofs_share": 0.4, "nifty20": 0.02, "ln_qib": 1.0})
    assert f == {"ofs_share": None, "nifty20": None, "ln_qib": 1.0}


def test_evaluate_fits_on_prior_years_only_and_applies_the_bar():
    rows = _ipo_rows()
    oof = ic.oof_forecasts(rows)
    res = ic.evaluate(oof, years=(2019, 2020, 2021, 2022), gate_years=(2019, 2020, 2021, 2022))
    for f in res["folds"]:
        assert f["n_pool"] == sum(1 for o in oof if o.year < f["year"])
    assert res["pooled_2019_2025"]["n"] == sum(1 for o in oof if 2019 <= o.year <= 2022)
    # a pool under 30 falls back to the raw model
    early = ic.evaluate(oof, years=(2017,), gate_years=(2017,))
    assert early["folds"][0]["T"]["params"] == {"fallback": "pool < 30: uncalibrated"}
    assert early["folds"][0]["T"]["brier"] == pytest.approx(early["folds"][0]["raw"]["brier"])
    v = res["verdicts"]["S"]
    assert v["passes"] == (
        len(v["years_passed"]) >= 5 and v["pooled_brier_better"]
    )  # 4 years: can never pass
    assert v["passes"] is False
    out = ic.run(rows)
    assert out["decision"]["ship"] in (None, "T", "S", "P") and "RESULTS" not in out
    assert "## Decision" in ic.render(out, "synthetic")


# --------------------------------------------------------------------------- point-in-time universe
def _ch(d: str, kind: str, sym: str) -> su.Change:
    return su.Change(date.fromisoformat(d), kind, sym, "test")


def test_reconstruct_undoes_changes_and_checks_invariants():
    current = ["A", "B", "C"]
    changes = [_ch("2020-03-27", "exclude", "X"), _ch("2020-03-27", "include", "C"),
               _ch("2021-09-30", "exclude", "Y"), _ch("2021-09-30", "include", "B")]  # fmt: skip
    periods, problems = su.reconstruct(current, changes, allowed_sizes=(3,))
    assert problems == []
    assert su.members_on(periods, date(2019, 1, 31)) == {"A", "X", "Y"}
    assert su.members_on(periods, date(2020, 3, 27)) == {"A", "C", "Y"}  # effective on the date
    assert su.members_on(periods, date(2021, 9, 29)) == {"A", "C", "Y"}
    assert su.members_on(periods, date(2026, 1, 1)) == {"A", "B", "C"}
    bad = [_ch("2020-03-27", "include", "Z")]  # Z is not in the later set: a missed release
    _, problems = su.reconstruct(current, bad, allowed_sizes=(3,))
    assert any("included Z" in p for p in problems)


def test_committed_change_log_is_consistent_with_the_nse_list():
    periods, problems = su.reconstruct(su._current_list(), su.load_changes())
    assert problems == []
    assert len(su.members_on(periods, date(2014, 1, 31))) == 50
    assert "HDFC" in su.members_on(periods, date(2023, 6, 30)) and "HDFC" not in su.members_on(
        periods, date(2023, 7, 31)
    )
    assert "TATAMTRDVR" in su.members_on(periods, date(2016, 6, 30))  # the 51-stock period
    assert su.data_symbol("MCDOWELL-N") == "UNITDSPR" and su.data_symbol("INFY") == "INFY"


def test_ranks_average_ties_and_selectors():
    assert su.ranks({"a": 3.0, "b": 1.0, "c": 3.0}, descending=True) == {"a": 1.5, "c": 1.5, "b": 3.0}
    st = {f"S{i}": bt.State(above_trend=i % 2 == 0, mom=0.1 * i, vol=0.1 + 0.01 * i) for i in range(10)}
    assert su.select_b0(st, True) == ["S8", "S6"]  # k = 2, uptrend only, by mom/vol
    assert su.select_v1(st, False) == [] and su.select_v1(st, True) == ["S8", "S6"]
    assert su.select_v2(st, True) == ["S0", "S1"]
    # ensemble: S9 has the best momentum rank but the worst vol rank; the middle wins on the average
    ens = su.select_v3(st, True)
    assert len(ens) == 2 and "S9" not in ens


def test_deflated_sharpe_golden():
    nd = NormalDist()
    # single trial: SR0 = 0 and DSR = PSR(0) = Φ(SR√(n−1)/√(1 − γ3 SR + (γ4 − 1)/4 SR²))
    assert su.expected_max_sharpe(0.04, 1) == 0.0
    psr = su.probabilistic_sharpe(0.1, 0.0, 120, 0.0, 3.0)
    assert psr == pytest.approx(nd.cdf(0.1 * math.sqrt(119) / math.sqrt(1 + 0.5 * 0.01)))
    assert psr == pytest.approx(0.8617, abs=1e-4)
    # N = 4, V[SR] = 0.01: SR0 = 0.1·((1−γ)Φ⁻¹(0.75) + γΦ⁻¹(1 − 1/(4e)))
    g = su.EULER_GAMMA
    want = 0.1 * ((1 - g) * nd.inv_cdf(0.75) + g * nd.inv_cdf(1 - 1 / (4 * math.e)))
    assert su.expected_max_sharpe(0.01, 4) == pytest.approx(want)
    assert want == pytest.approx(0.1052, abs=1e-4)  # 0.1·(0.4228·0.6745 + 0.5772·1.3292)
    t_crit = su.T_CRIT
    assert t_crit == pytest.approx(2.394, abs=1e-3)
    d = su.deflated_sharpe([0.01, 0.02, -0.005, 0.015, 0.0, 0.01], [0.1, 0.3, 0.5, 0.2])
    assert 0 <= d["dsr"] <= 1 and d["n_trials"] == 4


def _synthetic_market(years: int = 3):
    start = date(2020, 1, 1)
    days = [
        start + timedelta(days=i)
        for i in range(int(years * 365.25))
        if (start + timedelta(days=i)).weekday() < 5
    ]
    universe = {}
    for k in range(12):
        drift = (k - 4.5) * 0.0004
        close = [
            100 * math.exp(drift * i) * (1 + 0.01 * ((i + k) % 2) * (1 + k / 10)) for i in range(len(days))
        ]
        universe[f"S{k:02d}"] = bt.Series(f"S{k:02d}", sg.adjust_for_actions(days, close, []))
    market = bt.Series(
        "M", sg.adjust_for_actions(days, [1000 * math.exp(0.0002 * i) for i in range(len(days))], [])
    )
    return universe, market


def test_run_pit_respects_membership_and_the_market_filter():
    universe, market = _synthetic_market()
    everyone = list(universe)
    only_ten = everyone[:10]
    r = su.run_pit(universe, market, lambda t: only_ten, su.select_b0)
    assert r["months"] and all(m["universe"] <= 10 for m in r["months"])
    assert all(set(m["held"]) <= set(only_ten) for m in r["months"])
    assert r["coverage"]["missing"] == {}
    r2 = su.run_pit(universe, market, lambda t: [*everyone, "GONE"], su.select_b0)
    assert r2["coverage"]["missing"]["GONE"] == r2["coverage"]["member_months"] // 13
    # a falling market: the trend filter holds cash every month
    down = bt.Series("D", sg.adjust_for_actions(market.adj.days, [1000 * math.exp(-0.0005 * i)
                                                                  for i in range(len(market.adj.days))], []))  # fmt: skip
    r3 = su.run_pit(universe, down, lambda t: everyone, su.select_v1)
    assert r3["months"] and all(m["held"] == [] and m["strategy"] == 0.0 for m in r3["months"])
    s = su.summarise(r["months"])
    assert s["months"] == len(r["months"]) and "halves" in s
    v = su.evaluate({"B0": {"stats": s, "months": r["months"]}, "V1": {"stats": su.summarise(r3["months"]),
                                                                        "months": r3["months"]}})  # fmt: skip
    assert v["V1"]["passes"] is False and v["B0"]["passes"] is False


def test_group_policy_counts_overlapping_stock_windows():
    stock = cp.group_policy("stock", 120)
    assert stock["tier"] == "base_rate" and stock["n_effective"] == 10 and stock["next_tier"] == {
        "tier": "shrink", "at_n": 600}  # fmt: skip
    ipo = cp.group_policy("ipo", 150)
    assert ipo["tier"] == "platt" and ipo["next_tier"] == {"tier": "isotonic", "at_n": 1000}
    assert cp.group_policy("fund", 5000)["next_tier"] is None


def test_committed_pit_results_match_the_signal_caveat():
    import json
    import pathlib

    res = json.loads((su.OUT_DIR / "results.json").read_text())
    assert res["passing"] == [] and res["membership"]["problems"] == []
    b0 = res["verdicts"]["B0"]
    assert round(b0["excess_cagr_vs_equal_weight"] * 100, 1) == -3.1 and round(b0["newey_west_t"], 2) == -0.97
    src = pathlib.Path(su.__file__).parents[1] / "signals" / "stock.py"
    text = src.read_text()
    assert "point-in-time NIFTY 50" in text and "by 3.1 pp a year" in text and "t −0.97" in text


def test_harvest_symbols_cover_every_member_and_the_market():
    syms = su.harvest_symbols()
    assert "NIFTYBEES" in syms and "HDFC" in syms and "UNITDSPR" in syms and "MCDOWELL-N" not in syms
    assert "TMPV" in syms and "TATAMOTORS" not in syms and len(syms) == len(set(syms)) == 88
