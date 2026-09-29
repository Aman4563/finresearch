"""NSE derivatives data for analysis: expiries and strikes, the option chain (per-strike premium, IV, OI and
volume for calls and puts) and market lot sizes. Nothing here places or routes orders."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from finresearch.adapters.nse import (
    API_HEADERS,
    NSE_BASE,
    NseClient,
    NseError,
    parse_nse_date,
    parse_nse_timestamp,
    parse_num,
)

INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
PAGE = f"{NSE_BASE}/option-chain"


class OptionQuote(BaseModel):
    last_price: Decimal | None
    iv: Decimal | None  # % as NSE publishes it
    oi: Decimal | None
    change_in_oi: Decimal | None
    volume: Decimal | None
    bid: Decimal | None
    ask: Decimal | None

    @classmethod
    def parse(cls, r: dict[str, Any] | None) -> OptionQuote | None:
        if not r:
            return None
        return cls(last_price=parse_num(r.get("lastPrice")), iv=parse_num(r.get("impliedVolatility")),
                   oi=parse_num(r.get("openInterest")), change_in_oi=parse_num(r.get("changeinOpenInterest")),
                   volume=parse_num(r.get("totalTradedVolume")), bid=parse_num(r.get("buyPrice1")),
                   ask=parse_num(r.get("sellPrice1")))  # fmt: skip


class ChainRow(BaseModel):
    strike: Decimal
    call: OptionQuote | None
    put: OptionQuote | None


class OptionChain(BaseModel):
    symbol: str
    expiry: date
    underlying: Decimal | None
    as_of: datetime | None
    rows: list[ChainRow]

    def atm(self) -> ChainRow | None:
        if not self.rows or self.underlying is None:
            return None
        return min(self.rows, key=lambda r: abs(r.strike - self.underlying))

    def pcr(self) -> Decimal | None:
        """Put-call ratio of open interest across the chain."""
        put = sum((r.put.oi or 0 for r in self.rows if r.put), Decimal(0))
        call = sum((r.call.oi or 0 for r in self.rows if r.call), Decimal(0))
        return put / call if call else None

    def max_pain(self) -> Decimal | None:
        """Strike at which option writers' total payout to holders at expiry is smallest."""
        best = None
        for s in (r.strike for r in self.rows):
            pain = sum(((max(Decimal(0), s - r.strike) * (r.call.oi or 0) if r.call else 0)
                        + (max(Decimal(0), r.strike - s) * (r.put.oi or 0) if r.put else 0)) for r in self.rows)  # fmt: skip
            if best is None or pain < best[1]:
                best = (s, pain)
        return best[0] if best else None


def parse_chain(symbol: str, expiry: date, data: dict[str, Any]) -> OptionChain:
    rec = data.get("records") or {}
    rows_raw = (data.get("filtered") or {}).get("data") or rec.get("data") or []
    rows = [ChainRow(strike=Decimal(str(r["strikePrice"])), call=OptionQuote.parse(r.get("CE")),
                     put=OptionQuote.parse(r.get("PE"))) for r in rows_raw]  # fmt: skip
    return OptionChain(symbol=symbol, expiry=expiry, underlying=parse_num(rec.get("underlyingValue")),
                       as_of=parse_nse_timestamp(rec.get("timestamp")), rows=sorted(rows, key=lambda r: r.strike))  # fmt: skip


def parse_lot_sizes(text: str) -> dict[str, dict[str, int]]:
    """{symbol: {"OCT-26": 65, ...}} from NSE's market-lots file."""
    reader = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(reader, [])]
    out: dict[str, dict[str, int]] = {}
    for row in reader:
        if len(row) < 3:
            continue
        sym = row[1].strip()
        lots = {
            header[i]: int(v.strip())
            for i, v in enumerate(row[2:], start=2)
            if i < len(header) and v.strip().isdigit()
        }
        if sym and sym.upper() != "SYMBOL" and lots:
            out[sym] = lots
    return out


def lot_size_for(lots: dict[str, dict[str, int]], symbol: str, expiry: date) -> int | None:
    return lots.get(symbol, {}).get(expiry.strftime("%b-%y").upper())


class NseFno:
    def __init__(self, client: NseClient | None = None):
        self.nse = client or NseClient(warmup_url=PAGE)

    async def __aenter__(self) -> NseFno:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.nse.aclose()

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        if not self.nse._warmed:
            await self.nse.warm_up()
        resp = await self.nse.http.get(
            f"{NSE_BASE}{path}", params=params, headers={**API_HEADERS, "Referer": PAGE}
        )
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {path}")
        return resp.json()

    async def contract_info(self, symbol: str) -> tuple[list[date], list[Decimal]]:
        d = await self._get("/api/option-chain-contract-info", {"symbol": symbol})
        return ([x for x in (parse_nse_date(e) for e in d.get("expiryDates", [])) if x],
                [Decimal(s) for s in d.get("strikePrice", [])])  # fmt: skip

    async def option_chain(self, symbol: str, expiry: date) -> OptionChain:
        kind = "Indices" if symbol.upper() in INDICES else "Equity"
        d = await self._get(
            "/api/option-chain-v3", {"type": kind, "symbol": symbol, "expiry": expiry.strftime("%d-%b-%Y")}
        )
        if not d:
            raise NseError(f"NSE returned no option chain for {symbol} {expiry}")
        return parse_chain(symbol, expiry, d)

    async def lot_sizes(self) -> dict[str, dict[str, int]]:
        resp = await self.nse.http.get(LOTS_URL, headers={"Referer": f"{NSE_BASE}/"})
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for the market-lots file")
        return parse_lot_sizes(resp.content.decode("utf-8", "replace"))
