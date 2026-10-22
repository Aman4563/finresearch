"""Citable option-implied facts as read-only MCP tools (#393): the implied move, the IV30 percentile and the 25-delta
skew read from the stored F&O bhavcopy (fno_bhav) and its derived series (fno_iv_eod), each returned with its source,
trade date, expiry, strikes and method version, and re-run by save_claim (grade D) when a claim cites it. Synthetic
chains only: prices come from the Black-76 engine at a known volatility, so the expected values are independent."""

from __future__ import annotations

import ast
import math
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

DAY = date(2026, 3, 2)
NEAR = date(2026, 3, 30)  # 28 days
WEEKLY = date(2026, 3, 5)  # 3 days: below MIN_DTE, never the default expiry
FWD = 22000.0
SIGMA = 0.15
RATE = 0.065  # fincalc.fno_history.RATE, the convention fno_iv_eod is computed with
URL = "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20260302_F_0000.csv.zip"


@pytest.fixture(autouse=True)
def _clean(request):
    """Each test starts with no stored F&O rows (the test database is shared by the session)."""
    if "env" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("env")
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod"))
    yield


def _smile(k: float, skew: float) -> float:
    return SIGMA + skew * (FWD - k) / FWD  # puts (low strikes) richer when skew > 0


def _price(right: str, k: float, expiry: date, skew: float = 0.0) -> D:
    from finresearch.fincalc import black76

    t = (expiry - DAY).days / 365
    p = black76.price(right, FWD, k, t, math.exp(-RATE * t), _smile(k, skew))
    return D(str(round(p, 2)))


def _load(symbol: str = "NIFTY", skew: float = 0.0, day: date = DAY, instrument: str = "IDO") -> None:
    """One trading day of a synthetic chain: a future and options on NEAR, a few options on WEEKLY."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay

    fut = "IDF" if instrument == "IDO" else "STF"
    rows = [FnoBhav(day=day, symbol=symbol, instrument=fut, expiry=NEAR, strike=D(0), option_type="XX",
                    settle=D(str(FWD)), close=D(str(FWD)), contracts=5000, oi=10**6, underlying=D("21900"))]  # fmt: skip
    for expiry, strikes in ((NEAR, range(19000, 25001, 100)), (WEEKLY, range(21800, 22201, 100))):
        for k in strikes:
            for right, ot in (("call", "CE"), ("put", "PE")):
                px = _price(right, k, expiry, skew)
                if px < D("0.05"):
                    continue
                rows.append(FnoBhav(day=day, symbol=symbol, instrument=instrument, expiry=expiry, strike=D(k),
                                    option_type=ot, settle=px, close=px, contracts=100, oi=10**5,
                                    underlying=D("21900")))  # fmt: skip
    with session_scope() as s:
        s.merge(FnoBhavDay(day=day, status="loaded", format="udiff", url=URL, stock_symbols=["ZZSECRET"]))
        s.add_all(rows)


def _run(tmp_path, symbol: str | None = None) -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        co = get_or_create_company(s, "of-" + tmp_path.name[-8:], "Synthetic Options Co")
        co.nse_symbol = symbol
        r = ResearchRun(company_id=co.id, kind="stock_report", manifest={})
        s.add(r)
        s.flush()
        return r.id


def _call(tool: str, **args):
    from finresearch.db import session_scope
    from finresearch.mcp_server import option_facts

    with session_scope() as s:
        return option_facts.run(s, tool, args)


# --------------------------------------------------------------------------- the implied move (golden)
def test_implied_move_golden_from_a_chain_fixture(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02")
    assert out["known"] is True, out
    t = 28 / 365
    # independent: the ATM 1-sigma move is sigma sqrt(T); the ATM straddle on the forward is
    # DF F (2 N(sigma sqrt(T) / 2) - 1) x 2, i.e. as a share of F: 2 DF erf(sigma sqrt(T) / (2 sqrt 2))
    assert float(out["move_1sd_pct"]) == pytest.approx(SIGMA * math.sqrt(t) * 100, abs=0.005)
    straddle = 2 * math.exp(-RATE * t) * math.erf(SIGMA * math.sqrt(t) / (2 * math.sqrt(2))) * 100
    assert float(out["atm_straddle_pct"]) == pytest.approx(straddle, abs=0.005)
    assert float(out["atm_iv_pct"]) == pytest.approx(15.0, abs=0.02)
    p = out["provenance"]
    assert p["trade_date"] == "2026-03-02" and p["expiry"] == "2026-03-30" and p["days_to_expiry"] == 28
    assert p["dataset"].startswith("fno_bhav") and p["source_url"] == URL
    assert p["forward_source"] == "future" and p["strikes_used"] == [22000.0]
    assert p["method_version"] and "fno_history" in p["computation"]
    assert "ZZSECRET" not in str(out)  # never which stock underlyings are stored
    cite = out["citation"]["option_fact"]
    assert cite["tool"] == "option_implied_move"
    assert cite["args"] == {"symbol": "NIFTY", "trade_date": "2026-03-02", "expiry": "2026-03-30"}


def test_explicit_expiry_is_honoured_and_missing_data_is_unknown_with_a_reason(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", trade_date="2026-03-02", expiry="2026-03-05")
    assert out["provenance"]["expiry"] == "2026-03-05" and out["provenance"]["days_to_expiry"] == 3
    for args in ({"trade_date": "2026-03-03"}, {"trade_date": "2026-03-02", "expiry": "2026-04-27"}):
        out = _call("option_implied_move", run_id=run_id, symbol="NIFTY", **args)
        assert out["known"] is False and out["reason"]
        assert not any(k in out for k in ("move_1sd_pct", "atm_straddle_pct", "atm_iv_pct"))


def test_latest_trade_date_is_resolved_into_the_citation(env, tmp_path):
    _load()
    run_id = _run(tmp_path)
    out = _call("option_implied_move", run_id=run_id, symbol="NIFTY")
    assert out["citation"]["option_fact"]["args"]["trade_date"] == "2026-03-02"


# --------------------------------------------------------------------------- scope: indices and the run's own stock
def test_symbols_outside_the_run_are_refused_without_saying_why(env, tmp_path):
    _load(symbol="ZZSECRET", instrument="STO")
    other = _run(tmp_path, symbol="ACME")
    out = _call("option_implied_move", run_id=other, symbol="ZZSECRET", trade_date="2026-03-02")
    assert out["known"] is False and "not available to this run" in out["reason"]
    assert "track" not in out["reason"] and "hold" not in out["reason"] and "watch" not in out["reason"]
    own = _run(tmp_path / "own", symbol="ZZSECRET")
    assert _call("option_implied_move", run_id=own, symbol="ZZSECRET", trade_date="2026-03-02")["known"] is True


def test_index_list_matches_the_stored_index_options():
    from finresearch.fnohist.store import INDEX_OPTIONS
    from finresearch.mcp_server.option_facts import INDEXES

    assert tuple(INDEXES) == tuple(INDEX_OPTIONS)
