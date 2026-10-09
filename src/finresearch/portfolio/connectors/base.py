"""The read-only broker connector contract and the HTTP layer that enforces it.

A connector reads the user's own account: holdings, open positions, executed trades, mutual-fund holdings and a funds
summary. It can never place, modify or cancel anything:

* No vendor SDK is used (growwapi, kiteconnect, upstox-python-sdk, dhanhq and smartapi-python all ship order
  methods). Each connector talks to the broker's REST API through `ReadOnlyHttp`.
* `ReadOnlyHttp` sends GET only to paths in the connector's `READ_PATHS` allowlist, and a POST only to the
  authentication paths in `AUTH_PATHS` (turning a login/TOTP/authorisation code into an access token). Any other
  method or path raises `ForbiddenRequest` before a byte leaves the machine.
* No connector class has an order-shaped attribute (tests introspect every class for place/modify/cancel/order/gtt).

Secrets (API secret, TOTP seed, access token, PIN) are passed in by `store` from the local database and are redacted
from every error message this module raises (`redact`). Nothing here logs request or response bodies.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx

# test seam: an httpx transport (httpx.MockTransport with recorded responses); None = the network
TRANSPORT: httpx.AsyncBaseTransport | None = None
TIMEOUT_S = 20.0

# names no connector may expose (read-only enforcement is tested against this)
FORBIDDEN_ATTR = re.compile(r"(place|modify|cancel|exit_|convert|gtt|basket|amo|order(?!_id))", re.I)


class ConnectorError(RuntimeError):
    """A failure with a message that is safe to store and show (secrets redacted)."""


class ReconnectNeeded(ConnectorError):
    """The access token is missing or expired and the user has to log in again (or a headless login failed)."""


class ForbiddenRequest(ConnectorError):
    """A request outside the read-only allowlist: a programming error, never sent."""


@dataclass
class FieldSpec:
    """One setting the user enters in Profile → Connections."""

    name: str
    label: str
    secret: bool = False
    required: bool = True
    help: str = ""


@dataclass
class TokenGrant:
    token: str
    expires_at: datetime | None = None
    extra: dict[str, Any] = field(
        default_factory=dict
    )  # non-secret facts (user id shown as "connected as …")


@dataclass
class BrokerHolding:
    """A delivery holding (demat) or a fund holding as the broker reports it."""

    name: str
    quantity: Decimal
    isin: str | None = None
    symbol: str | None = None  # NSE trading symbol when known
    exchange: str | None = None
    bse_code: str | None = None
    avg_price: Decimal | None = None
    last_price: Decimal | None = None
    asset_type: str = "stock"  # stock | mf
    scheme_code: str | None = None
    pledged: Decimal | None = None
    t1_quantity: Decimal | None = None  # bought, not yet delivered (T+1): included in quantity
    raw_keys: tuple[str, ...] = ()  # which broker fields were present (for the mapping tests)


@dataclass
class BrokerPosition:
    """An open (intraday or carry-forward) position: shown, never turned into lots."""

    symbol: str
    quantity: Decimal
    product: str | None = None
    exchange: str | None = None
    avg_price: Decimal | None = None
    last_price: Decimal | None = None
    pnl: Decimal | None = None
    segment: str | None = None


@dataclass
class BrokerTrade:
    """One executed equity fill (or an executed order when the broker reports orders, not fills)."""

    day: date
    side: str  # buy | sell
    quantity: Decimal
    price: Decimal
    name: str
    isin: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    trade_id: str | None = None
    order_id: str | None = None  # the exchange order id when the broker gives it, else the broker's
    product: str | None = None  # CNC/DELIVERY vs MIS/INTRADAY
    segment: str | None = None
    executed_at: str | None = None


class ReadOnlyHttp:
    """GET-only client with a per-connector path allowlist; POST only to the authentication paths."""

    def __init__(self, base_url: str, *, read_paths: tuple[str, ...], auth_paths: tuple[str, ...] = (),
                 headers: dict[str, str] | None = None, secrets: tuple[str, ...] = ()) -> None:  # fmt: skip
        self.base_url = base_url.rstrip("/")
        self._read = [re.compile(p) for p in read_paths]
        self._auth = [re.compile(p) for p in auth_paths]
        self.headers = dict(headers or {})
        self.secrets = tuple(s for s in secrets if s)

    def _check(self, method: str, path: str) -> None:
        allowed = self._read if method == "GET" else self._auth if method == "POST" else []
        if not any(p.fullmatch(path) for p in allowed):
            raise ForbiddenRequest(f"{method} {path} is not an allowed read-only request")

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._send("GET", path, params=params)

    async def auth_post(self, path: str, *, json: Any = None, data: Any = None, params: dict[str, Any] | None = None,
                        headers: dict[str, str] | None = None) -> Any:  # fmt: skip
        return await self._send("POST", path, json=json, data=data, params=params, headers=headers)

    async def _send(self, method: str, path: str, **kw: Any) -> Any:
        self._check(method, path)
        extra = kw.pop("headers", None) or {}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=TRANSPORT, follow_redirects=False) as c:
                r = await c.request(method, self.base_url + path, headers={**self.headers, **extra}, **kw)
        except httpx.HTTPError as e:
            raise ConnectorError(self.redact(f"network error: {type(e).__name__}")) from None
        if r.status_code in (401, 403):
            raise ReconnectNeeded(self.redact(f"the broker refused the session (HTTP {r.status_code}): "
                                              f"{_brief(r)}"))  # fmt: skip
        if r.status_code >= 400:
            raise ConnectorError(self.redact(f"HTTP {r.status_code} from {path}: {_brief(r)}"))
        try:
            return r.json()
        except ValueError:
            raise ConnectorError(f"{path} did not return JSON") from None

    def redact(self, text: str) -> str:
        return redact(text, *self.secrets, *(v for k, v in self.headers.items() if _secret_header(k)))


def _secret_header(name: str) -> bool:
    return name.lower() in ("authorization", "access-token", "x-privatekey", "api-key", "x-api-key")


def _brief(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(body, dict):
        for k in ("message", "errorMessage", "error_message", "error", "errors", "remarks"):
            if body.get(k):
                return str(body[k])[:200]
    return str(body)[:200]


def redact(text: str, *secret_values: str) -> str:
    for s in secret_values:
        if s and len(s) >= 4:
            text = text.replace(s, "[redacted]")
        if s and s.lower().startswith(("bearer ", "token ")):
            text = text.replace(s.split(" ", 1)[1], "[redacted]")
    return text


def dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).replace(",", ""))
    except Exception:
        return None


class BrokerConnector(ABC):
    """One broker. Subclasses declare their settings, capabilities, allowlisted paths, and implement the reads."""

    key: ClassVar[str]
    label: ClassVar[str]
    account: ClassVar[str]  # the portfolio account label; the same as the broker's tradebook importer uses
    auth_kind: ClassVar[
        str
    ]  # totp (headless) | oauth (browser login each day) | token (pasted) | password_totp
    capabilities: ClassVar[frozenset[str]]  # holdings | positions | trades | mf | funds
    fields: ClassVar[tuple[FieldSpec, ...]]
    docs_url: ClassVar[str]
    cost: ClassVar[str]
    token_note: ClassVar[str]  # how long a session lasts, in plain words
    trades_note: ClassVar[str]  # how far back the trade history goes
    verified: ClassVar[str] = (
        "docs"  # "docs" (official documentation) | "partial" (some fields unverified [U])
    )
    # False: today's delivery buys show in positions until tomorrow
    holdings_include_today: ClassVar[bool] = True
    first_sync_days: ClassVar[int] = 30  # trade history requested on the first sync (clamped by the broker)
    # the trades endpoint only ever answers today's orders (Groww): a failed read cannot be made up on a later day,
    # so sync retries it the same day (sync.TRADES_RETRIES) and never claims coverage of earlier days
    trades_today_only: ClassVar[bool] = False
    # one BrokerTrade per *order* (its filled quantity and average fill price so far), keyed by the broker's order
    # id: a re-read of the same order with more fills updates the stored row in place (merge.upsert_orders)
    fills_aggregated: ClassVar[bool] = False

    def __init__(self, config: dict[str, Any], token: str | None = None) -> None:
        self.config = dict(config or {})
        self.token = token

    @property
    def source(self) -> str:
        return f"{self.key}_api"

    def secret_values(self) -> tuple[str, ...]:
        return (*(str(self.config.get(f.name) or "") for f in self.fields if f.secret), self.token or "")

    # --- authentication (only the auth paths may be POSTed to)
    def login_url(self, redirect_uri: str, state: str) -> str | None:
        """The broker's login page for a browser (OAuth-style) connector; None for headless ones."""
        return None

    async def login(self) -> TokenGrant:
        """A headless login (TOTP/API secret). Browser connectors raise ReconnectNeeded with the next step."""
        raise ReconnectNeeded("log in through the broker's page (Reconnect)")

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenGrant:
        raise ConnectorError(f"{self.label} has no browser login")

    # --- reads
    @abstractmethod
    async def holdings(self) -> list[BrokerHolding]: ...

    async def positions(self) -> list[BrokerPosition]:
        return []

    async def trades(self, since: date, until: date) -> list[BrokerTrade]:
        return []

    async def mf_holdings(self) -> list[BrokerHolding]:
        return []

    async def funds(self) -> dict[str, Any]:
        return {}

    def describe(self) -> dict[str, Any]:
        return {"key": self.key, "label": self.label, "account": self.account, "auth_kind": self.auth_kind,
                "capabilities": sorted(self.capabilities), "docs_url": self.docs_url, "cost": self.cost,
                "token_note": self.token_note, "trades_note": self.trades_note, "verified": self.verified,
                "fields": [{"name": f.name, "label": f.label, "secret": f.secret, "required": f.required,
                            "help": f.help} for f in self.fields]}  # fmt: skip


def ist_day_end(d: date) -> datetime:
    """A token that "expires at the end of the day" as an aware datetime (IST 23:59)."""
    from datetime import time

    from finresearch.fincalc.dates import IST

    return datetime.combine(d, time(23, 59), tzinfo=IST)
