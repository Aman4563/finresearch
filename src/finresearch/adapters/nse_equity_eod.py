"""NSE's daily full equity bhavcopy with delivery (#373 (a)): every cash-market security's previous close, OHLC,
last, close, average price, traded quantity, turnover, trade count and delivery, one CSV a day at
`products/content/sec_bhavdata_full_<DDMMYYYY>.csv` on nsearchives.nseindia.com. Read-only public file; personal,
non-distributed use only (owner decision #362).

Format (the 21-Oct-2026 file, read once on 22-Oct-2026 at 04:56 IST: 400,625 bytes, 3,536 rows, LF line ends):
header `SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE,
TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER`, every token after the first padded with one space;
DATE1 is dd-Mon-yyyy. Every series is listed (that day: EQ 2,694, SM 393, BE 222, ST 83, GS 50, GB 43, BZ 30, IV 13,
RR 6, E1 2), and (symbol, series) is unique. DELIV_QTY and DELIV_PER are '-' on the trade-to-trade series (BE, BZ):
kept as None (unknown), never 0 and never inferred. Turnover is in ₹ lakh as printed.

The file has no ISIN: the dated security master (#373 (b), behind a registered study) would read the CM UDiFF
bhavcopy, which NSE serves from mid-2024 only (404 for 2020, per the issue); this file was confirmed for 2020.
A missing day answers 404 (a holiday, a weekend, or not yet published), read as "no file"; any non-CSV 200 (a block
page) or a refusal raises NseError and is never read as "no file".
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseError

ARCHIVES = "https://nsearchives.nseindia.com"
URL = ARCHIVES + "/products/content/sec_bhavdata_full_{day:%d%m%Y}.csv"
HEADER = ("SYMBOL", "SERIES", "DATE1", "PREV_CLOSE", "OPEN_PRICE", "HIGH_PRICE", "LOW_PRICE", "LAST_PRICE",
          "CLOSE_PRICE", "AVG_PRICE", "TTL_TRD_QNTY", "TURNOVER_LACS", "NO_OF_TRADES", "DELIV_QTY", "DELIV_PER")  # fmt: skip
PARSER_VERSION = "1"  # bump when the parser's output changes for the same file (#368 lineage)
MAX_BYTES = 8 * 1024 * 1024  # a day's file is ~400 KB


@dataclass(slots=True)
class EquityRow:
    day: date
    symbol: str
    series: str  # EQ, BE, BZ, SM, ST, GS, ... as printed
    prev_close: Decimal | None
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    last: Decimal | None
    close: Decimal | None  # NSE's official closing price
    avg_price: Decimal | None
    qty: int | None  # shares traded
    turnover_lacs: Decimal | None  # ₹ lakh
    trades: int | None
    deliv_qty: int | None  # shares marked for delivery; None ('-') on trade-to-trade series
    deliv_pct: Decimal | None  # percent of qty


def url_for(day: date) -> str:
    return URL.format(day=day)


def _dec(v: str | None) -> Decimal | None:
    s = (v or "").strip()
    if not s or s == "-":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _int(v: str | None) -> int | None:
    d = _dec(v)
    return None if d is None else int(d)


def _date(v: str) -> date:
    s = v.strip()
    try:
        return datetime.strptime(s, "%d-%b-%Y").date()
    except ValueError:
        raise NseError(f"unreadable bhavcopy date {s!r}") from None


def parse(text: str, day: date) -> list[EquityRow]:
    """The file's rows in file order. Raises NseError for anything that is not this file (a block page) or a file
    dated another day; a row with no symbol is skipped."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = [h.strip() for h in next(reader, None) or []]
    if tuple(head[: len(HEADER)]) != HEADER:
        raise NseError("not an NSE full bhavcopy (sec_bhavdata_full)")
    out: list[EquityRow] = []
    for r in reader:
        if not r or not r[0].strip():
            continue
        r = ([c.strip() for c in r] + [""] * 15)[:15]
        d = _date(r[2])
        if d != day:
            raise NseError(f"the full bhavcopy for {day} carries another day ({d})")
        out.append(EquityRow(day=d, symbol=r[0], series=r[1], prev_close=_dec(r[3]), open=_dec(r[4]),
                             high=_dec(r[5]), low=_dec(r[6]), last=_dec(r[7]), close=_dec(r[8]),
                             avg_price=_dec(r[9]), qty=_int(r[10]), turnover_lacs=_dec(r[11]), trades=_int(r[12]),
                             deliv_qty=_int(r[13]), deliv_pct=_dec(r[14])))  # fmt: skip
    return out


async def fetch_day(http: PoliteClient, day: date) -> tuple[str, bytes] | None:
    """(url, body) of the day's file; None on 404. A refusal, a 5xx or a non-CSV 200 raises NseError."""
    url = url_for(day)
    resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "text/csv,*/*"})
    if resp.status == 404:
        return None
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {url}")
    if not resp.content.lstrip(b"\xef\xbb\xbf").startswith(b"SYMBOL,") or len(resp.content) > MAX_BYTES:
        raise NseError(f"NSE served something other than the full bhavcopy CSV for {url}: a block page?")
    return url, resp.content
