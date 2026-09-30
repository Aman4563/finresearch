"""Golden values for the IPO base rates and the numpy listing model (roadmap §D.1, items 2 and 14)."""

from __future__ import annotations

import math
import random

import numpy as np
import pytest

from finresearch.fincalc import ipo as fipo
from finresearch.fincalc import ipo_model as m


# --------------------------------------------------------------------------- Wilson, quantiles, bands
def test_wilson_interval_matches_the_textbook_values():
    lo, hi = fipo.wilson_interval(7, 10)  # roadmap §C.10: 7/10 gives about 40-89%
    assert round(lo, 4) == 0.3968 and round(hi, 4) == 0.8922
    lo, hi = fipo.wilson_interval(0, 10)  # never a zero-width interval at the boundary
    assert lo == 0.0 and round(hi, 4) == 0.2775
    assert fipo.wilson_interval(0, 0) is None
    with pytest.raises(ValueError):
        fipo.wilson_interval(11, 10)


def test_quantile_is_type_7_and_bands_are_half_open():
    assert fipo.quantile([1, 2, 3, 4], 0.25) == 1.75  # numpy default
    assert fipo.quantile([3, 1, 2], 0.5) == 2 and fipo.quantile([], 0.5) is None
    assert [fipo.band_of(x) for x in (0.99, 1.0, 9.99, 10.0, 49.9, 50.0, 100.0, 250.0)] == [
        "<1x", "1–10x", "1–10x", "10–50x", "10–50x", "50–100x", ">100x", ">100x"]  # fmt: skip
    assert fipo.smoothed_rate(0, 0) == 0.5 and fipo.smoothed_rate(3, 3) == 0.8


def test_base_rates_by_band_and_regime_hand_computed():
    rows = [
        {"qib_times": 0.5, "total_times": 0.8, "return_open": -0.10, "post_2022": True},
        {"qib_times": 0.7, "total_times": 1.2, "return_open": 0.02, "post_2022": True},
        {"qib_times": 120, "total_times": 60, "return_open": 0.40, "post_2022": True},
        {"qib_times": 150, "total_times": 80, "return_open": 0.20, "post_2022": True},
        {
            "qib_times": 200,
            "total_times": 90,
            "return_open": 0.0,
            "post_2022": True,
        },  # flat: neither gain nor loss
        {"qib_times": 20, "total_times": 15, "return_open": 0.05, "post_2022": False},
        {
            "qib_times": None,
            "total_times": 15,
            "return_open": 0.05,
            "post_2022": False,
        },  # no QIB: skipped by qib
    ]
    t = fipo.base_rates(rows)
    cell = {(c["band"], c["regime"]): c for c in t["cells"]}
    low = cell[("<1x", "post_2022")]
    assert low["n"] == 2 and low["p_loss"] == 0.5 and math.isclose(low["median"], -0.04)
    hot = cell[(">100x", "post_2022")]
    assert hot["n"] == 3 and hot["p_loss"] == 0 and math.isclose(hot["p_gain"], 2 / 3)
    assert (
        math.isclose(hot["median"], 0.20) and math.isclose(hot["q1"], 0.10) and math.isclose(hot["q3"], 0.30)
    )
    assert hot["p_loss_ci"] == [0.0, pytest.approx(0.5615, abs=1e-4)]  # Wilson 0/3
    assert cell[("10–50x", "pre_2022")]["n"] == 1 and cell[("1–10x", "post_2022")]["n"] == 0
    assert t["n"] == 6 and t["regime_totals"]["post_2022"]["n"] == 5
    assert fipo.base_rates(rows, by="total")["n"] == 7


# --------------------------------------------------------------------------- the numpy models
def test_logistic_recovers_closed_form_mle():
    y = np.array([1, 0, 1, 1, 0, 1, 1, 0, 0, 1.0])
    b = m.fit_logistic(np.zeros((10, 0)), y, l2=0)
    assert b[0] == pytest.approx(math.log(0.6 / 0.4), abs=1e-9)  # intercept-only: logit(ȳ)
    x = np.array([[0], [0], [0], [0], [1], [1], [1], [1], [1], [1.0]])
    y = np.array([1, 0, 0, 0, 1, 1, 1, 1, 0, 1.0])  # group rates 1/4 and 5/6
    b = m.fit_logistic(x, y, l2=0)
    assert b[0] == pytest.approx(math.log(1 / 3), abs=1e-8)
    assert b[1] == pytest.approx(math.log(5) - math.log(1 / 3), abs=1e-7)
    shrunk = m.fit_logistic(x, y, l2=5.0)  # the penalty shrinks the slope, never the intercept's role
    assert 0 < shrunk[1] < b[1]
    p = m.predict_logistic(b, x)
    assert p[0] == pytest.approx(0.25) and p[-1] == pytest.approx(5 / 6)


def test_logistic_gradient_is_zero_at_the_penalised_optimum():
    rng = np.random.default_rng(7)
    z = rng.normal(size=(200, 3))
    y = (rng.random(200) < m.sigmoid(z @ np.array([1.0, -0.5, 0.0]))).astype(float)
    b = m.fit_logistic(z, y, l2=1.0)
    x = np.hstack([np.ones((200, 1)), z])
    grad = x.T @ (m.sigmoid(x @ b) - y) + np.r_[0.0, 1.0 * b[1:]]
    assert np.max(np.abs(grad)) < 1e-8


def test_quantile_regression_golden_values():
    v = np.array([3.0, 1, 4, 1, 5, 9, 2, 6, 5])
    assert m.fit_quantile(np.zeros((9, 0)), v, 0.5)[0] == pytest.approx(4.0, abs=1e-4)  # the sample median
    xs = np.linspace(0, 1, 50)[:, None]
    assert m.fit_quantile(xs, 2 + 3 * xs[:, 0], 0.5) == pytest.approx([2.0, 3.0], abs=1e-4)  # exact line
    # a shifted upper quantile: y = x + e, e in {0, 1} alternating → τ = 0.9 sits on the upper line
    x = np.repeat(np.arange(10.0), 2)[:, None]
    y = x[:, 0] + np.tile([0.0, 1.0], 10)
    assert m.fit_quantile(x, y, 0.9) == pytest.approx([1.0, 1.0], abs=1e-3)
    with pytest.raises(ValueError):
        m.fit_quantile(xs, xs[:, 0], 1.0)


def test_scores_brier_auc_pinball_reliability():
    p, o = [0.9, 0.2, 0.6, 0.4], [1, 0, 0, 1]
    assert m.brier(p, o) == pytest.approx((0.01 + 0.04 + 0.36 + 0.36) / 4)
    assert m.brier_skill(0.19, 0.25) == pytest.approx(0.24) and m.brier_skill(0.1, 0) is None
    assert m.auc([0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]) == 0.75  # the scikit-learn documentation example
    assert m.auc([0.5, 0.5], [0, 1]) == 0.5 and m.auc([0.3], [1]) is None
    assert m.pinball([1.0, 3.0], [2.0, 2.0], 0.9) == pytest.approx((0.1 + 0.9) / 2)
    rel = m.reliability([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], bins=2)
    assert rel == [{"n": 2, "mean_p": pytest.approx(0.15), "observed": 0.0},
                   {"n": 2, "mean_p": pytest.approx(0.85), "observed": 1.0}]  # fmt: skip


# --------------------------------------------------------------------------- walk-forward
def _synthetic(signal: bool, seed: int = 3) -> list[dict]:
    rnd = random.Random(seed)
    rows = []
    for year in range(2016, 2027):
        for i in range(40):
            qib = math.exp(rnd.uniform(-1, 5))
            ret = 0.12 * math.log1p(qib) - 0.3 + rnd.gauss(0, 0.08) if signal else rnd.gauss(0.05, 0.2)
            rows.append({"symbol": f"S{year}{i}", "series": "EQ", "listing_date": f"{year}-06-{(i % 28) + 1:02d}",
                         "qib_times": qib, "nii_times": qib * rnd.uniform(0.5, 2), "retail_times": rnd.uniform(1, 20),
                         "total_times": qib, "public_book_cr": rnd.uniform(100, 2000), "ofs_share": None,
                         "nifty_ret20_close": rnd.gauss(0, 0.03), "ipo_count_90d": rnd.randint(0, 30),
                         "post_2022": year >= 2022, "return_open": ret})  # fmt: skip
    return rows


def test_walk_forward_trains_only_on_past_years_and_applies_the_gate():
    from finresearch.evals import ipo_model as im

    rep = im.walk_forward(_synthetic(signal=True))
    folds = {f["year"]: f for f in rep["folds"]}
    assert sorted(folds) == list(range(2019, 2027))
    assert folds[2019]["n_train"] == 3 * 40 and folds[2026]["n_train"] == 10 * 40  # expanding window
    assert rep["gate"]["years_evaluated"] == list(range(2019, 2026))  # 2026 (partial) is reported, not gated
    # QIB drives the outcome smoothly within bands too: the model beats the band table out of sample
    assert rep["gate"]["passes"] and rep["final_model"]["n"] == 440
    assert rep["pooled"]["auc_model"] > 0.9 and folds[2023]["coverage_p10_p90_model"] > 0.5

    noise = im.walk_forward(_synthetic(signal=False))
    assert not noise["gate"]["passes"] and noise["final_model"] is None


def test_prediction_rearranges_crossing_quantiles_and_imputes_missing_ofs():
    from finresearch.evals import ipo_model as im

    rows = _synthetic(signal=True)
    model = im.fit(rows[:200])
    feats = [im.raw_features(r) for r in rows[200:210]]
    pred = im.predict(model, feats)
    assert np.all(pred["q10"] <= pred["q50"]) and np.all(pred["q50"] <= pred["q90"])
    assert np.all((pred["p"] > 0) & (pred["p"] < 1))
    c = im.contributions(model, feats[0])
    logit = model["logistic"][0] + sum(c.values())
    assert m.sigmoid(np.array([logit]))[0] == pytest.approx(pred["p"][0])
