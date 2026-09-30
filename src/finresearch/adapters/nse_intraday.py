"""NSE intraday price series: today's (or the last session's) 1-minute series for an equity or an index.

Endpoints (the quote pages' own "1D" charts; verified live 30-Sep-2026, JSON after the quote-page cookie warm-up):
- `/api/NextApi/apiClient/GetQuoteApi?functionName=getSymbolChartData&symbol=<SYMBOL><SERIES>N&days=1D`
      -> {"identifier": "INFYEQN", "name": "INFY", "closePrice": <previous close>, "grapthData": [[ts, price, phase, chg, chg%], ...]}
- `/api/NextApi/apiClient/indexTrackerApi?functionName=getIndexChart&index=<NIFTY 50>&flag=1D`
      -> {"data": {same shape}}

Facts that matter (see timeframes research notes):
- One last-traded-price sample per minute (stamped hh:mm:59) plus a trailing live point; NO volume and no OHLC.
- `ts` is IST wall-clock time encoded as if it were UTC epoch milliseconds (1790772670000 = 12:51:10 IST on
  30-Sep-2026), so it is decoded as UTC and relabelled IST, never converted.
- `phase` "PO" = pre-open (09:00-09:08), "NM" = normal market. Index payloads repeat timestamps; callers normalise.
- Only one session is served: there is no multi-day intraday series (days=5D answers HTTP 404 NULL_POINTER), so the
  app archives each day's series itself (monitor.intraday).
- The legacy `/api/chart-databyindex` endpoint answers an empty `grapthData` for every input and is not used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from finresearch.adapters.http import IST
from finresearch.adapters.nse import API_HEADERS, NSE_BASE, NseClient, NseError, parse_num
from finresearch.fincalc.candles import Tick, normalise

EQUITY_PATH = "/api/NextApi/apiClient/GetQuoteApi"
INDEX_PATH = "/api/NextApi/apiClient/indexTrackerApi"
# the indices the app offers (names exactly as NSE's index pages spell them)
INDICES = (
    "NIFTY 50",
    "NIFTY BANK",
    "NIFTY NEXT 50",
    "NIFTY FIN SERVICE",
    "NIFTY MIDCAP 100",
    "NIFTY IT",
    "INDIA VIX",
)


def equity_page(symbol: str) -> str:
    return f"{NSE_BASE}/get-quotes/equity?symbol={symbol}"


def index_page(name: str) -> str:
    return f"{NSE_BASE}/index-tracker/{name.replace(' ', '%20')}"


def decode_ts(ms: int | float) -> datetime:
    """NSE chart timestamp -> aware IST datetime (the epoch value already holds IST wall-clock time)."""
    return datetime.fromtimestamp(ms / 1000, UTC).replace(tzinfo=IST)


@dataclass
class IntradaySeries:
    symbol: str  # NSE symbol ("INFY") or index name ("NIFTY 50")
    kind: str  # "equity" | "index"
    prev_close: Decimal | None
    ticks: list[Tick] = field(default_factory=list)
    source: str = ""

    @property
    def as_of(self) -> datetime | None:
        return self.ticks[-1].at if self.ticks else None

    @property
    def day(self):
        return self.ticks[-1].at.date() if self.ticks else None


def parse_chart(data: dict[str, Any], symbol: str, kind: str, source: str) -> IntradaySeries:
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(body, dict) or "grapthData" not in body:
        raise NseError(f"NSE chart payload for {symbol} has no grapthData")
    ticks = []
    for row in body.get("grapthData") or []:
        if not isinstance(row, list) or len(row) < 2 or row[0] is None:
            continue
        price = parse_num(row[1])
        if price is None:
            continue
        ticks.append(Tick(decode_ts(row[0]), price, str(row[2]) if len(row) > 2 and row[2] else "NM"))
    prev = parse_num(body.get("closePrice"))
    return IntradaySeries(symbol, kind, prev if prev else None, normalise(ticks), source)


async def _get(nse: NseClient, path: str, params: dict[str, str], referer: str) -> Any:
    if not nse._warmed:
        nse.http.cookies.clear()
        await nse.http.get(referer)
        nse._warmed = True
    resp = await nse.http.get(f"{NSE_BASE}{path}", params=params, headers={**API_HEADERS, "Referer": referer})
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {path} ({params})")
    try:
        return resp.json()
    except ValueError as e:
        raise NseError(f"NSE returned a non-JSON page for {path}") from e


def equity_chart_params(symbol: str, series: str = "EQ") -> dict[str, str]:
    return {"functionName": "getSymbolChartData", "symbol": f"{symbol}{series}N", "days": "1D"}


def equity_chart_url(symbol: str, series: str = "EQ") -> str:
    """The exact NSE request behind a stock's 1-minute series (the quote page's chart API)."""
    from urllib.parse import urlencode

    return f"{NSE_BASE}{EQUITY_PATH}?" + urlencode(equity_chart_params(symbol, series))


async def equity_intraday(nse: NseClient, symbol: str, series: str = "EQ") -> IntradaySeries:
    page = equity_page(symbol)
    data = await _get(nse, EQUITY_PATH, equity_chart_params(symbol, series), page)
    return parse_chart(data, symbol, "equity", page)


async def index_intraday(nse: NseClient, name: str) -> IntradaySeries:
    page = index_page(name)
    data = await _get(nse, INDEX_PATH, {"functionName": "getIndexChart", "index": name, "flag": "1D"}, page)
    return parse_chart(data, name, "index", page)
