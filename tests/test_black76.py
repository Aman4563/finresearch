"""The app's own Black-76 engine (#385, P3.2): prices on a forward with an explicit discount factor, a bracketed
Newton implied-volatility solver with stated tolerances and no-arbitrage bounds, and IVs from quotes.

Reference values were computed independently of this code: closed form with scipy.stats.norm and with py_vollib
1.0.x (Jaeckel's "Let's be rational" implementation of Black-76), both agreeing to 1e-11; the textbook cases are
Hull's and Haug's worked examples (quoted to the books' printed precision)."""

import math

import pytest

from finresearch.fincalc import black76 as b
from finresearch.fincalc import options as o


def df(r: float, t: float) -> float:
    return math.exp(-r * t)


# --------------------------------------------------------------------------- prices against textbook values
def test_hull_futures_put_example() -> None:
    # Hull, Options, Futures and Other Derivatives (Black's model for futures options, worked example): F = K = 20,
    # 4 months, r = 9 %, sigma = 25 %: put = 1.12. Closed form to 1e-12: 1.1166414565589438
    t = 4 / 12
    got = b.price("put", 20, 20, t, df(0.09, t), 0.25)
    assert round(got, 2) == 1.12 and got == pytest.approx(1.1166414565589438, abs=1e-10)


def test_haug_black76_example() -> None:
    # Haug, The Complete Guide to Option Pricing Formulas (2nd ed.), Black-76: F = X = 19, T = 0.75, r = 10 %,
    # v = 28 %: c = p = 1.7011
    t = 0.75
    c = b.price("call", 19, 19, t, df(0.10, t), 0.28)
    p = b.price("put", 19, 19, t, df(0.10, t), 0.28)
    assert round(c, 4) == round(p, 4) == 1.7011 and c == pytest.approx(1.701050725236268, abs=1e-10)


@pytest.mark.parametrize(
    "right,f,k,t,r,s,want",
    [
        ("call", 25000, 25500, 10 / 365, 0.065, 0.13, 52.46884501584977),
        ("put", 25000, 24000, 30 / 365, 0.065, 0.16, 114.30909998081971),
        ("call", 100, 150, 0.5, 0.03, 0.40, 1.153468223979898),
    ],
)
def test_prices_match_independent_closed_form(right, f, k, t, r, s, want) -> None:
    assert b.price(right, f, k, t, df(r, t), s) == pytest.approx(want, rel=1e-11)


def test_put_call_parity_holds_exactly() -> None:
    d = df(0.07, 0.2)
    for k in (80.0, 100.0, 125.0):
        c, p = b.price("call", 100, k, 0.2, d, 0.3), b.price("put", 100, k, 0.2, d, 0.3)
        assert c - p == pytest.approx(d * (100 - k), abs=1e-12)


def test_zero_volatility_is_the_discounted_intrinsic() -> None:
    assert b.price("call", 110, 100, 0.5, 0.97, 0.0) == pytest.approx(0.97 * 10)
    assert b.price("put", 110, 100, 0.5, 0.97, 0.0) == 0.0


def test_black76_equals_black_scholes_with_the_implied_dividend_yield() -> None:
    # S, r, q -> F = S e^((r - q) T), DF = e^(-rT): Black-76 on (F, DF) is Black-Scholes on (S, r, q), price and delta
    s, r, q, t, k, v = 24800.0, 0.066, 0.013, 45 / 365, 25000.0, 0.15
    f, d = s * math.exp((r - q) * t), df(r, t)
    for right in ("call", "put"):
        bs = o.greeks(right, s, k, t, r, v, div_yield=q)
        assert b.price(right, f, k, t, d, v) == pytest.approx(bs.price, rel=1e-12)
        assert b.spot_delta(right, f, k, t, d, v, s) == pytest.approx(bs.delta, rel=1e-12)
    assert b.implied_q(r, f, s, t) == pytest.approx(q, abs=1e-12)


def test_bounds_are_discounted_intrinsic_and_discounted_forward_or_strike() -> None:
    assert b.bounds("call", 110, 100, 0.9) == pytest.approx((9.0, 99.0))
    assert b.bounds("put", 110, 100, 0.9) == pytest.approx((0.0, 90.0))


# --------------------------------------------------------------------------- the implied-volatility solver
@pytest.mark.parametrize(
    "right,p,f,k,t,r,want",
    [  # goldens: py_vollib 1.0.x black.implied_volatility (Jaeckel, "Let's be rational", Wilmott 2015)
        ("call", 50.0, 25000, 25500, 10 / 365, 0.065, 0.1277171278363589),
        ("put", 120.0, 25000, 24000, 30 / 365, 0.065, 0.16301310066641475),
        ("call", 1.0, 100, 150, 0.5, 0.03, 0.38682831239853765),
        ("put", 0.10, 25000, 23000, 7 / 365, 0.065, 0.1860682136817625),
        ("put", 1.1166414565589438, 20, 20, 4 / 12, 0.09, 0.25),
    ],
)
def test_implied_vol_matches_lets_be_rational_goldens(right, p, f, k, t, r, want) -> None:
    got = b.implied_vol(right, p, f, k, t, df(r, t))
    assert got.reason is None and got.sigma == pytest.approx(want, abs=b.SIGMA_TOL * 10)
    assert got.price_error <= b.PRICE_TOL * max(1.0, p)


@pytest.mark.parametrize("k", [70.0, 90.0, 100.0, 110.0, 140.0])
@pytest.mark.parametrize("right", ["call", "put"])
def test_round_trip_in_and_out_of_the_money(right, k) -> None:
    d = df(0.06, 0.25)
    for s in (0.05, 0.2, 0.8, 2.5):
        p = b.price(right, 100, k, 0.25, d, s)
        got = b.implied_vol(right, p, 100, k, 0.25, d)
        if got.sigma is None:  # only where the time value is below what a double can resolve (deep ITM, low vol)
            assert got.reason and p - b.bounds(right, 100, k, d)[0] < 1e-9
        else:
            assert got.sigma == pytest.approx(s, abs=1e-8), (right, k, s)


def test_outside_no_arbitrage_bounds_is_unknown_with_a_reason() -> None:
    d = 0.99
    below = b.implied_vol("call", 9.0, 110, 100, 0.1, d)  # under the discounted intrinsic 9.9
    above = b.implied_vol("call", 109.0, 110, 100, 0.1, d)  # above the discounted forward 108.9
    at = b.implied_vol("put", 99.0, 110, 100, 0.1, d)  # at the discounted strike
    zero = b.implied_vol("put", 0.0, 110, 100, 0.1, d)
    for got in (below, above, at, zero):
        assert got.sigma is None and got.reason
    assert "lower bound" in below.reason and "upper bound" in above.reason and "upper bound" in at.reason


def test_bad_inputs_are_unknown_not_errors() -> None:
    for args in [(10.0, 0, 100, 0.1, 0.99), (10.0, 100, 100, 0, 0.99), (10.0, 100, 100, 0.1, None),
                 (math.nan, 100, 100, 0.1, 0.99), (10.0, 100, 100, 0.1, 1.5)]:  # fmt: skip
        got = b.implied_vol("call", *args)
        assert got.sigma is None and got.reason


def test_a_price_beyond_the_volatility_bracket_is_unknown() -> None:
    d = df(0.06, 0.25)
    p = b.price("call", 100, 100, 0.25, d, b.SIGMA_HI * 1.5)  # inside the bounds, beyond the bracket's top
    got = b.implied_vol("call", p, 100, 100, 0.25, d)
    assert got.sigma is None and "bracket" in got.reason
