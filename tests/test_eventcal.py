"""The point-in-time event calendar (#367): known_since filtering, coverage (unknown never reads as "no events"), the
India session an event lands in, and the plan's tier-1 window check."""

from datetime import date, datetime, timedelta, timezone

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
