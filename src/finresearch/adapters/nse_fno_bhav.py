"""NSE F&O end-of-day bhavcopy (every futures and options contract's OHLC, settlement price, open interest and volume)
for the historical options backbone (#306). Read-only public files; nothing here places or routes orders.

Two formats, both zipped CSVs on nsearchives.nseindia.com (verified by direct fetch on 12-Oct-2026):

- UDiFF (NSE's "unified distilled file format"), `content/fo/BhavCopy_NSE_FO_0_0_0_<YYYYMMDD>_F_0000.csv.zip`.
  Header `TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,
  OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,
  ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1..4`. FinInstrmTp is IDF/IDO/STF/
  STO (index/stock futures/options); a future has an empty strike and option type. Served from 01-Jan-2024 (20231229
  answered 404, 20240101 200); the sole format from 08-Jul-2024 (jugaad-data's `udiff_start_date`,
  https://github.com/jugaad-py/jugaad-data/blob/master/jugaad_data/nse/archives.py).
- Legacy, `content/historical/DERIVATIVES/<YYYY>/<MMM>/fo<DD><MMM><YYYY>bhav.csv.zip`. Header `INSTRUMENT,SYMBOL,
  EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,` (a
  trailing comma); INSTRUMENT FUTIDX/OPTIDX/FUTSTK/OPTSTK, futures with strike 0 and type XX, dates 25-Jul-2024 /
  05-JUL-2024, turnover in lakh. Last served for 05-Jul-2024 (08-Jul-2024 answered 404). It has no underlying price and
  no lot size: those stay None (unknown), never 0.

Units (checked on 30-Sep-2026, NIFTY 28-Dec-2028 24000 CE: 2 contracts x lot 65 = 130 units; turnover 35,18,446.75 =
130 x (24,000 + 3,065)): TtlTradgVol / CONTRACTS count contracts, OpnIntrst / OPEN_INT count units (shares or index
units, 2,665 = 41 lots x 65). A contract that did not trade keeps a stale close and gets NSE's theoretical settlement
price, so implied volatility is fitted on traded contracts only (fincalc.fno_history).

Holidays and not-yet-published days answer HTTP 404 with a ~3.4 KB HTML page on both routes (2-Oct-2026,
26-Jan-2022). HEAD is refused (503), so files are fetched with GET. Publication time: the 30-Sep-2026 zip entry is
stamped 17:56 IST, Last-Modified 18:59 IST (5-Jul-2024) and 19:17 IST (29-Sep-2026); one day (4-Jan-2024) was
re-uploaded the next afternoon. The nightly job runs from 19:30 IST and re-tries missing days (monitor.fno_bhav).
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseError

ARCHIVES = "https://nsearchives.nseindia.com"
UDIFF_URL = ARCHIVES + "/content/fo/BhavCopy_NSE_FO_0_0_0_{day:%Y%m%d}_F_0000.csv.zip"
LEGACY_URL = ARCHIVES + "/content/historical/DERIVATIVES/{y}/{m}/fo{d:02d}{m}{y}bhav.csv.zip"
UDIFF_FROM = date(2024, 1, 1)  # first day NSE serves a UDiFF F&O file (probed 12-Oct-2026)
LEGACY_UNTIL = date(2024, 7, 5)  # last day the legacy file exists (08-Jul-2024 onward: UDiFF only)
MAX_UNZIPPED = 120 * 1024 * 1024  # a day's CSV is ~6.4 MB; anything far larger is not a bhavcopy
LEGACY_TYPES = {"FUTIDX": "IDF", "OPTIDX": "IDO", "FUTSTK": "STF", "OPTSTK": "STO"}
INSTRUMENTS = ("IDF", "IDO", "STF", "STO")
Format = Literal["udiff", "legacy"]


# bump when the parsers' output changes for the same file (#368 lineage: dataset_load.parser_version)
PARSER_VERSION = "1"


@dataclass(slots=True)
class FoRow:
    """One contract's day. `right` is CE/PE for options, XX for futures (strike 0)."""

    day: date
    symbol: str
    instrument: str  # IDF / IDO / STF / STO
    expiry: date
    strike: Decimal
    right: str
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    settle: Decimal | None
    oi: int | None  # units
    chg_oi: int | None  # units
    contracts: int | None  # contracts traded
    turnover: Decimal | None  # rupees
    underlying: Decimal | None  # UDiFF only
    lot_size: int | None  # UDiFF only

    @property
    def is_future(self) -> bool:
        return self.instrument in ("IDF", "STF")


@dataclass(slots=True)
class Bhavcopy:
    day: date
    format: Format
    url: str
    rows: list[FoRow]
    total: int  # rows in the file before filtering


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


def _keep_all(instrument: str, symbol: str) -> bool:
    return True


def parse_udiff(text: str, keep=_keep_all) -> tuple[list[FoRow], int]:
    """UDiFF CSV -> (rows that `keep(instrument, symbol)` accepts, rows in the file)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or reader.fieldnames[0] != "TradDt" or "SttlmPric" not in reader.fieldnames:
        raise NseError("not an NSE UDiFF F&O bhavcopy")
    out, total = [], 0
    for r in reader:
        total += 1
        kind, sym = (r.get("FinInstrmTp") or "").strip(), (r.get("TckrSymb") or "").strip().upper()
        if kind not in INSTRUMENTS or not keep(kind, sym):
            continue
        fut = kind in ("IDF", "STF")
        right = "XX" if fut else (r.get("OptnTp") or "").strip().upper()
        if right not in ("CE", "PE", "XX"):
            continue
        out.append(FoRow(day=date.fromisoformat(r["TradDt"].strip()), symbol=sym, instrument=kind,
                         expiry=date.fromisoformat(r["XpryDt"].strip()),
                         strike=Decimal(0) if fut else (_dec(r.get("StrkPric")) or Decimal(0)), right=right,
                         open=_dec(r.get("OpnPric")), high=_dec(r.get("HghPric")), low=_dec(r.get("LwPric")),
                         close=_dec(r.get("ClsPric")), settle=_dec(r.get("SttlmPric")), oi=_int(r.get("OpnIntrst")),
                         chg_oi=_int(r.get("ChngInOpnIntrst")), contracts=_int(r.get("TtlTradgVol")),
                         turnover=_dec(r.get("TtlTrfVal")), underlying=_dec(r.get("UndrlygPric")),
                         lot_size=_int(r.get("NewBrdLotQty"))))  # fmt: skip
    return out, total


def _legacy_date(v: str) -> date:
    return datetime.strptime(v.strip().title(), "%d-%b-%Y").date()


def parse_legacy(text: str, keep=_keep_all) -> tuple[list[FoRow], int]:
    """Legacy fo<DDMMMYYYY>bhav CSV -> (kept rows, rows in the file). Turnover lakh -> rupees; no underlying or lot."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames or reader.fieldnames[0] != "INSTRUMENT" or "SETTLE_PR" not in reader.fieldnames:
        raise NseError("not an NSE legacy F&O bhavcopy")
    out, total = [], 0
    for r in reader:
        total += 1
        kind, sym = (
            LEGACY_TYPES.get((r.get("INSTRUMENT") or "").strip()),
            (r.get("SYMBOL") or "").strip().upper(),
        )
        if kind is None or not keep(kind, sym):
            continue
        fut = kind in ("IDF", "STF")
        right = "XX" if fut else (r.get("OPTION_TYP") or "").strip().upper()
        if right not in ("CE", "PE", "XX"):
            continue
        lakh = _dec(r.get("VAL_INLAKH"))
        out.append(FoRow(day=_legacy_date(r["TIMESTAMP"]), symbol=sym, instrument=kind,
                         expiry=_legacy_date(r["EXPIRY_DT"]),
                         strike=Decimal(0) if fut else (_dec(r.get("STRIKE_PR")) or Decimal(0)), right=right,
                         open=_dec(r.get("OPEN")), high=_dec(r.get("HIGH")), low=_dec(r.get("LOW")),
                         close=_dec(r.get("CLOSE")), settle=_dec(r.get("SETTLE_PR")), oi=_int(r.get("OPEN_INT")),
                         chg_oi=_int(r.get("CHG_IN_OI")), contracts=_int(r.get("CONTRACTS")),
                         turnover=None if lakh is None else lakh * 100000, underlying=None, lot_size=None))  # fmt: skip
    return out, total


def unzip_csv(content: bytes) -> str:
    """The single CSV inside a bhavcopy zip. Raises NseError for anything else (block page, bomb, empty zip)."""
    if not content.startswith(b"PK\x03\x04"):
        raise NseError("NSE served something other than a zip for the F&O bhavcopy")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            infos = [i for i in zf.infolist() if i.filename.lower().endswith(".csv")]
            if len(infos) != 1 or infos[0].file_size > MAX_UNZIPPED:
                raise NseError("unexpected F&O bhavcopy zip contents")
            return zf.read(infos[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile as e:
        raise NseError("corrupt F&O bhavcopy zip") from e


def url_for(day: date, fmt: Format) -> str:
    if fmt == "udiff":
        return UDIFF_URL.format(day=day)
    return LEGACY_URL.format(y=day.year, m=day.strftime("%b").upper(), d=day.day)


def formats_for(day: date) -> list[Format]:
    """Routes to try, in order: UDiFF (richer: underlying and lot size) from 2024, legacy before or as a fallback."""
    if day < UDIFF_FROM:
        return ["legacy"]
    return ["udiff", "legacy"] if day <= LEGACY_UNTIL else ["udiff"]


def parse(content: bytes, fmt: Format, keep=_keep_all) -> tuple[list[FoRow], int]:
    text = unzip_csv(content)
    return parse_udiff(text, keep) if fmt == "udiff" else parse_legacy(text, keep)


async def fetch_day(http: PoliteClient, day: date) -> tuple[Format, str, bytes] | None:
    """The day's zip as (format, url, bytes); None when every route answers 404 (a holiday, a weekend, or not yet
    published). A refusal, a 5xx or a non-zip 200 raises NseError: never read as "no file". GET only (HEAD is 503)."""
    for fmt in formats_for(day):
        url = url_for(day, fmt)
        resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "*/*"})
        if resp.status == 404:
            continue
        if not resp.ok:
            raise NseError(f"NSE HTTP {resp.status} for {url}")
        if not resp.content.startswith(b"PK\x03\x04"):
            raise NseError(f"NSE served a non-zip body (HTTP {resp.status}) for {url}: a block page?")
        return fmt, url, resp.content
    return None
