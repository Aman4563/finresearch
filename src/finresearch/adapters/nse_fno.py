"""NSE derivatives data for analysis: expiries and strikes, the option chain (per-strike premium, IV, OI and
volume for calls and puts) and market lot sizes. Nothing here places or routes orders."""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel

from finresearch.adapters.nse import (
    API_HEADERS,
    NSE_BASE,
    NseClient,
    NseError,
    _looks_json,
    parse_nse_date,
    parse_nse_timestamp,
    parse_num,
)

INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}
# the index name NSE's index-history API expects for each F&O index symbol (verified live 30-Sep-2026 for the first
# three; the API answers FINNIFTY rows as "NIFTY FIN SERVICE")
INDEX_NAMES = {"NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FINANCIAL SERVICES",
               "MIDCPNIFTY": "NIFTY MID SELECT", "NIFTYNXT50": "NIFTY NEXT 50"}  # fmt: skip
INDEX_HISTORY_PAGE = f"{NSE_BASE}/reports-indices-historical-index-data"
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
    source: str = "NSE"  # "Groww" when adapters.groww_fno read it from the user's Groww API (#267)

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


def parse_index_history(data: dict[str, Any]) -> list[tuple[date, Decimal]]:
    """(day, close) oldest first from NSE's /api/historicalOR/indicesHistory payload."""
    out = {}
    for r in (data or {}).get("data") or []:
        d, close = parse_nse_date(r.get("EOD_TIMESTAMP")), parse_num(r.get("EOD_CLOSE_INDEX_VAL"))
        if d and close:
            out[d] = close
    return sorted(out.items())


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
        """GET an option-chain API with the option-chain page as referer. Like NseClient.get_json, a 401/403 or a
        block page served with HTTP 200 re-warms the cookies once, and a body that is not JSON raises NseError (a
        raw JSONDecodeError would not read as transient, so callers would take it for "no data")."""
        if not self.nse._warmed:
            await self.nse.warm_up()
        for attempt in range(2):
            resp = await self.nse.http.get(
                f"{NSE_BASE}{path}", params=params, headers={**API_HEADERS, "Referer": PAGE}
            )
            if resp.status in (401, 403) or (resp.ok and not _looks_json(resp)):
                if attempt == 0:
                    await self.nse.warm_up()
                    continue
                raise NseError(f"NSE refused {path} after re-warm: HTTP {resp.status} (block page)")
            if not resp.ok:
                raise NseError(f"NSE HTTP {resp.status} for {path}")
            try:
                return resp.json()
            except ValueError as exc:
                raise NseError(f"NSE returned invalid JSON for {path}") from exc
        raise NseError(f"NSE refused {path}")  # pragma: no cover - the loop always returns or raises

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

    async def closes(self, symbol: str, start: date, end: date) -> list[tuple[date, Decimal]]:
        """Daily closes of the underlying, oldest first: NSE's index history for indices, the equity history for
        stocks."""
        sym = symbol.upper()
        if sym in INDICES:
            # the API returns at most about 70 rows from the start of the range: ask in 60-day windows
            out: dict[date, Decimal] = {}
            async with NseClient(warmup_url=INDEX_HISTORY_PAGE) as nse:
                lo = start
                while lo <= end:
                    hi = min(end, lo + timedelta(days=59))
                    d, _ = await nse.get_json("/api/historicalOR/indicesHistory",
                                              {"indexType": INDEX_NAMES[sym], "from": lo.strftime("%d-%m-%Y"),
                                               "to": hi.strftime("%d-%m-%Y")})  # fmt: skip
                    out.update(parse_index_history(d))
                    lo = hi + timedelta(days=1)
            return sorted(out.items())
        from finresearch.adapters.nse_equity import NseEquity, walk_history
        from finresearch.fincalc.signals import adjust_for_actions

        # NSE answers a range with only its latest ~70 trading days (walk back for the rest), and its closes are not
        # adjusted for splits and bonuses: a bonus inside the window would read as a -50 % day and inflate the
        # realised volatility the F&O risk gate uses (adjusted as the stock signal does, fincalc.signals)
        async with NseEquity() as eq:
            bars, _partial = await walk_history(lambda lo, hi: eq.history(sym, lo, hi), start, end)
            bars = [b for b in bars if b.close]
            try:
                actions = [(a.ex_date, a.subject) for a in await eq.corporate_actions(sym) if a.ex_date]
            except (
                Exception
            ):  # unadjusted closes still serve; an unexplained jump is the signal's anomaly to flag
                actions = []
        if not bars:
            return []
        adj = adjust_for_actions([b.day for b in bars], [float(b.close) for b in bars], actions)
        return [(d, Decimal(str(round(c, 4)))) for d, c in zip(adj.days, adj.close, strict=True)]

    async def lot_sizes(self) -> dict[str, dict[str, int]]:
        resp = await self.nse.http.get(LOTS_URL, headers={"Referer": f"{NSE_BASE}/"})
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for the market-lots file")
        return parse_lot_sizes(resp.content.decode("utf-8", "replace"))
