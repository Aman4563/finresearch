"""CPI inflation for the goal planner (#347): chaining MoSPI's 2012-base index into the 2024-base series by the
official year-on-year rates, trailing / long-run rates and the year-to-year spread. Synthetic numbers only."""

from __future__ import annotations

import math
from datetime import date

import pytest

from finresearch.wealth import inflation as inf


def synthetic_old(first: int = 2013, last: int = 2025, growth: float = 0.05) -> dict[date, float]:
    """A made-up 2012-base index growing exactly `growth` a year (monthly compounding)."""
    return {
        date(y, m, 1): 100 * (1 + growth) ** ((y - first) + (m - 1) / 12)
        for y in range(first, last + 1)
        for m in range(1, 13)
    }


def synthetic_new_yoy(months: int = 9, pct: float = 3.0) -> dict[date, float]:
    return {date(2026, m, 1): pct for m in range(1, months + 1)}


def test_chain_extends_old_index_by_official_yoy():
    old = synthetic_old()
    lv = inf.chain(old, synthetic_new_yoy())
    # Sep-2026 = Sep-2025 x 1.03 (the 2024-base series' own year-on-year rate), no base-linking factor needed
    assert lv[date(2026, 9, 1)] == pytest.approx(old[date(2025, 9, 1)] * 1.03, rel=1e-12)
    assert max(lv) == date(2026, 9, 1)


def test_chain_stops_at_a_gap_in_the_new_series():
    yoy = synthetic_new_yoy()
    del yoy[date(2026, 5, 1)]
    lv = inf.chain(synthetic_old(), yoy)
    assert max(lv) == date(2026, 4, 1)  # never skips a month (a gap would silently stretch the window)


def test_stats_trailing_long_run_and_spread_hand_computed():
    lv = inf.chain(synthetic_old(), synthetic_new_yoy())
    st = inf.stats(lv, years=10)
    assert st.as_of == date(2026, 9, 1)
    assert st.trailing_pct == pytest.approx(3.0, abs=1e-9)
    # Sep-2016 -> Sep-2026: nine years at 5 % then one at 3 %
    assert st.long_run_pct == pytest.approx(((1.05**9 * 1.03) ** 0.1 - 1) * 100, abs=1e-9)
    # annual readings 5 x9 and 3: mean 4.8, squares 9 x 0.04 + 3.24 = 3.6, sample variance 3.6 / 9 = 0.4
    assert st.sd_pct == pytest.approx(math.sqrt(0.4), abs=1e-9)
    assert st.years == 10 and st.start == date(2016, 9, 1)


def test_stats_too_short_history_is_unknown_with_reason():
    lv = inf.chain(synthetic_old(first=2022), synthetic_new_yoy())
    st = inf.stats(lv, years=10)
    assert st.long_run_pct is None and st.sd_pct is None
    assert "10 years" in st.why
    assert st.trailing_pct == pytest.approx(3.0, abs=1e-9)  # the part that is known stays known


def test_stats_empty_is_unknown():
    st = inf.stats({}, years=10)
    assert st.as_of is None and st.trailing_pct is None and st.long_run_pct is None and st.why


# --------------------------------------------------------------------------- inflation paths in the Monte Carlo
from finresearch.wealth import goals  # noqa: E402

KW = dict(start=500_000.0, sip0=15_000.0, step_up_pct=5.0, months=120, target_today=3_000_000.0,
          inflation_pct=6.0, equity_pct=None, gold_pct=10.0, assumptions=goals.DEFAULT_ASSUMPTIONS, n=2000,
          seed=11)  # fmt: skip


def test_growth_from_draws_compounds_whole_and_part_years_hand_computed():
    import numpy as np

    g = goals.growth_from_draws(np.array([[0.10, 0.20], [0.0, -0.10]]), months=18)
    assert g == pytest.approx([1.10 * 1.20**0.5, 1.0 * 0.90**0.5], rel=1e-12)


def test_zero_spread_keeps_the_fixed_inflation_plan_exactly():
    a, b = goals.plan(**KW), goals.plan(**KW, inflation_sd_pct=0.0)
    assert a.to_json() == b.to_json()
    assert b.target_nominal == pytest.approx(3_000_000 * 1.06**10)


def test_inflation_spread_reuses_the_return_draws_and_spreads_the_target():
    a, b = goals.plan(**KW), goals.plan(**KW, inflation_sd_pct=1.5)
    assert b.terminal_pcts == a.terminal_pcts  # returns drawn from their own stream: common random numbers
    assert b.target_nominal == pytest.approx(a.target_nominal)  # the central target is the long-run rate's
    lo, mid, hi = (b.target_pcts[k] for k in ("p10", "p50", "p90"))
    assert lo < mid < hi and lo < a.target_nominal < hi
    # mean target ~ central: E[prod(1 + pi)] = (1 + mu)^10 for independent years; ~1.5 % sd of 2,000 paths
    assert b.target_mean == pytest.approx(a.target_nominal, rel=0.01)
    assert b.inflation_sd_pct == 1.5


def test_inflation_sensitivity_rows_match_fixed_rate_runs():
    b = goals.plan(**KW, inflation_sd_pct=1.5, sensitivity=[("CPI trailing 12 months", 3.0), ("+2 pp", 8.0)])
    rows = {r["label"]: r for r in b.inflation_sensitivity}
    for label, rate in (("CPI trailing 12 months", 3.0), ("+2 pp", 8.0)):
        fixed = goals.plan(**(KW | {"inflation_pct": rate}), extras=False)
        assert rows[label]["inflation_pct"] == rate
        assert rows[label]["p_success"] == pytest.approx(fixed.p_success)
    assert rows["+2 pp"]["p_success"] <= rows["CPI trailing 12 months"]["p_success"]


# --------------------------------------------------------------------------- the goal's inflation basis
from datetime import datetime  # noqa: E402

from finresearch.adapters.mospi_cpi import CpiData  # noqa: E402


def cpi_data(first: int = 2013) -> CpiData:
    """General grows 5 %/yr then 3 % in 2026; education 8 %/yr then 6 %; health 6 %/yr then 4 %."""
    old = {"general": synthetic_old(first, growth=0.05), "education": synthetic_old(first, growth=0.08),
           "health": synthetic_old(first, growth=0.06)}  # fmt: skip
    new = {"general": synthetic_new_yoy(pct=3.0), "education": synthetic_new_yoy(pct=6.0),
           "health": synthetic_new_yoy(pct=4.0)}  # fmt: skip
    return CpiData(old=old, new_yoy=new, old_last=date(2025, 12, 1), new_last=date(2026, 9, 1),
                   imputed=[date(2020, 4, 1)], fetched_at=datetime(2026, 10, 22, 9, 0),
                   urls=["https://api.mospi.gov.in/api/cpi/getCPIIndex?x"])  # fmt: skip


def test_manual_basis_is_labelled_your_assumption_with_no_spread():
    r = inf.resolve("manual", 7.0, None, None)
    assert r["known"] and r["mean_pct"] == 7.0 and r["sd_pct"] == 0.0
    assert "your assumption" in r["label"]
    assert [x[1] for x in r["sensitivity"]] == [5.0, 7.0, 9.0]


def test_cpi_education_uses_its_long_run_rate_spread_and_premium():
    r = inf.resolve("cpi_education", 7.0, inf.summarize(cpi_data()), None)
    lr = ((1.08**9 * 1.06) ** 0.1 - 1) * 100  # Sep-2016..Sep-2026: nine years at 8 %, one at 6 %
    gen = ((1.05**9 * 1.03) ** 0.1 - 1) * 100
    assert r["known"] and r["mean_pct"] == pytest.approx(lr, abs=1e-9)
    assert r["sd_pct"] == pytest.approx(math.sqrt(0.4), abs=1e-9)  # same 2-pp drop in one year of ten
    assert r["trailing_pct"] == pytest.approx(6.0, abs=1e-9)
    assert r["premium_pct"] == pytest.approx(lr - gen, abs=1e-9)  # over general CPI, both long-run
    assert r["as_of"] == "2026-09-01" and r["window_start"] == "2016-09-01"
    assert r["vintage"]["base2012_last"] == "2025-12-01" and r["vintage"]["base2024_last"] == "2026-09-01"
    assert r["vintage"]["imputed"] == ["2020-04-01"] and r["vintage"]["fetched_at"].startswith("2026-10-22")
    assert "MoSPI" in r["label"] and "education" in r["label"].lower()
    labels = [x[0] for x in r["sensitivity"]]
    assert any("trailing" in x for x in labels) and any("general" in x.lower() for x in labels)


def test_cpi_basis_without_cpi_is_unknown_and_the_fallback_is_labelled():
    r = inf.resolve("cpi_general", 6.5, None, "MoSPI CPI API HTTP 503")
    assert r["known"] is False and r["value_pct"] is None
    assert r["reason"] == "MoSPI CPI API HTTP 503"
    assert r["mean_pct"] == 6.5 and r["sd_pct"] == 0.0  # the goal's own fixed rate, never a silent default
    assert "unknown" in r["label"] and "6.5" in r["label"] and "503" in r["label"]


def test_cpi_basis_with_too_short_history_is_unknown_with_reason():
    r = inf.resolve("cpi_health", 6.0, inf.summarize(cpi_data(first=2022)), None)
    assert r["known"] is False and "10 years" in r["reason"]
    assert r["trailing_pct"] == pytest.approx(4.0, abs=1e-9)  # what is known is still shown


def test_unknown_basis_is_refused():
    with pytest.raises(ValueError):
        inf.resolve("cpi_food", 6.0, None, None)


# --------------------------------------------------------------------------- /api/wealth/goals/{id}/plan
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
GOAL = {"name": "Child's college", "target_inr": "2500000", "target_date": "2036-06-30", "inflation_pct": "7",
        "current_inr": "400000", "monthly_sip": "12000", "step_up_pct": "5"}  # fmt: skip


@pytest.fixture
def api(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE wealth_goal, portfolio_setting CASCADE"))
    app = create_app()
    app.state.wealth_today = date(2026, 10, 22)
    state = {"calls": 0, "fail": None}

    async def source():
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError(state["fail"])
        return cpi_data()

    app.state.cpi_source = source
    with TestClient(app) as c:
        yield c, state


def _goal(c, **kw):
    r = c.post("/api/wealth/goals", json=GOAL | kw, headers=ORIGIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_api_manual_goal_never_fetches_cpi_and_says_your_assumption(api):
    c, state = api
    g = _goal(c)
    assert g["inflation_basis"] == "manual"  # existing and API-created goals keep their fixed rate
    p = c.get(f"/api/wealth/goals/{g['id']}/plan?n=500").json()
    assert state["calls"] == 0
    assert p["inflation"]["basis"] == "manual" and "your assumption" in p["inflation"]["label"]
    assert p["target_nominal"] == pytest.approx(2_500_000 * 1.07 ** (p["months"] / 12))
    assert [r["inflation_pct"] for r in p["inflation_sensitivity"]] == [5.0, 7.0, 9.0]


def test_api_cpi_education_goal_uses_mospi_rates_and_records_the_vintage(api):
    c, state = api
    g = _goal(c, inflation_basis="cpi_education")
    p = c.get(f"/api/wealth/goals/{g['id']}/plan?n=500").json()
    lr = ((1.08**9 * 1.06) ** 0.1 - 1) * 100
    inf_ = p["inflation"]
    assert inf_["known"] and inf_["mean_pct"] == pytest.approx(lr, abs=1e-9)
    assert p["inflation_pct"] == pytest.approx(lr, abs=1e-9) and p["inflation_sd_pct"] > 0
    assert p["target_nominal"] == pytest.approx(2_500_000 * (1 + lr / 100) ** (p["months"] / 12))
    assert p["target_pcts"]["p10"] < p["target_pcts"]["p90"]
    assert inf_["vintage"]["base2024_last"] == "2026-09-01" and inf_["as_of"] == "2026-09-01"
    assert len(p["inflation_sensitivity"]) == 4  # trailing, average, +2 pp, general CPI
    c.get(f"/api/wealth/goals/{g['id']}/plan?n=500")
    assert state["calls"] == 1  # the series is kept in memory between plans


def test_api_cpi_failure_is_unknown_with_reason_and_not_cached(api):
    c, state = api
    state["fail"] = "MoSPI CPI API HTTP 503"
    g = _goal(c, inflation_basis="cpi_general", inflation_pct="6.5")
    p = c.get(f"/api/wealth/goals/{g['id']}/plan?n=500").json()
    assert p["inflation"]["known"] is False and p["inflation"]["value_pct"] is None
    assert "503" in p["inflation"]["reason"] and "unknown" in p["inflation"]["label"]
    assert p["target_nominal"] == pytest.approx(2_500_000 * 1.065 ** (p["months"] / 12))  # labelled fallback
    assert p["inflation_sd_pct"] == 0.0
    state["fail"] = None
    p2 = c.get(f"/api/wealth/goals/{g['id']}/plan?n=500").json()
    assert p2["inflation"]["known"] is True and state["calls"] == 2


def test_api_rejects_an_unknown_basis(api):
    c, _ = api
    r = c.post("/api/wealth/goals", json=GOAL | {"inflation_basis": "cpi_food"}, headers=ORIGIN)
    assert r.status_code == 422
