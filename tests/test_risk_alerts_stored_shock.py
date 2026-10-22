"""P4.5 (#398): the nightly common-shock P&L from stored data only (fnobook.scenarios.stored_shock): tonight's marks
(fno_mark settles) on the same-expiry future's settle as the forward, else put-call parity on the bhavcopy settles,
repriced by fincalc.book_scenarios.shock_pnls one session ahead. No chain is read, so no NSE website request. An
unpriced leg, an unknown direction or an unknown forward makes the whole shock unknown; no open leg is exactly 0.
Test database only; synthetic rows."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import session_clock
from finresearch.fincalc.tradeplan import SHOCK_MOVES

DAY, EXP = date(2026, 10, 21), date(2026, 10, 27)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav, fno_mark CASCADE"))

    wipe()
    yield env
    wipe()


def _add(*rows) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        s.add_all(rows)


def _bhav(right: str, strike: float, settle: float):
    from finresearch.db.models import FnoBhav

    return FnoBhav(day=DAY, symbol="NIFTY", instrument="IDF" if right == "XX" else "IDO", expiry=EXP,
                   strike=D(str(strike)), option_type=right, settle=D(str(settle)), close=D(str(settle)))  # fmt: skip


def _mark(i: int, strike: float, qty: int | None, price: float | None):
    from finresearch.db.models import FnoMark

    return FnoMark(day=DAY, book="paper", leg_key=f"paper_leg:{i}", symbol="NIFTY", expiry=EXP, right="put",
                   strike=D(str(strike)), qty=qty, price=None if price is None else D(str(price)), spot=D("24000"))  # fmt: skip


def _shock():
    from finresearch.db import session_scope
    from finresearch.fnobook.scenarios import stored_shock

    with session_scope() as s:
        return stored_shock(s, DAY, holidays=set())


def test_the_shock_reprices_tonights_marks_on_the_same_expiry_future(clean) -> None:
    _add(_bhav("XX", 0, 24050.0), _mark(1, 23800, -75, 60.0))
    got, _ = _shock()
    m = bs.Market(session_clock(DAY), 0.065, {("NIFTY", EXP): 24050.0})
    leg = bs.BookLeg("NIFTY", EXP, "put", 23800.0, -75, 60.0, "settle")
    want, _ = bs.shock_pnls([leg], m, bs.horizon_clock(session_clock(DAY), 1, set()))
    assert got == pytest.approx(want) and set(got) == set(SHOCK_MOVES)
    assert got[-0.10] < 0  # a short put loses on a fall


def test_without_a_future_the_forward_is_put_call_parity_on_the_settles(clean) -> None:
    _add(_bhav("CE", 24000, 150.0), _bhav("PE", 24000, 120.0), _mark(1, 23800, -75, 60.0))
    got, _ = _shock()
    t = (session_clock(EXP) - session_clock(DAY)).total_seconds() / (365 * 86400)
    f = 24000 + math.exp(0.065 * t) * (150.0 - 120.0)
    m = bs.Market(session_clock(DAY), 0.065, {("NIFTY", EXP): f})
    want, _ = bs.shock_pnls([bs.BookLeg("NIFTY", EXP, "put", 23800.0, -75, 60.0, "settle")], m,
                            bs.horizon_clock(session_clock(DAY), 1, set()))  # fmt: skip
    assert got == pytest.approx(want, rel=1e-6)


def test_an_unpriced_or_undirected_leg_makes_the_shock_unknown(clean) -> None:
    _add(_bhav("XX", 0, 24050.0), _mark(1, 23800, -75, None))
    got, notes = _shock()
    assert got is None and notes
    with __import__("finresearch.db", fromlist=["session_scope"]).session_scope() as s:
        s.execute(text("TRUNCATE fno_mark"))
    _add(_mark(2, 23800, None, 60.0))
    got, notes = _shock()
    assert got is None and "long or short" in " ".join(notes)


def test_no_open_leg_is_exactly_zero(clean) -> None:
    got, notes = _shock()
    assert got == dict.fromkeys(SHOCK_MOVES, 0.0) and notes == []


def test_a_stock_underlying_without_stored_futures_history_has_an_unknown_beta_so_the_shock_is_unknown(
    clean,
) -> None:
    from finresearch.db.models import FnoMark

    _add(FnoMark(day=DAY, book="paper", leg_key="paper_leg:9", symbol="EXAMPLESTK", expiry=EXP, right="put",
                 strike=D("1000"), qty=-500, price=D("12"), spot=D("1010")),
         _bhav_sym("EXAMPLESTK", 1015.0))  # fmt: skip
    got, notes = _shock()
    assert got is None and notes  # the beta read (its own session) says why; never 0


def _bhav_sym(sym: str, settle: float):
    from finresearch.db.models import FnoBhav

    return FnoBhav(day=DAY, symbol=sym, instrument="STF", expiry=EXP, strike=D(0), option_type="XX",
                   settle=D(str(settle)), close=D(str(settle)))  # fmt: skip
