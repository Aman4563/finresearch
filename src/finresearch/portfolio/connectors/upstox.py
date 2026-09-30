"""Upstox API v2 (read-only use): holdings, positions and up to three financial years of trade history.

Official docs (read 30-Sep-2026): https://upstox.com/developer/api-documentation/authentication/ and .../get-token/ (login dialog,
token, 03:30 expiry), .../get-holdings/, .../get-positions/, .../get-historical-trades/ (last 3 financial years).
Research notes: brokers-research.md.

* Base ``https://api.upstox.com/v2``; header ``Authorization: Bearer <access token>``, ``Accept: application/json``.
* Login is a browser redirect each day: ``/login/authorization/dialog?client_id&redirect_uri&response_type=code
  &state`` → the app's callback gets ``code`` → ``POST /login/authorization/token`` (form: code, client_id,
  client_secret, redirect_uri, grant_type=authorization_code). The token expires at 03:30 the next day. (An
  ``extended_token`` for read-only use exists but its eligibility and lifetime are not documented [U]: not used.)
* Holdings ``GET /portfolio/long-term-holdings`` → ``data[]``: isin, trading_symbol, company_name, quantity,
  t1_quantity, average_price, last_price, exchange ... Total held = ``quantity + t1_quantity`` [U].
* Positions ``GET /portfolio/short-term-positions`` → ``data[]``.
* Trade history ``GET /charges/historical-trades?start_date&end_date&page_number&page_size&segment=EQ`` → ``data[]``:
  trade_id, trade_date, transaction_type, symbol, isin, quantity, price, amount, exchange, segment, scrip_name.
  The paging envelope is not documented beyond page_number/page_size [U]: pages are read until a short page.
* Funds and MF holdings endpoints were not verified in the docs: not used.
* Cost: free.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import urlencode

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

BASE = "https://api.upstox.com/v2"
READ_PATHS = (r"/portfolio/long-term-holdings", r"/portfolio/short-term-positions", r"/charges/historical-trades",
              r"/user/profile")  # fmt: skip
AUTH_PATHS = (r"/login/authorization/token",)
PAGE = 1000
MAX_DAYS = 3 * 366  # "last 3 financial years"


def next_330(now: datetime) -> datetime:
    ist = to_ist(now)
    t = ist.replace(hour=3, minute=30, second=0, microsecond=0)
    return t if ist < t else t + timedelta(days=1)


def _data(body: Any) -> Any:
    if not isinstance(body, dict) or body.get("status") != "success":
        errs = body.get("errors") if isinstance(body, dict) else None
        msg = (
            errs[0].get("message")
            if isinstance(errs, list) and errs and isinstance(errs[0], dict)
            else "an error"
        )
        raise ConnectorError(f"Upstox answered {msg}")
    return body.get("data")


def map_holding(r: dict[str, Any]) -> BrokerHolding:
    q = (dec(r.get("quantity")) or Decimal(0)) + (dec(r.get("t1_quantity")) or Decimal(0))
    sym = str(r.get("trading_symbol") or r.get("tradingsymbol") or "").strip().upper() or None
    exch = r.get("exchange")
    return BrokerHolding(name=str(r.get("company_name") or sym or r.get("isin") or ""), quantity=q,
                         isin=r.get("isin") or None, symbol=sym if exch != "BSE" else None, exchange=exch,
                         avg_price=dec(r.get("average_price")), last_price=dec(r.get("last_price")),
                         t1_quantity=dec(r.get("t1_quantity")), raw_keys=tuple(sorted(r)))  # fmt: skip


def map_position(r: dict[str, Any]) -> BrokerPosition:
    return BrokerPosition(symbol=str(r.get("trading_symbol") or r.get("tradingsymbol") or ""),
                          quantity=dec(r.get("quantity")) or Decimal(0), product=r.get("product"),
                          exchange=r.get("exchange"), avg_price=dec(r.get("average_price")),
                          last_price=dec(r.get("last_price")), pnl=dec(r.get("pnl")))  # fmt: skip


def map_trade(r: dict[str, Any]) -> BrokerTrade | None:
    side = str(r.get("transaction_type") or "").upper()
    if side not in ("BUY", "SELL") or str(r.get("segment") or "EQ").upper() not in ("EQ", "EQUITY"):
        return None
    try:
        day = date.fromisoformat(str(r.get("trade_date") or "")[:10])
    except ValueError:
        return None
    qty, price = dec(r.get("quantity")), dec(r.get("price"))
    if not qty or price is None:
        return None
    sym = str(r.get("symbol") or "").strip().upper()
    exch = str(r.get("exchange") or "").upper() or None
    return BrokerTrade(day=day, side=side.lower(), quantity=qty, price=price, name=str(r.get("scrip_name") or sym),
                       isin=r.get("isin") or None, symbol=sym if exch != "BSE" else None, exchange=exch,
                       trade_id=str(r.get("trade_id") or "") or None, executed_at=str(r.get("trade_date")))  # fmt: skip


class UpstoxConnector(BrokerConnector):
    key: ClassVar[str] = "upstox"
    label: ClassVar[str] = "Upstox"
    account: ClassVar[str] = "Upstox"
    auth_kind: ClassVar[str] = "oauth"
    capabilities: ClassVar[frozenset[str]] = frozenset({"holdings", "positions", "trades"})
    holdings_include_today: ClassVar[bool] = False
    first_sync_days: ClassVar[int] = 1100
    docs_url: ClassVar[str] = "https://upstox.com/developer/api-documentation/"
    cost: ClassVar[str] = "Free"
    token_note: ClassVar[str] = "One browser login a day (the session ends at 03:30 IST): press Reconnect."
    trades_note: ClassVar[str] = "Trade history for the last 3 financial years (read on the first sync)."
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec(
            "api_key", "API key (client id)", help="account.upstox.com/developer/apps → New app → API key."
        ),
        FieldSpec("api_secret", "API secret", secret=True, help="Shown on the same app page."),
    )

    def secret_values(self) -> tuple[str, ...]:
        return (*super().secret_values(), str(self.config.get("api_key") or ""))

    def _http(self) -> ReadOnlyHttp:
        if not self.token:
            raise ReconnectNeeded("no Upstox session: press Reconnect and log in to Upstox")
        return ReadOnlyHttp(BASE, read_paths=READ_PATHS, secrets=self.secret_values(),
                            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"})  # fmt: skip

    def login_url(self, redirect_uri: str, state: str) -> str | None:
        q = {"client_id": self.config.get("api_key") or "", "redirect_uri": redirect_uri, "response_type": "code",
             "state": state}  # fmt: skip
        return f"{BASE}/login/authorization/dialog?{urlencode(q)}"

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenGrant:
        http = ReadOnlyHttp(BASE, read_paths=(), auth_paths=AUTH_PATHS, secrets=(*self.secret_values(), code),
                            headers={"Accept": "application/json"})  # fmt: skip
        body = await http.auth_post("/login/authorization/token", data={
            "code": code, "client_id": self.config.get("api_key") or "",
            "client_secret": self.config.get("api_secret") or "", "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"})  # fmt: skip
        tok = body.get("access_token") if isinstance(body, dict) else None
        if not tok:
            raise ConnectorError("Upstox's token answer had no access token")
        return TokenGrant(tok, next_330(datetime.now(IST)), {"connected_as": body.get("user_id")})

    async def holdings(self) -> list[BrokerHolding]:
        rows = _data(await self._http().get("/portfolio/long-term-holdings")) or []
        return [h for h in (map_holding(r) for r in rows if isinstance(r, dict)) if h.quantity > 0]

    async def positions(self) -> list[BrokerPosition]:
        rows = _data(await self._http().get("/portfolio/short-term-positions")) or []
        return [p for p in (map_position(r) for r in rows if isinstance(r, dict)) if p.quantity != 0]

    async def trades(self, since: date, until: date) -> list[BrokerTrade]:
        http, out = self._http(), []
        start = max(since, until - timedelta(days=MAX_DAYS))
        for page in range(1, 50):
            rows = _data(await http.get("/charges/historical-trades", {
                "start_date": start.isoformat(), "end_date": until.isoformat(), "page_number": page,
                "page_size": PAGE, "segment": "EQ"})) or []  # fmt: skip
            if isinstance(rows, dict):  # an envelope with the list inside [U]
                rows = rows.get("trades") or rows.get("data") or []
            out += [t for t in (map_trade(r) for r in rows if isinstance(r, dict)) if t is not None]
            if len(rows) < PAGE:
                break
        return out
