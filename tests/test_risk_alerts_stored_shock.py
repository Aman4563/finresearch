"""P4.5 (#398): the nightly common-shock P&L from stored data only (fnobook.scenarios.stored_shock): tonight's marks
(fno_mark settles) on the same-expiry future's settle as the forward, else put-call parity on the bhavcopy settles,
repriced by fincalc.book_scenarios.shock_pnls one session ahead. No chain is read, so no NSE website request. An
unpriced leg, an unknown direction or an unknown forward makes the whole shock unknown; no open leg is exactly 0.
#480: each forward is rolled at its own expiry's carry ln(F/S)/T (the future's settle against the underlying's close,
else a parity fit over the day's market settles), never at the rate; an unknown carry leaves the shock unknown.
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
T6 = 6 / 365  # 21-Oct 15:30 to 27-Oct 15:30 IST
# #479: the RBI repo-rate proxy at the expiry's own 6 days, 5.50 % simple (since 07-Oct-2026) -> continuous
R6 = math.log(1 + 0.055 * 6 / 365) * 365 / 6


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


def _bhav(right: str, strike: float, settle: float, *, expiry: date = EXP, contracts: int | None = None):
    from finresearch.db.models import FnoBhav

    return FnoBhav(day=DAY, symbol="NIFTY", instrument="IDF" if right == "XX" else "IDO", expiry=expiry,
                   strike=D(str(strike)), option_type=right, settle=D(str(settle)), close=D(str(settle)),
                   contracts=contracts)  # fmt: skip


def _mark(i: int, strike: float | None, qty: int | None, price: float | None, *, right: str = "put",
          expiry: date = EXP):  # fmt: skip
    from finresearch.db.models import FnoMark

    return FnoMark(day=DAY, book="paper", leg_key=f"paper_leg:{i}", symbol="NIFTY", expiry=expiry, right=right,
                   strike=None if strike is None else D(str(strike)), qty=qty,
                   price=None if price is None else D(str(price)), spot=D("24000"))  # fmt: skip


def _shock():
    from finresearch.db import session_scope
    from finresearch.fnobook.scenarios import stored_shock

    with session_scope() as s:
        return stored_shock(s, DAY, holidays=set())


def test_the_shock_reprices_tonights_marks_on_the_same_expiry_future(clean) -> None:
    _add(_bhav("XX", 0, 24050.0), _mark(1, 23800, -75, 60.0))
    got, notes = _shock()
    # #479: the RBI repo-rate proxy at the expiry's own 6 days (5.50 % since 07-Oct-2026), named in the notes
    assert any(
        n.startswith("rate: RBI repo-rate proxy: repo 5.50 % in force from 07-Oct-2026") for n in notes
    )
    # #480: rolled at the future's own carry against tonight's close (24,000), named in the notes
    b = math.log(24050 / 24000) / T6
    assert any(n.startswith("carry: NIFTY 27-Oct-2026: 12.66% a year") for n in notes)
    m = bs.Market(session_clock(DAY), R6, {("NIFTY", EXP): 24050.0}, carries={("NIFTY", EXP): bs.Carry(b)})
    leg = bs.BookLeg("NIFTY", EXP, "put", 23800.0, -75, 60.0, "settle")
    want, _ = bs.shock_pnls([leg], m, bs.horizon_clock(session_clock(DAY), 1, set()))
    assert got == pytest.approx(want) and set(got) == set(SHOCK_MOVES)
    assert got[-0.10] < 0  # a short put loses on a fall


def test_regression_a_future_is_rolled_at_its_settles_carry_not_at_the_rate(clean) -> None:
    """#480: one session on (Thu 22-Oct), a long future at 24,050 moves to 24,050 (1 + m) e^{-b/365} with
    b = ln(24,050 / 24,000) / (6/365) = 12.66 % a year; the old rule rolled it at the 6.5 % rate."""
    _add(_bhav("XX", 0, 24050.0), _mark(1, None, 75, 24050.0, right="future"))
    got, _ = _shock()
    b = math.log(24050 / 24000) / T6
    assert b == pytest.approx(0.126604, abs=1e-6)
    for mv in SHOCK_MOVES:
        assert got[mv] == pytest.approx(75 * (24050 * (1 + mv) * math.exp(-b / 365) - 24050), rel=1e-9)
        old = 75 * (24050 * (1 + mv) * math.exp(-0.065 / 365) - 24050)
        assert abs(got[mv] - old) > 250  # 75 x 24,050 (1 + m) (b - 6.5 %) / 365: ₹274 at -10 %, ₹320 at +5 %


def test_without_a_future_or_a_reliable_parity_fit_the_carry_and_the_shock_are_unknown(clean) -> None:
    """#480 (changed on purpose): one strike's settles price the leg (single-strike parity), but they cannot identify
    the carry, so the shock is unknown with the reason instead of rolled at the rate."""
    _add(_bhav("CE", 24000, 150.0), _bhav("PE", 24000, 120.0), _mark(1, 23800, -75, 60.0))
    got, notes = _shock()
    assert got is None
    text_ = " ".join(notes)
    assert "carry" in text_ and "parity" in text_ and "never rolled at the risk-free rate" in text_


def test_without_a_future_the_carry_is_a_parity_fit_over_the_market_settles(clean) -> None:
    """#480: C - P = DF (F - K) at three traded strikes (F = 24,030, DF = e^{-0.06 x 6/365}): the fit gives
    F = 24,030 and the carry ln(24,030 / 24,000) / (6/365) = 7.60 % a year."""
    from finresearch.db import session_scope
    from finresearch.fnobook.scenarios import stored_book

    df, f = math.exp(-0.06 * T6), 24030.0
    rows = [_mark(1, 23800, -75, 60.0)]
    for k, put in ((23900, 70.0), (24000, 110.0), (24100, 160.0)):
        rows += [
            _bhav("PE", k, put, contracts=500),
            _bhav("CE", k, round(put + df * (f - k), 2), contracts=500),
        ]
    _add(*rows)
    with session_scope() as s:
        legs, mk, _ = stored_book(s, DAY)
    assert mk.forwards[("NIFTY", EXP)] == pytest.approx(f, abs=0.02)  # the settles are rounded to the paisa
    b = mk.carries[("NIFTY", EXP)].b
    assert b == pytest.approx(math.log(f / 24000) / T6, abs=1e-4) and b == pytest.approx(0.0760, abs=1e-4)
    assert "parity" in mk.carries[("NIFTY", EXP)].source
    got, _ = _shock()
    want, _ = bs.shock_pnls(legs, mk, bs.horizon_clock(session_clock(DAY), 1, set()))
    assert got is not None and got == pytest.approx(want)


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


def test_the_shock_uses_the_rate_known_before_the_day_never_stored_fbil_rows(clean, monkeypatch) -> None:
    """#479 (changed on purpose: the stored FBIL curve was read before; FBIL is paused, #476): the owner's 91-day
    entry of 20-Oct (5.80 %, no repo change since) is the expiry's rate, held flat to its 6 days,
    r = ln(1 + 0.058 x 6/365) x 365/6; the entry of the day itself (7.00 %) and stored FBIL rows are not used."""
    from finresearch.db import session_scope
    from finresearch.db.models import RateCurve
    from finresearch.refseries import owner_rates
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OWNER", None)  # read the owner_rate table
    with session_scope() as s:
        s.execute(text("TRUNCATE rate_curve, owner_rate"))
        s.add(RateCurve(series="gsec_par", day=date(2026, 10, 20), tenor="0.25Y", tenor_years=D("0.25"),
                        rate=D("0.06")))  # fmt: skip
        owner_rates.add(s, date(2026, 10, 20), "91D", D("5.80"), "test entry", today=DAY)
        owner_rates.add(s, DAY, "91D", D("7.00"), "test entry of the day", today=DAY)
    try:
        from finresearch.fnobook.scenarios import stored_book

        _add(_bhav("CE", 24000, 150.0), _bhav("PE", 24000, 120.0), _mark(1, 23800, -75, 60.0))
        r = math.log(1 + 0.058 * 6 / 365) * 365 / 6
        with session_scope() as s:
            _, mk, notes = stored_book(s, DAY)
        # #480: it is the expiry's own rate, and it is what the single-strike parity forward is read at
        assert mk.rate_for(EXP) == pytest.approx(r, rel=1e-12)
        assert mk.forwards[("NIFTY", EXP)] == pytest.approx(
            24000 + math.exp(r * T6) * (150.0 - 120.0), rel=1e-12
        )
        assert any(n.startswith("rate: owner-entered rate: 91D for 20-Oct-2026") for n in notes)
        _add(_bhav("XX", 0, 24050.0))  # with the future the carry is known, so the shock is priced, at r
        got, notes = _shock()
        m = bs.Market(session_clock(DAY), r, {("NIFTY", EXP): 24050.0},
                      carries={("NIFTY", EXP): bs.Carry(math.log(24050 / 24000) / T6)})  # fmt: skip
        want, _ = bs.shock_pnls([bs.BookLeg("NIFTY", EXP, "put", 23800.0, -75, 60.0, "settle")], m,
                                bs.horizon_clock(session_clock(DAY), 1, set()))  # fmt: skip
        assert got == pytest.approx(want, rel=1e-9)
        assert any(n.startswith("rate: owner-entered rate: 91D for 20-Oct-2026") for n in notes)
    finally:
        with session_scope() as s:
            s.execute(text("TRUNCATE rate_curve, owner_rate"))


def test_each_expiry_is_discounted_at_its_own_tenors_rate_not_the_longest_legs(clean, monkeypatch) -> None:
    """#480: signals.rates is asked once per expiry, at that expiry's own tenor and as known before the
    day; the old rule asked once, at the longest leg's tenor, and used that rate for every expiry."""
    from finresearch.db import session_scope
    from finresearch.fnobook.scenarios import stored_book
    from finresearch.signals import rates

    asked = []

    def fake(s, day, t, *, before=False):
        asked.append((day, round(t * 365, 6), before))
        return {"known": True, "rate": 0.05 + 0.02 * t, "par_yield": None, "tenor_years": t, "as_of": "2026-10-20",
                "fallback": False, "source": f"a test curve at {t:.4f} years", "note": "test"}  # fmt: skip

    monkeypatch.setattr(rates, "rate_on", fake)
    far = date(2027, 3, 30)  # 160 days on
    _add(_bhav("XX", 0, 24050.0), _bhav("XX", 0, 24400.0, expiry=far), _mark(1, 23800, -75, 60.0),
         _mark(2, None, 75, 24400.0, right="future", expiry=far))  # fmt: skip
    with session_scope() as s:
        _, mk, notes = stored_book(s, DAY)
    assert sorted(asked) == [(DAY, 6.0, True), (DAY, 160.0, True)]
    assert mk.rate_for(EXP) == pytest.approx(0.05 + 0.02 * T6) and mk.rate_for(far) == pytest.approx(
        0.05 + 0.02 * 160 / 365
    )
    assert {n for n in notes if n.startswith("rate: ")} == {
        "rate: a test curve at 0.0164 years: 5.03% continuous (the 27-Oct-2026 expiry)",
        "rate: a test curve at 0.4384 years: 5.88% continuous (the 30-Mar-2027 expiry)",
    }


def test_a_known_shock_reading_names_the_carry_as_well_as_the_rate() -> None:
    """#480: the risk alert's shock detail says which rate discounted the book and which carry rolled each forward."""
    from finresearch.alerts import risk_readers as rr

    notes = ["rate: RBI repo-rate proxy: repo 5.50 % in force from 07-Oct-2026: 5.49% continuous (the 27-Oct-2026 "
             "expiry)",
             "carry: NIFTY 27-Oct-2026: 12.66% a year (the same-expiry future's settle on 21-Oct-2026 against the "
             "underlying's close)", "an unrelated note"]  # fmt: skip
    rows = rr.book_readings({-0.03: -1500.0, 0.05: 900.0}, notes, 100_000.0, None, None, book_open=True)
    shock = next(rd for rid, _, rd in rows if rid == "limit_shock")
    assert notes[0] in shock.detail and notes[1] in shock.detail and notes[2] not in shock.detail
