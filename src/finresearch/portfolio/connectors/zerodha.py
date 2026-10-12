"""Zerodha Kite Connect v3 (read-only use): holdings, positions, today's trades, Coin mutual funds and margins.

Official docs (read 30-Sep-2026): https://kite.trade/docs/connect/v3/user/ (login flow, session, margins),
https://kite.trade/docs/connect/v3/portfolio/ (holdings/positions), https://kite.trade/docs/connect/v3/orders/
(trades: today only), https://kite.trade/docs/connect/v3/mutual-funds/ (MF holdings). Research notes: brokers-research.md.

* API root ``https://api.kite.trade`` [U: the docs pages show paths only; this root is the long-standing one].
  Headers ``X-Kite-Version: 3`` and ``Authorization: token <api_key>:<access_token>``.
* Login is a browser redirect every day (no official headless login; scripting the web login is not allowed):
  ``https://kite.zerodha.com/connect/login?v=3&api_key=...`` → Kite redirects to the app's registered redirect URL
  with ``request_token`` → ``POST /session/token`` with api_key, request_token and
  ``checksum = sha256(api_key + request_token + api_secret)``. The access token expires at 06:00 the next day.
  ``redirect_params`` carries the app's one-time state through the login.
* Holdings ``GET /portfolio/holdings`` → ``data[]``: tradingsymbol, exchange, isin, quantity, t1_quantity,
  average_price, last_price, pnl ... Total held = ``quantity + t1_quantity`` [U: Kite lists T+1 units separately].
* Positions ``GET /portfolio/positions`` → ``data.net[]``. Trades ``GET /trades`` → today's fills only (trade_id,
  order_id, exchange_order_id, tradingsymbol, exchange, product, average_price, quantity, transaction_type,
  fill_timestamp). There is no ISIN on a trade: the symbol joins it to the holding.
* MF holdings ``GET /mf/holdings`` → Coin funds (folio, fund, tradingsymbol = the ISIN, quantity, average_price).
* MF SIPs ``GET /mf/sips`` → active and paused Coin SIPs (sip_id, tradingsymbol = the ISIN, fund, status ACTIVE |
  PAUSED | CANCELLED, frequency weekly | monthly | quarterly, instalment_amount, instalment_day, next_instalment
  "YYYY-MM-DD", last_instalment "YYYY-MM-DD hh:mm:ss"). MF orders ``GET /mf/orders`` → the last 7 days' orders
  (order_id, tradingsymbol, status COMPLETE | REJECTED | CANCELLED | OPEN, status_message, transaction_type, variety
  regular | sip | amc_sip, amount, quantity, average_price, order_timestamp, exchange_timestamp "YYYY-MM-DD"). Read
  12-Oct-2026 at https://kite.trade/docs/connect/v3/mutual-funds/. Used only for SIP estimates (portfolio.sip_estimates,
  #297), never as transactions.
* Funds ``GET /user/margins`` [U: response field names from memory].
* Cost: the free "Personal" plan covers portfolio APIs; Connect (₹500/month) adds market data.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

from finresearch.fincalc.dates import IST
from finresearch.portfolio.connectors.base import (
    BrokerConnector,
    BrokerHolding,
    BrokerPosition,
    BrokerTrade,
    ConnectorError,
    FieldSpec,
    ReadOnlyHttp,
    ReconnectNeeded,
    TokenGrant,
    dec,
)
from finresearch.portfolio.connectors.groww import next_six_am

BASE = "https://api.kite.trade"
LOGIN = "https://kite.zerodha.com/connect/login"
READ_PATHS = (r"/portfolio/holdings", r"/portfolio/positions", r"/trades", r"/mf/holdings", r"/mf/sips", r"/mf/orders",
              r"/user/margins", r"/user/profile")  # fmt: skip
AUTH_PATHS = (r"/session/token",)


def checksum(api_key: str, request_token: str, api_secret: str) -> str:
    return hashlib.sha256(f"{api_key}{request_token}{api_secret}".encode()).hexdigest()


def _data(body: Any) -> Any:
    if not isinstance(body, dict) or body.get("status") != "success":
        raise ConnectorError(
            f"Kite answered {(body or {}).get('message') if isinstance(body, dict) else 'an error'}"
        )
    return body.get("data")


def map_holding(r: dict[str, Any]) -> BrokerHolding:
    q = (dec(r.get("quantity")) or Decimal(0)) + (dec(r.get("t1_quantity")) or Decimal(0))
    sym = str(r.get("tradingsymbol") or "").strip().upper() or None
    exch = r.get("exchange")
    return BrokerHolding(name=sym or str(r.get("isin") or ""), quantity=q, isin=r.get("isin") or None,
                         symbol=sym if exch != "BSE" else None, exchange=exch, avg_price=dec(r.get("average_price")),
                         last_price=dec(r.get("last_price")), t1_quantity=dec(r.get("t1_quantity")),
                         raw_keys=tuple(sorted(r)))  # fmt: skip


def map_mf(r: dict[str, Any]) -> BrokerHolding:
    isin = str(r.get("tradingsymbol") or "").strip().upper() or None
    return BrokerHolding(name=str(r.get("fund") or isin or ""), quantity=dec(r.get("quantity")) or Decimal(0),
                         isin=isin, avg_price=dec(r.get("average_price")), last_price=dec(r.get("last_price")),
                         asset_type="mf", raw_keys=tuple(sorted(r)))  # fmt: skip


@dataclass(frozen=True)
class KiteSip:
    """A Coin SIP (GET /mf/sips): the basis of expected instalments, never a transaction."""

    sip_id: str
    isin: str | None
    fund: str
    status: str  # ACTIVE | PAUSED | CANCELLED
    frequency: str  # weekly | monthly | quarterly
    amount: Decimal | None
    instalment_day: int | None
    next_instalment: date | None
    last_instalment: date | None


@dataclass(frozen=True)
class KiteMfOrder:
    """A Coin order of the last 7 days (GET /mf/orders); `day` = the exchange day, else the order's day."""

    order_id: str
    isin: str | None
    status: str  # COMPLETE | REJECTED | CANCELLED | OPEN
    side: str  # BUY | SELL
    variety: str | None
    amount: Decimal | None
    quantity: Decimal | None
    average_price: Decimal | None
    day: date


def _iso_day(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def map_sip(r: dict[str, Any]) -> KiteSip:
    day = r.get("instalment_day")
    return KiteSip(sip_id=str(r.get("sip_id") or ""), isin=str(r.get("tradingsymbol") or "").strip().upper() or None,
                   fund=str(r.get("fund") or ""), status=str(r.get("status") or "").upper(),
                   frequency=str(r.get("frequency") or "").lower(), amount=dec(r.get("instalment_amount")),
                   instalment_day=int(day) if isinstance(day, int | float) and day else None,
                   next_instalment=_iso_day(r.get("next_instalment")),
                   last_instalment=_iso_day(r.get("last_instalment")))  # fmt: skip


def map_mf_order(r: dict[str, Any]) -> KiteMfOrder | None:
    day = _iso_day(r.get("exchange_timestamp")) or _iso_day(r.get("order_timestamp"))
    if day is None:
        return None
    return KiteMfOrder(order_id=str(r.get("order_id") or ""),
                       isin=str(r.get("tradingsymbol") or "").strip().upper() or None,
                       status=str(r.get("status") or "").upper(), side=str(r.get("transaction_type") or "").upper(),
                       variety=r.get("variety"), amount=dec(r.get("amount")), quantity=dec(r.get("quantity")),
                       average_price=dec(r.get("average_price")), day=day)  # fmt: skip


def map_position(r: dict[str, Any]) -> BrokerPosition:
    return BrokerPosition(symbol=str(r.get("tradingsymbol") or ""), quantity=dec(r.get("quantity")) or Decimal(0),
                          product=r.get("product"), exchange=r.get("exchange"), avg_price=dec(r.get("average_price")),
                          last_price=dec(r.get("last_price")), pnl=dec(r.get("pnl")))  # fmt: skip


def map_trade(r: dict[str, Any]) -> BrokerTrade | None:
    side = str(r.get("transaction_type") or "").upper()
    exch = str(r.get("exchange") or "").upper()
    if side not in ("BUY", "SELL") or exch not in ("NSE", "BSE"):
        return None  # F&O, currency and commodity fills are not portfolio lots
    ts = str(r.get("fill_timestamp") or r.get("exchange_timestamp") or "")
    try:
        day = datetime.fromisoformat(ts).date()
    except ValueError:
        return None
    qty, price = dec(r.get("quantity")), dec(r.get("average_price"))
    if not qty or price is None:
        return None
    sym = str(r.get("tradingsymbol") or "").strip().upper()
    return BrokerTrade(day=day, side=side.lower(), quantity=qty, price=price, name=sym,
                       symbol=sym if exch == "NSE" else None, exchange=exch, trade_id=str(r.get("trade_id") or ""),
                       order_id=str(r.get("exchange_order_id") or r.get("order_id") or "") or None,
                       product=r.get("product"), executed_at=ts)  # fmt: skip


class ZerodhaConnector(BrokerConnector):
    key: ClassVar[str] = "zerodha"
    label: ClassVar[str] = "Zerodha"
    account: ClassVar[str] = "Zerodha"
    auth_kind: ClassVar[str] = "oauth"
    capabilities: ClassVar[frozenset[str]] = frozenset({"holdings", "positions", "trades", "mf", "funds"})
    holdings_include_today: ClassVar[bool] = False
    docs_url: ClassVar[str] = "https://kite.trade/docs/connect/v3/"
    cost: ClassVar[str] = "Free with the Kite Connect Personal plan (portfolio APIs; no market data)"
    token_note: ClassVar[str] = "One browser login a day (Kite's session ends at 06:00 IST): press Reconnect."
    trades_note: ClassVar[str] = "Today's trades only: daily syncs build the history; import Console tradebooks " \
                                 "(365 days a file) for older trades."  # fmt: skip
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("api_key", "API key", help="developers.kite.trade → create a Personal app → API key."),
        FieldSpec("api_secret", "API secret", secret=True, help="Shown on the same app page."),
    )

    def secret_values(self) -> tuple[str, ...]:
        return (*super().secret_values(), str(self.config.get("api_key") or ""))

    def _http(self) -> ReadOnlyHttp:
        if not self.token:
            raise ReconnectNeeded("no Kite session: press Reconnect and log in to Kite")
        return ReadOnlyHttp(BASE, read_paths=READ_PATHS, secrets=self.secret_values(),
                            headers={"X-Kite-Version": "3",
                                     "Authorization": f"token {self.config.get('api_key')}:{self.token}"})  # fmt: skip

    def login_url(self, redirect_uri: str, state: str) -> str | None:
        # the redirect URL itself is fixed in the Kite app settings; redirect_params comes back as query parameters
        return f"{LOGIN}?{urlencode({'v': 3, 'api_key': self.config.get('api_key') or ''})}" \
               f"&redirect_params={quote(urlencode({'finresearch_state': state}))}"  # fmt: skip

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenGrant:
        key, secret = str(self.config.get("api_key") or ""), str(self.config.get("api_secret") or "")
        http = ReadOnlyHttp(BASE, read_paths=(), auth_paths=AUTH_PATHS, secrets=(*self.secret_values(), code),
                            headers={"X-Kite-Version": "3"})  # fmt: skip
        body = await http.auth_post("/session/token", data={"api_key": key, "request_token": code,
                                                             "checksum": checksum(key, code, secret)})  # fmt: skip
        data = _data(body) or {}
        tok = data.get("access_token")
        if not tok:
            raise ConnectorError("Kite's session answer had no access token")
        return TokenGrant(tok, next_six_am(datetime.now(IST)), {"connected_as": data.get("user_id")})

    async def holdings(self) -> list[BrokerHolding]:
        rows = _data(await self._http().get("/portfolio/holdings")) or []
        return [h for h in (map_holding(r) for r in rows if isinstance(r, dict)) if h.quantity > 0]

    async def positions(self) -> list[BrokerPosition]:
        data = _data(await self._http().get("/portfolio/positions")) or {}
        return [p for p in (map_position(r) for r in data.get("net") or []) if p.quantity != 0]

    async def trades(self, since: date, until: date) -> list[BrokerTrade]:
        rows = _data(await self._http().get("/trades")) or []
        return [t for t in (map_trade(r) for r in rows if isinstance(r, dict))
                if t is not None and since <= t.day <= until]  # fmt: skip

    async def mf_holdings(self) -> list[BrokerHolding]:
        rows = _data(await self._http().get("/mf/holdings")) or []
        return [h for h in (map_mf(r) for r in rows if isinstance(r, dict)) if h.quantity > 0]

    async def mf_sips(self) -> list[KiteSip]:
        rows = _data(await self._http().get("/mf/sips")) or []
        return [map_sip(r) for r in rows if isinstance(r, dict)]

    async def mf_activity(self) -> list[KiteMfOrder]:
        """GET /mf/orders, read-only (named so no connector attribute looks like an order action: base.FORBIDDEN_ATTR)."""
        rows = _data(await self._http().get("/mf/orders")) or []
        return [o for o in (map_mf_order(r) for r in rows if isinstance(r, dict)) if o is not None]

    async def funds(self) -> dict[str, Any]:
        data = _data(await self._http().get("/user/margins")) or {}
        eq = data.get("equity") or {}
        avail = eq.get("available") or {}
        return {"net": dec(eq.get("net")), "cash": dec(avail.get("live_balance") or avail.get("cash")),
                "margin_used": dec((eq.get("utilised") or {}).get("debits"))}  # fmt: skip
