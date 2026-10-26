"""The IPO ranges' pinball loss and 80 % coverage per walk-forward year, with issue #378's acceptance (d)
(skill-monitor-v2). Literal values worked by hand; synthetic issues only (made-up symbols, no ipo_history row)."""

from __future__ import annotations

import math
import random

import pytest

from finresearch.skillmon import ipo_intervals as ii

YEARS = range(2019, 2026)


@pytest.mark.parametrize(("inside", "n", "ok"), [(73, 100, True), (72, 100, False), (87, 100, True),
                                                 (88, 100, False), (12, 15, True), (0, 0, False)])  # fmt: skip
def test_within_seven_points_is_exact_integer_arithmetic(inside: int, n: int, ok: bool) -> None:
    assert ii.within(inside, n) is ok  # 0.8 - 0.73 = 0.07000000000000006 in floats: integers decide


def test_pinball_and_coverage_of_one_year_by_hand() -> None:
    y = [-0.1, 0.05, 0.1, 0.2, 0.3]
    r = ii.year_scores({"q10": [0.0] * 5, "q50": [0.1] * 5, "q90": [0.2] * 5}, y)
    # tau 0.1 at q 0: (0.09 + 0.005 + 0.01 + 0.02 + 0.03) / 5; tau 0.5 at q 0.1: (0.1 + 0.025 + 0 + 0.05 + 0.1) / 5;
    # tau 0.9 at q 0.2: (0.03 + 0.015 + 0.01 + 0 + 0.09) / 5
    assert r["pinball"] == pytest.approx({"q10": 0.031, "q50": 0.055, "q90": 0.029})
    assert r["pinball_mean"] == pytest.approx(0.115 / 3)
    assert r["n"] == 5 and r["inside"] == 3 and r["coverage"] == pytest.approx(0.6) and r["within"] is False
    assert r["p"] == pytest.approx(
        0.00032 + 0.0064 + 0.0512 + 0.2048
    )  # Bin(5, 0.8): outcomes no likelier than 3


def _year(within: bool | None) -> dict | None:
    return None if within is None else {"n": 50, "within": within}


@pytest.mark.parametrize(("marks", "verdict"), [
    ([True] * 5 + [False] * 2, "pass"),
    ([True] * 4 + [None] + [False] * 2, "insufficient data"),  # 4 within + 1 unknown could still reach 5
    ([True] * 4 + [False] * 3, "fail"),
    ([True] * 3 + [None] + [False] * 3, "fail"),  # even if the unknown year were within: 4 < 5
])  # fmt: skip
def test_acceptance_d_is_five_of_seven_complete_years(marks: list, verdict: str) -> None:
    years = {y: _year(m) for y, m in zip(YEARS, marks, strict=True)}
    years[2026] = _year(True)  # the partial year is reported, never counted
    r = ii.acceptance(years)
    assert r["verdict"] == verdict and r["within"] == sum(m is True for m in marks)
    assert set(r["years"]) == set(YEARS)


def _issues(seed: int = 7, first: int = 2016) -> list[dict]:
    """Made-up mainboard issues whose open return rises with the QIB book, 40 a year."""
    rnd = random.Random(seed)
    rows = []
    for year in range(first, 2027):
        for i in range(40):
            qib = math.exp(rnd.uniform(-1, 5))
            rows.append({"symbol": f"FAKE{year}{i}", "series": "EQ", "listing_date": f"{year}-07-{i % 28 + 1:02d}",
                         "qib_times": qib, "nii_times": qib * rnd.uniform(0.5, 2), "retail_times": rnd.uniform(1, 20),
                         "total_times": qib, "public_book_cr": rnd.uniform(100, 2000), "ofs_share": None,
                         "nifty_ret20_close": rnd.gauss(0, 0.03), "ipo_count_90d": rnd.randint(0, 30),
                         "post_2022": year >= 2022, "return_open": 0.1 * math.log1p(qib) - 0.25 + rnd.gauss(0, 0.1)})  # fmt: skip
    return rows


def test_the_years_are_the_walk_forwards_own_folds() -> None:
    """Each year's model range is the one evals.ipo_model.walk_forward scores (same split and fits)."""
    from finresearch.evals import ipo_model as im

    rows = _issues()
    rep, wf = ii.evaluate(rows), {f["year"]: f for f in im.walk_forward(rows)["folds"]}
    for f in rep["years"]:
        w = wf[f["year"]]
        assert f["model"]["coverage"] == pytest.approx(w["coverage_p10_p90_model"])
        assert f["model"]["pinball"] == pytest.approx(w["pinball_model"])
        assert f["table"]["pinball"] == pytest.approx(w["pinball_table"])
    assert [f["year"] for f in rep["years"]] == list(range(2019, 2027)) and rep["n_rows"] == 440


def test_a_year_the_walk_forward_skips_is_insufficient_data() -> None:
    rows = [
        r
        for r in _issues(first=2018)
        if not r["listing_date"].startswith("2018") or r["symbol"] < "FAKE201810"
    ]
    rep = ii.evaluate(rows)  # 2 issues before 2019 (FAKE20180, FAKE20181): fewer than 30 training rows
    first = rep["years"][0]
    assert first["year"] == 2019 and first["skipped"] and "model" not in first
    for name in ("model", "table"):
        assert rep["acceptance"][name]["years"][2019] == "insufficient data"
