"""NSE Clearing's daily SPAN risk-parameter file and FOVOLT volatility file (#370). Read-only public files on
nsearchives.nseindia.com; personal, non-distributed use only (owner decision #362).

SPAN file: `archives/nsccl/span/nsccl.<YYYYMMDD>.s.zip`, one `.spn` entry holding SPAN XML (fileFormat 4.00). The
16-Oct-2026 file is a 9.4 MB zip with 51.7 MB of XML and 141,323 option series; the 23-Mar-2020 and 24-Oct-2008 files
also answered 200 (data check of 20-Oct-2026). It is stream-parsed (ElementTree iterparse, each portfolio cleared once
read: ~2 s and ~32 MB peak RSS for the 2026 file) and never extracted to disk. Layout used here:

- `pointInTime/date` (YYYYMMDD) and `spanFile/created` (YYYYMMDDhhmm);
- `clearingOrg/pointDef/scanPointDef`: the 16 risk scenarios (price move as a multiple of the price scan range, vol
  move as a multiple of the vol scan range, weight). NSCCL's 2026 file: points 1-14 at 0, ±1/3, ±2/3, ±1 range with
  vol ±1 at weight 1, points 15/16 at ±3 ranges, vol unchanged, weight 0.30;
- `exchange/phyPf` per underlying (`pfCode`): `phy/p` the underlying price and `phy/scanRate/priceScan` (rupees) and
  `volScan` (a fraction of volatility: 0.04 = 4 vol points). The price scan range % is priceScan / p: 9.30 % for
  NIFTY (2152.59 / 23146.15) and BANKNIFTY on 16-Oct-2026, 14-20 % for most stocks;
- `exchange/futPf/fut` and `exchange/oopPf/series/opt`: each contract's price `p`, delta `d` and its risk array `ra/a`
  (16 values). Sign convention: each value is the per-unit LOSS of a long position in that scenario (the NIFTY
  23200 PE priced 146.95 shows +146.95 in every price-up scenario);
- `clearingOrg/ccDef` per combined commodity (`cc`): `somTiers/tier/rate/val` (the short-option minimum) and the
  calendar spread charges `dSpread` (spread no, charge method, rate, legs by expiry).

FOVOLT: `archives/nsccl/volt/FOVOLT_<DDMMYYYY>.csv`, NSE's EWMA volatility per F&O underlying:
sigma_t = sqrt(0.995 x sigma_{t-1}^2 + 0.005 x r_t^2), annualised by sqrt(365) (its own column headers).

A missing day answers 404 ("no file"); a non-zip 200 (a block page) or a refusal raises NseError, never "no file".
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import IO, Any
from xml.etree.ElementTree import Element, ParseError, iterparse

from finresearch.adapters.http import PoliteClient
from finresearch.adapters.nse import NSE_BASE, NseError

ARCHIVES = "https://nsearchives.nseindia.com"
URL = ARCHIVES + "/archives/nsccl/span/nsccl.{day:%Y%m%d}.s.zip"
VOLT_URL = ARCHIVES + "/archives/nsccl/volt/FOVOLT_{day:%d%m%Y}.csv"
PARSER_VERSION = "1"  # bump when the parser's output changes for the same file (#368 lineage)
MAX_UNZIPPED = 400 * 1024 * 1024  # the 2026 file is ~52 MB of XML; anything far larger is not a SPAN file
MAX_RATIO = 60  # XML compresses ~5.5x; a far higher ratio is a zip bomb
VOLT_HEADER = "Date"


@dataclass(slots=True)
class SpanParam:
    """One underlying's day: price, scan ranges, short-option minimum and calendar spread charges."""

    symbol: str
    price: Decimal | None
    price_scan: Decimal | None  # rupees per unit
    vol_scan: Decimal | None  # fraction of volatility (0.04 = 4 vol points)
    som_rate: Decimal | None = None  # None: no ccDef / tier in the file (unknown, never 0)
    spreads: list[dict[str, Any]] = field(default_factory=list)

    @property
    def scan_pct(self) -> Decimal | None:
        if self.price is None or self.price_scan is None or self.price <= 0:
            return None
        return self.price_scan / self.price


@dataclass(slots=True)
class SpanFile:
    day: date
    created: str | None
    points: list[dict[str, Any]]
    params: dict[str, SpanParam]


@dataclass(slots=True)
class RiskArray:
    kind: str  # FUT | OPT
    expiry: date
    strike: Decimal | None
    right: str | None  # C | P for options
    price: Decimal | None
    delta: Decimal | None
    values: list[Decimal]  # 16 per-unit losses of a long position
    composite_delta: Decimal | None


def url_for(day: date) -> str:
    return URL.format(day=day)


def volt_url_for(day: date) -> str:
    return VOLT_URL.format(day=day)


def _dec(v: str | None) -> Decimal | None:
    s = (v or "").strip()
    if not s or s == "-":
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _ymd(v: str | None) -> date | None:
    s = (v or "").strip()
    return datetime.strptime(s, "%Y%m%d").date() if len(s) == 8 and s != "00000000" else None


def open_spn(content: bytes) -> IO[bytes]:
    """A stream over the single .spn entry of a SPAN zip. Raises NseError for anything else."""
    if not content.startswith(b"PK\x03\x04"):
        raise NseError("NSE served something other than a zip for the SPAN file")
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
        infos = [i for i in zf.infolist() if i.filename.lower().endswith(".spn")]
        if len(infos) != 1 or len(zf.infolist()) != 1:
            raise NseError("unexpected SPAN zip contents")
        i = infos[0]
        if i.file_size > MAX_UNZIPPED or i.file_size > MAX_RATIO * max(i.compress_size, 1):
            raise NseError("unexpected SPAN zip contents (size)")
        return zf.open(i)
    except zipfile.BadZipFile as e:
        raise NseError("corrupt SPAN zip") from e


def _events(content: bytes) -> Iterator[tuple[str, Element]]:
    try:
        yield from iterparse(open_spn(content), events=("start", "end"))
    except ParseError as e:
        raise NseError(f"unreadable SPAN XML: {e}") from e


def _point(el: Element) -> dict[str, Any]:
    num = lambda p: float(el.findtext(p) or "nan")  # noqa: E731
    return {"point": int(el.findtext("point") or 0), "price_mult": num("priceScanDef/mult"),
            "vol_mult": num("volScanDef/mult"), "weight": num("weight")}  # fmt: skip


def _spreads(cc: Element) -> list[dict[str, Any]]:
    out = []
    for d in cc.findall("dSpread"):
        out.append({"spread": int(d.findtext("spread") or 0), "charge": (d.findtext("chargeMeth") or "").strip(),
                    "rate": (d.findtext("rate/val") or "").strip(),
                    "legs": [[(x.findtext("pe") or "").strip(), (x.findtext("rs") or "").strip(),
                              (x.findtext("i") or "").strip()] for x in d.findall("pLeg")]})  # fmt: skip
    return out


def parse(content: bytes) -> SpanFile:
    """The day's per-underlying parameters (risk arrays are skipped: `risk_arrays`). Raises NseError for a file that
    is not a SPAN file."""
    day: date | None = None
    created: str | None = None
    points: list[dict[str, Any]] = []
    params: dict[str, SpanParam] = {}
    ccs: dict[str, tuple[Decimal | None, list[dict[str, Any]]]] = {}
    root_seen = False
    for ev, el in _events(content):
        if ev == "start":
            if not root_seen:
                if el.tag != "spanFile":
                    raise NseError("not a SPAN file (no spanFile root)")
                root_seen = True
            continue
        tag = el.tag
        if tag == "created" and created is None:
            created = (el.text or "").strip() or None
        elif tag == "date" and day is None:
            day = _ymd(el.text)
        elif tag == "pointDef":
            points = [_point(p) for p in el.findall("scanPointDef")]
            el.clear()
        elif tag == "phyPf":
            code = (el.findtext("pfCode") or "").strip().upper()
            phy = el.find("phy")
            if code and phy is not None:
                params[code] = SpanParam(code, _dec(phy.findtext("p")), _dec(phy.findtext("scanRate/priceScan")),
                                         _dec(phy.findtext("scanRate/volScan")))  # fmt: skip
            el.clear()
        elif tag in ("futPf", "oopPf"):
            el.clear()
        elif tag == "ccDef":
            cc = (el.findtext("cc") or "").strip().upper()
            tiers = el.findall("somTiers/tier")
            ccs[cc] = (_dec(tiers[0].findtext("rate/val")) if tiers else None, _spreads(el))
            el.clear()
    if day is None:
        raise NseError("not a SPAN file (no pointInTime date)")
    for code, (som, spreads) in ccs.items():
        if code in params:
            params[code].som_rate, params[code].spreads = som, spreads
    return SpanFile(day=day, created=created, points=sorted(points, key=lambda p: p["point"]), params=params)


def _array(kind: str, expiry: date, el: Element) -> RiskArray:
    ra = el.find("ra")
    vals = [] if ra is None else [Decimal((a.text or "0").strip()) for a in ra.findall("a")]
    opt = kind == "OPT"
    return RiskArray(kind=kind, expiry=expiry, strike=_dec(el.findtext("k")) if opt else None,
                     right=((el.findtext("o") or "").strip() or None) if opt else None,
                     price=_dec(el.findtext("p")), delta=_dec(el.findtext("d")), values=vals,
                     composite_delta=None if ra is None else _dec(ra.findtext("d")))  # fmt: skip


def risk_arrays(content: bytes, symbol: str) -> list[RiskArray]:
    """Every future's and option's risk array for one underlying (pfCode), in file order."""
    sym = symbol.upper()
    out: list[RiskArray] = []
    for ev, el in _events(content):
        if ev != "end" or el.tag not in ("phyPf", "futPf", "oopPf", "ccDef"):
            continue
        if el.tag in ("futPf", "oopPf") and (el.findtext("pfCode") or "").strip().upper() == sym:
            if el.tag == "futPf":
                for f in el.findall("fut"):
                    if (e := _ymd(f.findtext("pe"))) is not None:
                        out.append(_array("FUT", e, f))
            else:
                for ser in el.findall("series"):
                    if (e := _ymd(ser.findtext("pe"))) is not None:
                        out.extend(_array("OPT", e, o) for o in ser.findall("opt"))
        el.clear()
    return out


@dataclass(slots=True)
class VoltRow:
    """One underlying's FOVOLT row: NSE's EWMA volatilities (daily as fractions; annual = daily x sqrt(365))."""

    day: date
    symbol: str
    underlying_close: Decimal | None
    underlying_prev_close: Decimal | None
    underlying_log_return: Decimal | None
    underlying_prev_vol: Decimal | None
    underlying_daily_vol: Decimal | None
    underlying_annual_vol: Decimal | None
    futures_close: Decimal | None
    futures_daily_vol: Decimal | None
    futures_annual_vol: Decimal | None
    applicable_daily_vol: Decimal | None
    applicable_annual_vol: Decimal | None


def parse_volt(text: str, day: date) -> list[VoltRow]:
    """FOVOLT CSV rows (16 columns by position: NSE's headers carry their formulas). Raises NseError for anything
    else or a file dated another day."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    head = next(reader, None)
    if not head or head[0].strip() != VOLT_HEADER or len(head) < 16 or "Volatility" not in ",".join(head):
        raise NseError("not an NSE FOVOLT file")
    out = []
    for r in reader:
        if len(r) < 16 or not r[1].strip():
            continue
        d = datetime.strptime(r[0].strip(), "%d-%b-%y").date()
        if d != day:
            raise NseError(f"the FOVOLT file for {day} carries another day ({d})")
        v = [_dec(x) for x in r]
        out.append(VoltRow(day=d, symbol=r[1].strip().upper(), underlying_close=v[2], underlying_prev_close=v[3],
                           underlying_log_return=v[4], underlying_prev_vol=v[5], underlying_daily_vol=v[6],
                           underlying_annual_vol=v[7], futures_close=v[8], futures_daily_vol=v[12],
                           futures_annual_vol=v[13], applicable_daily_vol=v[14], applicable_annual_vol=v[15]))  # fmt: skip
    return out


async def _get(http: PoliteClient, url: str) -> bytes | None:
    resp = await http.get(url, headers={"Referer": f"{NSE_BASE}/", "Accept": "*/*"})
    if resp.status == 404:
        return None
    if not resp.ok:
        raise NseError(f"NSE HTTP {resp.status} for {url}")
    return resp.content


async def fetch_day(http: PoliteClient, day: date) -> tuple[str, bytes] | None:
    """(url, zip bytes) of the day's SPAN file; None on 404. A refusal, a 5xx or a non-zip 200 raises NseError."""
    url = url_for(day)
    body = await _get(http, url)
    if body is not None and not body.startswith(b"PK\x03\x04"):
        raise NseError(f"NSE served a non-zip body for {url}: a block page?")
    return None if body is None else (url, body)


async def fetch_volt(http: PoliteClient, day: date) -> tuple[str, str] | None:
    """(url, text) of the day's FOVOLT file; None on 404; anything but the CSV raises NseError."""
    url = volt_url_for(day)
    body = await _get(http, url)
    if body is None:
        return None
    if not body.lstrip(b"\xef\xbb\xbf").startswith(VOLT_HEADER.encode()):
        raise NseError(f"NSE served something other than the FOVOLT CSV for {url}: a block page?")
    return url, body.decode("utf-8", "replace")
