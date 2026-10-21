"""IIMA's Indian Fama-French-Momentum data library: daily market, size, value and momentum factor returns (#348).

Source: Agarwalla, S. K., Jacob, J. and Varma, J. R. (2013), "Four factor model in Indian equities market", Working
Paper W.P. No. 2013-09-05, Indian Institute of Management Ahmedabad; data library at
https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/ (the old `/~iffm/` address answers 301). Built by the
authors from CMIE Prowess. The page asks users to cite the working paper above ("Please cite the source of the data as
follows") and warns that "The values may undergo a change if CMIE revises its database retrospectively".

Terms: read 22-Oct-2026, the page and its FAQ state no licence, terms of use or redistribution permission, only the
citation request. So the data is used here for personal analysis only, cited wherever it is shown, cached in the
gitignored data/ folder, and never committed, published or shared (tests use hand-made synthetic files).

The file read is the newest release's `<YYYY-MM>_FourFactors_and_Market_Returns_Daily_SurvivorshipBiasAdjusted.csv`
(the page recommends the survivorship-bias-adjusted series). Columns `Date,SMB,HML,WML,MF,RF`, values in percent
holding-period returns per trading day (checked 22-Oct-2026: compounding the 2023 and 2024 daily rows reproduces the
yearly file's rows to the third decimal). `MF` is the market risk premium Rm - Rf (working paper §5.3: the
value-weighted market portfolio less the 91-day T-bill rate; the page's table calls it "Market premium %"), and `RF`
is the 91-day T-bill rate per trading day. `NA` marks a day without a figure (the first day of the series).
Releases come a few times a year (the page: "three releases every year"), so the series ends months before today:
callers must say where it ends.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from finresearch.adapters.http import PoliteClient

BASE = "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/"
CITATION = ("Agarwalla, Jacob and Varma (2013), 'Four factor model in Indian equities market', IIM Ahmedabad W.P. "
            "2013-09-05; data: IIMA Indian Fama-French-Momentum data library")  # fmt: skip
TERMS = ("The library states no licence or terms beyond a citation request: used here for personal analysis only, "
         "never redistributed.")  # fmt: skip
HEADER = ["Date", "SMB", "HML", "WML", "MF", "RF"]
INDEX_TTL_S = 7 * 86400  # a new release shows up within a week
FILE_TTL_S = 120 * 86400  # one URL per release; the next release has a new name
_LINK = re.compile(r"""href=["']?((?:\./)?DATA/(\d{4}-\d{2})_FourFactors_and_Market_Returns_Daily_"""
                   r"""SurvivorshipBiasAdjusted\.csv)""")  # fmt: skip


class IimaError(RuntimeError):
    pass


@dataclass(frozen=True)
class FactorDay:
    mf: float  # market premium Rm - Rf, fraction per day
    smb: float
    hml: float
    wml: float
    rf: float  # risk-free (91-day T-bill), fraction per day


@dataclass
class FactorSeries:
    release: str  # "2025-12"
    url: str
    rows: dict[date, FactorDay]
    skipped: list[date] = field(default_factory=list)  # days with an NA cell
    fetched_at: datetime | None = None

    @property
    def first(self) -> date:
        return min(self.rows)

    @property
    def last(self) -> date:
        return max(self.rows)


def latest_daily_link(html: str) -> tuple[str, str]:
    """(absolute URL, release) of the newest survivorship-bias-adjusted daily file linked from the index page."""
    found = [(rel, href) for href, rel in _LINK.findall(html)]
    if not found:
        raise IimaError("no daily factor file is linked from the IIMA data library page")
    rel, href = max(found)
    return BASE + href.removeprefix("./"), rel


def parse_daily(text: str, *, release: str, url: str) -> FactorSeries:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows or [c.strip() for c in rows[0]] != HEADER:
        raise IimaError("not the IIMA daily factor file (unexpected header)")
    out: dict[date, FactorDay] = {}
    skipped: list[date] = []
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        if len(r) != len(HEADER):
            raise IimaError(f"malformed row {r[0]!r}: {len(r)} cells")
        try:
            d = date.fromisoformat(r[0].strip())
        except ValueError as e:
            raise IimaError(f"bad date {r[0]!r}") from e
        cells = [c.strip() for c in r[1:]]
        if any(c.upper() == "NA" or c == "" for c in cells):
            skipped.append(d)
            continue
        smb, hml, wml, mf, rf = (float(c) / 100 for c in cells)
        out[d] = FactorDay(mf=mf, smb=smb, hml=hml, wml=wml, rf=rf)
    if not out:
        raise IimaError("the IIMA factor file has no rows")
    return FactorSeries(release=release, url=url, rows=out, skipped=skipped)


def _is_factor_file(f) -> bool:
    return f.content.lstrip(b"\xef\xbb\xbf").startswith(b"Date,SMB,HML,WML,MF,RF")


class IimaFactorsClient:
    def __init__(self, client: PoliteClient | None = None):
        self._own = client is None
        self.http = client or PoliteClient()

    async def __aenter__(self) -> IimaFactorsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own:
            await self.http.aclose()

    async def latest_daily(self) -> FactorSeries:
        page = await self.http.get(BASE, cache_ttl=INDEX_TTL_S,
                                   cache_if=lambda f: b"FourFactors_and_Market_Returns" in f.content)  # fmt: skip
        if not page.ok:
            raise IimaError(f"IIMA data library HTTP {page.status}")
        url, release = latest_daily_link(page.text)
        got = await self.http.get(url, cache_ttl=FILE_TTL_S, cache_if=_is_factor_file)
        if not got.ok:
            raise IimaError(f"IIMA factor file HTTP {got.status}")
        s = parse_daily(got.text, release=release, url=url)
        s.fetched_at = got.record.fetched_at
        return s
