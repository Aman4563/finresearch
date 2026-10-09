"""Groww Trade API call budget: shared rate buckets per Groww category and a count of today's calls (issue #267).

Groww's published limits (https://groww.in/trade-api/docs/curl, "Rate Limits", read 9-Oct-2026):

    Authentication  5/s   30/min   token generation; /token/api/access also capped at 150 per 24 hours
    Orders         10/s  250/min   (never used: the app is read-only)
    Live Data      10/s  300/min   market quotes, LTP, OHLC
    Non Trading    20/s  500/min   order status, positions, holdings, margin

Which category the historical-candle, option-chain, expiry and contract endpoints count against is not stated [U]:
they are put in Live Data, the stricter of the two read buckets.

Every Groww request from every FinResearch process (the API with its monitor, `monitor run`, the CLI) reserves a slot
in its category through `adapters.http.SharedSlots` (a flock'd file under state_dir/ratelimit), so the whole Mac stays
under Groww's limits however many clients run. A slot is the larger of the per-second and per-minute spacings
(Live Data: max(1/10, 60/300) = 0.2 s apart), which honours both limits without a burst model. A 429 pushes the
category's next slot back for everyone (Retry-After when given, else 2 s).

Calls are also counted per IST day and category (state_dir/ratelimit/groww-calls-<day>.json) for the settings card's
"calls today" figure. A token request beyond AUTH_DAILY_GUARD in one day is refused before it is sent.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from finresearch.adapters.http import SHARED_SLOTS, SharedSlots
from finresearch.fincalc.dates import today_ist
from finresearch.portfolio.connectors.base import ConnectorError

# (per second, per minute) from the docs table above
LIMITS: dict[str, tuple[float, float]] = {"auth": (5, 30), "live": (10, 300), "non_trading": (20, 500)}
LABELS = {"auth": "Login (token)", "live": "Live and historical data", "non_trading": "Holdings, positions, orders",
          "instruments": "Instruments file"}  # fmt: skip
TOKEN_DAILY_CAP = 150  # /token/api/access per 24 hours (docs)
AUTH_DAILY_GUARD = 140  # stop short of the cap: the user's own scripts may also log in with the same key
HOLD_OFF_S = 2.0  # a 429 without Retry-After

_PATHS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("auth", re.compile(r"/token/api/access")),
    ("live", re.compile(r"/live-data/.*|/option-chain/.*|/historical/.*")),
    ("non_trading", re.compile(r"/holdings/.*|/positions/.*|/order/.*|/margins/.*|/user/.*")),
)


class BudgetExceeded(ConnectorError):
    """A Groww call the daily budget does not allow (never sent; the message is safe to show)."""


def category(path: str) -> str:
    for name, pat in _PATHS:
        if pat.fullmatch(path):
            return name
    return "live"  # an unknown read path: count it against the stricter bucket


def interval(cat: str) -> float:
    per_s, per_min = LIMITS[cat]
    return max(1.0 / per_s, 60.0 / per_min)


class Budget:
    """Shared slots plus daily counters. `slots`/`directory`/`today` are injectable for tests."""

    def __init__(self, slots: SharedSlots | None = None, directory: Path | Callable[[], Path] | None = None,
                 today: Callable[[], date] = today_ist) -> None:  # fmt: skip
        self.slots = slots or SHARED_SLOTS
        self._directory = directory
        self._today = today

    def _dir(self) -> Path:
        if callable(self._directory):
            return self._directory()
        if self._directory is not None:
            return self._directory
        from finresearch.config import get_settings

        return Path(get_settings().state_dir) / "ratelimit"

    @contextlib.contextmanager
    def _counts(self, day: date):
        d = self._dir()
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / f"groww-calls-{day.isoformat()}.json", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.pread(fd, 65536, 0)
            try:
                counts: dict[str, int] = json.loads(raw.decode() or "{}")
            except ValueError:
                counts = {}
            box = [counts]
            yield box
            data = json.dumps(box[0]).encode()
            os.ftruncate(fd, 0)
            os.pwrite(fd, data, 0)
        finally:
            os.close(fd)

    def count(self, cat: str, n: int = 1) -> int:
        """Add `n` calls to today's count for `cat`; returns the new count."""
        with self._counts(self._today()) as box:
            box[0][cat] = int(box[0].get(cat, 0)) + n
            return box[0][cat]

    def wait_s(self, cat: str) -> float:
        """Reserve the category's next shared slot and count the call; returns how long to wait for the slot."""
        if cat == "auth":
            with self._counts(self._today()) as box:
                if int(box[0].get("auth", 0)) >= AUTH_DAILY_GUARD:
                    raise BudgetExceeded(f"Groww login refused: {AUTH_DAILY_GUARD} token requests already today "
                                         f"(Groww allows {TOKEN_DAILY_CAP} a day)")  # fmt: skip
                box[0]["auth"] = int(box[0].get("auth", 0)) + 1
        else:
            self.count(cat)
        if cat not in LIMITS:
            return 0.0
        return self.slots.reserve(f"groww-{cat}", interval(cat))

    def hold_off(self, cat: str, seconds: float) -> None:
        if cat in LIMITS:
            self.slots.hold_off(f"groww-{cat}", max(seconds, HOLD_OFF_S))

    def today(self, day: date | None = None) -> dict[str, Any]:
        """Today's calls by category (for the settings card): no secrets, counts only."""
        day = day or self._today()
        p = self._dir() / f"groww-calls-{day.isoformat()}.json"
        try:
            counts = json.loads(p.read_text() or "{}")
        except (OSError, ValueError):
            counts = {}
        rows = []
        for cat in ("live", "non_trading", "auth", "instruments"):
            lim = LIMITS.get(cat)
            rows.append({"category": cat, "label": LABELS[cat], "calls": int(counts.get(cat, 0)),
                         "per_second": lim[0] if lim else None, "per_minute": lim[1] if lim else None,
                         "daily_cap": TOKEN_DAILY_CAP if cat == "auth" else None})  # fmt: skip
        return {"day": day.isoformat(), "categories": rows, "total": sum(r["calls"] for r in rows)}


BUDGET = Budget()


async def before_request(method: str, path: str) -> None:
    """`ReadOnlyHttp`'s hook for api.groww.in: wait for the category's shared slot (and count the call)."""
    import asyncio

    delay = BUDGET.wait_s(category(path))
    if delay > 0:
        await asyncio.sleep(delay)


def after_response(method: str, path: str, status: int, retry_after: str | None) -> None:
    """A 429 backs the whole category off, for every process."""
    if status == 429:
        secs = float(retry_after) if (retry_after or "").strip().isdigit() else HOLD_OFF_S
        BUDGET.hold_off(category(path), min(secs, 60.0))
