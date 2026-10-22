"""GET /api/portfolio/analytics/attribution (#348) on the synthetic portfolio of test_portfolio_analytics (20 AAA after
a 1:2 split, 5 BBB, 10,000 units of fund 111; prices 5-8 Jan 2026), a fake Nifty 50 weight source and synthetic
bhavcopy rows in the test database. Nothing leaves the machine; the GET writes nothing."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text
from test_portfolio_analytics import DAYS, _db_writes, api  # noqa: F401  (the `api` fixture)

from finresearch.adapters.nse_index_weights import IndexMember, IndexWeights, IndexWeightsError
from finresearch.portfolio import attribution as at

ROWS = {"AAA": [(100, 99), (110, 100), (55, 55), (60, 55)],  # NSE adjusted the previous close on the split day
        "ZZZ": [(25, 25), (26, 25), (27, 26), (30, 27)]}  # fmt: skip


def _seed(nifty=(20000, 24000)) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import EquityEod, EquityEodDay, IndexEod

    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day, index_eod"))
        for i, d in enumerate(DAYS):
            s.add(EquityEodDay(day=d, status="loaded", rows=2))
            for sym, rows in ROWS.items():
                c, p = rows[i]
                s.add(EquityEod(day=d, symbol=sym, series="EQ", close=Decimal(c), prev_close=Decimal(p)))
        s.add(IndexEod(day=DAYS[0], symbol="NIFTY", index_name="Nifty 50", close=Decimal(nifty[0])))
        s.add(IndexEod(day=DAYS[-1], symbol="NIFTY", index_name="Nifty 50", close=Decimal(nifty[1])))


def _weights():
    return IndexWeights("NIFTY 50", None, "https://example.invalid/nifty50", None,
                        [IndexMember("AAA", "AAA Ltd", "INE000A01011", "Financial Services", 1200.0, 60.0),
                         IndexMember("ZZZ", "ZZZ Ltd", "INE000Z01011", "Information Technology", 3000.0, 30.0)])  # fmt: skip


@pytest.fixture
def short(monkeypatch):
    monkeypatch.setitem(at.PERIODS, "1m", (DAYS[-1] - DAYS[0]).days)  # the fixture has four days of prices


def test_attribution_route_by_hand_and_writes_nothing(api, short):  # noqa: F811
    _seed()
    calls = []

    async def source():
        calls.append(1)
        return _weights()

    api.app.state.benchmark_source = source
    with _db_writes() as writes:
        r = api.get("/api/portfolio/analytics/attribution?period=1m")
    assert r.status_code == 200 and writes == []
    body = r.json()
    assert body["available"] is True and body["window"] == {"start": "2026-01-05", "end": "2026-01-08"}
    # the fixture records no split for AAA, so its 110 -> 55 day is a probable unrecorded split: AAA is left out and
    # counted. BBB 5 × 205 = 1,025 at 205/190 -> 950 at the start; the fund 10,000 × 10.1 = 1,01,000 at 10.1/9.8 ->
    # 98,000. R_p = 1,02,025 / 98,950 - 1; coverage 1,02,025 of 1,02,625 (AAA 10 × 60 = 600)
    assert body["excluded"] == [{"key": "NSE:AAA", "name": "AAA Ltd",
                                 "reason": "a one-day move beyond ±40 % with no recorded split or bonus"}]  # fmt: skip
    assert body["coverage"] == pytest.approx(102025 / 102625, abs=1e-4) and body["complete"] is False
    t = body["totals"]
    rp = 102025 / 98950 - 1
    # benchmark: AAA 1,200 at +20 % -> 1,000; ZZZ 3,000 at 30/25 - 1 = +20 % -> 2,500; R_b = .2
    assert t["portfolio_return"] == pytest.approx(rp, abs=1e-6) and t["benchmark_return"] == pytest.approx(
        0.2
    )
    assert t["active"] == pytest.approx(rp - 0.2, abs=1e-6)
    assert t["allocation"] + t["selection"] + t["interaction"] == pytest.approx(t["active"], abs=1e-9)
    seg = {x["segment"]: x for x in body["segments"]}
    assert (
        seg["Financial Services"]["wb"] == pytest.approx(1000 / 3500, abs=1e-6)
        and seg["Financial Services"]["wp"] == 0
    )
    assert seg["Information Technology"]["note"] == at.brinson.NOT_HELD
    # the fund: off the benchmark, all allocation: (98,000 / 98,950)(10.1/9.8 - 1 - .2)
    f = seg["Funds (no look-through)"]
    assert (
        f["allocation"] == pytest.approx(98000 / 98950 * (10.1 / 9.8 - 1.2), abs=1e-6) and f["selection"] == 0
    )
    assert body["benchmark"]["index_return"] == pytest.approx(0.2) and body["benchmark"][
        "difference"
    ] == pytest.approx(0)
    assert body["benchmark"]["warning"] is None and body["benchmark"]["source"]["url"].endswith("nifty50")
    assert "never sent to an LLM" in body["privacy"] and body["disclaimer"]
    api.get("/api/portfolio/analytics/attribution?period=1m")
    assert len(calls) == 1  # the weights are read once and kept in memory


def test_attribution_index_mismatch_is_a_warning(api, short):  # noqa: F811
    _seed(nifty=(20000, 26000))  # NSE's index +30 % against the rebuilt +20 %
    api.app.state.benchmark_source = lambda: _async(_weights())
    b = api.get("/api/portfolio/analytics/attribution?period=1m").json()["benchmark"]
    assert b["difference"] == pytest.approx(-0.1) and b["warning"].startswith(
        "the rebuilt Nifty 50 return differs"
    )


def test_attribution_unreachable_source_is_a_reason_and_is_retried(api, short):  # noqa: F811
    _seed()
    calls = []

    async def source():
        calls.append(1)
        raise IndexWeightsError("NSE index weights HTTP 403")

    api.app.state.benchmark_source = source
    body = api.get("/api/portfolio/analytics/attribution?period=1m").json()
    assert body["available"] is False and body["totals"] is None
    assert (
        body["reason"] == "Nifty 50 weights could not be read: IndexWeightsError: NSE index weights HTTP 403"
    )
    api.get("/api/portfolio/analytics/attribution?period=1m")
    assert len(calls) == 2  # a failure is never cached


def test_attribution_without_stored_bhavcopy_is_unknown(api):  # noqa: F811
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day, index_eod"))
    api.app.state.benchmark_source = lambda: _async(_weights())
    body = api.get("/api/portfolio/analytics/attribution").json()
    assert body["available"] is False and body["reason"].startswith("no NSE bhavcopy is stored yet")
    assert api.get("/api/portfolio/analytics/attribution?period=2y").status_code == 422


async def _async(x):
    return x


def test_attribution_weights_from_nse_list_and_quotes_failed_quote_kept_briefly(api, short, monkeypatch):  # noqa: F811
    """No seam: the route reads the member list (stubbed here) and each member's quote through the app's quote source;
    a member whose quote fails makes its sector unknown, and that answer is not kept for the full 12 hours."""
    from types import SimpleNamespace

    from finresearch.adapters import nse_index_weights as nw

    _seed()
    lists = []

    async def fake_nifty50(self, quote):
        lists.append(1)
        cons = [nw.Constituent("AAA", "AAA Ltd", "Financial Services", None),
                nw.Constituent("ZZZ", "ZZZ Ltd", "Information Technology", None)]  # fmt: skip
        return await nw.weights(cons, quote, url=nw.LIST_URL)

    async def quote(sym):
        if sym == "ZZZ":
            raise RuntimeError("HTTP 403")
        return SimpleNamespace(ffmc=Decimal(1200), last_price=Decimal(60), sector=None)

    import finresearch.api.markets as mk

    monkeypatch.setattr(
        mk, "NEGATIVE_TTL_S", 0
    )  # an incomplete answer expires at once (normally after minutes)
    monkeypatch.setattr(nw.NiftyWeightsClient, "nifty50", fake_nifty50)
    api.app.state.markets.quote = quote
    with _db_writes() as writes:
        body = api.get("/api/portfolio/analytics/attribution?period=1m").json()
    assert writes == []
    assert body["available"] is True and body["totals"]["active"] is None
    assert body["benchmark_unknown"] == [
        {"symbol": "ZZZ", "name": "ZZZ Ltd", "reason": "its NSE quote failed: RuntimeError"}
    ]
    assert body["unreachable"] == ["ZZZ"]
    api.get("/api/portfolio/analytics/attribution?period=1m")
    assert len(lists) == 2
