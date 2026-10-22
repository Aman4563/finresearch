"""GET /api/portfolio/analytics/liquidity (#348) on the synthetic portfolio of test_portfolio_analytics (10 AAA, 5 BBB,
10,000 units of fund 111) with synthetic NSE volume rows in the test database. The GET writes nothing."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from test_portfolio_analytics import DAYS, _db_writes, api  # noqa: F401  (the `api` fixture)


def _seed(n: int) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import EquityEod, EquityEodDay

    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day"))
        d, k = DAYS[-1], 0
        while k < n:
            if d.weekday() < 5:
                s.add(EquityEodDay(day=d, status="loaded", rows=1))
                s.add(
                    EquityEod(
                        day=d,
                        symbol="AAA",
                        series="EQ",
                        close=Decimal(60),
                        qty=40,
                        turnover_lacs=Decimal("0.03"),
                    )
                )
                k += 1
            d -= timedelta(days=1)


def test_liquidity_route_by_hand_and_writes_nothing(api):  # noqa: F811
    _seed(20)
    with _db_writes() as writes:
        r = api.get("/api/portfolio/analytics/liquidity?participation=0.25")
    assert r.status_code == 200 and writes == []
    body = r.json()
    rows = {x["key"]: x for x in body["positions"]}
    # AAA: 10 units / (0.25 × 40 shares a day) = 1 day; BBB has no stored volume; the fund is not exchange-traded
    assert (
        rows["NSE:AAA"]["days"] == pytest.approx(1.0) and rows["NSE:AAA"]["adv_rupees"] == 3000
    )  # 0.03 lakh
    assert rows["NSE:BBB"]["days"] is None and rows["NSE:BBB"]["reason"].startswith(
        "traded on 0 of the last 20"
    )
    assert rows["MF:111"]["status"] == "not_applicable"
    assert body["participation"] == 0.25 and body["summary"]["complete"] is False
    assert body["as_of"] == DAYS[-1].isoformat() and "never sent to an LLM" in body["privacy"]
    assert body["source"]["name"].startswith("NSE full bhavcopy")


def test_liquidity_route_rejects_a_bad_participation(api):  # noqa: F811
    assert api.get("/api/portfolio/analytics/liquidity?participation=0").status_code == 422
    assert api.get("/api/portfolio/analytics/liquidity?participation=2").status_code == 422


def test_liquidity_with_no_stored_volume_is_unknown_not_zero(api):  # noqa: F811
    _seed(0)
    body = api.get("/api/portfolio/analytics/liquidity").json()
    rows = {x["key"]: x for x in body["positions"]}
    assert rows["NSE:AAA"]["days"] is None
    assert rows["NSE:AAA"]["reason"] == "only 0 sessions of NSE volume are stored; needs 15"
    assert (
        body["summary"]["within"]["20"] is None  # nothing known: unknown, not "0 % sellable"
        and body["summary"]["unknown"] > 0
        and body["participation"] == 0.2
    )
