"""FBIL (Financial Benchmarks India Pvt Ltd, https://www.fbil.org.in): the G-sec par yield curve.

FBIL publishes, after each business day, "FBIL GOI Prices including Par Yield": an Excel workbook with a "Par Yield"
sheet of tenors (0.25-year steps) against the par yield, half-yearly compounded (as G-secs pay coupons) and
annualised. The site's own front end reads two endpoints:

* ``/wasdm/gsec/fetch?authenticated=false`` -> the latest published dates, newest first;
* ``/wasdm/gsec/downloadPublished?date=YYYY-MM-DD`` -> that day's workbook, served as ``.xls``: really ``.xlsx`` in
  recent years, a true OLE2 ``.xls`` up to 2022 (the 03-Jan-2022 file); both are read (adapters.xls).

The domain is fbil.org.in. (fbil.co.in is an unrelated bullion dealer.)

No FBIL data is embedded in this public repository (#479: FBIL's Terms of Use forbid redistributing it, §2.1), and no
live path of the app calls this adapter any more: risk-free rates come from signals.rates (owner-entered yields and
the RBI repo-rate proxy) and the bond signal has no G-sec curve source. The client stays, paused, so a future
subscription can re-enable it.

Every request first asks `data_rights.require(DATASET)` and raises FbilError without a recorded decision, so no caller
reaches FBIL unless the owner has decided. FBIL's Terms of Use of 13-Oct-2026 (section 9) limit the free website to
delayed, non-commercial viewing and prohibit building a database, archive or tracking feed from it without a paid
subscription, so the 23-Oct-2026 decision was paused pending the owner (#476).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import pairwise

from finresearch.adapters import xls
from finresearch.adapters.http import PoliteClient

BASE = "https://www.fbil.org.in/wasdm"
LATEST_URL = f"{BASE}/gsec/fetch"
DOWNLOAD_URL = f"{BASE}/gsec/downloadPublished"
SITE = "https://www.fbil.org.in/"
HEADERS = {"Referer": SITE, "Accept": "application/json, text/plain, */*"}
DATASET = "fbil_rates"  # the data-rights entry covering every FBIL request


class FbilError(RuntimeError):
    pass


def _require() -> None:
    from finresearch import data_rights

    try:
        data_rights.require(DATASET)
    except data_rights.NoDecision as e:
        raise FbilError(str(e)) from None


@dataclass(frozen=True)
class ParPoint:
    tenor_years: Decimal
    semi_annual: Decimal  # fraction, compounded half-yearly (0.0666 = 6.66 %)
    annualised: Decimal | None  # fraction, FBIL's own annualised figure


@dataclass(frozen=True)
class ParCurve:
    as_of: date
    points: list[ParPoint]
    source: str = SITE
    fallback: bool = False  # True: FBIL couldn't be reached and this is the dated table below
    notes: list[str] = field(default_factory=list)

    def par_yield(self, years: float) -> Decimal:
        """Half-yearly par yield at `years` to maturity, linear between FBIL's tenors and flat beyond its ends."""
        pts = sorted(self.points, key=lambda p: p.tenor_years)
        if not pts:
            raise FbilError("empty par curve")
        y = Decimal(str(years))
        if y <= pts[0].tenor_years:
            return pts[0].semi_annual
        if y >= pts[-1].tenor_years:
            return pts[-1].semi_annual
        for a, b in pairwise(pts):
            if a.tenor_years <= y <= b.tenor_years:
                w = (y - a.tenor_years) / (b.tenor_years - a.tenor_years)
                return (a.semi_annual + (b.semi_annual - a.semi_annual) * w).quantize(Decimal("1e-8"))
        return pts[-1].semi_annual  # unreachable


def _pct(v: object) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v).strip()) / 100
    except InvalidOperation:
        return None


def _as_of(v: object) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%d-%b-%Y", "%d %b, %Y"):  # .xlsx era / OLE2 .xls era ('03 Jan, 2022')
        try:
            return datetime.strptime(str(v).strip(), fmt).date()
        except ValueError:
            pass
    return None


def parse_par_yield(content: bytes) -> ParCurve:
    """The "Par Yield" sheet of FBIL's G-sec workbook, .xlsx or (up to 2022) OLE2 .xls (adapters.xls): a title row
    with the date ('23-Sep-2026', or '03 Jan, 2022' in the .xls era), then 'Tenor (Year)', 'YTM% p.a.(Semi-Annual)',
    'YTM % p.a.(Annualized)' rows."""
    from finresearch.adapters.xls import NotAWorkbook, read_sheets

    try:
        sheets = read_sheets(content)
    except NotAWorkbook as e:
        raise FbilError(str(e)) from e
    name = next((n for n in sheets if "par" in n.lower()), None)
    if name is None:
        raise FbilError(f"no Par Yield sheet (sheets: {list(sheets)})")
    as_of, points, in_table = None, [], False
    for row in sheets[name]:
        cells = [c for c in row if c is not None and str(c).strip() != ""]
        if not cells:
            continue
        first = str(cells[0]).strip().lower()
        if as_of is None and "par yield" in first and len(cells) > 1:
            as_of = _as_of(cells[1])
            continue
        if first.startswith("tenor"):
            in_table = True
            continue
        if in_table and len(cells) >= 2:
            try:
                tenor = Decimal(str(cells[0]))
            except InvalidOperation:
                continue
            semi = _pct(cells[1])
            if semi is not None:
                points.append(ParPoint(tenor, semi, _pct(cells[2]) if len(cells) > 2 else None))
    if as_of is None or len(points) < 2:
        raise FbilError("could not read the par yield table")
    return ParCurve(as_of=as_of, points=points)


parse_par_yield_xlsx = parse_par_yield  # the earlier name (callers and tests)


class FbilClient:
    def __init__(self, client: PoliteClient | None = None):
        self._own = client is None
        self.http = client or PoliteClient()

    async def __aenter__(self) -> FbilClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own:
            await self.http.aclose()

    async def _get(self, url: str, **kw):
        _require()  # no decision, no request (FBIL Terms of Use, 13-Oct-2026)
        return await self.http.get(url, **kw)

    async def published_dates(self) -> list[date]:
        resp = await self._get(LATEST_URL, params={"authenticated": "false"}, headers=HEADERS, cache_ttl=3600,
                                   cache_if=lambda f: f.content.lstrip()[:1] == b"[")  # fmt: skip
        if not resp.ok:
            raise FbilError(f"FBIL HTTP {resp.status} for {LATEST_URL}")
        days = [_iso(r.get("processRunDate")) for r in resp.json() if isinstance(r, dict)]
        return sorted((d for d in days if d), reverse=True)

    async def workbook(self, day: date, *, cache_ttl: float | None = None) -> bytes:
        """`day`'s G-sec workbook as downloaded (.xlsx or OLE2 .xls). Not cached unless `cache_ttl` is given: the
        backfill reads ~0.73 MB a day and discards it after parsing."""
        resp = await self._get(DOWNLOAD_URL, params={"date": day.isoformat()}, headers=HEADERS,
                                   cache_ttl=cache_ttl, cache_if=lambda f: xls.kind(f.content) is not None)  # fmt: skip
        if not resp.ok or xls.kind(resp.content) is None:
            raise FbilError(f"FBIL has no G-sec workbook for {day} (HTTP {resp.status})")
        return resp.content

    async def par_curve(self, day: date) -> ParCurve:
        curve = parse_par_yield(await self.workbook(day, cache_ttl=30 * 86400))
        return ParCurve(
            as_of=curve.as_of, points=curve.points, source=f"{DOWNLOAD_URL}?date={day.isoformat()}"
        )

    async def _filtered(self, path: str, start: date, end: date) -> tuple[str, bytes, object]:
        url = f"{BASE}/{path}/fetchfiltered"
        params = {"fromDate": start.isoformat(), "toDate": end.isoformat(), "authenticated": "false"}
        resp = await self._get(url, params=params, headers=HEADERS)
        if not resp.ok or resp.content.lstrip()[:1] != b"[":
            raise FbilError(f"FBIL HTTP {resp.status} for {url} (not a JSON list)")
        try:
            data = resp.json()
        except ValueError as e:
            raise FbilError(f"FBIL {path}: unreadable JSON") from e
        return f"{url}?fromDate={start.isoformat()}&toDate={end.isoformat()}", resp.content, data

    async def fixings(self, series: str, start: date, end: date) -> tuple[str, bytes, list[RateFixing]]:
        """One series' fixings for [start, end]: (url, body, rows)."""
        url, body, data = await self._filtered(FIXING_PATHS[series], start, end)
        return url, body, parse_fixings(series, data)

    async def gsec_dates(self, start: date, end: date) -> list[date]:
        """The days FBIL published a G-sec workbook in [start, end], oldest first."""
        _, _, data = await self._filtered("gsec", start, end)
        if not isinstance(data, list):
            raise FbilError("FBIL gsec: expected a list")
        return sorted({d for r in data if isinstance(r, dict) and (d := _iso(r.get("processRunDate")))})

    async def latest_par_curve(self) -> ParCurve:
        days = await self.published_dates()
        if not days:
            raise FbilError("FBIL lists no published G-sec dates")
        return await self.par_curve(days[0])


# FBIL's money-market fixings, read as JSON from the same endpoints its site's tables use (no workbook):
# wasdm/<path>/fetchfiltered?fromDate&toDate&authenticated=false, one row per day and tenor, the rate in percent.
# History seen on 22-Oct-2026: overnight MIBOR from 22-Jul-2015, term MIBOR from 23-Sep-2015, T-bills from 2018.
# The public pages showed fixings up to about a week before the request day (14-Oct on 22-Oct-2026).
FIXING_PATHS = {"tbill": "tbill", "mibor_on": "ovnmibor", "mibor_term": "termmibor"}
_TENOR = re.compile(r"^(\d+)\s*(D|DAY|DAYS|M|MONTH|MONTHS)$")


@dataclass(frozen=True)
class RateFixing:
    day: date
    series: str  # tbill | mibor_on | mibor_term
    tenor: str  # canonical: ON, 3D, 7D, 14D, 1M ... 12M
    tenor_days: int  # months x 364/12 rounded (T-bills run 91/182/364 days); ON = 1
    rate: Decimal  # fraction, as FBIL publishes it (percent / 100); its compounding basis is the series' own
    published_at: datetime | None  # FBIL's display time (IST)


def _tenor(label: object) -> tuple[str, int]:
    s = str(label or "").strip().upper()
    if s in ("O/N", "ON", "OVERNIGHT"):
        return "ON", 1
    m = _TENOR.match(s)
    if not m:
        raise FbilError(f"unknown FBIL tenor {label!r}")
    n = int(m.group(1))
    if m.group(2).startswith("D"):
        return f"{n}D", n
    return f"{n}M", round(n * 364 / 12)


def _dt(v: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).strip())
    except ValueError:
        return None


def parse_fixings(series: str, payload: object) -> list[RateFixing]:
    """FBIL's fetchfiltered rows for one series. A row without a numeric rate is left out (unknown, never 0); an
    unknown tenor label or a payload that is not a list raises FbilError (the layout changed: stop, don't guess)."""
    if series not in FIXING_PATHS:
        raise ValueError(f"unknown FBIL series {series!r}")
    if not isinstance(payload, list):
        raise FbilError(f"FBIL {series}: expected a list, got {type(payload).__name__}")
    out = []
    for r in payload:
        if not isinstance(r, dict):
            raise FbilError(f"FBIL {series}: unexpected row {r!r}"[:200])
        day = _iso(r.get("processRunDate"))
        tenor, days = _tenor(r.get("tenorName") or r.get("tenor"))
        rate = r.get("rate")
        if day is None or isinstance(rate, bool) or not isinstance(rate, int | float | str):
            continue
        try:
            frac = Decimal(str(rate)) / 100
        except InvalidOperation:
            continue
        if not frac.is_finite():
            continue
        out.append(RateFixing(day, series, tenor, days, frac, _dt(r.get("displayTime"))))
    return out


def _iso(v: object) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None
