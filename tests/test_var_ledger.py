"""#399: the portfolio's historical 1-day 95 % VaR logged daily to the forecast ledger (by the monitor), resolved on the
next day of the value history and backtested with Kupiec and Christoffersen once 250 days are logged. Synthetic series
only; expected values come from numpy, by hand, or from the independent horserace.scores implementation."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest

from finresearch.portfolio.series import Series

IST = timezone(timedelta(hours=5, minutes=30))
END = date(2026, 9, 30)  # a Wednesday


def _bdays(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _rets(n: int) -> list[float]:
    return [0.012 * math.sin(i * 1.3) + 0.0015 * math.cos(i * 0.4) for i in range(n)]


def _series(n_days: int = 262, end: date = END, complete_last: bool = True) -> Series:
    """A value series whose daily TWR returns are `_rets`, with ₹50,000 put in on day 5 (a flow, not a gain)."""
    days = _bdays(end, n_days)
    rs = _rets(n_days - 1)
    value, invested = [1_000_000.0], [1_000_000.0]
    for t, r in enumerate(rs, start=1):
        flow = 50_000.0 if t == 5 else 0.0
        value.append(value[-1] * (1 + r) + flow)
        invested.append(invested[-1] + flow)
    complete = [True] * n_days
    complete[-1] = complete_last
    return Series(days, value, invested, complete, fingerprint="fp")


def at(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST)


# --------------------------------------------------------------------------- pure
def test_daily_returns_remove_flows():
    from finresearch.portfolio.var_ledger import daily_returns

    ser = _series(20)
    got = daily_returns(ser)
    assert [d for d, _ in got] == ser.days[1:]
    assert [r for _, r in got] == pytest.approx(_rets(19), abs=1e-12)  # the ₹50,000 on day 5 is not a return


def test_forecast_is_the_historical_var_of_the_last_250_returns():
    from finresearch.portfolio.var_ledger import var_forecast

    ser = _series(262)  # 261 returns: the last 250 are used
    v, why = var_forecast(ser, at(END, 16, 0))
    assert why is None
    expected = -float(np.quantile(np.array(_rets(261)[-250:]), 0.05))  # numpy's linear interpolation
    assert v["inputs"]["var"] == pytest.approx(expected, abs=1e-6)
    assert v["inputs"]["as_of"] == END.isoformat() and v["inputs"]["n"] == 250
    assert v["inputs"]["window_start"] == ser.days[-250].isoformat()
    assert v["probability"] == 0.05 and v["asset"] == "portfolio" and v["instrument"] == "PORTFOLIO"
    assert v["resolve_on"] == date(2026, 10, 1)
    # the ledger holds no rupee figure and no holding: fractions and dates only
    assert v["name"] is None
    flat = repr(v)
    assert "inr" not in flat.lower() and all(f"{x:.0f}" not in flat for x in ser.value[-3:])


@pytest.mark.parametrize("ser, now, reason", [
    (_series(250), at(END, 16), "needs 250 daily returns"),  # 249 returns
    (_series(262), at(END, 15, 0), "before the close"),  # during the session
    (_series(262), at(END + timedelta(days=1), 10), "not today's close"),  # a rebuild the next morning: too late
    (_series(262, complete_last=False), at(END, 16), "incomplete"),
])  # fmt: skip
def test_no_forecast_without_a_full_window_or_outside_the_after_close_window(ser, now, reason):
    from finresearch.portfolio.var_ledger import var_forecast

    v, why = var_forecast(ser, now)
    assert v is None and reason in why


# --------------------------------------------------------------------------- the ledger (DB)
@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    def clean():
        with session_scope() as s:
            s.execute(text("TRUNCATE forecast, portfolio_txn, portfolio_holding, portfolio_setting, alert_eval_slot "
                           "CASCADE"))  # fmt: skip

    clean()
    yield env
    clean()  # later tests in the session (the risk API's logged backtest) expect no VaR rows


def _rows():
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    with session_scope() as s:
        out = list(s.scalars(select(Forecast).where(Forecast.asset == "portfolio").order_by(Forecast.id)))
        s.expunge_all()
        return out


def test_the_history_pass_logs_one_var_forecast_a_day_after_the_close(db):
    """The close and NAV passes both run history_pass: one row for the day, the later replacing the earlier; a
    rebuild the next morning (the outcome's session has opened) logs nothing."""
    import asyncio
    from types import SimpleNamespace

    from finresearch.monitor.portfolio_daily import history_pass
    from finresearch.portfolio.history import History

    ser = _series(262)

    async def pf_history(holdings, today):
        return History(days=ser.days, value=ser.value, invested=ser.invested, complete=ser.complete,
                       start_reason="synthetic")  # fmt: skip

    deps = SimpleNamespace(pf_history=pf_history)
    res = asyncio.run(history_pass(deps, END, now=at(END, 16, 0)))
    assert res["var"]["logged"] is True
    asyncio.run(history_pass(deps, END, now=at(END, 23, 0)))
    rows = _rows()
    assert len(rows) == 1 and rows[0].probability == 0.05 and rows[0].resolve_on == date(2026, 10, 1)
    assert rows[0].created_at == at(END, 23, 0)  # the NAV pass's (later) forecast replaced the close pass's
    res = asyncio.run(history_pass(deps, END, now=at(date(2026, 10, 1), 10, 0)))
    assert res["var"]["logged"] is False and "not today's close" in res["var"]["reason"]
    assert len(_rows()) == 1
    assert "var" not in asyncio.run(history_pass(deps, END))  # no clock given (old callers): nothing logged


def _store(ser):
    from finresearch.db import session_scope
    from finresearch.portfolio import cache, series

    with session_scope() as s:
        raw = {"days": [d.isoformat() for d in ser.days], "value": ser.value, "invested": ser.invested,
               "complete": ser.complete, "fingerprint": series.current_fingerprint(s), "built_on": END.isoformat()}  # fmt: skip
        cache.write(s, series.KEY, raw)


def _log(ser, now):
    from finresearch.db import session_scope
    from finresearch.portfolio.var_ledger import log_var

    with session_scope() as s:
        return log_var(s, ser, now)


def _resolve(now):
    import asyncio

    from finresearch.signals.ledger import resolve_due

    return asyncio.run(resolve_due(None, now, recheck=True))


def test_the_resolver_scores_the_next_day_and_waits_or_voids_honestly(db):
    from finresearch.portfolio.var_ledger import var_forecast

    full = _series(263, end=date(2026, 10, 1))  # Thu 1-Oct is the outcome day
    hist = Series(full.days[:-1], full.value[:-1], full.invested[:-1], full.complete[:-1], "fp")
    _store(hist)
    assert _log(hist, at(END, 16))["logged"] is True
    assert _resolve(at(date(2026, 10, 1), 17))["waiting"] == 1  # the history has no 1-Oct yet
    _store(full)
    _resolve(at(date(2026, 10, 1), 18))
    (f,) = _rows()
    r = _rets(262)[-1]  # the return on 1-Oct, by construction
    var = var_forecast(hist, at(END, 16))[0]["inputs"]["var"]
    assert f.status == "resolved" and f.resolution_value == pytest.approx(r, abs=1e-9)
    assert f.outcome == int(r < -var)


def test_an_incomplete_outcome_day_voids_the_forecast_never_no_exception(db):
    full = _series(263, end=date(2026, 10, 1), complete_last=False)
    hist = Series(full.days[:-1], full.value[:-1], full.invested[:-1], [True] * 262, "fp")
    _store(hist)
    _log(hist, at(END, 16))
    _store(full)
    _resolve(at(date(2026, 10, 1), 18))
    (f,) = _rows()
    assert f.status == "void" and f.outcome is None and "stale price" in f.resolution_note


def _resolved_rows(hits):
    """Resolved ledger rows with the given 0/1 exceptions on consecutive business days."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.portfolio import var_ledger as vl

    days = _bdays(END, len(hits))
    with session_scope() as s:
        for i, (d, h) in enumerate(zip(days, hits, strict=True)):
            s.add(Forecast(asset=vl.ASSET, instrument=vl.INSTRUMENT, source="monitor:portfolio",
                           event_kind=vl.EVENT_KIND, event=vl.EVENT, horizon="1 trading day", resolve_on=d,
                           probability=0.05, action="RISK", method=vl.METHOD, validation_status="rule_based",
                           inputs={"as_of": d.isoformat(), "var": 0.02}, status="resolved", outcome=h,
                           dedupe_key=f"t{i}"))  # fmt: skip


def test_the_logged_backtest_waits_for_250_resolved_days(db):
    from finresearch.db import session_scope
    from finresearch.portfolio.var_ledger import backtest

    _resolved_rows([0] * 249 + [1])
    with session_scope() as s:
        b = backtest(s)
    assert b["resolved"] == 250 and b["reason"] is None and b["days"] == 250 and b["exceptions"] == 1


def test_the_logged_backtest_matches_kupiec_and_christoffersen(db):
    """30 exceptions in 300 days, two of them back to back: the p-values come from horserace.scores (a separate
    implementation of the same tests)."""
    from finresearch.db import session_scope
    from finresearch.horserace import scores as sc
    from finresearch.portfolio.var_ledger import backtest

    hits = [1 if i % 10 == 3 else 0 for i in range(300)]
    hits[4] = 1
    hits[13] = 0  # still 30 exceptions, one pair clustered
    _resolved_rows(hits)
    with session_scope() as s:
        b = backtest(s)
    k, c = sc.kupiec(30, 300, 0.05), sc.christoffersen(hits)
    assert b["days"] == 300 and b["exceptions"] == 30 and b["expected"] == 15
    assert b["p_uc"] == pytest.approx(k["p"], abs=1e-4) and b["p_ind"] == pytest.approx(c["p"], abs=1e-4)
    assert b["p_cc"] == pytest.approx(math.exp(-(k["lr"] + c["lr"]) / 2), abs=1e-4)
    assert b["verdict"] == "too_many" and b["trigger"] is True  # p_cc < 0.05: #399's "on trigger" work starts


def test_too_few_logged_days_is_a_reason_not_a_verdict(db):
    from finresearch.db import session_scope
    from finresearch.portfolio.var_ledger import backtest

    _resolved_rows([0] * 120)
    with session_scope() as s:
        b = backtest(s)
    assert b["resolved"] == 120 and "needs 250" in b["reason"]
    assert b.get("verdict") is None and b.get("trigger") is None  # unknown, never "consistent"


def test_var_rows_stay_out_of_the_calibration_headlines(db):
    from finresearch.db import session_scope
    from finresearch.signals.ledger import HEADLINE, calibration_groups

    _resolved_rows([0, 1, 0])
    with session_scope() as s:
        groups = calibration_groups(s)
    assert not [g for g in groups if g["method"] in (HEADLINE, "all methods") and g["total"]]
    assert [g for g in groups if g["asset"] == "portfolio" and g["total"] == 3]  # still scored in its own row


def test_the_daily_var_never_becomes_the_calibration_pages_next_forecast(db):
    """A VaR row resolves every trading day: it would always be the 'next to resolve' on /api/calibration."""
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    with session_scope() as s:
        for i, (asset, on) in enumerate((("portfolio", date(2026, 10, 1)), ("ipo", date(2026, 10, 9)))):
            s.add(Forecast(asset=asset, instrument="EXAMPLE" if asset == "ipo" else "PORTFOLIO", source="t",
                           event_kind="listing_gain" if asset == "ipo" else "var95_1d", event="e", horizon="h",
                           resolve_on=on, probability=0.6 if asset == "ipo" else 0.05, action="A", method="m",
                           validation_status="rule_based", inputs={"as_of": "2026-09-30"}, status="open",
                           dedupe_key=f"n{i}"))  # fmt: skip
    with TestClient(create_app()) as c:
        out = c.get("/api/calibration").json()
    assert out["next_open"]["asset"] == "ipo" and out["next_scored"]["asset"] == "ipo"


def test_the_logged_backtest_counts_days_its_independence_test_skips(db):
    """Christoffersen's test reads consecutive rows as consecutive days; a missing logged day is counted, not hidden."""
    from sqlalchemy import delete

    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.portfolio.var_ledger import backtest

    _resolved_rows([0] * 260)
    with session_scope() as s:
        s.execute(delete(Forecast).where(Forecast.dedupe_key == "t100"))  # one business day not logged
        b = backtest(s)
    assert b["days"] == 259 and b["gaps"] == 1
