"""GET /api/portfolio/optimise (#346) on the synthetic portfolio of test_portfolio_analytics (test database, fake
prices and closes: nothing leaves the machine)."""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from test_portfolio_analytics import _db_writes, api  # noqa: F401  (the `api` fixture)

from finresearch.adapters.nse import Quote


@pytest.fixture
def priced(api):  # noqa: F811
    """The analytics fixture's quotes carry only an industry: give its two stocks a last price."""

    async def quote(sym):
        return Quote(symbol=sym, last_price={"AAA": D(60), "BBB": D(205)}[sym], industry=None)

    api.app.state.markets.quote = quote
    return api


def test_optimise_route_reads_only_and_fixes_short_histories(priced):
    with _db_writes() as writes:
        r = priced.get("/api/portfolio/optimise")
    assert r.status_code == 200, r.text
    assert writes == []
    body = r.json()
    # four days of closes: nothing has the validation's span, so nothing is optimised or traded
    assert body["status"] == "too_few" and body["sells"] == [] and body["buys"] == []
    assert body["holdings"] and all(
        h["status"] == "fixed" and "daily returns" in h["reason"] for h in body["holdings"]
    )
    assert "no order" in body["disclaimer"]


def test_optimise_route_refuses_unknown_methods(priced):
    c = priced
    assert c.get("/api/portfolio/optimise", params={"method": "mean_variance"}).status_code == 422
    assert c.get("/api/portfolio/optimise", params={"method": "hrp"}).json()["method"] == "hrp"


def test_default_risk_limit_is_a_note_not_a_bound(priced):
    # no max position on the profile: the 5 / 8 / 10 % default is a warning threshold, not a hard cap
    body = priced.get("/api/portfolio/optimise").json()
    assert body["max_position_pct"] is None
    assert body["position_limit"]["source"] == "risk"
    assert any("not applied as a limit" in n for n in body["notes"])
