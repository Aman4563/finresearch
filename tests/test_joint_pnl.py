"""The stored daily joint P&L's arithmetic (#397 (b), fincalc.joint_pnl): the actual P&L of both books between two
evenings and its explained parts. Pure; synthetic numbers only, every expected value worked by hand."""

from __future__ import annotations

from datetime import date

import pytest

from finresearch.fincalc import joint_pnl as jp

D0, D1 = date(2026, 10, 20), date(2026, 10, 21)
EXP = date(2026, 10, 27)


def _m(key: str, price: float | None, *, qty: int | None = -75, right: str = "put", strike: float = 25000.0,
       expiry: date = EXP, spot: float | None = 25500.0, iv: float | None = 0.12, t: float = 0.02,
       symbol: str = "NIFTY") -> jp.Mark:  # fmt: skip
    return jp.Mark(key, symbol, expiry, right, strike, qty, price, spot, iv, t)


# --------------------------------------------------------------------------- the book's actual P&L
def test_fno_actual_is_signed_qty_times_the_settle_change_of_carried_legs():
    prev = [_m("paper_leg:1", 100.0), _m("paper_leg:2", 40.0, qty=75, strike=24500.0)]
    cur = [_m("paper_leg:1", 130.0), _m("paper_leg:2", 52.0, qty=75, strike=24500.0)]
    got, why = jp.fno_actual(prev, cur, D1)
    # -75 x (130 - 100) + 75 x (52 - 40) = -2250 + 900
    assert got == pytest.approx(-1350.0) and why == []


def test_fno_actual_of_an_empty_book_is_exactly_zero():
    assert jp.fno_actual([], [], D1) == (0.0, [])


def test_a_leg_that_expired_at_the_previous_close_carries_nothing():
    prev = [_m("paper_leg:1", 3.0, expiry=D0), _m("paper_leg:2", 100.0)]
    cur = [_m("paper_leg:2", 90.0)]
    got, why = jp.fno_actual(prev, cur, D1)
    assert got == pytest.approx(-75 * (90.0 - 100.0)) and why == []


@pytest.mark.parametrize(
    ("prev", "cur", "needle"),
    [
        ([_m("paper_leg:1", 100.0)], [], "changed"),  # closed during the day
        ([], [_m("paper_leg:1", 100.0)], "changed"),  # opened during the day
        ([_m("paper_leg:1", 100.0)], [_m("paper_leg:1", 100.0, qty=-150)], "changed"),
        ([_m("groww:X", 100.0, qty=None)], [_m("groww:X", 90.0, qty=None)], "long or short"),
        ([_m("paper_leg:1", None)], [_m("paper_leg:1", 90.0)], "price"),
        ([_m("paper_leg:1", 100.0)], [_m("paper_leg:1", None)], "price"),
    ],
)
def test_fno_actual_is_unknown_with_a_reason_never_zero(prev, cur, needle):
    got, why = jp.fno_actual(prev, cur, D1)
    assert got is None and why and needle in " ".join(why)


# --------------------------------------------------------------------------- the holdings' actual P&L
def test_holdings_actual_is_the_value_change_less_the_days_net_money_in():
    # value 1,00,000 -> 1,12,000 with 10,000 bought that day: the market made 2,000
    got, why = jp.holdings_actual(jp.SeriesDay(D0, 100_000.0, 80_000.0, True),
                                  jp.SeriesDay(D1, 112_000.0, 90_000.0, True))  # fmt: skip
    assert got == pytest.approx(2_000.0) and why == []


@pytest.mark.parametrize(("c0", "c1"), [(False, True), (True, False)])
def test_holdings_actual_on_a_stale_priced_day_is_unknown(c0, c1):
    got, why = jp.holdings_actual(jp.SeriesDay(D0, 1.0, 1.0, c0), jp.SeriesDay(D1, 2.0, 1.0, c1))
    assert got is None and "stale" in why[0]


def test_holdings_actual_without_a_series_day_is_unknown():
    got, why = jp.holdings_actual(None, jp.SeriesDay(D1, 2.0, 1.0, True))
    assert got is None and why


# --------------------------------------------------------------------------- the holdings' explained P&L
def test_own_move_is_each_previous_evening_value_times_its_own_return():
    snap = [jp.Snap("NSE:FAKEEQ", 100_000.0, 1.2), jp.Snap("MF:999", 50_000.0, None)]
    got, why = jp.own_move(snap, {"NSE:FAKEEQ": -0.02, "MF:999": 0.001})
    assert got == pytest.approx(100_000 * -0.02 + 50_000 * 0.001) and why == []


def test_own_move_with_a_missing_return_is_unknown_and_names_it():
    got, why = jp.own_move([jp.Snap("NSE:FAKEEQ", 1.0, 1.0)], {"NSE:FAKEEQ": None})
    assert got is None and "NSE:FAKEEQ" in why[0]


def test_beta_move_is_the_previous_evenings_d_holdings_times_niftys_move():
    assert jp.beta_move(150_000.0, -0.02) == pytest.approx(-3_000.0)
    assert jp.beta_move(None, -0.02) is None


# --------------------------------------------------------------------------- the book's explained P&L
def _priced(
    key: str, spot: float, iv: float, t: float, *, qty: int = -75, strike: float = 25000.0
) -> jp.Mark:
    """A mark whose settle IS the Black-Scholes price (as fnobook.marks implies the IV from it), rounded like
    fno_mark (price 2 dp, IV 6 dp)."""
    from finresearch.fincalc import options as o

    p = round(o.bs_price("put", spot, strike, t, jp.RATE, iv), 2)
    return jp.Mark(key, "NIFTY", EXP, "put", strike, qty, p, spot, round(o.implied_vol("put", p, spot, strike, t,
                                                                                         jp.RATE), 6), t)  # fmt: skip


def test_the_market_revaluation_reproduces_the_settles_by_construction():
    # Why the forward test's explained book P&L is the model (L at the realised move), not this: repricing each leg
    # at the day's own spot and own implied IV gives back the settle change up to rounding, so it cannot fail.
    prev = [_priced("paper_leg:1", 25500.0, 0.12, 5 / 365)]
    cur = [_priced("paper_leg:1", 25000.0, 0.15, 4 / 365)]
    actual, _ = jp.fno_actual(prev, cur, D1)
    market, why = jp.fno_market(prev, cur, D1)
    assert why == [] and actual != 0 and market == pytest.approx(actual, abs=0.75 * 75 / 100 + 1e-6)


def test_the_market_revaluation_of_an_empty_book_is_zero_and_unknown_iv_is_unknown():
    assert jp.fno_market([], [], D1) == (0.0, [])
    prev, cur = [_m("paper_leg:1", 100.0, iv=None)], [_m("paper_leg:1", 90.0)]
    got, why = jp.fno_market(prev, cur, D1)
    assert got is None and why


def _market(beta_known: bool = True):
    from datetime import datetime, time
    from types import SimpleNamespace

    from finresearch.fincalc import book_scenarios as bs
    from finresearch.fincalc.dates import IST

    now = datetime.combine(D0, time(15, 30), tzinfo=IST)
    betas = (
        {"FAKESTK": SimpleNamespace(beta=1.2, lo=1.0, hi=1.4, known=True, reason=None)} if beta_known else {}
    )
    fw = {("NIFTY", EXP): 25600.0, ("FAKESTK", EXP): 1000.0}
    return bs.Market(now, jp.RATE, fw, betas), bs.horizon_clock(now, 1)


def test_the_model_revalues_the_previous_evenings_legs_at_niftys_realised_move():
    import math

    from finresearch.fincalc import book_scenarios as bs
    from finresearch.fincalc.dates import years_to_expiry

    m, at = _market()
    legs = [bs.BookLeg("NIFTY", EXP, "future", 0.0, 75, 25600.0, "settle", "paper_leg:1")]
    got, why = jp.fno_model(legs, m, at, -0.02)
    # a future: F' = F (1 + m) e^{-r (T0 - T1)}, one session on
    roll = math.exp(-jp.RATE * (years_to_expiry(EXP, m.now) - years_to_expiry(EXP, at)))
    assert why == [] and got == pytest.approx(75 * (25600.0 * 0.98 * roll - 25600.0))


def test_the_model_takes_the_worse_end_of_a_stocks_beta_band_and_an_unknown_beta_is_unknown():
    import math

    from finresearch.fincalc import book_scenarios as bs
    from finresearch.fincalc.dates import years_to_expiry

    leg = [bs.BookLeg("FAKESTK", EXP, "future", 0.0, 500, 1000.0, "settle", "paper_leg:9")]
    m, at = _market()
    got, _ = jp.fno_model(leg, m, at, -0.02)
    roll = math.exp(-jp.RATE * (years_to_expiry(EXP, m.now) - years_to_expiry(EXP, at)))
    assert got == pytest.approx(
        500 * (1000.0 * (1 - 1.4 * 0.02) * roll - 1000.0)
    )  # long: the hi end is worse
    m, at = _market(beta_known=False)
    got, why = jp.fno_model(leg, m, at, -0.02)
    assert got is None and why
    assert jp.fno_model([], m, at, -0.02) == (0.0, [])


# --------------------------------------------------------------------------- one day
def test_a_day_with_every_part_known_is_usable_and_sums_its_parts():
    d = jp.day(D1, D0, nifty_move=-0.02, holdings=(2000.0, []), fno=(-1350.0, []), own=(1900.0, []),
               beta=-3000.0, model=(-1200.0, []), market=(-1349.0, []))  # fmt: skip
    assert d.usable and d.reasons == ()
    assert (d.actual, d.explained) == (pytest.approx(650.0), pytest.approx(700.0))
    assert d.holdings_beta == -3000.0 and d.fno_market == -1349.0


@pytest.mark.parametrize("part", ["nifty_move", "holdings", "fno", "own", "model"])
def test_any_unknown_part_makes_the_day_unusable_with_its_reason(part):
    kw = dict(nifty_move=-0.02, holdings=(2000.0, []), fno=(-1350.0, []), own=(1900.0, []), beta=-3000.0,
              model=(-1200.0, []), market=(-1349.0, []))  # fmt: skip
    kw[part] = None if part == "nifty_move" else (None, [f"{part} is unknown"])
    d = jp.day(D1, D0, **kw)  # type: ignore[arg-type]
    assert not d.usable and d.reasons and (d.actual is None or d.explained is None or d.nifty_move is None)


def test_the_side_figures_never_make_a_day_unusable():
    d = jp.day(D1, D0, nifty_move=-0.02, holdings=(2000.0, []), fno=(0.0, []), own=(1900.0, []), beta=None,
               model=(0.0, []), market=(None, ["an IV is unknown"]))  # fmt: skip
    assert d.usable and d.holdings_beta is None and d.fno_market is None and "an IV is unknown" in d.notes
