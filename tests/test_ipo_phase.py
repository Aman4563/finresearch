"""IPO watch lifecycle (#291): phases, archive, withdrawn, conversion to a stock watch that keeps the listing record."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from test_monitor import FakeNse, deps, ist, orient_detail

# Orient Cables (fixture): bidding 25-29 Sep 2026, allotment 30 Sep, expected listing 2 Oct (T+3 without holidays),
# anchor lock-ins end 30 Oct and 29 Dec 2026, the six-month unlock 30 Mar 2027
SYMBOL = "ORIENTCABL"
FAKE_ISIN = "INE000Z99991"  # synthetic
# cached trading holidays for the trading-day counts: Gandhi Jayanti (2-Oct) and a made-up 19-Oct closure
HOLIDAYS = {date(2026, 10, 2): "Gandhi Jayanti", date(2026, 10, 19): "test closure"}


@pytest.fixture
def ipo(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import (
        Alert,
        Forecast,
        MonitorJob,
        PortfolioHolding,
        ResearchRun,
        SubscriptionSnapshotRow,
        Watch,
    )  # fmt: skip
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.watch import upsert_watch
    from finresearch.suggest.advisor import load_profile, save_profile

    with session_scope() as s:
        p = load_profile(s)  # the profile outlives a test: back to the default archive delay
        p.preferences.watch.ipo_archive_trading_days = 10
        save_profile(s, p)
        for model in (Alert, MonitorJob, Watch, SubscriptionSnapshotRow, Forecast, PortfolioHolding):
            s.query(model).delete()
        co = get_or_create_company(s, "ph-" + tmp_path.name[-8:], "Phase Co")
        co.nse_symbol, co.isin = SYMBOL, FAKE_ISIN
        s.add(ResearchRun(company_id=co.id, kind="ipo_report", status="done",
                          manifest={"facts": {"issue_info": orient_detail().issue_info}}))  # fmt: skip
        slug = co.slug
    w = upsert_watch(slug, orient_detail().issue_info)
    return {"slug": slug, "watch_id": w["id"]}


def cache_holidays():
    from finresearch.adapters.nse_holidays import save_holidays

    save_holidays(HOLIDAYS, "trading")


def set_listed(wid: int, listing: date = date(2026, 10, 5), close: bool = True) -> None:
    """The state the listing checks leave: NSE's listing date and the listing-day open (and close)."""
    from finresearch.db import session_scope
    from finresearch.db.models import Watch

    with session_scope() as s:
        w = s.get(Watch, wid)
        w.listing_date = listing
        w.meta = {**(w.meta or {}), "listing_confirmed": True, "expected_listing_date": "2026-10-02",
                  "listing_open": "300", **({"listing_close": "305"} if close else {})}  # fmt: skip


def watch(wid: int):
    from finresearch.db import session_scope
    from finresearch.db.models import Watch

    with session_scope() as s:
        w = s.get(Watch, wid)
        s.expunge(w)
        return w


def add_forecast(created, instrument: str = SYMBOL, status: str = "open", key: str = "f1") -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    with session_scope() as s:
        f = Forecast(created_at=created, asset="ipo", instrument=instrument, name="Phase Co", source="signal:ipo",
                     event_kind="listing_gain", event="opens above the issue price", horizon="listing day",
                     resolve_on=date(2026, 10, 2), probability=0.6, action="APPLY", method="test",
                     validation_status="shadow", inputs={"symbol": instrument, "issue_price": "250"},
                     status=status, dedupe_key=key)  # fmt: skip
        s.add(f)
        s.flush()
        return f.id


def test_phases_follow_the_dates_and_only_a_recorded_price_means_listed():
    from finresearch.db.models import Watch
    from finresearch.monitor.phase import date_phase

    w = Watch(kind="ipo", nse_symbol="X", open_date=date(2026, 9, 25), close_date=date(2026, 9, 29),
              allotment_date=date(2026, 10, 1), listing_date=date(2026, 10, 5), meta={})  # fmt: skip
    expected = {date(2026, 9, 24): "announced", date(2026, 9, 25): "open", date(2026, 9, 29): "open",
                date(2026, 9, 30): "closed", date(2026, 10, 1): "allotted", date(2026, 10, 5): "allotted",
                date(2026, 10, 30): "allotted"}  # fmt: skip
    assert {d: date_phase(w, d) for d in expected} == expected  # the listing date passing is not a listing
    w.meta = {"listing_close": "305"}  # the open check failed but the close was read
    assert date_phase(w, date(2026, 10, 5)) == "listed"
    assert date_phase(Watch(kind="ipo", nse_symbol="X", meta={}), date(2026, 10, 5)) is None  # dates unknown


async def test_a_moved_listing_date_keeps_the_watch_allotted_until_nse_lists_it(ipo):
    from finresearch.adapters.nse import Quote
    from finresearch.monitor.scheduler import tick

    fake = FakeNse()
    await tick(deps(fake), ist(2026, 10, 2, 10, 20))  # expected listing day: no quote yet
    assert (watch(ipo["watch_id"]).meta["phase"], watch(ipo["watch_id"]).listing_date) == (
        "allotted",
        date(2026, 10, 2),
    )
    await tick(deps(fake), ist(2026, 10, 5, 9, 0))
    assert watch(ipo["watch_id"]).meta["phase"] == "allotted"
    fake.q = Quote(symbol=SYMBOL, open=Decimal("300"), last_price=Decimal("310"), close_price=Decimal("0"),
                   listing_date=date(2026, 10, 6), as_of=ist(2026, 10, 6, 10, 15))  # fmt: skip
    await tick(deps(fake), ist(2026, 10, 6, 10, 16))  # the listing check records the open on the moved date
    await tick(deps(fake), ist(2026, 10, 6, 10, 17))  # the next pass sees it
    w = watch(ipo["watch_id"])
    assert (w.listing_date, w.meta["expected_listing_date"], w.meta["phase"]) == (
        date(2026, 10, 6),
        "2026-10-02",
        "listed",
    )
    # the card's gain: (300 - 272) / 272 = 10.29% against the fixture's ₹272 upper band
    assert (w.meta["issue_price_upper"], w.meta["listing_gain_pct"]) == ("272", "10.29")
    assert [h["phase"] for h in w.meta["phase_history"]] == ["allotted", "listed"]


async def test_archive_needs_the_close_ten_trading_days_and_no_open_forecast(ipo):
    from finresearch.monitor.phase import phase_step

    wid = ipo["watch_id"]
    set_listed(wid, close=False)
    await phase_step(ist(2026, 11, 30, 18, 0))
    assert watch(wid).meta["phase"] == "listed"  # holidays not cached: trading days unknown, nothing moves
    cache_holidays()
    await phase_step(ist(2026, 11, 30, 18, 0))
    assert watch(wid).meta["phase"] == "listed"  # no listing-day close recorded
    set_listed(wid)
    # trading days after 5-Oct: 6,7,8,9,12,13,14,15,16 (9), 19-Oct closed, 20-Oct the 10th
    await phase_step(ist(2026, 10, 19, 18, 0))
    assert watch(wid).meta["phase"] == "listed"
    add_forecast(ist(2026, 9, 26, 12, 0))  # still open (e.g. waiting to resolve)
    await phase_step(ist(2026, 10, 20, 18, 0))
    assert watch(wid).meta["phase"] == "listed"
    await phase_step(ist(2026, 12, 3, 18, 0))  # 59 days after the listing
    assert watch(wid).meta["phase"] == "listed"
    out = await phase_step(ist(2026, 12, 4, 18, 0))  # 60 days: an unresolved forecast no longer holds it
    assert watch(wid).meta["phase"] == "archived" and out["ipo_archived"] == 1 and out["ipo_converted"] == 0
    assert watch(wid).kind == "ipo" and watch(wid).active  # not held, not kept: stays an IPO watch (lock-ins)


async def test_archive_after_ten_trading_days_with_forecasts_resolved_and_a_profile_setting(ipo):
    from finresearch.db import session_scope
    from finresearch.monitor.phase import phase_step
    from finresearch.suggest.advisor import load_profile, save_profile

    wid = ipo["watch_id"]
    set_listed(wid)
    cache_holidays()
    add_forecast(ist(2026, 9, 26, 12, 0), status="resolved")
    add_forecast(ist(2026, 9, 26, 12, 0), instrument="OTHERIPO", key="f2")  # another instrument's
    await phase_step(ist(2026, 10, 20, 18, 0))
    assert watch(wid).meta["phase"] == "archived"
    with session_scope() as s:  # 15 trading days instead of 10
        p = load_profile(s)
        p.preferences.watch.ipo_archive_trading_days = 15
        save_profile(s, p)
        w = s.get(type(watch(wid)), wid)
        w.meta = {k: v for k, v in w.meta.items() if k not in ("phase", "phase_history")}
    await phase_step(ist(2026, 10, 26, 18, 0))  # 14 trading days (21,22,23,26 after the 10th)
    assert watch(wid).meta["phase"] == "listed"
    await phase_step(ist(2026, 10, 27, 18, 0))
    assert watch(wid).meta["phase"] == "archived"


async def test_an_archived_watch_still_runs_its_lock_in_checks(ipo):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.monitor.scheduler import tick

    wid = ipo["watch_id"]
    set_listed(wid)
    cache_holidays()
    await tick(
        deps(FakeNse()), ist(2026, 10, 30, 9, 5)
    )  # archived in this pass, then the 30-Oct lock-in runs
    w = watch(wid)
    assert (w.meta["phase"], w.kind, w.active) == ("archived", "ipo", True)
    with session_scope() as s:
        assert (
            "anchor (50%) lock-in ends on 2026-10-30" in s.query(Alert).filter_by(kind="lockin").one().message
        )


def hold(qty: str) -> None:
    """A synthetic lot of the company's shares (matched by ISIN only) with `qty` still open."""
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding, PortfolioLot

    with session_scope() as s:
        h = s.query(PortfolioHolding).filter_by(isin=FAKE_ISIN).first()
        if h is None:
            h = PortfolioHolding(ikey=f"ISIN:{FAKE_ISIN}", account="Test demat", asset_type="stock", name="Phase Co",
                                 isin=FAKE_ISIN, meta={})  # fmt: skip
            s.add(h)
            s.flush()
            s.add(PortfolioLot(holding_id=h.id, acquired=date(2026, 9, 30), origin="buy", quantity=Decimal(10),
                               open_quantity=Decimal(qty), cost_per_unit=Decimal(272)))  # fmt: skip
        else:
            s.query(PortfolioLot).filter_by(holding_id=h.id).update({"open_quantity": Decimal(qty)})


async def test_a_held_ipo_becomes_a_stock_watch_on_archive_and_keeps_its_listing_record(ipo):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, Forecast, MonitorJob
    from finresearch.monitor.scheduler import tick
    from finresearch.signals.ledger import resolve_listing_gain

    wid = ipo["watch_id"]
    cache_holidays()
    await tick(deps(FakeNse()), ist(2026, 9, 30, 19, 5))  # plans every check up to the six-month lock-in
    set_listed(wid)
    hold("0")  # a sold-out lot is not a holding
    await tick(deps(FakeNse()), ist(2026, 10, 20, 18, 0))
    assert (watch(wid).kind, watch(wid).meta["phase"]) == ("ipo", "archived")
    with session_scope() as s:
        w = s.get(type(watch(wid)), wid)
        w.meta = {k: v for k, v in w.meta.items() if k not in ("phase", "phase_history")}
    hold("10")
    out = await tick(deps(FakeNse()), ist(2026, 10, 21, 18, 0))
    assert out["ipo_converted"] == 1
    w = watch(wid)
    assert (w.kind, w.active, w.listing_date, w.allotment_date) == (
        "stock",
        True,
        date(2026, 10, 5),
        date(2026, 9, 30),
    )
    assert (w.meta["listing_open"], w.meta["listing_close"]) == ("300", "305")
    assert w.meta["ipo"]["listing_open"] == "300" and w.meta["ipo"]["listing_date"] == "2026-10-05"
    assert w.meta["ipo"]["phase"] == "archived" and w.meta["ipo"]["anchor_shares"] == "6088233"
    with session_scope() as s:
        pending = {j.kind for j in s.query(MonitorJob).filter_by(watch_id=wid, status="pending")}
        assert pending == {"lockin", "stock_daily"}  # the IPO's lock-ins survive the conversion
    await tick(deps(FakeNse()), ist(2026, 12, 29, 9, 5))
    with session_scope() as s:
        assert "lock-in ends on 2026-12-29" in s.query(Alert).filter_by(kind="lockin").one().message
        # the listing-gain resolver still reads the listing-day open from the (now stock) watch
        f = s.get(Forecast, add_forecast(ist(2026, 9, 26, 12, 0)))
        r = await resolve_listing_gain(f, None, s)
        assert (r.outcome, round(r.value, 2)) == (1, 20.0)  # (300 - 250) / 250
        assert "opened at ₹300 on 2026-10-05" in r.note and "monitor" in r.note
        late = s.get(Forecast, add_forecast(ist(2026, 10, 5, 9, 30), key="late"))
        assert (await resolve_listing_gain(late, None, s)).outcome is None  # made after the open: still void


def test_keep_watching_converts_an_archived_watch_and_the_list_carries_the_phase(ipo):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Company, Watch
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.watch import upsert_watch

    wid = ipo["watch_id"]
    set_listed(wid)
    with session_scope() as s:
        co = get_or_create_company(s, ipo["slug"] + "-b", "Bidding Co")
        co.nse_symbol = "BIDCO"
        w = s.get(Watch, wid)
        w.meta = {**w.meta, "phase": "archived"}
        slug_b = co.slug
    info = {**orient_detail().issue_info}
    today = date.today()
    info["Issue Period"] = (
        f"{(today - timedelta(days=1)):%d-%b-%Y} to {(today + timedelta(days=30)):%d-%b-%Y}"
    )
    bid = upsert_watch(slug_b, info)
    with TestClient(create_app()) as c:
        rows = c.get("/api/watches").json()
        assert [(r["id"], r["phase"]) for r in rows] == [(bid["id"], "open"), (wid, "archived")]
        assert c.post("/api/watches/999999/keep", json={"keep": True}).status_code == 404
        r = c.post(f"/api/watches/{wid}/keep", json={"keep": True})
        assert r.status_code == 200 and (r.json()["kind"], r.json()["phase"]) == ("stock", None)
        assert r.json()["meta"]["ipo"]["keep_watching"] is True and r.json()["meta"]["listing_open"] == "300"
        assert (
            c.post(f"/api/watches/{wid}/keep", json={"keep": False}).status_code == 422
        )  # a stock watch now
        r = c.post(f"/api/watches/{bid['id']}/keep", json={"keep": True})  # not archived: only remembered
        assert (r.json()["kind"], r.json()["meta"]["keep_watching"]) == ("ipo", True)
    with session_scope() as s:
        assert s.query(Company).filter_by(slug=slug_b).one().nse_symbol == "BIDCO"


class Lists:
    """NSE's issue lists and quote for the withdrawn check."""

    def __init__(self, symbols, quote_error=None):
        self.quote_error = quote_error or RuntimeError("HTTP 404 for the quote")
        self.symbols, self.calls = symbols, 0

    async def ipo_symbols(self):
        self.calls += 1
        if isinstance(self.symbols, Exception):
            raise self.symbols
        return self.symbols

    async def quote(self, symbol):
        raise self.quote_error


def lists_deps(fake: Lists):
    from finresearch.monitor.jobs import Deps

    return Deps(ipo_detail=None, quote=fake.quote, ipo_symbols=fake.ipo_symbols)


async def test_an_issue_that_never_lists_is_withdrawn_only_on_clear_evidence(ipo):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob
    from finresearch.monitor.phase import phase_step
    from finresearch.monitor.scheduler import sync_slots

    wid = ipo["watch_id"]
    sync_slots(ist(2026, 9, 30, 10, 0))
    cache_holidays()
    # expected listing 2-Oct; 10 trading days later is 16-Oct (5..9, 12..16): not yet past the threshold
    await phase_step(ist(2026, 10, 16, 18, 0), lists_deps(Lists(set())))
    assert watch(wid).meta["phase"] == "allotted"
    for unknown in (Lists(ConnectionError("NSE down")), Lists(set(), quote_error=TimeoutError("timed out"))):
        await phase_step(ist(2026, 10, 20, 18, 0), lists_deps(unknown))  # a source failed: unknown, unchanged
        assert watch(wid).meta["phase"] == "allotted" and "withdrawn_checked" not in watch(wid).meta
    await phase_step(ist(2026, 10, 20, 18, 0), None)  # no lists configured: never judged
    await phase_step(
        ist(2026, 10, 20, 18, 0), lists_deps(Lists({SYMBOL}))
    )  # still on a list (e.g. past issues)
    assert (watch(wid).meta["phase"], watch(wid).active) == ("allotted", True)
    again = Lists(set())
    await phase_step(ist(2026, 10, 20, 19, 0), lists_deps(again))
    assert again.calls == 0  # checked today already
    out = await phase_step(ist(2026, 10, 21, 18, 0), lists_deps(again))
    assert out["ipo_withdrawn"] == 1 and (watch(wid).meta["phase"], watch(wid).active) == ("withdrawn", False)
    with session_scope() as s:
        assert not s.query(MonitorJob).filter_by(watch_id=wid, status="pending").count()
        assert "treated as withdrawn" in s.query(Alert).filter_by(kind="ipo_withdrawn").one().message
    # a wrong verdict is undone by watching the issue again: the dates decide the phase, the checks come back
    from finresearch.monitor.watch import upsert_watch

    w = upsert_watch(ipo["slug"], orient_detail().issue_info)
    assert (w["phase"], w["active"], w["meta"]["phase_history"][-1]["phase"]) == (
        "allotted",
        True,
        "rewatched",
    )


async def test_listed_and_archived_ipos_leave_the_brief_and_the_ipo_alert_rules(ipo):
    from finresearch.alerts.engine import targets
    from finresearch.db import session_scope
    from finresearch.db.models import Watch
    from finresearch.monitor.digest import ipo_events
    from finresearch.suggest.profile import AlertRule

    rule = AlertRule(id="r1", kind="ipo", metric="total_times", op=">", value=1)

    wid = ipo["watch_id"]
    with session_scope() as s:
        assert ipo_events(s, date(2026, 9, 28))[0]["title"] == f"IPO {SYMBOL} closes (UPI mandate by 5 PM)"
        assert [t.instrument for t in targets(s, rule)] == [SYMBOL]
    set_listed(wid)
    with session_scope() as s:
        assert ipo_events(s, date(2026, 9, 28)) == []  # its dates are history once it has listed
        s.get(Watch, wid).meta = {**s.get(Watch, wid).meta, "phase": "archived"}
    with session_scope() as s:
        assert targets(s, rule) == []
