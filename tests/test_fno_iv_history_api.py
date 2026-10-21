"""IV rank and percentile from the bhavcopy history (#306, closes the 60-day wait of #117): source selection, the
/api/fno/{symbol}/iv payload and the signal's IV-context gate. Test database only."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from finresearch.signals import fno as fs

DAY0 = date(2026, 6, 1)


@pytest.fixture
def client(env):
    from finresearch.api import create_app
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_iv_eod, iv_history"))
    with TestClient(create_app()) as c:
        yield c
    with session_scope() as s:  # the database is shared by the session: leave no IV rows for other tests
        s.execute(text("TRUNCATE fno_iv_eod, iv_history"))


def add_eod(n: int, iv=lambda i: 15.0 + (i % 10)) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod

    with session_scope() as s:
        for i in range(n):
            s.add(FnoIvEod(symbol="NIFTY", day=DAY0 + timedelta(days=i), iv_30d=Decimal(str(iv(i))),
                           near_iv=Decimal("14.000"), next_iv=Decimal("15.500"), near_expiry=date(2026, 10, 13),
                           next_expiry=date(2026, 10, 27), skew_25d=Decimal("2.100"), rv_20=Decimal("11.200"),
                           iv_30d_method="interpolated", forward=Decimal("22707.30"), forward_source="future",
                           rate=Decimal("0.065")))  # fmt: skip


def add_recorded(n: int) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import IvHistory

    with session_scope() as s:
        for i in range(n):
            s.add(IvHistory(symbol="NIFTY", day=DAY0 + timedelta(days=i), expiry=date(2026, 10, 6),
                            atm_iv=Decimal(10 + i % 20), skew_25d=Decimal("1.5")))  # fmt: skip


def test_short_bhavcopy_history_falls_back_to_the_recorded_series(client):
    add_eod(59)
    add_recorded(61)
    iv = client.get("/api/fno/NIFTY/iv").json()
    assert (iv["source_kind"], iv["n"], iv["term"]) == ("recorded", 61, None)
    # #354 b: 61 recorded days are 24 % of a 252-session window, so no rank yet (was "ok" over 60+ days)
    assert iv["status"].startswith("insufficient coverage") and iv["rank"] is None
    assert iv["source"] == "https://www.nseindia.com/option-chain"


def test_bhavcopy_history_gives_rank_and_percentile_at_once(client):
    # 300 sessions (#354 b: the rank reads the last 252 and needs 80 % of them): IV cycles 15..24, the last session's
    # 12.0 is below every earlier one -> rank 0, percentile 0
    add_eod(300, iv=lambda i: 12.0 if i == 299 else 15.0 + (i % 10))
    add_recorded(5)  # a short recorded series is ignored, never spliced in
    iv = client.get("/api/fno/NIFTY/iv").json()
    assert (iv["source_kind"], iv["n"], iv["status"], iv["current"]) == ("bhavcopy", 252, "ok", 12.0)
    assert (iv["rank"], iv["percentile"], iv["low"], iv["high"]) == (0.0, 0.0, 12.0, 24.0)
    assert iv["as_of"] == (DAY0 + timedelta(days=299)).isoformat() and iv["rv_20"] == 11.2
    assert (
        iv["term"]["near_iv"] == 14.0
        and iv["term"]["next_iv"] == 15.5
        and iv["term"]["forward_source"] == "future"
    )
    assert iv["series"][-1] | {} == {"day": iv["as_of"], "atm_iv": 12.0, "skew_25d": 2.1, "near_iv": 14.0,
                                     "next_iv": 15.5, "rv_20": 11.2}  # fmt: skip
    assert "bhavcopy" in iv["method"] and "Black-76" in iv["method"]
    rows, src = fs.iv_series_info("nifty")
    assert src["source"] == "bhavcopy" and len(rows) == 300


def test_iv_gate_says_where_the_percentile_comes_from(env):
    import json
    from pathlib import Path

    from finresearch.adapters.nse_fno import parse_chain, parse_index_history

    fno = Path(__file__).parent / "fixtures" / "nse" / "fno"
    ch = parse_chain(
        "NIFTY", date(2026, 10, 6), json.loads((fno / "option_chain_NIFTY_20261006_trimmed.json").read_text())
    )
    closes = [float(c) for _, c in parse_index_history(json.loads(
        (fno / "indices_history_NIFTY50_202606_202609.json").read_text()))]  # fmt: skip
    a = fs.analyse(ch, 65, fs.preset_legs(ch, "iron_condor"), today=date(2026, 9, 29), closes=closes,
                   iv_series=[*([20.0] * 70), 13.36], capital=1_000_000, max_loss_pct=2, brokerage=20,
                   iv_source={"source": "bhavcopy"}, iv_as_of=date(2026, 9, 28))  # fmt: skip
    assert a["iv_context"]["source"] == "bhavcopy" and a["iv_context"]["as_of"] == "2026-09-28"
    _, factors, failed = fs.assess(a)
    gate = next(f for f in factors if f.name == "IV context fits the strategy")
    assert "IV context fits the strategy" in failed and "bhavcopy" in json.dumps(gate.__dict__, default=str)
