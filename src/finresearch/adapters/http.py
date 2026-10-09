"""Polite async HTTP client shared by every exchange/regulator adapter.

Why a wrapper instead of bare httpx:
- NSE and SEBI block or throttle obvious bots, so every request carries browser-like headers and a
  per-host rate limit (NSE ≤ 2 req/s, everyone else ≤ 1 req/s by default), shared by every client in every
  FinResearch process on the Mac (`SharedSlots`), not kept per client.
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
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

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


class SharedSlots:
    """Request slots per host shared by every client in this process and by every FinResearch process on the Mac
    (the API with its monitor, `monitor run`, research workers and their MCP servers), issue #246.

    Each request reserves the next free slot: under a thread lock and an exclusive `flock` on
    `<state_dir>/ratelimit/<host>.slot`, read the host's next free time (wall clock, comparable across processes),
    take max(now, it) and store that plus the interval. The caller then sleeps until its slot outside every lock, so
    nothing is held across an await and threads with their own event loops share it safely. Before #246 each client
    kept its own spacing, so ~14 live NSE clients reached ~28 req/s against the intended 2.

    If the state dir cannot be written, the slots are kept in this process only (logged once).
    """

    MAX_AHEAD_S = (
        600.0  # a stored slot further ahead than this is a clock jump or a corrupt file: start again
    )

    def __init__(self, directory: Path | Callable[[], Path] | None = None, *,
                 clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        self._directory = directory
        self._clock = clock
        self._mutex = threading.Lock()
        self._memory: dict[str, float] = {}
        self._warned = False

    def _dir(self) -> Path:
        if callable(self._directory):
            return self._directory()
        if self._directory is not None:
            return self._directory
        from finresearch.config import get_settings

        return Path(get_settings().state_dir) / "ratelimit"

    def _update(self, key: str, fn: Callable[[float, float], tuple[float, float]]) -> float:
        """Apply fn(now, next_free) -> (result, new next_free) atomically for `key`; return the result."""
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)[:120]
        with self._mutex:
            now = self._clock()
            try:
                d = self._dir()
                d.mkdir(parents=True, exist_ok=True)
                fd = os.open(d / f"{safe}.slot", os.O_RDWR | os.O_CREAT, 0o600)
            except OSError as e:
                if not self._warned:
                    self._warned = True
                    logging.getLogger(__name__).warning("rate limit kept per process only: %s", e)
                nxt = self._memory.get(key, 0.0)
                out, self._memory[key] = fn(now, nxt if nxt - now <= self.MAX_AHEAD_S else 0.0)
                return out
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                raw = os.pread(fd, 64, 0)
                try:
                    nxt = float(raw.decode() or 0)
                except ValueError:
                    nxt = 0.0
                if nxt - now > self.MAX_AHEAD_S:
                    nxt = 0.0
                out, new = fn(now, nxt)
                data = repr(new).encode()
                os.ftruncate(fd, 0)
                os.pwrite(fd, data, 0)
                return out
            finally:
                os.close(fd)  # also releases the flock

    def reserve(self, key: str, interval: float) -> float:
        """Reserve the next slot for `key`; returns how long to wait for it (0 = now)."""

        def take(now: float, nxt: float) -> tuple[float, float]:
            slot = max(now, nxt)
            return slot - now, slot + interval

        return self._update(key, take)

    def hold_off(self, key: str, seconds: float) -> None:
        """Push the host's next slot `seconds` from now (a 429's Retry-After), for every client."""
        self._update(key, lambda now, nxt: (0.0, max(nxt, now + seconds)))


SHARED_SLOTS = SharedSlots()  # the process-wide (and cross-process) default


class HostRateLimiter:
    """Minimum spacing between requests to the same host.

    By default the spacing is shared by every client (`SharedSlots`): a host configured by suffix
    ("nseindia.com") is one budget for www., archives. and the rest. A test that injects its own `clock` gets a private
    limiter with the old per-instance behaviour (one asyncio lock per host serialises "wait then stamp").
    """

    def __init__(
        self,
        rates: Mapping[str, float] | None = None,
        default_rate: float = DEFAULT_RATE,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] | None = None,
        slots: SharedSlots | None = None,
    ) -> None:
        self._rates = dict(DEFAULT_HOST_RATES if rates is None else rates)
        self._default = default_rate
        self._sleep = sleep
        self._shared = slots if slots is not None else (SHARED_SLOTS if clock is None else None)
        self._clock = clock or time.monotonic
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def rate_for(self, host: str) -> float:
        return self._match(host)[1]

    def _match(self, host: str) -> tuple[str, float]:
        """(the key the spacing is kept under, requests per second)."""
        host = host.lower()
        for suffix, rate in self._rates.items():
            if host == suffix or host.endswith("." + suffix):
                return suffix, rate
        return host, self._default

    async def wait(self, host: str) -> None:
        key, rate = self._match(host)
        if rate <= 0:
            return
        interval = 1.0 / rate
        if self._shared is not None:
            delay = self._shared.reserve(key, interval)
            if delay > 0:
                await self._sleep(delay)
            return
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last.get(host)
            if last is not None:
                delay = interval - (self._clock() - last)
                if delay > 0:
                    await self._sleep(delay)
            self._last[host] = self._clock()

    def hold_off(self, host: str, seconds: float) -> None:
        if self._shared is not None and seconds > 0:
            self._shared.hold_off(self._match(host)[0], seconds)


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


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Whether `ip` is a routable public unicast address. IPv4 inside IPv6 (mapped ::ffff:a.b.c.d, 6to4 2002::/16,
    Teredo) is judged by the IPv4 address it carries. `is_global` is False for private, loopback, link-local,
    unspecified, reserved, documentation and shared/CGNAT (100.64.0.0/10, RFC 6598) space; multicast is checked too
    because some multicast ranges count as global."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if (
            inner is None and ip in _NAT64
        ):  # 64:ff9b::/96 (RFC 6052) carries the IPv4 address in its last 32 bits
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if inner is not None:  # judged by the carried address alone: older Pythons call all of ::ffff:0:0/96 reserved
            return is_public_ip(inner)
    return ip.is_global and not (ip.is_multicast or ip.is_loopback or ip.is_link_local or ip.is_unspecified
                                 or ip.is_reserved or ip.is_private)  # fmt: skip


def check_public_url(url: str, *, resolve: Callable[..., list] | None = None) -> str:
    """Defence in depth against SSRF (CodeQL py/full-ssrf): this client only talks to public data sources, so it refuses
    other schemes, `localhost` names and private, loopback, link-local or reserved IP literals. Callers that take a URL
    from the user still allow-list hosts first (adapters.amc_portfolio.check_url, ingest.documents).

    `resolve` (socket.getaddrinfo or a fake): also resolve a host name and refuse it unless EVERY address it resolves
    to is public (#259); a name that does not resolve is refused too (fail closed). Used for agent-chosen URLs
    (fetch_page), on every redirect hop. Without it a name is not resolved (the exchange adapters' fixed hosts)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise UnsafeURLError(f"refusing a non-http(s) URL: {parts.scheme or '?'}://")
    if host == "localhost" or host.endswith(".localhost"):
        raise UnsafeURLError("refusing a URL on this machine (localhost)")
    try:
        addrs = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        if resolve is None:
            return url
        if host.endswith((".local", ".internal", ".home.arpa", ".lan")):
            raise UnsafeURLError(f"refusing a local network name ({host})") from None
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            infos = resolve(host, port)
            addrs = [ipaddress.ip_address(str(ai[4][0]).split("%")[0]) for ai in infos]
        except (OSError, UnicodeError, ValueError) as e:
            raise UnsafeURLError(
                f"could not resolve {host} to check that it is public ({type(e).__name__})"
            ) from e
        if not addrs:
            raise UnsafeURLError(f"{host} resolved to no address") from None
    for ip in addrs:
        if not is_public_ip(ip):
            raise UnsafeURLError(
                f"refusing a URL on a private, loopback or reserved address ({host} -> {ip})"
            )
    return url


async def _check_request(request: httpx.Request) -> None:
    check_public_url(str(request.url))


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
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] = now_ist,
        slots: SharedSlots | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={**BROWSER_HEADERS, **(headers or {})},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            # every request, redirects included, is checked: an official host redirecting to 127.0.0.1 or a private
            # address is refused (the agents' MCP tools fetch through this client)
            event_hooks={"request": [_check_request]},
        )
        # clock=None (production): the process-wide, cross-process limiter; a test clock gets a private one
        self._limiter = HostRateLimiter(host_rates, default_rate, sleep=sleep, clock=clock, slots=slots)
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
            if resp.status_code == 429:  # every client of this host backs off, not just this one
                self._limiter.hold_off(host, self._backoff(attempt + 1, resp))
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


# --------------------------------------------------------------------------- agent-chosen URLs (fetch_page, #259)
MAX_PUBLIC_BYTES = (
    5 * 1024 * 1024
)  # a web page an agent reads; an RHP or annual report goes through ingest.documents
MAX_PUBLIC_REDIRECTS = 5


class ResponseTooLarge(ValueError):
    """The body passed the size cap; nothing of it is returned."""


async def fetch_public(url: str, *, max_bytes: int = MAX_PUBLIC_BYTES, max_redirects: int = MAX_PUBLIC_REDIRECTS,
                       resolve: Callable[..., list] | None = None, timeout: float = 20.0,
                       transport: httpx.AsyncBaseTransport | None = None,
                       limiter: HostRateLimiter | None = None) -> Fetched:  # fmt: skip
    """GET a URL an agent chose, failing closed (#259): before each request (the first and every redirect hop, at most
    `max_redirects`) the host name is resolved and every address must be public (check_public_url with `resolve`);
    redirects are followed here, not by httpx, so no hop escapes the check. The body is streamed and the fetch is
    aborted once it passes `max_bytes` (decoded bytes, so a compressed bomb is caught too); a declared Content-Length
    over the cap is refused before reading. Browser headers and the shared per-host rate limit as PoliteClient.

    Left open: the name is resolved again when connecting, so a DNS server that answers public then private (DNS
    rebinding) within the TTL is not caught; that needs connecting to the checked address itself."""
    resolve = resolve or socket.getaddrinfo  # looked up per call, so a test's fake DNS reaches fetch_page
    limiter = limiter or HostRateLimiter()
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=timeout, follow_redirects=False,
                                 transport=transport) as client:  # fmt: skip
        target = url
        for _hop in range(max_redirects + 1):
            await asyncio.to_thread(check_public_url, target, resolve=resolve)
            await limiter.wait(urlsplit(target).hostname or "")
            resp = await client.send(client.build_request("GET", target), stream=True)
            if not resp.is_redirect:
                break
            await resp.aclose()
            target = urljoin(target, resp.headers.get("location", ""))
        else:
            raise UnsafeURLError(f"more than {max_redirects} redirects from {url}")
        try:
            declared = resp.headers.get("content-length", "")
            if declared.strip().isdigit() and int(declared) > max_bytes:
                raise ResponseTooLarge(
                    f"response is {int(declared):,} bytes, over the {max_bytes:,}-byte cap"
                )
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise ResponseTooLarge(f"response passed the {max_bytes:,}-byte cap; fetch aborted")
        finally:
            await resp.aclose()
    content = bytes(body)
    record = FetchRecord(url=str(resp.request.url), status=resp.status_code,
                         content_type=resp.headers.get("content-type"), fetched_at=now_ist(),
                         sha256=hashlib.sha256(content).hexdigest(), size=len(content))  # fmt: skip
    return Fetched(record=record, content=content)
