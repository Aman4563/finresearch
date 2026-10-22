"""#366: freshness and content checks for the FBIL rate series in the nightly data-quality run (dq.run, dq.rates).
FBIL's public pages lag about a week, so freshness allows MAX_LAG sessions; a never-loaded series is unknown, not ok.
Synthetic rows; test database only."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

NIGHT = datetime(2026, 9, 30, 15, 30, tzinfo=UTC)  # Wed 21:00 IST
FREE = lambda _p: 50 * 2**30  # noqa: E731


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE rate_curve, dataset_load, dq_check, dataset_status"))
    yield env


def _rate(s, series, day, tenor, r):
    from finresearch.db.models import RateCurve

    s.add(RateCurve(series=series, day=day, tenor=tenor, tenor_years=Decimal("0.25"), rate=Decimal(r)))


def _status(s, name):
    from finresearch.db.models import DatasetStatus

    r = s.scalar(select(DatasetStatus).where(DatasetStatus.dataset == name))
    return None if r is None else (r.status, r.lag_sessions, list(r.failed or []))


def test_rate_series_are_checked_with_the_public_lag_allowed(clean):
    from finresearch.db import session_scope
    from finresearch.dq import run

    with session_scope() as s:
        for tenor in ("0.25Y", "10Y"):
            _rate(
                s, "gsec_par", date(2026, 9, 24), tenor, "0.055"
            )  # 4 sessions behind 30-Sep: within the lag
        _rate(s, "tbill", date(2026, 9, 24), "3M", "0.25")  # 25 %: out of range
        _rate(s, "mibor_on", date(2026, 9, 14), "ON", "0.055")  # 12 sessions behind: stale
    with session_scope() as s:
        run.run_all(s, NIGHT, holidays=set(), free_bytes=FREE)
    with session_scope() as s:
        assert _status(s, "rate_gsec_par") == ("ok", 4, [])
        assert _status(s, "rate_tbill")[0] == "fail" and "row_ranges" in _status(s, "rate_tbill")[2]
        assert _status(s, "rate_mibor_on")[0] == "fail" and "freshness" in _status(s, "rate_mibor_on")[2]
        assert _status(s, "rate_mibor_term")[0] == "unknown"  # never loaded: never ok


def test_missing_expected_tenor_fails(clean):
    from finresearch.db import session_scope
    from finresearch.dq import rates

    with session_scope() as s:
        _rate(s, "gsec_par", date(2026, 9, 24), "0.25Y", "0.055")  # no 10-year point
    with session_scope() as s:
        got = {c.check: c for c in rates.checks_for("gsec_par")(s, date(2026, 9, 24))}
    assert got["expected_tenors"].status == "fail" and "10Y" in got["expected_tenors"].note
    assert got["reupload"].status == "unknown" and got["reupload"].critical is False


def test_report_labels_every_dataset():
    from finresearch.dq import report, run

    assert {d.name for d in run.DATASETS} <= set(report.LABELS)
