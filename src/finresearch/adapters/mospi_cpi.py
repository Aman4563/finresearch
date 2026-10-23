"""MoSPI's all-India Consumer Price Index (Combined) for the goal planner (#347): general, health and education.

Source: the Ministry of Statistics and Programme Implementation's CPI API, https://api.mospi.gov.in/ (documented at
https://api.mospi.gov.in/cpi, swagger `layout/swagger_user_cpi.yaml`; the same API backs MoSPI's eSankhyiki portal).
Two series, both read 22-Oct-2026:
- base 2012=100, `/api/cpi/getCPIIndex` (base_year=2012, series=Current, state_code=99 all India, sector_code=3
  Combined): monthly indices Jan-2013..Dec-2025, subgroups General-Overall (0.99), Health (6.1.02), Education
  (6.1.05). `status` is F (final) or F* (imputed: April/May 2020, the lockdown months, which also have no
  year-on-year rate).
- base 2024=100, `/api/cpi/getCPIData` (base_year=2024, state_code=1 all India, sector_code=3): from the
  January-2026 index (MoSPI/PIB release of 12-Feb-2026: "the inflation data of the CPI 2012 series is available up
  to December 2025"), divisions 0 CPI (General), 06 Health, 10 Education services. A division filter also returns
  its group/class/item rows; only the division row (group empty) is kept. These rows carry no status; MoSPI's
  newest month is provisional until the next release.
The 2024-base rows are used for their published year-on-year `inflation` only (`wealth.inflation.chain`).

Terms, read 22-Oct-2026: the API's user manual (CPI API User Manual.pdf, 10-Apr-2024) describes a sign-up and a
15-minute token, "Without access token the APIs will fetch only the first 10 records"; the live API now serves up to
`limit` records a page (200 for base 2012, 100 for base 2024) without one, and MoSPI's own MCP server
(github.com/nso-india/esankhyiki-mcp) calls it without a token. No licence text could be read (mospi.gov.in's
policy pages render only with JavaScript). Used for personal planning only, cited wherever shown, cached in the
gitignored data/ folder and never redistributed; tests use hand-made synthetic pages.

Politeness: PoliteClient (per-host rate limit, retries), about 8 GETs per refresh, cached on disk (the frozen 2012
series for 30 days, the 2024 series for a day). A response that is not `statusCode: true` is an error and is never
cached; more than MAX_PAGES pages is refused rather than truncated.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from finresearch.adapters.http import Fetched, PoliteClient

BASE = "https://api.mospi.gov.in/api/cpi/"
DATASET = "mospi_cpi"  # data_rights
SOURCE = "MoSPI, All-India CPI (Combined), api.mospi.gov.in"
CATEGORIES = ("general", "health", "education")
OLD_SUBGROUPS = {"General-Overall": "general", "Health": "health", "Education": "education"}
NEW_DIVISIONS = {"CPI (General)": "general", "Health": "health", "Education services": "education"}
OLD_LIMIT, NEW_LIMIT = 200, 100
OLD_TTL_S, NEW_TTL_S = 30 * 86400, 86400
MAX_PAGES = 40
MONTHS = {m: i + 1 for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                          "September", "October", "November", "December"))}  # fmt: skip


class MospiError(RuntimeError):
    pass


@dataclass
class CpiData:
    old: dict[str, dict[date, float]]  # category -> 2012-base index by month
    new_yoy: dict[str, dict[date, float]]  # category -> 2024-base year-on-year %, by month
    old_last: date
    new_last: date | None
    imputed: list[date] = field(default_factory=list)  # general-index months MoSPI marks F* (imputed)
    fetched_at: datetime | None = None
    urls: list[str] = field(default_factory=list)


def _body(f: Fetched) -> dict[str, Any]:
    try:
        body = json.loads(f.content)
    except ValueError as e:
        raise MospiError("MoSPI CPI API answered with something other than JSON") from e
    if (
        not isinstance(body, dict)
        or body.get("statusCode") is not True
        or not isinstance(body.get("data"), list)
    ):
        msg = body.get("message") or body.get("error") or body.get("msg") if isinstance(body, dict) else None
        raise MospiError(f"MoSPI CPI API error: {msg or 'unexpected answer'}")
    return body


def _cacheable(f: Fetched) -> bool:
    try:
        return bool(_body(f)["data"])
    except MospiError:
        return False


def _month(row: dict[str, Any]) -> date:
    try:
        return date(int(row["year"]), MONTHS[str(row["month"]).strip()], 1)
    except (KeyError, ValueError) as e:
        raise MospiError(
            f"MoSPI CPI row with a bad year/month: {row.get('year')!r} {row.get('month')!r}"
        ) from e


def _num(v: Any) -> float | None:
    return None if v in (None, "", "NA", "-") else float(v)


def tls_context() -> ssl.SSLContext:
    """The default verifying context plus OP_LEGACY_SERVER_CONNECT: api.mospi.gov.in does not support RFC 5746
    secure renegotiation, which OpenSSL 3 otherwise requires (checked 22-Oct-2026). Certificates and the host name
    are still verified; the option is used for this host's client only."""
    ctx = ssl.create_default_context()
    ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return ctx


class MospiCpiClient:
    def __init__(self, client: PoliteClient | None = None):
        self._own = client is None
        self.http = client or PoliteClient(verify=tls_context())

    async def __aenter__(self) -> MospiCpiClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._own:
            await self.http.aclose()

    async def _rows(self, path: str, params: dict[str, str], limit: int, ttl: float,
                    urls: list[str]) -> tuple[list[dict[str, Any]], datetime | None]:  # fmt: skip
        rows: list[dict[str, Any]] = []
        fetched: datetime | None = None
        p, pages = 1, 1
        while p <= pages:
            got = await self.http.get(BASE + path, params=params | {"limit": str(limit), "page": str(p)},
                                      cache_ttl=ttl, cache_if=_cacheable)  # fmt: skip
            if not got.ok:
                raise MospiError(f"MoSPI CPI API HTTP {got.status}")
            body = _body(got)
            pages = int((body.get("meta_data") or {}).get("totalPages") or 1)
            if pages > MAX_PAGES:
                raise MospiError(
                    f"MoSPI CPI API reports {pages} pages (over {MAX_PAGES}); not reading a part of it"
                )
            rows += body["data"]
            fetched = got.record.fetched_at if fetched is None else min(fetched, got.record.fetched_at)
            if p == 1:
                urls.append(got.record.url)
            p += 1
        return rows, fetched

    async def fetch(self, *, today: date) -> CpiData:
        """Both series. `today` (IST) bounds the 2024-base years asked for."""
        from finresearch import data_rights

        data_rights.require(DATASET)  # before the first request (#471): no decision, no request
        urls: list[str] = []
        old_rows, f1 = await self._rows("getCPIIndex", {
            "base_year": "2012", "series": "Current", "year": ",".join(str(y) for y in range(2013, 2026)),
            "state_code": "99", "sector_code": "3", "group_code": "0,6", "subgroup_code": "0.99,6.1.02,6.1.05",
            "format": "JSON"}, OLD_LIMIT, OLD_TTL_S, urls)  # fmt: skip
        new_rows, f2 = await self._rows("getCPIData", {
            "base_year": "2024", "series": "Current", "year": ",".join(str(y) for y in range(2026, today.year + 1)),
            "state_code": "1", "sector_code": "3", "division_code": "0,6,10", "Format": "JSON"},
            NEW_LIMIT, NEW_TTL_S, urls)  # fmt: skip
        old: dict[str, dict[date, float]] = {c: {} for c in CATEGORIES}
        imputed: list[date] = []
        for r in old_rows:
            cat = OLD_SUBGROUPS.get(str(r.get("subgroup")))
            idx = _num(r.get("index"))
            if cat is None or idx is None:
                continue
            m = _month(r)
            old[cat][m] = idx
            if cat == "general" and str(r.get("status", "")).strip() == "F*":
                imputed.append(m)
        if not all(old.values()):
            raise MospiError("MoSPI CPI API returned no rows for the 2012-base series "
                             f"({', '.join(c for c in CATEGORIES if not old[c])})")  # fmt: skip
        new: dict[str, dict[date, float]] = {c: {} for c in CATEGORIES}
        for r in new_rows:
            cat = NEW_DIVISIONS.get(str(r.get("division")))
            yoy = _num(r.get("inflation"))
            if cat is None or r.get("group") not in (None, "") or yoy is None:
                continue
            new[cat][_month(r)] = yoy
        stamps = [x for x in (f1, f2) if x is not None]
        return CpiData(old=old, new_yoy=new, old_last=max(old["general"]),
                       new_last=max(new["general"]) if new["general"] else None, imputed=sorted(imputed),
                       fetched_at=min(stamps) if stamps else None, urls=urls)  # fmt: skip
