"""GET /api/portfolio/analytics/factors (#348) on the synthetic portfolio of test_portfolio_analytics (test database,
fake prices, a fake factor source: nothing leaves the machine)."""

from __future__ import annotations

from datetime import date, timedelta

from test_portfolio_analytics import DAYS, _db_writes, api  # noqa: F401  (the `api` fixture)

from finresearch.adapters.iima_factors import FactorDay, FactorSeries, IimaError


def _series(last: date) -> FactorSeries:
    rows, d = {}, last - timedelta(days=400)
    while d <= last:
        if d.weekday() < 5:
            rows[d] = FactorDay(mf=0.001, smb=0.0, hml=-0.001, wml=0.002, rf=0.00025)
        d += timedelta(days=1)
    return FactorSeries(release="2099-12", url="https://example.invalid/f.csv", rows=rows)


def test_factors_route_gives_reasons_for_a_short_history_and_writes_nothing(api):  # noqa: F811
    calls = []

    async def source():
        calls.append(1)
        return _series(DAYS[-1])

    api.app.state.factor_source = source
    with _db_writes() as writes:
        r = api.get("/api/portfolio/analytics/factors")
    assert r.status_code == 200 and writes == []
    body = r.json()
    assert body["available"] is True and body["source"]["release"] == "2099-12"
    p = body["portfolio"]
    assert (
        p["available"] is False and p["exposures"] == [] and p["reason"].startswith("needs 120 daily returns")
    )
    assert {h["key"] for h in body["holdings"]} == {"NSE:AAA", "NSE:BBB", "MF:111"}
    assert all(h["available"] is False and h["reason"] for h in body["holdings"])
    assert "never sent to an LLM" in body["privacy"] and body["disclaimer"]
    api.get("/api/portfolio/analytics/factors")
    assert len(calls) == 1  # the factor file is read once and kept in memory


def test_factors_route_unreachable_source_is_a_reason_and_is_retried(api):  # noqa: F811
    calls = []

    async def source():
        calls.append(1)
        raise IimaError("IIMA data library HTTP 503")

    api.app.state.factor_source = source
    body = api.get("/api/portfolio/analytics/factors").json()
    assert body["available"] is False and body["portfolio"] is None
    assert body["reason"] == "IIMA factor data could not be read: IimaError: IIMA data library HTTP 503"
    api.get("/api/portfolio/analytics/factors")
    assert len(calls) == 2  # a failure is never cached as data
