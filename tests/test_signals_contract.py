"""The shared signal contract and its API route."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from finresearch.signals import Factor, Signal, Validation, action_for_score, clip_score, register, registry


def sample(instrument: str = "TEST") -> Signal:
    return Signal(asset="stock", instrument=instrument, name="Test Co", action="HOLD", score=5.0,
                  event="12-month total return above the Nifty 50", horizon="12 months", method="test method",
                  validation=Validation(status="uncalibrated"), probability=0.52, probability_interval=(0.4, 0.64),
                  factors=[Factor(name="momentum", value=0.1, contribution=5.0, explanation="test")],
                  as_of=datetime(2026, 9, 30, tzinfo=UTC))  # fmt: skip


def test_action_cut_offs_are_fixed():
    assert [action_for_score(s) for s in (80, 50, 49, 20, 0, -19, -20, -49, -50, -90)] == [
        "BUY", "BUY", "ACCUMULATE", "ACCUMULATE", "HOLD", "HOLD", "REDUCE", "REDUCE", "SELL", "SELL"]  # fmt: skip
    assert clip_score(250) == 100 and clip_score(-250) == -100


def test_signal_json_carries_disclaimer_interval_and_time():
    j = sample().to_json()
    assert j["probability_interval"] == [0.4, 0.64] and j["as_of"] == "2026-09-30T00:00:00+00:00"
    assert "not investment advice" in j["disclaimer"].lower() and j["validation"]["status"] == "uncalibrated"


def test_register_rejects_unknown_assets():
    with pytest.raises(ValueError):
        register("crypto")


@pytest.fixture
def client(monkeypatch):
    from finresearch.api import create_app

    monkeypatch.setattr(registry, "_PROVIDERS", {})
    monkeypatch.setattr(registry, "_loaded", True)
    with TestClient(create_app()) as c:
        yield c


def test_signal_route_dispatches_to_the_provider_with_query_context(client):
    seen = {}

    @register("stock")
    async def provider(instrument, ctx):
        seen.update(ctx, instrument=instrument)
        if instrument == "MISSING":
            raise LookupError("unknown symbol MISSING")
        return sample(instrument)

    r = client.get("/api/signals/stock/INFY", params={"horizon": "12m"})
    assert (
        r.status_code == 200
        and r.json()["instrument"] == "INFY"
        and seen == {"horizon": "12m", "instrument": "INFY"}
    )
    assert client.get("/api/signals/stock/MISSING").status_code == 404
    assert client.get("/api/signals/fund/120505").status_code == 404  # no provider registered
    assets = {a["asset"]: a["available"] for a in client.get("/api/signals").json()["assets"]}
    assert assets == {"ipo": False, "stock": True, "fund": False, "bond": False, "fno": False}
