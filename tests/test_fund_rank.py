"""Fund category rank and percentile (issue #177): hand-computed golden values for every metric on synthetic NAV
series, ties, short-history exclusion, the AMFI category folding, the universe filter, the stored result, the API,
the `category_rank_drop` alert metric and the monitor step. Nothing touches the network."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.amfi import SchemeNav, category_key, parse_nav_all
from finresearch.alerts import compute
from finresearch.alerts.compute import Reader
from finresearch.fincalc import fund_rank as R
from finresearch.signals import fund_rank as FR

AS_OF = date(2026, 9, 30)
GRID = R.month_grid(AS_OF, 60)


def points(monthly: list[float], months: int = 60, start: Decimal = Decimal(100)) -> list[R.Point]:
    """NAVs on GRID: `start` at grid[months], then multiplied by (1 + monthly[i]) each month up to grid[0] (the list
    is oldest first and must have `months` entries); None before grid[months]."""
    assert len(monthly) == months
    navs = [start]
    for g in monthly:
        navs.append(navs[-1] * (Decimal(1) + Decimal(str(g))))
    pts: list[R.Point] = [None] * 61
    for i, v in enumerate(navs):  # navs[0] sits at grid[months], navs[-1] at grid[0]
        pts[months - i] = (GRID[months - i], v)
    return pts


def const(g: float, months: int = 60) -> list[R.Point]:
    return points([g] * months, months)


# --------------------------------------------------------------------------- dates
def test_month_end_grid_moves_weekends_back_and_steps_one_month():
    assert R.month_end(2023, 9) == date(2023, 9, 29)  # 30-Sep-2023 was a Saturday
    assert R.month_end(2026, 2) == date(2026, 2, 27)  # 28-Feb-2026 is a Saturday
    assert R.month_end(2026, 12) == date(2026, 12, 31)
    assert R.latest_month_end(date(2026, 9, 30)) == date(2026, 9, 30)
    assert R.latest_month_end(date(2026, 9, 29)) == date(2026, 8, 31)
    assert R.month_grid(AS_OF, 2) == [date(2026, 9, 30), date(2026, 8, 31), date(2026, 7, 31)]
    assert GRID[12] == date(2025, 9, 30) and GRID[36] == date(2023, 9, 29) and len(GRID) == 61
    # the ranking is as of the last month-end on or before yesterday: 1-Oct ranks as of 30-Sep, 30-Sep as of 31-Aug
    assert FR.as_of_for(date(2026, 10, 1)) == date(2026, 9, 30)
    assert FR.as_of_for(date(2026, 9, 30)) == date(2026, 8, 31)


# --------------------------------------------------------------------------- metrics, by hand
def test_cagr_is_point_to_point_between_the_same_grid_dates():
    pts: list[R.Point] = [None] * 61
    pts[0] = (GRID[0], Decimal(121))
    pts[12] = (GRID[12], Decimal(100))  # 30-Sep-2025 -> 30-Sep-2026: 365 days, so 1y = 121/100 - 1
    pts[36] = (GRID[36], Decimal("90.909"))
    assert R.cagr(pts, 1) == pytest.approx(0.21, abs=1e-9)
    # 29-Sep-2023 -> 30-Sep-2026 is 1,097 days (29-Feb-2024 included): (121 / 90.909) ** (365 / 1097) - 1
    # = 1.331 ** 0.33273 - 1 = 9.98 %
    assert R.cagr(pts, 3) == pytest.approx((121 / 90.909) ** (365 / 1097) - 1, rel=1e-9)
    assert R.cagr(pts, 3) == pytest.approx(0.0998, abs=1e-4)
    assert R.cagr(pts, 5) is None  # no NAV five years back: short history


def test_max_drawdown_over_month_ends_by_hand():
    # 100 rising to 120 by month 10, falling to 90 by month 20, recovering to 130: worst fall 90 / 120 - 1 = -25 %
    navs = [100 + 2 * i for i in range(11)] + [120 - 3 * i for i in range(1, 11)]
    navs += [90 + (40 * i) / 16 for i in range(1, 17)]
    assert len(navs) == 37 and navs[10] == 120 and navs[20] == 90 and navs[-1] == 130
    path = [(GRID[36 - i], Decimal(str(v))) for i, v in enumerate(navs)]
    assert R.drawdown(path) == pytest.approx(-0.25)


def test_sortino_monthly_against_a_stated_mar_by_hand():
    # months alternate +2 % and -1 %. MAR 6.5 % a year = 0.5417 % a month.
    # mean excess = 0.5 % - 0.5417 % = -0.04167 % a month -> x12 = -0.5 %
    # downside: 18 months of (-1 % - 0.5417 %) = -1.5417 %; DD = sqrt(18 x 0.015417^2 / 36) = 1.0901 % a month
    # -> x sqrt(12) = 3.7763 %; Sortino = -0.5 / 3.7763 = -0.1324
    pts = points([0.02, -0.01] * 18, 36)
    path = R.three_year_path(pts)
    value, no_down = R.sortino(path, Decimal("0.065"))
    assert not no_down and value == pytest.approx(-0.1324, abs=5e-4)
    # a fund earning 0.5 % every month (below the MAR every month): (r - m) x 12 / (|r - m| x sqrt 12) = -sqrt(12)
    value, _ = R.sortino(R.three_year_path(const(0.005)), Decimal("0.065"))
    assert value == pytest.approx(-math.sqrt(12), rel=1e-6)
    # 1 % every month never falls below the MAR: no downside, the ratio is unbounded
    assert R.sortino(R.three_year_path(const(0.01)), Decimal("0.065")) == (None, True)


def test_three_year_metrics_need_every_month_end():
    pts = const(0.01)
    pts[5] = None  # a month-end without a NAV (not priced)
    m, rolls = R.fund_metrics(R.RankInput("1", "Gappy Fund", pts), R.MAR_DEFAULT)
    assert rolls is None and m.values["sortino_3y"] is None and m.values["max_drawdown_3y"] is None
    assert "complete 3-year" in m.reasons["consistency_3y"]
    assert m.values["cagr_3y"] is not None  # point-to-point still works: both ends are there


# --------------------------------------------------------------------------- ranking, ties, exclusion
def _category():
    """Five funds with 5 years of steady monthly growth, one launched 20 months ago (the best grower)."""
    return [
        R.RankInput("1", "Slow Fund", const(0.005), ter=1.0),
        R.RankInput("2", "Mid Fund A", const(0.01), ter=0.5),
        R.RankInput("3", "Mid Fund B", const(0.01), ter=0.5),
        R.RankInput("4", "Good Fund", const(0.015), ter=None),
        R.RankInput("5", "Best Fund", const(0.02), ter=2.0),
        R.RankInput("6", "New Fund", points([0.03] * 20, 20), ter=0.3),
    ]


def test_rank_category_ranks_percentiles_ties_and_short_history_by_hand():
    res = R.rank_category(_category())
    by = {f["code"]: f for f in res["funds"]}
    assert res["size"] == 6
    # 1-year: 30-Sep-2025 -> 30-Sep-2026 is 365 days, so it is (1 + g) ** 12 - 1; the new fund has it too
    assert by["5"]["values"]["cagr_1y"] == pytest.approx(1.02**12 - 1, abs=1e-6)
    assert {c: by[c]["ranks"]["cagr_1y"] for c in by} == {"6": 1, "5": 2, "4": 3, "2": 4, "3": 4, "1": 6}
    # percentile = share beaten, ties half: the tied pair beat 1 of 6 and tie 2 -> (1 + 1) / 6
    assert by["2"]["percentiles"]["cagr_1y"] == by["3"]["percentiles"]["cagr_1y"] == round(2 / 6, 4)
    assert by["6"]["percentiles"]["cagr_1y"] == round(5.5 / 6, 4) and by["1"]["percentiles"][
        "cagr_1y"
    ] == round(0.5 / 6, 4)
    # 3- and 5-year: the new fund is excluded (short history), the other five are ranked
    assert res["counts"]["cagr_3y"] == 5 and by["6"]["ranks"]["cagr_3y"] is None
    assert "short history" in by["6"]["missing"]["cagr_3y"]
    assert res["counts"]["cagr_5y"] == 5 and by["5"]["ranks"]["cagr_5y"] == 1
    # consistency: in every window the median is the two 1 % funds; only 1.5 % and 2 % are strictly above it
    assert [by[c]["values"]["consistency_3y"] for c in "12345"] == [0.0, 0.0, 0.0, 1.0, 1.0]
    assert [by[c]["ranks"]["consistency_3y"] for c in "12345"] == [3, 3, 3, 1, 1]
    assert by["5"]["percentiles"]["consistency_3y"] == 0.8 and by["1"]["percentiles"]["consistency_3y"] == 0.3
    # Sortino: four funds never earn less than the MAR in a month (unbounded, tied first); 0.5 % a month = -sqrt 12
    assert [by[c]["no_downside"] for c in "12345"] == [False, True, True, True, True]
    assert [by[c]["ranks"]["sortino_3y"] for c in "12345"] == [5, 1, 1, 1, 1]
    assert by["1"]["values"]["sortino_3y"] == pytest.approx(-math.sqrt(12), abs=1e-4)
    assert (
        by["2"]["values"]["sortino_3y"] is None and by["2"]["percentiles"]["sortino_3y"] == 0.6
    )  # (1 + 4/2) / 5
    # steady growth never falls: every drawdown is 0 and all five tie
    assert {by[c]["ranks"]["max_drawdown_3y"] for c in "12345"} == {1}
    assert by["1"]["percentiles"]["max_drawdown_3y"] == 0.5
    # TER: lower is better; one fund has none
    assert {c: by[c]["ranks"]["ter"] for c in by} == {"6": 1, "2": 2, "3": 2, "1": 4, "5": 5, "4": None}
    assert by["2"]["percentiles"]["ter"] == 0.6 and res["counts"]["ter"] == 5


def test_a_metric_with_too_few_funds_is_not_ranked():
    res = R.rank_category(_category()[:4])  # 4 funds < MIN_RANKED
    assert res["ranked"]["cagr_1y"] is False and all(f["ranks"]["cagr_1y"] is None for f in res["funds"])
    assert all(f["values"]["consistency_3y"] is None for f in res["funds"])


# --------------------------------------------------------------------------- AMFI categories and universe
def test_category_key_folds_spelling_variants_but_keeps_different_names_apart():
    k = category_key
    assert k("Equity Scheme - Flexi Cap Fund") == k("Equity Schemes - Flexi Cap Fund") == "equity:flexi cap"
    assert k("Equity Scheme - ELSS") == k("Equity Schemes - ELSS- Tax Saver Fund") == "equity:elss"
    assert k("Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage") == k(
        "Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation"
    )
    assert k("Hybrid Scheme - Equity Savings") == k("Hybrid Schemes - Equity Savings Fund")
    assert k("Solution Oriented Schemes ** - Retirement Fund") == k(
        "Solution Oriented Scheme - Retirement Fund"
    )
    assert k("Children’s Fund - Childrens' Fund") == k("Solution Oriented Scheme - Children’s Fund")
    assert k("Debt Scheme - Liquid Fund") == k("Income/Debt Oriented Schemes - Liquid Fund") == "debt:liquid"
    assert k("Debt Scheme - Banking and PSU Fund") == k(
        "Income/Debt Oriented Schemes - Banking and PSU Debt Fund"
    )
    # names that differ are not assumed to be the same SEBI category
    assert k("Debt Scheme - Short Duration Fund") != k("Income/Debt Oriented Schemes - Short Term Fund")
    assert k("Equity Scheme - Sectoral/ Thematic") != k("Equity Schemes - Thematic Fund")
    assert k("Income") == "legacy:income" and k(None) is None


NAV_ALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

Example Mutual Fund
100001;INE000X01011;-;Example Flexi Cap Fund;Direct Plan;Growth;50.0;30-Sep-2026
100002;INE000X01029;-;Example Flexi Cap Fund;Regular Plan;Growth;45.0;30-Sep-2026
100003;INE000X01037;INE000X01045;Example Flexi Cap Fund;Direct Plan;IDCW;20.0;30-Sep-2026
100004;INE000X01052;-;Example Flexi Cap Fund;Direct Plan;Bonus Option;20.0;30-Sep-2026

Open Ended Schemes(Equity Schemes - Flexi Cap Fund)

Sample Mutual Fund
100005;INE000X01060;-;Sample Flexi Cap Fund;Direct Plan;Growth Option;30.0;30-Sep-2026
100006;INE000X01078;-;Sample Flexi Cap Fund (Segregated Portfolio 1);Direct Plan;Growth;0.1;30-Sep-2026
100007;INE000X01060;-;Sample Flexicap Fund;Direct Plan;Growth;30.0;30-Sep-2026

Open Ended Schemes(Debt Scheme - Credit Risk Fund)

Example Mutual Fund
100010;INE000X01102;-;Example Credit Risk Fund (No. of segregated portfolios- 3);Direct Plan;Growth;25.0;30-Sep-2026
100011;INE000X01110;-;Example Credit Risk Fund (Segregated - 06032020);Direct Plan;Growth;0.5;30-Sep-2026

Open Ended Schemes(Other Scheme - Index Funds)

Example Mutual Fund
100008;INE000X01086;-;Example Nifty 50 Index Fund;Direct Plan;Growth;15.0;30-Sep-2026

Close Ended Schemes(Equity Scheme - Flexi Cap Fund)

Example Mutual Fund
100009;INE000X01094;-;Example Flexi Cap Fund Series 1;Direct Plan;Growth;12.0;30-Sep-2026
"""


def test_universe_keeps_one_direct_growth_row_per_scheme_and_maps_its_other_plans():
    rows = parse_nav_all(NAV_ALL)
    assert {r.structure for r in rows} == {"open", "close"}
    picked, alias = FR.universe(rows)
    flexi = picked["equity:flexi cap"]  # both spellings in one category; the close-ended series left out
    assert sorted(x.code for x in flexi) == ["100001", "100005"]  # 100007 repeats 100005's growth ISIN
    # the regular plan and the IDCW option are ranked through their scheme's direct-growth row
    assert alias["100002"] == alias["100003"] == "100001" and alias["100007"] == "100005"
    assert "100004" not in {x.code for x in flexi} and "100006" not in {x.code for x in flexi}
    assert "100009" not in alias
    # the main scheme that mentions its segregated portfolios is ranked; a segregated portfolio's own row is not
    assert [x.code for x in picked["debt:credit risk"]] == ["100010"]
    assert (
        FR.unranked_reason("other:index funds", "open")
        and FR.unranked_reason("equity:flexi cap", "open") is None
    )
    assert "sectoral" in FR.unranked_reason("equity:sectoral/ thematic", "open")


# --------------------------------------------------------------------------- compute, store, lookup
@pytest.fixture
def fake_amfi(monkeypatch):
    """Ten flexi-cap funds across the two AMFI spellings with steady monthly growth (fund i: 0.3 % + 0.1 % x i a
    month), a regular plan, an index fund, and one fund launched after the as-of month-end."""
    cat_a, cat_b = "Equity Scheme - Flexi Cap Fund", "Equity Schemes - Flexi Cap Fund"
    rows, growth = [], {}
    for i in range(10):
        code = str(200000 + i)
        growth[code] = 0.003 + 0.001 * i
        rows.append(SchemeNav(code, f"Fund {i} Flexi Cap Fund", "Direct Plan", "Growth", f"INE000X0{i:04d}", None,
                              Decimal(10), date(2026, 9, 30), cat_a if i % 2 else cat_b, "Example Mutual Fund",
                              "open"))  # fmt: skip
    rows.append(SchemeNav("200100", "Fund 3 Flexi Cap Fund", "Regular Plan", "Growth", "INE000X09999", None,
                          Decimal(9), date(2026, 9, 30), cat_a, "Example Mutual Fund", "open"))  # fmt: skip
    rows.append(SchemeNav("200200", "Late Flexi Cap Fund", "Direct Plan", "Growth", "INE000X08888", None,
                          Decimal(10), date(2026, 10, 1), cat_b, "Example Mutual Fund", "open"))  # fmt: skip
    rows.append(SchemeNav("200300", "Example Nifty 50 Index Fund", "Direct Plan", "Growth", "INE000X07777", None,
                          Decimal(10), date(2026, 9, 30), "Other Scheme - Index Funds", "Example Mutual Fund",
                          "open"))  # fmt: skip
    asked: list[date] = []

    async def schemes():
        return rows

    async def snapshot(anchor: date):
        asked.append(anchor)
        k = GRID.index(anchor)
        return {c: (anchor, Decimal(100) * Decimal(str((1 + g) ** (60 - k)))) for c, g in growth.items()}

    async def ter(month: date):
        raise RuntimeError("AMFI HTTP 503")

    monkeypatch.setattr(FR, "SOURCES", FR.RankSources(schemes=schemes, snapshot=snapshot, ter=ter,
                                                      today=lambda: date(2026, 10, 1)))  # fmt: skip
    return asked


async def test_compute_ranks_from_month_end_snapshots_and_records_exclusions(fake_amfi):
    res = await FR.compute()
    assert res["as_of"] == "2026-09-30" and len(fake_amfi) == 61 and fake_amfi[0] == AS_OF
    cat = res["categories"]["equity:flexi cap"]
    assert cat["size"] == 10 and set(cat["raw_labels"]) == {"Equity Scheme - Flexi Cap Fund",
                                                            "Equity Schemes - Flexi Cap Fund"}  # fmt: skip
    assert cat["excluded"] == [{"code": "200200", "name": "Late Flexi Cap Fund",
                                "reason": "no NAV at the as-of month-end (launched after it)"}]  # fmt: skip
    best = next(f for f in cat["funds"] if f["code"] == "200009")
    assert best["ranks"]["cagr_3y"] == 1 and best["percentiles"]["cagr_3y"] == 0.95  # (9 + 0.5) / 10
    assert res["ter_error"].startswith("AMFI TER file unavailable") and cat["counts"]["ter"] == 0
    assert "other:index funds" in res["unranked"] and "survivor" in " ".join(res["caveats"]).lower()

    FR.save(res)
    data = FR.load()
    hit = FR.lookup(data, "200100")  # the regular plan of fund 3, ranked through its direct plan
    assert hit["status"] == "ok" and hit["via"] == "200003" and hit["fund"]["code"] == "200003"
    s = FR.summary(data, "200003", "cagr_3y")
    assert (s["rank"], s["of"], s["label"], s["as_of"]) == (7, 10, "Flexi Cap Fund", "2026-09-30")
    assert FR.lookup(data, "200300")["status"] == "not_ranked"
    assert FR.lookup(data, "200200")["status"] == "excluded"
    assert FR.lookup(data, "999999")["status"] == "unknown"


async def test_empty_early_month_ter_file_falls_back_to_the_previous_month(monkeypatch):
    """Regression: on 1-Oct-2026 AMFI's October TER file downloaded fine but held no rows; the fund signal then
    read every fund's TER as missing instead of falling back to September's file."""
    from finresearch.adapters import amfi
    from finresearch.signals import fund

    months: list[date] = []

    async def ter(self, month):
        months.append(month)
        return {} if month.month == 10 else {"x": "september row"}

    monkeypatch.setattr(amfi.AmfiClient, "ter", ter)
    monkeypatch.setattr(fund, "SOURCES", fund.FundSources())
    assert await fund._ter(date(2026, 10, 1)) == {"x": "september row"}
    assert months == [date(2026, 10, 1), date(2026, 9, 30)]


# --------------------------------------------------------------------------- API
def test_category_rank_api_reads_the_stored_ranking_only(env, fake_amfi):
    import asyncio

    from finresearch.api import create_app

    with TestClient(create_app()) as c:
        r = c.get("/api/funds/200003/category-rank").json()
        assert r["status"] == "not_computed" and "finresearch fund rank" in r["message"]
        assert (
            c.get("/api/funds/category-ranks", params={"codes": "200003"}).json()["status"] == "not_computed"
        )

        FR.save(asyncio.run(FR.compute()))
        fake_amfi.clear()
        r = c.get("/api/funds/200100/category-rank").json()
        assert r["status"] == "ok" and r["via"] == "200003" and r["as_of"] == "2026-09-30"
        assert r["category"]["size"] == 10 and len(r["category"]["funds"]) == 10 and r["mar"] == 0.065
        assert r["fund"]["ranks"]["cagr_1y"] == 7 and r["caveats"]
        b = c.get("/api/funds/category-ranks", params={"codes": "200009,200300", "metric": "cagr_1y"}).json()
        assert b["ranks"]["200009"]["rank"] == 1 and b["ranks"]["200009"]["of"] == 10
        assert b["ranks"]["200300"]["status"] == "not_ranked" and b["ranks"]["200300"]["rank"] is None
        assert fake_amfi == []  # a page load never asks AMFI for anything
        assert c.get("/api/funds/category-ranks", params={"codes": "1", "metric": "alpha"}).status_code == 422
        assert c.get("/api/funds/category-ranks", params={"codes": "1;drop"}).status_code == 422
        assert c.get("/api/funds/abc/category-rank").status_code == 422


# --------------------------------------------------------------------------- alert metric
def _store(percentile: float, rank: int) -> dict:
    return {"as_of": "2026-09-30", "index": {"300001": ["equity:flexi cap", "300001"]},
            "labels": {"equity:flexi cap": "Flexi Cap Fund"},
            "categories": {"equity:flexi cap": {"label": "Flexi Cap Fund", "size": 34,
                                                "counts": {"cagr_3y": 34},
                                                "funds": [{"code": "300001", "ranks": {"cagr_3y": rank},
                                                           "percentiles": {"cagr_3y": percentile},
                                                           "values": {"cagr_3y": 0.15}, "missing": {}}],
                                                "excluded": []}}}  # fmt: skip


def test_category_rank_drop_records_a_baseline_then_reports_percentile_points_lost(monkeypatch):
    import asyncio

    from finresearch.alerts.registry import spec

    m = spec("fund", "category_rank_drop")
    assert m is not None and m.rebase == "on_fire" and m.unit == "pp"
    store = {"data": _store(0.9, 4)}

    async def ranks():
        return store["data"]

    monkeypatch.setattr(compute, "SOURCES", compute.Sources(fund_ranks=ranks))
    first = asyncio.run(Reader(None).read("fund", "category_rank_drop", "300001", {}, {}))
    assert first.value == 0 and first.baseline["percentile"] == "90.0" and first.baseline["rank"] == 4
    store["data"] = _store(0.4, 21)
    later = asyncio.run(Reader(None).read("fund", "category_rank_drop", "300001", {}, first.baseline))
    assert later.value == Decimal("50.00") and later.detail == "4/34 -> 21/34"
    bad = asyncio.run(Reader(None).read("fund", "category_rank_drop", "300001", {"basis": "alpha"}, {}))
    assert bad.value is None and "unknown ranking basis" in bad.note
    store["data"] = None
    assert asyncio.run(Reader(None).read("fund", "category_rank_drop", "300001", {}, {})).value is None


def test_registry_no_longer_says_fund_rank_has_no_source():
    from finresearch.alerts import registry

    assert "no ranking source" not in registry.__doc__ and "category_rank_drop" in registry.__doc__


# --------------------------------------------------------------------------- monitor
def test_monitor_ranks_once_a_day_after_seven(env, monkeypatch):
    import asyncio

    from finresearch.monitor import fund_ranks

    calls: list[date] = []

    async def refresh(today=None):
        calls.append(today)
        return {"as_of": "2026-09-30", "categories": 1, "funds": 10}

    monkeypatch.setattr(FR, "refresh", refresh)
    early = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)  # 06:30 IST
    assert fund_ranks.due_slot(early) is None and asyncio.run(fund_ranks.fund_ranks_step(early)) == {}
    later = datetime(2026, 10, 1, 2, 0, tzinfo=UTC)  # 07:30 IST
    assert asyncio.run(fund_ranks.fund_ranks_step(later))["fund_ranks"]["funds"] == 10
    assert asyncio.run(fund_ranks.fund_ranks_step(later)) == {}  # claimed: once a day
    assert calls == [date(2026, 10, 1)]


def test_peers_endpoint_groups_amfi_spelling_variants_and_drops_close_ended(env):
    """Regression: /api/funds/{code}/peers matched the raw heading, so a fund under "Equity Scheme - Flexi Cap Fund"
    never saw the peers under "Equity Schemes - Flexi Cap Fund", and a close-ended series with the same heading
    counted as a peer."""
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources

    rows = parse_nav_all(NAV_ALL)

    async def nav_all():
        return rows

    async def navs_on(day):
        return {r.code: SchemeNav(r.code, r.name, r.plan, r.option, None, None, r.nav / 2, day, r.category, r.amc)
                for r in rows if r.nav}  # fmt: skip

    app = create_app(nav_all=nav_all)
    app.state.markets = MarketSources(navs_on=navs_on, today=lambda: date(2026, 10, 1))
    with TestClient(app) as c:
        r = c.get("/api/funds/100001/peers").json()
    assert r["peers"] == 3  # 100001, 100005 and 100007 (two codes of one scheme: /peers does not dedupe)
