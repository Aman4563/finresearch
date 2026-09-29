"""F&O analytics: Black–Scholes and greeks (textbook values), strategy payoffs, the NSE option chain and the API."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.nse_fno import lot_size_for, parse_chain, parse_lot_sizes
from finresearch.fincalc import options as o

FNO = Path(__file__).parent / "fixtures" / "nse" / "fno"


def chain():
    return parse_chain(
        "NIFTY", date(2026, 10, 6), json.loads((FNO / "option_chain_NIFTY_20261006_trimmed.json").read_text())
    )


def test_black_scholes_and_greeks_match_textbook_values():
    # Hull: S=K=100, r=5%, sigma=20%, T=1 -> call 10.4506, put 5.5735
    assert o.bs_price("call", 100, 100, 1, 0.05, 0.2) == pytest.approx(10.4506, abs=1e-4)
    assert o.bs_price("put", 100, 100, 1, 0.05, 0.2) == pytest.approx(5.5735, abs=1e-4)
    g = o.greeks("call", 100, 100, 1, 0.05, 0.2)
    assert (round(g.delta, 4), round(g.gamma, 5), round(g.vega, 4), round(g.theta, 5), round(g.rho, 4)) == (
        0.6368, 0.01876, 0.3752, -0.01757, 0.5323)  # fmt: skip
    assert o.greeks("put", 100, 100, 1, 0.05, 0.2).delta == pytest.approx(
        0.6368 - 1, abs=1e-4
    )  # put-call parity
    assert o.implied_vol("call", 10.4506, 100, 100, 1, 0.05) == pytest.approx(0.2, abs=1e-5)
    assert o.implied_vol("call", 150, 100, 100, 1, 0.05) is None  # above the spot: no volatility fits


def test_strategy_profiles():
    spread = o.profile([o.Leg("call", 100, 5, 1), o.Leg("call", 110, 2, -1)], 100)
    assert (
        spread.breakevens == [103.0]
        and spread.max_profit == 7
        and spread.max_loss == -3
        and spread.net_premium == 3
    )
    straddle = o.profile([o.Leg("call", 100, 5, 1), o.Leg("put", 100, 5, 1)], 100)
    assert straddle.breakevens == [90.0, 110.0] and straddle.max_profit is None and straddle.max_loss == -10
    assert o.profile([o.Leg("call", 100, 5, -1)], 100).max_loss is None  # naked short call: unlimited loss
    assert o.profile([o.Leg("put", 100, 5, 1)], 100).max_profit == 95  # a price cannot fall below zero
    assert (
        o.profile([o.Leg("future", 100, 0, 1), o.Leg("call", 100, 5, -1)], 100).max_profit == 5
    )  # covered call
    pop = o.probability_of_profit([o.Leg("call", 100, 5, 1)], 100, 1, 0.2)
    assert 0.3 < pop < 0.5
    with pytest.raises(ValueError):
        o.profile([], 100)


def test_option_chain_analytics_and_lot_sizes():
    c = chain()
    assert c.underlying == Decimal("22780.25") and c.as_of.hour == 15 and len(c.rows) == 30
    row = next(r for r in c.rows if r.strike == 23150)
    assert (
        row.call.last_price == Decimal("62.6")
        and row.call.iv == Decimal("13.18")
        and row.put.oi == Decimal("2387")
    )
    assert c.atm().strike == 22800 and c.max_pain() == 23000 and round(c.pcr(), 2) == Decimal("0.96")
    lots = parse_lot_sizes((FNO / "fo_mktlots_head.csv").read_text())
    assert (
        lot_size_for(lots, "NIFTY", date(2026, 10, 6)) == 65
        and lot_size_for(lots, "INFY", date(2026, 10, 27)) == 400
    )


class FakeFno:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def contract_info(self, symbol):
        return [date(2026, 10, 6), date(2026, 10, 13)], [Decimal(22800), Decimal(23000)]

    async def option_chain(self, symbol, expiry):
        return chain()

    async def lot_sizes(self):
        return parse_lot_sizes((FNO / "fo_mktlots_head.csv").read_text())


def test_fno_api_chain_and_strategy(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.fincalc import dates

    monkeypatch.setattr(
        dates, "now_ist", lambda: __import__("datetime").datetime(2026, 9, 29, 10, tzinfo=dates.IST)
    )
    with TestClient(create_app(fno_client=FakeFno)) as c:
        assert c.get("/api/fno/nifty/expiries").json()["expiries"][0] == "2026-10-06"
        ch = c.get("/api/fno/NIFTY/chain", params={"expiry": "2026-10-06"}).json()
        assert ch["lot_size"] == 65 and ch["atm_strike"] == "22800" and ch["max_pain"] == "23000"
        body = {"symbol": "NIFTY", "expiry": "2026-10-06", "legs": [
            {"right": "call", "strike": "22800", "side": "buy", "lots": 1},
            {"right": "call", "strike": "23000", "side": "sell", "lots": 1}]}  # fmt: skip
        r = c.post("/api/fno/strategy", json=body).json()
        assert r["lot_size"] == 65 and r["max_profit"] > 0 and r["max_loss"] < 0 and len(r["breakevens"]) == 1
        assert 22800 < r["breakevens"][0] < 23000 and 0 < r["probability_of_profit"] < 1
        assert r["net_greeks"]["delta"] > 0 and "Analysis only" in r["disclaimer"]
        bad = {**body, "legs": [{"right": "call", "strike": "99999", "side": "buy", "lots": 1}]}
        assert c.post("/api/fno/strategy", json=bad).status_code == 422
