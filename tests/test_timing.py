"""Point-in-time invariant (#244): features must be available by the decision they inform; the IPO listing model is
labelled because its subscription features are the final book; F&O uses FBIL's curve for the risk-free rate."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import pytest

from finresearch.evals.timing import POST_CUTOFF_LABEL, Feature, LookAheadError, TimingReport, check, late

IST = datetime(2026, 1, 1, tzinfo=UTC).astimezone().tzinfo  # any aware zone works for ordering


def test_check_raises_on_an_undeclared_late_feature_and_records_declared_ones():
    decide = datetime(2026, 9, 29, 17, 0, tzinfo=UTC)
    early = Feature("nifty20", decide - timedelta(hours=1), "close")
    same = Feature("rhp", decide, "RHP")  # available exactly at the decision: allowed (<=)
    after = Feature("ln_qib", decide + timedelta(minutes=1), "final book")
    assert late([early, same, after], decide) == [after]
    with pytest.raises(LookAheadError, match="ln_qib"):
        check([early, after], decide, context="ACME")
    rep = TimingReport("close 17:00")
    assert check([early, after], decide, allow={"ln_qib"}, report=rep) == [after]
    assert rep.to_dict() == {"decision_at": "close 17:00", "rows_checked": 1, "post_decision_features": ["ln_qib"],
                             "late_counts": {"ln_qib": 1}, "usable_at_decision": False, "label": POST_CUTOFF_LABEL}  # fmt: skip
    with pytest.raises(ValueError, match="timezone"):
        late([early], datetime(2026, 9, 29, 17, 0))


def test_ipo_model_is_labelled_post_cutoff_and_the_check_catches_an_undeclared_leak(monkeypatch):
    from finresearch.evals import ipo_model as im

    rows = [
        {"symbol": "ACME", "ipo_start": date(2026, 9, 25), "ipo_end": "2026-09-29"},
        {"symbol": "NOCLOSE"},
    ]
    dp = im.decision_point(rows)
    assert dp["rows_checked"] == 1 and dp["post_decision_features"] == ["ln_nii", "ln_qib", "ln_retail"]
    assert (
        dp["usable_at_decision"] is False
        and dp["label"] == "uses post-cutoff data, not usable at the decision point"
    )
    feats, decide = im.feature_timing(rows[0])
    by = {f.name: f.available_at for f in feats}
    assert decide.hour == 17 and decide.date() == date(2026, 9, 29)
    assert by["nifty20"] < decide < by["ln_qib"]  # the 15:30 close is known; the final book is not
    assert im.usable_at_decision() is False
    monkeypatch.setattr(im, "POST_DECISION", ())  # pretending the final book is decision-time data is caught
    with pytest.raises(LookAheadError, match="ACME"):
        im.decision_point(rows)


def test_earnings_events_cannot_use_a_surprise_broadcast_after_the_event_session(monkeypatch):
    from finresearch.evals import earnings_surprise as es
    from finresearch.fincalc import surprise as S

    sessions = [date(2026, 5, 4), date(2026, 5, 5)]
    d = {"prices": [[x.isoformat(), 100.0] for x in sessions], "actions": [],
         "quarters": [{"quarter_end": "2026-03-31", "start": "2026-01-01", "announced": "2026-05-05T18:00:00+05:30",
                       "filed": "2026-05-05T18:00:00+05:30", "eps": 10.0, "revenue": 100.0}]}  # fmt: skip
    data = {es.MARKET: {"prices": d["prices"], "actions": []}, "ACME": d}
    # a bug that put t0 BEFORE the broadcast (the evening of 5-May) would leak the surprise into the reaction window
    monkeypatch.setattr(S, "event_session", lambda announced, has_time, sess: date(2026, 5, 4))
    with pytest.raises(LookAheadError, match="ACME 2026-03-31"):
        es.build_events(data, lambda _t0: frozenset({"ACME"}), first=date(2026, 3, 1), last=date(2026, 4, 1))


async def test_fno_rate_comes_from_fbil_converted_to_continuous(monkeypatch):
    from finresearch.adapters.fbil import FALLBACK_CURVE
    from finresearch.signals import rates

    y = 0.0623
    r = rates.continuous_from_half_yearly(y)
    assert math.isclose(math.exp(r), (1 + y / 2) ** 2, rel_tol=1e-12)  # same growth over a year
    week = await rates.risk_free(7 / 365)  # conftest serves FBIL's dated fallback curve
    assert week["tenor_years"] == 0.25 and week["par_yield"] == 0.0534 and week["fallback"] is True
    assert math.isclose(math.exp(week["rate"]), (1 + 0.0534 / 2) ** 2, rel_tol=1e-12)
    assert "shortest tenor" in week["note"] and "FBIL fallback curve of 23-Sep-2026" in week["source"]
    two_y = await rates.risk_free(2.0)
    assert two_y["par_yield"] == float(FALLBACK_CURVE.par_yield(2))

    async def gone():
        return None

    monkeypatch.setattr(rates, "CURVE", gone)
    none = await rates.risk_free(0.1)
    assert none["rate"] == 0.065 and none["fallback"] is True and "fixed 6.5 %" in none["source"]
