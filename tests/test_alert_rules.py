"""Alert rules for every asset kind (roadmap item 10): the metric registry, golden metric values per kind, the
fire-once / clear / cooldown engine, portfolio skip, and delivery (ntfy, Telegram, macOS) with retries, quiet hours
and secret handling. All offline: every data source and transport is a fake."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from finresearch.alerts import compute
from finresearch.alerts.compute import Reader, parse_entry_legs, pct
from finresearch.alerts.registry import METRICS, TEMPLATES, fmt_value, sentence, spec
from finresearch.suggest.profile import UI_FIELDS, AlertRule, Profile

TODAY = date(2026, 9, 30)
NOW = datetime(2026, 9, 30, 11, 0, tzinfo=UTC)  # 16:30 IST


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fake_sources(monkeypatch):
    """Every test starts with no live source: a forgotten fake fails instead of calling NSE/AMFI."""

    async def boom(*a, **k):
        raise AssertionError("a live data source was called in an offline test")

    monkeypatch.setattr(compute, "SOURCES", compute.Sources(
        stock_quote=boom, stock_inputs=boom, signal=boom, fund_scheme=boom, fund_navs=boom, fund_analyse=boom, fund_ranks=boom,
        bonds=boom, bond_freq=boom, fno_expiries=boom, fno_chain=boom, fno_lot=boom,
        iv_series=lambda s: [], today=lambda: TODAY))  # fmt: skip
    from finresearch.alerts import portfolio

    monkeypatch.setattr(portfolio, "SOURCE", None)


def src(**kw):
    return compute.Sources(**{**compute.SOURCES.__dict__, **kw})


def reader(session=None):
    return Reader(session)


# --------------------------------------------------------------------------- registry and rules
def test_registry_is_complete_and_templates_are_valid():
    kinds = {m.kind for m in METRICS}
    assert kinds == {"ipo", "stock", "fund", "bond", "fno", "portfolio"}
    for m in METRICS:
        assert (
            m.label
            and m.phrase
            and m.description
            and m.source
            and m.unit
            and m.cadence in ("intraday", "daily")
        )
        if m.event:
            assert m.event_text and m.unit == "flag"
    for t in TEMPLATES:
        assert spec(t.kind, t.metric) is not None, t
    assert spec("stock", "promoter_pledge_pct") is None  # not computable: the SHP parser reads no pledge


def test_sentences_and_units_golden():
    assert sentence("stock", "price", "<", Decimal(1400), "INFY") == "INFY: price is below ₹1,400"
    assert (
        sentence("stock", "day_change_pct", "<=", Decimal(-5))
        == "Any watched stock: move on the day is at most -5%"
    )
    assert sentence("fund", "ter_change_pp", ">=", Decimal("0.1"), "120503") == (
        "120503: change in the expense ratio since the last alert is at least +0.1 pp"
    )
    assert (
        sentence("bond", "rating_changed", "==", 1, "INE001A07TP5")
        == "INE001A07TP5: its credit rating changed"
    )
    assert (
        sentence("ipo", "qib_times", ">=", 10, "ORIENTCABL") == "ORIENTCABL: QIB subscription is at least 10x"
    )
    assert fmt_value("inr", "12345678.5") == "₹1,23,45,678.50"
    assert fmt_value("inr", "-2500") == "-₹2,500"
    assert fmt_value("days", 1) == "1 day" and fmt_value("days", 3) == "3 days"


def test_alert_rule_validation():
    with pytest.raises(ValueError, match="unknown stock metric"):
        AlertRule(id="x", kind="stock", metric="qib_times")
    r = AlertRule(id="x", kind="stock", metric="signal_action_changed", op="<", value=5, instrument=" infy ")
    assert (r.op, r.value, r.instrument) == ("==", Decimal(1), "INFY")  # a change flag always fires on change
    with pytest.raises(ValueError, match="needs an instrument"):
        AlertRule(id="x", kind="fno", metric="spot", op=">", value=25000)
    with pytest.raises(ValueError, match="needs legs, expiry"):
        AlertRule(id="x", kind="fno", metric="strategy_pnl_pct_of_max_loss", instrument="NIFTY")
    assert AlertRule(id="p", kind="portfolio", metric="drawdown_pct").instrument == "PORTFOLIO"
    assert AlertRule(id="c", kind="stock", metric="price", channels=["ntfy", "ntfy"]).channels == ["ntfy"]


def test_old_profiles_load_and_alert_rules_stay_out_of_the_model_prompt():
    p = Profile.model_validate(
        {"capital_per_ipo_inr": "15000", "rules": []}
    )  # saved before alert rules existed
    assert p.alert_rules == []
    assert "alert_rules" in UI_FIELDS
    p = Profile(alert_rules=[AlertRule(id="a", kind="stock", metric="price", instrument="INFY", value=1400)])
    assert "alert_rules" not in p.model_dump(mode="json", exclude=UI_FIELDS)


# --------------------------------------------------------------------------- golden metric values per kind
def test_pct_golden():
    assert pct(Decimal(95), Decimal(100)) == Decimal("-5.0000")
    assert pct(Decimal(1500), Decimal(1200)) == Decimal("25.0000")


def quote(**kw):
    base = dict(last_price=Decimal(1400), close_price=None, previous_close=Decimal(1500), week52_high=Decimal(1750),
                week52_low=Decimal(1372), as_of=None)  # fmt: skip
    return SimpleNamespace(**{**base, **kw})


def test_stock_quote_metrics(monkeypatch):
    calls = []

    async def q(key):
        calls.append(key)
        return quote()

    monkeypatch.setattr(compute, "SOURCES", src(stock_quote=q))
    rd = reader()
    assert run(rd.read("stock", "price", "INFY", {}, {})).value == Decimal(1400)
    assert run(rd.read("stock", "day_change_pct", "INFY", {}, {})).value == Decimal("-6.6667")
    assert run(rd.read("stock", "pct_from_52w_high", "INFY", {}, {})).value == Decimal("-20.0000")
    assert run(rd.read("stock", "pct_from_52w_low", "INFY", {}, {})).value == Decimal("2.0408")
    assert calls == ["INFY"]  # one quote request per pass, however many metrics


def test_stock_daily_metrics_from_raw_inputs(monkeypatch):
    from finresearch.adapters.nse_equity import CorporateAction, PriceBar

    bars = [PriceBar(day=date(2025, 1, 1) + timedelta(days=i), open=None, high=None, low=None,
                     close=Decimal(100 if i < 199 else 120), prev_close=None, vwap=None, volume=None,
                     value_inr=None, trades=None)
            for i in range(200)]  # fmt: skip
    raw = {"quote": None, "bars": bars,
           "actions": [CorporateAction(symbol="X", subject="Dividend - Rs 5 Per Share", ex_date=date(2026, 10, 3),
                                       record_date=None, dividend_per_share=Decimal(5)),
                       CorporateAction(symbol="X", subject="old", ex_date=date(2026, 1, 3), record_date=None,
                                       dividend_per_share=None)],
           "results": {"quarters": [{"filed_at": "2026-09-29T18:00:00+05:30"}, {"filed_at": "2026-07-20T18:00:00"}]},
           "shareholding": {"quarters": [{"as_of": "2026-03-31", "groups": {"promoter": 52.1}},
                                         {"as_of": "2026-06-30", "groups": {"promoter": 50.6}}]},
           "annual": {}}  # fmt: skip

    async def inputs(key):
        return raw

    monkeypatch.setattr(compute, "SOURCES", src(stock_inputs=inputs))
    rd = reader()
    # SMA200 = (199 x 100 + 120) / 200 = 100.1; 120 / 100.1 - 1 = 19.8801 %
    assert run(rd.read("stock", "pct_vs_200dma", "X", {}, {})).value == Decimal("19.8801")
    ex = run(rd.read("stock", "days_to_ex_date", "X", {}, {}))
    assert ex.value == 3 and "Dividend" in ex.source
    assert run(rd.read("stock", "days_since_results", "X", {}, {})).value == 1
    assert run(rd.read("stock", "promoter_change_pp", "X", {}, {})).value == Decimal("-1.5000")
    fz = run(rd.read("stock", "forensic_red_flags", "X", {}, {}))
    assert fz.value is None and "no annual results" in fz.note  # unknown, never a false 0


def test_signal_action_change_is_a_change_flag(monkeypatch):
    actions = iter(["HOLD", "HOLD", "REDUCE"])
    seen_ctx = []

    async def sig(asset, inst):
        seen_ctx.append((asset, inst))
        return SimpleNamespace(action=next(actions), score=-25.0)

    monkeypatch.setattr(compute, "SOURCES", src(signal=sig))
    first = run(reader().read("stock", "signal_action_changed", "INFY", {}, {}))
    assert first.value == 0 and first.baseline == {"action": "HOLD"}  # the first check only records
    same = run(reader().read("stock", "signal_action_changed", "INFY", {}, first.baseline))
    assert same.value == 0
    changed = run(reader().read("stock", "signal_action_changed", "INFY", {}, same.baseline))
    assert changed.value == 1 and changed.detail == "HOLD -> REDUCE"


def test_informational_stock_signal_change_is_the_tilt_and_says_so(monkeypatch):
    # #193: the stock signal is informational, so the change flag follows the factor tilt and the alert text says
    # "informational, no proven edge" (never "HOLD -> BUY")
    tilts = iter(["factors are mixed", "factors are mixed", "factors lean positive"])

    async def sig(asset, inst):
        return SimpleNamespace(action="INFORMATIONAL", score=25.0,
                               call={"status": "informational", "tilt": next(tilts), "composite_action": "ACCUMULATE"})  # fmt: skip

    monkeypatch.setattr(compute, "SOURCES", src(signal=sig))
    first = run(reader().read("stock", "signal_action_changed", "INFY", {}, {}))
    assert first.value == 0 and first.baseline == {"tilt": "factors are mixed"}
    # a baseline stored before #193 ({"action": "HOLD"}) has no tilt: recorded again, no false alert
    same = run(reader().read("stock", "signal_action_changed", "INFY", {}, {"action": "HOLD"}))
    assert same.value == 0
    changed = run(reader().read("stock", "signal_action_changed", "INFY", {}, same.baseline))
    assert changed.value == 1
    assert changed.detail == "informational, no proven edge: factors are mixed -> factors lean positive"
    assert "BUY" not in changed.detail and "ACCUMULATE" not in changed.detail


def test_live_signal_checks_do_not_log_forecasts():
    import inspect

    from finresearch.signals import stock

    assert '"log": "0"' in inspect.getsource(compute._live_signal)
    assert 'ctx.get("log", "1")' in inspect.getsource(stock.stock_signal)


def test_fund_metrics(monkeypatch):
    scheme = SimpleNamespace(code="120503", nav=Decimal("97"), day=date(2026, 9, 29))

    async def sch(code):
        return scheme

    async def navs(s):
        return [(date(2026, 9, 26), Decimal(100)), (date(2026, 9, 29), Decimal(97))]

    analysed = {"passive": False, "ter": {"ter": 0.75, "day": "2026-09-29"},
                "windows_3y": [{"end": "2026-03-31", "fund": 0.12, "median": 0.10},
                               {"end": "2026-06-30", "fund": 0.08, "median": 0.11},
                               {"end": "2026-09-30", "fund": 0.09, "median": 0.115}]}  # fmt: skip

    async def analyse(code):
        return analysed

    monkeypatch.setattr(compute, "SOURCES", src(fund_scheme=sch, fund_navs=navs, fund_analyse=analyse))
    rd = reader()
    assert run(rd.read("fund", "nav", "120503", {}, {})).value == Decimal("97")
    assert run(rd.read("fund", "nav_change_pct", "120503", {}, {})).value == Decimal("-3.0000")
    assert run(rd.read("fund", "hit_rate_3y_pct", "120503", {}, {})).value == Decimal("33.33")
    assert run(rd.read("fund", "excess_3y_pp", "120503", {}, {})).value == Decimal("-2.5")
    assert run(rd.read("fund", "ter_pct", "120503", {}, {})).value == Decimal("0.75")
    first = run(rd.read("fund", "ter_change_pp", "120503", {}, {}))
    assert first.value == 0 and first.baseline == {"ter": "0.75"}
    later = run(rd.read("fund", "ter_change_pp", "120503", {}, {"ter": "0.62"}))
    assert later.value == Decimal("0.13")


def test_bond_metrics(monkeypatch):
    from finresearch.adapters.nse_bonds import ListedBond

    b = ListedBond(symbol="ABC", series="N1", isin="INE000A07AB1", coupon_pct=Decimal(8), face_value=Decimal(1000),
                   last_price=Decimal(1000), close=None, maturity=date(2029, 9, 30),
                   next_interest_date=date(2026, 10, 5), rating="AA", rating_agency="CRISIL", traded_value=Decimal(500000),
                   as_of=None)  # fmt: skip

    async def bonds():
        return [b]

    async def freq(isin):
        return None

    monkeypatch.setattr(compute, "SOURCES", src(bonds=bonds, bond_freq=freq))
    rd = reader()
    y = run(rd.read("bond", "ytm_pct", "INE000A07AB1", {}, {}))
    assert (
        abs(y.value - Decimal(8)) < Decimal("0.001") and "assumed yearly" in y.source
    )  # at par, YTM = coupon
    assert run(rd.read("bond", "days_to_coupon", "INE000A07AB1", {}, {})).value == 5
    r0 = run(rd.read("bond", "rating_changed", "INE000A07AB1", {}, {}))
    assert r0.value == 0 and r0.baseline == {"rating": "AA (CRISIL)"}
    r1 = run(rd.read("bond", "rating_changed", "INE000A07AB1", {}, {"rating": "AA+ (CRISIL)"}))
    assert r1.value == 1 and r1.detail == "AA+ (CRISIL) -> AA (CRISIL)"
    assert run(rd.read("bond", "price", "INE999", {}, {})).value is None


def chain(underlying=105.0, prices=None):
    prices = prices or {100: (3.0, 1.0), 110: (1.0, 6.0)}
    rows = [SimpleNamespace(strike=Decimal(k), call=SimpleNamespace(last_price=Decimal(str(c))),
                            put=SimpleNamespace(last_price=Decimal(str(p)))) for k, (c, p) in prices.items()]  # fmt: skip
    return SimpleNamespace(underlying=Decimal(str(underlying)), rows=rows, as_of=None)


def test_fno_strategy_pnl_vs_max_loss_golden(monkeypatch):
    async def ch(sym, exp):
        return chain()

    async def lot(sym, exp):
        return 10

    monkeypatch.setattr(compute, "SOURCES", src(fno_chain=ch, fno_lot=lot))
    # bull call spread: buy 100C @5, sell 110C @2, 1 lot of 10. Now 3 and 1: P&L = (3-5)x10 + (1-2)x(-10) = -10.
    # Maximum loss = net debit 3 x 10 = 30, so -10 / 30 = -33.33 %.
    r = run(reader().read("fno", "strategy_pnl_pct_of_max_loss", "NIFTY",
                          {"legs": "buy:call:100:1:5,sell:call:110:1:2", "expiry": "2026-10-06"}, {}))  # fmt: skip
    assert r.value == Decimal("-33.33")
    naked = run(reader().read("fno", "strategy_pnl_pct_of_max_loss", "NIFTY",
                              {"legs": "sell:call:100:1:5", "expiry": "2026-10-06"}, {}))  # fmt: skip
    assert naked.value is None and "unlimited" in naked.note
    with pytest.raises(ValueError):
        parse_entry_legs("buy:call:100:1")  # the entry premium is required


def test_fno_iv_expiry_and_spot(monkeypatch):
    series = [(date(2026, 1, 1) + timedelta(days=i), 10.0 + i * 0.1, None) for i in range(59)]

    async def exps(sym):
        return [date(2026, 9, 29), date(2026, 10, 6), date(2026, 10, 27)]

    async def ch(sym, exp):
        return chain(underlying=25012.5)

    monkeypatch.setattr(compute, "SOURCES", src(iv_series=lambda s: series, fno_expiries=exps, fno_chain=ch))
    short = run(reader().read("fno", "iv_percentile", "NIFTY", {}, {}))
    assert short.value is None and "insufficient history" in short.note  # 59 days: no percentile yet
    series.append((date(2026, 3, 1), 12.0, None))  # 60th day: 12.0 is above 20 of the 59 earlier values
    assert run(reader().read("fno", "iv_percentile", "NIFTY", {}, {})).value == Decimal("33.9")
    assert run(reader().read("fno", "atm_iv", "NIFTY", {}, {})).value == Decimal("12.0")
    assert run(reader().read("fno", "days_to_expiry", "NIFTY", {}, {})).value == 6  # past expiries skipped
    assert run(reader().read("fno", "spot", "NIFTY", {}, {})).value == Decimal("25012.5")


def test_source_failure_is_unknown_not_a_crash(monkeypatch):
    async def down(key):
        raise httpx.ConnectError("NSE refused")

    monkeypatch.setattr(compute, "SOURCES", src(stock_quote=down))
    r = run(reader().read("stock", "price", "INFY", {}, {}))
    assert r.value is None and "ConnectError" in r.note


# --------------------------------------------------------------------------- engine (database)
@pytest.fixture
def clean(env):
    from finresearch.db import session_scope
    from finresearch.db.models import (
        Alert,
        AlertDelivery,
        AlertEvalSlot,
        AlertRuleState,
        InvestorProfile,
        MonitorJob,  # fmt: skip
        NotificationSetting,
        Watch,
    )

    with session_scope() as s:
        for m in (AlertDelivery, Alert, AlertRuleState, AlertEvalSlot, NotificationSetting, MonitorJob, Watch,
                  InvestorProfile):  # fmt: skip
            s.query(m).delete()
    return env


def stock_watch(s, slug="alrt-infy", sym="INFY"):
    from finresearch.db.models import Watch
    from finresearch.ingest.documents import get_or_create_company

    co = get_or_create_company(s, slug, sym)
    co.nse_symbol = sym
    s.flush()
    w = Watch(company_id=co.id, kind="stock", nse_symbol=sym, exchange="NSE", meta={}, active=True)
    s.add(w)
    s.flush()
    return w


def test_fire_once_clear_cooldown_and_edit_reset(clean, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertRuleState

    price = {"v": Decimal(1450)}

    async def q(key):
        return quote(last_price=price["v"])

    monkeypatch.setattr(compute, "SOURCES", src(stock_quote=q))
    rule = AlertRule(id="infy-buy", kind="stock", metric="price", op="<", value=Decimal(1400), instrument="INFY",
                     cooldown_h=Decimal(24))  # fmt: skip

    def step(v, at):
        price["v"] = Decimal(v)
        with session_scope() as s:
            return run(engine.evaluate(s, [rule], at))

    with session_scope() as s:
        w = stock_watch(s)
        wid = w.id
    t0 = NOW
    assert step(1450, t0)["clear"] == 1
    assert step(1390, t0 + timedelta(minutes=15))["fired"] == 1
    assert step(1380, t0 + timedelta(minutes=30))["still_fired"] == 1  # de-duplicated
    assert step(1410, t0 + timedelta(minutes=45))["clear"] == 1
    assert step(1395, t0 + timedelta(hours=2))["suppressed"] == 1  # inside the 24 h cooldown
    assert step(1420, t0 + timedelta(hours=3))["clear"] == 1
    assert step(1390, t0 + timedelta(hours=25))["fired"] == 1  # cooldown over
    with session_scope() as s:
        alerts = s.query(Alert).filter(Alert.kind == "rule_alert").order_by(Alert.id).all()
        assert len(alerts) == 2
        a = alerts[0]
        assert a.watch_id == wid and a.level == "warn"
        assert a.message == "INFY: price is ₹1,390 (is below your ₹1,400)"
        assert a.data["rule_id"] == "infy-buy" and a.data["path"] == "/stocks/INFY"
        assert a.data["rule"] == "INFY: price is below ₹1,400"
        st = s.query(AlertRuleState).one()
        assert (st.fired_count, st.suppressed_count, st.status) == (2, 1, "fired")
    edited = rule.model_copy(update={"value": Decimal(1300)})
    with session_scope() as s:
        price["v"] = Decimal(1390)
        assert run(engine.evaluate(s, [edited], t0 + timedelta(hours=26)))["clear"] == 1
        st = s.query(AlertRuleState).one()
        assert (st.fired_count, st.status) == (0, "clear")  # editing the rule started it afresh


def test_unknown_never_fires_and_all_watches_target(clean, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    async def q(key):
        if key == "TCS":
            raise httpx.ReadTimeout("slow")
        return quote(previous_close=Decimal(1500), last_price=Decimal(1400))

    monkeypatch.setattr(compute, "SOURCES", src(stock_quote=q))
    rule = AlertRule(id="big-fall", kind="stock", metric="day_change_pct", op="<=", value=Decimal(-5))
    with session_scope() as s:
        stock_watch(s, "alrt-infy", "INFY")
        stock_watch(s, "alrt-tcs", "TCS")
        res = run(engine.evaluate(s, [rule], NOW))
    assert (res["fired"], res["unknown"]) == (1, 1)
    with session_scope() as s:
        msgs = [a.message for a in s.query(Alert).filter(Alert.kind == "rule_alert")]
    assert msgs == ["INFY: move on the day is -6.67% (is at most your -5%)"]


def test_event_rule_fires_once_per_change(clean, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope

    actions = iter(["HOLD", "HOLD", "SELL", "SELL"])

    async def sig(asset, inst):
        return SimpleNamespace(action=next(actions), score=0.0)

    monkeypatch.setattr(compute, "SOURCES", src(signal=sig))
    rule = AlertRule(id="bond-sig", kind="bond", metric="signal_action_changed", instrument="INE000A07AB1",
                     cooldown_h=Decimal(0))  # fmt: skip
    results = []
    for i in range(4):
        with session_scope() as s:
            res = run(engine.evaluate(s, [rule], NOW + timedelta(days=i)))
            results.append(next(k for k in ("fired", "clear", "unknown", "still_fired") if res[k]))
    assert results == ["clear", "clear", "fired", "clear"]


def test_portfolio_metrics_skip_until_the_portfolio_exists(clean, monkeypatch):
    from finresearch.alerts import engine, portfolio
    from finresearch.db import session_scope

    rule = AlertRule(id="dd", kind="portfolio", metric="drawdown_pct", op="<=", value=Decimal(-10))
    # "not set up" is simulated, not taken from the shared test DB: other test modules leave portfolio rows behind,
    # which made this test depend on the order it ran in
    monkeypatch.setattr(portfolio, "SOURCE", None)
    monkeypatch.setattr(portfolio, "_default_source", lambda: None)
    with session_scope() as s:
        res = run(engine.evaluate(s, [rule], NOW))
    assert res["unknown"] == 1 and "not set up" in res["checks"][0]["note"]
    monkeypatch.setattr(
        portfolio, "SOURCE", lambda session: {"drawdown_pct": (Decimal("-12.5"), "test view")}
    )
    with session_scope() as s:
        res = run(engine.evaluate(s, [rule], NOW))
    assert res["fired"] == 1


def test_ipo_alert_reads_the_monitor_snapshot(clean):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.db.models import SubscriptionSnapshotRow

    with session_scope() as s:
        s.query(SubscriptionSnapshotRow).filter(SubscriptionSnapshotRow.nse_symbol == "ALRTIPO").delete()
        s.add(SubscriptionSnapshotRow(nse_symbol="ALRTIPO", as_of=NOW, source="nse_combined", total_times=Decimal(4),
                                      categories=[{"name": "Qualified Institutional Buyers", "code": "1", "times": "12.5"},
                                                  {"name": "QIB (ex anchor) sub", "code": "1(a)", "times": "99"}]))  # fmt: skip
    rule = AlertRule(
        id="q10", kind="ipo", metric="qib_times", op=">=", value=Decimal(10), instrument="ALRTIPO"
    )
    with session_scope() as s:
        res = run(engine.evaluate(s, [rule], NOW))
    assert res["fired"] == 1 and res["checks"][0]["value"] == "12.5"


def test_due_passes_are_market_hours_aware():
    from finresearch.alerts.engine import due_passes
    from finresearch.fincalc.dates import ist_datetime

    at = lambda d, h, m: ist_datetime(d, h, m)  # noqa: E731
    wed = date(2026, 9, 30)
    assert due_passes(at(wed, 9, 10), (16, 30), set()) == []
    assert due_passes(at(wed, 9, 20), (16, 30), set()) == [("intraday", "intraday:2026-09-30T09:15")]
    assert due_passes(at(wed, 15, 40), (16, 30), set()) == [("intraday", "intraday:2026-09-30T15:30")]
    assert due_passes(at(wed, 15, 50), (16, 30), set()) == []
    assert due_passes(at(wed, 18, 0), (16, 30), set()) == [("daily", "daily:2026-09-30")]
    assert due_passes(at(date(2026, 10, 3), 11, 0), (16, 30), set()) == []  # Saturday
    assert due_passes(at(wed, 11, 0), (16, 30), {wed}) == []  # trading holiday


def test_rules_step_claims_each_pass_once(clean, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import ist_datetime
    from finresearch.suggest.advisor import save_profile

    async def q(key):
        return quote(last_price=Decimal(1390))

    monkeypatch.setattr(compute, "SOURCES", src(stock_quote=q))
    with session_scope() as s:
        stock_watch(s)
        save_profile(s, Profile(alert_rules=[AlertRule(id="r", kind="stock", metric="price", op="<",
                                                       value=Decimal(1400), instrument="INFY")]))  # fmt: skip
    t = ist_datetime(date(2026, 9, 30), 10, 5)
    assert run(engine.rules_step(t, holidays=set())) == {
        "intraday": {"fired": 1, "suppressed": 0, "unknown": 0}
    }
    assert run(engine.rules_step(t + timedelta(minutes=5), holidays=set())) == {}  # same 10:00 pass: claimed


def test_a_failed_pass_releases_its_slot(clean, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.fincalc.dates import ist_datetime
    from finresearch.suggest.advisor import save_profile

    with session_scope() as s:
        save_profile(
            s, Profile(alert_rules=[AlertRule(id="r", kind="stock", metric="price", instrument="INFY")])
        )

    async def broken(*a, **k):
        raise RuntimeError("database blip")

    monkeypatch.setattr(engine, "evaluate", broken)
    t = ist_datetime(date(2026, 9, 30), 10, 5)
    with pytest.raises(RuntimeError):
        run(engine.rules_step(t, holidays=set()))
    with session_scope() as s:
        assert s.get(AlertEvalSlot, "intraday:2026-09-30T10:00") is None  # retried on the next tick


def test_no_rules_means_no_work(clean):
    from finresearch.alerts import engine

    assert run(engine.rules_step(NOW, holidays=set())) == {}


# --------------------------------------------------------------------------- delivery
# a made-up token, built at runtime so secret scanners don't mistake the fixture for a real one
TOKEN = "123456789" + ":" + "AAH-" + "x" * 31
TOPIC = "finresearch-verysecret-topic-1234"


def settings(s, **kw):
    from finresearch.monitor import notify

    notify.update(s, kw)


def test_settings_mask_secrets_and_keep_them_on_round_trip(clean):
    from finresearch.db import session_scope
    from finresearch.monitor import notify

    with session_scope() as s:
        out = notify.update(s, {"ntfy": {"enabled": True, "topic": TOPIC},
                                "telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "42"}})  # fmt: skip
        assert TOPIC not in json.dumps(out) and TOKEN not in json.dumps(out)
        assert (
            out["ntfy"]["topic"] == "••••1234"
            and out["ntfy"]["topic_set"]
            and out["ntfy"]["status"] == "ready"
        )
        # the page sends the masked values back with another change: the secrets are kept
        notify.update(s, {"ntfy": {**out["ntfy"], "server": "https://ntfy.example.org/"},
                          "telegram": out["telegram"]})  # fmt: skip
        assert (
            notify.load(s, "ntfy").topic == TOPIC
            and notify.load(s, "ntfy").server == "https://ntfy.example.org"
        )
        assert notify.load(s, "telegram").bot_token == TOKEN
        notify.update(s, {"telegram": {"clear_bot_token": True}})
        assert notify.load(s, "telegram").bot_token == ""
        with pytest.raises(ValueError):
            notify.update(s, {"ntfy": {"topic": "has spaces"}})


def test_ntfy_payload_priority_click_and_bearer(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.monitor import notify

    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"id": "x"})

    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(handler))
    with session_scope() as s:
        settings(s, ntfy={"enabled": True, "topic": TOPIC, "token": "tk_abcdefabcdefabcdefabcdefabcdef"},
                 general={"app_url": "http://192.168.1.5:3100"})  # fmt: skip
        run(notify.send(s, "ntfy", notify.Message("FinResearch · Price", "INFY: price is ₹1,390", "urgent",
                                                  "http://192.168.1.5:3100/stocks/INFY")))  # fmt: skip
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://ntfy.sh/"
    assert body["topic"] == TOPIC and body["priority"] == 5 and body["message"] == "INFY: price is ₹1,390"
    assert body["click"].endswith("/stocks/INFY") and body["actions"][0]["action"] == "view"
    assert seen[0].headers["authorization"] == "Bearer tk_abcdefabcdefabcdefabcdefabcdef"


def test_queue_retry_backoff_redaction_and_failure(clean, monkeypatch):
    from finresearch.alerts import engine
    from finresearch.db import session_scope
    from finresearch.db.models import AlertDelivery
    from finresearch.monitor import notify

    replies = [httpx.Response(429, json={"ok": False, "description": "Too Many Requests",
                                         "parameters": {"retry_after": 90}})]  # fmt: skip

    def handler(req: httpx.Request):
        if replies:
            return replies.pop(0)
        raise httpx.ConnectError(f"cannot reach {req.url}")  # the URL carries the bot token

    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(handler))

    async def q(key):
        return quote(last_price=Decimal(1390))

    monkeypatch.setattr(compute, "SOURCES", src(stock_quote=q))
    rule = AlertRule(id="r", kind="stock", metric="price", op="<", value=Decimal(1400), instrument="INFY",
                     channels=["telegram", "ntfy"], priority="high")  # fmt: skip
    with session_scope() as s:
        settings(s, telegram={"enabled": True, "bot_token": TOKEN, "chat_id": "42"})  # ntfy left unconfigured
        run(engine.evaluate(s, [rule], NOW))
        rows = s.query(AlertDelivery).all()
        assert [(d.channel, d.priority, d.status) for d in rows] == [("telegram", "high", "pending")]
    r1 = run(notify.deliver_due(NOW, spacing_s=0))
    assert r1["retry"] == 1
    with session_scope() as s:
        d = s.query(AlertDelivery).one()
        assert (
            d.next_try_at == NOW + timedelta(seconds=90) and "429" in d.last_error
        )  # retry_after beats 30 s
    t = NOW
    for _ in range(3):
        t += timedelta(minutes=10)
        run(notify.deliver_due(t, spacing_s=0))
    with session_scope() as s:
        d = s.query(AlertDelivery).one()
        assert d.status == "failed" and d.attempts == notify.MAX_ATTEMPTS
        assert TOKEN not in d.last_error and "[redacted]" in d.last_error


def test_quiet_hours_hold_non_urgent(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertDelivery
    from finresearch.fincalc.dates import ist_datetime
    from finresearch.monitor import notify
    from finresearch.suggest.advisor import save_profile
    from finresearch.suggest.profile import Preferences, WatchWindows

    sent = []
    monkeypatch.setattr(
        notify, "TRANSPORT", httpx.MockTransport(lambda r: sent.append(r) or httpx.Response(200))
    )
    night = ist_datetime(date(2026, 9, 30), 23, 30)
    with session_scope() as s:
        save_profile(
            s, Profile(preferences=Preferences(watch=WatchWindows(quiet_start="22:00", quiet_end="07:00")))
        )
        settings(s, ntfy={"enabled": True, "topic": TOPIC})
        for pr in ("default", "urgent"):
            a = Alert(kind="rule_alert", level="warn", message=pr, data={})
            s.add(a)
            s.flush()
            notify.queue(s, a, ["ntfy"], pr, title="t", message=pr, path="/", now=night)
    res = run(notify.deliver_due(night, spacing_s=0))
    assert (res["sent"], res["held"]) == (1, 1) and json.loads(sent[0].content)["message"] == "urgent"
    with session_scope() as s:
        held = s.query(AlertDelivery).filter(AlertDelivery.status == "pending").one()
        assert held.next_try_at == ist_datetime(date(2026, 10, 1), 7, 0) and "quiet hours" in held.held
    assert run(notify.deliver_due(ist_datetime(date(2026, 10, 1), 7, 1), spacing_s=0))["sent"] == 1


def test_quiet_until_golden():
    from finresearch.fincalc.dates import ist_datetime
    from finresearch.monitor.notify import quiet_until

    d = date(2026, 9, 30)
    assert quiet_until(ist_datetime(d, 23, 0), "22:00", "07:00") == ist_datetime(d + timedelta(days=1), 7, 0)
    assert quiet_until(ist_datetime(d, 6, 59), "22:00", "07:00") == ist_datetime(d, 7, 0)
    assert quiet_until(ist_datetime(d, 7, 0), "22:00", "07:00") is None
    assert quiet_until(ist_datetime(d, 13, 0), "12:00", "14:00") == ist_datetime(d, 14, 0)
    assert quiet_until(ist_datetime(d, 13, 0), None, None) is None


def test_macos_text_is_passed_as_arguments(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.monitor import notify

    calls = []

    async def fake_run(argv):
        calls.append(argv)
        return 0, ""

    monkeypatch.setattr(notify, "RUN", fake_run)
    monkeypatch.setattr(notify.sys, "platform", "darwin")
    evil = 'x" & do shell script "rm -rf ~" & "'
    with session_scope() as s:
        settings(s, macos={"enabled": True})
        run(notify.send(s, "macos", notify.Message("t", evil, "high")))
    argv = calls[0]
    assert argv[0] == "osascript" and evil in argv and evil not in argv[2]  # data, never script
    assert argv[-1] == "Glass"


def test_forwarding_monitor_alerts_starts_from_now(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertDelivery
    from finresearch.monitor import notify

    with session_scope() as s:
        s.add(Alert(kind="listing_open", level="action", message="old history", data={}))
        s.flush()
        settings(s, ntfy={"enabled": True, "topic": TOPIC}, general={"forward_level": "action",
                                                                     "forward_channels": ["ntfy"]})  # fmt: skip
        s.add(Alert(kind="listing_open", level="action", message="ABC listed", data={}))
        s.add(Alert(kind="results", level="info", message="info only", data={}))
    with session_scope() as s:
        assert notify.forward_monitor_alerts(s, NOW) == 1
        assert notify.forward_monitor_alerts(s, NOW) == 0  # the cursor moved
        assert [d.message for d in s.query(AlertDelivery)] == ["ABC listed"]


# --------------------------------------------------------------------------- API
def test_api_registry_notifications_and_test_send(clean, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api.app import create_app
    from finresearch.monitor import notify

    monkeypatch.setattr(notify, "TRANSPORT", httpx.MockTransport(lambda r: httpx.Response(500, text="down")))
    with TestClient(create_app()) as c:
        reg = c.get("/api/alert-rules/registry").json()
        assert {m["kind"] for m in reg["metrics"]} >= {"stock", "fund", "bond", "fno", "portfolio", "ipo"}
        r = c.put(
            "/api/notifications", json={"telegram": {"enabled": True, "bot_token": TOKEN, "chat_id": "42"}}
        )
        assert r.status_code == 200 and TOKEN not in r.text
        assert TOKEN not in c.get("/api/notifications").text
        t = c.post("/api/notifications/test", json={"channel": "telegram"}).json()
        assert t["status"] == "failed" and "HTTP 500" in t["last_error"] and TOKEN not in json.dumps(t)
        log = c.get("/api/notifications/deliveries").json()
        assert log[0]["test"] and log[0]["status"] == "failed"
        assert c.put("/api/notifications", json={"ntfy": {"topic": "bad topic"}}).status_code == 422
        prof = c.get("/api/profile").json()
        rule = {
            "id": "a",
            "kind": "stock",
            "metric": "price",
            "op": "<",
            "value": "1400",
            "instrument": "INFY",
        }
        assert c.put("/api/profile", json={**prof, "alert_rules": [rule, rule]}).status_code == 422
        ok = c.put("/api/profile", json={**prof, "alert_rules": [rule]})
        assert ok.status_code == 200 and ok.json()["alert_rules"][0]["instrument"] == "INFY"
        assert (
            c.put("/api/profile", json={**prof, "alert_rules": [{**rule, "metric": "nope"}]}).status_code
            == 422
        )


def test_bond_ytm_alert_reads_nse_price_as_dirty(monkeypatch):
    """NSE's CM-segment bond prices include accrued interest (the bond page and the signal read them so); the alert
    took them as clean and understated the YTM. Annual 8 % bond, maturity 31-Mar-2030, settlement 30-Sep-2026: 183 days
    accrued = 1000 x 0.08 x 183/365 = 40.1096, so a dirty 1040.1096 is a clean 1000. Hand bisection of
    1040.1096 = sum (80 [+1000]) / (1+y)^(182/365 + k), k = 0..3, gives y = 7.9740 %. Read as clean, the old code gave
    6.6561 % (the same price taken as a clean 104.01096 per 100)."""
    from finresearch.adapters.nse_bonds import ListedBond

    b = ListedBond(symbol="EXM", series="N2", isin="INE000X07AB2", coupon_pct=Decimal(8), face_value=Decimal(1000),
                   last_price=Decimal("1040.1096"), close=None, maturity=date(2030, 3, 31),
                   next_interest_date=date(2027, 3, 31), rating="AA", rating_agency="CRISIL",
                   traded_value=Decimal(500000), as_of=None)  # fmt: skip

    async def bonds():
        return [b]

    async def freq(isin):
        return None

    monkeypatch.setattr(compute, "SOURCES", src(bonds=bonds, bond_freq=freq))
    y = run(reader().read("bond", "ytm_pct", "INE000X07AB2", {}, {}))
    assert abs(y.value - Decimal("7.9740")) < Decimal("0.0005"), y
    assert "dirty price" in y.source and "accrued" in y.source


def test_bond_ytm_alert_unknown_when_no_yield_fits(monkeypatch):
    """A price no yield in -50 %..100 % explains is unknown, not the solver's 100 % bound."""
    from finresearch.adapters.nse_bonds import ListedBond

    b = ListedBond(symbol="EXM", series="N3", isin="INE000X07AB3", coupon_pct=Decimal(9), face_value=Decimal(1000),
                   last_price=Decimal(10), close=None, maturity=date(2030, 3, 31),
                   next_interest_date=date(2027, 3, 31), rating="AA", rating_agency="CRISIL",
                   traded_value=Decimal(500000), as_of=None)  # fmt: skip

    async def bonds():
        return [b]

    async def freq(isin):
        return None

    monkeypatch.setattr(compute, "SOURCES", src(bonds=bonds, bond_freq=freq))
    y = run(reader().read("bond", "ytm_pct", "INE000X07AB3", {}, {}))
    assert y.value is None and "no yield" in (y.note or "")
