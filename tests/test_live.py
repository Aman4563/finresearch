"""Live data in session: NSE market hours (holiday aware), the fast quote and a watched IPO's live book."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.http import IST
from finresearch.adapters.nse import Quote
from finresearch.api.live import market_status, radar_ttl

HOLIDAYS = {date(2026, 10, 2): "Mahatma Gandhi Jayanti"}


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


def test_equity_session_and_ipo_bidding_hours():
    s = market_status(at(2026, 9, 29, 9, 14), HOLIDAYS)
    assert not s["equity"]["open"] and s["equity"]["next_open"] == "2026-09-29T09:15:00+05:30"
    s = market_status(at(2026, 9, 29, 9, 15), HOLIDAYS)
    assert s["equity"]["open"] and not s["ipo_bidding"]["open"]  # bidding starts at 10:00
    s = market_status(at(2026, 9, 29, 15, 45), HOLIDAYS)
    assert not s["equity"]["open"] and s["ipo_bidding"]["open"]  # the book keeps moving until 17:00
    assert s["equity"]["next_open"] == "2026-09-30T09:15:00+05:30"
    s = market_status(at(2026, 9, 29, 17, 0), HOLIDAYS)
    assert not s["ipo_bidding"]["open"]


def test_holidays_and_weekends_are_closed():
    s = market_status(at(2026, 10, 2, 11, 0), HOLIDAYS)
    assert s["holiday"] == "Mahatma Gandhi Jayanti" and not s["trading_day"] and not s["equity"]["open"]
    assert s["equity"]["next_open"] == "2026-10-05T09:15:00+05:30"  # Fri holiday, then the weekend
    assert not market_status(at(2026, 10, 3, 11, 0), HOLIDAYS)["ipo_bidding"]["open"]  # Saturday


def test_radar_refreshes_every_minute_in_bidding_hours(monkeypatch):
    monkeypatch.setattr("finresearch.api.live._holidays", lambda: HOLIDAYS)
    assert radar_ttl(at(2026, 9, 29, 11, 0), 300) == 60
    assert radar_ttl(at(2026, 9, 29, 18, 0), 300) == 300


@pytest.fixture
def live_app(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources

    monkeypatch.setattr("finresearch.api.live._holidays", lambda: HOLIDAYS)
    now = {"t": at(2026, 9, 29, 11, 0)}
    calls = {"quote": 0, "book": 0}

    async def quote(symbol):
        calls["quote"] += 1
        return Quote(symbol=symbol, company="Infosys Limited", open=Decimal("1002"), last_price=Decimal("1003.2"),
                     previous_close=Decimal("1000.2"), as_of=at(2026, 9, 29, 10, 59))  # fmt: skip

    from finresearch.adapters.nse import CategorySubscription, IpoDetail, SubscriptionSnapshot
    from finresearch.monitor.jobs import Deps

    async def ipo_detail(symbol):
        calls["book"] += 1
        snap = SubscriptionSnapshot(symbol=symbol, as_of=at(2026, 9, 29, 10, 57), source="nse_combined",
                                    categories=[CategorySubscription(name="Total", code=None, times=Decimal("15.6"))],
                                    total_times=Decimal("15.6"))  # fmt: skip
        return IpoDetail(symbol=symbol, series="EQ", company_name="Live Co", combined=snap, issue_info={})

    app = create_app(monitor_deps=Deps(ipo_detail=ipo_detail, quote=quote), clock=lambda: now["t"])
    app.state.markets = MarketSources(quote=quote)
    with TestClient(app) as c:
        yield c, now, calls


def test_quote_is_live_in_session_and_cached_briefly(live_app):
    c, now, calls = live_app
    r = c.get("/api/stocks/infy/quote").json()
    assert r["live"] and r["quote"]["last_price"] == "1003.2" and r["quote"]["change"] == "3"
    assert r["fetched_at"] and "symbol=INFY" in r["source"]
    c.get("/api/stocks/INFY/quote")
    assert calls["quote"] == 1  # served from the 15 s cache
    now["t"] = at(2026, 9, 29, 16, 0)
    closed = c.get("/api/stocks/INFY/quote").json()
    assert not closed["live"] and closed["market"]["next_open"] == "2026-09-30T09:15:00+05:30"
    assert c.get("/api/market/status").json()["ipo_bidding"]["open"]


def test_watch_live_book_only_in_bidding_hours_and_read_only(live_app):
    """#247: the GET once stored each new exchange timestamp as a subscription snapshot; it is now read-only (the
    monitor's scheduled checks record the book)."""
    from finresearch.db import session_scope
    from finresearch.db.models import SubscriptionSnapshotRow, Watch
    from finresearch.ingest.documents import get_or_create_company

    c, now, calls = live_app
    with session_scope() as s:
        s.query(SubscriptionSnapshotRow).filter_by(nse_symbol="LIVECO").delete()
        co = get_or_create_company(s, "live-co", "Live Co")
        w = Watch(company_id=co.id, nse_symbol="LIVECO", open_date=date(2026, 9, 25), close_date=date(2026, 9, 29),
                  meta={})  # fmt: skip
        s.add(w)
        s.flush()
        wid = w.id
    r = c.get(f"/api/watches/{wid}/live").json()
    assert (
        r["live"] and r["book"]["total_times"] == "15.6" and r["book"]["as_of"] == "2026-09-29T10:57:00+05:30"
    )
    c.get(f"/api/watches/{wid}/live")
    assert calls["book"] == 1  # one exchange call a minute at most
    with session_scope() as s:
        assert s.query(SubscriptionSnapshotRow).filter_by(nse_symbol="LIVECO").count() == 0
    now["t"] = at(2026, 9, 30, 11, 0)  # the day after the close
    assert c.get(f"/api/watches/{wid}/live").json() == {
        "live": False,
        "reason": "outside bidding hours (10:00–17:00 IST on bidding days)",
    }
    assert c.get("/api/watches/999999/live").status_code == 404
