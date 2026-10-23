"""#479 through the API: owner-entered rates are written only by POST/DELETE; the GETs read the entries, the answer
for a day and tenor (tier, date and source) and the curated RBI policy-rate table."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import func, select, text
from starlette.testclient import TestClient

from finresearch.fincalc import dates

ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
CLOCK = datetime(2026, 10, 23, 11, tzinfo=dates.IST)


@pytest.fixture
def api(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OWNER", None)
    with session_scope() as s:
        s.execute(text("TRUNCATE owner_rate"))
    with TestClient(create_app(clock=lambda: CLOCK)) as c:
        yield c


def _count() -> int:
    from finresearch.db import session_scope
    from finresearch.db.models import OwnerRate

    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(OwnerRate))


def test_post_stores_an_entry_and_the_answer_uses_it(api):
    r = api.post("/api/rates/owner", headers=ORIGIN,
                 json={"day": "2026-10-14", "tenor": "91D", "yield_pct": 5.7281, "source": "RBI auction, 14-Oct"})  # fmt: skip
    assert r.status_code == 201, r.text
    assert r.json()["entry"] == {"day": "2026-10-14", "tenor": "91D", "yield_pct": 5.7281,
                                 "source": "RBI auction, 14-Oct"}  # fmt: skip
    got = api.get("/api/rates/risk-free", params={"day": "2026-10-20", "tenor": "91D"}).json()
    assert got["tier"] == "owner" and got["yield"] == pytest.approx(0.057281) and got["as_of"] == "2026-10-14"
    assert [e["day"] for e in api.get("/api/rates/owner").json()["entries"]] == ["2026-10-14"]


def test_the_answer_without_entries_is_the_repo_proxy_with_its_source(api):
    got = api.get("/api/rates/risk-free", params={"tenor": "91D"}).json()  # today: 23-Oct-2026
    assert got["tier"] == "proxy" and got["yield"] == pytest.approx(0.055) and got["day"] == "2026-10-23"
    assert got["source"].startswith("RBI repo-rate proxy") and got["url"].startswith(
        "https://www.rbi.org.in/"
    )
    old = api.get("/api/rates/risk-free", params={"day": "2014-06-30", "tenor": "91D"}).json()
    assert old["known"] is False and old["rate"] is None and "01-Jan-2015" in old["note"]


def test_gets_write_nothing(api):
    api.get("/api/rates/risk-free", params={"day": "2026-10-20", "tenor": "91D"})
    api.get("/api/rates/owner")
    api.get("/api/rates/policy")
    assert _count() == 0


def test_a_bad_entry_is_refused_and_nothing_is_stored(api):
    r = api.post(
        "/api/rates/owner", headers=ORIGIN, json={"day": "2026-10-24", "tenor": "91D", "yield_pct": 5.7}
    )
    assert r.status_code == 422 and "future" in r.text
    r = api.post(
        "/api/rates/owner", headers=ORIGIN, json={"day": "2026-10-14", "tenor": "3M", "yield_pct": 5.7}
    )
    assert r.status_code == 422
    assert _count() == 0


def test_delete_removes_an_entry_and_404s_when_there_is_none(api):
    api.post(
        "/api/rates/owner", headers=ORIGIN, json={"day": "2026-10-14", "tenor": "182D", "yield_pct": 6.2896}
    )
    assert api.delete("/api/rates/owner/2026-10-14/182D", headers=ORIGIN).status_code == 200
    assert api.delete("/api/rates/owner/2026-10-14/182D", headers=ORIGIN).status_code == 404
    assert _count() == 0


def test_the_policy_table_lists_every_change_with_its_release(api):
    body = api.get("/api/rates/policy").json()
    rows = body["rows"]
    assert (
        body["start"] == "2015-01-01" and rows[-1]["effective"] == "2026-10-07" and rows[-1]["repo"] == 0.055
    )
    assert all(r["url"].startswith("https://www.rbi.org.in/") for r in rows)
    assert date.fromisoformat(body["reviewed"]) <= date(2026, 10, 23)
