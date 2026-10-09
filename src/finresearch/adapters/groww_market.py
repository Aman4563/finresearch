"""Groww Trade API market data (read-only): LTP, daily candles, option chains and the instruments file (issue #267).

The user pays for the Groww Trade API; when the Groww connection is on and its session is valid the app reads prices
here first and falls back to NSE/BSE (AMFI for funds: Groww has no fund data) on any error or unknown instrument.
Nothing here can place an order: requests go through `ReadOnlyHttp` with a GET-only allowlist (MARKET_PATHS on
api.groww.in, the instruments CSV on growwapi-assets.groww.in), and every call takes a shared slot in its Groww
rate category (`adapters.groww_budget`).

Official docs (read 9-Oct-2026), and the growwapi 1.5.0 SDK source where the pages leave a detail out:
* LTP ``GET /v1/live-data/ltp?segment=CASH&exchange_symbols=NSE_RELIANCE&exchange_symbols=BSE_SENSEX`` → ``payload``
  = {"NSE_RELIANCE": 2334.2, ...}; at most 50 instruments a call. The SDK passes the symbols as a tuple, which
  `requests` sends as a repeated parameter (as here); the SDK docstring says a comma-joined string works too [U].
* Daily candles ``GET /v1/historical/candles?exchange=NSE&segment=CASH&groww_symbol=NSE-WIPRO&start_time=
  yyyy-MM-dd HH:mm:ss&end_time=...&candle_interval=1day`` (the non-deprecated endpoint; "1day" is the SDK constant
  CANDLE_INTERVAL_DAY) → ``payload.candles`` = [[timestamp, open, high, low, close, volume, open_interest], ...]. The
  docs example shows the timestamp as "2025-09-24T10:30:00" (no zone: read as IST [U]); the deprecated endpoint gave
  epoch seconds, so both are read. Max range per request for 1day: 180 days on the backtesting page (the older page
  and issue #267 say 1080 [U]): requests are made in 180-day windows. Data from 2020 on ("Data of Equities, Indices
  and FNO instruments are available from 2020").
* Whether daily closes are adjusted for splits/bonuses is not stated [U]: they are used only where both readings give
  the same number (see `portfolio.history.Fetcher`), and whether a day's 1day close equals the exchange's official
  close is not stated either [U]: after the session the valuation uses the exchange's official close and a Groww
  close only when that is not out, labelled CLOSE_FALLBACK_LABEL; each stock-day with both is compared and stored
  (`groww_close_check`, #283) for the agreement rate on the Groww card.
* Option chain ``GET /v1/option-chain/exchange/{NSE|BSE}/underlying/{NIFTY}?expiry_date=YYYY-MM-DD`` →
  ``payload.underlying_ltp`` and ``payload.strikes[strike][CE|PE]`` = {greeks: {delta, gamma, theta, vega, rho, iv},
  trading_symbol, ltp, open_interest, volume}. No change in OI, bid or ask: those stay unknown (None), never 0.
* Instruments CSV ``https://growwapi-assets.groww.in/instruments/instrument.csv`` (no auth): exchange,
  exchange_token, trading_symbol, groww_symbol, name, instrument_type, segment, series, isin, underlying_symbol, ...
  Downloaded at most once per IST day and cached on disk. That BSE rows carry the BSE scrip code as exchange_token,
  and how indices are named, is not shown in the docs [U]: an instrument the file does not resolve falls back.
* Groww's OHLC endpoint is not used: its documented sample payload is a string ("{open: 149.50,...}") [U].
"""

from __future__ import annotations

import asyncio
import csv
import fcntl
import io
import json
import logging
import os
import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from finresearch.fincalc.dates import IST, to_ist
from finresearch.portfolio.connectors.base import ConnectorError, ReadOnlyHttp

log = logging.getLogger(__name__)

BASE = "https://api.groww.in/v1"
ASSETS = "https://growwapi-assets.groww.in"
INSTRUMENTS_PATH = "/instruments/instrument.csv"
# GET only, and only these: no order, margin-for-order, smart-order or socket-token path is reachable from here
MARKET_PATHS = (r"/live-data/ltp", r"/historical/candles", r"/historical/expiries",
                r"/option-chain/exchange/(NSE|BSE)/underlying/[A-Z0-9&_-]{1,30}")  # fmt: skip
ASSET_PATHS = (r"/instruments/instrument\.csv",)
SOURCE = "Groww"
LTP_PROVENANCE, CLOSE_PROVENANCE = f"{SOURCE} LTP", f"{SOURCE} daily close"
# the label of a Groww close used as a price: only when the exchange's official close is not available (#283)
CLOSE_FALLBACK_LABEL = "Groww daily close (not the exchange's official close)"
CLOSE_MATCH_TOL = 0.01  # ₹: a Groww close within one paisa of the official close counts as the same figure
BATCH = 50  # instruments per LTP call (docs)
GROWW_FROM = date(2020, 1, 1)  # earliest daily candle Groww serves (docs)
CANDLE_WINDOW_DAYS = 180

# market hours (IST): normal session 09:15-15:30, closing session to 16:00 (NSE)
OPEN, CLOSE, CLOSING_END = time(9, 15), time(15, 30), time(16, 0)
LTP_TTL_S = 15.0  # in session: one LTP batch per 15 s at most, however many pages ask
CHAIN_TTL_S = 30.0
RETRY_TTL_S = 600.0  # today's close not out yet at Groww: ask again in 10 minutes
FAIL_TTL_S = 60.0  # a failed call is not repeated for a minute (the caller falls back meanwhile)


# --------------------------------------------------------------------------- market hours
def _holidays() -> set[date]:
    """Known NSE trading holidays; an empty or missing list counts every weekday as trading (calls are then made on an
    unlisted holiday, which costs a call but never hides a price)."""
    try:
        from finresearch.adapters.nse_holidays import trading_holidays

        return trading_holidays()
    except Exception:
        return set()


def trading_day(d: date, holidays: set[date] | None = None) -> bool:
    return d.weekday() < 5 and d not in (holidays if holidays is not None else _holidays())


def phase(now: datetime, holidays: set[date] | None = None) -> str:
    """'open' 09:15-15:30, 'closing' 15:30-16:00, 'after' after 16:00, 'pre' before 09:15 on a trading day; 'closed' on
    a weekend or holiday."""
    ist = to_ist(now)
    if not trading_day(ist.date(), holidays):
        return "closed"
    t = ist.time()
    if t < OPEN:
        return "pre"
    if t < CLOSE:
        return "open"
    return "closing" if t < CLOSING_END else "after"


def next_open(now: datetime, holidays: set[date] | None = None) -> datetime:
    hol = holidays if holidays is not None else _holidays()
    ist = to_ist(now)
    d = ist.date()
    if trading_day(d, hol) and ist.time() < OPEN:
        return datetime.combine(d, OPEN, IST)
    d += timedelta(days=1)
    while not trading_day(d, hol):
        d += timedelta(days=1)
    return datetime.combine(d, OPEN, IST)


def ltp_ttl(now: datetime, holidays: set[date] | None = None) -> float:
    """LTP is asked for only in session (and the closing session); then it lives LTP_TTL_S."""
    return LTP_TTL_S if phase(now, holidays) in ("open", "closing") else 0.0


def closes_ttl(now: datetime, holidays: set[date] | None = None) -> float:
    """Daily closes change once a day: until 16:00 on a trading day (the previous close is fixed), then until the next
    session opens (weekends and holidays included: no calls on a closed day once cached)."""
    ph = phase(now, holidays)
    ist = to_ist(now)
    if ph in ("pre", "open", "closing"):
        return (datetime.combine(ist.date(), CLOSING_END, IST) - ist).total_seconds()
    return (next_open(now, holidays) - ist).total_seconds()


# --------------------------------------------------------------------------- instruments
# index names the app uses -> the trading symbols Groww's instruments file may list them under [U: not in the docs]
INDEX_ALIASES = {"NIFTY 50": ("NIFTY",), "NIFTY BANK": ("BANKNIFTY",), "NIFTY NEXT 50": ("NIFTYNXT50", "NIFTYJR"),
                 "NIFTY FINANCIAL SERVICES": ("FINNIFTY",), "NIFTY MID SELECT": ("MIDCPNIFTY",),
                 "SENSEX": ("SENSEX",)}  # fmt: skip


class Instruments:
    """The CASH-segment rows of Groww's instruments file, looked up by (exchange, trading symbol), (exchange,
    exchange token) and index name."""

    def __init__(self, rows: list[dict[str, str]], day: date | None = None) -> None:
        self.day = day
        self.by_symbol: dict[tuple[str, str], dict[str, str]] = {}
        self.by_token: dict[tuple[str, str], dict[str, str]] = {}
        for r in rows:
            if (r.get("segment") or "").upper() != "CASH":
                continue
            ex = (r.get("exchange") or "").upper()
            sym = (r.get("trading_symbol") or "").strip().upper()
            if sym:
                self.by_symbol.setdefault((ex, sym), r)
            tok = (r.get("exchange_token") or "").strip()
            if tok:
                self.by_token.setdefault((ex, tok), r)

    @classmethod
    def parse(cls, text: str, day: date | None = None) -> Instruments:
        return cls(list(csv.DictReader(io.StringIO(text))), day)

    def __len__(self) -> int:
        return len(self.by_symbol)

    def stock(self, symbol: str, exchange: str = "NSE") -> dict[str, str] | None:
        """An NSE symbol, or a BSE scrip code (BSE rows keyed by exchange_token [U]) or BSE trading symbol."""
        ex, s = exchange.upper(), symbol.strip().upper()
        if ex == "BSE" and s.isdigit():
            return self.by_token.get(("BSE", s))
        return self.by_symbol.get((ex, s))

    def index(self, name: str) -> dict[str, str] | None:
        nm = " ".join(name.upper().split())
        for ex in ("NSE", "BSE"):
            for alias in (*INDEX_ALIASES.get(nm, ()), nm, nm.replace(" ", "")):
                r = self.by_symbol.get((ex, alias))
                if r is not None and (r.get("instrument_type") or "").upper() in ("IDX", "INDEX", ""):
                    return r
        return None


def exchange_symbol(row: dict[str, str]) -> str:
    """The LTP endpoint's key for a row: "NSE_RELIANCE", "BSE_SENSEX" (docs)."""
    return f"{row['exchange'].upper()}_{row['trading_symbol'].strip().upper()}"


def groww_symbol(row: dict[str, str]) -> str:
    """The candles endpoint's symbol: the file's groww_symbol ("NSE-WIPRO"), else exchange-symbol."""
    return (
        row.get("groww_symbol") or ""
    ).strip() or f"{row['exchange'].upper()}-{row['trading_symbol'].upper()}"


# --------------------------------------------------------------------------- parsing
def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None  # NaN, zero and negative are "no price", never a price of 0


def _ok(body: Any) -> Any:
    if not isinstance(body, dict) or str(body.get("status", "SUCCESS")).upper() != "SUCCESS":
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        raise ConnectorError(f"Groww answered {msg or 'FAILURE'}")
    return body.get("payload")


def candle_day(ts: Any) -> date | None:
    """The IST trading day of a candle timestamp: ISO text (the current endpoint) or epoch seconds/milliseconds."""
    if isinstance(ts, (int, float)):
        secs = ts / 1000 if ts > 10**11 else ts
        return datetime.fromtimestamp(secs, UTC).astimezone(IST).date()
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        return to_ist(dt).date() if dt.tzinfo else dt.date()
    return None


def parse_candles(payload: Any) -> dict[date, dict[str, float | None]]:
    out: dict[date, dict[str, float | None]] = {}
    for c in (payload or {}).get("candles") or [] if isinstance(payload, dict) else []:
        if not isinstance(c, (list, tuple)) or len(c) < 5:
            continue
        d, close = candle_day(c[0]), _num(c[4])
        if d is None or close is None:
            continue
        out[d] = {"open": _num(c[1]), "high": _num(c[2]), "low": _num(c[3]), "close": close,
                  "volume": float(c[5]) if len(c) > 5 and isinstance(c[5], (int, float)) else None}  # fmt: skip
    return out


# --------------------------------------------------------------------------- the session
KEY = "groww"
TOKEN_RECHECK_S = (
    60.0  # the connection row is re-read at most once a minute (turning Groww off takes effect then)
)


class Session:
    """The Groww access token, read from the connection store (Keychain via `store.token_of`) and kept in this
    process's memory until it expires or the row changes, so a price refresh never touches the Keychain.

    Reading the session never logs in (a GET must not write): `session_step`, from the monitor tick, does the one
    daily login through the broker sync when needed; until then the prices come from NSE/BSE."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(UTC))
        self._token: str | None = None
        self._expires: datetime | None = None
        self._checked = -1e18
        self._state = "not_connected"

    def forget(self) -> None:
        """A 401/403 from Groww: drop the token now (re-read from the store at the next check)."""
        self._token, self._checked, self._state = None, -1e18, "refused"

    @property
    def state(self) -> str:
        return self._state

    def token(self) -> str | None:
        now = self.clock()
        if self._token and self._expires is not None and self._expires <= now:
            self._token, self._checked = None, -1e18
        if _time.monotonic() - self._checked < TOKEN_RECHECK_S:
            return self._token
        self._checked = _time.monotonic()
        try:
            self._token, self._expires, self._state, _ = _read_token(now)
        except Exception as e:  # no database (tests without one, a CLI without settings): Groww is not used
            log.debug("Groww session unavailable: %s", type(e).__name__)
            self._token, self._state = None, "unavailable"
        return self._token


async def session_step(now: datetime) -> dict[str, int]:
    """The monitor tick's hook (never a GET): when the Groww session has expired during market hours, log in once a
    day through the broker sync so market data can use Groww. Not when the connection or auto-sync is off, or today's
    login already failed or was already tried (by any process)."""
    if phase(now) not in ("open", "closing"):
        return {}
    try:
        tok, _, _, can_login = _read_token(now)
    except Exception:
        return {}
    if tok is not None or not can_login or not _claim_login_day(to_ist(now).date()):
        return {}
    await _login()
    return {"groww_market_login": 1}


def _read_token(now: datetime) -> tuple[str | None, datetime | None, str, bool]:
    """(token, expiry, state, may log in) for the Groww row."""
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.store import missing_fields, token_of, token_valid

    with session_scope() as s:
        row = s.get(BrokerConnection, KEY)
        if row is None:
            return None, None, "not_connected", False
        if not row.enabled:
            return None, None, "off", False
        if token_valid(row, now):
            return token_of(row), row.token_expires_at, "connected", False
        failed = (row.state or {}).get("failed_day") == to_ist(now).date().isoformat()
        ok = bool(row.auto_sync) and not failed and not missing_fields(KEY, row.config or {})
        return None, None, "expired", ok


def _claim_login_day(day: date) -> bool:
    """True once per IST day across every process (an O_EXCL marker file under state_dir/groww_market)."""
    try:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("login-*.marker"):
            if old.name != f"login-{day.isoformat()}.marker":
                old.unlink(missing_ok=True)
        fd = os.open(d / f"login-{day.isoformat()}.marker", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except OSError:
        return False


async def _login() -> None:
    try:
        from finresearch.portfolio.connectors.sync import sync_now

        res = await sync_now(KEY, trigger="market_data")
        log.info("Groww session for market data: sync %s", res.get("status"))
    except Exception as e:
        log.warning("Groww login for market data failed: %s", type(e).__name__)
    finally:
        MARKET.session._checked = -1e18  # re-read the row at the next request


def _dir() -> Path:
    from finresearch.config import get_settings

    return Path(get_settings().state_dir) / "groww_market"


# --------------------------------------------------------------------------- the client
class GrowwMarket:
    """Groww market data with caches that know the market's hours, batching and in-flight coalescing. One per process
    (`MARKET`); every method returns nothing (or raises ConnectorError) when Groww cannot answer, so callers fall
    back."""

    def __init__(self, *, session: Session | None = None, clock: Callable[[], datetime] | None = None,
                 holidays: Callable[[], set[date]] = _holidays) -> None:  # fmt: skip
        self.clock = clock or (lambda: datetime.now(UTC))
        self.session = session or Session(self.clock)
        self.holidays = holidays
        self._ltp: dict[str, tuple[float, float | None, datetime]] = {}  # key -> (expires, price, fetched)
        self._closes: dict[str, tuple[float, dict[date, float]]] = {}  # groww symbol -> (expires, closes)
        self._chains: dict[tuple[str, date], tuple[float, Any]] = {}
        self._windows: dict[tuple, tuple[float, dict]] = {}  # candle windows asked lately
        self._inflight: dict[tuple[int, Any], asyncio.Future] = {}
        self._instruments: Instruments | None = None
        # (symbol, exchange) -> (valid until, the exchange quote carrying its official close): after the session the
        # official close does not change until the next open, so it is asked for once (#283)
        self.official: dict[tuple[str, str], tuple[float, Any]] = {}
        self._instruments_failed = -1e18
        self.supplied: dict[
            str, str
        ] = {}  # what Groww answered lately, for the settings card: kind -> ISO time

    # ---- plumbing
    def _now_s(self) -> float:
        return self.clock().timestamp()

    def _api(self, token: str) -> ReadOnlyHttp:
        return ReadOnlyHttp(BASE, read_paths=MARKET_PATHS, secrets=(token,),
                            headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                     "X-API-VERSION": "1.0"})  # fmt: skip

    def token(self) -> str | None:
        return self.session.token()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        from finresearch.portfolio.connectors.base import ReconnectNeeded

        tok = self.token()
        if not tok:
            raise ConnectorError("no valid Groww session")
        try:
            return _ok(await self._api(tok).get(path, params))
        except ReconnectNeeded:
            self.session.forget()
            raise

    async def _once(self, key: Any, fn: Callable[[], Any]) -> Any:
        """Run `fn` once for concurrent identical requests in this event loop; the others await its result."""
        loop = asyncio.get_running_loop()
        k = (id(loop), key)
        fut = self._inflight.get(k)
        if fut is not None:
            return await asyncio.shield(fut)
        fut = loop.create_future()
        self._inflight[k] = fut
        try:
            v = await fn()
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()  # retrieved: no "never retrieved" warning when nobody else waited
            raise
        else:
            fut.set_result(v)
            return v
        finally:
            self._inflight.pop(k, None)

    def _mark(self, kind: str) -> None:
        self.supplied[kind] = to_ist(self.clock()).isoformat(timespec="seconds")

    # ---- instruments
    async def instruments(self) -> Instruments | None:
        """Groww's instruments file: downloaded at most once per IST day (cached on disk; every process shares the
        file), yesterday's copy when today's download fails, None when there is none."""
        today = to_ist(self.clock()).date()
        if self._instruments is not None and self._instruments.day == today:
            return self._instruments
        if self._now_s() - self._instruments_failed < FAIL_TTL_S * 30:
            return self._instruments
        return await self._once("instruments", lambda: self._load_instruments(today))

    async def _load_instruments(self, today: date) -> Instruments | None:
        d = _dir()
        path = d / f"instruments-{today.isoformat()}.csv"
        try:
            d.mkdir(parents=True, exist_ok=True)
            lock = os.open(d / "instruments.lock", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)  # another process may be downloading today's file right now
            if not path.exists():
                try:
                    body = await ReadOnlyHttp(ASSETS, read_paths=ASSET_PATHS).get_bytes(INSTRUMENTS_PATH)
                    _count("instruments")
                    tmp = path.with_suffix(".tmp")
                    tmp.write_bytes(body)
                    tmp.replace(path)
                    for old in d.glob("instruments-*.csv"):
                        if old != path:
                            old.unlink(missing_ok=True)
                except (ConnectorError, OSError) as e:
                    log.warning("Groww instruments file not downloaded: %s", e)
                    self._instruments_failed = self._now_s()
            files = sorted(d.glob("instruments-*.csv"))
            if not files:
                return None
            p = files[-1]
            text = await asyncio.to_thread(p.read_text, "utf-8", "replace")
            inst = Instruments.parse(text, date.fromisoformat(p.stem.split("-", 1)[1]))
            self._instruments = inst if len(inst) else None
            return self._instruments
        finally:
            os.close(lock)

    # ---- LTP (batched, cached in session, coalesced per instrument)
    async def ltp(self, keys: list[str], *, cached_only: bool = False) -> dict[str, tuple[float, datetime]]:
        """{"NSE_INFY": (price, fetched at)} for the keys Groww priced. Only asked in session (09:15-16:00 IST on a
        trading day); 50 keys a call; a key fetched in the last LTP_TTL_S is served from memory, and a key another
        request is already fetching is awaited, not asked again. Unpriced keys are simply absent."""
        now_s, out = self._now_s(), {}
        todo, waits = [], {}
        for k in dict.fromkeys(keys):
            hit = self._ltp.get(k)
            if hit is not None and hit[0] > now_s:
                if hit[1] is not None:
                    out[k] = (hit[1], hit[2])
                continue
            fut = self._inflight.get((id(asyncio.get_running_loop()), ("ltp", k)))
            if fut is not None:
                waits[k] = fut
            else:
                todo.append(k)
        if cached_only or ltp_ttl(self.clock(), self.holidays()) <= 0:
            todo = []
        if todo:
            loop = asyncio.get_running_loop()
            futs = {k: loop.create_future() for k in todo}
            for k, f in futs.items():
                self._inflight[(id(loop), ("ltp", k))] = f
            try:
                for i in range(0, len(todo), BATCH):
                    await self._ltp_batch(todo[i : i + BATCH], futs)
            finally:
                for k, f in futs.items():
                    if not f.done():
                        f.set_result(None)
                    self._inflight.pop((id(loop), ("ltp", k)), None)
            waits.update(futs)
        for k, f in waits.items():
            got = await asyncio.shield(f)
            if got is not None:
                out[k] = got
        return out

    async def _ltp_batch(self, chunk: list[str], futs: dict[str, asyncio.Future]) -> None:
        now = self.clock()
        try:
            payload = await self._get("/live-data/ltp", {"segment": "CASH", "exchange_symbols": tuple(chunk)})
        except Exception as e:  # ConnectorError and anything unexpected: these keys fall back for a minute
            log.info("Groww LTP failed (%s): falling back", type(e).__name__)
            for k in chunk:
                self._ltp[k] = (self._now_s() + FAIL_TTL_S, None, now)
            return
        ttl = ltp_ttl(now, self.holidays()) or LTP_TTL_S
        prices = payload if isinstance(payload, dict) else {}
        for k in chunk:
            p = _num(prices.get(k))
            self._ltp[k] = (self._now_s() + (ttl if p is not None else FAIL_TTL_S), p, now)
            futs[k].set_result((p, now) if p is not None else None)
        self._mark("ltp")

    # ---- daily candles
    async def candles(
        self, row: dict[str, str], start: date, end: date
    ) -> dict[date, dict[str, float | None]]:
        """Daily candles for [start, end] (from 2020 at the earliest), in CANDLE_WINDOW_DAYS windows. Raises
        ConnectorError when any window fails (the caller falls back for the whole range)."""
        start = max(start, GROWW_FROM)
        out: dict[date, dict[str, float | None]] = {}
        lo = start
        while lo <= end:
            hi = min(end, lo + timedelta(days=CANDLE_WINDOW_DAYS - 1))
            params = {"exchange": row["exchange"].upper(), "segment": "CASH", "groww_symbol": groww_symbol(row),
                      "start_time": f"{lo.isoformat()} 00:00:00", "end_time": f"{hi.isoformat()} 23:59:59",
                      "candle_interval": "1day"}  # fmt: skip
            key = ("candles", params["groww_symbol"], lo, hi)
            hit = self._windows.get(key)
            if hit is not None and hit[0] > self._now_s():
                got = hit[1]
            else:
                got = await self._once(key, lambda p=params: self._candles_call(p))
                # a window that ends before today never changes; one reaching today changes after 16:00 only
                today = to_ist(self.clock()).date()
                ttl = 86400.0 if hi < today - timedelta(days=5) else closes_ttl(self.clock(), self.holidays())
                if hi >= today and today not in got and phase(self.clock(), self.holidays()) == "after":
                    ttl = RETRY_TTL_S  # today's candle is not at Groww yet
                self._windows[key] = (self._now_s() + ttl, got)
            out.update(got)
            lo = hi + timedelta(days=1)
        self._mark("candles")
        return {d: v for d, v in out.items() if start <= d <= end}

    async def _candles_call(self, params: dict[str, Any]) -> dict[date, dict[str, float | None]]:
        return parse_candles(await self._get("/historical/candles", params))

    async def recent_closes(self, row: dict[str, str], *, cached_only: bool = False) -> dict[date, float]:
        """The last ~two weeks of daily closes, cached until they can change (closes_ttl: the next 16:00 or the next
        session's open) in memory and on disk, so a weekend or a restart asks Groww nothing."""
        gs = groww_symbol(row)
        now = self.clock()
        hit = self._closes.get(gs) or _disk_closes().get(gs)
        if hit is not None and (hit[0] > self._now_s() or cached_only):
            self._closes[gs] = hit
            return hit[1]
        if cached_only:
            return {}
        today = to_ist(now).date()
        got = {d: c["close"] for d, c in (await self.candles(row, today - timedelta(days=14), today)).items()}
        ph = phase(now, self.holidays())
        ttl = closes_ttl(now, self.holidays())
        if ph == "after" and today not in got:
            ttl = RETRY_TTL_S  # Groww has not added today's candle yet
        entry = (self._now_s() + ttl, got)
        self._closes[gs] = entry
        _save_disk_closes(gs, entry)
        return got

    # ---- quotes for valuation
    async def quotes(
        self, items: list[tuple[str, str]], *, cached_only: bool = False
    ) -> dict[tuple[str, str], Any]:
        """{(symbol, exchange): GrowwQuote} for the stocks Groww can price now; the rest are absent (fall back).

        In session (09:15-16:00 IST): the batched LTP is the last traded price, and the previous close comes from the
        cached recent closes (for the day change; unknown when they could not be read). After 16:00 and on closed days:
        the latest daily close, labelled as Groww's daily close. Before 16:00 the day's close is never taken from a
        candle (it is still forming)."""
        if not items or not self.token():
            return {}
        inst = await self.instruments()
        if inst is None:
            return {}
        rows = {it: r for it in dict.fromkeys(items) if (r := inst.stock(it[0], it[1])) is not None}
        if not rows:
            return {}
        now = self.clock()
        ph = phase(now, self.holidays())
        today = to_ist(now).date()
        prices = await self.ltp([exchange_symbol(r) for r in rows.values()], cached_only=cached_only) \
            if ph in ("open", "closing") else {}  # fmt: skip

        async def closes(r: dict[str, str]) -> dict[date, float]:
            try:
                return await self.recent_closes(r, cached_only=cached_only)
            except Exception as e:
                log.info("Groww closes failed (%s)", type(e).__name__)
                return {}

        sem = asyncio.Semaphore(4)

        async def one(it: tuple[str, str], r: dict[str, str]) -> tuple[tuple[str, str], Any]:
            async with sem:
                cl = await closes(r)
            before = sorted(d for d in cl if d < today)
            prev = cl[before[-1]] if before else None
            if ph in ("open", "closing"):
                got = prices.get(exchange_symbol(r))
                if got is None:
                    return it, None
                return it, GrowwQuote(exchange=it[1], symbol=it[0], last_price=got[0], previous_close=prev,
                                      as_of=to_ist(got[1]), provenance=LTP_PROVENANCE)  # fmt: skip
            days = sorted(d for d in cl if d <= today)
            if not days:
                return it, None
            last = days[-1]
            prior = cl[days[-2]] if len(days) > 1 else None
            return it, GrowwQuote(exchange=it[1], symbol=it[0], close_price=cl[last], previous_close=prior,
                                  as_of=datetime.combine(last, CLOSE, IST), provenance=CLOSE_PROVENANCE)  # fmt: skip

        got = await asyncio.gather(*(one(it, r) for it, r in rows.items()))
        if any(q is not None for _, q in got):
            self._mark("prices")
        return {it: q for it, q in got if q is not None}

    async def live_value(self, kind: str, name: str) -> tuple[float, datetime] | None:
        """In session only: an index's value (kind "index", e.g. "NIFTY 50") or a stock's LTP (an NSE symbol or
        "BSE:<scrip code>"), with when Groww answered; None when Groww cannot say (the caller keeps its own source)."""
        if not self.token() or phase(self.clock(), self.holidays()) not in ("open", "closing"):
            return None
        try:
            inst = await self.instruments()
            if inst is None:
                return None
            if kind == "index":
                row = inst.index(name)
            else:
                ex, _, code = name.partition(":") if name.startswith("BSE:") else ("NSE", "", name)
                row = inst.stock(code, ex)
            if row is None:
                return None
            got = (await self.ltp([exchange_symbol(row)])).get(exchange_symbol(row))
        except Exception as e:
            log.info("Groww live value for %s failed (%s)", name, type(e).__name__)
            return None
        if got is not None:
            self._mark("indices" if kind == "index" else "prices")
        return got

    async def history_closes(
        self, exchange: str, symbol: str, start: date, end: date
    ) -> dict[date, float] | None:
        """Daily closes for [start, end] (from 2020), None when Groww is not connected or does not list the stock;
        raises ConnectorError when Groww fails. Today's candle is left out until 16:00 IST (it is still forming)."""
        if not self.token():
            return None
        inst = await self.instruments()
        row = inst.stock(symbol, exchange) if inst is not None else None
        if row is None:
            return None
        got = await self.candles(row, start, end)
        now = self.clock()
        today = to_ist(now).date()
        forming = phase(now, self.holidays()) in ("pre", "open", "closing")
        return {
            d: float(c["close"])
            for d, c in got.items()
            if c["close"] is not None and not (forming and d >= today)
        }

    # ---- option chain
    async def option_chain(self, symbol: str, expiry: date, exchange: str = "NSE") -> Any:
        """The chain as `adapters.nse_fno.OptionChain` (source Groww): per strike LTP, IV, OI and volume. Change in OI,
        bid and ask are not in Groww's chain and stay unknown. Cached CHAIN_TTL_S in session, else until the next
        open. Raises ConnectorError when Groww cannot answer."""
        from decimal import Decimal

        from finresearch.adapters.nse_fno import ChainRow, OptionChain, OptionQuote

        sym = symbol.strip().upper()
        key = (sym, expiry)
        hit = self._chains.get(key)
        if hit is not None and hit[0] > self._now_s():
            return hit[1]
        now = self.clock()
        payload = await self._once(("chain", exchange, sym, expiry),
                                   lambda: self._get(f"/option-chain/exchange/{exchange}/underlying/{sym}",
                                                     {"expiry_date": expiry.isoformat()}))  # fmt: skip
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("strikes"), dict)
            or not payload["strikes"]
        ):
            raise ConnectorError(f"Groww has no option chain for {sym} {expiry}")

        def dec(v: Any) -> Decimal | None:
            return Decimal(str(v)) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        def side(d: Any) -> OptionQuote | None:
            if not isinstance(d, dict):
                return None
            g = d.get("greeks") if isinstance(d.get("greeks"), dict) else {}
            return OptionQuote(last_price=dec(d.get("ltp")), iv=dec(g.get("iv")), oi=dec(d.get("open_interest")),
                               change_in_oi=None, volume=dec(d.get("volume")), bid=None, ask=None)  # fmt: skip

        rows = []
        for strike, legs in payload["strikes"].items():
            try:
                k = Decimal(str(strike))
            except Exception:
                continue
            legs = legs if isinstance(legs, dict) else {}
            rows.append(ChainRow(strike=k, call=side(legs.get("CE")), put=side(legs.get("PE"))))
        chain = OptionChain(symbol=sym, expiry=expiry, underlying=dec(payload.get("underlying_ltp")),
                            as_of=to_ist(now), rows=sorted(rows, key=lambda r: r.strike))  # fmt: skip
        ph = phase(now, self.holidays())
        ttl = (
            CHAIN_TTL_S
            if ph in ("open", "closing")
            else (next_open(now, self.holidays()) - to_ist(now)).total_seconds()
        )
        self._chains[key] = (self._now_s() + ttl, chain)
        self._mark("option_chain")
        return chain

    # ---- status for the settings card
    def status(self) -> dict[str, Any]:
        from finresearch.adapters.groww_budget import BUDGET

        tok = self.token()
        return {"active": bool(tok), "session": self.session.state, "phase": phase(self.clock(), self.holidays()),
                "supplies": SUPPLIES, "last_answered": dict(self.supplied), "calls_today": BUDGET.today(),
                "close_check": close_check_stats(),
                "instruments_day": self._instruments.day.isoformat() if self._instruments and self._instruments.day
                else None}  # fmt: skip


SUPPLIES = [
    {
        "kind": "prices",
        "label": "Portfolio stock prices in session (live LTP); after it the exchange's official "
        "close, Groww's daily close only when that is not out",
    },
    {"kind": "indices", "label": "Index values on the market charts (in session)"},
    {"kind": "candles", "label": "Daily price history for performance, risk and charts (from 2020)"},
    {"kind": "option_chain", "label": "F&O option chains with IV"},
    {"kind": "mutual_funds", "label": "Not supplied: mutual-fund NAVs stay on AMFI (Groww has no fund data)"},
]


class GrowwQuote:
    """A quote-shaped object for `fincalc.price.price_view` (the fields it reads), with Groww as its source."""

    def __init__(self, *, exchange: str, symbol: str, last_price: float | None = None, close_price: float | None = None,
                 previous_close: float | None = None, as_of: datetime | None = None, provenance: str = SOURCE) -> None:  # fmt: skip
        from decimal import Decimal

        def d(v: float | None) -> Decimal | None:
            return Decimal(str(v)) if v is not None else None

        self.exchange, self.symbol, self.provenance, self.as_of = exchange, symbol, provenance, as_of
        self.last_price, self.close_price, self.previous_close = (
            d(last_price),
            d(close_price),
            d(previous_close),
        )
        self.base_price = self.indicative_close = self.listing_date = None
        self.industry = self.market_cap = self.issued_shares = None


class Prefetch:
    """`valuation.fetch_prices`' Groww step: every stock of a valuation asked in one go (batched LTP). `cached_only`
    answers from memory and the disk cache only (the portfolio page's instant first paint), and then asks no exchange
    for the industry either (`backfill`)."""

    def __init__(self, market: GrowwMarket | None = None, *, cached_only: bool = False) -> None:
        self.market, self.cached_only, self.backfill = market, cached_only, not cached_only

    async def __call__(self, items: list[tuple[str, str]]) -> dict[tuple[str, str], Any]:
        return await (self.market or MARKET).quotes(items, cached_only=self.cached_only)

    # ---- the official close after the session (#283): remembered until the next open, compared with Groww's close
    def official(self, key: tuple[str, str]) -> Any:
        """The exchange quote whose official close was seen since the last session ended, else None."""
        m = self.market or MARKET
        hit = m.official.get(key)
        return hit[1] if hit is not None and hit[0] > m._now_s() else None

    def remember_official(self, key: tuple[str, str], quote: Any) -> None:
        m = self.market or MARKET
        m.official[key] = (next_open(m.clock(), m.holidays()).timestamp(), quote)

    async def record(self, checks: list[dict[str, Any]]) -> None:
        """Store the day's Groww-close-vs-official-close comparisons; a database failure never affects a price."""
        if not checks or self.cached_only:
            return
        try:
            await asyncio.to_thread(record_close_checks, checks)
        except Exception as e:
            log.info("Groww close check not stored (%s)", type(e).__name__)


def record_close_checks(checks: list[dict[str, Any]]) -> None:
    """Upsert one row per (day, exchange, symbol): {day, exchange, symbol, groww_close, official_close, source}."""
    from decimal import Decimal

    from sqlalchemy.dialects.postgresql import insert

    from finresearch.db import session_scope
    from finresearch.db.models import GrowwCloseCheck

    rows = {}
    for c in checks:
        g, o = Decimal(str(c["groww_close"])), Decimal(str(c["official_close"]))
        rows[(c["day"], c["exchange"], c["symbol"])] = {
            "day": c["day"], "exchange": c["exchange"], "symbol": c["symbol"][:40], "groww_close": g,
            "official_close": o, "official_source": str(c["source"])[:80],
            "matched": abs(g - o) <= Decimal(str(CLOSE_MATCH_TOL))}  # fmt: skip
    with session_scope() as s:
        stmt = insert(GrowwCloseCheck).values(list(rows.values()))
        s.execute(stmt.on_conflict_do_update(
            index_elements=["day", "exchange", "symbol"],
            set_={k: stmt.excluded[k] for k in ("groww_close", "official_close", "official_source", "matched")}))  # fmt: skip


def close_check_stats() -> dict[str, Any] | None:
    """{matched, checked, since, last_day} over every stored stock-day, or None when the database cannot say."""
    try:
        from sqlalchemy import func, select

        from finresearch.db import session_scope
        from finresearch.db.models import GrowwCloseCheck as C

        with session_scope() as s:
            n, ok, lo, hi = s.execute(select(func.count(), func.count().filter(C.matched.is_(True)), func.min(C.day),
                                             func.max(C.day))).one()  # fmt: skip
    except Exception:
        return None
    return {"checked": int(n), "matched": int(ok), "since": lo.isoformat() if lo else None,
            "last_day": hi.isoformat() if hi else None, "tolerance_inr": CLOSE_MATCH_TOL}  # fmt: skip


def _count(cat: str) -> None:
    try:
        from finresearch.adapters.groww_budget import BUDGET

        BUDGET.count(cat)
    except Exception:
        pass


def _disk_closes() -> dict[str, tuple[float, dict[date, float]]]:
    try:
        raw = json.loads((_dir() / "closes.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for k, v in raw.items() if isinstance(raw, dict) else []:
        try:
            out[k] = (float(v["until"]), {date.fromisoformat(d): float(c) for d, c in v["closes"].items()})
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _save_disk_closes(key: str, entry: tuple[float, dict[date, float]]) -> None:
    try:
        d = _dir()
        d.mkdir(parents=True, exist_ok=True)
        lock = os.open(d / "closes.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            p = d / "closes.json"
            try:
                raw = json.loads(p.read_text())
            except (OSError, ValueError):
                raw = {}
            raw[key] = {"until": entry[0], "closes": {x.isoformat(): c for x, c in sorted(entry[1].items())}}
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(raw))
            tmp.replace(p)
        finally:
            os.close(lock)
    except OSError as e:
        log.debug("Groww closes not saved: %s", e)


MARKET = GrowwMarket()  # the process's client (tests replace it)
