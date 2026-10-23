"""#366: storing FBIL's dated rate series (refseries.rates): the data-rights gate, month windows for the JSON
fixings, the G-sec workbook days, lineage, and the polite stops. Test database only; FBIL mocked with respx and
synthetic payloads (made-up rates)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select, text

from finresearch.adapters import fbil
from finresearch.adapters.http import PoliteClient

XLS = (
    Path(__file__).parent / "fixtures" / "fbil" / "gsec_synthetic_2022.xls"
).read_bytes()  # as of 03-Jan-2022
NIGHT = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)  # 20:30 IST
TODAY = date(2026, 10, 21)


async def _no_sleep(_s: float) -> None:
    return None


def client() -> PoliteClient:
    return PoliteClient(cache_dir=None, sleep=_no_sleep, clock=lambda: 0.0, max_retries=0)


def tbill(day: str, rate: float = 3.62) -> list[dict]:
    return [{"processRunDate": f"{day} 00:00:00", "displayTime": f"{day} 17:30:00", "tenorName": t, "rate": rate}
            for t in ("3 Months", "6 Months")]  # fmt: skip


@pytest.fixture
def clean(env, monkeypatch):
    from finresearch import data_rights
    from finresearch.db import session_scope

    monkeypatch.setitem(data_rights.DECISIONS, "fbil_rates", data_rights.Decision(
        "fbil_rates", "accept-personal", "2026-10-22", "test", "test"))  # fmt: skip
    with session_scope() as s:
        s.execute(text("TRUNCATE rate_curve, rate_curve_load, dataset_load"))
    yield env


def rows(series: str) -> list[tuple]:
    from finresearch.db import session_scope
    from finresearch.db.models import RateCurve

    with session_scope() as s:
        return [tuple(r) for r in s.execute(select(RateCurve.day, RateCurve.tenor, RateCurve.rate)
                                            .where(RateCurve.series == series).order_by(RateCurve.day, RateCurve.tenor))]  # fmt: skip


def lineage(dataset: str) -> list[tuple]:
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad

    with session_scope() as s:
        return [tuple(r) for r in s.execute(select(DatasetLoad.day, DatasetLoad.prior_sha256)
                                            .where(DatasetLoad.dataset == dataset).order_by(DatasetLoad.id))]  # fmt: skip


async def test_no_data_rights_decision_no_request(env, monkeypatch):
    from finresearch import data_rights
    from finresearch.refseries import rates

    # the owner decided on 23-Oct-2026 (#471); without that decision nothing is requested, and the refusal cites
    # FBIL's terms while they are pending
    monkeypatch.delitem(data_rights.DECISIONS, "fbil_rates", raising=False)
    monkeypatch.setitem(data_rights.PENDING, "fbil_rates", "FBIL disclaimer: owner decision needed")
    with respx.mock(assert_all_called=False) as mock, pytest.raises(data_rights.NoDecision, match="FBIL"):
        await rates.backfill(["tbill"], date(2022, 1, 1), today=TODAY, http=client(), now=lambda: NIGHT)
    assert not mock.calls


@respx.mock
async def test_fixings_month_windows_stored_with_lineage(clean):
    from finresearch.refseries import rates

    route = respx.get(f"{fbil.BASE}/tbill/fetchfiltered").mock(side_effect=lambda req: httpx.Response(
        200, json=tbill("2022-01-03") if req.url.params["fromDate"] == "2022-01-01" else []))  # fmt: skip
    out = await rates.backfill(["tbill"], date(2022, 1, 1), date(2022, 2, 28), today=TODAY, http=client(),
                               now=lambda: NIGHT)  # fmt: skip
    windows = [(c.request.url.params["fromDate"], c.request.url.params["toDate"]) for c in route.calls]
    assert windows == [("2022-01-01", "2022-01-31"), ("2022-02-01", "2022-02-28")]
    assert out["loaded"] == 1 and out["no_file"] == 1 and out["rows"] == 2

    assert rows("tbill") == [
        (date(2022, 1, 3), "3M", Decimal("0.03620000")),
        (date(2022, 1, 3), "6M", Decimal("0.03620000")),
    ]
    assert lineage("fbil_tbill") == [(date(2022, 1, 3), None)]
    again = await rates.backfill(["tbill"], date(2022, 1, 1), date(2022, 2, 28), today=TODAY, http=client(),
                                 now=lambda: NIGHT)  # fmt: skip
    assert again["planned"] == 0 and len(route.calls) == 2  # settled months are not asked again


@respx.mock
async def test_an_unsettled_month_is_refetched_and_a_revision_is_seen(clean):
    from finresearch.refseries import rates

    rate = {"v": 5.40}
    respx.get(f"{fbil.BASE}/tbill/fetchfiltered").mock(
        side_effect=lambda req: httpx.Response(200, json=tbill("2026-10-14", rate["v"]))
    )
    kw = {"today": TODAY, "http": client(), "now": lambda: NIGHT}
    await rates.backfill(["tbill"], date(2026, 10, 1), **kw)
    await rates.backfill(["tbill"], date(2026, 10, 1), **kw)  # same body: no new lineage row
    assert lineage("fbil_tbill") == [(date(2026, 10, 14), None)]
    rate["v"] = 5.45
    await rates.backfill(["tbill"], date(2026, 10, 1), **kw)
    got = lineage("fbil_tbill")
    assert len(got) == 2 and got[1][1] is not None  # FBIL re-published the day: prior hash kept
    assert {r[2] for r in rows("tbill")} == {Decimal("0.05450000")}


@respx.mock
async def test_gsec_workbook_days_from_the_published_list(clean):
    from finresearch.refseries import rates

    respx.get(f"{fbil.BASE}/gsec/fetchfiltered").mock(return_value=httpx.Response(200, json=[
        {"processRunDate": "2022-01-03", "archive": "x"}, {"processRunDate": "2022-01-04", "archive": "x"}]))  # fmt: skip
    respx.get(fbil.DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=XLS))  # always the 03-Jan file
    out = await rates.backfill(["gsec_par"], date(2022, 1, 1), date(2022, 1, 31), today=TODAY, http=client(),
                               now=lambda: NIGHT)  # fmt: skip
    assert (
        out["loaded"] == 1 and "gsec_par 2022-01-04" in out["failed"]
    )  # a workbook dated another day is refused
    assert "dated 2022-01-03" in out["failed"]["gsec_par 2022-01-04"]
    pts = rows("gsec_par")
    assert len(pts) == 8 and pts[0][:2] == (date(2022, 1, 3), "0.25Y")
    assert lineage("fbil_gsec_par") == [(date(2022, 1, 3), None)]


@respx.mock
async def test_two_refusals_in_a_row_stop_the_run(clean):
    from finresearch.refseries import rates

    route = respx.get(f"{fbil.BASE}/tbill/fetchfiltered").mock(return_value=httpx.Response(403, text="no"))
    out = await rates.backfill(["tbill"], date(2022, 1, 1), date(2022, 6, 30), today=TODAY, http=client(),
                               now=lambda: NIGHT)  # fmt: skip
    assert len(route.calls) == 2 and "refused" in out["stopped"]


@respx.mock
async def test_a_mid_month_start_still_asks_for_the_whole_month(clean):
    """Regression: a window starting mid-month (the nightly job's 45 days) must not mark the month settled with its
    first half never fetched; months are always requested whole."""
    from finresearch.refseries import rates

    route = respx.get(f"{fbil.BASE}/tbill/fetchfiltered").mock(return_value=httpx.Response(200, json=[]))
    await rates.backfill(["tbill"], date(2026, 8, 16), today=TODAY, http=client(), now=lambda: NIGHT)
    assert route.calls[0].request.url.params["fromDate"] == "2026-08-01"
    assert route.calls[0].request.url.params["toDate"] == "2026-08-31"
