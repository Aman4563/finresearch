"""Backtest storage, the database-backed history (read-only) and the /api/fno/backtests route (#307). Test database
only, filled with synthetic rows (tests/fno_synth.py)."""

from __future__ import annotations

import json
from datetime import date

import fno_synth as fs
import pytest
from sqlalchemy import insert, text

TABLES = "fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract, fno_backtest_result, fno_backtest_run"


@pytest.fixture
def bt_db(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES} CASCADE"))


def store_days(days) -> None:
    """Write synthetic Days as fno_bhav rows (options IDO, futures IDF) the way the #306 loader stores them."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav

    rows = []
    for x in days:
        for (e, right, k), q in x.options.items():
            rows.append(dict(day=x.day, symbol=x.symbol, instrument="IDO", expiry=e, strike=k,
                             option_type="CE" if right == "call" else "PE", open=q.open, high=q.high, low=q.low,
                             close=q.close, settle=q.settle,
                             oi=q.oi, contracts=q.contracts, underlying=x.spot, lot_size=q.lot))  # fmt: skip
        for e, f in x.futures.items():
            rows.append(dict(day=x.day, symbol=x.symbol, instrument="IDF", expiry=e, strike=0, option_type="XX",
                             close=f, settle=f, oi=1000, contracts=100, underlying=x.spot, lot_size=50))  # fmt: skip
    with session_scope() as s:
        s.execute(insert(FnoBhav), rows)


def test_db_history_reads_what_was_stored_read_only(bt_db):
    from sqlalchemy.exc import InternalError

    from finresearch.backtest.dbhistory import DbHistory
    from finresearch.db.models import FnoBhav

    px = fs.path(date(2025, 3, 3), date(2025, 3, 7))
    mem = fs.history(
        date(2025, 3, 3), date(2025, 3, 7), prices=px, opens={d: s * 1.01 for d, s in px.items()}
    )
    store_days(mem.days["NIFTY"].values())
    h = DbHistory()
    assert h.sessions("NIFTY", date(2025, 3, 1), date(2025, 3, 31)) == sorted(mem.days["NIFTY"])
    d = date(2025, 3, 5)
    got, want = h.day("NIFTY", d), mem.day("NIFTY", d)
    assert (
        got.options == want.options
        and got.futures == want.futures
        and got.spot == pytest.approx(want.spot, abs=0.005)
    )
    assert (
        got.iv30 is not None and abs(got.iv30 - 15.0) < 0.2
    )  # no derived series: computed from the day itself
    assert h.lot("NIFTY", date(2025, 3, 27), d) == 50
    q = next(
        q for q in got.options.values() if q.open is not None
    )  # #382: the day's open, high and low are read
    assert q.high == max(q.open, q.close) and q.low == min(q.open, q.close)
    with pytest.raises(InternalError), h._s() as s:  # every session is READ ONLY
        s.execute(
            insert(FnoBhav).values(day=d, symbol="X", instrument="IDF", expiry=d, strike=0, option_type="XX")
        )


def test_report_is_saved_and_served_with_candidates_setup(bt_db):
    from starlette.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.backtest import evaluate
    from finresearch.backtest import store as bstore
    from finresearch.db import session_scope

    mem = fs.history(date(2023, 6, 1), date(2025, 1, 31))
    rep = json.loads(json.dumps(evaluate.evaluate(mem, stock=None, source="synthetic"), default=str))
    by = {r["key"]: r for r in rep["strategies"]}
    assert by["nifty_pcs_m"]["status"] == "fails"
    by["nifty_pcs_m"]["status"] = "candidate"  # pretend it passed, to exercise the candidate path
    with session_scope() as s:
        run_id = bstore.save(s, rep)
    store_days(
        [mem.day("NIFTY", date(2025, 1, 15))]
    )  # the app's F&O history: one close, no derived IV series
    with TestClient(create_app()) as c:
        body = c.get("/api/fno/backtests").json()
        assert body["registration"]["matches_code"] is True and len(body["registration"]["specs"]) == 8
        run = body["run"]
        assert run["id"] == run_id and run["oos_evaluated"] is True
        assert {s["key"]: s["status"] for s in run["strategies"]}["stock_cc_m"] == "no_data"
        (cand,) = run["candidates"]
        assert (
            cand["key"] == "nifty_pcs_m"
            and cand["oos"]["trades"] == by["nifty_pcs_m"]["windows"]["oos"]["trades"]
        )
        # IV percentile needs a year of stored IVs: unknown, so the setup says wait (never a default "enter")
        assert cand["setup"]["status"] == "wait" and "IV percentile unknown" in cand["setup"]["reason"]
        tr = c.get(f"/api/fno/backtests/{run_id}/nifty_dcs_m/trades").json()
        assert len(tr["trades"]) == len(by["nifty_dcs_m"]["trades"])
        assert c.get(f"/api/fno/backtests/{run_id}/nope/trades").status_code == 404


def test_no_run_saved_yet(bt_db):
    from starlette.testclient import TestClient

    from finresearch.api import create_app

    with TestClient(create_app()) as c:
        body = c.get("/api/fno/backtests").json()
    assert body["run"] is None and body["registration"]["variants_tried"] == 8
