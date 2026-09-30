"""The F&O strategy signal (risk/cost/fit gate), the extended strategy API, IV history and its monitor job. Offline:
the recorded NIFTY chain (06-Oct-2026 expiry, 28-Sep close) and NSE's NIFTY 50 index history (Jun–Sep 2026)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.nse_fno import parse_index_history, parse_lot_sizes
from finresearch.fincalc import dates
from finresearch.signals import fno as fs

FNO = Path(__file__).parent / "fixtures" / "nse" / "fno"
NOW = datetime(2026, 9, 29, 10, tzinfo=dates.IST)


def chain(symbol: str = "NIFTY"):
    from finresearch.adapters.nse_fno import parse_chain

    data = json.loads((FNO / "option_chain_NIFTY_20261006_trimmed.json").read_text())
    return parse_chain(symbol, date(2026, 10, 6), data)


class FakeFno:
    calls: ClassVar[dict[str, int]] = {}

    def __init__(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    def _count(self, k):
        FakeFno.calls[k] = FakeFno.calls.get(k, 0) + 1

    async def contract_info(self, symbol):
        self._count("contract_info")
        return [date(2026, 10, 6), date(2026, 10, 13)], [Decimal(22800)]

    async def option_chain(self, symbol, expiry):
        self._count("option_chain")
        return chain(symbol)

    async def lot_sizes(self):
        self._count("lot_sizes")
        return parse_lot_sizes((FNO / "fo_mktlots_head.csv").read_text())

    async def closes(self, symbol, start, end):
        return parse_index_history(
            json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text())
        )


def test_index_history_parser_and_leg_parsing():
    rows = parse_index_history(json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text()))
    assert (
        len(rows) == 84 and rows[0][0] < rows[-1][0] and rows[-1] == (date(2026, 9, 28), Decimal("22780.25"))
    )
    compact = fs.parse_legs("buy:call:22800:2,sell:call:23000")
    as_json = fs.parse_legs(json.dumps([{"right": "call", "strike": "22800", "side": "buy", "lots": 2},
                                        {"right": "call", "strike": "23000", "side": "sell", "lots": 1}]))  # fmt: skip
    assert compact == as_json and compact[0].lots == 2
    for bad in ("buy:call", "hold:call:22800", "buy:swap:22800:1"):
        with pytest.raises(ValueError):
            fs.parse_legs(bad)
    assert [x.strike for x in fs.preset_legs(chain(), "iron_condor")] == [23050, 23300, 22550, 22300]
    with pytest.raises(ValueError):
        fs.preset_legs(chain(), "martingale")


def _analyse(preset: str, capital: float = 1_000_000, series=()):
    closes = [
        float(c)
        for _, c in parse_index_history(
            json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text())
        )
    ]
    return fs.analyse(chain(), 65, fs.preset_legs(chain(), preset), today=date(2026, 9, 29), closes=closes,
                      iv_series=list(series), capital=capital, max_loss_pct=2, brokerage=20)  # fmt: skip


def test_analysis_shows_costs_both_probabilities_and_the_expected_move():
    a = _analyse("bull_call")
    assert a["vols"]["closes"] == 84 and 0 < a["vols"]["realised_20d"] < a["vols"]["atm_iv"]
    assert set(a["costs"]["entry"]["lines"]) == {"stt", "exchange", "sebi", "stamp", "brokerage", "gst"}
    assert (
        a["costs"]["entry"]["lines"]["brokerage"] == 40
        and a["costs"]["total_to_expiry"] > a["costs"]["entry"]["total"]
    )
    rn, rw = a["risk_neutral"], a["real_world"]
    assert 0 < rn["pop"] < 1 and 0 < rw["pop"] < 1 and rw["window"] in ("20-day", "60-day")
    # the headline real-world case is the less favourable realised-vol window
    assert rw["ev"] == min(a["real_world_20d"]["ev"], a["real_world_60d"]["ev"])
    em = a["expected_move"]  # ±S·σ·√T with the ATM IV and 7 days
    assert em["move"] == pytest.approx(a["spot"] * a["vols"]["atm_iv"] * (7 / 365) ** 0.5, rel=1e-3)
    assert (
        a["iv_context"]["status"].startswith("insufficient history (0 days")
        and a["iv_context"]["skew_25d"] is not None
    )
    assert "minus its costs" in a["fair_price_note"]
    assert (
        _analyse("strangle")["max_loss"] is None and _analyse("strangle")["capital_check"]["within"] is False
    )


def test_assessment_rules():
    action, factors, failed = fs.assess(_analyse("iron_condor"))
    assert action == "ENTER" and failed == [] and sum(f.contribution for f in factors) == 100
    assert fs.assess(_analyse("iron_condor", capital=100_000))[0] == "WAIT"  # max loss above 2 % of ₹1 lakh
    action, _, failed = fs.assess(_analyse("strangle"))
    assert action == "WAIT" and "Defined risk" in failed
    assert fs.assess(_analyse("strangle"), held=True)[0] == "EXIT"
    assert fs.assess(_analyse("iron_condor"), held=True)[0] == "HOLD"
    # buying options when IV is well above realised volatility: the EV and IV-context checks fail
    action, _, failed = fs.assess(_analyse("long_call"))
    assert action == "WAIT" and {"EV after costs (real-world)", "IV context fits the strategy"} <= set(failed)
    # with 60+ recorded days the IV percentile replaces the IV/RV proxy: today's 13.36 % is the lowest -> sellers wait
    a = _analyse("iron_condor", series=[*([20.0] * 70), 13.36])
    assert a["iv_context"]["percentile"] == 0 and "IV context fits the strategy" in fs.assess(a)[2]


def _set_profile(capital: str):
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import save_profile
    from finresearch.suggest.profile import default_profile

    with session_scope() as s:
        save_profile(s, default_profile().model_copy(update={"fno_capital_inr": Decimal(capital)}))


@pytest.fixture
def app_client(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.signals import registry

    monkeypatch.setattr(dates, "now_ist", lambda: NOW)
    monkeypatch.setattr(fs, "CLIENT", FakeFno)
    monkeypatch.setattr(registry, "_PROVIDERS", {"fno": fs.fno_signal})
    monkeypatch.setattr(registry, "_loaded", True)
    fs._CLOSES.clear()
    with TestClient(create_app(fno_client=FakeFno)) as c:
        yield c


def test_provider_actions_probabilities_and_caveats(app_client):
    c = app_client
    s = c.get("/api/signals/fno/NIFTY", params={"preset": "iron_condor", "expiry": "2026-10-06"}).json()
    assert s["action"] == "NO_SIGNAL" and "capital is not set" in s["event"]
    _set_profile("1000000")
    s = c.get("/api/signals/fno/NIFTY", params={"preset": "iron_condor", "expiry": "2026-10-06"}).json()
    assert s["action"] == "ENTER" and s["asset"] == "fno" and s["score"] == 100
    assert s["validation"] == {
        "status": "rule_based",
        "n": 0,
        "metrics": {},
        "description": s["validation"]["description"],
    }
    lo, hi = s["probability_interval"]
    assert lo <= s["probability"] <= hi and s["sizing"]["max_multiple_of_these_legs"] >= 1
    assert s["sizing"]["budget_inr"] == 20000 and s["expected_return"]["p10"] <= s["expected_return"]["p90"]
    names = {f["name"] for f in s["factors"]}
    assert {
        "Chance of profit, risk-neutral",
        "Chance of profit, real-world",
        "Costs to expiry",
        "IV rank",
    } <= names
    joined = " ".join(s["caveats"])
    assert (
        "87.7%" in joined
        and fs.SEBI_STUDY_URL in joined
        and "not investment advice" in s["disclaimer"].lower()
    )
    legs = "sell:call:23050:1,sell:put:22550:1"
    w = c.get("/api/signals/fno/NIFTY", params={"legs": legs, "expiry": "2026-10-06"}).json()
    assert (
        w["action"] == "WAIT"
        and w["sizing"]["max_multiple_of_these_legs"] == 0
        and "Unlimited loss" in " ".join(w["caveats"])
    )
    assert (
        c.get("/api/signals/fno/NIFTY", params={"legs": legs, "expiry": "2026-10-06", "held": "1"}).json()[
            "action"
        ]
        == "EXIT"
    )
    assert (
        c.get("/api/signals/fno/NIFTY", params={"expiry": "2026-10-06"}).json()["action"] == "NO_SIGNAL"
    )  # no legs
    assert c.get("/api/signals/fno/NIFTY", params={"legs": "buy:call"}).status_code == 422


def test_strategy_api_iv_and_risk_notice(app_client):
    from finresearch.db import session_scope
    from finresearch.db.models import IvHistory

    c = app_client
    _set_profile("0")
    body = {"symbol": "NIFTY", "expiry": "2026-10-06", "legs": [
        {"right": "call", "strike": "22800", "side": "buy", "lots": 1},
        {"right": "call", "strike": "23000", "side": "sell", "lots": 1}]}  # fmt: skip
    r = c.post("/api/fno/strategy", json=body).json()
    an = r["analysis"]
    assert (
        an["costs"]["entry"]["lines"]["brokerage"] == 40
        and an["real_world"]
        and an["expected_move"]["move"] > 0
    )
    assert (
        r["probability_of_profit"] == an["risk_neutral"]["pop"] and r["risk_notice"]["date"] == "2026-08-20"
    )
    assert an["capital_check"]["message"] == "No F&O capital set in the profile."
    over = c.post("/api/fno/strategy", json={**body, "charge_overrides": {"stt_option_sell": "0"}}).json()
    assert over["analysis"]["costs"]["entry"]["lines"]["stt"] == 0
    assert c.post("/api/fno/strategy", json={**body, "charge_overrides": {"toll": "0.1"}}).status_code == 422
    assert c.get("/api/fno/risk-notice").json()["headline"].startswith("87.7%")
    iv = c.get("/api/fno/NIFTY/iv").json()
    assert iv["n"] == 0 and iv["status"].startswith("insufficient history (0 days") and iv["rank"] is None
    with session_scope() as s:
        for i in range(61):
            s.add(IvHistory(symbol="NIFTY", day=date(2026, 6, 1) + timedelta(days=i), expiry=date(2026, 10, 6),
                            atm_iv=Decimal(10 + i % 20), skew_25d=Decimal("1.5")))  # fmt: skip
    iv = c.get("/api/fno/NIFTY/iv").json()
    assert iv["n"] == 61 and iv["status"] == "ok" and iv["rank"] is not None and iv["skew_25d"] == 1.5
    assert (
        len(iv["series"]) == 61
        and c.get("/api/fno/NIFTY/iv", params={"days": 5}).json()["series"][0]["day"] == "2026-07-27"
    )


async def test_monitor_records_daily_atm_iv_once(env):
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Company, IvHistory, Watch
    from finresearch.monitor import iv

    iv.reset_tries()
    FakeFno.calls = {}
    assert iv.pick_expiry([date(2026, 10, 6), date(2026, 10, 13)], date(2026, 9, 29)) == date(2026, 10, 6)
    assert iv.pick_expiry([date(2026, 10, 6), date(2026, 10, 13)], date(2026, 9, 30)) == date(2026, 10, 13)
    with session_scope() as s:
        co = Company(slug="infy-iv", name="Infosys")
        s.add(co)
        s.flush()
        s.add(Watch(company_id=co.id, kind="stock", nse_symbol="INFY"))
    early = await iv.record_iv(FakeFno, datetime(2026, 9, 29, 15, 0, tzinfo=dates.IST))
    assert early["recorded"] == [] and FakeFno.calls == {}
    # the fixture chain is stamped 28-Sep 15:30; record as that day
    first = await iv.record_iv(FakeFno, datetime(2026, 9, 28, 16, 0, tzinfo=dates.IST))
    assert first["recorded"] == ["NIFTY", "BANKNIFTY", "FINNIFTY", "INFY"] and first["failed"] == {}
    calls = dict(FakeFno.calls)
    again = await iv.record_iv(FakeFno, datetime(2026, 9, 28, 16, 1, tzinfo=dates.IST).astimezone(UTC))
    assert again["recorded"] == [] and FakeFno.calls == calls  # no network once everything is recorded
    with session_scope() as s:
        rows = s.scalars(
            select(IvHistory).where(IvHistory.day == date(2026, 9, 28)).order_by(IvHistory.id)
        ).all()
        assert len(rows) == 4 and rows[0].day == date(2026, 9, 28) and rows[0].expiry == date(2026, 10, 6)
        assert rows[0].atm_strike == 22800 and rows[0].atm_iv > 0 and rows[0].skew_25d is not None
