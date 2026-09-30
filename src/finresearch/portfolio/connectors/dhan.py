"""Dhan HQ API v2 (read-only use): holdings, positions, trade history by date range and the fund limit.

Official docs (read 30-Sep-2026): https://dhanhq.co/docs/v2/authentication/ (tokens, TOTP login, static IP only for
order APIs), https://dhanhq.co/docs/v2/portfolio/ (holdings/positions), https://dhanhq.co/docs/v2/statements/ (trade
history, ledger), https://dhanhq.co/docs/v2/funds/. Research notes: brokers-research.md.

* Base ``https://api.dhan.co/v2``; header ``access-token: <JWT>`` (and ``client-id`` [U: the auth page names a
  dhanClientId header for some endpoints]).
* Tokens last 24 hours. Two ways in, both supported:
  - paste the token generated at web.dhan.co → "Access DhanHQ APIs" (valid 24 h: paste a new one to reconnect);
  - headless: client id + PIN + TOTP secret → ``POST https://auth.dhan.co/app/generateAccessToken`` with
    dhanClientId, pin and totp [U: the parameter names and whether they go in the query or the body were not
    captured; sent as query parameters, the form the docs example suggests].
* A static IP is required only for order placement/modification/cancellation; "no such IP whitelisting is required"
  to read orders and trades (docs, verified).
* Holdings ``GET /holdings`` → a list: exchange, tradingSymbol, securityId, isin, totalQty, dpQty, t1Qty,
  availableQty, collateralQty, avgCostPrice (no price). Total held = ``totalQty``.
* Positions ``GET /positions``. Trade history ``GET /trades/{from}/{to}/{page}`` (YYYY-MM-DD, page from 0 [U]) with
  exchangeTradeId, exchangeOrderId, transactionType, exchangeSegment, productType, tradingSymbol, isin,
  tradedQuantity, tradedPrice, exchangeTime and the charges.
* Funds ``GET /fundlimit``: availabelBalance (sic), withdrawableBalance, utilizedAmount.
* Cost: free.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

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

BASE = "https://api.dhan.co/v2"
AUTH_BASE = "https://auth.dhan.co"
READ_PATHS = (
    r"/holdings",
    r"/positions",
    r"/trades/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}/\d{1,3}",
    r"/fundlimit",
)
AUTH_PATHS = (r"/app/generateAccessToken",)
EQ_SEGMENTS = {"NSE_EQ", "BSE_EQ"}


def _list(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict):
        if body.get("errorCode") or body.get("errorMessage"):
            raise ConnectorError(f"Dhan answered {body.get('errorMessage') or body.get('errorCode')}")
        body = body.get("data") or []
    return [r for r in body or [] if isinstance(r, dict)]


def map_holding(r: dict[str, Any]) -> BrokerHolding:
    sym = str(r.get("tradingSymbol") or "").strip().upper() or None
    return BrokerHolding(name=sym or str(r.get("isin") or ""), quantity=dec(r.get("totalQty")) or Decimal(0),
                         isin=r.get("isin") or None, symbol=sym, exchange=r.get("exchange"),
                         avg_price=dec(r.get("avgCostPrice")), t1_quantity=dec(r.get("t1Qty")),
                         pledged=dec(r.get("collateralQty")), raw_keys=tuple(sorted(r)))  # fmt: skip


def map_position(r: dict[str, Any]) -> BrokerPosition:
    return BrokerPosition(symbol=str(r.get("tradingSymbol") or ""), quantity=dec(r.get("netQty")) or Decimal(0),
                          product=r.get("productType"), exchange=r.get("exchangeSegment"),
                          avg_price=dec(r.get("buyAvg")), pnl=dec(r.get("unrealizedProfit")),
                          segment=r.get("exchangeSegment"))  # fmt: skip


def map_trade(r: dict[str, Any]) -> BrokerTrade | None:
    seg = str(r.get("exchangeSegment") or "").upper()
    side = str(r.get("transactionType") or "").upper()
    if seg not in EQ_SEGMENTS or side not in ("BUY", "SELL"):
        return None
    ts = str(r.get("exchangeTime") or r.get("createTime") or "")
    try:
        day = date.fromisoformat(ts[:10])
    except ValueError:
        return None
    qty, price = dec(r.get("tradedQuantity")), dec(r.get("tradedPrice"))
    if not qty or price is None:
        return None
    sym = str(r.get("tradingSymbol") or r.get("customSymbol") or "").strip().upper()
    exch = seg.split("_")[0]
    return BrokerTrade(day=day, side=side.lower(), quantity=qty, price=price, name=sym, isin=r.get("isin") or None,
                       symbol=sym if exch == "NSE" else None, exchange=exch,
                       trade_id=str(r.get("exchangeTradeId") or "") or None,
                       order_id=str(r.get("exchangeOrderId") or r.get("orderId") or "") or None,
                       product=r.get("productType"), segment=seg, executed_at=ts)  # fmt: skip


class DhanConnector(BrokerConnector):
    key: ClassVar[str] = "dhan"
    label: ClassVar[str] = "Dhan"
    account: ClassVar[str] = "Dhan"
    auth_kind: ClassVar[str] = "totp"
    capabilities: ClassVar[frozenset[str]] = frozenset({"holdings", "positions", "trades", "funds"})
    holdings_include_today: ClassVar[bool] = False
    first_sync_days: ClassVar[int] = 365
    docs_url: ClassVar[str] = "https://dhanhq.co/docs/v2/"
    cost: ClassVar[str] = "Free (static IP needed only for order APIs, which this app never uses)"
    token_note: ClassVar[str] = "With PIN + TOTP secret it logs in by itself; with a pasted token, paste a new one " \
                                "every 24 hours."  # fmt: skip
    trades_note: ClassVar[str] = "Trade history by date range (the first sync reads the last year)."
    verified: ClassVar[str] = "partial"
    fields: ClassVar[tuple[FieldSpec, ...]] = (
        FieldSpec("client_id", "Client ID", help="Your Dhan client id (web.dhan.co → My Profile)."),
        FieldSpec("access_token", "Access token (24 h)", secret=True, required=False,
                  help="web.dhan.co → My Profile → Access DhanHQ APIs → generate. Or leave empty and use PIN + TOTP."),
        FieldSpec("pin", "PIN (for the automatic login)", secret=True, required=False,
                  help="Only with the TOTP secret below: the app then makes the daily token itself."),
        FieldSpec("totp_secret", "TOTP secret (for the automatic login)", secret=True, required=False,
                  help="Dhan → Setup TOTP: the base32 key shown with the QR code."),
    )  # fmt: skip

    def _http(self) -> ReadOnlyHttp:
        tok = self.token or str(self.config.get("access_token") or "")
        if not tok:
            raise ReconnectNeeded("no Dhan token: paste a token, or add your PIN and TOTP secret")
        return ReadOnlyHttp(BASE, read_paths=READ_PATHS, secrets=(*self.secret_values(), tok),
                            headers={"access-token": tok, "client-id": str(self.config.get("client_id") or ""),
                                     "Accept": "application/json"})  # fmt: skip

    async def login(self) -> TokenGrant:
        cid = str(self.config.get("client_id") or "")
        pin, seed = str(self.config.get("pin") or ""), str(self.config.get("totp_secret") or "")
        if pin and seed:
            try:
                code = totp(seed)
            except ValueError as e:
                raise ReconnectNeeded(str(e)) from None
            http = ReadOnlyHttp(AUTH_BASE, read_paths=(), auth_paths=AUTH_PATHS, secrets=self.secret_values(),
                                headers={"Accept": "application/json"})  # fmt: skip
            try:
                body = await http.auth_post("/app/generateAccessToken",
                                            params={"dhanClientId": cid, "pin": pin, "totp": code})  # fmt: skip
            except ReconnectNeeded as e:
                raise ReconnectNeeded(
                    f"Dhan refused the TOTP login ({e}): check the PIN and secret"
                ) from None
            body = body if isinstance(body, dict) else {}
            tok = body.get("accessToken") or body.get("access_token")
            if not tok:
                raise ConnectorError("Dhan's login answer had no access token")
            return TokenGrant(tok, _expiry(body.get("expiryTime")), {"connected_as": cid})
        pasted = str(self.config.get("access_token") or "")
        if pasted:
            return TokenGrant(pasted, None, {"connected_as": cid})
        raise ReconnectNeeded(
            "paste a Dhan access token, or add your PIN and TOTP secret for the automatic login"
        )

    async def holdings(self) -> list[BrokerHolding]:
        return [
            h for h in (map_holding(r) for r in _list(await self._http().get("/holdings"))) if h.quantity > 0
        ]

    async def positions(self) -> list[BrokerPosition]:
        return [
            p
            for p in (map_position(r) for r in _list(await self._http().get("/positions")))
            if p.quantity != 0
        ]

    async def trades(self, since: date, until: date) -> list[BrokerTrade]:
        http, out = self._http(), []
        for page in range(0, 50):
            rows = _list(await http.get(f"/trades/{since.isoformat()}/{until.isoformat()}/{page}"))
            out += [t for t in (map_trade(r) for r in rows) if t is not None]
            if not rows:
                break
        return out

    async def funds(self) -> dict[str, Any]:
        body = await self._http().get("/fundlimit")
        if not isinstance(body, dict):
            return {}
        return {"cash": dec(body.get("availabelBalance") or body.get("availableBalance")),
                "withdrawable": dec(body.get("withdrawableBalance")), "margin_used": dec(body.get("utilizedAmount"))}  # fmt: skip


def _expiry(v: Any) -> datetime:
    """Dhan's expiryTime (IST when it has no zone), else 23 hours from now (tokens last 24 h)."""
    from finresearch.fincalc.dates import IST

    try:
        d = datetime.fromisoformat(str(v))
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except (TypeError, ValueError):
        return datetime.now(UTC) + timedelta(hours=23)
