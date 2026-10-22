"""#383 the monthly post-trade review (fnobook.review): shortfall against the linked card, the P&L explain from the
mark table, plan adherence and the exposure mix. Synthetic trades, cards and marks only; hand-computed values."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D

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


# --------------------------------------------------------------------------- the daily P&L explain from the marks
def mark(s, day, leg_id, sid, right, strike, qty, price, spot, iv, t_days):
    from finresearch.db.models import FnoMark

    n = lambda v: None if v is None else D(str(v))  # noqa: E731
    s.add(FnoMark(day=day, book="paper", leg_key=f"paper_leg:{leg_id}", paper_id=sid, position_key="NIFTY:2026-10-27",
                  symbol="NIFTY", expiry=EXPIRY, right=right, strike=D(strike), qty=qty, price=n(price), spot=n(spot),
                  iv=n(iv), t_years=D(str(round(t_days / 365, 8)))))  # fmt: skip


def three_days(s):
    st = spread(s)
    a, b = (x.id for x in st.legs)
    for day, pa, pb, spot, iva, ivb, t in ((date(2026, 10, 20), 180, 80, 25100, 0.14, 0.13, 7),
                                           (date(2026, 10, 21), 200, 90, 25180, 0.15, 0.135, 6),
                                           (date(2026, 10, 22), 210, None, 25200, 0.15, None, 5)):  # fmt: skip
        mark(s, day, a, st.id, "call", 25000, 75, pa, spot, iva, t)
        mark(s, day, b, st.id, "call", 25200, -75, pb, spot, ivb, t)
    return st


def test_explain_of_a_paper_position_day(db):
    from finresearch.db import session_scope
    from finresearch.fincalc import posttrade as pt
    from finresearch.fnobook import review

    with session_scope() as s:
        st = three_days(s)
        days = review.explain_days(s, date(2026, 10, 1), date(2026, 11, 1))
    known, unknown = days
    # 20th -> 21st: +75 x (200 - 180) - 75 x (90 - 80) = 1,500 - 750 = 750
    assert (known["position"], known["day"], known["previous"]) == (
        f"paper:{st.id}",
        "2026-10-21",
        "2026-10-20",
    )
    assert known["actual"] == pytest.approx(750)
    legs = [pt.MarkLeg("call", 25000.0, 75, 0.14, 0.15), pt.MarkLeg("call", 25200.0, -75, 0.13, 0.135)]
    g = pt.legs_greek_explain(
        legs, s0=25100, s1=25180, t0=7 / 365, t1=6 / 365, d_days=1, rate=0.065, actual=750
    )
    r = pt.revaluation(legs, s0=25100, s1=25180, t0=7 / 365, t1=6 / 365, rate=0.065, actual=750)
    assert known["greeks"]["residual"] == pytest.approx(g.residual, abs=0.01)
    assert known["greeks"]["delta"] == pytest.approx(g.delta, abs=0.01)
    assert known["revaluation"]["model"] == pytest.approx(r.model, abs=0.01)
    # the spread's max loss is its net debit: (121.20 - 49.50) x 75 = 5,377.50; the floor is 0.5 % of it
    assert known["max_loss"] == pytest.approx(-5377.5)
    assert known["within"] == pt.within_tolerance(residual=g.residual, actual=750, max_loss=-5377.5)
    # 21st -> 22nd: the short call has no price on the 22nd -> unknown, never 0
    assert unknown["actual"] is None and unknown["greeks"] is None and unknown["within"] is None
    assert "price" in unknown["why"]


def test_explain_summary_counts_only_decided_days(db):
    from finresearch.db import session_scope
    from finresearch.fnobook import review

    with session_scope() as s:
        three_days(s)
        days = review.explain_days(s, date(2026, 10, 1), date(2026, 11, 1))
    sm = review.explain_summary(days)
    assert sm["position_days"] == 2 and sm["unknown"] == 1
    assert sm["decided"] == (days[0]["within"] is not None)
    assert sm["target_share"] == 0.8
