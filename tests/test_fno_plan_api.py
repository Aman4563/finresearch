"""Trade-plan cards, "Suggest setups" and the market-context panel (#317) through the API. Offline: the recorded NIFTY
chain (06-Oct-2026 expiry) and index history via test_fno_signal's FakeFno; every other HTTP request fails the test."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_fno_signal import FNO, NOW, FakeFno, chain

from finresearch.fincalc import dates
from finresearch.signals import fno as fs

PCS = [
    {"right": "put", "strike": 22600, "side": "sell", "lots": 1},
    {"right": "put", "strike": 22400, "side": "buy", "lots": 1},
]
FAILED = {"key": "nifty_pcs_m", "name": "NIFTY put credit spread, monthly", "role": "candidate", "status": "fails",
          "spec_hash": "x" * 64, "underlying": "NIFTY",
          "windows": {"oos": {"expectancy": -1789.4, "t_stat": -1.31, "trades": 42, "from": "2024-07-01",
                              "to": "2026-10-09"}}}  # fmt: skip


class NoSources:
    async def board_meetings(self, symbol):
        raise AssertionError("no network in tests")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    from finresearch.adapters import http
    from finresearch.fnobook import sources

    async def refuse(self, *a, **kw):
        raise AssertionError(f"network request in an offline test: {a[:1]}")

    monkeypatch.setattr(http.PoliteClient, "get", refuse)
    monkeypatch.setattr(sources, "SOURCES", NoSources())


def _profile(capital: str, pct: str = "2"):
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import save_profile
    from finresearch.suggest.profile import default_profile

    with session_scope() as s:
        save_profile(s, default_profile().model_copy(update={"fno_capital_inr": Decimal(capital),
                                                            "fno_max_loss_pct": Decimal(pct)}))  # fmt: skip


def _save_backtest(rows):
    from finresearch.backtest import store
    from finresearch.db import session_scope

    rep = {"version": "t", "registration_hash": "h", "registered_hash": "h", "oos_evaluated": True, "source": "test",
           "variants_tried": 8, "stock_rule_symbol": None, "strategies": rows}  # fmt: skip
    with session_scope() as s:
        store.save(s, rep)


@pytest.fixture
def client(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(
            text("TRUNCATE fno_backtest_result, fno_backtest_run, fno_participant_oi, fno_bhav CASCADE")
        )
    monkeypatch.setattr(dates, "now_ist", lambda: NOW)
    fs._CLOSES.clear()
    with TestClient(create_app(fno_client=FakeFno)) as c:
        yield c
    _profile("0")  # the default profile again: later test files expect F&O capital unset


def test_plan_card_has_every_part_and_no_strong_call_without_evidence(client):
    _profile("1000000")  # budget 2% = ₹20,000
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS}).json()
    assert card["kind"] == "put_credit" and card["action"] in ("ENTER", "WAIT", "AVOID")
    if card["action"] == "ENTER":
        assert card["qualifier"] == "no proven edge — size small"  # nothing saved: never an unqualified ENTER
    assert [(x["strike"], x["side"]) for x in card["legs"]] == [(22600, "sell"), (22400, "buy")]
    # risk per set = max loss + entry costs ≈ ₹12.2k → floor(20,000 / 12.2k) = 1; half of it is still 1
    assert card["size"]["multiple"] == 1 and card["size"]["suggested"] == 1
    assert (
        card["entry"]["status"] == "ok" and card["entry"]["net_mid"] is not None
    )  # the NSE chain has bid/ask
    ex = card["exits"]
    assert ex["source"] == "rule of thumb" and ex["stop"]["underlying_at_expiry"]["below"] is not None
    assert ex["stop"]["premium_per_share"] > ex["entry_net"] / 65 > ex["target"]["premium_per_share"]
    assert {r["kind"] for r in card["exit_rules"]} == {
        "profit",
        "stop",
        "dte",
        "delta",
    }  # no results for an index
    assert 0 < card["probability"]["real_world"] < 1 and 0 < card["probability"]["risk_neutral"] < 1
    assert card["evidence"]["match"] == "related" and card["evidence"]["level"] == "none"
    assert card["evidence"]["text"].startswith("No pre-registered backtest is saved yet")
    assert card["paper"]["trades"] == 0 and card["invalidation"] and card["adjustments"]["rules"]


def test_plan_is_avoid_when_the_matching_backtest_failed(client, monkeypatch):
    _profile("1000000")
    _save_backtest([FAILED])
    monkeypatch.setattr(
        dates, "now_ist", lambda: datetime(2026, 9, 1, 10, tzinfo=dates.IST)
    )  # 35 days: in window
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS}).json()
    assert card["evidence"]["match"] == "exact" and card["evidence"]["level"] == "negative"
    assert card["action"] == "AVOID" and card["label"] == "AVOID"
    assert "FAILED out-of-sample: expectancy ₹-1,789/trade, t=-1.3, 42 trades" in card["reasons"][0]
    assert card["exits"]["source"] == "spec:nifty_pcs_m" and card["exits"]["time"]["dte"] == 7


def test_plan_over_budget_is_avoid_and_unknown_budget_is_never_safe(client):
    _profile("100000")  # budget ₹2,000: one set risks ≈ ₹12k
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS}).json()
    assert card["action"] == "AVOID" and card["size"]["suggested"] == 0
    naked = [
        {"right": "put", "strike": 22600, "side": "sell", "lots": 1},
        {"right": "call", "strike": 23000, "side": "sell", "lots": 1},
    ]
    _profile("1000000")
    card = client.post(
        "/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": naked}
    ).json()
    assert (
        card["action"] == "AVOID"
        and "Defined risk" in card["failed"]
        and card["size"]["risk_per_set"] is None
    )


def test_suggest_applies_each_recipe_and_marks_closed_windows(client):
    _profile("1000000")
    _save_backtest([FAILED])
    got = client.get("/api/fno/NIFTY/suggest").json()
    cards = {c["spec_key"]: c for c in got["cards"]}
    assert set(cards) == {
        "nifty_pcs_m",
        "nifty_ccs_m",
        "banknifty_ic_m",
        "nifty_ic_w",
        "nifty_dcs_m",
        "nifty_dps_m",
    }
    # 29-Sep: no monthly expiry 30-45 days away in the fixture's list → the monthly recipes wait; the failed one avoids
    assert cards["nifty_pcs_m"]["action"] == "AVOID" and not cards["nifty_pcs_m"]["legs"]
    assert (
        cards["nifty_ccs_m"]["action"] == "WAIT"
        and "entry window is closed" in cards["nifty_ccs_m"]["reasons"][0]
    )
    ic = cards["nifty_ic_w"]  # 4-9 days: the 06-Oct weekly, legs picked by delta on the live chain
    assert ic["kind"] == "iron_condor" and len(ic["legs"]) == 4
    puts = sorted(x["strike"] for x in ic["legs"] if x["right"] == "put")
    calls = sorted(x["strike"] for x in ic["legs"] if x["right"] == "call")
    assert puts[1] < 22780.25 < calls[0]
    # no IV history stored: the recipe's IV filter is unknown → never ENTER
    assert ic["action"] == "WAIT" and "Recipe IV filter" in ic["failed"]
    assert all(not (c["action"] == "ENTER" and c.get("qualifier") is None) for c in got["cards"])


def test_context_panel_reads_stored_history_and_labels_evidence(client):
    from finresearch.adapters import nse_participant_oi as po
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav
    from finresearch.fnohist import participants

    fixture = (FNO / "fao_participant_oi_09102026.csv").read_text()
    with session_scope() as s:
        participants.save_day(
            s, date(2026, 10, 9), po.parse(fixture), "https://nsearchives.nseindia.com/x.csv"
        )
        for d, close, oi in (
            (date(2026, 9, 25), 22900.0, 1_000_000),
            (date(2026, 9, 26), 22800.0, 1_100_000),
        ):
            s.add(FnoBhav(day=d, symbol="NIFTY", instrument="IDF", expiry=date(2026, 10, 27), strike=0,
                          option_type="XX", close=close, high=close + 50, low=close - 50, oi=oi))  # fmt: skip
    got = client.get("/api/fno/NIFTY/context", params={"expiry": "2026-10-06"}).json()
    fut = got["buildup"]["futures"]["rows"][0]
    assert (
        fut["price_change"] == -100.0 and fut["oi_change"] == 100_000 and fut["buildup"] == "short build-up"
    )
    opt = got["buildup"]["options"]
    assert opt["status"] == "ok"
    row = next(r for r in opt["rows"] if r["strike"] == 22800.0)
    assert row["call"]["buildup"] == "short build-up"  # premium −239.65, OI +20,165 (the fixture's 22800 CE)
    assert row["put"]["buildup"] == "long build-up"  # premium +105.2, OI +12,206
    assert got["participants"]["day"] == "2026-10-09" and got["participants"]["previous_day"] is None
    assert got["chain"]["pcr_oi"] is not None and got["chain"]["max_pain"] is not None
    assert (
        got["trend"]["closes"] == 84 and got["trend"]["averages"][2]["sma"] is None
    )  # 84 closes: no 200-day
    for block in ("trend", "volatility", "term", "participants", "events"):
        assert got[block]["evidence"]["evidence"] and got[block]["evidence"]["measures"]
    assert (
        "not signals" in got["not_a_signal"].lower() or "context, not signals" in got["not_a_signal"].lower()
    )


def test_context_never_changes_the_verdict():
    """Changing open interest (so PCR and max pain) leaves the plan's verdict as it was (participant data is never read):
    the verdict reads the strategy checks and the backtest only."""
    from finresearch.adapters.nse_fno import parse_index_history
    from finresearch.signals import tradeplan as tpl

    raw = json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text())
    closes = [float(c) for _, c in parse_index_history(raw)]
    x = tpl.Inputs(today=date(2026, 9, 29), closes=closes, iv_series=[], capital=1_000_000, max_loss_pct=2,
                   brokerage=20, defined_risk_only=True, rate=0.065)  # fmt: skip
    legs = [fs.LegIn("put", 22600, "sell"), fs.LegIn("put", 22400, "buy")]
    base = tpl.build(chain(), 65, legs, x)
    skewed = chain()
    for r in skewed.rows:
        if r.put:
            r.put.oi = (r.put.oi or 0) * 5
    assert skewed.pcr() != chain().pcr()
    again = tpl.build(skewed, 65, legs, x)
    assert (again["action"], again["failed"]) == (base["action"], base["failed"])


def test_capital_unset_says_so_and_related_specs_do_not_borrow_paper_trades(client):
    from finresearch.signals import tradeplan as tpl

    _profile("0")  # the default: not set (explicit, whatever ran before)
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": PCS}).json()
    assert card["action"] == "AVOID" and card["reasons"][0].startswith("F&O capital is not set")
    # a paper trade tagged with the NIFTY monthly recipe but of another shape: it must not count for a card that only
    # relates to that recipe (7 days to expiry is outside its 30-45 day window)
    tagged = {"symbol": "NIFTY", "label": "plan:nifty_pcs_m", "status": "closed", "net_pnl": -500.0,
              "legs": [tpl.tp.PlanLeg("call", 23000, "sell"), tpl.tp.PlanLeg("call", 23200, "buy")]}  # fmt: skip
    from finresearch.adapters.nse_fno import parse_index_history

    closes = [
        float(c)
        for _, c in parse_index_history(
            json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text())
        )
    ]
    x = tpl.Inputs(today=date(2026, 9, 29), closes=closes, iv_series=[], capital=1_000_000, max_loss_pct=2,
                   brokerage=20, defined_risk_only=True, rate=0.065, paper=[tagged])  # fmt: skip
    got = tpl.build(chain(), 65, [fs.LegIn("put", 22600, "sell"), fs.LegIn("put", 22400, "buy")], x)
    assert got["evidence"]["match"] == "related" and got["paper"]["trades"] == 0
    # 60 lots a leg with room for 3 sets would exceed the paper book's 100-lot limit: capped
    big = tpl.build(chain(), 65, [fs.LegIn("put", 22600, "sell", 60), fs.LegIn("put", 22400, "buy", 60)],
                    tpl.Inputs(today=date(2026, 9, 29), closes=closes, iv_series=[], capital=1e9, max_loss_pct=2,
                               brokerage=20, defined_risk_only=True, rate=0.065))  # fmt: skip
    assert max(x["lots"] for x in big["legs"]) <= 100
