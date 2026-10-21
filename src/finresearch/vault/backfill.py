"""Load the sealed 2008-2021 vault (#365, P1.3): NSE's legacy F&O bhavcopy for NIFTY and BANKNIFTY index futures and
options (FINNIFTY from its first contracts in 2021), into the separate vault database only (PREREG.md §1).

What it refuses (VaultLoadRefused, before any request): no vault database configured; a vault URL naming the app's
database; any day outside 1-Jan-2008 to 31-Dec-2021 (later days belong to the app database, `fnohist.store.backfill`);
no recorded data-rights decision for the F&O bhavcopy (data_rights, #362).

What it keeps: IDF/IDO rows of VAULT_SYMBOLS only (no stock futures or options, no other index: MINIFTY, CNXIT, ... are
out of the vault's definition). Dead contracts (no OI, no trade) are counted, not stored, as in the app's loader, so
the manifest's rows_kept + rows_dead + rows outside the vault's symbols = rows_total (the audit's check 3 compares
the kept and dead counts with the file).

Politeness (owner decision #362): one GET per day through the shared NSE budget (adapters.http.PoliteClient) plus
`pause` seconds (default 2.5 s, the issue's "one request every 2-3 s"); no download between 09:00 and 16:00 IST on a
weekday; stop after REFUSALS_TO_STOP refusals (HTTP 403/429) in a row or MAX_CONSECUTIVE_FAILURES failures of any kind;
`max_days` caps one run so the ~3,700 files spread over nights. Weekends are not requested (about 1,460 requests
saved) except WEEKEND_PROBES, the weekend sessions we know of (a probe that answers 404 is recorded no_file, so the
list may err on the side of extra dates); `probe_weekends=True` requests every weekend day instead. Weekday holidays
are skipped only when NSE's list for that year is cached (adapters.nse_holidays); otherwise the weekday is requested
and a holiday answers 404 (~3 KB).

Resume: the vault's fno_bhav_day manifest. Loaded and no_file days are never requested again (vault days are years
old: NSE will not publish them later); failed days are retried by the next run. Raw zips are not kept by default (the
issue leaves raw retention out); with keep_raw they go to data/vault/fno_bhav/<year>/, apart from the app's raw files.

Derived rows (fno_iv_eod per loaded day, fno_contract at the end) are computed by the app's own code
(fnohist.derive) inside the vault database. The legacy file has no underlying price and no lot size: the derivation
uses the futures (the front future serves as spot before index closes exist, #365 T12) and lots come from the dated
circular table (backtest.lots), never from the file.

Output is counts and statuses only: no price, IV or P&L from the vault is printed or returned (PREREG.md §2).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.db.models import FnoBhavDay
from finresearch.vault.registry import VAULT_END, VAULT_START

log = logging.getLogger(__name__)
DATASET = "fno_bhav"
VAULT_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY")
DEFAULT_PAUSE = 2.5
MAX_CONSECUTIVE_FAILURES = 5
REFUSALS_TO_STOP = 2
# Weekend sessions to request (all others are skipped). From memory, to be confirmed by the probe itself: a date that
# was not a session answers 404 and is recorded no_file. Budget-day Saturdays 28-Feb-2015 and 1-Feb-2020; Diwali
# Muhurat sessions that fell on a weekend (17-Oct-2009, 3-Nov-2013, 30-Oct-2016, 27-Oct-2019, 14-Nov-2020).
WEEKEND_PROBES = frozenset({date(2009, 10, 17), date(2013, 11, 3), date(2015, 2, 28), date(2016, 10, 30),
                            date(2019, 10, 27), date(2020, 2, 1), date(2020, 11, 14)})  # fmt: skip


class VaultLoadRefused(PermissionError):
    """The vault load was refused before any request (see the module docstring)."""


def keep(kind: str, sym: str) -> bool:
    """The vault's rows: index futures and options of VAULT_SYMBOLS only."""
    return kind in ("IDF", "IDO") and sym in VAULT_SYMBOLS


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "vault" / "fno_bhav"


def _check(start: date, end: date, vault_url: str | None, app_url: str | None) -> str:
    if not vault_url:
        raise VaultLoadRefused("no vault database is configured (FINRESEARCH_VAULT_DATABASE_URL)")
    if app_url and make_url(vault_url).database == make_url(app_url).database:
        raise VaultLoadRefused("the vault URL names the app's database: the vault lives in its own (#364 §1)")
    if end < start:
        raise VaultLoadRefused(f"the end {end} is before the start {start}: nothing to load after it")
    if start < VAULT_START or end > VAULT_END:
        raise VaultLoadRefused(f"{start} to {end} is not inside the vault period {VAULT_START} to {VAULT_END}; "
                               "later days belong to the app database (finresearch fno backfill)")  # fmt: skip
    return vault_url


def plan_days(s: Session, start: date, end: date, holidays: set[date] | None = None,
              extra_days: Iterable[date] = WEEKEND_PROBES, probe_weekends: bool = False) -> list[date]:  # fmt: skip
    """The days still to request, oldest first (module docstring)."""
    from finresearch.adapters.nse_holidays import known_trading_holidays

    extra = set(extra_days)
    done = {r.day: r.status for r in s.scalars(select(FnoBhavDay).where(FnoBhavDay.day.between(start, end)))}
    years: dict[int, set[date] | None] = {}
    out, d = [], start
    while d <= end:
        if holidays is None and d.year not in years:
            years[d.year] = known_trading_holidays(d)
        hol = holidays if holidays is not None else years[d.year]
        done_already = done.get(d) in ("loaded", "no_file")
        weekend_skip = d.weekday() >= 5 and not probe_weekends and d not in extra
        holiday = d.weekday() < 5 and bool(hol) and d in hol  # type: ignore[operator]
        if not (done_already or weekend_skip or holiday):
            out.append(d)
        d += timedelta(days=1)
    return out


async def backfill(start: date, end: date, *, vault_url: str | None, app_url: str | None = None, http: Any = None,
                   holidays: set[date] | None = None, pause: float = DEFAULT_PAUSE, keep_raw: bool = False,
                   derive: bool = True, max_days: int | None = None, extra_days: Iterable[date] = WEEKEND_PROBES,
                   probe_weekends: bool = False, now: Callable[[], datetime] | None = None,
                   progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store every missing vault day in [start, end] into `vault_url` (module docstring). `app_url` is the
    app's database (default: the configured one), which the vault URL must not name. Returns counts by outcome, the
    failed days with their errors, and `stopped` when the run stopped early."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.config import get_settings
    from finresearch.db import session_scope
    from finresearch.fnohist import derive as dv
    from finresearch.fnohist import store
    from finresearch.refseries.index_eod import in_market_hours, refused

    url = _check(start, end, vault_url, app_url if app_url is not None else get_settings().database_url)
    data_rights.require(DATASET)
    now = now or now_ist
    with session_scope(url) as s:
        days = plan_days(s, start, end, holidays, extra_days, probe_weekends)
    left = len(days)
    if max_days is not None:
        days = days[:max_days]
    out: dict[str, Any] = {"planned": len(days), "remaining_after": left - len(days), "loaded": 0, "no_file": 0,
                           "failed": {}, "rows": 0, "from_raw": 0}  # fmt: skip
    own = http is None
    http = http or PoliteClient()
    streak = refusals = 0
    try:
        for i, day in enumerate(days, 1):
            raw = _raw(day)
            if raw is None and in_market_hours(now()):
                out["stopped"] = "market hours (09:00-16:00 IST): no backfill downloads; re-run after the close"
                break
            status = "loaded"
            try:
                got = raw
                if got is not None:
                    out["from_raw"] += 1
                else:
                    got = await fb.fetch_day(http, day)
                    if pause:
                        await asyncio.sleep(pause)
                    if got is not None and keep_raw:
                        p = raw_dir() / str(day.year) / got[1].rsplit("/", 1)[1]
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(got[2])
                with session_scope(url) as s:
                    if got is None:
                        store.mark(s, day, "no_file")
                        status = "no_file"
                    else:
                        out["rows"] += store.load_day(s, day, *got, [], keep=keep)["kept"]
                if got is not None and derive:  # its own transaction: a derivation error never un-loads the day
                    try:
                        with session_scope(url) as s:
                            dv.derive_day(s, day)
                    except Exception as e:
                        log.warning("vault IV derivation failed for %s", day, exc_info=True)
                        out.setdefault("derive_failed", {})[day.isoformat()] = f"{type(e).__name__}"[:300]
                out[status] += 1
                streak = refusals = 0
            except Exception as e:  # a refusal, a bad file, a transport error: recorded, retried next run
                status = "failed"
                out["failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                with session_scope(url) as s:
                    store.mark(s, day, "failed", out["failed"][day.isoformat()])
                streak += 1
                refusals = refusals + 1 if refused(e) else 0
                if refusals >= REFUSALS_TO_STOP:
                    out["stopped"] = (f"NSE refused {refusals} requests in a row (403/429): stopped; pause this "
                                      "dataset and check before re-running (#362)")  # fmt: skip
                    break
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    out["stopped"] = f"{streak} consecutive failures: stopped to stay polite; re-run to resume"
                    break
            if progress:
                progress(i, len(days), day, status)
    finally:
        if own:
            await http.aclose()
    if derive and out["loaded"]:
        with session_scope(url) as s:
            out["contracts"] = dv.rebuild_contracts(s, since=start - timedelta(days=120))
    return out


def _raw(day: date) -> tuple[fb.Format, str, bytes] | None:
    """A zip kept by an earlier run with keep_raw (legacy route only: every vault day predates UDiFF)."""
    url = fb.url_for(day, "legacy")
    p = raw_dir() / str(day.year) / url.rsplit("/", 1)[1]
    return ("legacy", url, p.read_bytes()) if p.is_file() else None


def status(vault_url: str) -> dict[str, Any]:
    """Manifest counts by year and status (no row of market data is read)."""
    from sqlalchemy import extract, func

    from finresearch.db import session_scope

    with session_scope(vault_url) as s:
        y = extract("year", FnoBhavDay.day)
        rows = s.execute(select(y, FnoBhavDay.status, func.count()).group_by(y, FnoBhavDay.status)).all()
    out: dict[str, Any] = {}
    for yr, st, n in sorted(rows, key=lambda r: (r[0], r[1])):
        out.setdefault(str(int(yr)), {})[st] = n
    return out
