"""Forward quotes, intraday archive, IPO tables, Groww-vs-official closes and the disk check (#368). Synthetic rows,
made-up symbols; test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

DAY = date(2026, 9, 30)
TABLES = "option_quote, intraday_series, ipo_history, ipo_issue, groww_close_check"


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def by_check(checks):
    return {c.check: c for c in checks}


def quote(at: datetime, flags: int = 0):
    from finresearch.db.models import OptionQuote

    return OptionQuote(snapshot_at=at, requested_at=at, exchange_at=at, expiry=date(2026, 10, 27), strike=2500000,
                       flags=flags, underlying="NIFTY", option_type="CE", source="groww")  # fmt: skip


def test_quote_snapshots_counted_against_the_registered_times(clean):
    from finresearch.db import session_scope
    from finresearch.dq import misc
    from finresearch.fincalc.dates import IST
    from finresearch.fnohist import quotes as fq

    times = [datetime.combine(DAY, t, IST).astimezone(UTC) for t in fq.SNAPSHOT_TIMES]
    with session_scope() as s:
        assert by_check(misc.quote_checks(s, DAY))["snapshots"].status == "fail"  # none at all
        for t in times[:-1]:
            s.add(quote(t))
        s.flush()
        res = by_check(misc.quote_checks(s, DAY))
        assert (res["snapshots"].status, res["snapshots"].value) == ("fail", len(times) - 1)
        s.add(quote(times[-1], flags=fq.STALE))
        s.flush()
        res = by_check(misc.quote_checks(s, DAY))
        assert res["snapshots"].status == "ok"
        assert res["stale_share"].value == pytest.approx(1 / len(times))


def test_intraday_archive_indices(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import IntradaySeriesRow
    from finresearch.dq import misc
    from finresearch.monitor.intraday import ARCHIVE_INDICES

    with session_scope() as s:
        for sym in ARCHIVE_INDICES[1:]:
            s.add(IntradaySeriesRow(kind="index", symbol=sym, day=DAY, complete=True, source="synthetic"))
        s.flush()
        c = by_check(misc.intraday_checks(s, DAY))["indices_complete"]
    assert c.status == "fail" and ARCHIVE_INDICES[0] in c.note


def test_ipo_transient_errors_kept_as_final_and_stale_registry(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory, IpoIssue
    from finresearch.dq import misc

    now = datetime(2026, 9, 30, 15, 0, tzinfo=UTC)
    with session_scope() as s:
        s.add(IpoHistory(symbol="FAKESME", series="SME", status="error",
                         missing=["fetch failed: ReadTimeout: timed out"]))  # fmt: skip
        s.add(IpoHistory(symbol="FAKEDNS", series="SME", status="error",
                         missing=["fetch failed: ConnectError: [Errno 8] nodename nor servname provided"]))  # fmt: skip
        s.add(
            IpoHistory(
                symbol="FAKEREAL", series="EQ", status="error", missing=["fetch failed: NseError: 404"]
            )
        )
        s.add(IpoIssue(exchange="NSE", issue_key="FAKEIPO", name="Fake Ltd", board="sme", status="open",
                       first_seen=now - timedelta(days=9), last_seen=now - timedelta(days=3)))  # fmt: skip
        s.flush()
        res = by_check(misc.ipo_checks(s, now))
    assert (res["transient_errors_final"].status, res["transient_errors_final"].value) == ("warn", 2)
    assert res["registry_fresh"].status == "fail"


def test_groww_close_warns_only_beyond_the_ltp_vs_vwap_tolerance(clean):
    """Groww's close is the last traded price, the official close the 30-minute weighted average: a few paise apart on
    most stocks (live: median 0.1 %, max 1.2 %). Only a difference beyond CLOSE_WARN_TOL (2 %) warns; `matched`
    (₹0.01) is untouched. Expected values by hand: 101.5/100 = 1.5 % (within), 103/100 = 3 % (beyond)."""
    from finresearch.db import session_scope
    from finresearch.db.models import GrowwCloseCheck
    from finresearch.dq import misc

    def add(s, sym, groww, official):
        s.add(GrowwCloseCheck(day=DAY, exchange="NSE", symbol=sym, groww_close=Decimal(groww),
                              official_close=Decimal(official), official_source="synthetic",
                              matched=abs(Decimal(groww) - Decimal(official)) <= Decimal("0.01")))  # fmt: skip

    assert Decimal("0.02") == misc.CLOSE_WARN_TOL
    with session_scope() as s:
        assert by_check(misc.close_checks(s, DAY))["groww_vs_official"].status == "unknown"
        add(s, "FAKEA", "100", "100")
        add(s, "FAKEB", "101.5", "100")  # 1.5 %: LTP vs VWAP, not a warning
        add(s, "FAKEC", "99.9", "100")
        s.flush()
        c = by_check(misc.close_checks(s, DAY))["groww_vs_official"]
        assert (c.status, c.value, c.threshold) == ("ok", 0, 0.02)
        assert "2 of 3 differ by more than ₹0.01" in c.note and "max 1.50 %" in c.note
        add(s, "FAKED", "103", "100")  # 3 %: beyond tolerance
        s.flush()
        c = by_check(misc.close_checks(s, DAY))["groww_vs_official"]
    assert (c.status, c.value) == ("warn", 1)
    assert "1/4 Groww closes differ from the official close by more than 2 %" in c.note and "max 3.00 %" in c.note


def test_groww_close_with_no_official_close_is_never_ok(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import GrowwCloseCheck
    from finresearch.dq import misc

    with session_scope() as s:
        s.add(GrowwCloseCheck(day=DAY, exchange="NSE", symbol="FAKEZ", groww_close=Decimal(100),
                              official_close=Decimal(0), official_source="synthetic", matched=False))  # fmt: skip
        s.flush()
        c = by_check(misc.close_checks(s, DAY))["groww_vs_official"]
    assert c.status == "unknown" and "1 without an official close" in c.note


def test_disk_check(tmp_path):
    from finresearch.dq import misc

    assert misc.disk_check(tmp_path, free_bytes=lambda _p: 9 * 2**30).status == "fail"
    assert misc.disk_check(tmp_path, free_bytes=lambda _p: 11 * 2**30).status == "ok"
    assert misc.ingest_allowed(tmp_path, free_bytes=lambda _p: 9 * 2**30) is False
