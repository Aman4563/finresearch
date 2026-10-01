"""The daily layer (WP-D): the tax calendar, SIP inference and the holding-period watch (golden values), the monitor's
daily portfolio pass on a synthetic portfolio with fake exchange data, every new portfolio alert metric (values and
fire/clear), the morning brief and weekly digest (golden text, scheduling, one send per slot) and the API routes
(strip, calendar, export). Offline: every source is a fake; nothing touches NSE or AMFI."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from finresearch.fincalc.tax_calendar import advance_tax_estimate, calendar, instalments, next_instalment
from finresearch.monitor.digest import brief_text, contributors, due, value_change
from finresearch.monitor.portfolio_daily import close_time, day_move, due_passes, plan_of
from finresearch.portfolio.metrics import n_effective
from finresearch.portfolio.sip import infer_sip, status_for

FIX = Path(__file__).parent / "fixtures" / "nse"
IST_1630 = timedelta(hours=11)  # 16:30 IST in UTC


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- goldens (no DB)
def test_advance_tax_schedule_and_estimate_golden():
    ins = instalments(2027)  # FY 2026-27
    assert [(i.due, i.cumulative_pct) for i in ins] == [
        (date(2026, 6, 15), D("0.15")), (date(2026, 9, 15), D("0.45")),
        (date(2026, 12, 15), D("0.75")), (date(2027, 3, 15), D("1.00")),
    ]  # fmt: skip
    assert next_instalment(date(2026, 9, 30), 2027).due == date(2026, 12, 15)
    assert next_instalment(date(2027, 3, 16), 2027) is None
    # ₹20,800 tax on gains + ₹50,000 dividends at 30 % + 4 % cess (₹15,600) = ₹36,400; 75 % by 15-Dec = ₹27,300
    e = advance_tax_estimate(D(20800), D(50000), D("0.30"), date(2026, 9, 30), 2027)
    assert e["dividend_tax"] == 15600 and e["total"] == 36400 and e["next"]["amount"] == 27300
    assert e["next"]["days"] == 76 and not e["below_threshold"] and e["status"] == "unverified"
    small = advance_tax_estimate(D(5000), D(0), D("0.30"), date(2026, 9, 30), 2027)
    assert (
        small["below_threshold"] and small["next"]["amount"] == 0
    )  # under ₹10,000: nothing from these alone
    cal = calendar(date(2026, 9, 30), 2027, horizon_days=200)
    assert [(c["day"], c["kind"], c["verified"]) for c in cal] == [
        ("2026-12-15", "advance_tax", False), ("2027-03-15", "advance_tax", False), ("2027-03-31", "fy_end", True)]  # fmt: skip


def test_sip_inference_golden():
    monthly = [(date(2026, m, 5), D(5000)) for m in (3, 4, 5, 6)]
    assert infer_sip(monthly) == (4, D(5000), 5, date(2026, 6, 5))
    # a top-up after the last instalment, or between two, does not hide the SIP or count as an instalment
    assert infer_sip([*monthly, (date(2026, 6, 20), D(800))]) == (4, D(5000), 5, date(2026, 6, 5))
    assert infer_sip([(date(2026, 4, 20), D(800)), *monthly])[0] == 4
    assert infer_sip([(date(2025, 1, 5), D(5000)), *monthly])[0] == 4  # a gap over 35 days ends the run
    assert infer_sip(monthly[:2]) is None  # fewer than three instalments
    assert (
        infer_sip([(date(2026, 3, 5), D(5000)), (date(2026, 4, 5), D(9000)), (date(2026, 5, 5), D(5000))])
        is None
    )
    assert [status_for(d) for d in (35, 36, 65, 66)] == ["on track", "missed", "missed", "stopped"]


def test_lot_turning_long_term_tax_saved_golden():
    from finresearch.portfolio.tax import HoldingTax
    from finresearch.portfolio.tax_watch import lt_date, turning_long_term

    h = HoldingTax(1, "Example Ltd", "Manual", None, "equity", True, None)
    assert lt_date(h, date(2025, 10, 15), date(2026, 9, 30)) == date(2026, 10, 16)  # held MORE than 12 months
    assert lt_date(h, date(2024, 1, 1), date(2026, 9, 30)) is None  # already long-term
    lots = [{"holding": h, "acquired": date(2025, 10, 15), "quantity": D(100), "cost_per_unit": D(1000),
             "price": D(1500), "older_lots": 0}]  # fmt: skip
    got = turning_long_term(lots, [], date(2026, 9, 30), D("0.30"))
    # ₹50,000 gain: STCG 20 % + 4 % cess = ₹10,400 today; after 16-Oct it is LTCG inside the ₹1.25 lakh exemption
    assert got == [{"holding_id": 1, "name": "Example Ltd", "account": "Manual", "acquired": "2025-10-15",
                    "quantity": 100.0, "lt_date": "2026-10-16", "days": 16, "gain": 50000.0, "tax_now": 10400.0,
                    "tax_later": 0.0, "saved": 10400.0, "older_lots": 0, "price": 1500.0}]  # fmt: skip
    assert turning_long_term(lots, [], date(2026, 9, 1), D("0.30")) == []  # 45 days away: outside the window
    loss = [{**lots[0], "price": D(900)}]
    assert turning_long_term(loss, [], date(2026, 9, 30), D("0.30")) == []  # a loss is not listed


def test_concentration_value_change_and_moves_golden():
    assert n_effective([60, 10, 10, 10, 10]) == pytest.approx(2.5)  # HHI 0.36 + 4 x 0.01 = 0.40
    assert n_effective([25, 25, 25, 25]) == pytest.approx(4.0)
    rows = [(date(2026, 9, 21), 100000.0, 90000.0), (date(2026, 9, 22), 112000.0, 100000.0),
            (date(2026, 9, 23), 110000.0, 100000.0)]  # fmt: skip
    vc = value_change(rows)
    # (112000 - 10000) / 100000 = 1.02; x 110000 / 112000 = 1.0017857 -> +0.18 %; ₹10,000 change is all new money
    assert vc == {"from": "2026-09-21", "to": "2026-09-23", "start": 100000.0, "end": 110000.0, "change": 10000.0,
                  "new_money": 10000.0, "market": 0.0, "twr_pct": 0.18, "days": 3}  # fmt: skip
    hist = {
        "2026-09-21": {"1": [100.0, 10.0], "2": [50.0, 100.0]},
        "2026-09-23": {"1": [110.0, 20.0], "2": [48.0, 100.0]},
    }
    c = contributors(hist, {"1": "A", "2": "B"}, "2026-09-21", "2026-09-23")
    assert c == {"top": [{"name": "A", "inr": 100.0, "return_pct": 10.0}],  # start units: buying more is not a gain
                 "bottom": [{"name": "B", "inr": -200.0, "return_pct": -4.0}]}  # fmt: skip
    assert (
        day_move([100, 10], [106, 10]) == 6.0 and day_move([100, 10], [50, 20]) is None
    )  # a split is no move
    assert plan_of("X Fund - Regular Plan - Growth") == "regular" and plan_of("X Direct Growth") == "direct"
    assert plan_of("X Fund") is None


def test_pass_and_brief_times():
    assert (
        close_time((16, 30)).strftime("%H:%M") == "16:00" and close_time((16, 0)).strftime("%H:%M") == "15:50"
    )
    d = datetime(2026, 9, 30, tzinfo=UTC)
    assert due_passes(d + timedelta(hours=10, minutes=15), (16, 30), set()) == []  # 15:45 IST
    assert due_passes(d + IST_1630, (16, 30), set()) == [("close", "pf_daily:2026-09-30")]
    assert due_passes(d + timedelta(hours=17, minutes=31), (16, 30), set())[-1] == (
        "nav",
        "pf_nav:2026-09-30",
    )
    assert due_passes(datetime(2026, 10, 3, 11, tzinfo=UTC), (16, 30), set()) == []  # Saturday
    assert due(d + timedelta(hours=3, minutes=1), set()) == [("brief", "brief:2026-09-30")]  # 08:31 IST
    assert due(d + timedelta(hours=5, minutes=1), set()) == []  # 10:31 IST: too late, skipped
    assert due(d + timedelta(hours=2), set()) == []  # 07:30 IST
    sun = datetime(2026, 10, 4, 3, 45, tzinfo=UTC)  # 09:15 IST Sunday: digest only (not a trading day)
    assert due(sun, set()) == [("digest", "digest:2026-10-04")]


def test_brief_text_golden_and_wording():
    b = {"day": "2026-09-30", "headline": "Nothing needs a decision today: no rule fired and nothing is due. No action needed.",
         "rules_fired": [], "signal_changes": [{"name": "Infosys", "from": "HOLD", "to": "REDUCE"}],
         "events": [{"day": "2026-10-07", "title": "Infosys: Dividend (ex-date; record n/a)"}], "sip_missed": [],
         "performance": None, "health": []}  # fmt: skip
    assert brief_text(b) == (
        "Morning brief Wed 30 Sep\n"
        "Nothing needs a decision today: no rule fired and nothing is due. No action needed.\n"
        "• Signal Infosys: HOLD → REDUCE\n"
        "• 07 Oct: Infosys: Dividend (ex-date; record n/a)\n"
        "Personal research, not advice."
    )


# --------------------------------------------------------------------------- the daily pass on a synthetic portfolio
@pytest.fixture
def pf(env, monkeypatch):
    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.portfolio.service import manual_txn

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting, alert_delivery, alert, alert_rule_state, alert_eval_slot, "
                       "notification_setting, investor_profile, wealth_valuation, wealth_asset CASCADE"))  # fmt: skip
        manual_txn(s, {"asset_type": "stock", "name": "Infosys Ltd", "nse_symbol": "INFY", "day": date(2025, 10, 10),
                       "kind": "buy", "quantity": D(10), "price": D(1400), "amount": D(14000)})  # fmt: skip
        for m in (4, 5, 6, 7):
            manual_txn(s, {"asset_type": "mf", "name": "Example Flexi Cap Fund - Regular Plan - Growth",
                           "scheme_code": "100001", "account": "Folio 1", "day": date(2026, m, 5), "kind": "buy",
                           "quantity": D(100), "price": D(50), "amount": D(5000)})  # fmt: skip
        manual_txn(s, {"asset_type": "stock", "name": "Infosys Ltd", "nse_symbol": "INFY", "day": date(2026, 6, 1),
                       "kind": "dividend", "amount": D(200)})  # fmt: skip
    import finresearch.fincalc.dates as dates

    monkeypatch.setattr(dates, "today_ist", lambda: date(2026, 9, 30))
    return env


def fake_deps(price, nav, action, ter):
    from finresearch.adapters.amfi import SchemeNav, SchemeTer
    from finresearch.adapters.nse import Quote
    from finresearch.adapters.nse_equity import BoardMeeting, CorporateAction
    from finresearch.monitor.jobs import Deps
    from finresearch.signals.base import Signal, Validation

    async def quote(sym, exch):
        assert (sym, exch) == ("INFY", "NSE")
        return Quote(symbol=sym, last_price=D(price["v"]), industry="Computers - Software")

    async def rows():
        return [SchemeNav("100001", "Example Flexi Cap Fund - Regular Plan - Growth", "Regular", "Growth", None, None,
                          D(nav["v"]), date(2026, 9, 29), "Equity Scheme - Flexi Cap Fund", "Example MF")]  # fmt: skip

    calls = []

    async def signal(asset, inst):
        calls.append((asset, inst))
        return Signal(asset=asset, instrument=inst, name="Infosys" if asset == "stock" else "Example Flexi Cap",
                      action=action["v"] if asset == "stock" else "HOLD", score=-25.0 if action["v"] == "REDUCE" else 0.0,
                      event="12-month excess return", horizon="12 months", method="rule-based composite v1",
                      validation=Validation("rule_based"))  # fmt: skip

    async def events(sym):
        bm = [BoardMeeting.parse(r) for r in json.loads((FIX / "board_meetings_INFY.json").read_text())]
        return {"actions": [CorporateAction(symbol=sym, subject="Interim Dividend - Rs 23 Per Share",
                                            ex_date=date(2026, 10, 7), record_date=date(2026, 10, 7),
                                            dividend_per_share=D(23))],
                "board_meetings": bm,
                "results": [SimpleNamespace(filed_at=datetime(2026, 7, 23, 16, 5, tzinfo=UTC), period_to=date(2026, 6, 30))]}  # fmt: skip

    async def ter_file(month):
        return {"example flexi cap fund": SchemeTer("Example Flexi Cap Fund", "Equity", date(2026, 9, 1),
                                                    D(ter["v"]), D("0.60"))}  # fmt: skip

    d = Deps(ipo_detail=None, quote=None, portfolio_daily=True, pf_quote=quote, pf_scheme_rows=rows, pf_signal=signal,
             pf_stock_events=events, pf_ter=ter_file, pf_spacing_s=0)  # fmt: skip
    return d, calls


def two_passes(price, nav, action, ter):
    from finresearch.db import session_scope
    from finresearch.monitor.portfolio_daily import portfolio_step
    from finresearch.portfolio.service import manual_txn

    deps, calls = fake_deps(price, nav, action, ter)
    day1 = datetime(2026, 9, 29, tzinfo=UTC) + IST_1630
    r1 = run(portfolio_step(deps, day1, holidays=set(), stock_time=(16, 30)))
    assert r1["close"]["valuation"]["complete"]
    assert (
        run(portfolio_step(deps, day1, holidays=set(), stock_time=(16, 30))) == {}
    )  # claimed: once per slot
    price["v"], nav["v"], action["v"], ter["v"] = "1410", "52", "REDUCE", "1.62"
    with session_scope() as s:  # a dividend recorded between the passes
        manual_txn(s, {"asset_type": "stock", "name": "Infosys Ltd", "nse_symbol": "INFY", "day": date(2026, 9, 29),
                       "kind": "dividend", "amount": D(230)})  # fmt: skip
    r2 = run(
        portfolio_step(
            deps, datetime(2026, 9, 30, tzinfo=UTC) + IST_1630, holidays=set(), stock_time=(16, 30)
        )
    )
    return r1, r2, calls


def test_daily_pass_values_and_every_metric(pf):
    from finresearch.alerts.portfolio import portfolio_metrics
    from finresearch.alerts.registry import metrics_for
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot
    from finresearch.portfolio.metrics import PORTFOLIO_METRICS

    price, nav, action, ter = {"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.50"}
    _, r2, calls = two_passes(price, nav, action, ter)
    assert r2["close"]["signals"] == {"computed": 2, "errors_n": 0, "changes_n": 1}
    assert set(calls) == {("stock", "INFY"), ("fund", "100001")}
    with session_scope() as s:
        snaps = s.query(PortfolioSnapshot).order_by(PortfolioSnapshot.day).all()
        # the snapshot the drawdown and drift alerts read is written by the pass, not by opening /portfolio
        assert [(x.day, x.value, x.complete) for x in snaps] == [(date(2026, 9, 29), D("35000.00"), True),
                                                                 (date(2026, 9, 30), D("34900.00"), True)]  # fmt: skip
        m, reason = portfolio_metrics(s)
    assert reason is None
    # holdings_red_flags is computed by finresearch.disclosures (alerts.compute routes it there), not this module
    assert set(m) == set(PORTFOLIO_METRICS) == {x.key for x in metrics_for("portfolio")} - {"holdings_red_flags"}
    v = {k: x[0] for k, x in m.items()}
    # INFY 10 x 1410 = 14,100; fund 400 units x 52 = 20,800; total 34,900
    assert v["holding_day_move_pct"] == D("6.00") and m["holding_day_move_pct"][2] == "Infosys Ltd -6.00%"
    assert v["holding_signal_changed"] == 1 and m["holding_signal_changed"][2] == "Infosys HOLD -> REDUCE"
    assert v["reduce_signal_weight_pct"] == D("40.40")  # 14,100 / 34,900
    assert v["max_position_pct"] == D("59.60") and m["max_position_pct"][2].startswith("Example Flexi Cap")
    assert v["max_sector_pct"] == D("40.40") and m["max_sector_pct"][2] == "Computers - Software"
    assert v["n_effective"] == D(str(round(1 / (0.4040114613**2 + 0.5959885387**2), 2)))
    # INFY lot bought 10-Oct-2025: long-term from 11-Oct-2026; ₹100 gain -> STCG 20 % + cess = ₹20.80 saved by waiting
    assert v["days_to_next_lt_lot"] == 11 and v["lt_wait_tax_saved_inr"] == D("20.80")
    assert v["ltcg_used_pct"] == 0 and v["ltcg_headroom_inr"] == D("125000.0")
    assert v["sip_missed"] == 1 and "stopped" in m["sip_missed"][2]  # last instalment 5-Jul: 87 days
    assert v["dividend_received"] == 1 and m["dividend_received"][2] == "Infosys Ltd ₹230"
    assert v["days_to_holding_ex_date"] == 7
    assert v["days_to_holding_results_meeting"] == 23  # NSE board meeting 23-Oct-2026 (fixture)
    assert v["days_since_holding_results"] == 69  # filed 23-Jul-2026
    assert v["fund_ter_change_pp"] == D("0.1200") and m["fund_ter_change_pp"][2].endswith("1.5% -> 1.62%")
    assert v["regular_plan_value_inr"] == D("20800.00")
    assert v["unpriced_holdings"] == 0
    assert v["advance_tax_due_inr"] == 0 and "below the ₹10,000 threshold" in m["advance_tax_due_inr"][1]
    assert v["drawdown_pct"] == D("-0.29")  # 34,900 / 35,000 - 1 with no new money
    for k, x in m.items():
        assert x[1], k  # every reading says where it came from


def test_portfolio_rule_fires_once_with_detail_then_clears(pf, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.suggest.profile import AlertRule

    price, nav, action, ter = {"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.50"}
    two_passes(price, nav, action, ter)
    rules = [AlertRule(id="mv", kind="portfolio", metric="holding_day_move_pct", op=">=", value=D(5)),
             AlertRule(id="sig", kind="portfolio", metric="holding_signal_changed", op="==", value=D(1))]  # fmt: skip
    now = datetime(2026, 9, 30, 11, 5, tzinfo=UTC)
    with session_scope() as s:
        res = run(engine.evaluate(s, rules, now))
    assert res["fired"] == 2
    with session_scope() as s:
        msgs = sorted(a.message for a in s.query(Alert).filter(Alert.kind == "rule_alert"))
        assert msgs == ["Portfolio: a holding's signal action changed (Infosys HOLD -> REDUCE)",
                        "Portfolio: largest one-day move of a holding (either way) is 6% (is at least your 5%); "
                        "Infosys Ltd -6.00%"]  # fmt: skip
        again = run(engine.evaluate(s, rules, now + timedelta(hours=1)))
    assert again["fired"] == 0 and again["still_fired"] == 2
    import finresearch.fincalc.dates as dates

    monkeypatch.setattr(
        dates, "today_ist", lambda: date(2026, 10, 1)
    )  # next day, no pass yet: the flag clears
    with session_scope() as s:
        nxt = run(engine.evaluate(s, rules[1:], now + timedelta(days=1)))
    assert nxt["clear"] == 1


def test_scheduled_signals_never_touch_the_ledger(pf, monkeypatch):
    """The live signal call of the daily pass passes log=0 (signals.ledger logging rules)."""
    from finresearch.monitor import portfolio_daily
    from finresearch.signals import registry

    seen = []

    async def provider(inst, ctx):
        seen.append(ctx)
        raise LookupError("stop")

    monkeypatch.setattr(registry, "get_provider", lambda a: provider, raising=False)
    import finresearch.signals as sig

    monkeypatch.setattr(sig, "get_provider", lambda a: provider)
    with pytest.raises(LookupError):
        run(portfolio_daily._live_signal("stock", "INFY"))
    assert seen == [{"log": "0"}]


def test_failed_pass_is_retried_later_and_capped(pf):
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.monitor.portfolio_daily import portfolio_step

    deps, _ = fake_deps({"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.5"})

    async def down():
        raise ConnectionError("AMFI down")

    deps.pf_scheme_rows = down
    t = datetime(2026, 9, 30, tzinfo=UTC) + IST_1630
    # fetch_prices degrades a failed NAV file to the statement price, so force a failure in the snapshot write
    import finresearch.portfolio.report as report

    orig = report.snapshot
    report.snapshot = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert run(portfolio_step(deps, t, holidays=set(), stock_time=(16, 30))) == {"close": "failed"}
        assert run(portfolio_step(deps, t + timedelta(minutes=5), holidays=set(), stock_time=(16, 30))) == {}
        assert run(portfolio_step(deps, t + timedelta(minutes=25), holidays=set(), stock_time=(16, 30))) == {
            "close": "failed"
        }
        assert run(portfolio_step(deps, t + timedelta(minutes=50), holidays=set(), stock_time=(16, 30))) == {
            "close": "failed"
        }
        assert (
            run(portfolio_step(deps, t + timedelta(hours=2), holidays=set(), stock_time=(16, 30))) == {}
        )  # 3 attempts
    finally:
        report.snapshot = orig
    with session_scope() as s:
        row = s.get(AlertEvalSlot, "pf_daily:2026-09-30")
        assert row.result["attempts"] == 3 and "boom" in row.result["error"]


def test_no_holdings_no_work(env):
    from sqlalchemy import text

    from finresearch.db import session_scope
    from finresearch.monitor.portfolio_daily import portfolio_step

    with session_scope() as s:
        s.execute(
            text("TRUNCATE portfolio_txn, portfolio_holding, portfolio_setting, alert_eval_slot CASCADE")
        )
    deps, calls = fake_deps({"v": "1"}, {"v": "1"}, {"v": "HOLD"}, {"v": "1"})
    assert (
        run(portfolio_step(deps, datetime(2026, 9, 30, 11, tzinfo=UTC), holidays=set(), stock_time=(16, 30)))
        == {}
    )
    assert calls == []


# --------------------------------------------------------------------------- brief, digest and the API
def test_brief_is_built_sent_once_and_pushed(pf):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertDelivery
    from finresearch.monitor import notify
    from finresearch.monitor.digest import BRIEF_KIND, brief_step, build_brief, save_settings

    two_passes({"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.50"})
    with session_scope() as s:
        notify._store(s, "ntfy", notify.NtfySettings(enabled=True, topic="finresearch-test-topic"))
        save_settings(s, {"channels": ["ntfy"]})
    t = datetime(2026, 10, 1, 3, 5, tzinfo=UTC)  # 08:35 IST Thursday
    assert brief_step(t, holidays=set()) == {"brief": 1}
    assert brief_step(t + timedelta(minutes=1), holidays=set()) == {}  # one brief per day
    with session_scope() as s:
        a = s.query(Alert).filter(Alert.kind == BRIEF_KIND).one()
        assert a.level == "info" and a.data["path"] == "/brief"
        d = s.query(AlertDelivery).filter(AlertDelivery.alert_id == a.id).one()
        assert d.channel == "ntfy" and d.title == "FinResearch · Morning brief"
        b = build_brief(s, t)
    text = a.message
    assert text.startswith("Morning brief Thu 01 Oct") and text.endswith("Personal research, not advice.")
    assert "• Signal Infosys: HOLD → REDUCE" in text  # yesterday's after-close change is news this morning
    kinds = {e["kind"] for e in b["events"]}
    assert kinds == {
        "corporate_action"
    }  # the lot turns long-term in 10 days: in the watch list, not this week
    assert b["long_term"][0]["lt_date"] == "2026-10-11" and b["long_term"][0]["saved"] == 20.8
    assert any("Infosys Ltd: Interim Dividend" in e["title"] for e in b["events"])
    assert b["sip_missed"][0]["status"] == "stopped" and b["performance"] is None  # daily P&L is opt-in
    assert all("you should" not in json.dumps(x).lower() for x in b.values())
    assert b["disclaimer"].startswith("Personal research")


def test_weekly_digest_split(pf):
    from finresearch.db import session_scope
    from finresearch.monitor.digest import build_digest, digest_text

    two_passes({"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.50"})
    with session_scope() as s:
        d = build_digest(s, datetime(2026, 10, 4, 3, 45, tzinfo=UTC))
    assert (
        d["value"]["change"] == -100.0 and d["value"]["market"] == -100.0 and d["value"]["new_money"] == 0.0
    )
    assert d["contributors"]["bottom"] == [{"name": "Infosys Ltd", "inr": -900.0, "return_pct": -6.0}]
    assert d["contributors"]["top"] == [{"name": "Example Flexi Cap Fund - Regular Plan - Growth", "inr": 800.0,
                                         "return_pct": 4.0}]  # fmt: skip
    assert digest_text(d).splitlines()[1] == ("Time-weighted return -0.29% (2026-09-29 to 2026-09-30): market move "
                                              "-₹100, new money ₹0.")  # fmt: skip


def test_api_strip_calendar_brief_and_export(pf):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import WealthAsset, WealthValuation
    from finresearch.monitor import notify

    two_passes({"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.50"})
    with session_scope() as s:
        notify._store(
            s, "telegram", notify.TelegramSettings(enabled=True, bot_token="123456:" + "A" * 30, chat_id="42")
        )
        a = WealthAsset(kind="bank", name="Savings (synthetic)", liquid=True)
        s.add(a)
        s.flush()
        s.add(WealthValuation(asset_id=a.id, day=date(2026, 9, 1), value=D(50000)))
    app = create_app(clock=lambda: datetime(2026, 9, 30, 12, tzinfo=UTC))
    origin = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
    with TestClient(app) as c:
        st = c.get("/api/dashboard/portfolio").json()
        assert st["value"] == 34900.0 and st["ltcg_headroom"] == 125000.0 and st["week"]["twr_pct"] == -0.29
        assert st["net_worth"]["net_worth"] == 84900.0  # the /wealth module: portfolio ₹34,900 + bank ₹50,000
        cal = c.get("/api/brief/calendar").json()
        assert [(i["day"], i["kind"]) for i in cal["items"]] == [
            ("2026-10-07", "corporate_action"), ("2026-10-11", "long_term"), ("2026-10-23", "results")]  # fmt: skip
        assert any(i["kind"] == "results" and i["day"] == "2026-10-23" for i in cal["items"])
        assert cal["advance_tax"]["status"] == "unverified"
        br = c.get("/api/brief").json()
        assert br["has_portfolio"] and br["history"] == []
        assert c.put("/api/brief/settings", headers=origin, json={"daily_performance": True}).json()[
            "daily_performance"
        ]
        assert c.put("/api/brief/settings", headers=origin, json={"channels": ["pager"]}).status_code == 422
        sent = c.post("/api/brief/send?kind=digest", headers=origin).json()
        assert sent["kind"] == "weekly_digest"
        assert c.get(f"/api/brief/{sent['id']}").json()["body"]["value"]["change"] == -100.0
        r = c.get("/api/export/all.zip")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    assert {
        "README.txt",
        "manifest.json",
        "json/portfolio_transactions.json",
        "csv/portfolio_holdings.csv",
    } <= names
    blob = b"".join(z.read(n) for n in names)
    assert b"PERSONAL \xe2\x80\x93 NOT FOR DISTRIBUTION" in z.read("README.txt")
    assert b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in blob  # notification secrets are not exported
    assert b"cache:valuation" not in blob  # derived caches are not exported
    tx = json.loads(z.read("json/portfolio_transactions.json"))
    assert len(tx["rows"]) == 7 and tx["watermark"].startswith("PERSONAL")
    assert json.loads(z.read("json/wealth_assets.json"))["rows"][0]["name"] == "Savings (synthetic)"
    assert not any("broker" in n for n in names)  # broker connections hold tokens: never exported


def test_board_meetings_fixture_parses():
    """NSE /api/corporate-board-meetings?index=equities&symbol=INFY, read 30-Sep-2026 (trimmed)."""
    from finresearch.adapters.nse_equity import BoardMeeting, nse_source_url

    rows = [BoardMeeting.parse(r) for r in json.loads((FIX / "board_meetings_INFY.json").read_text())]
    assert rows[0].symbol == "INFY" and rows[0].day == date(2026, 10, 23) and rows[0].results
    assert rows[1].purpose == "Financial Results/Dividend" and rows[1].attachment.startswith(
        "https://nsearchives"
    )
    assert nse_source_url("board_meetings", "INFY").endswith(
        "/api/corporate-board-meetings?index=equities&symbol=INFY"
    )


def test_tick_runs_the_pass_in_the_background_and_the_schedule_says_so(pf, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot
    from finresearch.monitor import portfolio_daily, scheduler

    deps, _ = fake_deps({"v": "1500"}, {"v": "50"}, {"v": "HOLD"}, {"v": "1.5"})
    deps.brief = True
    monkeypatch.setattr(
        portfolio_daily, "due_passes", lambda now, st, h=None: [("close", "pf_daily:2026-09-30")]
    )
    import finresearch.monitor.digest as dg

    monkeypatch.setattr(dg, "due", lambda now, h=None: [])

    async def go():
        await scheduler.tick(deps, datetime(2026, 9, 30, 11, tzinfo=UTC))
        await scheduler.drain()

    run(go())
    with session_scope() as s:
        assert s.get(PortfolioSnapshot, date(2026, 9, 30)).value == D("35000.00")
    sj = scheduler.schedule_json(running=True)
    assert sj["portfolio"]["close_pass"] == "16:00" and sj["portfolio"]["brief"] == "08:30"
