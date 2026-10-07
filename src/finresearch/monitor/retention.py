"""Retention: the weekly prune of data that is only a cache or has a stated lifetime (#247).

Policy (docs/USAGE.md "Retention"):
- HTTP disk cache (data/cache/http): an entry is deleted once it is past the TTL it was written with (stored in its
  meta file since #247; the TTL is still checked on every read). Entries written before that carry no TTL and are
  deleted after LEGACY_MAX_AGE, longer than the longest TTL any caller uses (45 days, evals.ipo_history). A body
  whose meta file is missing (an interrupted write) is deleted after a day.
- Intraday 1-minute series (table intraday_series): rows older than INTRADAY_KEEP_DAYS are deleted. The charts read
  at most the last 5 sessions; a year and a month keeps a full year of sessions for any later study.
- Logs: `finresearch serve --log-file` and `finresearch logpipe` rotate by size (RotatingFileHandler, LOG_MAX_BYTES x
  LOG_BACKUPS), so they never need pruning here.
Never pruned: research packs and reports (data/reports), backups (scripts/backup.sh keeps the newest 14), documents
(data/docs), run transcripts (data/runs), the validation archive (data/archive: append-only and irreplaceable) and
every other table. This module only ever deletes `*.meta.json` / `*.body` files inside the HTTP cache folder and
intraday_series rows.

Schedule: once per ISO week (slot "retention:<year>-W<week>" in alert_eval_slot, so two monitor processes never run
it twice): the first tick of the week from Monday RUN_AFTER IST (before the market opens).
"""

from __future__ import annotations

import json
import logging
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

LEGACY_MAX_AGE = timedelta(days=60)
ORPHAN_MAX_AGE = timedelta(days=1)
INTRADAY_KEEP_DAYS = 400
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUPS = 5
_DONE: set[str] = (
    set()
)  # this process's finished weeks: no claim attempt every minute for the rest of the week
RUN_AFTER = time(3, 0)  # Monday 03:00 IST: no market, no scheduled checks


def _expired(meta: Path, now: datetime) -> bool:
    try:
        data = json.loads(meta.read_text())
        fetched = datetime.fromisoformat(data["fetched_at"])
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ):  # unreadable: it is never served; drop it once it is old
        return now.timestamp() - meta.stat().st_mtime > LEGACY_MAX_AGE.total_seconds()
    ttl = data.get("cache_ttl_s")
    age = (now - fetched).total_seconds()
    return age > (float(ttl) if ttl is not None else LEGACY_MAX_AGE.total_seconds())


def prune_http_cache(cache_dir: Path, now: datetime) -> dict[str, int]:
    """Delete expired HTTP cache entries (meta + body) and stale orphan bodies. Returns counts and bytes freed."""
    out = {"entries": 0, "orphans": 0, "bytes": 0}
    if not cache_dir.is_dir():
        return out
    for meta in cache_dir.glob("*.meta.json"):
        body = meta.with_name(meta.name.removesuffix(".meta.json") + ".body")
        try:
            if not _expired(meta, now):
                continue
            for p in (body, meta):
                if p.exists():
                    out["bytes"] += p.stat().st_size
                    p.unlink()
            out["entries"] += 1
        except OSError:  # removed by a concurrent write or prune
            continue
    for body in cache_dir.glob("*.body"):
        meta = body.with_name(body.name.removesuffix(".body") + ".meta.json")
        try:
            if not meta.exists() and now.timestamp() - body.stat().st_mtime > ORPHAN_MAX_AGE.total_seconds():
                out["bytes"] += body.stat().st_size
                body.unlink()
                out["orphans"] += 1
        except OSError:
            continue
    return out


def prune_intraday(session, today: date, keep_days: int = INTRADAY_KEEP_DAYS) -> int:
    """Delete 1-minute series older than `keep_days` before `today`. Returns rows deleted."""
    from finresearch.db.models import IntradaySeriesRow

    cutoff = today - timedelta(days=keep_days)
    res = session.execute(delete(IntradaySeriesRow).where(IntradaySeriesRow.day < cutoff))
    return int(res.rowcount or 0)


def due_slot(now: datetime) -> str | None:
    ist = to_ist(now)
    if ist.weekday() == 0 and ist.time() < RUN_AFTER:  # the week's run starts on Monday from RUN_AFTER
        return None
    year, week, _ = ist.date().isocalendar()
    return f"retention:{year}-W{week:02d}"


def prune(now: datetime, *, cache_dir: Path | None = None) -> dict[str, Any]:
    """Run the whole policy once (the CLI and the weekly job)."""
    from finresearch.adapters.http import DEFAULT_CACHE_DIR
    from finresearch.db import session_scope

    started = _time.monotonic()
    out: dict[str, Any] = {"http_cache": prune_http_cache(cache_dir or DEFAULT_CACHE_DIR, now)}
    with session_scope() as s:
        out["intraday_rows"] = prune_intraday(s, to_ist(now).date())
    out["seconds"] = round(_time.monotonic() - started, 1)
    return out


async def retention_step(now: datetime, *, cache_dir: Path | None = None) -> dict[str, Any]:
    """The monitor's weekly prune: claimed per ISO week; a failure is retried (monitor.disclosures.MAX_ATTEMPTS)."""
    import asyncio

    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot
    from finresearch.monitor.disclosures import MAX_ATTEMPTS, claim, finish

    slot = due_slot(now)
    if slot is None or slot in _DONE:
        return {}
    if not claim(slot, now):  # another process holds or finished the week (a failed run is retried by claim)
        with session_scope() as s:
            row = s.get(AlertEvalSlot, slot)
            r = (row.result or {}) if row is not None else {}
            if (
                r.get("status") == "done" or int(r.get("attempts", 1)) >= MAX_ATTEMPTS
            ):  # nothing left this week
                _DONE.add(slot)
        return {}
    try:
        res = await asyncio.to_thread(prune, now, cache_dir=cache_dir)
    except Exception as e:
        log.warning("retention prune failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"retention": "failed"}
    finish(slot, res, now, failed=False)
    _DONE.add(slot)
    return {"retention": res}
