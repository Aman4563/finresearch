"""NSE Clearing's combined open interest file (combineoi) and NSE's dated F&O ban notice (P1.12, #374). Read-only
public archive files, one per dataset per day; nothing here places or routes orders.

- combineoi: `archives/nsccl/mwpl/combineoi_<DDMMYYYY>.zip`, one CSV per stock in F&O with its ISIN, the
  market-wide position limit (MWPL) and the combined open interest across exchanges at the end of the day. The roadmap
  data check (#374) found the files from at least Mar-2020. [U: the path and the header `Date,ISIN,Scrip Name,NSE
  Symbol,MWPL,Open Interest,Limit for Next Day` are as commonly described, NOT verified by a fetch in this change
  (no data-rights decision exists for the dataset yet); the owner's one-file probe in the PR verifies both before any
  backfill.] Columns are therefore matched by name, a "Future Equivalent Open Interest" column (SEBI's delta-based
  OI, circular SEBI/HO/MRD/TPD-1/P/CIR/2025/79) is preferred when present and recorded as `oi_basis`, and a body
  without the expected columns is refused, never read as "no stock".
- Ban notice: `archives/fo/sec_ban/fo_secban_<DDMMYYYY>.csv` for trade day DDMMYYYY (the path RuchiTanmay/nselib's
  `fno_security_in_ban_period` uses, https://github.com/RuchiTanmay/nselib, nselib/derivatives/derivative_data.py),
  in the same layout as the live `content/fo/fo_secban.csv` (adapters.nse_disclosures.parse_fno_ban). Used only for
  the 20-day validation of the derived ban history.

A 404 is a holiday or a day not yet published (None). A refusal or 5xx raises NseError with "HTTP <status>" (the
backfills stop on repeated 403/429, refseries.index_eod.refused); a 200 that is not the expected body (a block page)
raises too.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseError
from finresearch.adapters.nse_disclosures import FnoBan, parse_day, parse_fno_ban

ARCHIVES = "https://nsearchives.nseindia.com"
COMBINEOI_URL = ARCHIVES + "/archives/nsccl/mwpl/combineoi_{d:%d%m%Y}.zip"
SECBAN_URL = ARCHIVES + "/archives/fo/sec_ban/fo_secban_{d:%d%m%Y}.csv"
MAX_UNZIPPED = 5 * 1024 * 1024  # a day's CSV is a few hundred rows
PARSER_VERSION = "1"
FUT_EQ = "future equivalent open interest"


@dataclass(frozen=True)
class MwplRow:
    symbol: str
    isin: str | None
    mwpl: int | None
    oi: int | None
    limit_next: int | None
    utilisation: Decimal | None  # oi / mwpl; None when either is unknown or MWPL is not positive


@dataclass(frozen=True)
class MwplDay:
    day: date | None
    oi_basis: str  # the OI column read, as printed
    rows: list[MwplRow]


def combineoi_url(day: date) -> str:
    return COMBINEOI_URL.format(d=day)


def secban_url(day: date) -> str:
    return SECBAN_URL.format(d=day)


def _num(v: str | None) -> int | None:
    s = (v or "").replace(",", "").strip()
    if not s or s == "-":
        return None
    try:
        return int(Decimal(s))
    except InvalidOperation:
        return None


def unzip(content: bytes) -> str:
    if not content.startswith(b"PK\x03\x04"):
        raise NseError("NSE served something other than a zip for the combined OI file")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = [i for i in zf.infolist() if i.filename.lower().endswith(".csv")]
            if len(infos) != 1 or infos[0].file_size > MAX_UNZIPPED:
                raise NseError("unexpected combined OI zip contents")
            return zf.read(infos[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile as e:
        raise NseError("corrupt combined OI zip") from e


def parse_combineoi(text: str, day: date) -> MwplDay:
    """The day's rows. Raises NseError for a body without the symbol, MWPL and OI columns, or dated another day."""
    reader = csv.DictReader(io.StringIO((text or "").lstrip("﻿")))
    names = {(h or "").strip().lower(): h for h in (reader.fieldnames or [])}
    sym = names.get("nse symbol") or names.get("symbol")
    mwpl = names.get("mwpl")
    oi = names.get(FUT_EQ) or names.get("open interest")
    if not (sym and mwpl and oi):
        raise NseError("not NSE's combined OI file (no symbol, MWPL or open interest column): a block page?")
    dcol, isin, lim = names.get("date"), names.get("isin"), names.get("limit for next day")
    rows, seen = [], None
    for r in reader:
        s = (r.get(sym) or "").strip().upper()
        if not s:
            continue
        if dcol and (d := parse_day(r.get(dcol))) is not None:
            seen = seen or d
            if d != day:
                raise NseError(f"the combined OI file for {day} is dated another day ({d})")
        m, o = _num(r.get(mwpl)), _num(r.get(oi))
        u = Decimal(o) / Decimal(m) if m and m > 0 and o is not None else None
        rows.append(MwplRow(s, (r.get(isin) or "").strip() or None if isin else None, m if m and m > 0 else None,
                            o, _num(r.get(lim)) if lim else None, u))  # fmt: skip
    if not rows:
        raise NseError("the combined OI file has no rows")
    return MwplDay(seen, oi.strip(), rows)


async def _get(http: PoliteClient, url: str):
    resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "*/*"})
    if resp.status == 404:
        return None
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {url}")
    return resp


async def fetch_combineoi(http: PoliteClient, day: date) -> tuple[str, bytes] | None:
    """(url, zip as served); None on 404. GET only, through the shared NSE budget."""
    url = combineoi_url(day)
    resp = await _get(http, url)
    if resp is None:
        return None
    if not resp.content.startswith(b"PK\x03\x04"):
        raise NseError(f"NSE served a non-zip body (HTTP {resp.status}) for {url}: a block page?")
    return url, resp.content


async def fetch_ban_notice(http: PoliteClient, day: date) -> tuple[str, bytes, FnoBan] | None:
    """(url, body, parsed notice) for trade day `day`; None on 404. A body without the notice header raises."""
    url = secban_url(day)
    resp = await _get(http, url)
    if resp is None:
        return None
    ban = parse_fno_ban(resp.text)
    if ban.trade_date != day:
        raise NseError(f"the ban notice at {url} is for trade date {ban.trade_date}, not {day}")
    return url, resp.content, ban
