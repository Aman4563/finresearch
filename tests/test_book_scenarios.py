"""Book-level exposures and the full-revaluation scenario engine (#394, fincalc.book_scenarios). Synthetic legs only.
Expected values are computed independently in the tests (statistics.NormalDist, hand arithmetic), never by calling
the code under test."""

from __future__ import annotations

import math
from datetime import date, datetime
from statistics import NormalDist

import pytest

from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import IST

N = NormalDist()
NOW = datetime(2026, 10, 22, 15, 30, tzinfo=IST)  # a Thursday, at the close: T = calendar days / 365
EXP = date(2026, 11, 26)  # 35 days later
T35 = 35 / 365


def _b76(right: str, f: float, k: float, t: float, r: float, v: float) -> float:
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    d2 = d1 - v * math.sqrt(t)
    c = math.exp(-r * t) * (f * N.cdf(d1) - k * N.cdf(d2))
    return c if right == "call" else c - math.exp(-r * t) * (f - k)


def test_black76_matches_hand_values():
    # F = K = 100, T = 1, r = 0, sigma = 20 %: 100 x (2 N(0.1) - 1) = 7.9656
    assert bs.b76_price("call", 100, 100, 1.0, 0.0, 0.2) == pytest.approx(7.965567, abs=1e-5)
    assert bs.b76_price("call", 100, 100, 1.0, 0.05, 0.2) == pytest.approx(
        7.965567 * math.exp(-0.05), abs=1e-5
    )
    assert bs.b76_price("put", 24000, 23500, T35, 0.065, 0.14) == pytest.approx(
        _b76("put", 24000, 23500, T35, 0.065, 0.14), rel=1e-9
    )
    iv = bs.b76_iv("call", _b76("call", 24000, 24500, T35, 0.065, 0.13), 24000, 24500, T35, 0.065)
    assert iv == pytest.approx(0.13, abs=1e-6)


def test_parity_forward():
    # C - P = e^{-rT}(F - K)  =>  F = K + e^{rT}(C - P)
    c, p = _b76("call", 24100, 24000, T35, 0.065, 0.14), _b76("put", 24100, 24000, T35, 0.065, 0.14)
    assert bs.parity_forward(24000, c, p, T35, 0.065) == pytest.approx(24100, abs=1e-6)


def _roll(days: float) -> float:
    """The forward rolled `days` closer to expiry at the same spot: F(T') = F e^{-r (T - T')}, r = 6.5 %."""
    return math.exp(-0.065 * days / 365)


def _mkt(**kw):
    base = {"now": NOW, "rate": 0.065, "forwards": {("NIFTY", EXP): 24000.0}, "betas": {}, "nifty": 23900.0}
    return bs.Market(**{**base, **kw})


def test_leg_iv_is_inverted_from_the_mark_on_the_forward():
    mark = _b76("put", 24000, 23500, T35, 0.065, 0.15)
    st = bs.price_legs([bs.BookLeg("NIFTY", EXP, "put", 23500, -75, mark, "mid")], _mkt())[0]
    assert st.reason is None and st.t == pytest.approx(T35)
    assert st.iv == pytest.approx(0.15, abs=1e-6)
    assert st.value == pytest.approx(mark, abs=1e-6)


@pytest.mark.parametrize(
    ("leg", "mkt", "why"),
    [
        (bs.BookLeg("NIFTY", EXP, "put", 23500, -75, None, None), {}, "price"),
        (bs.BookLeg("NIFTY", EXP, "put", 23500, -75, 30000.0, "mid"), {}, "no-arbitrage"),
        (bs.BookLeg("NIFTY", EXP, "put", 23500, -75, 300.0, "mid"), {"forwards": {}}, "forward"),
        (bs.BookLeg("NIFTY", date(2026, 10, 22), "put", 23500, -75, 3.0, "mid"), {}, "expired"),
        (bs.BookLeg("NIFTY", EXP, "future", 0, 75, None, None), {"forwards": {}}, "forward"),
    ],
)
def test_unknown_inputs_leave_the_leg_unpriced_with_a_reason(leg, mkt, why):
    st = bs.price_legs([leg], _mkt(**mkt))[0]
    assert st.iv is None and st.value is None
    assert why in st.reason


def test_ltp_mark_is_flagged():
    mark = _b76("call", 24000, 24500, T35, 0.065, 0.13)
    st = bs.price_legs([bs.BookLeg("NIFTY", EXP, "call", 24500, 75, mark, "ltp")], _mkt())[0]
    assert st.reason is None and any("LTP" in f for f in st.flags)


# --------------------------------------------------------------------------- exposures
def _greeks76(right: str, f: float, k: float, t: float, r: float, v: float) -> dict[str, float]:
    d1 = (math.log(f / k) + v * v * t / 2) / (v * math.sqrt(t))
    df, phi = math.exp(-r * t), N.pdf(d1)
    delta = df * N.cdf(d1) if right == "call" else -df * N.cdf(-d1)
    gamma = df * phi / (f * v * math.sqrt(t))
    vega = f * df * phi * math.sqrt(t) / 100
    theta = (-f * df * phi * v / (2 * math.sqrt(t)) + r * _b76(right, f, k, t, r, v)) / 365
    return {"delta": delta, "gamma": gamma, "vega": vega, "theta": theta}


def test_exposures_per_leg_and_per_bucket():
    put = _b76("put", 24000, 23500, T35, 0.065, 0.15)
    near = date(2026, 10, 27)  # 5 days: this week
    legs = [
        bs.BookLeg("NIFTY", EXP, "put", 23500, -75, put, "mid"),
        bs.BookLeg("NIFTY", EXP, "future", 0, 75, 24000.0, "mid"),
        bs.BookLeg("NIFTY", near, "future", 0, -150, 23950.0, "mid"),
    ]
    ex = bs.exposures(legs, _mkt(forwards={("NIFTY", EXP): 24000.0, ("NIFTY", near): 23950.0}))
    g = _greeks76("put", 24000, 23500, T35, 0.065, 0.15)
    later = ex.row("NIFTY", "later")
    assert later.exposure.rs_delta == pytest.approx(g["delta"] * 24000 * -75 + 24000 * 75, rel=1e-6)
    assert later.exposure.rs_gamma_1pct == pytest.approx(0.5 * g["gamma"] * 24000**2 * 1e-4 * -75, rel=1e-6)
    assert later.exposure.vega_pt == pytest.approx(g["vega"] * -75, rel=1e-6)
    assert later.exposure.theta_day == pytest.approx(g["theta"] * -75, rel=1e-6)
    assert ex.row("NIFTY", "this_week").exposure.rs_delta == pytest.approx(23950 * -150)
    assert ex.complete and ex.reasons == ()
    total = later.exposure.rs_delta + 23950 * -150
    assert ex.total.rs_delta == pytest.approx(total)
    assert ex.nifty_delta == pytest.approx(total / 23900)  # NIFTY beta = 1, in NIFTY units


def test_buckets():
    assert bs.bucket(date(2026, 10, 29), date(2026, 10, 22)) == "this_week"  # 7 days
    assert bs.bucket(date(2026, 10, 30), date(2026, 10, 22)) == "this_month"
    assert (
        bs.bucket(date(2026, 11, 3), date(2026, 10, 28)) == "this_week"
    )  # across a month end: the week wins
    assert bs.bucket(date(2026, 11, 24), date(2026, 10, 22)) == "later"


def test_an_unpriced_leg_makes_its_row_and_the_total_unknown_never_zero():
    legs = [
        bs.BookLeg("NIFTY", EXP, "future", 0, 75, 24000.0, "mid"),
        bs.BookLeg("NIFTY", EXP, "put", 23500, -75, None, None),
        bs.BookLeg("BANKNIFTY", EXP, "future", 0, 35, 52000.0, "mid"),
    ]
    ex = bs.exposures(legs, _mkt(forwards={("NIFTY", EXP): 24000.0, ("BANKNIFTY", EXP): 52000.0}))
    assert not ex.complete and ex.total is None and ex.nifty_delta is None
    assert ex.row("NIFTY", "later").exposure is None
    assert ex.row("BANKNIFTY", "later").exposure.rs_delta == pytest.approx(52000 * 35)
    assert any("23500" in r for r in ex.reasons)


def test_nifty_equivalent_delta_needs_every_beta():
    legs = [bs.BookLeg("BANKNIFTY", EXP, "future", 0, 35, 52000.0, "mid")]
    fw = {("BANKNIFTY", EXP): 52000.0}
    ex = bs.exposures(legs, _mkt(forwards=fw))
    assert ex.complete and ex.total.rs_delta == pytest.approx(52000 * 35)
    assert ex.nifty_delta is None and any("beta" in r for r in ex.nifty_delta_reasons)
    ex = bs.exposures(legs, _mkt(forwards=fw, betas={"BANKNIFTY": 1.2}))
    assert ex.nifty_delta == pytest.approx(1.2 * 52000 * 35 / 23900)


# --------------------------------------------------------------------------- revaluation grid
def test_vol_response_follows_the_measured_nifty_slopes():
    assert bs.vol_response(-0.05) == pytest.approx(5 * 1.04)  # +1.04 vol points per 1 % fall
    assert bs.vol_response(0.03) == pytest.approx(-3 * 0.76)  # -0.76 per 1 % rise
    assert bs.vol_response(0.0) == 0.0


def test_horizon_clock_counts_weekday_sessions_to_the_close():
    assert bs.horizon_clock(NOW, 1) == datetime(2026, 10, 23, 15, 30, tzinfo=IST)
    assert bs.horizon_clock(NOW, 5) == datetime(2026, 10, 29, 15, 30, tzinfo=IST)  # Fri, Mon, Tue, Wed, Thu
    assert bs.horizon_clock(NOW, 1, holidays={date(2026, 10, 23)}) == datetime(
        2026, 10, 26, 15, 30, tzinfo=IST
    )


def test_a_future_moves_linearly_and_a_zero_scenario_is_zero():
    legs = [bs.BookLeg("NIFTY", EXP, "future", 0, 75, None, None)]
    m = _mkt()
    assert bs.revalue(legs, m, -0.05, 0.0, bs.horizon_clock(NOW, 1)).pnl == pytest.approx(
        75 * (24000 * 0.95 * _roll(1) - 24000)
    )
    put = bs.BookLeg("NIFTY", EXP, "put", 23500, -75, _b76("put", 24000, 23500, T35, 0.065, 0.15), "mid")
    assert bs.revalue([*legs, put], m, 0.0, 0.0, NOW, response=False).pnl == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize(
    ("move", "dvol", "resp"), [(-0.05, 0.0, 5.2), (0.03, 5.0, -2.28), (-0.10, -3.0, 10.4)]
)
def test_an_option_is_repriced_at_the_shocked_forward_vol_and_time(move, dvol, resp):
    v0 = _b76("put", 24000, 23500, T35, 0.065, 0.15)
    legs = [bs.BookLeg("NIFTY", EXP, "put", 23500, -75, v0, "mid")]
    cell = bs.revalue(legs, _mkt(), move, dvol, bs.horizon_clock(NOW, 1))
    v1 = _b76("put", 24000 * (1 + move) * _roll(1), 23500, 34 / 365, 0.065, 0.15 + (resp + dvol) / 100)
    assert cell.pnl == pytest.approx((v1 - v0) * -75, rel=1e-6)


def test_other_underlyings_move_by_their_beta_and_need_it():
    legs = [bs.BookLeg("BANKNIFTY", EXP, "future", 0, 35, None, None)]
    fw = {("BANKNIFTY", EXP): 52000.0}
    at = bs.horizon_clock(NOW, 1)
    cell = bs.revalue(legs, _mkt(forwards=fw, betas={"BANKNIFTY": 1.2}), -0.05, 0.0, at)
    assert cell.pnl == pytest.approx(35 * (52000 * (1 - 1.2 * 0.05) * _roll(1) - 52000))
    cell = bs.revalue(legs, _mkt(forwards=fw), -0.05, 0.0, at)
    assert cell.pnl is None and any("beta" in r for r in cell.reasons)


def test_a_leg_expiring_inside_the_horizon_is_valued_at_intrinsic():
    near = date(2026, 10, 27)
    t = 5 / 365
    v0 = _b76("call", 24000, 24000, t, 0.065, 0.12)
    legs = [bs.BookLeg("NIFTY", near, "call", 24000, 75, v0, "mid")]
    cell = bs.revalue(legs, _mkt(forwards={("NIFTY", near): 24000.0}), 0.02, 0.0, bs.horizon_clock(NOW, 5))
    assert cell.pnl == pytest.approx((24000 * 1.02 * _roll(5) - 24000 - v0) * 75, rel=1e-6)
    assert cell.intrinsic == 1


def test_an_unpriced_leg_leaves_every_cell_unknown():
    legs = [
        bs.BookLeg("NIFTY", EXP, "future", 0, 75, None, None),
        bs.BookLeg("NIFTY", EXP, "put", 23500, -75, None, None),
    ]
    g = bs.grid(legs, _mkt())
    assert not g.complete and g.reasons
    assert len(g.cells) == 14 * 5 * 3 and all(c.pnl is None for c in g.cells)


def test_grid_axes_and_the_expiry_horizon():
    legs = [bs.BookLeg("NIFTY", EXP, "future", 0, 75, None, None)]
    g = bs.grid(legs, _mkt())
    assert g.complete and {c.horizon for c in g.cells} == {"1d", "5d", "expiry"}
    assert sorted({c.move for c in g.cells}) == sorted(
        x / 100 for x in (-15, -10, -7, -5, -3, -2, -1, 0, 1, 2, 3, 5, 7, 10)
    )
    assert sorted({c.dvol for c in g.cells}) == [-3, 0, 5, 10, 20]
    c = g.cell(-0.10, 0, "expiry")
    assert c.pnl == pytest.approx(75 * (24000 * 0.9 * _roll(35) - 24000)) and c.at == datetime(
        2026, 11, 26, 15, 30, tzinfo=IST
    )


def test_a_vol_driven_below_the_floor_is_floored_and_flagged():
    v0 = _b76("call", 24000, 24500, T35, 0.065, 0.05)
    cell = bs.revalue([bs.BookLeg("NIFTY", EXP, "call", 24500, 75, v0, "mid")], _mkt(), 0.10, -3.0,
                      bs.horizon_clock(NOW, 1))  # fmt: skip
    v1 = _b76("call", 26400 * _roll(1), 24500, 34 / 365, 0.065, bs.MIN_VOL)
    assert cell.floored == 1 and cell.pnl == pytest.approx((v1 - v0) * 75, rel=1e-6)


# --------------------------------------------------------------------------- historical replays and reverse stress
def test_the_single_day_replay_library():
    days = {r.day: r for r in bs.REPLAYS}
    assert set(days) == {date(2022, 2, 24), date(2024, 6, 4), date(2024, 8, 5), date(2025, 4, 7)}
    assert days[date(2024, 6, 4)].move == pytest.approx(math.exp(-0.0611) - 1)  # an index log return
    assert days[date(2022, 2, 24)].move == pytest.approx(-0.0488)  # the front future's move
    assert days[date(2025, 4, 7)].iv30_change == 9.60


def test_a_replay_applies_the_day_s_move_and_iv30_change_without_the_response_rule():
    v0 = _b76("put", 24000, 23500, T35, 0.065, 0.15)
    legs = [
        bs.BookLeg("NIFTY", EXP, "put", 23500, -75, v0, "mid"),
        bs.BookLeg("NIFTY", EXP, "future", 0, 75, None, None),
    ]
    out = {r.replay.day: r for r in bs.replays(legs, _mkt())}
    r = out[date(2024, 6, 4)]
    mv = math.exp(-0.0611) - 1
    v1 = _b76("put", 24000 * (1 + mv) * _roll(1), 23500, 34 / 365, 0.065, 0.15 + 4.46 / 100)
    assert r.cell.pnl == pytest.approx((v1 - v0) * -75 + 75 * (24000 * (1 + mv) * _roll(1) - 24000), rel=1e-6)
    assert any("skew" in n for n in r.notes)


def test_a_replay_marks_beta_mapped_underlyings():
    legs = [bs.BookLeg("BANKNIFTY", EXP, "future", 0, 35, None, None)]
    rs = bs.replays(legs, _mkt(forwards={("BANKNIFTY", EXP): 52000.0}, betas={"BANKNIFTY": 1.2}))
    assert all(any("BANKNIFTY" in n and "beta" in n for n in r.notes) for r in rs)
    assert rs[0].cell.pnl == pytest.approx(35 * (52000 * (1 + 1.2 * rs[0].replay.move) * _roll(1) - 52000))


def test_reverse_stress_finds_the_smallest_breaching_point():
    legs = [bs.BookLeg("NIFTY", EXP, "future", 0, -75, None, None)]  # loses 18,000 per 1 % rise
    g = bs.grid(legs, _mkt())
    out = {r.horizon: r for r in bs.reverse_stress(g, 50_000)}
    assert out["1d"].complete and (out["1d"].cell.move, out["1d"].cell.dvol) == (0.03, 0.0)
    assert out["1d"].cell.pnl == pytest.approx(-75 * (24000 * 1.03 * _roll(1) - 24000))
    none = bs.reverse_stress(g, 10_000_000)
    assert all(r.complete and r.cell is None for r in none)


def test_reverse_stress_on_an_incomplete_grid_is_unknown_not_safe():
    g = bs.grid([bs.BookLeg("NIFTY", EXP, "put", 23500, -75, None, None)], _mkt())
    for r in bs.reverse_stress(g, 50_000):
        assert not r.complete and r.cell is None and r.reasons


def test_the_book_limit_is_the_open_book_budget():
    assert bs.book_limit(500_000) == pytest.approx(40_000)  # 8 % of F&O capital (#358)
    with pytest.raises(ValueError):
        bs.reverse_stress(bs.grid([], _mkt()), 0)


def test_forward_lookup_ignores_case_and_a_price_of_unknown_kind_is_flagged_as_such():
    mark = _b76("call", 24000, 24500, T35, 0.065, 0.13)
    st = bs.price_legs([bs.BookLeg("nifty", EXP, "call", 24500, 75, mark, None)], _mkt())[0]
    assert st.reason is None and st.iv == pytest.approx(0.13, abs=1e-6)
    assert st.flags and "unknown kind" in st.flags[0] and "LTP" not in st.flags[0]
