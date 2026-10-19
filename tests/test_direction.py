"""The F&O direction model (#327) on synthetic data: no look-ahead, walk-forward refits, metrics against hand values,
the acceptance rule, the decision layer's EV maths and its NO TRADE gate. No database, no network, no real prices."""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import date

import direction_synth as ds
import pytest

from finresearch.direction import decide, evaluate, features, metrics, models, prereg, specs
from finresearch.direction.features import DayRow


@pytest.fixture
def registered(monkeypatch):
    """The out-of-sample evaluator requires the registration hash; tests evaluate the code's own spec."""
    monkeypatch.setattr(prereg, "HASH", specs.registration_hash())


# --------------------------------------------------------------------------- the registration
def test_registered_hash_matches_the_spec_and_a_changed_spec_is_refused(monkeypatch):
    assert specs.registration_hash() == prereg.HASH, (
        "specs.py changed after registration: add a new variant instead"
    )
    monkeypatch.setattr(specs, "K", 0.75)
    with pytest.raises(evaluate.PreregistrationError):
        evaluate.require_registered()


# --------------------------------------------------------------------------- features and targets
def test_features_match_hand_values():
    rows = ds.panel(date(2023, 1, 2), date(2023, 12, 29), seed=5)
    i = 230
    f = features.features_at(rows, i)
    rets = [r.ret for r in rows]
    assert f["ret_5"] == pytest.approx(sum(rets[i - 4 : i + 1]))
    levels = [r.close for r in rows]
    assert f["dist_ma20"] == pytest.approx(
        (levels[i] / (sum(levels[i - 19 : i + 1]) / 20) - 1) * 100, rel=1e-9
    )
    assert f["dist_ma200"] == pytest.approx(
        (levels[i] / (sum(levels[i - 199 : i + 1]) / 200) - 1) * 100, rel=1e-9
    )
    w = rets[i - 19 : i + 1]
    m = sum(w) / 20
    assert f["rv_20"] == pytest.approx(math.sqrt(sum((x - m) ** 2 for x in w) / 19 * 252) * 100)
    assert f["pcr_oi"] == pytest.approx(rows[i].put_oi / rows[i].call_oi)
    assert f["pcr_chg_5"] == pytest.approx(
        rows[i].put_oi / rows[i].call_oi - rows[i - 5].put_oi / rows[i - 5].call_oi
    )
    ivs = [r.iv_30 for r in rows[max(0, i - 251) : i + 1]]
    assert f["iv_rank"] == pytest.approx((rows[i].iv_30 - min(ivs)) / (max(ivs) - min(ivs)))
    assert f["iv_rv_spread"] == pytest.approx(rows[i].iv_30 - f["rv_20"])
    assert f["term_slope"] == pytest.approx(rows[i].next_iv - rows[i].near_iv)
    assert f["dte_near"] == (rows[i].near_expiry - rows[i].day).days
    dow = rows[i].day.weekday()
    assert [f["dow_mon"], f["dow_tue"], f["dow_wed"], f["dow_thu"]] == [float(dow == k) for k in range(4)]
    assert f["adx_14"] is not None and 0 <= f["adx_14"] <= 100
    assert f["fii_fut_net"] is None  # no participant data: missing, never 0
    # a missing return inside a window leaves it unknown, never filled
    rows[i - 2].ret = None
    g = features.features_at(rows, i)
    assert g["ret_5"] is None and g["rv_20"] is None and g["dist_ma20"] is None and g["ret_60"] is None


def test_participant_features_from_counts():
    r = DayRow(date(2026, 10, 16), participants={"FII": {"fut_idx_long": 30, "fut_idx_short": 70, "opt_idx_call_long": 10,
               "opt_idx_call_short": 20, "opt_idx_put_long": 40, "opt_idx_put_short": 30}})  # fmt: skip
    f = features.features_at([r], 0)
    assert f["fii_fut_net"] == pytest.approx(-0.4)
    assert f["fii_opt_net"] == pytest.approx((10 - 20 - 40 + 30) / 100)
    assert f["pro_fut_net"] is None


def test_no_look_ahead_guard_passes_and_catches_a_leak(monkeypatch):
    rows = ds.panel(date(2023, 1, 2), date(2023, 9, 29), seed=2)
    feats = features.build_features(
        rows, check_every=1
    )  # every row recomputed with the future replaced by junk
    assert len(feats) == len(rows)
    real = features.features_at

    def leaky(rs, i):
        out = real(rs, i)
        if i + 1 < len(rs):
            out["iv_30"] = rs[i + 1].iv_30  # tomorrow's IV: a look-ahead bug
        return out

    monkeypatch.setattr(features, "features_at", leaky)
    with pytest.raises(features.LookAheadError):
        features.build_features(rows, check_every=7)


def test_targets_use_the_next_h_returns_and_k_sigma():
    rows = [
        DayRow(date(2024, 1, 1 + d), ret=r, iv_30=16.0)
        for d, r in enumerate([None, 0.01, -0.004, 0.002, 0.03])
    ]
    assert features.forward_return(rows, 0, 3) == pytest.approx(0.008)
    assert features.forward_return(rows, 2, 3) is None  # beyond the data
    sig = 0.16 * math.sqrt(3 / 252)
    assert features.sigma_h(16.0, 3) == pytest.approx(sig)
    assert features.target(rows, 0, 3, "up") == 1
    assert 0.5 * sig > 0.008  # the 3-session return 0.008 is below the 0.5·σ threshold (0.00873)
    assert features.target(rows, 0, 3, "up_k") == 0 and features.target(rows, 0, 3, "down_k") == 0
    assert features.target(rows, 1, 3, "up_k") == 1  # −0.004 + 0.002 + 0.03 = 0.028 > 0.00873


# --------------------------------------------------------------------------- models
def test_implied_probabilities_hand_values():
    s, tau = 0.20, 5 / 252
    m = (specs.RATE - s * s / 2) * tau / (s * math.sqrt(tau))
    n = models.N01.cdf
    assert models.implied_p(20.0, 5, "up") == pytest.approx(n(m))
    assert models.implied_p(20.0, 5, "up_k") == pytest.approx(n(m - 0.5))
    assert models.implied_p(20.0, 5, "down_k") == pytest.approx(n(-m - 0.5))
    assert models.implied_p(None, 5, "up") is None and models.implied_p(0.0, 5, "up") is None


def test_base_rate_uses_the_trailing_window_only():
    assert models.base_rate_p([1] * 59) is None  # fewer than min_labels
    known = [0] * 1000 + [1] * 500  # the last 500 are all events
    assert models.base_rate_p(known) == 1.0
    assert models.base_rate_p([1, 0] * 40) == 0.5


def test_logistic_fit_is_the_penalised_optimum_and_finds_a_real_signal():
    import random

    rnd = random.Random(4)
    xs = [{"a": rnd.gauss(0, 1), "b": rnd.gauss(0, 1), "c": None if i % 3 else 1.0} for i in range(800)]
    y = [int(rnd.random() < 1 / (1 + math.exp(-(0.3 + 1.2 * x["a"])))) for x in xs]
    fit = models.fit_logit(xs, y, 1, names=["a", "b", "c"])
    assert fit.converged and "c" in fit.dropped  # missing on 2/3 of rows: left out
    coef = dict(zip(fit.names, fit.coef, strict=True))
    # the true slope is 1.2 per sd; the penalty (α = 0.5 against a curvature of about p(1 − p) ≈ 0.2) shrinks it ~3×
    assert 0.25 < coef["a"] < 1.2 and abs(coef["b"]) < 0.05
    # first-order condition of mean log loss + (α·h/2)‖w‖²: the gradient vanishes at the fit
    import numpy as np

    x, _used, *_ = models.design(xs, ["a", "b", "c"])
    a = np.hstack([np.ones((len(xs), 1)), x])
    w = np.array([fit.intercept, *fit.coef])
    p = 1 / (1 + np.exp(-(a @ w)))
    grad = a.T @ (p - np.array(y)) / len(xs) + np.array([0.0, *[specs.LOGIT.alpha * 1 * c for c in fit.coef]])
    assert np.max(np.abs(grad)) < 1e-8
    # the penalty grows with the horizon: the same data at h = 20 shrinks the slope
    assert (
        dict(zip(fit.names, models.fit_logit(xs, y, 20, names=["a", "b"]).coef, strict=True))["a"] < coef["a"]
    )


# --------------------------------------------------------------------------- walk-forward
def test_walk_forward_refits_monthly_on_known_outcomes_only(monkeypatch):
    rows = ds.panel(date(2022, 1, 3), date(2023, 12, 29), seed=8)
    feats = features.build_features(rows)
    seen = []
    real = models.fit_logit

    def spy(xs, y, h, *a, **kw):
        seen.append(len(xs))
        return real(xs, y, h, *a, **kw)

    monkeypatch.setattr(models, "fit_logit", spy)
    h = 10
    wf = evaluate.walk_forward(rows, feats, h, "up")
    points = evaluate.refit_points(rows)
    assert [
        rows[i].day.day <= 7 and (i == 0 or rows[i - 1].day.month != rows[i].day.month) for i in points
    ] == [True] * len(points)
    assert len(points) == 24 and len(wf.refits) == 24
    y = evaluate.labels(rows, h, "up")
    for r, i in zip(wf.refits, points, strict=True):
        tr = evaluate.train_set(y, rows, h, i)
        assert r["train_n"] == len(tr) and all(j + h <= i for j in tr)
    assert seen == [r["train_n"] for r in wf.refits if r["fitted"]]
    assert not wf.refits[0]["fitted"] and wf.refits[-1]["fitted"]  # min_train rows first
    # a day's logistic forecast is the fit of its month's first session: forecasts before the first fit are None
    first = next(k for k, r in enumerate(wf.refits) if r["fitted"])
    first_day = date.fromisoformat(wf.refits[first]["day"])
    assert all(p.p["logistic"] is None for p in wf.preds if p.day < first_day)
    assert all(p.p["logistic"] is not None for p in wf.preds if p.day >= first_day)


def test_future_rows_do_not_change_past_forecasts():
    rows = ds.panel(date(2022, 1, 3), date(2023, 12, 29), seed=9)
    cut = 400
    a = evaluate.walk_forward(rows, features.build_features(rows), 5, "up_k")
    junk = features.junk_after(rows, cut)
    b = evaluate.walk_forward(junk, features.build_features(junk), 5, "up_k")
    # every forecast dated on or before `cut` (logistic, base rate, implied) is unchanged by what follows it
    assert len(a.preds) == len(b.preds)
    assert all(pa.p == pb.p for pa, pb in zip(a.preds, b.preds, strict=True) if pa.i <= cut)
    assert any(pa.p != pb.p for pa, pb in zip(a.preds, b.preds, strict=True) if pa.i > cut)


# --------------------------------------------------------------------------- metrics
def test_metrics_hand_values():
    p, o = [0.1, 0.4, 0.35, 0.8], [0, 0, 1, 1]
    assert metrics.mean(metrics.brier_each(p, o)) == pytest.approx((0.01 + 0.16 + 0.4225 + 0.04) / 4)
    assert metrics.mean(metrics.logloss_each(p, o)) == pytest.approx(
        -(math.log(0.9) + math.log(0.6) + math.log(0.35) + math.log(0.8)) / 4
    )
    assert metrics.auc(p, o) == pytest.approx(0.75)  # 3 of 4 positive-negative pairs ranked right
    assert metrics.auc([0.5, 0.5], [0, 1]) == pytest.approx(0.5)  # a tie counts half
    assert metrics.auc([0.3], [1]) is None
    assert metrics.hit_rate(p, o) == pytest.approx(0.75)  # 0.35 on an event is the miss
    assert metrics.skill(0.2, 0.25) == pytest.approx(0.2)
    assert metrics.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": None}) == pytest.approx(
        {"a": 0.03, "b": 0.06, "c": 0.06, "d": None}
    )
    d = [0.1, -0.2, 0.3, 0.05, 0.0]
    m = sum(d) / 5
    assert metrics.newey_west_var(d, 0) == pytest.approx(sum((x - m) ** 2 for x in d) / 5 / 5)
    # lag 1 adds 2·(1 − 1/2)·γ1
    g1 = sum((d[j] - m) * (d[j - 1] - m) for j in range(1, 5)) / 5
    assert metrics.newey_west_var(d, 1) == pytest.approx((sum((x - m) ** 2 for x in d) / 5 + g1) / 5)
    for t, df, want in (
        (1.0, 10, 0.170446566),
        (2.0, 30, 0.027312522),
        (-1.5, 5, 0.903048160),
        (0.0, 7, 0.5),
    ):
        assert metrics._t_sf(t, df) == pytest.approx(want, abs=1e-6)


def test_calibration_slope_is_one_for_calibrated_forecasts_and_below_one_for_overconfident():
    import random

    rnd = random.Random(11)
    p = [rnd.uniform(0.05, 0.95) for _ in range(6000)]
    o = [int(rnd.random() < x) for x in p]
    a, b = metrics.calibration_fit(p, o)
    assert abs(a) < 0.1 and abs(b - 1) < 0.1
    over = [1 / (1 + math.exp(-3 * math.log(x / (1 - x)))) for x in p]  # logits tripled
    assert metrics.calibration_fit(over, o)[1] == pytest.approx(1 / 3, abs=0.05)


def test_diebold_mariano_detects_a_better_forecast_and_not_an_equal_one():
    import random

    rnd = random.Random(3)
    o = [int(rnd.random() < 0.5) for _ in range(800)]
    noisy = [min(0.99, max(0.01, (0.75 if y else 0.25) + rnd.gauss(0, 0.2))) for y in o]
    flat = [0.5] * 800
    good = metrics.diebold_mariano(metrics.brier_each(flat, o), metrics.brier_each(noisy, o), 1, 5)
    same = metrics.diebold_mariano(metrics.brier_each(flat, o), metrics.brier_each(flat, o), 1, 5)
    assert good["mean_diff"] > 0 and good["p"] < 1e-6
    assert same["p"] is None  # zero variance: no test


# --------------------------------------------------------------------------- the evaluation and its verdict
def test_null_panel_has_no_edge_and_a_strong_planted_edge_is_accepted(registered):
    null = evaluate.evaluate(
        {"SYN": ds.panel(end=date(2026, 6, 30), seed=21)}, source="synthetic", check_every=60
    )
    assert null["accepted"] == [] and null["verdict"].startswith("no edge")
    cell = next(c for c in null["cells"] if c["horizon"] == 1 and c["event"] == "up")
    assert cell["status"] == "no_edge" and cell["reasons"][0].startswith("fails:")
    assert {x["criterion"] for x in cell["criteria"]} >= {"OOS days", "Calibration slope"}
    assert (
        cell["oos"]["n"] >= 250 and cell["oos"]["from"] >= "2024-07-01" and cell["is"]["to"] <= "2024-06-30"
    )
    edge = evaluate.evaluate({"SYN": ds.panel(end=date(2026, 6, 30), seed=21, signal=0.45)}, source="synthetic",
                             check_every=60)  # fmt: skip
    assert "SYN h=1 up" in edge["accepted"]
    up1 = next(c for c in edge["cells"] if c["horizon"] == 1 and c["event"] == "up")
    t = up1["oos"]["tests"]
    assert up1["status"] == "accepted" and t["logistic_vs_implied"]["p_holm"] < 0.05
    assert t["logistic_vs_base_rate"]["brier_skill"] > 0 and up1["oos"]["models"]["logistic"]["auc"] > 0.6
    # Holm: the adjusted p is never below the raw p
    for c in edge["cells"]:
        for k in ("logistic_vs_implied", "logistic_vs_base_rate"):
            tt = c["oos"]["tests"][k]
            assert tt["p_holm"] is None or tt["p_holm"] >= tt["p"] - 1e-12


def test_in_sample_only_never_reads_an_outcome_after_is_end(registered):
    rows = ds.panel(end=date(2025, 6, 30), seed=4)
    a = evaluate.evaluate({"S": rows}, source="t", oos=False, check_every=0)
    cut = max(i for i, r in enumerate(rows) if r.day <= specs.IS_END)
    b = evaluate.evaluate({"S": features.junk_after(rows, cut)}, source="t", oos=False, check_every=0)
    assert a["oos_evaluated"] is False and all(c["oos"] is None for c in a["cells"])
    assert [c["is"] for c in a["cells"]] == [c["is"] for c in b["cells"]]


# --------------------------------------------------------------------------- the decision layer
def test_region_masses_are_coherent():
    m, notes = decide.region_masses({"up": 0.55, "up_k": 0.30, "down_k": 0.20})
    assert m == pytest.approx([0.20, 0.25, 0.25, 0.30]) and not notes
    m, notes = decide.region_masses({"up": 0.40, "up_k": 0.45, "down_k": 0.70})
    assert m == pytest.approx([0.60, 0.0, 0.0, 0.40]) and len(notes) == 2 and sum(m) == pytest.approx(1)


def test_implied_grid_reproduces_the_implied_probabilities_and_reweighting_hits_the_targets():
    spot, iv, h = 20000.0, 18.0, 5
    gi = decide.horizon_grid(spot, iv, h, None)
    sd = iv / 100 * math.sqrt(h / 252)
    up = sum(w for s, w in gi if math.log(s / spot) > 0)
    upk = sum(w for s, w in gi if math.log(s / spot) > 0.5 * sd)
    dk = sum(w for s, w in gi if math.log(s / spot) < -0.5 * sd)
    assert sum(w for _, w in gi) == pytest.approx(1)
    assert up == pytest.approx(models.implied_p(iv, h, "up"), abs=1e-9)
    assert upk == pytest.approx(models.implied_p(iv, h, "up_k"), abs=1e-9)
    assert dk == pytest.approx(models.implied_p(iv, h, "down_k"), abs=1e-9)
    # any target masses give a proper distribution
    raw = decide.horizon_grid(spot, iv, h, [0.25] * 4)
    assert sum(w for _, w in raw) == pytest.approx(1)
    target = [0.1, 0.2, 0.3, 0.4]
    gm = decide.horizon_grid(spot, iv, h, target)
    assert sum(w for s, w in gm if math.log(s / spot) > 0.5 * sd) == pytest.approx(0.4)
    assert sum(w for s, w in gm if math.log(s / spot) < -0.5 * sd) == pytest.approx(0.1)


def test_ev_of_a_long_call_held_to_expiry_is_the_hand_sum():
    from finresearch.fincalc import charges as ch

    leg = decide.CLeg("call", 20000.0, "buy", 150.0, 0.18)
    grid = [(19500.0, 0.25), (20000.0, 0.25), (20300.0, 0.25), (20600.0, 0.25)]
    today = date(2026, 10, 19)
    r = decide.evaluate_trade([leg], 75, 20000.0, grid, grid, 0.0, today, 0.065, 20.0)
    entry = ch.order_costs([ch.TradeLeg("option", "buy", 150.0, 75)], today, 20.0).total
    stt = float(ch.rates_as_of(today)["stt_option_exercise"].rate)
    pnl = [75 * (max(0, s - 20000) - 150) - stt * 75 * max(0, s - 20000) for s, _ in grid]
    assert r["ev_model"] == pytest.approx(sum(pnl) / 4 - entry, abs=0.01)
    assert r["exit_costs"] == 0 and r["costs"] == pytest.approx(
        entry, abs=0.01
    )  # held to expiry: no square-off
    assert r["max_loss"] == pytest.approx(-150 * 75)


def test_no_trade_without_an_accepted_model_even_with_a_positive_ev():
    row = {"ev_model": 50_000.0, "ev_implied": -100.0, "costs": 100.0, "max_loss": -10_000.0}
    ok, why = decide.gate(row, accepted=False)
    assert not ok and "no accepted model" in why[0]
    ok, why = decide.gate(row, accepted=True)
    assert ok and not why
    small = {**row, "ev_model": 400.0}  # below 5 % of the max loss (₹500)
    assert not decide.gate(small, accepted=True)[0]
    worse = {**row, "ev_implied": 60_000.0}
    assert not decide.gate(worse, accepted=True)[0]


def test_decide_on_a_chain_trades_only_with_an_accepted_strongly_bullish_model():
    from test_fno_signal import chain

    c = chain()  # NIFTY, expiry 6-Oct-2026, spot ≈ 22,800
    kw = dict(
        today=date(2026, 9, 29), horizon_day=date(2026, 10, 6), h=5, iv_30=14.0, rate=0.065, brokerage=20.0
    )
    implied = {ev: models.implied_p(14.0, 5, ev) for ev in specs.EVENTS}
    d = decide.decide(c, 75, probs_model=implied, accepted=False, **kw)
    assert d["action"] == "NO TRADE" and d["label"] == "NO TRADE: no measured edge" and d["chosen"] is None
    rows = [r for r in d["candidates"] if r["status"] != "unavailable"]
    assert rows and all(
        abs(r["edge"]) < 1e-6 for r in rows
    )  # the same probabilities: no edge by construction
    bull = {"up": 0.95, "up_k": 0.9, "down_k": 0.01}
    d = decide.decide(c, 75, probs_model=bull, accepted=True, **kw)
    assert (
        d["action"] == "TRADE"
        and d["chosen"]["view"] == "up"
        and d["chosen"]["ev_model"] > d["chosen"]["ev_implied"]
    )
    assert d["held_to_expiry"] is True
    d = decide.decide(c, 75, probs_model=bull, accepted=False, **kw)
    assert d["action"] == "NO TRADE"  # the same view without an accepted model is never a trade


def test_decision_parameters_are_registered():
    reg = specs.registration()
    assert reg["decision"]["margin_frac_max_loss"] == specs.DECISION.margin_frac_max_loss
    assert set(reg["decision"]["candidates"]) == set(decide.LABELS)
    assert replace(specs.DECISION) == specs.DECISION
