"""MWPL utilisation history and the derived F&O ban history (P1.12, #374; PREREG.md §3): `finresearch fno panel
mwpl-backfill` and `ban-check`. Behind the registered study (prereg.require_registered) and the owner's data-rights
decision for each new dataset (data_rights.PENDING until it is recorded: nothing is requested before).

Politeness (owner decision #362): one combineoi zip per session, through the shared NSE budget (adapters.http), with
`pause` seconds between downloads; no download between 09:00 and 16:00 IST on a weekday (kept files are still read);
the run stops after REFUSALS_TO_STOP refusals (HTTP 403/429) in a row or MAX_CONSECUTIVE_FAILURES failures of any
kind (a block page is a failure, never "no file"). Sessions are the F&O bhavcopy manifest's loaded days, so no
holiday or weekend is probed. Resume: optpanel_file_day records every (dataset, day) tried; loaded days are skipped
and a 404 day is re-tried for RECHECK_DAYS. Raw zips are kept under data/mwpl/<year>/ (gitignored, a few KB a day) so
a parser change re-reads them. Each file loaded gets a lineage row (dq.lineage).

The ban history is derived on read (optpanel.ban.states), never stored: per stock in session order, the state for the
session after D from u_D, unknown after a session whose file is missing or that omits the stock.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.adapters import nse_mwpl as mw
from finresearch.db.models import FnoBanNotice, FnoBhavDay, MwplOi, OptPanelFileDay
from finresearch.optpanel import ban, prereg
from finresearch.refseries.index_eod import (
    MAX_CONSECUTIVE_FAILURES,
    REFUSALS_TO_STOP,
    in_market_hours,
    refused,
)

log = logging.getLogger(__name__)
DATASET_OI, DATASET_BAN = "nse_combineoi", "nse_secban"
RECHECK_DAYS = 3
MARKET_HOURS_STOP = "market hours (09:00-16:00 IST): no NSE downloads; re-run after the close"


def raw_dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir).parent / "mwpl"


def raw_path(day: date) -> Path:
    return raw_dir() / str(day.year) / mw.combineoi_url(day).rsplit("/", 1)[1]


def _manifest(s: Session, dataset: str, day: date, **values: Any) -> None:
    stmt = pg_insert(OptPanelFileDay).values(dataset=dataset, day=day, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["dataset", "day"], set_={
        **values, "attempts": OptPanelFileDay.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def sessions(s: Session, start: date, end: date) -> list[date]:
    return list(s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.day.between(start, end),
                                                        FnoBhavDay.status == "loaded").order_by(FnoBhavDay.day)))  # fmt: skip


def _done(s: Session, dataset: str, start: date, end: date) -> dict[date, str]:
    return dict(s.execute(select(OptPanelFileDay.day, OptPanelFileDay.status).where(
        OptPanelFileDay.dataset == dataset, OptPanelFileDay.day.between(start, end))).all())  # fmt: skip


def plan_days(s: Session, start: date, end: date, today: date) -> list[date]:
    done, stale = _done(s, DATASET_OI, start, end), today - timedelta(days=RECHECK_DAYS)
    return [d for d in sessions(s, start, end)
            if not (done.get(d) == "loaded" or (done.get(d) == "no_file" and d < stale))]  # fmt: skip


def load_day(s: Session, day: date, url: str, content: bytes) -> int:
    """Replace `day`'s rows from the zip (in the caller's transaction) and record its lineage."""
    from finresearch.dq.lineage import record_load

    got = mw.parse_combineoi(mw.unzip(content), day)
    s.execute(delete(MwplOi).where(MwplOi.day == day))
    s.execute(insert(MwplOi), [{"day": day, "symbol": r.symbol, "isin": r.isin, "mwpl": r.mwpl, "oi": r.oi,
                                "limit_next": r.limit_next, "utilisation": r.utilisation, "oi_basis": got.oi_basis}
                               for r in {r.symbol: r for r in got.rows}.values()])  # fmt: skip
    _manifest(s, DATASET_OI, day, status="loaded", url=url, rows=len(got.rows), error=None)
    record_load(s, DATASET_OI, day, content, url=url, parser_version=mw.PARSER_VERSION, rows=len(got.rows))
    return len(got.rows)


async def backfill(start: date, end: date | None = None, *, today: date | None = None, http: Any = None,
                   pause: float = 0.0, keep_raw: bool = True, now: Callable[[], datetime] | None = None,
                   progress: Callable[[int, int, date, str], None] | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch and store every planned session's combineoi file in [start, end] (module docstring)."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.db import session_scope
    from finresearch.fincalc.dates import today_ist

    prereg.require_registered()
    data_rights.require(DATASET_OI)
    now = now or now_ist
    today = today or today_ist()
    end = min(end or today, today)
    with session_scope() as s:
        days = plan_days(s, start, end, today)
    out: dict[str, Any] = {
        "planned": len(days),
        "loaded": 0,
        "no_file": 0,
        "failed": {},
        "rows": 0,
        "from_raw": 0,
    }
    own, streak, refusals = http is None, 0, 0
    try:
        for i, day in enumerate(days, 1):
            p = raw_path(day)
            if not p.is_file() and in_market_hours(now()):
                out["stopped"] = MARKET_HOURS_STOP
                break
            status = "loaded"
            try:
                if p.is_file():
                    got: tuple[str, bytes] | None = (mw.combineoi_url(day), p.read_bytes())
                    out["from_raw"] += 1
                else:
                    http = http or PoliteClient()
                    got = await mw.fetch_combineoi(http, day)
                    if pause:
                        await asyncio.sleep(pause)
                    if got is not None and keep_raw:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(got[1])
                with session_scope() as s:
                    if got is None:
                        status = "no_file"
                        _manifest(s, DATASET_OI, day, status="no_file", error=None)
                    else:
                        out["rows"] += load_day(s, day, *got)
                out[status] += 1
                streak = refusals = 0
            except Exception as e:  # a refusal, a block page, a bad file: recorded, retried on the next run
                status = "failed"
                out["failed"][day.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                with session_scope() as s:
                    _manifest(s, DATASET_OI, day, status="failed", error=out["failed"][day.isoformat()])
                streak += 1
                refusals = refusals + 1 if refused(e) else 0
                if refusals >= REFUSALS_TO_STOP:
                    out["stopped"] = (f"NSE refused {refusals} requests in a row (403/429): stopped; pause this "
                                      "dataset and check before re-running (#362)")  # fmt: skip
                    break
                if streak >= MAX_CONSECUTIVE_FAILURES:
                    out["stopped"] = (
                        f"{streak} consecutive failures: stopped to stay polite; re-run to resume"
                    )
                    break
            if progress:
                progress(i, len(days), day, status)
    finally:
        if own and http is not None:
            await http.aclose()
    return out


def ban_history(s: Session, start: date, end: date) -> dict[date, dict[str, bool | None]]:
    """{trade day: {symbol: banned / not / None = unknown}} for the sessions after each session in [start, end]
    whose successor is in the calendar, for every symbol seen in a combineoi file in the range."""
    days = sessions(s, start, end + timedelta(days=10))
    inside = [d for d in days if d <= end]
    loaded = {d for d, st in _done(s, DATASET_OI, start, end).items() if st == "loaded"}
    us: dict[str, dict[date, float | None]] = {}
    for d, sym, u in s.execute(select(MwplOi.day, MwplOi.symbol, MwplOi.utilisation)
                               .where(MwplOi.day.between(start, end))):  # fmt: skip
        us.setdefault(sym, {})[d] = None if u is None else float(u)
    nxt = dict(zip(days, days[1:], strict=False))
    out: dict[date, dict[str, bool | None]] = {}
    for sym, by_day in us.items():
        series = [(d, by_day.get(d) if d in loaded else None) for d in inside]
        for d, state in ban.states(series):
            if d in nxt:
                out.setdefault(nxt[d], {})[sym] = state
    return out


async def check_notices(n: int = 20, *, seed: int = 0, start: date | None = None, end: date | None = None,
                        http: Any = None, now: Callable[[], datetime] | None = None) -> dict[str, Any]:  # fmt: skip
    """#374's acceptance check: the derived ban list against NSE's dated notice on `n` trade days drawn reproducibly
    for `seed` among the days whose deciding file is stored. One notice file per day; stores each notice with its
    lineage. Never during market hours."""
    from finresearch import data_rights
    from finresearch.adapters.http import PoliteClient, now_ist
    from finresearch.db import session_scope
    from finresearch.dq.lineage import record_load
    from finresearch.fincalc.dates import today_ist

    prereg.require_registered()
    data_rights.require(DATASET_BAN)
    now = now or now_ist
    with session_scope() as s:
        lo = start or s.scalar(select(OptPanelFileDay.day).where(OptPanelFileDay.dataset == DATASET_OI)
                               .order_by(OptPanelFileDay.day).limit(1))  # fmt: skip
        if lo is None:
            return {"sampled": 0, "matched": 0, "days": [], "stopped": "no combineoi file is stored yet"}
        hist = ban_history(s, lo, end or today_ist())
    pool = sorted(d for d, states in hist.items() if any(v is not None for v in states.values()))
    picked = sorted(random.Random(seed).sample(pool, min(n, len(pool))))
    out: dict[str, Any] = {"sampled": 0, "matched": 0, "days": []}
    own = http is None
    refusals = 0
    try:
        for d in picked:
            if in_market_hours(now()):
                out["stopped"] = MARKET_HOURS_STOP
                break
            http = http or PoliteClient()
            try:
                got = await mw.fetch_ban_notice(http, d)
            except Exception as e:
                out["days"].append({"trade_day": d.isoformat(), "error": f"{type(e).__name__}: {e}"[:300]})
                refusals = refusals + 1 if refused(e) else 0
                if refusals >= REFUSALS_TO_STOP:
                    out["stopped"] = "NSE refused 2 requests in a row (403/429): stopped (#362)"
                    break
                continue
            refusals = 0
            with session_scope() as s:
                if got is None:
                    _manifest(s, DATASET_BAN, d, status="no_file", error=None)
                    out["days"].append({"trade_day": d.isoformat(), "error": "no notice (404)"})
                    continue
                url, body, notice = got
                s.execute(delete(FnoBanNotice).where(FnoBanNotice.trade_day == d))
                for sym in notice.symbols:
                    s.add(FnoBanNotice(trade_day=d, symbol=sym))
                _manifest(s, DATASET_BAN, d, status="loaded", url=url, rows=len(notice.symbols), error=None)
                record_load(s, DATASET_BAN, d, body, url=url, parser_version=mw.PARSER_VERSION,
                            rows=len(notice.symbols))  # fmt: skip
            res = ban.compare(hist[d], notice.symbols)
            out["sampled"] += 1
            out["matched"] += bool(res["match"])
            out["days"].append({"trade_day": d.isoformat(), **res})
    finally:
        if own and http is not None:
            await http.aclose()
    return out
