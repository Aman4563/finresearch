"""Store and read NSE's participant-wise F&O files (#317, #371; adapters.nse_participant_oi): open interest
(fno_participant_oi) and trading volume (fno_participant_vol), one small CSV per dataset per day.

`backfill` (`finresearch fno participants`) loads [start, end] for both datasets and the nightly job (`update`, from
monitor.fno_bhav) runs it over the last few days. A day is written in one transaction per dataset (all four
participants or nothing) with its lineage row (dq.lineage: the served body's sha256, the parser version). `latest`
returns the newest stored OI day with the day-over-day change against the stored day before it.

Resume: `fno_participant_day` records every (dataset, day) tried. Loaded days and days already in the table (the OI
rows #317's nightly job stored before the manifest existed) are skipped; a 404 day (no_file) is skipped once it is
older than RECHECK_DAYS. Requests are kept to likely sessions, as the bhavcopy manifest (fno_bhav_day) says (#354 i): a
weekend is fetched only when the bhavcopy loaded a file that day (a special session), a weekday is skipped when the
bhavcopy found no file for it or it is a known NSE trading holiday; other weekdays are fetched (a holiday then answers
404, ~1 KB). The manifest starts in 2022, so pre-2022 weekend special sessions (budget Saturdays, Diwali muhurat) are
not fetched.

Politeness (owner decision #362, finresearch.data_rights): every request goes through the shared NSE budget
(adapters.http) and `pause` adds seconds between downloads; a dataset stops after REFUSALS_TO_STOP refusals (HTTP
403/429) in a row or MAX_CONSECUTIVE_FAILURES failures of any kind (a block page is a failure, never "no file"), and a
backfill makes no download between 09:00 and 16:00 IST on a weekday (re-run after the close to continue). Raw CSVs
are kept under data/fno_participant/<year>/ (gitignored, ~1 KB a file) unless keep_raw=False, so a parser change
re-reads them without downloading again.

What the numbers are: contracts open at the day's end (OI) or traded that day (volume) by participant category, as
NSE classifies them. They describe positioning; no study verified for this app shows that they predict index moves
(see signals.fno_context EVIDENCE).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_participant_oi as po
from finresearch.db.models import FnoParticipantDay, FnoParticipantOi, FnoParticipantVol

log = logging.getLogger(__name__)
LOOKBACK_DAYS = 5
RECHECK_DAYS = 3
MAX_CONSECUTIVE_FAILURES = 2  # a small file a day: two failures in a row are a pattern, not noise
REFUSALS_TO_STOP = 2
TABLES: dict[str, type[FnoParticipantOi] | type[FnoParticipantVol]] = {
    po.OI.dataset: FnoParticipantOi, po.VOL.dataset: FnoParticipantVol}  # fmt: skip


@dataclass(frozen=True)
class Source:
    """One daily archive file the backfill can load: its dataset name (table, manifest, lineage, data rights), URL,
    fetch (None on 404) and load (rows into the caller's transaction, with manifest and lineage). The participant
    files are po.Kind (see `_source`); fnohist.fii_stats.SOURCE is the FII statistics workbook (#371)."""

    dataset: str
    url_for: Callable[[date], str]
    fetch: Callable[[Any, date], Any]  # async (http, day) -> (url, body) | None
    load: Callable[[Session, date, str, bytes], int]
    table: Any  # the ORM table with a `day` column


def _source(k: po.Kind | Source) -> Source:
    if isinstance(k, Source):
        return k
    return Source(k.dataset, k.url_for, lambda http, day: po.fetch_day(http, day, k),
                  lambda s, day, url, body: load_day(s, k, day, url, body), TABLES[k.dataset])  # fmt: skip


def save_day(s: Session, day: date, parsed: po.ParticipantDay, url: str, kind: po.Kind = po.OI) -> int:
    if parsed.day is not None and parsed.day != day:
        raise po.ParticipantFileError(f"the file for {day} says it is as on {parsed.day}")
    table = TABLES[kind.dataset]
    s.execute(delete(table).where(table.day == day))
    for who, counts in parsed.rows.items():
        s.add(table(day=day, client_type=who, source=url, **counts))
    s.flush()
    return len(parsed.rows)


def _manifest(s: Session, kind: po.Kind | Source, day: date, **values: Any) -> None:
    stmt = pg_insert(FnoParticipantDay).values(dataset=kind.dataset, day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["dataset", "day"], set_={
        **values, "attempts": FnoParticipantDay.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def load_day(s: Session, kind: po.Kind, day: date, url: str, content: bytes) -> int:
    """Replace `day`'s rows of `kind` with the file's and record its lineage (in the caller's transaction). Raises
    ParticipantFileError for a file that is not this kind's, is dated another day, or does not add up."""
    from finresearch.dq.lineage import record_load

    n = save_day(s, day, po.parse(content.decode("utf-8", "replace"), kind), url, kind)
    _manifest(s, kind, day, status="loaded", url=url, rows=n, error=None)
    record_load(s, kind.dataset, day, content, url=url, parser_version=po.PARSER_VERSION, rows=n)
    return n


def mark(s: Session, kind: po.Kind | Source, day: date, status: str, error: str | None = None) -> None:
    _manifest(s, kind, day, status=status, error=None if error is None else error[:300])


def stored_days(s: Session, start: date, end: date, kind: po.Kind | Source = po.OI) -> set[date]:
    table = _source(kind).table
    return set(s.scalars(select(table.day).where(table.day.between(start, end)).distinct()))


def bhav_sessions(s: Session, start: date, end: date) -> dict[date, str]:
    """{day: status} from the bhavcopy manifest (fno_bhav_day) between start and end."""
    from finresearch.db.models import FnoBhavDay

    q = select(FnoBhavDay.day, FnoBhavDay.status).where(FnoBhavDay.day.between(start, end))
    return {d: st for d, st in s.execute(q)}


def plan_days(s: Session, kind: po.Kind | Source, start: date, end: date, today: date,
              holidays: set[date] | None = None) -> list[date]:  # fmt: skip
    """The days a run still has to fetch for `kind`, oldest first (see the module docstring)."""
    from finresearch.adapters.nse_holidays import known_trading_holidays

    done = {r.day: r for r in s.scalars(select(FnoParticipantDay).where(
        FnoParticipantDay.dataset == kind.dataset, FnoParticipantDay.day.between(start, end)))}  # fmt: skip
    have = stored_days(s, start, end, kind)
    bhav = bhav_sessions(s, start, end)
    stale = today - timedelta(days=RECHECK_DAYS)
    years: dict[int, set[date] | None] = {}
    out, d = [], start
    while d <= min(end, today):
        m = done.get(d)
        if holidays is None and d.year not in years:
            years[d.year] = known_trading_holidays(d)
        hol = holidays if holidays is not None else years[d.year]
        if d in have or (m is not None and (m.status == "loaded" or (m.status == "no_file" and d < stale))):
            pass
        elif bhav.get(d) == "loaded":
            out.append(d)  # a session on record, weekend or not
        elif d.weekday() >= 5 or bhav.get(d) == "no_file" or (hol and d in hol):
            pass  # a weekend without a session on record, or a weekday holiday: no request
        else:
            out.append(d)
        d += timedelta(days=1)
    return out


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "fno_participant"


def raw_path(kind: po.Kind | Source, day: date) -> Path:
    return raw_dir() / str(day.year) / kind.url_for(day).rsplit("/", 1)[1]


def _counts(n: int) -> dict[str, Any]:
    return {"planned": n, "loaded": 0, "no_file": 0, "failed": {}, "from_raw": 0}


async def _one(http: Any, kind: Source, day: date, keep_raw: bool, pause: float, res: dict[str, Any]) -> str:
    """Fetch (or re-read) and store one file; returns loaded / no_file. Errors propagate to the caller."""
    p = raw_path(kind, day)
    if p.is_file():
        got: tuple[str, bytes] | None = (kind.url_for(day), p.read_bytes())
        res["from_raw"] += 1
    else:
        got = await kind.fetch(http, day)
        if pause:
            await asyncio.sleep(pause)
        if got is not None and keep_raw:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(got[1])
    from finresearch.db import session_scope

    with session_scope() as s:
        if got is None:
            mark(s, kind, day, "no_file")
            return "no_file"
        kind.load(s, day, *got)
    return "loaded"


async def backfill(start: date, end: date | None = None, *, kinds: tuple[po.Kind | Source, ...] = po.KINDS,
                   today: date | None = None, http: Any = None, holidays: set[date] | None = None,
                   pause: float = 0.0, keep_raw: bool = True, market_hours: bool = True,
                   now: Callable[[], datetime] | None = None,
                   progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store every missing day in [start, end] for each kind. Resumable and idempotent (module docstring).
    Returns per dataset the counts by outcome, the failed days with their errors and `stopped` when that dataset
    stopped early; a top-level `stopped` when the whole run stopped (market hours). `market_hours=False` is for the
    nightly job, which the monitor runs only after the close."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist
    from finresearch.refseries.index_eod import in_market_hours, refused

    kinds = tuple(_source(k) for k in kinds)
    for k in kinds:
        data_rights.require(k.dataset)
    now = now or now_ist
    today = today or today_ist()
    end = min(end or today, today)
    with session_scope() as s:
        plans = {k.dataset: set(plan_days(s, k, start, end, today, holidays)) for k in kinds}
    out: dict[str, Any] = {k.dataset: _counts(len(plans[k.dataset])) for k in kinds}
    days = sorted(set().union(*plans.values()))
    streak = {k.dataset: 0 for k in kinds}
    refusals = dict(streak)
    own = http is None
    http = http or PoliteClient()
    try:
        for i, day in enumerate(days, 1):
            todo = [k for k in kinds if day in plans[k.dataset] and "stopped" not in out[k.dataset]]
            if not todo:
                continue
            if market_hours and in_market_hours(now()) and any(not raw_path(k, day).is_file() for k in todo):
                out["stopped"] = (
                    "market hours (09:00-16:00 IST): no backfill downloads; re-run after the close"
                )
                break
            for k in todo:
                res = out[k.dataset]
                try:
                    status = await _one(http, k, day, keep_raw, pause, res)
                    res[status] += 1
                    streak[k.dataset] = refusals[k.dataset] = 0
                except Exception as e:  # a refusal, a block page, a bad file: recorded, retried next run
                    status = "failed"
                    res["failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                    with session_scope() as s:
                        mark(s, k, day, "failed", res["failed"][day.isoformat()])
                    streak[k.dataset] += 1
                    refusals[k.dataset] = refusals[k.dataset] + 1 if refused(e) else 0
                    if refusals[k.dataset] >= REFUSALS_TO_STOP:
                        res["stopped"] = (f"NSE refused {refusals[k.dataset]} requests in a row (403/429): stopped "
                                          f"{k.dataset}; pause it and check before re-running (#362)")  # fmt: skip
                    elif streak[k.dataset] >= MAX_CONSECUTIVE_FAILURES:
                        res["stopped"] = f"{streak[k.dataset]} failures in a row: stopped to stay polite"
                if progress:
                    progress(i, len(days), day, f"{k.dataset.removeprefix('fno_participant_')} {status}")
    finally:
        if own:
            await http.aclose()
    return out


async def update(today: date, *, http: Any = None, lookback: int = LOOKBACK_DAYS,
                 holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    """The nightly load: both files for the missing sessions in [today - lookback, today]. Returns the counts summed
    over the datasets, failed days keyed "<dataset> <day>", `stopped` when a dataset stopped, and `by_dataset`."""
    res = await backfill(today - timedelta(days=lookback), today, today=today, http=http, holidays=holidays,
                         keep_raw=False, market_hours=False)  # fmt: skip
    by = {k: v for k, v in res.items() if k != "stopped"}
    out: dict[str, Any] = {key: sum(v[key] for v in by.values()) for key in ("planned", "loaded", "no_file")}
    out["failed"] = {f"{ds} {d}": err for ds, v in by.items() for d, err in v["failed"].items()}
    stops = [f"{ds}: {v['stopped']}" for ds, v in by.items() if "stopped" in v]
    if stops:
        out["stopped"] = "; ".join(stops)
    out["by_dataset"] = by
    return out


def _row(r: FnoParticipantOi) -> dict[str, int]:
    return {k: int(getattr(r, k)) for k in po.KEYS}


def latest(s: Session) -> dict[str, Any] | None:
    """The newest stored day: per participant its counts, net positions and the change since the previous stored
    day (None when there is no previous day). None when nothing is stored."""
    last = s.scalar(select(FnoParticipantOi.day).order_by(FnoParticipantOi.day.desc()).limit(1))
    if last is None:
        return None
    prev = s.scalar(select(FnoParticipantOi.day).where(FnoParticipantOi.day < last)
                    .order_by(FnoParticipantOi.day.desc()).limit(1))  # fmt: skip
    rows = {
        r.client_type: _row(r)
        for r in s.scalars(select(FnoParticipantOi).where(FnoParticipantOi.day == last))
    }
    before = ({r.client_type: _row(r) for r in s.scalars(select(FnoParticipantOi).where(FnoParticipantOi.day == prev))}
              if prev else {})  # fmt: skip
    src = s.scalar(select(FnoParticipantOi.source).where(FnoParticipantOi.day == last).limit(1))
    out = []
    for who in po.PARTICIPANTS:
        r = rows.get(who)
        if r is None:
            continue
        p = before.get(who)
        nets = net_positions(r)
        out.append({"participant": who, "counts": r, "net": nets,
                    "net_change": None if p is None else {k: v - net_positions(p)[k] for k, v in nets.items()}})  # fmt: skip
    return {
        "day": last.isoformat(),
        "previous_day": prev.isoformat() if prev else None,
        "rows": out,
        "source": src,
    }


def net_positions(r: dict[str, int]) -> dict[str, int]:
    """Long minus short contracts per bucket. For options this counts contracts, not delta: a long put is bearish
    exposure but counts +1 in `index_puts`."""
    return {"index_futures": r["fut_idx_long"] - r["fut_idx_short"],
            "stock_futures": r["fut_stk_long"] - r["fut_stk_short"],
            "index_calls": r["opt_idx_call_long"] - r["opt_idx_call_short"],
            "index_puts": r["opt_idx_put_long"] - r["opt_idx_put_short"]}  # fmt: skip
