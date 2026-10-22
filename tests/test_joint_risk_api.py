"""GET /api/portfolio/analytics/joint-risk and POST .../joint-risk/pretrade (#397) on the synthetic portfolio of
test_portfolio_analytics (test database, fake prices, no paper trades, Groww not connected: nothing leaves the
machine)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_portfolio_analytics import CLOSES, ORIGIN, _db_writes, api  # noqa: F401  (the `api` fixture)

VALUES = {
    "NSE:AAA": 10 * CLOSES["NSE:AAA"][-1],
    "NSE:BBB": 5 * CLOSES["NSE:BBB"][-1],
    "MF:111": 10000 * CLOSES["MF:111"][-1],
}  # fmt: skip  (the API fixture records no split)


@pytest.fixture
def empty_book(api):  # noqa: F811
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_mark, paper_leg, paper_strategy, broker_connection CASCADE"))
    return api


def test_short_history_holdings_leave_the_joint_figures_unknown_and_the_get_writes_nothing(empty_book):
    with _db_writes() as writes:
        r = empty_book.get("/api/portfolio/analytics/joint-risk")
    assert r.status_code == 200 and writes == []
    body = r.json()
    assert body["book"] == {"known": True, "legs": 0, "reason": None}  # nothing open, and that is known
    d = body["d_total"]
    assert d["total"] is None and d["holdings"] is None and d["fno"] == 0 and not d["complete"]
    assert {h["key"] for h in body["holdings"]} == set(VALUES) and all(
        h["beta"] is None for h in body["holdings"]
    )
    assert all(x["total"] is None and x["reasons"] for x in body["losses"])
    assert body["hedge_trigger"]["fired"] is None
    assert all(r["total"] is None and r["reasons"] for r in body["replays"])
    assert "not evaluable yet" in body["forward_test"]["status"]
    assert (
        "never sent to an LLM" in body["privacy"] and body["disclaimer"] and "NIFTYBEES" in body["beta_note"]
    )


def test_the_beta_one_option_gives_hand_values_with_an_empty_book(empty_book):
    body = empty_book.get("/api/portfolio/analytics/joint-risk?assume_beta1=true").json()
    total = sum(VALUES.values())  # every fixture holding is equity-taxed: beta 1 assumed for each
    assert body["d_total"]["total"] == pytest.approx(total, abs=0.01)
    assert body["d_total"]["per_pct"]["total"] == pytest.approx(total / 100, abs=0.01)
    l10 = next(x for x in body["losses"] if x["move"] == -0.10)
    assert (
        l10["total"] == pytest.approx(-0.10 * total, abs=0.01) and l10["fno"] == 0 and l10["fno_share"] == 0
    )
    assert body["hedge_trigger"]["fired"] is False


class NoChain:
    async def chain(self, symbol, expiry):
        raise RuntimeError("no chain in this test")


def test_pretrade_needs_valid_legs_and_writes_nothing(empty_book):
    from finresearch.fincalc.dates import today_ist

    empty_book.app.state.fno_sources = NoChain()
    exp = (today_ist() + timedelta(days=20)).isoformat()
    bad = empty_book.post("/api/portfolio/analytics/joint-risk/pretrade", headers=ORIGIN,
                          json={"legs": [{"underlying": "NIFTY", "expiry": exp, "right": "put", "strike": 0,
                                          "qty": -75}]})  # fmt: skip
    assert bad.status_code == 422
    with _db_writes() as writes:
        r = empty_book.post("/api/portfolio/analytics/joint-risk/pretrade?assume_beta1=true", headers=ORIGIN,
                            json={"legs": [{"underlying": "NIFTY", "expiry": exp, "right": "future", "strike": 0,
                                            "qty": -75}]})  # fmt: skip
    assert r.status_code == 200 and writes == []
    p = r.json()
    assert p["d_before"]["total"] == pytest.approx(sum(VALUES.values()), abs=0.01)
    # the fake app reads no option chain, so the candidate future's forward is unknown: after = unknown, never 0
    assert p["d_after"]["total"] is None and p["d_change"] is None and p["d_after"]["reasons"]
