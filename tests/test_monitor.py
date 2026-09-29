"""Monitoring: the schedule, watches from NSE issue information, idempotent jobs, retries and alerts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.fincalc.dates import IST
from finresearch.monitor.plan import expected_dates, plan
from finresearch.monitor.watch import parse_anchor_shares, parse_period

FIX = Path(__file__).parent / "fixtures" / "nse"


def orient_detail(qib: str | None = None):
    from finresearch.adapters.nse import parse_ipo_detail

    d = parse_ipo_detail(
        "ORIENTCABL", json.loads((FIX / "ipo_detail_ORIENTCABL_20260928_1636_live.json").read_text())
    )
    if qib is not None:
        for c in d.combined.categories:
            if c.code == "1":
                c.times = Decimal(qib)
    return d


def test_orient_schedule_follows_the_bidding_days_and_t_plus_3():
    info = orient_detail().issue_info
    open_d, close_d = parse_period(info["Issue Period"])
    assert (open_d, close_d) == (date(2026, 9, 25), date(2026, 9, 29))
    assert parse_anchor_shares(info["Issue Size"]) == 6088233
    allot, listing = expected_dates(close_d)
    assert (allot, listing) == (
        date(2026, 9, 30),
        date(2026, 10, 2),
    )  # expected; holidays are not in the calendar
    slots = plan("ORIENTCABL", open_d, close_d, allot, listing, Decimal(6088233))
    subs = [s for s in slots if s.kind == "subscription"]
    assert len(subs) == 3 * 6 and {s.due_at.date() for s in subs} == {
        date(2026, 9, 25),
        date(2026, 9, 28),
        date(2026, 9, 29),
    }  # weekend skipped
    assert [s.slot for s in subs if s.params["final"]] == ["ORIENTCABL:subscription:2026-09-29:1715"]
    lockins = [s for s in slots if s.kind == "lockin"]
    assert [s.params["unlock_date"] for s in lockins] == ["2026-10-30", "2026-12-29", "2027-03-30"]
    assert lockins[0].params["shares"] == "3044116" and len({s.slot for s in slots}) == len(slots)
    six = lockins[
        -1
    ].params  # promoters above the minimum and pre-IPO holders: 6 months, usually the largest unlock
    assert "pre-IPO" in six["holder"] and "Reg 16" in six["basis"] and "Reg 17" in six["basis"]


@pytest.fixture
def watched(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, Decision, MonitorJob, ResearchRun, SubscriptionSnapshotRow, Watch
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.watch import upsert_watch

    with session_scope() as s:
        for model in (Alert, MonitorJob, Watch, SubscriptionSnapshotRow):
            s.query(model).delete()
        co = get_or_create_company(s, "mon-" + tmp_path.name[-8:], "Mon Co")
        co.nse_symbol = "ORIENTCABL"
        run = ResearchRun(company_id=co.id, kind="ipo_report", status="done",
                          manifest={"facts": {"issue_info": orient_detail().issue_info}})  # fmt: skip
        s.add(run)
        s.flush()
        d = Decision(run_id=run.id, company_id=co.id, action="SKIP", lots=0, category="retail", suggestion={},
                     inputs={"rules": [{"rule": {"id": "qib-floor"}, "status": "fired"}], "metrics": {}})  # fmt: skip
        s.add(d)
        slug, decision_id = co.slug, None
        s.flush()
        decision_id = d.id
    w = upsert_watch(slug, orient_detail().issue_info)
    return {"slug": slug, "watch_id": w["id"], "decision_id": decision_id}


class FakeNse:
    def __init__(self, qib="0.05", quote=None):
        self.qib, self.q, self.calls = qib, quote, 0

    async def ipo_detail(self, symbol):
        self.calls += 1
        return orient_detail(self.qib)

    async def quote(self, symbol):
        if self.q is None:
            raise RuntimeError("HTTP 404")
        return self.q


def deps(fake):
    from finresearch.monitor.jobs import Deps

    return Deps(ipo_detail=fake.ipo_detail, quote=fake.quote)


def ist(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


async def test_final_day_checks_run_once_store_snapshots_and_alert_rule_changes(watched):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, SubscriptionSnapshotRow
    from finresearch.monitor.scheduler import tick

    fake = FakeNse(qib="1.25")  # QIB has crossed 1x since the suggestion (which fired the qib-floor rule)
    stats = await tick(deps(fake), ist(2026, 9, 29, 17, 20))
    # slots older than the 2-hour grace are not backfilled: 15:30+ -> 16:00 and 17:15 run now
    assert stats["done"] == 2 and fake.calls == 2
    again = await tick(deps(fake), ist(2026, 9, 29, 17, 21))
    assert again["done"] == 0 and again["added"] == 0 and fake.calls == 2  # never twice for the same slot
    with session_scope() as s:
        assert s.query(SubscriptionSnapshotRow).count() == 1  # same NSE timestamp -> one snapshot
        kinds = [a.kind for a in s.query(Alert).order_by(Alert.id)]
        assert kinds.count("rule_change") == 1 and kinds.count("subscription_final") == 1
        change = s.query(Alert).filter_by(kind="rule_change").one().message
        assert "rule qib-floor is now clear (qib_times = 1.25; was fired)" in change
        final = s.query(Alert).filter_by(kind="subscription_final").one().message
        assert "QIB 1.25x" in final and "NII 15.78x" in final
        assert s.query(MonitorJob).filter_by(kind="subscription", status="done").count() == 2


async def test_listing_waits_for_nse_then_updates_the_journal(watched):
    from finresearch.adapters.nse import Quote
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, Decision, MonitorJob, Watch
    from finresearch.monitor.scheduler import tick

    fake = FakeNse()
    await tick(deps(fake), ist(2026, 10, 2, 10, 20))  # expected listing day, but 2-Oct is an exchange holiday
    with session_scope() as s:
        job = s.query(MonitorJob).filter_by(slot="ORIENTCABL:listing:2026-10-02:open").one()
        assert job.status == "pending" and job.attempts == 1 and "not" in job.error.lower()
        assert job.due_at.astimezone(IST) == ist(2026, 10, 5, 10, 15)  # next exchange day, same time
    await tick(deps(fake), ist(2026, 10, 2, 15, 50))  # the close check also retries on the next exchange day
    fake.q = Quote(symbol="ORIENTCABL", open=Decimal("300"), last_price=Decimal("310"), close_price=Decimal("0"),
                   listing_date=date(2026, 10, 5), as_of=ist(2026, 10, 5, 10, 15))  # fmt: skip
    await tick(deps(fake), ist(2026, 10, 5, 10, 16))
    with session_scope() as s:
        job = s.query(MonitorJob).filter_by(slot="ORIENTCABL:listing:2026-10-02:open").one()
        assert job.status == "done" and job.result["gain_pct"] == "10.29"
        d = s.get(Decision, watched["decision_id"])
        assert d.listing_price == Decimal("300") and d.issue_price == Decimal("272")
        assert d.outcome["listing_gain_pct"] == "10.29"
        w = s.get(Watch, watched["watch_id"])
        assert w.listing_date == date(2026, 10, 5) and w.meta["expected_listing_date"] == "2026-10-02"
        assert "+10.29% vs the ₹272 upper band" in s.query(Alert).filter_by(kind="listing_open").one().message
    # regression: the confirmed date moved the planned listing slots; the old ones must not alert again
    await tick(deps(fake), ist(2026, 10, 5, 10, 17))
    fake.q = fake.q.model_copy(update={"close_price": Decimal("305")})
    await tick(deps(fake), ist(2026, 10, 5, 15, 50))
    await tick(deps(fake), ist(2026, 10, 6, 15, 50))
    with session_scope() as s:
        kinds = [a.kind for a in s.query(Alert)]
        assert kinds.count("listing_open") == 1 and kinds.count("listing_close") == 1
        assert "close ₹305" in s.query(Alert).filter_by(kind="listing_close").one().message
        assert not s.query(MonitorJob).filter_by(kind="listing", status="pending").count()


async def test_listing_compares_the_ist_date(watched):
    from finresearch.adapters.nse import Quote
    from finresearch.db import session_scope
    from finresearch.db.models import MonitorJob, Watch
    from finresearch.monitor import jobs

    q = Quote(symbol="ORIENTCABL", open=Decimal("300"), last_price=Decimal("300"), close_price=None,
              listing_date=date(2026, 10, 5), as_of=ist(2026, 10, 5, 1, 0))  # fmt: skip

    async def quote(symbol):
        return q

    with session_scope() as s:  # 02:00 IST on 5-Oct is still 4-Oct in UTC
        w = s.get(Watch, watched["watch_id"])
        job = MonitorJob(
            watch_id=w.id, kind="listing", slot="x", due_at=ist(2026, 10, 5, 2, 0), params={"which": "open"}
        )
        now = ist(2026, 10, 5, 2, 0).astimezone(UTC)  # the scheduler passes UTC
        out = await jobs.listing(s, job, w, jobs.Deps(ipo_detail=None, quote=quote), now)
        s.rollback()
    assert out["listing_date"] == "2026-10-05"


def test_stopping_a_watch_cancels_its_checks(watched):
    import asyncio

    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob
    from finresearch.monitor.scheduler import tick

    asyncio.run(tick(deps(FakeNse()), ist(2026, 9, 29, 17, 20)))  # plans every slot up to the lock-ins
    with TestClient(create_app()) as c:
        assert c.post(f"/api/watches/{watched['watch_id']}/stop").json()["active"] is False
    with (
        session_scope() as s
    ):  # a check still pending (e.g. retrying) from before the stop must not run either
        s.query(MonitorJob).filter_by(slot="ORIENTCABL:allotment:2026-09-30").update({"status": "pending"})
    for now in (ist(2026, 9, 30, 19, 5), ist(2026, 10, 30, 9, 5), ist(2026, 12, 29, 9, 5)):
        stats = asyncio.run(tick(deps(FakeNse()), now))
        assert stats["added"] == 0 and stats["done"] == 0
    with session_scope() as s:
        kinds = {a.kind for a in s.query(Alert)}
        assert not kinds & {"allotment", "lockin", "listing_open"}
        assert s.query(MonitorJob).filter_by(status="cancelled").count() > 0


def test_ipo_and_stock_watches_convert_in_place(watched):
    from finresearch.db import session_scope
    from finresearch.db.models import MonitorJob, Watch
    from finresearch.monitor.scheduler import sync_slots
    from finresearch.monitor.watch import stop_watch, upsert_watch, watch_stock

    slug, wid = watched["slug"], watched["watch_id"]
    sync_slots(ist(2026, 9, 29, 10, 0))
    with pytest.raises(ValueError, match="stop that watch first"):
        watch_stock(slug)  # an active IPO watch that has not listed
    with session_scope() as s:
        s.get(Watch, wid).meta = {"listing_confirmed": True, "rule_status": {"qib-floor": "clear"}}
    w = watch_stock(slug)  # listed: becomes a stock watch
    assert (w["id"], w["kind"], w["active"], w["open_date"], w["listing_date"], w["meta"]) == (
        wid,
        "stock",
        True,
        None,
        None,
        {},
    )
    with session_scope() as s:
        assert not s.query(MonitorJob).filter(MonitorJob.watch_id == wid, MonitorJob.status == "pending",
                                              MonitorJob.kind != "stock_daily").count()  # fmt: skip
    with pytest.raises(ValueError, match="stop that watch first"):
        upsert_watch(slug, orient_detail().issue_info)  # an active stock watch
    stop_watch(wid)
    w = upsert_watch(slug, orient_detail().issue_info)  # inactive: back to an IPO watch
    assert (w["kind"], w["active"], w["listing_date"], w["meta"]) == ("ipo", True, "2026-10-02", {})
    sync_slots(ist(2026, 9, 29, 10, 0))  # the checks cancelled by the conversion are planned again
    with session_scope() as s:
        job = s.query(MonitorJob).filter_by(slot="ORIENTCABL:subscription:2026-09-29:1030").one()
        assert (job.status, job.watch_id) == ("pending", wid)
    stop_watch(wid)
    assert watch_stock(slug)["kind"] == "stock"  # an inactive IPO watch can become a stock watch


async def test_lock_in_and_allotment_alerts(watched):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.monitor.scheduler import tick

    await tick(deps(FakeNse()), ist(2026, 9, 30, 19, 5))
    await tick(deps(FakeNse()), ist(2026, 10, 30, 9, 5))
    with session_scope() as s:
        msgs = {a.kind: a.message for a in s.query(Alert)}
    assert "basis of allotment is expected today (2026-09-30)" in msgs["allotment"]
    assert "anchor (50%) lock-in ends on 2026-10-30; 3,044,116 shares" in msgs["lockin"]


async def test_failed_checks_retry_then_alert(watched):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob
    from finresearch.monitor.scheduler import tick

    class Down(FakeNse):
        async def ipo_detail(self, symbol):
            raise ConnectionError("network is unreachable")

    now = ist(2026, 9, 29, 10, 31)
    for i in range(3):
        await tick(deps(Down()), now + timedelta(minutes=11 * i))
    with session_scope() as s:
        job = s.query(MonitorJob).filter_by(slot="ORIENTCABL:subscription:2026-09-29:1030").one()
        assert job.status == "failed" and job.attempts == 3
        assert "failed 3 times" in s.query(Alert).filter_by(kind="monitor_error").first().message


def test_watch_api_and_cors_for_the_dashboard(watched):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app

    async def detail(symbol):
        return orient_detail()

    with TestClient(create_app(nse_detail=detail)) as c:
        rows = c.get("/api/watches").json()
        assert rows[0]["nse_symbol"] == "ORIENTCABL" and rows[0]["listing_date"] == "2026-10-02"
        r = c.post("/api/watches", json={"company": watched["slug"]})
        assert r.status_code == 201, r.text
        assert r.json()["close_date"] == "2026-09-29"
        assert c.post("/api/watches", json={"company": "nope"}).status_code == 404
        wid = watched["watch_id"]
        assert c.get(f"/api/watches/{wid}").json()["alerts"] == []
        assert c.post(f"/api/watches/{wid}/stop").json()["active"] is False
        # regression: PUT /api/profile and PATCH /api/decisions from the dashboard need CORS preflight approval
        for method in ("PUT", "PATCH"):
            r = c.options("/api/profile", headers={"Origin": "http://127.0.0.1:3100",
                                                   "Access-Control-Request-Method": method})  # fmt: skip
            assert r.status_code == 200 and method in r.headers["access-control-allow-methods"]


async def test_slots_left_pending_while_the_monitor_was_down_are_missed_not_run_late(watched):
    from finresearch.db import session_scope
    from finresearch.db.models import MonitorJob
    from finresearch.monitor.scheduler import tick

    fake = FakeNse()
    await tick(deps(fake), ist(2026, 9, 29, 10, 0))  # plans the close day's slots; none due yet
    stats = await tick(deps(fake), ist(2026, 9, 29, 16, 5))  # monitor was down from 10:00 to 16:05
    with session_scope() as s:
        status = {
            j.slot[-4:]: j.status for j in s.query(MonitorJob).filter(MonitorJob.slot.like("%2026-09-29:%"))
        }
    assert status == {"1030": "missed", "1200": "missed", "1330": "missed", "1500": "done", "1600": "done",
                      "1715": "pending"}  # fmt: skip
    assert stats["missed"] == 3 and fake.calls == 2
