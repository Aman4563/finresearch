"""Checks on the forward quote archive, the intraday archive, the IPO tables, Groww's closes and the disk (#368).

* quotes: one snapshot per registered time (fnohist.quotes.SNAPSHOT_TIMES) on a session, and the share of quotes
  flagged STALE (a last trade older than STALE_AFTER at the request).
* intraday: every ARCHIVE_INDICES series archived and complete for the session.
* IPO history: 'error' rows whose only reason is a transient network failure (ReadTimeout, DNS) are re-fetched by the
  nightly harvest with a backoff (evals.ipo_history.retry_due, #433); those that used up their attempts are a failure
  kept as data and counted until they are re-harvested. The issue registry is refreshed twice a day, so a newest row
  older than REGISTRY_MAX_AGE means the refresh has stopped.
* Groww vs official close (reconciliation d, groww_close_check): closes beyond CLOSE_WARN_TOL on the day (Groww's
  last trade vs the exchange's 30-minute weighted average differ by paise on most stocks). Public prices, no holdings.
* disk: below MIN_FREE_GIB free on the data volume, ingests stop (`ingest_allowed`) and the check fails.
* backup (#507): the age of the newest good dump and the last run's outcome, from the key=value file
  scripts/backup.sh writes on every run (<state dir>/backup_status). A refused or failed last run fails at once; a
  good one older than BACKUP_MAX_AGE_H fails too (the job stopped running); no record is unknown, never ok.
* pgvector (#507): a NEW database session must be able to load the extension's library. A pooled session that
  loaded it before the file went missing keeps working, so the probe opens its own connection.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finresearch.db.models import GrowwCloseCheck, IntradaySeriesRow, IpoHistory, IpoIssue, OptionQuote
from finresearch.dq.base import Check, share_status
from finresearch.fincalc.dates import IST

STALE_SHARE = 0.5
REGISTRY_MAX_AGE = timedelta(days=2)
MIN_FREE_GIB = 10
# [U] the nightly dump runs at 02:30 local time (docs/BACKUPS.md) and the passes at 08:00 and 21:00 IST, so a good
# backup is at most ~19 h old at a pass; 36 h allows one late run (launchd runs a missed job at wake), not two nights
BACKUP_MAX_AGE_H = 36
BACKUP_OUTCOMES = {  # scripts/backup.sh's OUTCOME values, as the brief words them
    "refused_plaintext_secrets": "was REFUSED: plaintext secrets are still in the database (run `uv run finresearch "
                                 "secrets migrate --apply`)",
    "database_unreachable": "could not reach the database",
    "secrets_check_failed": "could not run the secrets check",
    "offsite_failed": "wrote the local dump but the off-machine copy failed",
    "failed": "failed (see data/backups/backup.log)",
}  # fmt: skip
VECTOR_PROBE_SQL = "select '[1]'::vector"  # the type's input function lives in the library: this loads it
# Groww's close is the last traded price; NSE's official closing price is the volume-weighted average price of the
# trades in the last 30 minutes of the continuous session, or the last traded price when there was no trade in that
# window (NSE's closing-price method as brokers' explainers quote it, e.g. Kotak Securities' "Why is there a difference
# between last traded price and closing price on NSE?"; BSE uses the same 30-minute average). F&O stocks moved to a
# closing auction session in Aug-2026 per broker notes (not confirmed from an NSE circular here); its price also differs
# from the last trade. Live groww_close_check rows (Oct-2026): median |diff| 0.1 %, max 1.2 %. Beyond 2 % the gap is
# more than the method explains (a wrong day, instrument or an unadjusted corporate action), so only that warns.
CLOSE_WARN_TOL = Decimal("0.02")


def _ist_day(day: date) -> tuple[datetime, datetime]:
    a = datetime.combine(day, datetime.min.time(), IST)
    return a, a + timedelta(days=1)


def quote_checks(s: Session, day: date) -> list[Check]:
    from finresearch.fnohist import quotes as fq

    a, b = _ist_day(day)
    n_snap = s.scalar(select(func.count(func.distinct(OptionQuote.snapshot_at)))
                      .where(OptionQuote.snapshot_at >= a, OptionQuote.snapshot_at < b)) or 0  # fmt: skip
    want = len(fq.SNAPSHOT_TIMES)
    out = [Check("option_quote", "snapshots", "ok" if n_snap >= want else "fail", n_snap, want,
                 None if n_snap >= want else f"{n_snap} of {want} snapshots on {day}", critical=False)]  # fmt: skip
    n, stale = s.execute(select(func.count(), func.count().filter(OptionQuote.flags.op("&")(fq.STALE) != 0))
                         .where(OptionQuote.snapshot_at >= a, OptionQuote.snapshot_at < b)).one()  # fmt: skip
    st, v = share_status(stale, n, STALE_SHARE)
    out.append(Check("option_quote", "stale_share", "warn" if st == "fail" else st, v, STALE_SHARE,
                     f"{stale}/{n} quotes stale", critical=False))  # fmt: skip
    return out


def intraday_checks(s: Session, day: date) -> list[Check]:
    from finresearch.monitor.intraday import ARCHIVE_INDICES

    done = set(s.scalars(select(IntradaySeriesRow.symbol).where(
        IntradaySeriesRow.kind == "index", IntradaySeriesRow.day == day, IntradaySeriesRow.complete.is_(True))))  # fmt: skip
    miss = [x for x in ARCHIVE_INDICES if x not in done]
    return [Check("intraday_series", "indices_complete", "fail" if miss else "ok", len(miss), 0,
                  f"not archived: {', '.join(miss)}" if miss else None, critical=False)]  # fmt: skip


def ipo_checks(s: Session, now: datetime) -> list[Check]:
    from finresearch.evals.ipo_history import MAX_FETCH_ATTEMPTS, fetch_attempts, transient_only

    errs = s.execute(select(IpoHistory.missing, IpoHistory.source).where(IpoHistory.status == "error")).all()
    net = [src for m, src in errs if transient_only(m)]
    n = sum(1 for src in net if fetch_attempts(src) >= MAX_FETCH_ATTEMPTS)  # retries used up: final
    waiting = len(net) - n
    parts = (
        [
            f"{n} row(s) failed on a network error {MAX_FETCH_ATTEMPTS} times and are kept as final 'error' rows"
        ]
        if n
        else []
    )
    if waiting:
        parts.append(f"{waiting} awaiting a retry by the nightly harvest")
    out = [Check("ipo_history", "transient_errors_final", "warn" if n else "ok", n, 0, "; ".join(parts) or None,
                 critical=False)]  # fmt: skip
    last = s.scalar(select(func.max(IpoIssue.last_seen)))
    if last is None:
        out.append(Check("ipo_issue", "registry_fresh", "unknown", note="the issue registry is empty"))
    else:
        age = now - last
        out.append(Check("ipo_issue", "registry_fresh", "fail" if age > REGISTRY_MAX_AGE else "ok",
                         round(age.total_seconds() / 3600, 1), REGISTRY_MAX_AGE.total_seconds() / 3600,
                         f"last refreshed {last:%d %b %H:%M} UTC"))  # fmt: skip
    return out


def close_checks(s: Session, day: date) -> list[Check]:
    """Groww's close vs the official close on `day`: warn only beyond CLOSE_WARN_TOL (see the constant). `matched`
    (₹0.01, groww_market.CLOSE_MATCH_TOL) is counted for the note only; a row with no official close is unknown."""
    C = GrowwCloseCheck
    rel = func.abs(C.groww_close - C.official_close) / func.nullif(C.official_close, 0)
    n, unmatched, beyond, no_official, worst = s.execute(
        select(func.count(), func.count().filter(C.matched.is_(False)), func.count().filter(rel > CLOSE_WARN_TOL),
               func.count().filter(rel.is_(None)), func.max(rel)).where(C.day == day)).one()  # fmt: skip
    if not n:
        return [
            Check(
                "groww_close",
                "groww_vs_official",
                "unknown",
                note=f"no close checks for {day}",
                critical=False,
            )
        ]
    tol = f"{CLOSE_WARN_TOL * 100:.0f} %"
    parts = [f"{beyond}/{n} Groww closes differ from the official close by more than {tol}" if beyond else
             f"{unmatched} of {n} differ by more than ₹0.01 (last trade vs 30-minute average), none beyond {tol}"]  # fmt: skip
    if worst is not None:
        parts.append(f"max {worst * 100:.2f} %")
    if no_official:
        parts.append(f"{no_official} without an official close")
    status = "warn" if beyond else "unknown" if no_official else "ok"
    return [Check("groww_close", "groww_vs_official", status, beyond, float(CLOSE_WARN_TOL), "; ".join(parts),
                  critical=False)]  # fmt: skip


def _free(p: Path) -> int:
    return shutil.disk_usage(p).free


def disk_check(path: Path, free_bytes: Callable[[Path], int] = _free) -> Check:
    try:
        gib = free_bytes(path) / 2**30
    except OSError as e:
        return Check("disk", "free_space", "unknown", note=f"cannot read free space: {type(e).__name__}")
    return Check("disk", "free_space", "fail" if gib < MIN_FREE_GIB else "ok", round(gib, 1), MIN_FREE_GIB,
                 f"{gib:.1f} GiB free on the data volume")  # fmt: skip


def ingest_allowed(path: Path, free_bytes: Callable[[Path], int] = _free) -> bool:
    return disk_check(path, free_bytes).status == "ok"


def _utc(v: str) -> datetime:
    at = datetime.fromisoformat(v)
    if at.tzinfo is None:
        raise ValueError("a time without a zone")
    return at


def _age(hours: float) -> str:
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def backup_check(state_dir: Path, now: datetime) -> Check:
    """The newest good backup's age in hours, from scripts/backup.sh's record of its last run (module docstring)."""
    name, f = ("backup", "last_good_backup"), Path(state_dir) / "backup_status"
    if not f.is_file():
        return Check(*name, "unknown", note="no backup run is recorded yet (scripts/backup.sh writes "
                     "backup_status in the state folder on every run; is its launchd job loaded?)")  # fmt: skip
    try:
        got = dict(ln.split("=", 1) for ln in f.read_text().splitlines() if "=" in ln)
        ran, outcome = _utc(got["at"]), got["outcome"]
        ok_at = _utc(got["last_ok_at"]) if got.get("last_ok_at") else None
    except (OSError, KeyError, ValueError) as e:
        return Check(*name, "unknown", note=f"backup_status could not be read ({type(e).__name__})")
    hours = None if ok_at is None else max(0.0, (now - ok_at).total_seconds() / 3600)
    last = ("no good backup is on record" if ok_at is None or hours is None else
            f"newest good backup {_age(hours)} old ({ok_at.astimezone(UTC):%d-%b-%Y %H:%M} UTC)")  # fmt: skip
    value = None if hours is None else round(hours, 1)
    if outcome != "ok":
        why = BACKUP_OUTCOMES.get(outcome, f"ended with an unknown outcome ({outcome[:40]})")
        fresh = hours is not None and hours <= BACKUP_MAX_AGE_H
        status = "warn" if outcome == "offsite_failed" and fresh else "fail"
        return Check(*name, status, value, BACKUP_MAX_AGE_H,
                     f"the last run ({ran.astimezone(UTC):%d-%b-%Y %H:%M} UTC) {why}; {last}")  # fmt: skip
    if hours is None:
        return Check(*name, "unknown", note="the last run reports ok but names no dump")
    if hours > BACKUP_MAX_AGE_H:
        return Check(*name, "fail", value, BACKUP_MAX_AGE_H,
                     f"no backup has run since: {last}; is the launchd job loaded?")  # fmt: skip
    return Check(*name, "ok", value, BACKUP_MAX_AGE_H, last)


def _fresh_session(sql: str) -> None:
    """Run `sql` on a new connection that is closed afterwards (never a pooled one, see the module docstring)."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    from finresearch.db import get_engine

    eng = create_engine(get_engine().url, poolclass=NullPool)
    try:
        with eng.connect() as c:
            c.execute(text(sql))
    finally:
        eng.dispose()


def vector_check(sql: str | None = None) -> Check:
    """Can a new session load pgvector? fail with Postgres's own message when its library is missing (SQLSTATE
    58P01, `could not access file "$libdir/vector"`: Homebrew's pgvector no longer builds for Postgres 17) or the
    extension is not created in this database; unknown when the probe failed for another reason."""
    name = ("pgvector", "extension_loadable")
    try:
        _fresh_session(sql or VECTOR_PROBE_SQL)
    except Exception as e:
        orig = getattr(e, "orig", None) or e
        msg, code = str(orig).strip().splitlines()[0][:120], getattr(orig, "sqlstate", None)
        if code == "58P01" or "could not access file" in msg:
            return Check(*name, "fail", note=f"{msg}: document search and chunk embeddings fail until pgvector is "
                         "rebuilt for this Postgres (docs/dev/ENGINEERING_HANDOFF.md, pgvector on Postgres 17)")  # fmt: skip
        if code == "42704":
            return Check(*name, "fail", note=f"{msg}: the vector extension is not installed in this database "
                         "(CREATE EXTENSION vector)")  # fmt: skip
        return Check(*name, "unknown", note=f"could not be checked: {type(e).__name__}: {msg}")
    return Check(*name, "ok")


def vector_loadable() -> bool | None:
    """For GET /api/health: True, False (the library or the extension is missing) or None (could not be checked)."""
    return {"ok": True, "fail": False}.get(vector_check().status)
