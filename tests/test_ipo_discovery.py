"""#292: IPO forecasts resolve from the harvest's listing bar or NSE's listing-day bar only (EQ, else the T2T BE
series), never from a later session; the nightly incremental harvest; the issue registry and its new-issue alert.
Offline: synthetic symbols, fake NSE/BSE sources. Bar shapes follow NSE's history API (an issue that listed in
trade-for-trade has BE rows first and EQ rows ~10 sessions later, e.g. NSE's ASTRON history, Dec-2017/Jan-2018)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from finresearch.fincalc.dates import IST


@dataclass
class Bar:
    day: date
    open: Decimal | None
    close: Decimal | None = None
    prev_close: Decimal | None = None


class Deps:
    def __init__(self, eq: dict[str, list[Bar]] | None = None, be: dict[str, list[Bar]] | None = None):
        self.eq, self.be, self.calls = eq or {}, be, []

    async def price_history(self, symbol, start, end):
        self.calls.append(("EQ", symbol, start))
        return [b for b in self.eq.get(symbol, []) if start <= b.day <= end]

    async def _be(self, symbol, start, end):
        self.calls.append(("BE", symbol, start))
        return [b for b in (self.be or {}).get(symbol, []) if start <= b.day <= end]

    @property
    def t2t_price_history(self):
        return self._be if self.be is not None else None


@pytest.fixture
def db(env, tmp_path):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, AlertEvalSlot, Forecast, IpoHistory, IpoIssue

    with session_scope() as s:
        for m in (Forecast, IpoHistory, IpoIssue, Alert):
            s.query(m).delete()
        s.query(AlertEvalSlot).filter(AlertEvalSlot.slot.like("ipo_%")).delete(synchronize_session=False)
    return tmp_path.name[-6:].upper()


def at(y, m, d, h=12) -> datetime:
    return datetime(y, m, d, h, tzinfo=IST)


def forecast(s, symbol: str, method: str, *, issue="100", created=at(2026, 9, 30), resolve_on=date(2026, 10, 6),
             listing: str | None = None) -> int:  # fmt: skip
    from finresearch.db.models import Forecast
    from finresearch.signals import ledger

    f = Forecast(created_at=created, asset="ipo", instrument=symbol, name=f"{symbol} Ltd", source="signal:ipo",
                 event_kind="listing_gain", event=ledger.IPO_EVENT, horizon="listing day", resolve_on=resolve_on,
                 probability=0.7, action="APPLY", method=method, validation_status="validated",
                 inputs={"symbol": symbol, "issue_price": issue, "listing_date": listing}, status="open",
                 dedupe_key=ledger.dedupe_key("ipo", symbol, "listing_gain", created.date(), method))  # fmt: skip
    s.add(f)
    s.flush()
    return f.id


def history_row(s, symbol: str, listing: date, bars: list[dict], *, list_open: str | None, series="EQ") -> None:
    from finresearch.db.models import IpoHistory

    s.add(IpoHistory(symbol=symbol, series="EQ", company=f"{symbol} Ltd", ipo_start=listing - timedelta(days=6),
                     ipo_end=listing - timedelta(days=4), listing_date=listing, issue_price=Decimal(100),
                     list_open=Decimal(list_open) if list_open else None, status="complete" if list_open else "partial",
                     missing=[], source={"history": {"series": series}}, raw={"bars": bars}))  # fmt: skip


def bar_json(day: date, open_: str, prev: str) -> dict:
    return {"day": day.isoformat(), "open": open_, "close": open_, "prev_close": prev}


async def _resolve(deps, now=at(2026, 10, 9, 18), recheck=False):
    from finresearch.signals import ledger

    return await ledger.resolve_due(deps, now, recheck=recheck)


# --------------------------------------------------------------------------- forecast resolver
async def test_unwatched_listed_forecasts_resolve_from_the_ipo_history_harvest(db):
    """The 14-forecast pattern: unwatched issues, listed, the live call plus its shadow method per symbol. The harvest's
    listing bar resolves every one of them without any NSE price request."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    up, down = "UP" + db, "DN" + db
    listing = date(2026, 10, 6)
    with session_scope() as s:
        ids = [forecast(s, sym, m) for sym in (up, down) for m in ("live", "shadow")]
        history_row(s, up, listing, [bar_json(listing, "124.50", "100")], list_open="124.50")
        history_row(s, down, listing, [bar_json(listing, "91", "100")], list_open="91")
    deps = Deps()  # no bars at all: the harvest alone must resolve them
    stats = await _resolve(deps)
    assert stats["resolved"] == 4 and deps.calls == []
    with session_scope() as s:
        got = [s.get(Forecast, i) for i in ids]
        assert [(f.status, f.outcome) for f in got] == [("resolved", 1)] * 2 + [("resolved", 0)] * 2
        assert got[0].resolution_value == pytest.approx(24.5)  # 124.50 / 100 - 1
        assert got[2].resolution_value == pytest.approx(-9.0)  # 91 / 100 - 1
        assert "IPO history harvest" in got[0].resolution_note and "2026-10-06" in got[0].resolution_note


async def test_a_later_session_open_is_never_scored_as_the_listing_open(db):
    """Listing date known (harvest row without a listing bar; NSE-stated date in the inputs): EQ rows that start ten
    sessions later (a T2T listing) are not the listing. Listing date unknown: a first bar whose previous close is not
    the issue price is a later session. All keep waiting."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    hist, stated, unknown = "LH" + db, "LS" + db, "LU" + db
    listing = date(2026, 9, 21)
    later = [Bar(date(2026, 10, 5), Decimal("153.15"), prev_close=Decimal("150.10"))]
    with session_scope() as s:
        a = forecast(s, hist, "m", created=at(2026, 9, 15))
        history_row(s, hist, listing, [bar_json(date(2026, 10, 5), "153.15", "150.10")], list_open="153.15")
        b = forecast(s, stated, "m", created=at(2026, 9, 15), listing=listing.isoformat())
        c = forecast(s, unknown, "m", created=at(2026, 9, 15))
    deps = Deps({hist: later, stated: later, unknown: later}, be={})
    stats = await _resolve(deps)
    assert stats["waiting"] == 3 and stats["resolved"] == 0
    with session_scope() as s:
        for fid in (a, b, c):
            f = s.get(Forecast, fid)
            assert f.status == "open" and f.outcome is None and f.resolution_note.startswith("waiting")
        assert "2026-09-21 (listing day)" in s.get(Forecast, b).resolution_note


async def test_t2t_listing_resolves_from_the_be_series(db):
    """EQ has no row for the listing day; NSE's BE series has it (previous close = the issue price): that open is
    scored, labelled with its series."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    sym = "TT" + db
    listing = date(2026, 9, 21)
    with session_scope() as s:
        fid = forecast(s, sym, "m", issue="50", created=at(2026, 9, 15), listing=listing.isoformat())
    deps = Deps({sym: [Bar(date(2026, 10, 5), Decimal("153.15"), prev_close=Decimal("150.10"))]},
                be={sym: [Bar(listing, Decimal("115"), prev_close=Decimal("50")),
                          Bar(date(2026, 9, 22), Decimal("126.75"), prev_close=Decimal("120.75"))]})  # fmt: skip
    assert (await _resolve(deps))["resolved"] == 1
    with session_scope() as s:
        f = s.get(Forecast, fid)
        assert (f.outcome, f.resolution_value) == (1, pytest.approx(130.0))  # 115 / 50 - 1
        assert "BE series" in f.resolution_note and "2026-09-21" in f.resolution_note
    # without a BE source configured the same forecast would only wait (never the EQ bar of 5-Oct)
    with session_scope() as s:
        g = forecast(s, "TN" + db, "m", issue="50", created=at(2026, 9, 15), listing=listing.isoformat())
    await _resolve(Deps({"TN" + db: [Bar(date(2026, 10, 5), Decimal("153.15"), prev_close=Decimal("150.10"))]}))
    with session_scope() as s:
        assert s.get(Forecast, g).status == "open"


async def test_listing_day_eq_bar_and_unknown_date_marker(db):
    """Known listing date with the EQ bar on that day: scored. Unknown date: the first bar after the forecast counts
    only with the listing-day marker (NSE sets the listing day's previous close to the issue price)."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    k, u = "KE" + db, "UE" + db
    with session_scope() as s:
        fk = forecast(s, k, "m", listing="2026-10-06")
        fu = forecast(s, u, "m")
    deps = Deps({k: [Bar(date(2026, 10, 6), Decimal("98"))],
                 u: [Bar(date(2026, 10, 6), Decimal("140"), prev_close=Decimal("100.00"))]})  # fmt: skip
    assert (await _resolve(deps))["resolved"] == 2
    with session_scope() as s:
        assert (s.get(Forecast, fk).outcome, s.get(Forecast, fu).outcome) == (0, 1)
        assert "EQ series" in s.get(Forecast, fk).resolution_note


async def test_recheck_tries_forecasts_checked_in_the_last_hours(db):
    from finresearch.db import session_scope

    sym = "RC" + db
    with session_scope() as s:
        forecast(s, sym, "m")
    assert (await _resolve(Deps()))["waiting"] == 1
    with session_scope() as s:
        history_row(s, sym, date(2026, 10, 6), [bar_json(date(2026, 10, 6), "110", "100")], list_open="110")
    later = at(2026, 10, 9, 19)
    assert (await _resolve(Deps(), later))["resolved"] == 0  # checked an hour ago: not due yet
    assert (await _resolve(Deps(), later, recheck=True))["resolved"] == 1
