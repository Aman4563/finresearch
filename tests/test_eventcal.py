"""The point-in-time event calendar (#367): known_since filtering, coverage (unknown never reads as "no events"), the
India session an event lands in, and the plan's tier-1 window check."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from finresearch.eventcal import calendar as cal
from finresearch.fincalc.dates import IST

ET = timezone(timedelta(hours=-4), "EDT")


def _ev(kind: str, day: date, known: date, *, at: datetime | None = None, tier: int = 1) -> cal.Event:
    return cal.Event(type=kind, tier=tier, scope="IN" if tier == 1 else "US", day=day, at=at, title=kind,
                     source_url="https://example.gov/x", known_since=datetime(known.year, known.month, known.day,
                                                                             tzinfo=IST))  # fmt: skip


def _cov(kind: str, through: date, known: date) -> cal.Coverage:
    return cal.Coverage(type=kind, scope="IN", through=through, source_url="https://example.gov/s",
                        known_since=datetime(known.year, known.month, known.day, tzinfo=IST))  # fmt: skip


FULL = [_cov(k, date(2027, 3, 31), date(2026, 1, 1)) for k in cal.TIER1_TYPES]
T = datetime(2026, 9, 1, 15, 30, tzinfo=IST)


def test_known_at_never_returns_a_row_announced_after_t():
    rows = [
        _ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1)),
        _ev("rbi_mpc", date(2026, 10, 9), date(2026, 9, 2)),
    ]
    got = cal.known_at(rows, T)
    assert [r.day for r in got] == [date(2026, 10, 1)]
    assert all(r.known_since <= T for r in got)


def test_india_day_moves_an_after_close_us_release_to_the_next_session():
    fomc = datetime(2026, 9, 16, 14, 0, tzinfo=ET)  # 23:30 IST on the 16th: after NSE's 15:30 close
    assert cal.india_day(_ev("fomc", date(2026, 9, 16), date(2025, 6, 1), at=fomc, tier=2)) == date(
        2026, 9, 17
    )
    rbi = datetime(2026, 10, 1, 10, 0, tzinfo=IST)  # mid-session
    assert cal.india_day(_ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1), at=rbi)) == date(2026, 10, 1)
    assert cal.india_day(_ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1))) == date(
        2026, 10, 1
    )  # no time: the day


def test_window_with_a_tier1_event_shows_it():
    rows = [_ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1))]
    c = cal.window_check(rows, FULL, start=date(2026, 9, 1), end=date(2026, 10, 27), as_of=T)
    assert c["status"] == "events"
    assert [e["day"] for e in c["tier1"]] == ["2026-10-01"]
    assert c["uncovered"] == []


def test_window_without_events_reads_none_only_when_every_tier1_type_is_covered():
    c = cal.window_check([], FULL, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "none"


def test_missing_coverage_reads_unknown_never_none():
    cov = [c for c in FULL if c.type != "union_budget"]
    c = cal.window_check([], cov, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "unknown"
    assert c["uncovered"] == ["union_budget"]


def test_coverage_that_ends_inside_the_window_reads_unknown():
    cov = [*[c for c in FULL if c.type != "rbi_mpc"], _cov("rbi_mpc", date(2026, 9, 10), date(2026, 1, 1))]
    c = cal.window_check([], cov, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "unknown" and c["uncovered"] == ["rbi_mpc"]


def test_coverage_published_after_t_is_not_used():
    cov = [*[c for c in FULL if c.type != "rbi_mpc"], _cov("rbi_mpc", date(2027, 3, 31), date(2026, 9, 2))]
    c = cal.window_check([], cov, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "unknown" and c["uncovered"] == ["rbi_mpc"]


def test_an_event_announced_after_t_is_invisible_to_the_check():
    rows = [_ev("rbi_mpc", date(2026, 9, 20), date(2026, 9, 5))]  # an off-cycle meeting announced later
    c = cal.window_check(rows, FULL, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "none" and c["tier1"] == []


def test_context_events_are_listed_but_never_make_the_status():
    fomc = datetime(2026, 9, 16, 14, 0, tzinfo=ET)
    rows = [_ev("fomc", date(2026, 9, 16), date(2025, 6, 1), at=fomc, tier=2)]
    c = cal.window_check(rows, FULL, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "none"
    assert [(e["type"], e["india_day"]) for e in c["context"]] == [("fomc", "2026-09-17")]


def test_unknown_calendar_reads_unknown():
    c = cal.window_check(None, None, start=date(2026, 9, 1), end=date(2026, 9, 24), as_of=T)
    assert c["status"] == "unknown" and c["uncovered"] == list(cal.TIER1_TYPES)


# --------------------------------------------------------------------------- the store (database)
def test_store_round_trip_keeps_known_since_and_is_idempotent(env):
    from finresearch.db import session_scope
    from finresearch.eventcal import store

    fomc = datetime(2026, 9, 16, 14, 0, tzinfo=ET)
    rows = [_ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1)), _ev("fomc", date(2026, 9, 16), date(2025, 6, 1),
                                                                      at=fomc, tier=2)]  # fmt: skip
    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))  # another file's rows
    with session_scope() as s:
        assert store.load(s) == ([], [])  # empty: no coverage, so every check reads unknown
        assert store.upsert(s, rows, FULL) == (2, 3)
    with session_scope() as s:
        assert store.upsert(s, rows, FULL) == (0, 0)  # same rows again: nothing new
        events, cov = store.load(s)
    assert sorted(events, key=lambda e: e.day) == sorted(rows, key=lambda e: e.day)
    assert {c.type for c in cov} == set(cal.TIER1_TYPES)
    assert next(e for e in events if e.type == "fomc").at == fomc


def test_store_keeps_the_earliest_known_since_when_a_row_is_seen_again(env):
    from finresearch.db import session_scope
    from finresearch.eventcal import store

    with session_scope() as s:
        store.upsert(s, [_ev("rbi_mpc", date(2026, 12, 4), date(2026, 3, 1))], [])
    with session_scope() as s:  # a later refresh sees the same meeting: when we first knew it does not move
        store.upsert(s, [_ev("rbi_mpc", date(2026, 12, 4), date(2026, 9, 30))], [])
        (e,) = [e for e in store.load(s)[0] if e.day == date(2026, 12, 4)]
    assert e.known_since == datetime(2026, 3, 1, tzinfo=IST)


# --------------------------------------------------------------------------- the trade-plan card's event check
def _plan(calendar):
    import json

    from test_fno_signal import FNO, chain

    from finresearch.adapters.nse_fno import parse_index_history
    from finresearch.signals import fno as fs
    from finresearch.signals import tradeplan as tpl

    hist = json.loads((FNO / "indices_history_NIFTY50_202606_202609.json").read_text())
    closes = [float(c) for _, c in parse_index_history(hist)]
    x = tpl.Inputs(today=date(2026, 9, 29), closes=closes, iv_series=[], capital=1_000_000, max_loss_pct=2,
                   brokerage=20, defined_risk_only=True, rate=0.065, book=tpl.EMPTY_BOOK, calendar=calendar)  # fmt: skip
    return tpl.build(chain(), 65, [fs.LegIn("put", 22600, "sell"), fs.LegIn("put", 22400, "buy")], x)


def test_an_index_plan_whose_window_spans_a_tier1_event_shows_the_check():
    rbi = _ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1), at=datetime(2026, 10, 1, 10, 0, tzinfo=IST))
    card = _plan(([rbi], FULL))  # NIFTY 06-Oct-2026 expiry, planned on 29-Sep-2026
    ec = card["event_check"]
    assert ec["status"] == "events" and [e["day"] for e in ec["tier1"]] == ["2026-10-01"]
    assert ec["window"] == {"from": "2026-09-29", "to": "2026-10-06"}


def test_a_plan_after_the_window_event_reads_none_and_one_without_a_calendar_reads_unknown():
    rbi = _ev("rbi_mpc", date(2026, 10, 9), date(2026, 3, 1))  # after the 06-Oct expiry
    assert _plan(([rbi], FULL))["event_check"]["status"] == "none"
    assert _plan(None)["event_check"]["status"] == "unknown"  # never read: unknown, never "no events"
    assert _plan(([], []))["event_check"]["status"] == "unknown"  # read but empty (no coverage): unknown


def test_the_plan_check_ignores_an_event_announced_after_the_plan_time():
    late = _ev("union_budget", date(2026, 10, 2), date(2026, 9, 30))  # public the day after the 29-Sep plan
    assert _plan(([late], FULL))["event_check"]["status"] == "none"


# --------------------------------------------------------------------------- fnobook.events reads the calendar
def test_event_dates_and_strip_read_the_calendar_only_when_covered(env):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.eventcal import store
    from finresearch.fnobook import events

    today, expiry = date(2026, 9, 29), date(2026, 10, 27)
    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))
    assert asyncio.run(events.event_dates("NIFTY", today, None, until=expiry)) is None  # empty: unknown
    rbi = _ev("rbi_mpc", date(2026, 10, 1), date(2026, 3, 1))
    budget = _ev("union_budget", date(2027, 2, 1), date(2026, 9, 1))
    late = _ev("rbi_mpc", date(2026, 10, 20), date(2026, 10, 2))  # announced after `today`: invisible today
    with session_scope() as s:
        store.upsert(s, [rbi, budget, late], [c for c in FULL if c.type != "general_election"])
    assert asyncio.run(events.event_dates("NIFTY", today, None, until=expiry)) is None  # elections uncovered
    with session_scope() as s:
        store.upsert(s, [], FULL)
    got = asyncio.run(events.event_dates("NIFTY", today, None, until=expiry))
    assert got == [(date(2026, 10, 1), "RBI policy"), (date(2027, 2, 1), "Union Budget")]
    short = [_cov(k, date(2026, 10, 10), date(2026, 1, 1)) for k in cal.TIER1_TYPES]
    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_coverage"))
        store.upsert(s, [], short)
    assert (
        asyncio.run(events.event_dates("NIFTY", today, None, until=expiry)) is None
    )  # coverage ends before expiry
    st = asyncio.run(events.strip("NIFTY", today, None, [date(2026, 10, 6)]))
    assert st["rbi"]["status"] == "ok"
    assert [(x["day"], x["kind"]) for x in st["items"]][:2] == [
        ("2026-10-01", "rbi"),
        ("2026-10-06", "expiry"),
    ]


def test_a_rescheduled_meeting_shows_the_old_day_until_the_notice_and_the_new_day_after():
    old = cal.Event(**{**_ev("rbi_mpc", date(2022, 2, 9), date(2021, 3, 31)).__dict__,
                       "withdrawn_since": datetime(2022, 2, 6, tzinfo=IST)})  # fmt: skip
    new = _ev("rbi_mpc", date(2022, 2, 10), date(2022, 2, 6))
    before, after = datetime(2022, 2, 1, 15, 30, tzinfo=IST), datetime(2022, 2, 7, 15, 30, tzinfo=IST)
    assert [e.day for e in cal.known_at([old, new], before)] == [date(2022, 2, 9)]
    assert [e.day for e in cal.known_at([old, new], after)] == [date(2022, 2, 10)]
