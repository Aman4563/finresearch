"""#218: every forecast probability carries its support: base-rate n, validation status and ledger calibration."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

IPO = {"asset": "ipo", "probability": 0.963, "probability_interval": [0.92, 0.99],
       "base_rate": {"n": 54, "p": 0.96}, "validation": {"status": "base_rate", "n": 54}}  # fmt: skip


def test_the_issue_example_reads_exactly():
    from finresearch.signals.reliability import reliability

    r = reliability(IPO, 1)
    assert (
        r["summary"]
        == "96 % (range 92–99 %) · base rate from 54 past IPOs · calibration not established: 1 scored"
    )
    assert r["support"] == "base rate from 54 past IPOs · calibration not established: 1 scored"
    assert r["base_rate_n"] == 54 and r["validation"]["status"] == "base_rate"
    assert r["calibration"] == {"scored": 1, "established": False, "tier": "base_rate", "needed": 50,
                                "label": "calibration not established: 1 scored"}  # fmt: skip


@pytest.mark.parametrize(("k", "established", "tier"), [(0, False, "base_rate"), (49, False, "base_rate"),
                                                         (50, True, "shrink"), (100, True, "platt")])  # fmt: skip
def test_calibration_is_established_at_the_policys_first_recalibration_tier(k, established, tier):
    from finresearch.evals.calibration_policy import TIERS
    from finresearch.signals.reliability import reliability

    assert TIERS[1] == ("shrink", 50)  # the policy's first tier above "base rate only"
    c = reliability(IPO, k)["calibration"]
    assert (c["established"], c["tier"]) == (established, tier)


def test_no_base_rate_is_said_not_hidden():
    from finresearch.signals.reliability import reliability

    rule = {
        "asset": "fund",
        "probability": None,
        "base_rate": None,
        "validation": {"status": "rule_based", "n": 0},
    }
    r = reliability(rule, 0)
    assert r["base_rate_n"] is None
    assert r["summary"] == "no historical base rate · calibration not established: 0 scored"
    assert r["validation"]["label"] == "rule-based, not validated on outcomes"


def _at(day: int, hour: int = 15) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=UTC)


def _sig(sym: str, p: float, when: datetime, method: str = "test reliability method"):
    from finresearch.signals import Signal, Validation

    return Signal(asset="ipo", instrument=sym, name=None, action="APPLY", score=10, event="listing gain",
                  horizon="listing day", method=method, validation=Validation("base_rate", 54), probability=p,
                  base_rate={"n": 54, "p": 0.96}, probability_interval=(0.92, 0.99), as_of=when)  # fmt: skip


@pytest.fixture
def ledger(env):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    with session_scope() as s:
        s.query(Forecast).delete()
    yield


def _log(s, sym, p, day, outcome, method="test reliability method"):
    from finresearch.signals import ledger

    fid = ledger.record(_sig(sym, p, _at(day), method), source="signal:ipo", resolve_on=date(2026, 9, 8),
                        event_kind="listing_gain", session=s)  # fmt: skip
    if outcome is not None:
        ledger.resolve(s, fid, outcome, now=_at(8, 17))


def test_scored_counts_independent_outcomes_of_the_same_method(ledger):
    from finresearch.db import session_scope
    from finresearch.signals.reliability import scored_events

    with session_scope() as s:
        for day in (1, 2, 3):  # one listing viewed on three days = one outcome
            _log(s, "RELA", 0.9, day, 1)
        _log(s, "RELB", 0.7, 2, 0)
        _log(s, "RELC", 0.8, 2, None)  # open: not scored
        _log(s, "RELD", 0.8, 2, 1, method="another method")
        s.flush()
        assert scored_events(s, "ipo", "test reliability method") == 2
        assert scored_events(s, "stock", "test reliability method") == 0


def test_signal_api_returns_the_reliability_beside_the_probability(ledger, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.signals import registry

    with session_scope() as s:
        _log(s, "RELA", 0.9, 1, 1)

    async def fake(instrument, ctx):
        return _sig(instrument, 0.963, _at(9))

    registry._load()
    monkeypatch.setitem(registry._PROVIDERS, "ipo", fake)
    with TestClient(create_app()) as c:
        body = c.get("/api/signals/ipo/NEWCO").json()
    assert body["probability"] == 0.963
    assert body["reliability"]["summary"] == ("96 % (range 92–99 %) · base rate from 54 past IPOs · calibration not "
                                              "established: 1 scored")  # fmt: skip
