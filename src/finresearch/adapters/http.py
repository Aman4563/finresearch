"""Polite async HTTP client shared by every exchange/regulator adapter.

Why a wrapper instead of bare httpx:
- NSE and SEBI block or throttle obvious bots, so every request carries browser-like headers and a
  per-host rate limit (NSE ≤ 2 req/s, everyone else ≤ 1 req/s by default).
- Transient failures (429, 5xx, timeouts) are common on exchange sites during IPO rush hours, so they
  are retried with exponential backoff. 401/403 are *not* retried here: they mean "cookie expired",
  which only the site adapter knows how to fix (NSE re-warms its session).
- Every number in a report must cite URL + timestamp, so each real fetch produces a `FetchRecord`
  (url, status, fetched_at in IST, sha256 of the body) and can be streamed to a recorder hook.
- The on-disk cache is opt-in per call (`cache_ttl`). Live subscription figures change minute to
  minute; a default-on cache would silently serve stale bids. Cache hits keep the *original*
  `fetched_at` so provenance stays honest.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from finresearch.config import REPO_ROOT

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

DEFAULT_CACHE_DIR = REPO_ROOT / "data" / "cache" / "http"

BROWSER_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Requests per second, matched against the host by suffix (so "nseindia.com" covers www. and archives.)
DEFAULT_HOST_RATES: dict[str, float] = {"nseindia.com": 2.0}
DEFAULT_RATE = 1.0

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

# how a network or exchange failure reads in the adapters' errors ("NSE HTTP 403 for ...", "BSE returned a non-JSON
# page ...", "NSE refused ... after re-warm"). A 404 or an empty answer is the exchange saying "no data": not transient.
_TRANSIENT_MSG = re.compile(r"\bHTTP (?:401|403|408|425|429|5\d\d)\b|non-JSON|invalid JSON|\brefused\b|block page",
                            re.IGNORECASE)  # fmt: skip


def is_transient(exc: BaseException | None) -> bool:
    """Whether a failure came from the network or the exchange's gate (DNS, connection, timeout, 401/403/429/5xx, a
    block page instead of JSON), so a retry soon may well succeed. Such a failure must never be cached as data or as
    "no data". Follows `raise ... from` chains (a LookupError wrapping an NseError)."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(
            exc, (httpx.TransportError, httpx.TimeoutException, TimeoutError, ConnectionError, OSError)
        ):
            return True
        if _TRANSIENT_MSG.search(str(exc)):
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def now_ist() -> datetime:
    return datetime.now(IST)


class FetchRecord(BaseModel):
    """Provenance for one HTTP response: enough to cite it and to prove the bytes didn't change."""

    url: str
    method: str = "GET"
    params: dict[str, str] = Field(default_factory=dict)
    status: int
    content_type: str | None = None
    fetched_at: datetime  # IST (+05:30)
    sha256: str
    size: int
    from_cache: bool = False


class Fetched(BaseModel):
    """A response body plus its provenance record."""

    record: FetchRecord
    content: bytes

    @property
    def status(self) -> int:
        return self.record.status

    @property
    def ok(self) -> bool:
        return 200 <= self.record.status < 300

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


class HostRateLimiter:
    """Minimum spacing between requests to the same host.

    One lock per host serialises the "wait then stamp" step, so concurrent tasks queue politely
    instead of bursting.
    """

    def __init__(
        self,
        rates: Mapping[str, float] | None = None,
        default_rate: float = DEFAULT_RATE,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rates = dict(DEFAULT_HOST_RATES if rates is None else rates)
        self._default = default_rate
        self._sleep = sleep
        self._clock = clock
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def rate_for(self, host: str) -> float:
        host = host.lower()
        for suffix, rate in self._rates.items():
            if host == suffix or host.endswith("." + suffix):
                return rate
        return self._default

    async def wait(self, host: str) -> None:
        rate = self.rate_for(host)
        if rate <= 0:
            return
        interval = 1.0 / rate
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last.get(host)
            if last is not None:
                delay = interval - (self._clock() - last)
                if delay > 0:
                    await self._sleep(delay)
            self._last[host] = self._clock()


def cache_key(method: str, url: str, params: Mapping[str, Any] | None, data: Mapping[str, Any] | None) -> str:
    payload = json.dumps(
        {
            "m": method.upper(),
            "u": url,
            "p": sorted((str(k), str(v)) for k, v in (params or {}).items()),
            "d": sorted((str(k), str(v)) for k, v in (data or {}).items()),
        }
    )
    return hashlib.sha256(payload.encode()).hexdigest()


class UnsafeURLError(ValueError):
    """A URL the shared client refuses: not http(s), or aimed at this machine or a private network."""


def check_public_url(url: str) -> str:
    """Defence in depth against SSRF (CodeQL py/full-ssrf): this client only talks to public data sources, so it refuses
    other schemes, `localhost` names and private, loopback, link-local or reserved IP literals. Callers that take a URL
    from the user still allow-list hosts first (adapters.amc_portfolio.check_url, ingest.documents). A public name
    that resolves to a private address is not caught here (that needs resolution at connect time)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise UnsafeURLError(f"refusing a non-http(s) URL: {parts.scheme or '?'}://")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURLError("refusing a URL on this machine (localhost)")
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return url
    if not ip.is_global or ip.is_multicast:
        raise UnsafeURLError(f"refusing a URL on a private, loopback or reserved address ({ip})")
    return url


class PoliteClient:
    """Async httpx wrapper: browser headers, per-host rate limit, retries, opt-in disk cache, recorder hook.

    `sleep`/`clock`/`wall_clock` are injectable so tests run instantly and deterministically.
    """

    def __init__(
        self,
        *,
        headers: Mapping[str, str] | None = None,
        host_rates: Mapping[str, float] | None = None,
        default_rate: float = DEFAULT_RATE,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        timeout: float = 30.0,
        cache_dir: Path | None = DEFAULT_CACHE_DIR,
        on_record: Callable[[Fetched], None]
        | None = None,  # gets body + record, so raw bytes can be archived
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = now_ist,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={**BROWSER_HEADERS, **(headers or {})},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
        )
        self._limiter = HostRateLimiter(host_rates, default_rate, sleep=sleep, clock=clock)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.cache_dir = cache_dir
        self.on_record = on_record
        self._sleep = sleep
        self._wall_clock = wall_clock

    # ---- lifecycle
    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    # ---- requests
    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float | None = None,
        cache_if: Callable[[Fetched], bool] | None = None,
    ) -> Fetched:
        return await self.request(
            "GET", url, params=params, headers=headers, cache_ttl=cache_ttl, cache_if=cache_if
        )

    async def post(
        self,
        url: str,
        *,
        data: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float | None = None,
    ) -> Fetched:
        """`content`: a raw body (e.g. JSON) instead of form `data`; such a POST is never cached."""
        return await self.request(
            "POST", url, data=data, content=content, headers=headers, cache_ttl=None if content else cache_ttl
        )

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        cache_ttl: float | None = None,
        cache_if: Callable[[Fetched], bool] | None = None,
    ) -> Fetched:
        """Fetch with rate limiting and retries. Non-2xx responses are returned, not raised.

        `cache_if` vets a 2xx body before it is written to the cache: exchanges serve block pages and
        not-yet-published placeholders with HTTP 200, and caching one would serve it for the whole TTL.

        An exception is raised only when every attempt failed at the transport level (timeout,
        connection reset); a final 429/5xx is returned so the caller can decide.
        """
        check_public_url(url)
        key = cache_key(method, url, params, data) if cache_ttl is not None else None
        if key is not None and (hit := self._cache_read(key, cache_ttl or 0)) is not None:
            return hit

        host = urlsplit(url).hostname or ""
        last_exc: Exception | None = None
        resp: httpx.Response | None = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                await self._sleep(self._backoff(attempt, resp))
            await self._limiter.wait(host)
            try:
                resp = await self._client.request(
                    method, url, params=params, data=data, content=content, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc, resp = exc, None
                continue
            if resp.status_code not in RETRY_STATUSES:
                break
        if resp is None:
            assert last_exc is not None
            raise last_exc

        fetched = self._to_fetched(method, resp, params)
        if self.on_record is not None:
            self.on_record(fetched)
        if key is not None and fetched.ok and (cache_if is None or cache_if(fetched)):
            self._cache_write(key, fetched, cache_ttl)
        return fetched

    def _backoff(self, attempt: int, resp: httpx.Response | None) -> float:
        if resp is not None and resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.strip().isdigit():
                return min(float(retry_after), self.backoff_max)
        return min(self.backoff_base * (2 ** (attempt - 1)), self.backoff_max)

    def _to_fetched(self, method: str, resp: httpx.Response, params: Mapping[str, Any] | None) -> Fetched:
        body = resp.content
        record = FetchRecord(
            url=str(resp.request.url) if resp.request else str(resp.url),
            method=method.upper(),
            params={str(k): str(v) for k, v in (params or {}).items()},
            status=resp.status_code,
            content_type=resp.headers.get("content-type"),
            fetched_at=self._wall_clock().astimezone(IST),
            sha256=hashlib.sha256(body).hexdigest(),
            size=len(body),
        )
        return Fetched(record=record, content=body)

    # ---- cache (meta JSON + raw body side by side; bodies may be JSON, HTML or PDF)
    def _cache_paths(self, key: str) -> tuple[Path, Path] | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{key}.meta.json", self.cache_dir / f"{key}.body"

    def _cache_read(self, key: str, ttl: float) -> Fetched | None:
        paths = self._cache_paths(key)
        if paths is None or not paths[0].exists() or not paths[1].exists():
            return None
        try:
            record = FetchRecord.model_validate_json(paths[0].read_text())
        except ValueError:
            return None
        age = (self._wall_clock() - record.fetched_at).total_seconds()
        if age > ttl:
            return None
        body = paths[1].read_bytes()
        if hashlib.sha256(body).hexdigest() != record.sha256:
            return None  # corrupted / partially written; refetch rather than trust it
        return Fetched(record=record.model_copy(update={"from_cache": True}), content=body)

    def _cache_write(self, key: str, fetched: Fetched, ttl: float | None = None) -> None:
        paths = self._cache_paths(key)
        if paths is None:
            return
        paths[0].parent.mkdir(parents=True, exist_ok=True)
        paths[1].write_bytes(fetched.content)
        meta = fetched.record.model_dump(mode="json")
        meta["cache_ttl_s"] = ttl  # for the weekly prune (monitor.retention); reads still check their own TTL
        paths[0].write_text(json.dumps(meta, indent=1))
