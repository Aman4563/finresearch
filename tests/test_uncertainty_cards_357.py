"""#357 (P0.7): uncertainty-honest F&O cards. A typed probability interval (a model span is never a confidence
interval), EV and the payoff ratio before the chance of profit, a reliability line on every F&O card, "Inconclusive
(95% CI spans 0)" for an out-of-sample result that cannot be told from zero, and a quantile dot-plot of the P&L."""

from __future__ import annotations

import pytest
from test_fno_plan_api import PCS, _no_network, _profile, client  # noqa: F401
from test_fno_signal import _set_profile, app_client  # noqa: F401

from finresearch.fincalc import options as o
from finresearch.fincalc import tradeplan as tp
from finresearch.signals.base import INTERVAL_LABELS, Signal, Validation


def test_payoff_ratio_and_quantile_dots_by_hand():
    dist = [(100.0, 0.5), (-50.0, 0.25), (-150.0, 0.25)]
    # E[P&L | win] = 100; E[P&L | loss] = (−50·0.25 − 150·0.25) / 0.5 = −100 → ratio 1.0
    assert o.payoff_ratio(dist) == pytest.approx(1.0)
    assert o.payoff_ratio([(10.0, 1.0)]) is None  # no losing outcome: undefined, never infinite
    # the q-quantile is the smallest P&L whose cumulative probability reaches q
    assert o.weighted_quantiles(dist, (0.1, 0.25, 0.3, 0.5, 0.9)) == [-150.0, -150.0, -50.0, -50.0, 100.0]


def test_a_typed_interval_says_what_kind_it_is():
    v = Validation("rule_based", 0, {}, "")
    s = Signal(asset="fno", instrument="NIFTY", name=None, action="WAIT", score=0, event="e", horizon="h", method="m",
               validation=v, probability=0.6, probability_interval=(0.5, 0.7),
               probability_interval_kind="model_span").to_json()  # fmt: skip
    assert s["interval"] == {
        "low": 0.5,
        "high": 0.7,
        "kind": "model_span",
        "label": INTERVAL_LABELS["model_span"],
    }
    assert "not a confidence interval" in INTERVAL_LABELS["model_span"]
    assert INTERVAL_LABELS["wilson95"].startswith("95%") and INTERVAL_LABELS["bootstrap95"].startswith("95%")
    # a producer that does not say its kind is labelled so, never as a confidence interval
    s = Signal(asset="stock", instrument="X", name=None, action="HOLD", score=0, event="e", horizon="h", method="m",
               validation=v, probability=0.6, probability_interval=(0.5, 0.7)).to_json()  # fmt: skip
    assert s["interval"]["kind"] is None and "confidence" not in s["interval"]["label"]


def test_the_fno_signal_interval_is_a_model_span(app_client):  # noqa: F811
    _set_profile("1000000")
    s = app_client.get(
        "/api/signals/fno/NIFTY", params={"preset": "iron_condor", "expiry": "2026-10-06"}
    ).json()
    lo, hi = s["probability_interval"]
    assert s["interval"] == {
        "low": lo,
        "high": hi,
        "kind": "model_span",
        "label": INTERVAL_LABELS["model_span"],
    }
    assert s["reliability"]["calibration"]["label"].startswith("calibration not established")
    _set_profile("0")


def test_the_plan_card_leads_with_ev_payoff_ratio_and_reliability(client):  # noqa: F811
    _profile("1000000")
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS}).json()
    ev = card["ev"]
    assert ev["label"] == "model EV - not validated" and ev["range"][0] <= ev["range"][1]
    assert ev["payoff_ratio"] is not None and ev["payoff_ratio"] > 0
    dots = card["pnl_dots"]
    assert len(dots["values"]) == 20 and dots["values"] == sorted(dots["values"])
    assert dots["model"].startswith("real-world") and "scenario" in dots["note"]
    rel = card["reliability"]
    assert (
        rel["calibration"]["label"] == "calibration not established: 0 scored"
    )  # no F&O card is ever scored yet


def test_inconclusive_evidence_says_the_ci_spans_zero():
    row = {"status": "fails", "windows": {"oos": {"expectancy": 741.0, "t_stat": 0.76, "trades": 40, "ci_low": -1262.0,
                                                  "ci_high": 2567.0}}}  # fmt: skip
    assert tp.evidence_text(row, "inconclusive").startswith("Inconclusive (95% CI spans 0)")
    unknown = {"status": "fails", "windows": {"oos": {"expectancy": 741.0, "trades": 40}}}
    assert tp.evidence_text(unknown, "inconclusive").startswith("Inconclusive (CI unknown)")
