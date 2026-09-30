"""Groww Trade API (read-only use): holdings, positions, today's executed orders and the funds summary.

Official docs (read 30-Sep-2026): https://groww.in/trade-api/docs/curl (auth, headers, rate limits),
https://groww.in/trade-api/docs/curl/portfolio (holdings/positions), https://groww.in/trade-api/docs/curl/orders
(order list: today only), https://groww.in/trade-api/docs/curl/margin. Research notes: brokers-research.md.

* Base ``https://api.groww.in/v1``; every call sends ``Authorization: Bearer <access token>``, ``Accept:
  application/json`` and ``X-API-VERSION: 1.0``.
* Login, headless: the TOTP flow. The user creates a TOTP API key on Groww (Profile → Trading APIs), which "has no
  expiry and needs no daily approval", and copies its TOTP secret. Each day the app POSTs
  ``/token/api/access`` with ``Authorization: Bearer <TOTP API key>`` and ``{"key_type": "totp", "totp": <code>}``.
  The access token expires daily at 06:00 IST. Token generation is capped at 150 a day; the app makes one.
  The response field that carries the token was not visible in the docs [U]: the common names are tried.
* Holdings ``GET /holdings/user`` → ``payload.holdings[]``: isin, trading_symbol, quantity, average_price,
  t1_quantity, pledge_quantity, demat_free_quantity ... No price (the app prices holdings itself). In the docs sample
  the free/locked/pledged parts add up to ``quantity`` and ``t1_quantity`` is separate, so the total held is taken as
  ``quantity + t1_quantity`` [U: inferred from the sample].
* Positions ``GET /positions/user?segment=CASH`` → ``payload.positions[]``.
* Trades: the order list ``GET /order/list?segment=CASH&page=&page_size=100`` covers **today only**; an order with
  ``filled_quantity > 0`` becomes one trade at ``average_fill_price``. History before the first sync comes from the
  holdings baseline or a Groww order-history export (Import tab).
* Funds ``GET /margins/detail/user`` → clear_cash, net_margin_used, ...
* No mutual-fund endpoint exists in the Trade API: use a CAS for Groww mutual funds.
* Cost: a paid Trading API subscription (₹499 + GST a month early-bird, ₹2000 standard, per the docs page).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from finresearch.fincalc.dates import IST, to_ist
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
from finresearch.portfolio.connectors.totp import totp

BASE = "https://api.groww.in/v1"
READ_PATHS = (r"/holdings/user", r"/positions/user", r"/order/list", r"/margins/detail/user", r"/user/detail")
AUTH_PATHS = (r"/token/api/access",)


def next_six_am(now: datetime) -> datetime:
    ist = to_ist(now)
    six = ist.replace(hour=6, minute=0, second=0, microsecond=0)
    return six if ist < six else six + timedelta(days=1)


def _payload(body: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or str(body.get("status", "SUCCESS")).upper() != "SUCCESS":
        msg = (body.get("error") or {}) if isinstance(body, dict) else {}
        raise ConnectorError(
            f"Groww answered {msg.get('message') if isinstance(msg, dict) else msg or 'FAILURE'}"
        )
    p = body.get("payload") or {}
    rows = p.get(key) if isinstance(p, dict) else None
    return [r for r in rows or [] if isinstance(r, dict)]


def map_holding(r: dict[str, Any]) -> BrokerHolding:
    q = (dec(r.get("quantity")) or Decimal(0)) + (dec(r.get("t1_quantity")) or Decimal(0))
    sym = str(r.get("trading_symbol") or "").strip().upper() or None
    return BrokerHolding(name=sym or str(r.get("isin") or ""), quantity=q, isin=(r.get("isin") or None), symbol=sym,
                         exchange="NSE", avg_price=dec(r.get("average_price")), pledged=dec(r.get("pledge_quantity")),
                         t1_quantity=dec(r.get("t1_quantity")), raw_keys=tuple(sorted(r)))  # fmt: skip


def map_position(r: dict[str, Any]) -> BrokerPosition:
    return BrokerPosition(symbol=str(r.get("trading_symbol") or ""), quantity=dec(r.get("quantity")) or Decimal(0),
                          product=r.get("product"), exchange=r.get("exchange"), avg_price=dec(r.get("net_price")),
                          pnl=dec(r.get("realised_pnl")), segment="CASH")  # fmt: skip


def map_order(r: dict[str, Any]) -> BrokerTrade | None:
    filled = dec(r.get("filled_quantity")) or Decimal(0)
    price = dec(r.get("average_fill_price"))
    side = str(r.get("transaction_type") or "").upper()
    if filled <= 0 or not price or side not in ("BUY", "SELL"):
        return None
    created = str(r.get("created_at") or "")
    day = _day(created)
    if day is None:
        return None
    sym = str(r.get("trading_symbol") or "").strip().upper()
    return BrokerTrade(day=day, side=side.lower(), quantity=filled, price=price, name=sym, symbol=sym or None,
                       exchange=r.get("exchange"), order_id=str(r.get("groww_order_id") or "") or None,
                       trade_id=str(r.get("groww_order_id") or "") or None, product=r.get("product"),
                       segment="CASH", executed_at=created)  # fmt: skip


def _day(s: str) -> date | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return to_ist(dt).date() if dt.tzinfo else dt.date()


def token_from(body: Any) -> str | None:
    """The access token in the token response: the field name is not in the docs [U], so look for the usual ones."""
    if not isinstance(body, dict):
        return None
    for src in (body, body.get("payload") or {}, body.get("data") or {}):
        if isinstance(src, dict):
            for k in ("token", "access_token", "accessToken"):
                if isinstance(src.get(k), str) and src[k]:
                    return src[k]
    return None


class GrowwConnector(BrokerConnector):
    key: ClassVar[str] = "groww"
    label: ClassVar[str] = "Groww"
    account: ClassVar[str] = "Groww"
    auth_kind: ClassVar[str] = "totp"
    capabilities: ClassVar[frozenset[str]] = frozenset({"holdings", "positions", "trades", "funds"})
    holdings_include_today: ClassVar[bool] = False
    docs_url: ClassVar[str] = "https://groww.in/trade-api/docs/curl"
    cost: ClassVar[str] = "Paid: Groww Trading API subscription (₹499 + GST/month early-bird; ₹2000 standard)"
    token_note: ClassVar[str] = (
        "Logs in by itself each day with your TOTP key (the session ends at 06:00 IST)."
    )
    trades_note: ClassVar[str] = "Today's orders only: daily syncs build the history; older trades come from the " \
                                 "holdings baseline or a Groww order-history export."  # fmt: skip
    verified: ClassVar[str] = "partial"
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("api_key", "TOTP API key", secret=True,
                  help="Groww → Profile → Trading APIs → create a key of type TOTP. Copy the key (a long token)."),
        FieldSpec("totp_secret", "TOTP secret", secret=True,
                  help="Shown with the QR code when you create the TOTP key (base32 letters and digits). The app "
                       "makes the 6-digit code itself each morning."),
    )  # fmt: skip

    def _http(self) -> ReadOnlyHttp:
        if not self.token:
            raise ReconnectNeeded("no Groww session yet: log in")
        return ReadOnlyHttp(BASE, read_paths=READ_PATHS, secrets=self.secret_values(),
                            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json",
                                     "X-API-VERSION": "1.0"})  # fmt: skip

    async def login(self) -> TokenGrant:
        key, seed = str(self.config.get("api_key") or ""), str(self.config.get("totp_secret") or "")
        if not key or not seed:
            raise ReconnectNeeded("enter the Groww TOTP API key and TOTP secret")
        http = ReadOnlyHttp(BASE, read_paths=(), auth_paths=AUTH_PATHS, secrets=self.secret_values(),
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                     "Accept": "application/json", "X-API-VERSION": "1.0"})  # fmt: skip
        try:
            code = totp(seed)
        except ValueError as e:
            raise ReconnectNeeded(str(e)) from None
        try:
            body = await http.auth_post("/token/api/access", json={"key_type": "totp", "totp": code})
        except ReconnectNeeded as e:
            raise ReconnectNeeded(f"Groww refused the TOTP login ({e}): check the key and secret") from None
        tok = token_from(body)
        if not tok:
            raise ConnectorError("Groww's login answer had no access token")
        return TokenGrant(tok, next_six_am(datetime.now(IST)))

    async def holdings(self) -> list[BrokerHolding]:
        rows = _payload(await self._http().get("/holdings/user"), "holdings")
        return [h for h in (map_holding(r) for r in rows) if h.quantity > 0]

    async def positions(self) -> list[BrokerPosition]:
        rows = _payload(await self._http().get("/positions/user", {"segment": "CASH"}), "positions")
        return [p for p in (map_position(r) for r in rows) if p.quantity != 0]

    async def trades(self, since: date, until: date) -> list[BrokerTrade]:
        http, out = self._http(), []
        for page in range(0, 20):
            body = await http.get("/order/list", {"segment": "CASH", "page": page, "page_size": 100})
            rows = _payload(body, "order_list") or _payload(body, "orders")
            out += [t for t in (map_order(r) for r in rows) if t is not None and since <= t.day <= until]
            if len(rows) < 100:
                break
        return out

    async def funds(self) -> dict[str, Any]:
        body = await self._http().get("/margins/detail/user")
        p = body.get("payload") if isinstance(body, dict) else None
        if not isinstance(p, dict):
            return {}
        return {"cash": dec(p.get("clear_cash")), "margin_used": dec(p.get("net_margin_used")),
                "collateral_available": dec(p.get("collateral_available"))}  # fmt: skip
