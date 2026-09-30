"""Forecast ledger and calibration: golden metric values, logging run verdicts, dedupe, resolvers, the monitor step
and the API."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from finresearch.evals import calibration as cal
from finresearch.fincalc.dates import IST


# ------------------------------------------------------------------------------------------------ golden metrics
def test_wilson_matches_the_roadmap_example_and_hand_values():
    lo, hi = cal.wilson(7, 10)  # roadmap §C.10: 7/10 gives about 40-89 %
    assert lo == pytest.approx(0.39677, abs=1e-4) and hi == pytest.approx(0.89222, abs=1e-4)
    lo, hi = cal.wilson(0, 5)  # centre = half-width = (z²/2n)/(1+z²/n) = 0.21724
    assert lo == 0.0 and hi == pytest.approx(0.43448, abs=1e-4)
    lo, hi = cal.wilson(5, 5)
    assert hi == 1.0 and lo == pytest.approx(1 - 0.43448, abs=1e-4)
    assert cal.wilson(0, 0) is None
    with pytest.raises(ValueError):
        cal.wilson(3, 2)


def test_brier_skill_log_loss_and_hit_rate_golden():
    p, o = [0.75, 0.65, 0.35, 0.55], [1, 0, 0, 1]
    # (0.0625 + 0.4225 + 0.1225 + 0.2025) / 4
    assert cal.brier(p, o) == pytest.approx(0.2025)
    assert cal.brier_reference(o) == pytest.approx(0.25)  # ō = 0.5
    assert cal.brier_skill(p, o) == pytest.approx(0.19)  # 1 - 0.2025 / 0.25
    expected = -(math.log(0.75) + math.log(0.35) + math.log(0.65) + math.log(0.55)) / 4
    assert cal.log_loss(p, o) == pytest.approx(expected) and expected == pytest.approx(0.591531, abs=1e-6)
    hits, calls, rate, ci = cal.hit_rate(p, o)
    assert (hits, calls, rate) == (3, 4, 0.75) and ci == pytest.approx(cal.wilson(3, 4))
    assert cal.brier([1.0, 0.0], [1, 0]) == 0.0 and cal.brier([0.5], [1]) == 0.25


def test_undefined_metrics_are_none_not_nan():
    assert cal.brier([], []) is None and cal.log_loss([], []) is None and cal.brier_skill([], []) is None
    assert cal.brier_skill([0.7, 0.6], [1, 1]) is None  # base rate 1: BS_ref = 0
    s = cal.summarize([], [])
    assert s.n == 0 and s.hit_rate is None and s.bins == [] and s.to_json()["hit_rate_ci"] is None
    assert cal.log_loss([1.0], [0]) == pytest.approx(-math.log(cal.EPS))  # clipped, finite
    assert cal.hit_rate([0.5, 0.5], [1, 0])[:3] == (0, 0, None)  # 0.5 is not a directional call
    with pytest.raises(ValueError):
        cal.brier([1.2], [1])
    with pytest.raises(ValueError):
        cal.brier([0.2], [2])


def test_reliability_bins_keep_ties_together():
    p = [0.55] * 4 + [0.65] * 3 + [0.75] * 3
    o = [1, 0, 0, 1, 1, 1, 0, 1, 1, 1]
    bins = cal.reliability_bins(p, o, n_bins=5)
    assert [(b.n, b.mean_p, b.observed) for b in bins] == [(4, 0.55, 0.5), (3, 0.65, pytest.approx(2 / 3)),
                                                            (3, 0.75, 1.0)]  # fmt: skip
    assert bins[0].ci == pytest.approx(cal.wilson(2, 4))
    spread = cal.reliability_bins([i / 10 for i in range(10)], [0, 0, 0, 0, 1, 0, 1, 1, 1, 1], n_bins=5)
    assert [b.n for b in spread] == [2, 2, 2, 2, 2] and spread[2].p_low == 0.4 and spread[2].observed == 0.5


# ------------------------------------------------------------------------------------------------ ledger
@dataclass
class Bar:
    day: date
    open: Decimal | None
    close: Decimal | None


@dataclass
class Action:
    subject: str
    ex_date: date | None
    dividend_per_share: Decimal | None = None


class FakeDeps:
    def __init__(self, bars: dict[str, list[Bar]] | None = None, actions: list[Action] | None = None):
        self.bars, self.actions, self.calls = bars or {}, actions or [], []
        self.forecasts, self.holidays, self.live_holidays = True, None, False

    async def price_history(self, symbol, start, end):
        self.calls.append((symbol, start, end))
        return [b for b in self.bars.get(symbol, []) if start <= b.day <= end]

    async def corporate_actions(self, symbol):
        return self.actions


@pytest.fixture
def ledger_db(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.monitor import scheduler

    scheduler._FORECASTS_CHECKED.clear()
    with session_scope() as s:
        s.query(Forecast).delete()
    yield tmp_path.name[-8:]
    scheduler._FORECASTS_CHECKED.clear()


def make_run(s, tag: str, kind: str, verdict: str, confidence: str, finished: datetime, symbol: str,
             listing: date | None = None, upper: str | None = "272") -> int:  # fmt: skip
    from finresearch.db.models import AgentStep, ResearchRun, Watch
    from finresearch.ingest.documents import get_or_create_company

    co = get_or_create_company(s, f"fc-{tag}-{symbol.lower()}", f"{symbol} Ltd")
    co.nse_symbol = symbol
    facts = {"issue_info": {"Price Range": f"Rs.258 to Rs.{upper}"}} if upper else {}
    run = ResearchRun(
        company_id=co.id, kind=kind, status="done", finished_at=finished, manifest={"facts": facts}
    )
    s.add(run)
    s.flush()
    key = "overall_verdict" if kind == "ipo_report" else "verdict"
    s.add(AgentStep(run_id=run.id, key="synthesis", stage="synthesis", status="done", finished_at=finished,
                    output={key: verdict, "confidence": confidence, "horizon": "3-5 years"}))  # fmt: skip
    if listing and s.scalar(select(Watch).where(Watch.company_id == co.id)) is None:
        s.add(
            Watch(
                company_id=co.id, kind="ipo", nse_symbol=symbol, listing_date=listing, meta={}, active=False
            )
        )
    s.flush()
    return run.id


def at(y, m, d, h=12) -> datetime:
    return datetime(y, m, d, h, tzinfo=IST)


def test_verdict_mapping_is_fixed_and_directional():
    from finresearch.signals.ledger import verdict_probability as vp

    assert vp("ipo_report", "APPLY", "high") == (0.75, "up")
    assert vp("ipo_report", "APPLY (listing gains only)", "low") == (0.55, "up")
    assert vp("ipo_report", "AVOID", "medium") == (0.35, "down")
    assert vp("ipo_report", "APPLY-CONDITIONAL", "medium") == (None, "no call")
    assert vp("stock_report", "ACCUMULATE", "medium") == (0.65, "up")
    assert vp("stock_report", "HOLD", "high") == (None, "no call")
    assert vp("fund_report", "INVEST", "high") is None and vp("bond_report", "BUY", "low") is None


def test_run_verdicts_are_logged_once_per_day_latest_run_wins(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals import ledger

    sym = f"IPO{ledger_db}".upper()[:20]
    with session_scope() as s:
        r1 = make_run(s, ledger_db, "ipo_report", "AVOID", "low", at(2026, 9, 28, 18), sym, date(2026, 10, 5))
        r2 = make_run(
            s, ledger_db, "ipo_report", "APPLY", "high", at(2026, 9, 28, 23), sym, date(2026, 10, 5)
        )
        f2 = ledger.record_run(s, r2)
        f1 = ledger.record_run(s, r1)  # older run on the same day: does not replace the newer call
        assert f1 == f2
        f = s.get(Forecast, f2)
        assert (f.run_id, f.probability, f.action, f.asset, f.instrument) == (r2, 0.75, "APPLY", "ipo", sym)
        assert f.resolve_on == date(2026, 10, 5) and f.inputs["issue_price"] == "272"
        assert f.validation_status == "uncalibrated" and f.event == ledger.IPO_EVENT
        # the backfill (oldest first) keeps the latest run, and is idempotent
        assert ledger.backfill_runs(s) and s.scalar(select(Forecast.run_id).where(Forecast.id == f2)) == r2
        n = s.query(Forecast).filter(Forecast.instrument == sym).count()
        ledger.backfill_runs(s)
        assert s.query(Forecast).filter(Forecast.instrument == sym).count() == n == 1
        # a run on another day is a separate forecast
        r3 = make_run(s, ledger_db, "ipo_report", "APPLY-CONDITIONAL", "medium", at(2026, 9, 29), sym)
        f3 = s.get(Forecast, ledger.record_run(s, r3))
        assert f3.id != f2 and f3.probability is None and f3.inputs["direction"] == "no call"


def test_stock_verdict_resolves_a_year_later_and_funds_are_skipped(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals import ledger

    with session_scope() as s:
        r = make_run(
            s, ledger_db, "stock_report", "REDUCE", "medium", at(2026, 9, 29, 4), "INFYX" + ledger_db[:3]
        )
        f = s.get(Forecast, ledger.record_run(s, r))
        assert f.asset == "stock" and f.probability == 0.35 and f.event_kind == "excess_return_12m"
        assert f.resolve_on >= date(2027, 9, 29) and f.inputs["start_date"] == "2026-09-29"
        assert f.inputs["report_horizon"] == "3-5 years"
        fund = make_run(s, ledger_db, "fund_report", "INVEST", "high", at(2026, 9, 29), "MF" + ledger_db[:3])
        assert ledger.record_run(s, fund) is None


def test_signal_record_dedupes_and_skips_no_signal(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals import Signal, Validation, ledger

    def sig(p, action="APPLY", when=None):
        when = when or at(2026, 9, 30, 10)
        return Signal(asset="ipo", instrument="SIGX", name="Sig", action=action, score=30, event=ledger.IPO_EVENT,
                      horizon="listing day", method="base rate by QIB band", validation=Validation("base_rate", 83),
                      probability=p, probability_interval=(p - 0.1, p + 0.1), as_of=when)  # fmt: skip

    with session_scope() as s:
        a = ledger.record(sig(0.7), source="signal:ipo", resolve_on=date(2026, 10, 5), event_kind="listing_gain",
                          session=s)  # fmt: skip
        b = ledger.record(sig(0.8, when=at(2026, 9, 30, 15)), source="signal:ipo", resolve_on=date(2026, 10, 5),
                          event_kind="listing_gain", session=s)  # fmt: skip
        assert a == b and s.get(Forecast, a).probability == 0.8 and s.get(Forecast, a).interval_high == 0.9
        assert ledger.record(sig(0.5, "NO_SIGNAL"), source="signal:ipo", resolve_on=date(2026, 10, 5),
                             event_kind="listing_gain", session=s) is None  # fmt: skip


async def test_ipo_forecast_resolves_from_the_watch_or_nse_history(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast, Watch
    from finresearch.signals import ledger

    a, b = ("WA" + ledger_db).upper()[:20], ("WB" + ledger_db).upper()[:20]
    with session_scope() as s:
        fa = ledger.record_run(s, make_run(s, ledger_db, "ipo_report", "APPLY", "medium", at(2026, 9, 28), a,
                                           date(2026, 10, 5)))  # fmt: skip
        s.scalar(select(Watch).where(Watch.nse_symbol == a)).meta = {"listing_open": "290.50"}
        fb = ledger.record_run(s, make_run(s, ledger_db, "ipo_report", "AVOID", "high", at(2026, 9, 28), b,
                                           None, upper="100"))  # fmt: skip
    deps = FakeDeps({b: [Bar(date(2026, 10, 1), Decimal("96"), Decimal("99"))]})
    # before the close on the listing date nothing is due
    assert (await ledger.resolve_due(deps, at(2026, 10, 5, 11)))["resolved"] == 0
    stats = await ledger.resolve_due(deps, at(2026, 10, 5, 17))
    assert stats["resolved"] == 2
    with session_scope() as s:
        x, y = s.get(Forecast, fa), s.get(Forecast, fb)
        assert (x.status, x.outcome) == ("resolved", 1) and x.resolution_value == pytest.approx(
            6.8015, abs=1e-3
        )
        assert "monitor" in x.resolution_note
        assert (y.outcome, y.probability) == (0, 0.25) and "NSE daily history" in y.resolution_note
        with pytest.raises(ValueError):
            ledger.resolve(s, fa, 0)  # a resolved forecast never changes


async def test_unlisted_ipo_waits_then_is_voided(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals import ledger

    sym = ("NL" + ledger_db).upper()[:20]
    with session_scope() as s:
        fid = ledger.record_run(s, make_run(s, ledger_db, "ipo_report", "APPLY", "low", at(2026, 9, 28), sym,
                                            date(2026, 10, 5)))  # fmt: skip
    deps = FakeDeps()
    assert (await ledger.resolve_due(deps, at(2026, 10, 6, 17)))["waiting"] == 1
    assert (await ledger.resolve_due(deps, at(2026, 10, 6, 18)))[
        "waiting"
    ] == 0  # checked at most every 6 hours
    with session_scope() as s:
        assert s.get(Forecast, fid).status == "open" and "waiting" in s.get(Forecast, fid).resolution_note
    assert (await ledger.resolve_due(deps, at(2026, 12, 10, 17)))["void"] == 1
    with session_scope() as s:
        assert s.get(Forecast, fid).status == "void" and s.get(Forecast, fid).outcome is None


def _series(sym_start: str, sym_end: str, d0: date, d1: date) -> list[Bar]:
    return [Bar(d0, None, Decimal(sym_start)), Bar(d1, None, Decimal(sym_end))]


async def test_stock_excess_return_counts_dividends_and_voids_on_splits(ledger_db):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals import ledger

    sym = ("ST" + ledger_db).upper()[:20]
    with session_scope() as s:
        fid = ledger.record_run(
            s, make_run(s, ledger_db, "stock_report", "BUY", "high", at(2026, 9, 29, 4), sym)
        )
        end = s.get(Forecast, fid).resolve_on
    d0 = date(2026, 9, 28)  # the last close on or before the report day
    bars = {sym: _series("1000", "1050", d0, end), "NIFTYBEES": _series("280", "300.8", d0, end)}
    deps = FakeDeps(bars, [Action("Dividend - Rs 25 Per Share", date(2027, 6, 1), Decimal("25")),
                           Action("Dividend - Rs 20 Per Share", date(2026, 6, 1), Decimal("20"))])  # fmt: skip
    now = datetime.combine(end, datetime.min.time(), tzinfo=IST) + timedelta(hours=17)
    assert (await ledger.resolve_due(deps, now))["resolved"] == 1
    with session_scope() as s:
        f = s.get(Forecast, fid)
        # stock (1050 + 25) / 1000 - 1 = 7.5 %; NIFTYBEES 300.8 / 280 - 1 = 7.43 %: excess +0.07 pp -> event happened
        assert f.outcome == 1 and f.resolution_value == pytest.approx(7.5 - 7.428571, abs=1e-4)
        assert "dividends ₹25" in f.resolution_note
        # a bonus in the window voids instead of scoring unadjusted prices
        f2 = ledger.record_run(
            s, make_run(s, ledger_db, "stock_report", "AVOID", "low", at(2026, 9, 30), sym + "B")
        )
    deps2 = FakeDeps(bars, [Action("Bonus 1:1", date(2027, 3, 1))])
    with session_scope() as s:
        f2row = s.get(Forecast, f2)
        end2 = f2row.resolve_on
    bars[sym + "B"] = _series("500", "260", date(2026, 9, 30), end2)
    bars["NIFTYBEES"] += [Bar(date(2026, 9, 30), None, Decimal("281")), Bar(end2, None, Decimal("301"))]
    now2 = datetime.combine(end2, datetime.min.time(), tzinfo=IST) + timedelta(hours=17)
    await ledger.resolve_due(deps2, now2)
    with session_scope() as s:
        g = s.get(Forecast, f2)
        assert g.status == "void" and "Bonus" in g.resolution_note


async def test_monitor_tick_logs_and_resolves_forecasts_once_an_hour(ledger_db, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast, Watch
    from finresearch.monitor import jobs, scheduler

    sym = ("MT" + ledger_db).upper()[:20]
    with session_scope() as s:
        s.query(Watch).update({"active": False})  # other tests' watches: no monitor jobs in this test
        rid = make_run(s, ledger_db, "ipo_report", "APPLY", "high", at(2026, 9, 28), sym, date(2026, 10, 5))
    deps = jobs.Deps(ipo_detail=None, quote=None, price_history=FakeDeps(
        {sym: [Bar(date(2026, 10, 5), Decimal("250"), Decimal("255"))]}).price_history, forecasts=True)  # fmt: skip
    out = await scheduler.tick(deps, at(2026, 10, 5, 17))
    assert out["resolved"] >= 1
    with session_scope() as s:
        f = s.scalar(select(Forecast).where(Forecast.run_id == rid))
        assert (f.status, f.outcome) == ("resolved", 0)  # opened at 250 below the 272 issue price
    assert "resolved" not in await scheduler.tick(deps, at(2026, 10, 5, 17))  # gated: once an hour
    off = jobs.Deps(ipo_detail=None, quote=None)
    assert off.forecasts is False  # tests and callers opt in; Deps.live() turns it on


def test_the_pipeline_hook_never_raises(monkeypatch):
    from finresearch.signals import ledger

    def boom(*a, **k):
        raise RuntimeError("no forecast table yet")

    monkeypatch.setattr(ledger, "record_run", boom)
    monkeypatch.setattr("finresearch.db.session_scope", _null_scope)
    assert ledger.record_run_safely(1) is None


class _null_scope:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def test_forecast_and_calibration_api(ledger_db):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.signals import ledger

    ids = []
    with session_scope() as s:
        for i, (p_verdict, conf, outcome) in enumerate([("APPLY", "high", 1), ("APPLY", "medium", 0),
                                                        ("AVOID", "medium", 0), ("APPLY", "low", 1),
                                                        ("APPLY-CONDITIONAL", "low", 1)]):  # fmt: skip
            sym = f"API{i}{ledger_db}".upper()[:20]
            fid = ledger.record_run(
                s, make_run(s, ledger_db, "ipo_report", p_verdict, conf, at(2026, 9, 20 + i), sym)
            )
            ledger.resolve(s, fid, outcome, value=1.0, note="test")
            ids.append(fid)
        ledger.record_run(s, make_run(s, ledger_db, "ipo_report", "APPLY", "high", at(2026, 9, 29), "OPEN" + ledger_db[:5],
                                      date(2026, 10, 5)))  # fmt: skip
    with TestClient(create_app()) as c:
        r = c.get("/api/forecasts", params={"status": "resolved", "limit": 2})
        assert r.status_code == 200 and r.json()["total"] == 5 and len(r.json()["items"]) == 2
        assert (
            c.get("/api/forecasts", params={"status": "open"}).json()["items"][0]["resolve_on"]
            == "2026-10-05"
        )
        assert c.get("/api/forecasts", params={"status": "nope"}).status_code == 422
        cal_json = c.get("/api/calibration").json()
    g = next(x for x in cal_json["groups"] if x["asset"] == "ipo" and x["method"] == ledger.RUN_METHOD)
    # scored: 0.75->1, 0.65->0, 0.35->0, 0.55->1 (the conditional call is resolved but not scored)
    assert (g["n"], g["resolved"], g["no_call"], g["open"]) == (4, 5, 1, 1)
    assert g["brier"] == pytest.approx(0.2025) and g["brier_skill"] == pytest.approx(0.19)
    assert (g["hits"], g["calls"]) == (3, 4) and len(g["hit_rate_ci"]) == 2
    assert cal_json["groups"][-1]["asset"] == "all" and cal_json["groups"][-1]["n"] == 4
    assert cal_json["confidence_map"] == {"low": 0.55, "medium": 0.65, "high": 0.75}
    assert (
        cal_json["next_open"]["resolve_on"] == "2026-10-05" and cal_json["next_scored"]["probability"] == 0.75
    )
