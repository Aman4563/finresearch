"""NSE's daily index close file (#366): every NSE index's OHLC, P/E, P/B and dividend yield, plus India VIX, one CSV a
day at `content/indices/ind_close_all_<DDMMYYYY>.csv` on nsearchives.nseindia.com. Read-only public file; personal,
non-distributed use only (owner decision #362).

Format (files of 01-Jun-2015 and 03-Jan-2022 read on 20-Oct-2026): header `Index Name,Index Date,Open Index Value,High
Index Value,Low Index Value,Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield`;
'-' marks a value the index does not have (India VIX has no P/E; some indices have no open/high/low), kept as None.
The date is dd/mm/yyyy in older files and dd-mm-yyyy in newer ones; names changed from 'CNX Nifty' / 'CNX Bank' /
'CNX Nifty Junior' to 'Nifty 50' / 'Nifty Bank' / 'Nifty Next 50' (2015 rebrand), so the F&O underlyings are mapped to
their F&O symbols (FNO_SYMBOLS) and every other index keeps its upper-cased name. India VIX rows are present in the
2015 and 2022 files; the 2013 start is in the issue (between Mar-2013 and Mar-2016).

Availability: the 13-Mar-2013, 16-Mar-2016 and 23-Mar-2020 files answered 200, the 24-Oct-2008, 15-Dec-2010 and
18-Jan-2012 files 404 (data check of 20-Oct-2026), so backfills start in 2013. A missing day answers 404 with an HTML
page; that is "no file" (a holiday, a weekend, or not yet published), while any non-CSV 200 (a block page) or a
refusal raises NseError and is never read as "no file".
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
URL = ARCHIVES + "/content/indices/ind_close_all_{day:%d%m%Y}.csv"
HEADER = "Index Name"
PARSER_VERSION = "1"  # bump when the parser's output changes for the same file (#368 lineage)
MAX_BYTES = 1024 * 1024  # a day's file is ~9 KB
# F&O underlyings by every name NSE has used for them in this file; the symbol is the F&O ticker so the index close
# joins fno_bhav and the direction model (direction/data.py reads index_eod by F&O symbol)
FNO_SYMBOLS: dict[str, str] = {
    "CNX NIFTY": "NIFTY", "NIFTY 50": "NIFTY",
    "CNX BANK": "BANKNIFTY", "NIFTY BANK": "BANKNIFTY", "BANK NIFTY": "BANKNIFTY",
    "CNX FINANCE": "FINNIFTY", "NIFTY FINANCIAL SERVICES": "FINNIFTY",
    "NIFTY MIDCAP SELECT": "MIDCPNIFTY",
    "CNX NIFTY JUNIOR": "NIFTYNXT50", "NIFTY NEXT 50": "NIFTYNXT50", "NIFTY JUNIOR": "NIFTYNXT50",
}  # fmt: skip
VIX = "INDIA VIX"


@dataclass(slots=True)
class IndexRow:
    day: date
    symbol: str  # F&O symbol for an F&O underlying, else the upper-cased index name ("INDIA VIX")
    index_name: str  # as printed in the file
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    points_change: Decimal | None
    pct_change: Decimal | None
    volume: int | None  # shares traded in the index's constituents
    turnover_cr: Decimal | None  # ₹ crore
    pe: Decimal | None
    pb: Decimal | None
    div_yield: Decimal | None  # percent (1.20 = 1.2 % a year)


def symbol_for(name: str) -> str:
    key = " ".join(name.split()).upper()
    return FNO_SYMBOLS.get(key, key)


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


def _date(v: str) -> date:
    s = v.strip()
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise NseError(f"unreadable index date {s!r}")


def parse(text: str, day: date) -> list[IndexRow]:
    """The file's rows. Raises NseError for anything that is not this file (a block page) or a file dated another
    day; a row with no index name is skipped."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")))
    head = next(reader, None)
    if not head or head[0].strip() != HEADER or len(head) < 13:
        raise NseError("not an NSE index close file (ind_close_all)")
    out: list[IndexRow] = []
    for r in reader:
        if not r or not r[0].strip():
            continue
        r = (r + [""] * 13)[:13]
        d = _date(r[1])
        if d != day:
            raise NseError(f"the index close file for {day} carries another day ({d})")
        vol = _dec(r[8])
        out.append(IndexRow(day=d, symbol=symbol_for(r[0]), index_name=r[0].strip(), open=_dec(r[2]),
                            high=_dec(r[3]), low=_dec(r[4]), close=_dec(r[5]), points_change=_dec(r[6]),
                            pct_change=_dec(r[7]), volume=None if vol is None else int(vol),
                            turnover_cr=_dec(r[9]), pe=_dec(r[10]), pb=_dec(r[11]), div_yield=_dec(r[12])))  # fmt: skip
    return out


async def fetch_day(http: PoliteClient, day: date) -> tuple[str, bytes] | None:
    """(url, body) of the day's file; None on 404. A refusal, a 5xx or a non-CSV 200 raises NseError."""
    url = url_for(day)
    resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "text/csv,*/*"})
    if resp.status == 404:
        return None
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {url}")
    if not resp.content.lstrip(b"\xef\xbb\xbf").startswith(HEADER.encode()) or len(resp.content) > MAX_BYTES:
        raise NseError(f"NSE served something other than the index close CSV for {url}: a block page?")
    return url, resp.content
