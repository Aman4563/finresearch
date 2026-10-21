"""Store and read NSE's participant-wise F&O open interest (#317, adapters.nse_participant_oi).

`update` fetches the weekdays of the last few days that are not stored yet (normally one file a night, from the
nightly F&O job), one GET each through the shared NSE budget (adapters.http), and stops after two refusals in a row.
A day is written in one transaction (all four participants or nothing). `latest` returns the newest stored day with
the day-over-day change against the stored day before it.

What the numbers are: contracts open at the day's end by participant category, as NSE classifies them. They describe
positioning; no study verified for this app shows that they predict index moves (see signals.fno_context EVIDENCE).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from finresearch.adapters import nse_participant_oi as po
from finresearch.db.models import FnoParticipantOi

log = logging.getLogger(__name__)
LOOKBACK_DAYS = 5
MAX_FAILURES = 2


def save_day(s: Session, day: date, parsed: po.ParticipantDay, url: str) -> int:
    if parsed.day is not None and parsed.day != day:
        raise po.ParticipantFileError(f"the file for {day} says it is as on {parsed.day}")
    s.execute(delete(FnoParticipantOi).where(FnoParticipantOi.day == day))
    for who, counts in parsed.rows.items():
        s.add(FnoParticipantOi(day=day, client_type=who, source=url, **counts))
    s.flush()
    return len(parsed.rows)


def stored_days(s: Session, start: date, end: date) -> set[date]:
    q = select(FnoParticipantOi.day).where(FnoParticipantOi.day.between(start, end)).distinct()
    return set(s.scalars(q))


def bhav_sessions(s: Session, start: date, end: date) -> dict[date, str]:
    """{day: status} from the bhavcopy manifest (fno_bhav_day) between start and end."""
    from finresearch.db.models import FnoBhavDay

    q = select(FnoBhavDay.day, FnoBhavDay.status).where(FnoBhavDay.day.between(start, end))
    return {d: st for d, st in s.execute(q)}


async def update(today: date, *, http: Any = None, lookback: int = LOOKBACK_DAYS) -> dict[str, Any]:
    """Fetch the missing weekdays in [today − lookback, today]; returns counts and errors by day."""
    from finresearch.adapters.http import PoliteClient
    from finresearch.db import session_scope

    start = today - timedelta(days=lookback)
    with session_scope() as s:
        have = stored_days(s, start, today)
        manifest = bhav_sessions(s, start, today)
    # the bhavcopy manifest's sessions decide (#354 i): a day it loaded is fetched even on a weekend (a special session),
    # a day it found no file for is a holiday and skipped; a day it has not tried yet stays a weekday guess
    days = [start + timedelta(days=i) for i in range(lookback + 1)]
    days = [
        d for d in days if manifest.get(d) == "loaded" or (d.weekday() < 5 and manifest.get(d) != "no_file")
    ]
    days = [d for d in days if d not in have]
    out: dict[str, Any] = {"planned": len(days), "loaded": 0, "no_file": 0, "failed": {}}
    own = http is None
    http = http or PoliteClient()
    streak = 0
    try:
        for d in days:
            try:
                got = await po.fetch_day(http, d)
                if got is None:
                    out["no_file"] += 1
                    continue
                parsed = po.parse(got[1])
                with session_scope() as s:
                    save_day(s, d, parsed, got[0])
                out["loaded"] += 1
                streak = 0
            except Exception as e:  # a refusal or a bad file: reported, retried on the next run
                out["failed"][d.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                streak += 1
                if streak >= MAX_FAILURES:
                    out["stopped"] = f"{streak} failures in a row: stopped to stay polite"
                    break
    finally:
        if own:
            await http.aclose()
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
