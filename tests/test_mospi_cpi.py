"""MoSPI CPI adapter (#347): both base series through PoliteClient, paginated, division rows only, failures as
errors (never as an empty series). Hand-made synthetic API pages; no real MoSPI numbers."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from finresearch.adapters import mospi_cpi as mc
from finresearch.adapters.http import PoliteClient

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]  # fmt: skip
OLD_SUB = {"general": ("General", "General-Overall"), "health": ("Miscellaneous", "Health"),
           "education": ("Miscellaneous", "Education")}  # fmt: skip
NEW_DIV = {"general": "CPI (General)", "health": "Health", "education": "Education services"}


def old_rows() -> list[dict]:
    """2012-base rows, Jan-2013..Dec-2025: general index 100 + k, health 200 + k, education 300 + k (k = month no.)."""
    out = []
    for cat, base in (("general", 100), ("health", 200), ("education", 300)):
        g, sg = OLD_SUB[cat]
        for k in range(156):
            y, m = 2013 + k // 12, k % 12
            out.append({"baseyear": "2012", "year": y, "month": MONTHS[m], "state": "All India", "sector": "Combined",
                        "group": g, "subgroup": sg, "index": f"{base + k:.1f}", "inflation": None,
                        "status": "F*" if (y, m) == (2020, 3) else "F"})  # fmt: skip
    return out


def new_rows() -> list[dict]:
    """2024-base rows for Jan..Mar-2026: division rows with yoy 2 / 3 / 4 %, plus a sub-level row to be ignored."""
    out = []
    for cat, yoy in (("general", "2.00"), ("health", "3.00"), ("education", "4.00")):
        for m in range(3):
            row = {"base_year": "2024", "series": "Current", "year": "2026", "month": MONTHS[m], "state": "All India",
                   "sector": "Combined", "division": NEW_DIV[cat], "group": None, "class": None, "sub_class": None,
                   "item": None, "code": None, "index": "101.00", "inflation": yoy, "imputation": None}  # fmt: skip
            out.append(row)
            out.append(row | {"group": "Some sub-group", "inflation": "99.00"})
    return out


def page(rows: list[dict], req: httpx.Request) -> httpx.Response:
    limit, p = int(req.url.params["limit"]), int(req.url.params.get("page", "1"))
    chunk = rows[(p - 1) * limit : p * limit]
    pages = max(1, -(-len(rows) // limit))
    body = {"data": chunk, "msg": "Data fetched successfully" if chunk else "No Data Found", "statusCode": True,
            "meta_data": {"page": p, "totalRecords": len(rows), "totalPages": pages, "recordPerPage": limit}}  # fmt: skip
    return httpx.Response(200, content=json.dumps(body).encode())


async def _no_sleep(_: float) -> None:
    return None


def client(handler, tmp_path=None) -> PoliteClient:
    return PoliteClient(cache_dir=tmp_path, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0,
                        transport=httpx.MockTransport(handler), slots=None)  # fmt: skip


def serve(old: list[dict], new: list[dict], calls: list[str] | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(req.url))
        return page(old if req.url.path.endswith("getCPIIndex") else new, req)

    return handler


async def test_fetch_parses_both_series_across_pages():
    calls: list[str] = []
    async with mc.MospiCpiClient(client(serve(old_rows(), new_rows(), calls))) as c:
        d = await c.fetch(today=date(2026, 10, 22))
    assert sum("getCPIIndex" in u for u in calls) == 3  # 468 rows at 200 a page
    assert d.old["general"][date(2013, 1, 1)] == 100.0 and d.old["education"][date(2025, 12, 1)] == 455.0
    assert d.new_yoy["health"] == {date(2026, m, 1): 3.0 for m in (1, 2, 3)}  # the sub-level row is ignored
    assert d.old_last == date(2025, 12, 1) and d.new_last == date(2026, 3, 1)
    assert d.imputed == [date(2020, 4, 1)]  # MoSPI's F* (imputed) months are reported
    assert "year=2026" in next(u for u in calls if "getCPIData" in u)


async def test_no_2024_base_months_yet_is_an_empty_new_series_not_an_error():
    async with mc.MospiCpiClient(client(serve(old_rows(), []))) as c:
        d = await c.fetch(today=date(2026, 1, 5))
    assert d.new_yoy == {"general": {}, "health": {}, "education": {}} and d.new_last is None


async def test_empty_2012_series_is_an_error():
    async with mc.MospiCpiClient(client(serve([], new_rows()))) as c:
        with pytest.raises(mc.MospiError, match="no rows"):
            await c.fetch(today=date(2026, 10, 22))


async def test_error_body_and_http_error_raise_and_are_not_cached(tmp_path):
    state = {"bad": True}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["bad"]:
            return httpx.Response(
                200, content=b'{"success":false,"message":"Limit cannot be greater than 200"}'
            )
        return serve(old_rows(), new_rows())(req)

    async with mc.MospiCpiClient(client(handler, tmp_path)) as c:
        with pytest.raises(mc.MospiError, match="Limit cannot"):
            await c.fetch(today=date(2026, 10, 22))
    state["bad"] = False
    async with mc.MospiCpiClient(client(handler, tmp_path)) as c:
        d = await c.fetch(today=date(2026, 10, 22))
    assert d.old_last == date(2025, 12, 1)
    async with mc.MospiCpiClient(client(lambda r: httpx.Response(503), None)) as c:
        with pytest.raises(mc.MospiError, match="HTTP 503"):
            await c.fetch(today=date(2026, 10, 22))


async def test_too_many_pages_is_refused_not_truncated(monkeypatch):
    monkeypatch.setattr(mc, "MAX_PAGES", 2)
    async with mc.MospiCpiClient(client(serve(old_rows(), new_rows()))) as c:
        with pytest.raises(mc.MospiError, match="pages"):
            await c.fetch(today=date(2026, 10, 22))
