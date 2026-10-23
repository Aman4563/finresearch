"""Store NSE's daily FII derivative statistics (#371, P1.9; adapters.nse_fii_stats): one .xls a day, one fii_stats
row per day and category.

`backfill` (`finresearch fno fii-stats`) and the nightly `update` (monitor.fno_bhav.fii_stats_step) run
fnohist.participants' resumable backfill with this file as its Source, so the same rules hold: the owner's
data-rights decision is required before the first request (`data_rights.require("fii_stats")`, PENDING until the
owner records it), the shared NSE budget plus `pause`, no backfill download between 09:00 and 16:00 IST on weekdays,
a stop after two refusals (403/429) or two failures in a row (a block page is a failure, never "no file"), the
manifest in fno_participant_day (dataset "fii_stats") so a re-run resumes, the bhavcopy manifest and holiday list to
skip non-sessions, and raw workbooks kept under data/fno_participant/<year>/ (gitignored) unless keep_raw=False. Each
day is written in one transaction with its lineage row (dq.lineage: the served body's sha256, the parser version).

What the numbers are: FIIs' (and, as NSE classifies them, FPIs') contracts and Rs crore bought and sold that day and
their open interest at the day's end, per category. Positioning context only; no study verified for this app shows
that they predict index moves.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from finresearch.adapters import nse_fii_stats as fs
from finresearch.db.models import FiiStats
from finresearch.fnohist import participants

LOOKBACK_DAYS = participants.LOOKBACK_DAYS


def save_day(s: Session, day: date, parsed: fs.FiiStatsDay, url: str) -> int:
    if parsed.day != day:
        raise fs.FiiStatsError(f"the file for {day} says it is for {parsed.day}")
    s.execute(delete(FiiStats).where(FiiStats.day == day))
    for category, vals in parsed.rows.items():
        s.add(FiiStats(day=day, category=category, source=url, **vals))
    s.flush()
    return len(parsed.rows)


def load_day(s: Session, day: date, url: str, content: bytes) -> int:
    """Replace `day`'s rows with the workbook's and record the manifest and lineage (in the caller's transaction).
    Raises FiiStatsError for a workbook in another layout or dated another day."""
    from finresearch.dq.lineage import record_load

    n = save_day(s, day, fs.parse(content), url)
    participants._manifest(s, SOURCE, day, status="loaded", url=url, rows=n, error=None)
    record_load(s, fs.DATASET, day, content, url=url, parser_version=fs.PARSER_VERSION, rows=n)
    return n


SOURCE = participants.Source(fs.DATASET, fs.url_for, fs.fetch_day, load_day, FiiStats)


async def backfill(start: date, end: date | None = None, **kw: Any) -> dict[str, Any]:
    """participants.backfill for this file only (same keywords and result shape, keyed "fii_stats")."""
    return await participants.backfill(start, end, kinds=(SOURCE,), **kw)


async def update(today: date, *, http: Any = None, lookback: int = LOOKBACK_DAYS,
                 holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    """The nightly load: the missing sessions in [today - lookback, today]. Returns the dataset's counts."""
    res = await backfill(today - timedelta(days=lookback), today, today=today, http=http, holidays=holidays,
                         keep_raw=False, market_hours=False)  # fmt: skip
    return res[fs.DATASET]
