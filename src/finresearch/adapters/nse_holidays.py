"""NSE exchange holidays (capital-market segment), from NSE's holiday master.

* trading holidays: no bidding or trading (e.g. Gandhi Jayanti, 2-Oct-2026);
* clearing (settlement) holidays: no settlement, although trading may be open (e.g. Annual Bank Closing).

NSE publishes the current year only, so each year's lists are cached in the state directory and read synchronously
by the date calculations. `refresh_holidays` fetches the current year when its cache is missing or older than a week.
The official 2026 list is NSE circular NSE/CMTR/71775 (12-Dec-2025), amended for 15-Jan-2026.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from finresearch.adapters.nse import NseClient

Kind = Literal["trading", "clearing"]
WARMUP = "https://www.nseindia.com/resources/exchange-communication-holidays"
MAX_AGE_S = 7 * 86400
log = logging.getLogger(__name__)


def parse_holidays(data: dict[str, Any], segment: str = "CM") -> dict[date, str]:
    out: dict[date, str] = {}
    for row in data.get(segment) or []:
        try:
            out[datetime.strptime(row["tradingDate"], "%d-%b-%Y").date()] = row.get("description") or ""
        except (KeyError, ValueError):
            continue
    return out


def _dir(state_dir: Path | None) -> Path:
    if state_dir is None:
        from finresearch.config import get_settings

        state_dir = get_settings().state_dir
    return Path(state_dir) / "holidays"


def _cache(state_dir: Path | None, kind: Kind, year: int) -> Path:
    return _dir(state_dir) / f"nse_cm_{kind}_{year}.json"


def save_holidays(holidays: dict[date, str], kind: Kind, state_dir: Path | None = None) -> list[Path]:
    by_year: dict[int, dict[str, str]] = {}
    for d, desc in holidays.items():
        by_year.setdefault(d.year, {})[d.isoformat()] = desc
    paths = []
    for year, rows in by_year.items():
        p = _cache(state_dir, kind, year)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(sorted(rows.items())), indent=1))
        tmp.replace(p)
        paths.append(p)
    return paths


def load_holidays(kind: Kind = "trading", state_dir: Path | None = None) -> dict[date, str]:
    """Every cached year of one list (empty if nothing is cached yet)."""
    out: dict[date, str] = {}
    folder = _dir(state_dir)
    if not folder.exists():
        return out
    for p in sorted(folder.glob(f"nse_cm_{kind}_*.json")):
        try:
            out.update({date.fromisoformat(k): v for k, v in json.loads(p.read_text()).items()})
        except (ValueError, OSError):
            log.warning("unreadable holiday cache %s", p)
    return out


def trading_holidays(state_dir: Path | None = None) -> set[date]:
    return set(load_holidays("trading", state_dir))


def settlement_holidays(state_dir: Path | None = None) -> set[date]:
    """Days on which T+N settlement does not advance: trading or clearing holidays."""
    return set(load_holidays("trading", state_dir)) | set(load_holidays("clearing", state_dir))


def cached_years(state_dir: Path | None = None) -> set[int]:
    folder = _dir(state_dir)
    return (
        {int(p.stem.rsplit("_", 1)[1]) for p in folder.glob("nse_cm_trading_*.json")}
        if folder.exists()
        else set()
    )


async def refresh_holidays(state_dir: Path | None = None, *, fetch=None, force: bool = False) -> bool:
    """Fetch the current year's lists when the cache is missing or stale. Returns True when it fetched."""
    from finresearch.fincalc.dates import today_ist

    year = today_ist().year  # the exchange's year: the machine's local date can still be 31-Dec at 00:30 IST
    fresh = all(
        (p := _cache(state_dir, k, year)).exists() and time.time() - p.stat().st_mtime < MAX_AGE_S
        for k in ("trading", "clearing")
    )
    if fresh and not force:
        return False
    for kind in ("trading", "clearing"):
        data = await fetch(kind) if fetch else await _fetch(kind)
        rows = parse_holidays(data)
        if not rows:
            raise ValueError(f"NSE returned no CM {kind} holidays")
        save_holidays(rows, kind, state_dir)
    return True


async def _fetch(kind: Kind) -> dict[str, Any]:
    async with NseClient(warmup_url=WARMUP) as nse:
        data, _ = await nse.get_json("/api/holiday-master", {"type": kind})
    return data
