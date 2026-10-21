"""margin_approx reads NSE's SPAN scan ranges (#370): sources labelled per assumption, the API and the backtest use
the day's file when one is stored, and every Groww comparison is logged for the P4.3 trigger (MF16)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from test_fno_book import SPREAD, TRADE_DAY, api, at_ist, book_db, offline  # noqa: F401

from finresearch.fincalc import margin_approx as ma

SHORT_PUT = [ma.MarginLeg("put", 23000, -75, 80.0, 0.15)]


def test_file_ranges_replace_the_assumptions_and_say_so():
    base = ma.estimate(SHORT_PUT, 23146.15, 10 / 365, 0.065, "index")
    span = ma.estimate(SHORT_PUT, 23146.15, 10 / 365, 0.065, "index", params={"price_scan": 0.093, "vol_scan": 0.04},
                       sources={"price_scan": "NSE SPAN file 2026-10-16", "vol_scan": "NSE SPAN file 2026-10-16"})  # fmt: skip
    assert span.scan_risk > base.scan_risk  # a 9.3 % range instead of the assumed 6 %
    j = ma.to_json(span, "index")
    assert j["assumptions"]["price_scan"] == {"value": 0.093, "status": "NSE SPAN file 2026-10-16"}
    assert j["assumptions"]["exposure"]["status"] == "unconfirmed"  # ELM is not in the file
    assert "NSE SPAN" in j["label"]
    assert ma.to_json(base, "index")["assumptions"]["price_scan"]["status"] == "unconfirmed"


def test_margin_check_summary_applies_the_mf16_trigger():
    rows = [(100.0, 100.0), (104.0, 100.0), (90.0, 100.0), (101.0, 100.0)]  # |diff| / groww: 0, 4, 10, 1 %
    s = ma.check_summary(rows)
    # median of [0, 1, 4, 10] % = 2.5 %; 95th percentile (nearest rank) = 10 %
    assert (
        s["n"] == 4 and s["median_abs_pct"] == pytest.approx(2.5) and s["p95_abs_pct"] == pytest.approx(10.0)
    )
    assert s["build_full_engine"] is False  # median <= 5 % and p95 <= 15 %
    assert ma.check_summary([(130.0, 100.0)] * 3)["build_full_engine"] is True
    assert ma.check_summary([])["build_full_engine"] is None  # no evidence: unknown, not "no"


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE span_param, span_day, margin_check"))

    wipe()
    yield env
    wipe()


def _store_nifty(day: date, price: str = "24950", scan: str = "2320.35") -> None:
    from decimal import Decimal

    from finresearch.db import session_scope
    from finresearch.db.models import SpanParam

    with session_scope() as s:
        s.add(SpanParam(day=day, symbol="NIFTY", price=Decimal(price), price_scan=Decimal(scan),
                        vol_scan=Decimal("0.04"), som_rate=Decimal(0), spreads=[]))  # fmt: skip


def test_the_margin_api_uses_the_stored_span_ranges(clean, api):  # noqa: F811
    _store_nifty(date(2026, 10, 9))  # the Friday before TRADE_DAY: 2320.35 / 24950 = 9.3 %
    body = {k: SPREAD[k] for k in ("symbol", "expiry", "legs")}
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/margin", json=body).json()
    a = r["assumptions"]
    assert a["price_scan"]["status"] == "NSE SPAN file 2026-10-09"
    assert a["price_scan"]["value"] == pytest.approx(0.093)
    assert a["exposure"]["status"] == "unconfirmed"


def test_the_margin_api_without_a_stored_file_keeps_the_labelled_assumptions(clean, api):  # noqa: F811
    _store_nifty(date(2026, 9, 1))  # too old for 12-Oct
    body = {k: SPREAD[k] for k in ("symbol", "expiry", "legs")}
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/margin", json=body).json()
    assert all(v["status"] == "unconfirmed" for v in r["assumptions"].values())


def test_every_groww_answer_is_logged_against_the_estimate(clean, api, monkeypatch):  # noqa: F811
    from finresearch.adapters import groww_market as gm
    from finresearch.db import session_scope
    from finresearch.db.models import MarginCheck

    _store_nifty(date(2026, 10, 9))

    async def ok(orders, segment="FNO"):
        return {"span_required": 61000.0, "exposure_required": 28125.5, "option_buy_premium": 8925.0,
                "brokerage_and_charges": 40.0, "total_requirement": 98090.5}  # fmt: skip

    monkeypatch.setattr(gm.MARKET, "basket_margin", ok, raising=False)
    body = {k: SPREAD[k] for k in ("symbol", "expiry", "legs")}
    with api(at_ist(TRADE_DAY, 10)) as c:
        r = c.post("/api/fno/margin", json=body).json()
    with session_scope() as s:
        (row,) = s.query(MarginCheck).all()
        assert row.symbol == "NIFTY" and row.span_day == date(2026, 10, 9)
        assert float(row.groww_margin) == pytest.approx(61000.0 + 28125.5)  # span + exposure, like for like
        assert float(row.approx_margin) == pytest.approx(r["scan_risk"] + r["exposure"], abs=0.01)


def test_backtest_margin_uses_the_days_file_ranges_when_the_history_has_them():
    from finresearch.backtest import engine, specs
    from finresearch.backtest.history import Clocked, LookAheadError, MemoryHistory

    spec = next(s for s in specs.SPECS if s.kind == "put_credit")
    legs = [
        engine.Leg("put", 23000.0, "sell", 75, 80.0, 0.15, 80.0),
        engine.Leg("put", 22800.0, "buy", 75, 50.0, 0.16, 50.0),
    ]
    base = engine.margin(spec, "NIFTY", legs, 23146.15, 10 / 365, specs.BOOK)
    wide = engine.margin(spec, "NIFTY", legs, 23146.15, 10 / 365, specs.BOOK,
                         scan={"params": {"price_scan": 0.093, "vol_scan": 0.04}, "sources": {}})  # fmt: skip
    assert wide > base

    class WithSpan(MemoryHistory):
        def span_params(self, symbol, d):
            return {"params": {"price_scan": 0.093}, "sources": {}, "day": d}

    h = Clocked(WithSpan([]))
    h.advance(date(2026, 10, 16))
    assert h.span_params("NIFTY", date(2026, 10, 16))["params"]["price_scan"] == 0.093
    with pytest.raises(LookAheadError):
        h.span_params("NIFTY", date(2026, 10, 19))
    plain = Clocked(MemoryHistory([]))
    plain.advance(date(2026, 10, 16))
    assert plain.span_params("NIFTY", date(2026, 10, 16)) is None  # no ranges: the labelled assumptions


def test_db_history_reads_span_ranges_only_when_asked(clean):
    from finresearch.backtest.dbhistory import DbHistory

    _store_nifty(date(2026, 9, 15))  # a monthly sample: used for the following weeks
    assert DbHistory().span_params("NIFTY", date(2026, 10, 12)) is None
    got = DbHistory(span_ranges=True).span_params("NIFTY", date(2026, 10, 12))
    assert got["day"] == date(2026, 9, 15) and got["params"]["price_scan"] == pytest.approx(0.093)


def test_cli_span_backfill_and_margin_check(clean, monkeypatch):
    from decimal import Decimal

    from typer.testing import CliRunner

    from finresearch import cli
    from finresearch.db import session_scope
    from finresearch.db.models import MarginCheck
    from finresearch.fnohist import span

    seen: dict = {}

    async def fake_backfill(start, end=None, **kw):
        seen.update(start=start, end=end, **kw)
        return {"planned": 0, "loaded": 0, "no_file": 0, "failed": {}}

    monkeypatch.setattr(span, "backfill", fake_backfill)
    r = CliRunner().invoke(cli.app, ["fno", "span", "backfill", "--from", "2008-01-01", "--to", "2008-12-31"])
    assert r.exit_code == 0, r.output
    assert seen["start"] == date(2008, 1, 1) and seen["monthly"] is True and seen["keep_raw"] is False
    with session_scope() as s:
        for a in (100, 104):
            s.add(MarginCheck(symbol="NIFTY", expiry=date(2026, 10, 27), kind="index", legs=[],
                              approx_margin=Decimal(a), groww_margin=Decimal(100)))  # fmt: skip
    r = CliRunner().invoke(cli.app, ["fno", "span", "margin-check"])
    assert r.exit_code == 0, r.output
    assert '"n": 2' in r.output and '"build_full_engine": false' in r.output
