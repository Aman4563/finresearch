"""AMFI (Association of Mutual Funds in India) NAV data: the primary source for Indian mutual-fund NAVs.

* NAVAll.txt: the latest NAV of every scheme, grouped under category headings ("Open Ended Schemes(Equity Scheme -
  Mid Cap Fund)") and AMC headings ("Axis Mutual Fund").
* NAV history report: NAVs for a date range, for one AMC (`mf` code) or all AMCs.

AMFI does not publish its AMC codes, so they are discovered once by asking for a single day per code and reading
the AMC heading, then cached. A one-day history request for all AMCs gives every scheme's NAV on that day, which is
how category-peer returns are computed without downloading each peer's full history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from finresearch.adapters.http import PoliteClient

NAV_ALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HISTORY_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
MAX_AMC_CODE = 90


class AmfiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SchemeNav:
    code: str
    name: str
    plan: str | None
    option: str | None
    isin_growth: str | None
    isin_reinvest: str | None
    nav: Decimal | None
    day: date | None
    category: str | None
    amc: str | None

    @property
    def is_direct_growth(self) -> bool:
        return "direct" in (self.plan or self.name).lower() and "growth" in (self.option or self.name).lower()


def _dec(v: str) -> Decimal | None:
    try:
        return Decimal(v.strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _day(v: str) -> date | None:
    try:
        return datetime.strptime(v.strip(), "%d-%b-%Y").date()
    except ValueError:
        return None


def _category(line: str) -> str | None:
    """'Open Ended Schemes(Equity Scheme - Mid Cap Fund)' -> 'Equity Scheme - Mid Cap Fund'."""
    if "(" in line and line.rstrip().endswith(")") and "scheme" in line.lower().split("(")[0]:
        return line[line.index("(") + 1 : line.rindex(")")].strip()
    return None


def _parse(text: str, history: bool) -> list[SchemeNav]:
    out: list[SchemeNav] = []
    category = amc = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("scheme code"):
            continue
        if ";" not in line:
            cat = _category(line)
            if cat is not None:
                category = cat
            else:
                amc = line
            continue
        f = [x.strip() for x in line.split(";")]
        if history:  # Scheme Code;NAV Name;Plan;Option;ISIN Growth;ISIN Reinvest;NAV;Date
            if len(f) < 8:
                continue
            code, name, plan, option, g, r, nav, day = f[:8]
        else:  # Scheme Code;ISIN Growth;ISIN Reinvest;Scheme Name;Plan;Option;NAV;Date
            if len(f) < 8:
                continue
            code, g, r, name, plan, option, nav, day = f[:8]
        out.append(SchemeNav(code=code, name=name, plan=plan or None, option=option or None,
                             isin_growth=None if g in ("", "-") else g, isin_reinvest=None if r in ("", "-") else r,
                             nav=_dec(nav), day=_day(day), category=category, amc=amc))  # fmt: skip
    return out


def parse_nav_all(text: str) -> list[SchemeNav]:
    return _parse(text, history=False)


def parse_nav_history(text: str) -> list[SchemeNav]:
    return _parse(text, history=True)


def search_schemes(schemes: list[SchemeNav], query: str, limit: int = 20) -> list[SchemeNav]:
    """Code match first, then names containing every word; direct-growth variants first."""
    q = query.strip().lower()
    if not q:
        return []
    words = q.split()
    hits = [
        s for s in schemes if s.code == q or all(w in f"{s.name} {s.plan} {s.option}".lower() for w in words)
    ]
    return sorted(hits, key=lambda s: (s.code != q, not s.is_direct_growth, s.name))[:limit]


class AmfiClient:
    def __init__(self, client: PoliteClient | None = None, *, cache_dir: Path | None = None):
        self._own = client is None
        self.http = client or PoliteClient()
        self.cache_dir = cache_dir

    async def __aenter__(self) -> AmfiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own:
            await self.http.aclose()

    async def _text(self, url: str, params: dict[str, str] | None = None) -> str:
        resp = await self.http.get(url, params=params)
        if not resp.ok:
            raise AmfiError(f"AMFI HTTP {resp.status} for {url}")
        return resp.content.decode("utf-8", "replace")

    async def nav_all(self) -> list[SchemeNav]:
        return parse_nav_all(await self._text(NAV_ALL_URL))

    async def history(self, start: date, end: date, amc_code: int | None = None) -> list[SchemeNav]:
        params = {"frmdt": start.strftime("%d-%b-%Y"), "todt": end.strftime("%d-%b-%Y")}
        if amc_code is not None:
            params["mf"] = str(amc_code)
        return parse_nav_history(await self._text(HISTORY_URL, params))

    async def navs_on(self, day: date, lookback_days: int = 7) -> dict[str, SchemeNav]:
        """Every scheme's latest NAV on or before `day` (a holiday falls back to the previous NAV date)."""
        rows = await self.history(day - timedelta(days=lookback_days), day)
        latest: dict[str, SchemeNav] = {}
        for r in rows:
            if r.day and r.day <= day and (r.code not in latest or r.day > latest[r.code].day):
                latest[r.code] = r
        return latest

    async def amc_codes(self, probe_day: date) -> dict[str, int]:
        """AMC name -> AMFI `mf` code, discovered by asking for one day per code (cached in cache_dir)."""
        cache = self.cache_dir / "amfi_amc_codes.json" if self.cache_dir else None
        if cache and cache.exists():
            return json.loads(cache.read_text())
        codes: dict[str, int] = {}
        for code in range(1, MAX_AMC_CODE + 1):
            rows = await self.history(probe_day, probe_day, amc_code=code)
            amc = next((r.amc for r in rows if r.amc), None)
            if amc:
                codes[amc] = code
        if cache and codes:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(codes, indent=1, sort_keys=True))
        return codes

    async def scheme_history(
        self, scheme: SchemeNav, start: date, end: date, probe_day: date
    ) -> list[SchemeNav]:
        codes = await self.amc_codes(probe_day)
        if scheme.amc not in codes:
            raise AmfiError(f"no AMFI code found for {scheme.amc!r}")
        rows = await self.history(start, end, amc_code=codes[scheme.amc])
        return sorted((r for r in rows if r.code == scheme.code and r.nav is not None), key=lambda r: r.day)
