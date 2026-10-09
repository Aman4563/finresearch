"""Run a read-only sync for one connection, and the monitor's daily schedule.

`sync_now` = (log in headlessly if the connector can) → read holdings, positions, trades, fund holdings and funds →
`merge.merge_sync` (baselines, trades, reconciliation) → store positions/funds on the connection → one
`broker_sync_log` row. A read failure of one kind (say positions) is recorded and the rest still merges; an expired
session marks the connection "reconnect" and raises an in-app alert once a day (forwarded to the phone when the
user forwards warnings). Nothing here ever retries a login on its own beyond the one headless attempt per sync (a
connector may resend its TOTP once with the next step's code). Any other failure still ends in a log row and, for a
scheduled sync, the day's `failed_day` (#266). One sync or login per connection at a time across processes (`claim`:
a Postgres advisory lock). A today-only trades endpoint (Groww) that failed is read again the same day, a bounded
number of times (TRADES_RETRIES); `trades_through` only moves when the trades were read.

Schedule (`connections_step`, from the monitor tick): once per trading day after SYNC_AFTER IST for every enabled
connection with auto-sync, and the statement inbox every INBOX_EVERY.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from finresearch.db import session_scope
from finresearch.db.models import Alert, BrokerConnection, BrokerSyncLog
from finresearch.fincalc.dates import to_ist
from finresearch.portfolio.connectors import CONNECTORS, INBOX_KEY
from finresearch.portfolio.connectors.base import ConnectorError, ReconnectNeeded, redact
from finresearch.portfolio.connectors.store import build_from, clear_token, set_token, token_valid

log = logging.getLogger(__name__)
SYNC_AFTER = time(16, 0)  # IST: after the 15:30 close, once brokers have settled the day's holdings
INBOX_EVERY = timedelta(minutes=5)
OVERLAP_DAYS = 3  # re-read a few days each sync: late fills and holidays are covered, duplicates are no-ops
HEADLESS = ("totp", "password_totp", "key_secret")
# a today-only trades endpoint (Groww) that failed is read again the same day: up to TRADES_RETRIES more syncs,
# TRADES_RETRY_AFTER apart and doubling (10, 20, 40 minutes); after that the day's scheduled attempts stop (#266)
TRADES_RETRIES = 3
TRADES_RETRY_AFTER = timedelta(minutes=10)
_LOCKS: dict[str, asyncio.Lock] = {}
_LAST_INBOX: dict[str, datetime] = {}


class SyncBusy(RuntimeError):
    """Another process (or request) is syncing or logging in to this connection right now."""


def _lock(key: str) -> asyncio.Lock:
    return _LOCKS.setdefault(key, asyncio.Lock())


def _db_lock(key: str) -> Any:
    """A Postgres session advisory lock on its own connection: held by one process at a time (the API's monitor, a
    `finresearch monitor run`, a manual Sync now or Log in), released on unlock or when the connection dies."""
    from sqlalchemy import text

    from finresearch.db import get_engine

    c = get_engine().connect()
    try:
        got = c.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:k))"), {"k": f"broker_sync:{key}"}
        ).scalar()
        c.commit()
    except Exception:
        c.close()
        raise
    if not got:
        c.close()
        return None
    return c


def _db_unlock(c: Any, key: str) -> None:
    from sqlalchemy import text

    try:
        c.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": f"broker_sync:{key}"})
        c.commit()
        c.close()
    except Exception:
        c.invalidate()  # never return a connection that may still hold the lock to the pool
        raise


@contextlib.asynccontextmanager
async def claim(key: str) -> AsyncIterator[None]:
    """One sync or login per connection across every process (#266): the in-process lock, then the database lock.
    Raises SyncBusy when another process holds it (the caller skips, or answers 409)."""
    async with _lock(key):
        c = await asyncio.to_thread(_db_lock, key)
        if c is None:
            raise SyncBusy(f"{key}: a sync or login is already running")
        try:
            yield
        finally:
            await asyncio.to_thread(_db_unlock, c, key)


def _json_dec(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    return v


async def sync_now(key: str, *, trigger: str = "manual", now: datetime | None = None) -> dict[str, Any]:
    """Sync one broker connection. Returns the sync-log entry as a dict. Raises LookupError (not connected) or
    SyncBusy (another sync or login holds the connection)."""
    if key not in CONNECTORS:
        raise LookupError(f"unknown connection {key}")
    now = now or datetime.now(UTC)
    async with claim(key):
        # filled by _sync as soon as the connector is built, so an unexpected failure's text is redacted too
        secrets: list[str] = []
        try:
            return await _sync(key, trigger, now, secrets)
        except LookupError:
            raise
        except Exception as e:  # anything unexpected still ends in a log row, a status and failed_day (#266)
            # the frames (file, line) but never the raw message: it may carry a token or a PIN (redacted here)
            log.warning("%s sync failed: %s: %s\n%s", key, type(e).__name__, redact(str(e), *secrets)[:300],
                        "".join(traceback.format_tb(e.__traceback__)))  # fmt: skip
            return await _finish_async(key, trigger, now, "error", {},
                                       redact(f"sync failed unexpectedly ({type(e).__name__})", *secrets),
                                       today=None)  # fmt: skip


async def _sync(key: str, trigger: str, now: datetime, secrets_out: list[str]) -> dict[str, Any]:
    today = to_ist(now).date()
    with session_scope() as s:
        row = s.get(BrokerConnection, key)
        if row is None or not row.enabled:
            raise LookupError(f"{key} is not connected or turned off")
        cols = (row.key, dict(row.config or {}), row.token)
        valid = token_valid(row, now)
        state0 = dict(row.state or {})
        # trades are read from the last day they were actually read, not the last sync: a sync whose trades read
        # failed ("partial") still advances last_sync_day, and those days' trades must be asked for again
        through = state0.get("trades_through")
        last_day = date.fromisoformat(through) if through else row.last_sync_day
    token_rev = state0.get("token_rev")
    conn = await asyncio.to_thread(build_from, *cols)  # Keychain reads: a subprocess each, off the event loop
    secrets = conn.secret_values()
    secrets_out[:] = secrets
    if not valid:
        try:
            grant = await conn.login()
        except ReconnectNeeded as e:
            return await _finish_async(
                key, trigger, now, "reconnect", {}, redact(str(e), *secrets), today=None
            )
        except ConnectorError as e:
            return await _finish_async(key, trigger, now, "error", {}, redact(str(e), *secrets), today=None)
        conn.token = grant.token
        secrets = conn.secret_values()
        secrets_out[:] = secrets
        token_rev = await asyncio.to_thread(_store_token, key, grant)

    data: dict[str, Any] = {}
    errors: list[str] = []
    caps = conn.capabilities
    # Groww: whether T1 shares are inside `quantity` is checked on the data (#282)
    t1_aware = hasattr(conn, "t1_semantics")
    if t1_aware:
        conn.t1_semantics = (state0.get("t1_semantics") or {}).get("result")
    today_only = bool(getattr(conn, "trades_today_only", False))
    since = (
        (last_day - timedelta(days=OVERLAP_DAYS))
        if last_day
        else today - timedelta(days=conn.first_sync_days)
    )
    steps = [("holdings", conn.holdings), ("positions", conn.positions), ("mf", conn.mf_holdings),
             ("funds", conn.funds), ("trades", lambda: conn.trades(since, today))]  # fmt: skip
    for name, fn in steps:
        if name not in caps:
            continue
        try:
            data[name] = await fn()
        except ReconnectNeeded as e:
            return await _finish_async(key, trigger, now, "reconnect", {"read": sorted(data)},
                                       redact(str(e), *secrets), today=None, expire=True,
                                       token_rev=token_rev)  # fmt: skip
        except ConnectorError as e:
            errors.append(f"{name}: {redact(str(e), *secrets)}")
        except Exception as e:  # a response shape we did not expect: keep the rest of the sync
            log.warning("%s %s read failed: %s", key, name, type(e).__name__)
            errors.append(f"{name}: unexpected response ({type(e).__name__})")

    from finresearch.portfolio.connectors.merge import merge_sync

    have_holdings = "holdings" in data
    have_trades = "trades" in data
    with session_scope() as s:
        res = merge_sync(s, account=conn.account, source=conn.source, label=conn.label,
                         holdings=data.get("holdings") or [], trades=data.get("trades") or [], today=today,
                         now=now, holdings_include_today=conn.holdings_include_today,
                         mf_holdings=data.get("mf") or None, allow_baseline=have_holdings,
                         order_keyed=bool(getattr(conn, "fills_aggregated", False)))  # fmt: skip
        summary = res.as_dict()
        if not have_holdings:  # not read: unknown, never "0 differences"
            summary["reconciliation"], summary["reconciled"], summary["differences"] = [], None, None
        summary["read"] = {k: (len(v) if isinstance(v, list) else 1) for k, v in data.items()}
        # the days the trades read covers: none when it failed; a today-only endpoint covers today whatever was asked
        summary["trades_since"] = (today if today_only else since).isoformat() if have_trades else None
        row = s.get(BrokerConnection, key)
        state = dict(row.state or {})
        if "positions" in data:
            state["positions"] = [{k: _json_dec(v) for k, v in vars(p).items()} for p in data["positions"]][
                :200
            ]
        if "funds" in data:
            state["funds"] = {k: _json_dec(v) for k, v in (data["funds"] or {}).items()}
        state["last_summary"] = {k: summary[k] for k in ("added", "duplicates", "updated", "differences",
                                                         "reconciled", "read", "covered_by_baseline",
                                                         "trades_since")}  # fmt: skip
        state["last_summary"]["baselines"] = len(summary["baselines"])
        state["last_summary"]["conflicts"] = len(summary["conflicts"])
        if have_trades:
            state["trades_through"] = today.isoformat()
        if t1_aware and have_holdings:
            state["t1_semantics"] = t1_state(state.get("t1_semantics"), data["holdings"], today)
        state.pop("reconnect_alert_day", None)
        state.pop("failed_day", None)
        state.pop("trades_retry", None)
        if "trades" in caps and not have_trades and today_only:
            # the endpoint only answers today: read it again later today, a bounded number of times
            prev = (row.state or {}).get("trades_retry") or {}
            n = int(prev.get("attempts", 0)) + 1 if prev.get("day") == today.isoformat() else 1
            if n <= TRADES_RETRIES:
                state["trades_retry"] = {"day": today.isoformat(), "attempts": n,
                                         "next_at": (now + TRADES_RETRY_AFTER * 2 ** (n - 1)).isoformat()}  # fmt: skip
                summary["trades_retry_at"] = state["trades_retry"]["next_at"]
            else:
                state["failed_day"] = today.isoformat()  # the cap: no more scheduled attempts today
                errors.append(f"trades: today's orders could not be read after {n} attempts; the broker answers "
                              "today's orders only, so add them from its order-history export")  # fmt: skip
        row.state = state
    status = "partial" if errors else "ok"
    return await _finish_async(key, trigger, now, status, summary, "; ".join(errors) or None, today=today)


def t1_state(prev: dict[str, Any] | None, holdings: list[Any], today: date) -> dict[str, Any]:
    """The connection's record of how the broker reports T1 shares (#282). The first sync whose holdings decide it
    (rows with T1 whose numbers fit exactly one hypothesis, and all fitting the same one) stores `result`, its `day`
    and the `evidence` counts; later syncs keep that result and only update `last_seen`. Rows that fit different
    hypotheses in one sync decide nothing (`conflict`)."""
    out = dict(prev or {})
    seen = {k: sum(1 for h in holdings if getattr(h, "t1_check", None) == k)
            for k in ("separate", "included", "unknown")}  # fmt: skip
    if not any(seen.values()):
        return out  # no holding with T1 shares in this sync: nothing learned
    out["last_seen"] = {"day": today.isoformat(), "evidence": seen}
    decisive = [k for k in ("separate", "included") if seen[k]]
    if not out.get("result"):
        if len(decisive) == 1:
            out.update(result=decisive[0], day=today.isoformat(), evidence=seen)
            out.pop("conflict", None)
        elif len(decisive) == 2:
            out["conflict"] = today.isoformat()
    return out


def _store_token(key: str, grant: Any) -> str:
    with session_scope() as s:
        return set_token(s, key, grant)


async def _finish_async(*args: Any, **kw: Any) -> dict[str, Any]:
    return await asyncio.to_thread(_finish, *args, **kw)  # clear_token deletes a Keychain item: off the loop


def _finish(key: str, trigger: str, now: datetime, status: str, summary: dict[str, Any], error: str | None, *,
            today: date | None, expire: bool = False, token_rev: str | None = None) -> dict[str, Any]:  # fmt: skip
    with session_scope() as s:
        row = s.get(BrokerConnection, key)
        if row is not None:
            if trigger == "scheduled" and status in ("reconnect", "error"):
                # one scheduled attempt per day: a wrong TOTP seed or a lapsed subscription must not retry every tick
                row.state = {**(row.state or {}), "failed_day": to_ist(now).date().isoformat()}
            if status == "reconnect":
                row.status = "reconnect"
                if expire and (row.state or {}).get("token_rev") == token_rev:
                    clear_token(row)  # only the token this sync used: a login since then stored a newer one
                day = to_ist(now).date().isoformat()
                if (row.state or {}).get("reconnect_alert_day") != day and trigger == "scheduled":
                    s.add(Alert(kind="broker_reconnect", level="warn", data={"connection": key},
                                message=f"{CONNECTORS[key].label}: log in again to keep your portfolio in sync "
                                        "(Profile → Connections)."))  # fmt: skip
                    row.state = {**(row.state or {}), "reconnect_alert_day": day}
            elif status == "error":
                row.status = "error"
            else:
                row.status = "connected"
                row.last_sync_at = now
                if today is not None:
                    row.last_sync_day = today
            row.last_error = error
        entry = BrokerSyncLog(key=key, trigger=trigger, status=status, started_at=now, finished_at=datetime.now(UTC),
                              summary=summary, error=error)  # fmt: skip
        s.add(entry)
        s.flush()
        return log_json(entry)


def log_json(e: BrokerSyncLog) -> dict[str, Any]:
    return {"id": e.id, "key": e.key, "trigger": e.trigger, "status": e.status,
            "started_at": e.started_at.isoformat() if e.started_at else None,
            "finished_at": e.finished_at.isoformat() if e.finished_at else None, "summary": e.summary or {},
            "error": e.error}  # fmt: skip


def recent_logs(s, key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    q = select(BrokerSyncLog).order_by(BrokerSyncLog.id.desc()).limit(limit)
    if key:
        q = q.where(BrokerSyncLog.key == key)
    return [log_json(e) for e in s.scalars(q)]


def due(row: BrokerConnection, now: datetime, holidays: set[date] | None = None) -> bool:
    """A scheduled sync is due once per trading day after SYNC_AFTER IST (a missed day is caught up on the next)."""
    ist = to_ist(now)
    if not (row.enabled and row.auto_sync):
        return False
    if row.status == "reconnect" and CONNECTORS[row.key].auth_kind not in HEADLESS:
        return False  # waiting for the user to log in again: the alert was raised once
    if (row.state or {}).get("failed_day") == ist.date().isoformat():
        return False  # today's scheduled attempt failed: tomorrow (or Sync now) tries again
    d = ist.date()
    retry = (row.state or {}).get("trades_retry") or {}
    if retry.get("day") == d.isoformat() and int(retry.get("attempts", 0)) <= TRADES_RETRIES:
        try:
            # today's orders could not be read: a today-only endpoint must be asked again today
            if now >= datetime.fromisoformat(retry["next_at"]):
                return True
        except (KeyError, TypeError, ValueError):
            return True
    trading = d.weekday() < 5 and d not in (holidays or set())
    if trading and ist.time() >= SYNC_AFTER:
        # a manual "Sync now" before the close also sets last_sync_day: the after-close read is still owed
        early = row.last_sync_at is not None and to_ist(row.last_sync_at) < datetime.combine(
            d, SYNC_AFTER, ist.tzinfo
        )
        return row.last_sync_day != d or early
    # not after today's close: catch up if the last scheduled sync is older than the previous weekday
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5 or prev in (holidays or set()):
        prev -= timedelta(days=1)
    return row.last_sync_day is not None and row.last_sync_day < prev


_HOLIDAYS_WARNED: set[date] = set()


def _holidays(day: date) -> set[date]:
    """The NSE trading holidays for the schedule. Unlike the after-close market jobs (monitor.market_days, which fail
    closed and pause), an unknown holiday list does NOT pause broker syncs: like the IPO watch plan and the intraday
    archive, skipping one loses data that cannot be fetched later (Groww's order list answers today only). Weekdays
    then count as trading days; a sync on a holiday reads an empty order list and unchanged holdings (idempotent).
    A list that cannot be read at all is treated the same way. Logged once a day."""
    try:
        from finresearch.adapters.nse_holidays import known_trading_holidays

        got = known_trading_holidays(day)
    except Exception:
        got = None
    if got is None:
        if day not in _HOLIDAYS_WARNED:
            _HOLIDAYS_WARNED.add(day)
            log.warning(
                "NSE %s holiday list unknown: broker syncs treat every weekday as a trading day", day.year
            )
        return set()
    return set(got)


async def connections_step(now: datetime) -> dict[str, int]:
    """The monitor's hook: scheduled syncs and the statement inbox. Failures never break the tick."""
    out: dict[str, int] = {}
    holidays = _holidays(to_ist(now).date())
    with session_scope() as s:
        rows = list(s.scalars(select(BrokerConnection)))
        dues = [r.key for r in rows if r.key in CONNECTORS and due(r, now, holidays)]
        inbox_on = any(r.key == INBOX_KEY and r.enabled for r in rows)
    for key in dues:
        try:
            res = await sync_now(key, trigger="scheduled", now=now)
            out["broker_synced"] = out.get("broker_synced", 0) + (res["status"] in ("ok", "partial"))
        except SyncBusy:
            log.info("scheduled %s sync skipped: another process is syncing it", key)
        except Exception:
            log.warning("scheduled %s sync failed", key, exc_info=True)
    last = _LAST_INBOX.get("at")
    if inbox_on and (last is None or now - last >= INBOX_EVERY):
        _LAST_INBOX["at"] = now
        try:
            from finresearch.portfolio.connectors.inbox import scan

            got = await asyncio.to_thread(scan, now=now)
            if got["imported"]:
                out["inbox_imported"] = got["imported"]
        except Exception:
            log.warning("statement inbox scan failed", exc_info=True)
    return out
