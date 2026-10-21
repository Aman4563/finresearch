"""Store NSE's F&O bhavcopy day by day (#306): `finresearch fno backfill` and the nightly job (monitor.fno_bhav).

What is kept: every future (index and stock, ~650 rows a day), the options of INDEX_OPTIONS, and the options of the
stocks the user holds or watches (disclosures.store.tracked, NSE only). A contract with no open interest and no trade
that day is dropped (counted in the manifest's rows_dead): its settlement price is NSE's theoretical value and no
position could have been opened or held in it, so a backtest never needs it. That is about 40 % of the kept
underlyings' rows on 30-Sep-2026 (NIFTY options: 1,774 listed, most far strikes untouched).

Why Postgres and not Parquet: the data joins the app's tables (signals, the forecast ledger, the backtester) and needs
the same migration, backup and test-database flow; the project has no Parquet engine (pyarrow / DuckDB are not
dependencies). Volume is a few thousand rows a day: about 5-8 million rows for 2022-2026, ~1-1.5 GB with the
(symbol, day) index, which Postgres scans per underlying in milliseconds. A day is loaded in one transaction (delete
the day, then bulk insert), so re-running any day is idempotent and a crash never leaves half a day.

Resume: `fno_bhav_day` records every calendar day tried. Loaded days are skipped (re-loaded when a newly tracked stock
is missing from them), and a 404 day (status no_file) is skipped once it is older than RECHECK_DAYS (NSE re-uploads a
late file within a day or two; 4-Jan-2024's file is stamped the next afternoon). Weekends are probed, not assumed
closed: NSE held F&O sessions on Saturday 20-Jan-2024, Saturday 2-Mar-2024, Saturday 1-Feb-2025 and Sunday
1-Feb-2026 (their UDiFF files answered 200 on 12-Oct-2026). Weekday NSE trading holidays are skipped without a request
when that year's holiday list is cached (adapters.nse_holidays); other years are probed (a 404 costs ~3 KB).

Raw zips are kept under data/fno_bhav/<year>/ (gitignored, ~1.5 MB a day) unless --no-keep-raw: adding a stock later
re-reads them locally instead of downloading every day again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_fno_bhav as fb
from finresearch.db.models import FnoBhav, FnoBhavDay

log = logging.getLogger(__name__)
INDEX_OPTIONS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
RECHECK_DAYS = 3
CHUNK = 2000
MAX_CONSECUTIVE_FAILURES = 5  # NSE refusing: stop the run rather than hammer it
COLUMNS = ("day", "symbol", "instrument", "expiry", "strike", "open", "high", "low", "close", "settle", "oi",
           "chg_oi", "contracts", "turnover", "underlying", "lot_size")  # fmt: skip


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "fno_bhav"


def raw_path(day: date, url: str) -> Path:
    return raw_dir() / str(day.year) / url.rsplit("/", 1)[1]


def read_raw(day: date) -> tuple[fb.Format, str, bytes] | None:
    for fmt in fb.formats_for(day):
        url = fb.url_for(day, fmt)
        p = raw_path(day, url)
        if p.is_file():
            return fmt, url, p.read_bytes()
    return None


def tracked_stocks() -> list[str]:
    """NSE stocks held or watched (largest holding first); BSE-only stocks have no options."""
    from finresearch.db import session_scope
    from finresearch.disclosures.store import tracked

    with session_scope() as s:
        return [k.upper() for k in tracked(s).stock_keys(nse_only=True)]


def keeper(stocks: Iterable[str]) -> Callable[[str, str], bool]:
    wanted = {x.upper() for x in stocks}

    def keep(kind: str, sym: str) -> bool:
        return (
            kind in ("IDF", "STF")
            or (kind == "IDO" and sym in INDEX_OPTIONS)
            or (kind == "STO" and sym in wanted)
        )

    return keep


def _manifest(s: Session, day: date, **values: Any) -> None:
    stmt = pg_insert(FnoBhavDay).values(day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["day"], set_={
        **values, "attempts": FnoBhavDay.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def load_day(
    s: Session, day: date, fmt: fb.Format, url: str, content: bytes, stocks: Iterable[str]
) -> dict[str, int]:
    """Replace `day`'s rows with the file's (one transaction with the caller's session). Raises NseError for a file
    that is not a bhavcopy or is dated another day."""
    stocks = sorted({x.upper() for x in stocks})
    rows, total = fb.parse(content, fmt, keeper(stocks))
    if any(r.day != day for r in rows):
        raise fb.NseError(f"the F&O bhavcopy for {day} carries another trade date")
    live = [r for r in rows if (r.oi or 0) > 0 or (r.contracts or 0) > 0]
    s.execute(delete(FnoBhav).where(FnoBhav.day == day))
    for i in range(0, len(live), CHUNK):
        s.execute(
            insert(FnoBhav),
            [{**{c: getattr(r, c) for c in COLUMNS}, "option_type": r.right} for r in live[i : i + CHUNK]],
        )
    _manifest(s, day, status="loaded", format=fmt, url=url, rows_total=total, rows_kept=len(live),
              rows_dead=len(rows) - len(live), stock_symbols=stocks, error=None)  # fmt: skip
    return {"total": total, "kept": len(live), "dead": len(rows) - len(live)}


def mark(s: Session, day: date, status: str, error: str | None = None) -> None:
    _manifest(s, day, status=status, error=None if error is None else error[:300])


def plan_days(s: Session, start: date, end: date, today: date, stocks: Iterable[str],
              holidays: set[date] | None = None) -> list[date]:  # fmt: skip
    """The days a run still has to fetch or re-load, oldest first (see the module docstring)."""
    from finresearch.adapters.nse_holidays import known_trading_holidays

    wanted = {x.upper() for x in stocks}
    done = {r.day: r for r in s.scalars(select(FnoBhavDay).where(FnoBhavDay.day.between(start, end)))}
    years: dict[int, set[date] | None] = {}
    out, d = [], start
    while d <= min(end, today):
        m = done.get(d)
        if holidays is None and d.year not in years:
            years[d.year] = known_trading_holidays(d)
        hol = holidays if holidays is not None else years[d.year]
        if (m is not None and m.status == "loaded" and wanted <= set(m.stock_symbols or [])) or (
            m is not None and m.status == "no_file" and d < today - timedelta(days=RECHECK_DAYS)
        ):
            pass
        elif m is None and d.weekday() < 5 and hol and d in hol:
            pass  # a known weekday trading holiday: no request
        else:
            out.append(d)
        d += timedelta(days=1)
    return out


async def backfill(start: date, end: date | None = None, *, today: date | None = None, stocks: Iterable[str] | None = None,
                   http: Any = None, holidays: set[date] | None = None, pause: float = 0.0, keep_raw: bool = True,
                   derive: bool = True, progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store every missing day in [start, end]. Resumable and idempotent (see the module docstring).
    Network goes through one PoliteClient (the shared NSE budget, adapters.http); `pause` adds seconds between
    downloads. Returns counts by outcome and the failed days with their errors.
    Refuses (VaultSealed) any range reaching into the sealed 2008-2021 vault (#364): those days are loaded only into
    the separate vault database by P1.3 (#365), never into this one."""
    from finresearch.vault.registry import VAULT_END, VaultSealed

    if start <= VAULT_END:
        raise VaultSealed(
            f"{start:%d-%b-%Y} is inside the sealed 2008-2021 vault: not loading it here (#364, #365)"
        )
    from finresearch.adapters.http import PoliteClient
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist
    from finresearch.fnohist import derive as dv

    today = today or today_ist()
    end = min(end or today, today)
    stocks = sorted({x.upper() for x in (tracked_stocks() if stocks is None else stocks)})
    with session_scope() as s:
        days = plan_days(s, start, end, today, stocks, holidays)
    out: dict[str, Any] = {"planned": len(days), "loaded": 0, "no_file": 0, "failed": {}, "rows": 0,
                           "from_raw": 0, "stocks": stocks}  # fmt: skip
    own = http is None
    http = http or PoliteClient()
    streak = 0
    try:
        for i, day in enumerate(days, 1):
            status = "loaded"
            try:
                got = read_raw(day)
                if got is not None:
                    out["from_raw"] += 1
                else:
                    got = await fb.fetch_day(http, day)
                    if pause:
                        await asyncio.sleep(pause)
                    if got is not None and keep_raw:
                        p = raw_path(day, got[1])
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(got[2])
                with session_scope() as s:
                    if got is None:
                        mark(s, day, "no_file")
                        status = "no_file"
                    else:
                        out["rows"] += load_day(s, day, *got, stocks)["kept"]
                # the derived rows in their own transaction: a derivation error never un-loads the day
                if got is not None and derive:
                    try:
                        with session_scope() as s:
                            dv.derive_day(s, day)
                    except Exception as e:
                        log.warning("F&O IV derivation failed for %s", day, exc_info=True)
                        msg = f"derive: {type(e).__name__}: {e}"[:300]
                        out.setdefault("derive_failed", {})[day.isoformat()] = msg
                        with session_scope() as s:
                            s.execute(update(FnoBhavDay).where(FnoBhavDay.day == day).values(error=msg))
                out[status] += 1
                streak = 0
            except (
                Exception
            ) as e:  # a refusal, a bad file or a transport error: recorded, retried on the next run
                status = "failed"
                out["failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                with session_scope() as s:
                    mark(s, day, "failed", out["failed"][day.isoformat()])
                streak += 1
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    out["stopped"] = (
                        f"{streak} consecutive failures: stopped to stay polite; re-run to resume"
                    )
                    break
            if progress:
                progress(i, len(days), day, status)
    finally:
        if own:
            await http.aclose()
    if derive and out["loaded"]:
        with session_scope() as s:
            out["contracts"] = dv.rebuild_contracts(s, since=start - timedelta(days=120))
    return out
