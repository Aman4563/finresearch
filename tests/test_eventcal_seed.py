"""The curated event-calendar seed (#367): every date has an official source URL, no row is known before it was
announced or after it happened, and replaying any day never exposes a row announced later (the backtest rule)."""

from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from finresearch.eventcal import calendar as cal
from finresearch.eventcal import seed
from finresearch.fincalc.dates import session_clock

OFFICIAL = ("rbi.org.in", "indiabudget.gov.in", "pib.gov.in", "eci.gov.in", "federalreserve.gov", "bls.gov",
            "mospi.gov.in", "nseindia.com", "newsonair.gov.in")  # fmt: skip


def _official(url: str | None) -> bool:
    u = urlparse(url or "")
    return u.scheme == "https" and any(
        u.hostname == h or (u.hostname or "").endswith("." + h) for h in OFFICIAL
    )


def test_every_seed_row_cites_an_official_source():
    assert seed.EVENTS and seed.COVERAGE
    for e in seed.EVENTS:
        assert _official(e.source_url), e
        assert e.known_since_url is None or _official(e.known_since_url), e
    for c in seed.COVERAGE:
        assert _official(c.source_url), c


def test_seed_rows_are_unique_tiered_and_known_before_they_happen():
    assert not [k for k, n in Counter((e.type, e.scope, e.day) for e in seed.EVENTS).items() if n > 1]
    for e in seed.EVENTS:
        assert e.tier == (1 if e.type in cal.TIER1_TYPES else 2), e
        assert e.known_since.tzinfo is not None and (e.at is None or e.at.tzinfo is not None), e
        assert e.known_since.date() <= cal.india_day(e), e  # announced no later than the session it moves
        assert e.at is None or e.known_since <= e.at, e
    for c in seed.COVERAGE:
        assert c.type in cal.TIER1_TYPES and c.known_since.date() <= c.through, c


def test_replaying_any_day_never_sees_a_row_announced_later():
    """The backtest rule (known_since <= t): a replay of every session from 2022 to the seed's end, checking a
    30-day window each day, never returns an event or uses coverage published after that day's close."""
    d, end = date(2022, 1, 3), max(e.day for e in seed.EVENTS)
    while d <= end:
        t = session_clock(d)
        c = cal.window_check(seed.EVENTS, seed.COVERAGE, start=d, end=d + timedelta(days=30), as_of=t)
        for row in c["tier1"] + c["context"]:
            assert datetime.fromisoformat(row["known_since"]) <= t, (d, row)
        for k in cal.TIER1_TYPES:
            assert all(x.known_since <= t for x in cal.known_at(seed.COVERAGE, t) if x.type == k)
        d += timedelta(days=1)


def test_cli_seed_loads_the_curated_rows_once(env):
    from sqlalchemy import text
    from typer.testing import CliRunner

    from finresearch import cli
    from finresearch.db import session_scope
    from finresearch.eventcal import store

    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))
    r = CliRunner().invoke(cli.app, ["events", "seed"])
    assert r.exit_code == 0, r.output
    assert f"{len(seed.EVENTS)} new events" in r.output
    again = CliRunner().invoke(cli.app, ["events", "seed"])
    assert "0 new events" in again.output
    with session_scope() as s:
        ev, cov = store.load(s)
    assert len(ev) == len(seed.EVENTS) and len(cov) == len(seed.COVERAGE)
