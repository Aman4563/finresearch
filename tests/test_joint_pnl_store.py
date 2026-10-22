"""The stored daily joint P&L (#397 (b), portfolio.joint_pnl): one append-only joint_pnl_day row per evening from
the canonical value history, fno_mark and index_eod, explained from the previous evening's stored snapshot.
Synthetic holdings, legs and prices only; no network."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

D0, D1, EXP = date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 27)
NOT_CONNECTED = {"status": "done", "real_note": "Groww positions not_connected", "real_unresolved": 0}


@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(
            text("TRUNCATE joint_pnl_day, fno_mark, fno_bhav, index_eod, alert_eval_slot, portfolio_setting")
        )
    return env


def _days(n: int, end: date) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _hist(closes_d0_d1=(50.0, 48.5), *, value: float = 97_000.0, beta_days: int = 260):
    """A benchmark-built history ending on D1: one equity whose closes equal NIFTYBEES' (beta exactly 1) over
    `beta_days`, with the given closes on D0 and D1."""
    days = _days(beta_days, D1)
    bench = [100.0 * (1 + 0.01 * math.sin(i)) for i in range(len(days))]
    eq = dict(zip(days, bench, strict=True))
    eq[D0], eq[D1] = closes_d0_d1
    eq = {d: c for d, c in eq.items() if c is not None}  # a missing close is absent, as in a built history
    b = {d: v for d, v in eq.items()}
    pos = [SimpleNamespace(key="NSE:FAKEEQ", name="Fake Equity Ltd", value=value, tax_class="equity")]
    return SimpleNamespace(ok=True, days=days, positions=pos, closes={"NSE:FAKEEQ": eq}, factors={},
                           benchmark=SimpleNamespace(closes=b, factors=None))  # fmt: skip


def _series(s, rows):
    from finresearch.portfolio import series

    days, value, invested, complete = zip(*rows, strict=True)
    h = SimpleNamespace(ok=True, days=list(days), value=list(value), invested=list(invested), complete=list(complete),
                        start_reason="", excluded=[], warnings=[], reason=None)  # fmt: skip
    series.save(s, h, series.current_fingerprint(s), D1)


def _setup(*, d1_price: float | None = 24600.0, d1_slot=NOT_CONNECTED, nifty_d1: float | None = 24500.0,
           series_rows=None, prev_row: bool = True, extra_close: date | None = None):  # fmt: skip
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, FnoBhav, FnoMark, IndexEod, JointPnlDay

    with session_scope() as s:
        for d, c in ((D0, 25000.0), (D1, nifty_d1), (extra_close, 24800.0)):
            if d is not None and c is not None:
                s.add(IndexEod(day=d, symbol="NIFTY", index_name="Nifty 50", close=D(str(c))))
        for d, p in ((D0, 25100.0), (D1, d1_price)):
            s.add(FnoMark(day=d, book="paper", leg_key="paper_leg:1", symbol="NIFTY", expiry=EXP, right="future",
                          qty=75, price=None if p is None else D(str(p)), spot=D("25000")))  # fmt: skip
            if p is not None:
                s.add(FnoBhav(day=d, symbol="NIFTY", instrument="IDF", expiry=EXP, strike=D(0), option_type="XX",
                              settle=D(str(p))))  # fmt: skip
        s.add(AlertEvalSlot(slot=f"fno_marks:{D0}", result=NOT_CONNECTED))
        if d1_slot is not None:
            s.add(AlertEvalSlot(slot=f"fno_marks:{D1}", result=d1_slot))
        _series(s, series_rows or [(D0, 100_000.0, 80_000.0, True), (D1, 97_000.0, 80_000.0, True)])
        if prev_row:
            s.add(JointPnlDay(day=D0, usable=False, reasons=["fixture"], notes=[], l_curve=[], snapshot_reasons=[],
                              d_holdings=D("120000"), holdings=[{"key": "NSE:FAKEEQ", "value": 100000.0,
                                                                  "beta": 1.2}]))  # fmt: skip


def _record(hist=None):
    from finresearch.db import session_scope
    from finresearch.portfolio import joint_pnl as jpl

    with session_scope() as s:
        return jpl.record(s, D1, _hist() if hist is None else hist, holidays=set())


def _row():
    from finresearch.db import session_scope
    from finresearch.db.models import JointPnlDay

    with session_scope() as s:
        r = s.query(JointPnlDay).filter_by(day=D1).one()
        s.expunge(r)
        return r


def test_a_known_session_stores_actual_and_explained_from_the_previous_evening(db):
    from finresearch.fincalc import joint_pnl as jp
    from finresearch.fincalc.dates import session_clock, years_to_expiry

    _setup()
    assert _record()["written"] is True
    r = _row()
    assert r.usable and r.reasons == [] and r.prev_day == D0
    assert float(r.nifty_move) == pytest.approx(24500 / 25000 - 1)
    assert float(r.holdings_actual) == pytest.approx(-3000.0)  # 97,000 - 1,00,000, no money in
    assert float(r.fno_actual) == pytest.approx(75 * (24600 - 25100))
    assert float(r.holdings_own) == pytest.approx(100_000 * (48.5 / 50 - 1))  # D0's value x its own return
    roll = math.exp(
        -jp.RATE * (years_to_expiry(EXP, session_clock(D0)) - years_to_expiry(EXP, session_clock(D1)))
    )
    model = 75 * (25100 * (24500 / 25000) * roll - 25100)  # D0's future at NIFTY's move, one session on
    assert float(r.fno_model) == pytest.approx(model, abs=0.01)
    assert float(r.explained) == pytest.approx(-3000.0 + model, abs=0.01)
    assert float(r.actual) == pytest.approx(-3000.0 - 37_500.0)
    assert float(r.holdings_beta) == pytest.approx(
        120_000 * (24500 / 25000 - 1)
    )  # D0's stored D_holdings x m


def test_the_evening_snapshot_stores_d_total_and_l_m_for_the_next_session(db):
    from finresearch.fincalc import book_scenarios as bs

    _setup()
    _record()
    r = _row()
    assert r.holdings == [{"key": "NSE:FAKEEQ", "value": 97000.0, "beta": pytest.approx(1.0)}]
    assert float(r.d_holdings) == pytest.approx(97_000.0, rel=1e-6)  # beta 1 x 97,000
    assert float(r.d_fno) == pytest.approx(
        75 * 24600.0
    )  # a future: delta 1 x its forward x qty, NIFTY beta 1
    assert float(r.d_total) == pytest.approx(97_000.0 + 75 * 24600.0, rel=1e-6)
    assert [x["move"] for x in r.l_curve] == list(bs.MOVES)
    assert all(x["total"] is not None for x in r.l_curve) and r.snapshot_reasons == []


def test_recording_twice_writes_once_and_never_updates(db):
    _setup()
    _record()
    first = _row()
    assert _record(_hist((50.0, 40.0)))["written"] is False
    again = _row()
    assert (again.id, again.holdings_own, again.recorded_at) == (
        first.id,
        first.holdings_own,
        first.recorded_at,
    )


@pytest.mark.parametrize(
    ("kw", "hist", "needle"),
    [
        ({"prev_row": False}, None, "no previous evening"),
        ({"d1_price": None}, None, "settle price is unknown"),
        (
            {"d1_slot": {**NOT_CONNECTED, "real_note": "Groww positions not read today (last read never)"}},
            None,
            "not read today",
        ),
        ({"d1_slot": None}, None, "marks"),
        ({"d1_slot": {**NOT_CONNECTED, "real_unresolved": 1}}, None, "instruments"),
        ({"nifty_d1": None}, None, "NIFTY"),
        ({"series_rows": [(D0, 100_000.0, 80_000.0, True), (D1, 97_000.0, 80_000.0, False)]}, None, "stale"),
        (
            {"series_rows": [(D0 - timedelta(days=1), 1.0, 1.0, True), (D0, 100_000.0, 80_000.0, True)]},
            None,
            "value history",
        ),
        ({"extra_close": None}, _hist((50.0, None)), "own return"),  # type: ignore[arg-type]
    ],
)
def test_an_unknown_component_makes_the_day_unusable_with_its_reason(db, kw, hist, needle):
    _setup(**kw)
    _record(hist)
    r = _row()
    assert not r.usable and needle in " ".join(r.reasons)
    assert r.actual is None or r.explained is None or r.nifty_move is None  # never 0 in its place


def test_a_session_missing_between_two_evenings_is_unusable(db):
    # the last stored evening is the session before D0, and NIFTY closed on D0 in between: the P&L would span two
    # sessions, so it is not one session's P&L
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEod, JointPnlDay

    _setup()
    before = D0 - timedelta(days=1)
    with session_scope() as s:
        s.add(IndexEod(day=before, symbol="NIFTY", index_name="Nifty 50", close=D("25000")))
        s.query(JointPnlDay).filter_by(day=D0).update({"day": before})
    _record()
    r = _row()
    assert not r.usable and "session" in " ".join(r.reasons)
