"""NSE holidays: parsing the holiday master, the cache, and holiday-aware T+N, bidding days and monitor plans."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from finresearch.adapters import nse_holidays as h
from finresearch.fincalc.dates import bidding_day_number

FIX = Path(__file__).parent / "fixtures" / "nse"


def payload(kind):
    return json.loads((FIX / f"holidays_2026_{kind}_cm.json").read_text())


@pytest.fixture
def cached(tmp_path, monkeypatch):
    monkeypatch.setenv("FINRESEARCH_STATE_DIR", str(tmp_path / "state"))
    from finresearch import config

    config.get_settings.cache_clear()
    for kind in ("trading", "clearing"):
        h.save_holidays(h.parse_holidays(payload(kind)), kind)
    yield
    config.get_settings.cache_clear()


def test_parse_2026_nse_lists():
    trading, clearing = h.parse_holidays(payload("trading")), h.parse_holidays(payload("clearing"))
    assert len(trading) == 20 and trading[date(2026, 10, 2)].startswith("Mahatma Gandhi")
    assert (
        date(2026, 4, 1) in clearing and date(2026, 4, 1) not in trading
    )  # annual bank closing: settlement only
    assert (
        date(2026, 8, 15) in trading and date(2026, 8, 15) not in clearing
    )  # a Saturday in the trading list


def test_cache_round_trip_and_union(cached):
    assert date(2026, 10, 2) in h.trading_holidays() and h.cached_years() == {2026}
    assert date(2026, 4, 1) in h.settlement_holidays() and date(2026, 4, 1) not in h.trading_holidays()


def test_orient_t_plus_3_skips_gandhi_jayanti(cached):
    from finresearch.monitor.plan import expected_dates, plan

    assert expected_dates(date(2026, 9, 29)) == (date(2026, 9, 30), date(2026, 10, 5))
    # an issue open 30-Sep..5-Oct bids on 30-Sep, 1-Oct and 5-Oct (2-Oct holiday, 3/4-Oct weekend)
    subs = {s.due_at.date() for s in plan("X", date(2026, 9, 30), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 8))
            if s.kind == "subscription"}  # fmt: skip
    assert subs == {date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 5)}
    assert bidding_day_number(date(2026, 9, 30), date(2026, 10, 5), h.trading_holidays()) == 3


def test_bidding_days_left_and_stock_plan_skip_holidays(cached):
    from finresearch.monitor.plan import plan_stock
    from finresearch.suggest.rules import bidding_days_left

    assert bidding_days_left(date(2026, 10, 1), date(2026, 10, 5)) == 2  # 1-Oct and 5-Oct
    days = [s.due_at.date() for s in plan_stock("INFY", date(2026, 10, 1), date(2026, 10, 6))]
    assert days == [date(2026, 10, 1), date(2026, 10, 5), date(2026, 10, 6)]


async def test_refresh_fetches_only_when_stale(tmp_path):
    calls = []

    async def fetch(kind):
        calls.append(kind)
        return payload(kind)

    state = tmp_path / "s"
    assert await h.refresh_holidays(state, fetch=fetch) is True and calls == ["trading", "clearing"]
    assert await h.refresh_holidays(state, fetch=fetch) is False and len(calls) == 2
    assert date(2026, 10, 2) in h.trading_holidays(state)

    async def empty(kind):
        return {}

    with pytest.raises(ValueError):
        await h.refresh_holidays(tmp_path / "e", fetch=empty)


async def test_moved_listing_date_cancels_the_old_check(env, cached, tmp_path):
    """The Orient watch was created before holidays were known (listing 2-Oct); refreshed dates cancel that check."""
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, SubscriptionSnapshotRow, Watch
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import sync_slots

    with session_scope() as s:
        for m in (Alert, MonitorJob, Watch, SubscriptionSnapshotRow):
            s.query(m).delete()
        co = get_or_create_company(s, "hol-" + tmp_path.name[-8:], "Hol Co")
        w = Watch(company_id=co.id, kind="ipo", nse_symbol="HOLCO", open_date=date(2026, 9, 25),
                  close_date=date(2026, 9, 29), allotment_date=date(2026, 9, 30), listing_date=date(2026, 10, 2), meta={})  # fmt: skip
        s.add(w)
        s.flush()
        wid = w.id
    now = datetime(2026, 9, 29, 12, tzinfo=UTC)
    sync_slots(now)
    with session_scope() as s:
        w = s.get(Watch, wid)
        w.listing_date = date(2026, 10, 5)
    sync_slots(now)
    with session_scope() as s:
        jobs = {j.slot: j.status for j in s.query(MonitorJob).filter(MonitorJob.kind == "listing")}
    assert (
        jobs["HOLCO:listing:2026-10-02:open"] == "cancelled"
        and jobs["HOLCO:listing:2026-10-05:open"] == "pending"
    )
    assert Deps(ipo_detail=None, quote=None).live_holidays is False
