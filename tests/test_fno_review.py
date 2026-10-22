"""#383 the monthly post-trade review (fnobook.review): shortfall against the linked card, the P&L explain from the
mark table, plan adherence and the exposure mix. Synthetic trades, cards and marks only; hand-computed values."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import delete

from finresearch.fincalc.dates import IST

EXPIRY = date(2026, 10, 27)


def _clean():
    from finresearch.db import session_scope
    from finresearch.db.models import FnoExitRule, FnoMark, PaperLeg, PaperStrategy

    with session_scope() as s:
        for m in (FnoMark, FnoExitRule, PaperLeg, PaperStrategy):
            s.execute(delete(m))


@pytest.fixture
def db(env):
    _clean()
    yield env
    _clean()


def at(day, hh=11, mm=0, month=10):
    return datetime(2026, month, day, hh, mm, tzinfo=IST)


def spread(s, opened=None, card=None):
    from finresearch.fnobook import book
    from finresearch.signals.fno import LegIn

    q = [
        book.Quote(120.0, "Groww LTP", "NIFTY26OCT25000CE"),
        book.Quote(50.0, "Groww LTP", "NIFTY26OCT25200CE"),
    ]
    return book.record(s, symbol="NIFTY", expiry=EXPIRY, lot=75, legs_in=[LegIn("call", 25000, "buy", 1),
                       LegIn("call", 25200, "sell", 1)], quotes=q, slippage_pct=1.0, brokerage=20,
                       now=opened or at(19), card=card)  # fmt: skip


def plan_card(s, mids=(119.0, 50.5), status="ok"):
    from finresearch.fnobook import card_log

    resp = {"symbol": "NIFTY", "expiry": EXPIRY.isoformat(), "kind": "bull_call_spread", "days_to_expiry": 8,
            "legs": [{"right": "call", "strike": 25000, "side": "buy", "lots": 1},
                     {"right": "call", "strike": 25200, "side": "sell", "lots": 1}],
            "entry": {"status": status, "legs": [{"mid": m} for m in mids]},
            "exits": {"source": "rule of thumb", "profit_pct": 50.0, "stop_pct": 50.0}}  # fmt: skip
    r = card_log.row("plan", symbol="NIFTY", expiry=EXPIRY, inputs={}, cards=[{}], response=resp)
    s.add(r)
    s.flush()
    return r.id


# --------------------------------------------------------------------------- implementation shortfall
def test_shortfall_against_the_linked_cards_mids(db):
    from finresearch.db import session_scope
    from finresearch.fnobook import review

    with session_scope() as s:
        st = spread(s, card=(plan_card(s), 0))
        fees = float(st.entry_costs)
        got = review.shortfall_of(s, st)
    # fills: buy 120 x 1.01 = 121.20, sell 50 x 0.99 = 49.50 (1 % slippage), 75 units each
    # +1 x (121.20 - 119) x 75 = 165; -1 x (49.50 - 50.50) x 75 = 75 -> 240 + the entry costs
    # base = |-119 x 75 + 50.50 x 75| = 68.50 x 75 = 5,137.50
    assert got["price"] == pytest.approx(240)
    assert got["total"] == pytest.approx(240 + fees)
    assert got["pct"] == pytest.approx((240 + fees) / 5137.5 * 100, abs=5e-4)  # shown to 3 decimals


def test_shortfall_without_a_card_or_mids_is_unknown(db):
    from finresearch.db import session_scope
    from finresearch.fnobook import review

    with session_scope() as s:
        a = review.shortfall_of(s, spread(s))
        b = review.shortfall_of(s, spread(s, card=(plan_card(s, status="no_quotes"), 0)))
    assert a["total"] is None and "not taken from a card" in a["why"]
    assert b["total"] is None and "mid" in b["why"]
