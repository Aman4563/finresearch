"""The stock factor diagnostics' study (#506) on generated panels with known answers: a planted rank IC is recovered
inside its interval at every horizon, planted decay is seen as decay, the top-quintile spread, turnover and break-even
cost match values derived by hand from the generator, a pure-noise panel detects nothing, ties and missing names are
handled, and what is unknown (too few names, months or blocks; a spread that is not positive) stays unknown with its
count. No price is read."""

import math

import factordiag_synth as fs
import numpy as np
import pytest

from finresearch.evals import stock_backtest as bt
from finresearch.factordiag import panel as pn
from finresearch.factordiag import report, specs, study

RHO = (0.15, 0.08, 0.08)  # the planted loading of month u's return on the score k = 1, 2, 3 months earlier
OTHERS = [c for c in specs.COMPONENTS if c != "mom_score"]
PER_SIDE = (bt.COST_BUY + bt.COST_SELL) / 2


def _planted(seed: int, *, months: int = 151, names: int = 40, scale: float = 0.05, rho=RHO) -> pn.Panel:
    """`mom_score` is iid N(0, 1) per month and name and predicts the next three months' returns with loadings rho;
    the other four components are noise. r_u = sum_k rho_k s_(u-k) + e_u, times `scale`."""
    rng = np.random.default_rng(seed)
    s = rng.normal(size=(months, names))
    monthly = rng.normal(size=(months, names))
    for k, r in enumerate(rho, start=1):
        monthly[k:] += r * s[:-k]
    scores = {"mom_score": s, **{c: rng.normal(size=(months, names)) for c in OTHERS}}
    return fs.array_panel(
        scores, monthly * scale, pn.WINDOWS, fs.month_end_dates(months, 2014)
    )  # Jan-2014 on


def _pearson(a: int, b: int, rho=RHO) -> float:
    """corr(s_t, sum of r over months t+a+1 .. t+b) for the generator above: cov = the loadings inside the window;
    var of an m-month sum = m g0 + 2 sum_lag (m - lag) g_lag with g0 = 1 + sum rho^2 and g_lag = sum_k rho_k
    rho_(k+lag) (returns share a past score)."""
    m = b - a
    g0 = 1 + sum(r * r for r in rho)
    g = lambda lag: sum(rho[k] * rho[k + lag] for k in range(len(rho) - lag))  # noqa: E731
    var = m * g0 + 2 * sum((m - lag) * g(lag) for lag in range(1, min(m, len(rho))))
    return sum(rho[k - 1] for k in range(a + 1, b + 1) if k <= len(rho)) / math.sqrt(var)


def _spearman(pearson: float, n: int) -> float:
    """Moran (1948): E[r_s] = 6 / (pi (n + 1)) x (asin(rho) + (n - 2) asin(rho / 2)) for n bivariate-normal pairs."""
    return 6 / (math.pi * (n + 1)) * (math.asin(pearson) + (n - 2) * math.asin(pearson / 2))


def _bh(ps: dict, m: int) -> dict:
    """Benjamini-Hochberg written out the slow way, as the check of the family's q-values."""
    known = sorted((p, k) for k, p in ps.items() if p is not None)
    return {k: min(1.0, min(m * pj / j for j, (pj, _) in enumerate(known, 1) if j >= i))
            for i, (_, k) in enumerate(known, 1)}  # fmt: skip


def test_the_generator_correlations_worked_by_hand() -> None:
    # g0 = 1 + 0.0225 + 0.0064 + 0.0064 = 1.0353; g1 = 0.15 x 0.08 + 0.08 x 0.08 = 0.0184; g2 = 0.15 x 0.08 = 0.012
    assert _pearson(0, 1) == pytest.approx(0.15 / math.sqrt(1.0353))
    assert _pearson(1, 3) == pytest.approx(0.16 / math.sqrt(2 * 1.0353 + 2 * 0.0184))
    assert _pearson(0, 3) == pytest.approx(0.31 / math.sqrt(3 * 1.0353 + 2 * (2 * 0.0184 + 0.012)))
    assert _pearson(0, 12) == pytest.approx(0.31 / math.sqrt(12 * 1.0353 + 2 * (11 * 0.0184 + 10 * 0.012)))
    assert _pearson(3, 6) == 0.0 and _pearson(6, 12) == 0.0


@pytest.fixture(scope="module")
def planted() -> dict:
    return study.run(_planted(506))


def test_the_planted_ic_is_recovered_inside_its_interval_at_every_horizon(planted: dict) -> None:
    cells = planted["components"]["mom_score"]["horizons"]
    for h in specs.HORIZONS:
        c, want = cells[str(h)], _spearman(_pearson(0, h), 40)
        assert c["months"] == 151 - h and c["names"] == {"min": 40, "median": 40, "max": 40}
        assert c["interval"]["lo"] < want < c["interval"]["hi"], (h, want, c["interval"])
        assert c["mean"] == pytest.approx(want, abs=0.04) and c["share_positive"] > 0.6
        assert c["icir"] == pytest.approx(c["mean"] / c["sd"]) and c["newey_west"]["lag"] == max(6, 2 * h)
        assert len(c["series"]) == c["months"] and c["series"][0][0] == "2014-01-31"
    # the cumulative IC peaks at three months (the last planted one) and is then diluted by months of pure noise
    assert cells["3"]["mean"] > cells["6"]["mean"] > cells["12"]["mean"] > 0


def test_planted_decay_is_seen_as_decay(planted: dict) -> None:
    decay = planted["components"]["mom_score"]["decay"]
    first, second, third, last = (decay[k] for k in ("0-1", "1-3", "3-6", "6-12"))
    assert first["interval"]["lo"] < _spearman(_pearson(0, 1), 40) < first["interval"]["hi"]
    assert second["interval"]["lo"] < _spearman(_pearson(1, 3), 40) < second["interval"]["hi"]
    assert (
        first["mean"] > second["mean"] > 0.05 and first["interval"]["lo"] > 0 and second["interval"]["lo"] > 0
    )
    for gone in (third, last):  # nothing was planted beyond three months
        assert gone["interval"]["lo"] < 0 < gone["interval"]["hi"] and abs(gone["mean"]) < 0.04
    assert (first["months"], second["months"], third["months"], last["months"]) == (150, 148, 145, 139)
    assert (
        last["interval"]["block"] == specs.BLOCK_MONTHS[12] and second["newey_west"]["lag"] == specs.NW_LAG[3]
    )


def test_family_a_q_values_are_bh_over_exactly_twenty_cells(planted: dict) -> None:
    comps = planted["components"]
    ps = {
        (n, h): comps[n]["horizons"][str(h)]["sign_flip"]["p"]
        for n in specs.COMPONENTS
        for h in specs.HORIZONS
    }
    fam = planted["families"]["A"]
    assert len(ps) == 20 and (fam["size"], fam["known"], fam["fdr"]) == (20, 20, 0.10)
    want = _bh(ps, 20)
    for (n, h), q in want.items():
        cell = comps[n]["horizons"][str(h)]
        assert cell["q"] == pytest.approx(q) and cell["detected"] is (q <= 0.10)
    assert sorted(map(tuple, planted["families"]["A"]["detected"])) == sorted(
        k for k, q in want.items() if q <= 0.10
    )
    mom = comps["mom_score"]["horizons"]
    assert mom["1"]["detected"] and mom["3"]["detected"] and mom["1"]["sign_flip"]["p"] < 0.001
    # 139 months at 12 months ahead: five blocks of 24 and a remainder of 19 make six, so p >= 2 / 64 whatever the IC
    assert mom["12"]["sign_flip"]["blocks"] == 6 and mom["12"]["sign_flip"]["min_p"] == 2 / 64
    assert mom["12"]["sign_flip"]["p"] == 2 / 64 and mom["12"]["detected"] is False  # the registered floor
    assert not any(
        c["detected"] for n in OTHERS for c in comps[n]["horizons"].values()
    )  # the noise components


# --------------------------------------------------------------------------------------------- quintiles and costs
def _top_fifth_mean(n: int = 40, draws: int = 40000) -> float:
    """E[mean of the top n/5 of n standard normals], by simulation of the order statistics alone."""
    z = np.sort(np.random.default_rng(99).normal(size=(draws, n)), axis=1)
    return float(z[:, -n // 5 :].mean())


def test_quintile_spread_turnover_and_break_even_match_the_generator(planted: dict) -> None:
    c = planted["components"]["mom_score"]["horizons"]["1"]
    q, top = c["quintiles"], _top_fifth_mean()
    assert top == pytest.approx(1.36, abs=0.02)  # near phi(0.8416) / 0.2 = 1.40, less in a sample of 40
    # next month's return = 0.05 x (0.15 s + noise): the top fifth by s beats the cross-section by 0.05 x 0.15 x top
    want = 0.05 * RHO[0] * top
    spread = q["top_minus_universe"]
    assert spread["interval"]["lo"] < want < spread["interval"]["hi"] and spread["mean"] == pytest.approx(
        want, rel=0.2
    )
    both = q["top_minus_bottom"]  # symmetric: the bottom fifth trails by as much
    assert both["interval"]["lo"] < 2 * want < both["interval"]["hi"] and "icir" not in both
    assert q["mean_return"] == sorted(q["mean_return"]) and q["monotonicity"] == {"rho": 1.0, "steps_up": 4}
    assert q["months"] == 150 and q["unknown_months"] == {} and abs(q["universe_mean"]) < 0.005
    # iid scores: a name of the top 8 at the later date was in the top 8 before with probability 8 / 40
    for h in specs.HORIZONS:
        t = planted["components"]["mom_score"]["horizons"][str(h)]["turnover"]
        assert t["top"] == pytest.approx(0.80, abs=0.03) and t["bottom"] == pytest.approx(0.80, abs=0.03)
        assert t["pairs"] == 151 - h  # every pair of month-ends h apart, a return is not needed
    be = c["break_even"]
    assert be["long_only"]["cost"] == pytest.approx(spread["mean"] / (2 * c["turnover"]["top"]))
    assert be["long_short"]["cost"] == pytest.approx(
        both["mean"] / (2 * (c["turnover"]["top"] + c["turnover"]["bottom"]))
    )
    assert be["long_only"]["cost"] == pytest.approx(want / 1.6, rel=0.25)  # about 0.64 % a side
    assert planted["costs"]["per_side"] == pytest.approx(PER_SIDE)
    assert pytest.approx(0.001611, abs=1e-6) == PER_SIDE  # (0.168605 % + 0.153605 %) / 2, the harness's costs


def test_the_registered_reading_of_a_planted_short_lived_skill(planted: dict) -> None:
    notes = planted["components"]["mom_score"]["reading"]
    by = {(n["label"], n["horizon"]) for n in notes}
    # the spread is far above the harness's cost per side: the diagnostic and a portfolio test would disagree
    assert ("disagrees", 1) in by and ("disagrees", 3) in by and ("short_lived", None) in by
    assert all(n["text"] == specs.READING[n["label"]] for n in notes)
    assert "none_detected" not in {n["label"] for n in notes}
    for other in OTHERS:
        assert [n["label"] for n in planted["components"][other]["reading"]] == ["none_detected"]
    assert planted["reading_always"] == specs.READING["always"]


def test_the_same_skill_at_a_fiftieth_of_the_return_scale_is_eaten_by_costs() -> None:
    """Rank ICs do not change with the scale of returns, but the spread does: 0.002 x 0.15 x 1.36 = 0.04 % a month
    against a break-even of spread / 1.6 = 0.03 % a side, below the harness's 0.16 %."""
    res = study.run(_planted(506, scale=0.002))
    c = res["components"]["mom_score"]["horizons"]["1"]
    assert c["detected"] and 0 < c["break_even"]["long_only"]["cost"] < PER_SIDE
    assert ("eaten_by_costs", 1) in {
        (n["label"], n["horizon"]) for n in res["components"]["mom_score"]["reading"]
    }


def test_turnover_by_hand_on_scores_that_rotate_one_name_a_month() -> None:
    """20 names, score of name i at month t = (i + t) mod 20: the top quintile (4 names) loses one name and gains
    one each month, so its one-way turnover is 1/4 at one month and 3/4 at three; same for the bottom quintile."""
    months, n = 30, 20
    s = np.array([[(i + t) % n for i in range(n)] for t in range(months)], dtype=float)
    panel = fs.array_panel(dict.fromkeys(specs.COMPONENTS, s), np.zeros((months, n)), pn.WINDOWS)
    assert study.turnover(panel, "mom_score", 1) == {"top": 0.25, "bottom": 0.25, "pairs": 29}
    assert study.turnover(panel, "mom_score", 3) == {"top": 0.75, "bottom": 0.75, "pairs": 27}
    assert study.turnover(panel, "mom_score", 6)["top"] == 1.0  # six steps on, none of the four is left


def test_break_even_is_unknown_never_zero_when_the_spread_is_not_positive() -> None:
    assert study.break_even(0.004, 0.5) == {"cost": pytest.approx(0.004), "reason": None}  # 0.4 % / (2 x 0.5)
    for spread, turn, word in ((-0.001, 0.5, "not positive"), (0.0, 0.5, "not positive"), (0.004, 0.0, "turnover is 0"),
                               (None, 0.5, "unknown"), (0.004, None, "unknown")):  # fmt: skip
        out = study.break_even(spread, turn)
        assert out["cost"] is None and word in out["reason"], (spread, turn)


# --------------------------------------------------------------------------------------------- a noise panel
def test_a_pure_noise_panel_detects_nothing_and_says_so() -> None:
    res = study.run(_planted(7, rho=(0.0, 0.0, 0.0)))
    assert res["families"]["A"]["detected"] == [] and res["families"]["B"]["detected"] == []
    assert res["families"]["A"]["known"] == 20 and res["families"]["B"] == {"size": 10, "known": 10, "fdr": 0.10,
                                                                            "detected": []}  # fmt: skip
    for comp in res["components"].values():
        assert [n["label"] for n in comp["reading"]] == ["none_detected"]
        for c in comp["horizons"].values():
            # about (2.2 + 0.9) x 0.16 / sqrt(150) = 0.04 for 40 names: the panel could not have seen an IC of 0.01
            assert c["interval"]["mde"] > 0.01
    text = report.render(res)
    assert "no detectable ranking skill on this panel" in text and "0 detected" in text
    assert "is never called an edge" in text and "Does not justify saying the IC is zero" in text


# --------------------------------------------------------------------------------------------- the two periods
def test_a_planted_difference_between_the_periods_is_detected_by_family_b() -> None:
    """The score predicts next month's return (loading 0.2) only for decision dates before 30-Apr-2020."""
    rng = np.random.default_rng(21)
    months, names = 151, 40
    dates = fs.month_end_dates(months, 2014)
    split = next(i for i, d in enumerate(dates) if d >= specs.SPLIT)
    s, monthly = rng.normal(size=(months, names)), rng.normal(size=(months, names))
    monthly[1 : split + 1] += 0.2 * s[:split]
    scores = {"mom_score": s, **{c: rng.normal(size=(months, names)) for c in OTHERS}}
    res = study.run(fs.array_panel(scores, monthly * 0.05, pn.WINDOWS, dates))
    c = res["components"]["mom_score"]["horizons"]["1"]
    first, second, diff = c["sub_periods"]["first"], c["sub_periods"]["second"], c["period_difference"]
    assert split == 75 and (first["months"], second["months"]) == (
        75,
        75,
    )  # Jan-2014..Mar-2020, Apr-2020..Jun-2026
    want = _spearman(0.2 / math.sqrt(1.04), 40)
    assert (
        first["interval"]["lo"] < want < first["interval"]["hi"]
        and second["interval"]["lo"] < 0 < second["interval"]["hi"]
    )
    assert diff["blocks"] == [6, 6] and diff["min_p"] == pytest.approx(2 / 924) and diff["exact"] is True
    assert (
        diff["difference"] == pytest.approx(want, abs=0.06) and diff["p"] < 0.01 and diff["detected"] is True
    )
    comps = res["components"]
    ps = {(n, h): comps[n]["horizons"][str(h)]["period_difference"]["p"]
          for n in specs.COMPONENTS for h in specs.FAMILY_B_HORIZONS}  # fmt: skip
    assert len(ps) == 10 and res["families"]["B"]["known"] == 10
    for (n, h), q in _bh(ps, 10).items():
        assert comps[n]["horizons"][str(h)]["period_difference"]["q"] == pytest.approx(q)
    assert ("period_difference", 1) in {(n["label"], n["horizon"]) for n in comps["mom_score"]["reading"]}
    assert all(
        "period_difference" not in comps[n]["horizons"][h] for n in specs.COMPONENTS for h in ("6", "12")
    )
    assert comps["mom_score"]["horizons"]["12"]["sub_periods"]["first"]["months"] == 75  # still described


# --------------------------------------------------------------------------------------------- ties, missing, unknown
def _spearman_slow(x: list[float], y: list[float]) -> float:
    """Spearman written out: average ranks by sorting, then Pearson's formula."""

    def rank(v: list[float]) -> list[float]:
        order, out, i = sorted(range(len(v)), key=lambda k: v[k]), [0.0] * len(v), 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    a, b = rank(x), rank(y)
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((p - ma) * (q - mb) for p, q in zip(a, b, strict=True))
    return cov / math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - mb) ** 2 for q in b))


def test_ties_and_missing_names_are_handled() -> None:
    rng = np.random.default_rng(13)
    months, names = 60, 30
    s = rng.choice([-60.0, 0.0, 60.0], size=(months, names))  # a clipped score: three values, heavy ties
    monthly = rng.normal(size=(months, names)) + 0.3 * np.vstack([np.zeros(names), s[:-1] / 60])
    s[5, :5] = np.nan  # five names have no score at month 5
    monthly[6, 25:] = np.nan  # five others have no return over month 6, so 20 names are left at month 5
    s[10] = 60.0  # every name tied: that month's IC is undefined
    s[12, :11] = np.nan  # 19 names: below the registered 20
    panel = fs.array_panel(dict.fromkeys(specs.COMPONENTS, s), monthly, pn.WINDOWS)
    x = study.ic_months(panel, "composite_price", (0, 1))
    assert x["unknown_months"] == {"constant": 1, "too_few_names": 1} and len(x["ic"]) == 59 - 2
    k = x["dates"].index(panel.months[5].t)
    assert x["names"][k] == 20 and min(x["names"]) == 20 and max(x["names"]) == 30
    both = fs.names(30)[5:25]
    want = _spearman_slow([panel.months[5].scores["composite_price"][n] for n in both],
                          [panel.months[5].returns[0, 1][n] for n in both])  # fmt: skip
    assert x["ic"][k] == pytest.approx(want, abs=1e-12)
    assert float(np.mean(x["ic"])) > 0.1  # the planted link survives the ties
    # three score values over 30 names: the tied groups land in quintiles 1, 3 and 5 at best, so 2 and 4 are empty
    q = study.quintile_stats(panel, "composite_price", 1)
    assert (
        q["months"] == 0
        and q["unknown_months"]["empty_quintile"] == 58
        and q["unknown_months"]["too_few_names"] == 1
    )
    assert (
        q["mean_return"] is None
        and q["top_minus_universe"] is None
        and "only 0 evaluated months" in q["unknown"]
    )
    assert study.break_even(None, 0.5)["cost"] is None


def test_too_few_months_or_blocks_is_unknown_with_its_count_and_still_counts_in_the_family() -> None:
    res = study.run(_planted(506, months=60))  # Jan-2014 to Dec-2018: every decision date is before the split
    comps, fam = res["components"], res["families"]
    c12, c6, c3 = (comps["mom_score"]["horizons"][h] for h in ("12", "6", "3"))
    # 48 months at 12 ahead make 2 blocks of 24; 54 at 6 ahead make 3 of 18: both below the 5 a p needs
    assert (c12["months"], c12["sign_flip"]["blocks"], c6["months"], c6["sign_flip"]["blocks"]) == (
        48,
        2,
        54,
        3,
    )
    for c in (c12, c6):
        assert (
            c["sign_flip"]["p"] is None and c["q"] is None and c["detected"] is None and c["mean"] is not None
        )
    assert c12["interval"]["lo"] is None and c12["interval"]["mde"] is None  # two blocks: no interval either
    assert c6["interval"]["lo"] is not None  # three blocks are enough for the interval
    assert (
        c3["sign_flip"]["blocks"] == 5 and c3["q"] is not None
    )  # 57 months: 4 blocks of 12 and a remainder of 9
    assert (fam["A"]["size"], fam["A"]["known"]) == (20, 10) and (fam["B"]["size"], fam["B"]["known"]) == (
        10,
        0,
    )
    ps = {
        (n, h): comps[n]["horizons"][str(h)]["sign_flip"]["p"]
        for n in specs.COMPONENTS
        for h in specs.HORIZONS
    }
    for (n, h), q in _bh(ps, 20).items():  # the ten known p-values are still corrected for twenty tests
        assert comps[n]["horizons"][str(h)]["q"] == pytest.approx(q)
    labels = {(n["label"], n["horizon"]) for n in comps["mom_score"]["reading"]}
    assert ("unknown", 12) in labels and ("unknown", 6) in labels
    second = c3["sub_periods"]["second"]
    assert (
        second["months"] == 0
        and second["mean"] is None
        and second["unknown"] == "only 0 evaluated months (needs 36)"
    )
    assert c3["period_difference"]["p"] is None and c3["period_difference"]["q"] is None
    short = study.run(_planted(506, months=40))["components"]["mom_score"]["horizons"]["12"]
    assert (
        short["unknown"] == "only 28 evaluated months (needs 36)"
        and short["mean"] is None
        and short["sd"] is None
    )
    assert short["months"] == 28 and short["newey_west"]["t"] is None and short["sign_flip"]["p"] is None
    text = report.render(res)
    assert "unknown" in text and "nan" not in text.lower() and "None" not in text


# --------------------------------------------------------------------------------------------- every registered reading
def _cell(q, *, mean=0.05, lo=None, cost=None, unknown=None, blocks=13, diff=None) -> dict:
    return {"q": q, "detected": None if q is None else q <= specs.FDR, "mean": mean, "unknown": unknown,
            "sign_flip": {"blocks": blocks},
            "quintiles": {"top_minus_universe": None if lo is None else {"interval": {"lo": lo}}},
            "break_even": {"long_only": {"cost": cost, "reason": "the turnover is unknown"}},
            **({"period_difference": {"detected": diff}} if diff is not None else {})}  # fmt: skip


def _labels(cells: dict[int, dict], tail=(-0.02, 0.03)) -> list[tuple[str, int | None]]:
    comp = {"horizons": {str(h): c for h, c in cells.items()},
            "decay": {"6-12": {"interval": {"lo": tail[0], "hi": tail[1]}}}}  # fmt: skip
    notes = study.reading(comp, PER_SIDE)
    assert all(n["text"] == specs.READING[n["label"]] for n in notes)
    return [(n["label"], n["horizon"]) for n in notes]


def test_each_outcome_gets_its_registered_reading_and_no_other() -> None:
    quiet = {h: _cell(0.5) for h in specs.HORIZONS}

    def one(
        cell: dict,
    ) -> list[tuple[str, int | None]]:  # detected at one month; the 6-12 month stretch is not empty
        return _labels({**quiet, 1: cell}, tail=(0.01, 0.05))

    assert _labels(quiet) == [("none_detected", None)]
    assert one(_cell(0.04, mean=-0.03)) == [("detected_negative", 1)]
    # detected positive: what the long-only spread and the break-even cost say decides the label
    assert one(_cell(0.04, lo=-0.001, cost=0.01)) == [("not_captured", 1)]
    assert one(_cell(0.04, lo=0.0, cost=0.01)) == [
        ("not_captured", 1)
    ]  # an interval touching 0 is not "above 0"
    assert one(_cell(0.04, lo=0.002, cost=PER_SIDE - 1e-6)) == [("eaten_by_costs", 1)]
    assert one(_cell(0.04, lo=0.002, cost=PER_SIDE)) == [("disagrees", 1)]  # "at or above the harness's"
    assert one(_cell(0.04, lo=None)) == [("unknown", 1)]  # no spread interval: no reading
    assert one(_cell(0.04, lo=0.002, cost=None)) == [("unknown", 1)]  # no break-even cost: none either
    assert one(_cell(specs.FDR, lo=-0.001)) == [("not_captured", 1)]  # q = 0.10 is detected
    assert one(_cell(0.1001, lo=-0.001)) == [("none_detected", None)]


def test_short_lived_needs_a_detection_at_one_or_three_months_a_known_miss_at_twelve_and_an_empty_tail() -> (
    None
):
    hit = _cell(0.04, lo=0.002, cost=0.01)
    base = {1: _cell(0.5), 3: hit, 6: _cell(0.5), 12: _cell(0.3)}
    assert _labels(base) == [("disagrees", 3), ("short_lived", None)]
    assert _labels(base, tail=(0.01, 0.05)) == [("disagrees", 3)]  # the 6-12 month stretch is not empty
    assert _labels({**base, 12: _cell(None, unknown="only 20 evaluated months (needs 36)")}) == [
        ("unknown", 12), ("disagrees", 3)]  # fmt: skip
    assert _labels({**base, 12: hit}) == [
        ("disagrees", 3),
        ("disagrees", 12),
    ]  # detected at 12: not short-lived
    assert _labels({**base, 3: _cell(0.5), 6: hit}) == [("disagrees", 6)]  # six months is not "1 or 3"
    assert _labels(base, tail=(None, None)) == [("disagrees", 3)]  # an unknown tail interval settles nothing
    # the registered condition names no sign: a wrong-way ranking seen only at short horizons gets both notes
    assert _labels({**base, 3: _cell(0.04, mean=-0.03)}) == [("detected_negative", 3), ("short_lived", None)]


def test_unknown_cells_and_period_differences_in_the_reading() -> None:
    blank = _cell(None, unknown="only 12 evaluated months (needs 36)")
    assert _labels(dict.fromkeys(specs.HORIZONS, blank)) == [("unknown", h) for h in specs.HORIZONS]
    few_blocks = {
        "horizons": {"12": _cell(None, blocks=2)},
        "decay": {"6-12": {"interval": {"lo": None, "hi": None}}},
    }
    note = study.reading(few_blocks, PER_SIDE)[0]
    assert (note["label"], note["horizon"], note["why"]) == ("unknown", 12, "2 blocks: no p")
    cells = {1: _cell(0.5, diff=True), 3: _cell(0.5, diff=False), 6: _cell(0.5), 12: _cell(0.5)}
    assert _labels(cells) == [("none_detected", None), ("period_difference", 1)]
