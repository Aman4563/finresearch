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


def test_a_far_wing_with_a_tiny_vega_still_converges_or_says_why() -> None:
    # 13 standard deviations out of the money: Newton alone crawls (vega ~ 1e-37); the bisection safeguard must end it
    d = df(0.06, 0.25)
    for right, k in (("call", 140.0), ("put", 70.0)):
        p = b.price(right, 100, k, 0.25, d, 0.05)
        got = b.implied_vol(right, p, 100, k, 0.25, d)
        assert got.reason is None or "no convergence" not in got.reason
        assert got.sigma is None or got.sigma == pytest.approx(0.05, abs=1e-6)
        assert got.iterations <= b.MAX_ITER


# --------------------------------------------------------------------------- the per-expiry parity regression
F_TRUE, R_TRUE, T30 = 25012.4, 0.066, 30 / 365


def chain(f=F_TRUE, r=R_TRUE, t=T30, lo=23000, hi=27000, step=100, sigma=0.14, round_to=None, bump=None):
    """Calls and puts priced by Black-76 at one volatility; `bump` = {strike: rupees added to the call}."""
    d, out = df(r, t), []
    for k in range(lo, hi + 1, step):
        c, p = b.price("call", f, k, t, d, sigma), b.price("put", f, k, t, d, sigma)
        if round_to:
            c, p = round(c / round_to) * round_to, round(p / round_to) * round_to
        c += (bump or {}).get(k, 0.0)
        out.append(b.ParityQuote(float(k), c, p))
    return out


def test_parity_regression_recovers_forward_and_discount_factor_exactly() -> None:
    fit = b.parity_fit(chain(), T30)
    assert fit.reason is None and fit.forward == pytest.approx(F_TRUE, abs=1e-6)
    assert fit.df == pytest.approx(df(R_TRUE, T30), abs=1e-10) and fit.r_impl == pytest.approx(R_TRUE, abs=1e-8)
    # only strikes with |ln K/F| <= 5 % enter: 23,800 to 26,200
    assert min(fit.strikes) == 23800 and max(fit.strikes) == 26200 and fit.n == 25 and fit.weights == "equal"


def test_tick_rounded_prices_give_the_forward_within_a_point() -> None:
    fit = b.parity_fit(chain(round_to=0.05), T30)
    assert fit.forward == pytest.approx(F_TRUE, abs=1.0) and fit.se_forward < 1.0


def test_one_mispriced_strike_barely_moves_the_huber_forward() -> None:
    # a 40-rupee model settle at one strike next to the money: the single-strike parity forward there would move by
    # 40 / DF ~ 40 points; the Huber fit stays within half a point
    fit = b.parity_fit(chain(bump={25000: 40.0}), T30)
    assert fit.forward == pytest.approx(F_TRUE, abs=0.5)


def test_too_few_strikes_is_unknown_with_a_reason() -> None:
    fit = b.parity_fit(chain(lo=24900, hi=25100, step=100)[:2], T30)
    assert fit.forward is None and fit.df is None and "strikes" in fit.reason


def test_a_narrow_noisy_band_gives_the_forward_but_no_implied_rate() -> None:
    # three strikes 100 apart with tick rounding and a 7-day expiry: the slope (DF) is not identified, the intercept
    # (F) still is; r_impl must be unknown, never a number off by several points
    t = 7 / 365
    qs = chain(t=t, lo=24900, hi=25100, step=100, round_to=0.05, bump={25100: 0.35})
    fit = b.parity_fit(qs, t)
    assert fit.forward == pytest.approx(F_TRUE, abs=2.0)
    assert fit.r_impl is None and fit.df is None and fit.df_reason


def test_spread_weights_downweight_a_wide_quote() -> None:
    qs = chain()
    qs = [b.ParityQuote(q.strike, q.call + (6.0 if q.strike == 25100 else 0.0), q.put,
                        20.0 if q.strike == 25100 else 0.5, 0.5) for q in qs]  # fmt: skip
    fit = b.parity_fit(qs, T30)
    assert fit.weights == "spread" and fit.forward == pytest.approx(F_TRUE, abs=0.05)


def test_basis_between_parity_and_future() -> None:
    assert b.basis(25010.0, 25000.0) == pytest.approx(0.0004)
    assert b.basis(None, 25000.0) is None and b.basis(25010.0, None) is None


# --------------------------------------------------------------------------- IVs from quotes, with error bands
D30 = df(R_TRUE, T30)


def quote(right, k, sigma=0.14, half=0.5, **kw):
    m = b.price(right, F_TRUE, k, T30, D30, sigma)
    return b.OptionQuote(float(k), right, **({"bid": m - half, "ask": m + half} | kw))


def test_mid_iv_and_its_bands_computed_by_hand() -> None:
    got = b.quote_iv(quote("put", 24500, half=0.75), F_TRUE, T30, D30)
    v = b.vega(F_TRUE, 24500, T30, D30, 0.14)
    assert got.reason is None and got.sigma == pytest.approx(0.14, abs=1e-9)
    assert got.band == pytest.approx(1.5 / (2 * v), rel=1e-6) and got.tick_band == pytest.approx(0.05 / v, rel=1e-6)


@pytest.mark.parametrize(
    "kw,why",
    [({"bid": None, "ask": 10.0}, "no bid"), ({"bid": 0.0, "ask": 10.0}, "no bid"), ({"bid": 9.0, "ask": None}, "no ask"),
     ({"bid": 10.0, "ask": 9.95}, "crossed"), ({"bid": 10.0, "ask": 10.0}, "locked")],
)  # fmt: skip
def test_unusable_quotes_are_dropped_with_a_reason(kw, why) -> None:
    got = b.quote_iv(b.OptionQuote(24000.0, "put", **kw), F_TRUE, T30, D30)
    assert got.sigma is None and why in got.reason


def test_a_band_over_two_vol_points_is_unknown() -> None:
    v = b.vega(F_TRUE, 26500, T30, D30, 0.14)
    got = b.quote_iv(quote("call", 26500, half=0.021 * v), F_TRUE, T30, D30)  # band = 2.1 vol points
    assert got.sigma is None and "2 vol points" in got.reason and got.band == pytest.approx(0.021, rel=1e-4)


def test_a_settle_without_bid_ask_has_an_unknown_band_not_zero() -> None:
    m = b.price("call", F_TRUE, 25500, T30, D30, 0.13)
    got = b.quote_iv(b.OptionQuote(25500.0, "call", price=m), F_TRUE, T30, D30)
    assert got.sigma == pytest.approx(0.13, abs=1e-9) and got.band is None and got.tick_band is not None


def test_no_discount_factor_means_no_iv() -> None:
    got = b.quote_iv(quote("put", 24500), F_TRUE, T30, None)
    assert got.sigma is None and "discount factor" in got.reason


def test_otm_side_per_strike_and_both_sides_at_the_money() -> None:
    qs = [quote(r, k, sigma=0.14 + (0.01 if r == "put" else 0.0)) for k in range(24500, 25600, 100)
          for r in ("call", "put")]  # fmt: skip
    rows = b.otm_ivs(qs, F_TRUE, T30, D30)
    sides = {(q.strike, q.right) for q in rows}
    assert (24500, "put") in sides and (24500, "call") not in sides
    assert (25500, "call") in sides and (25500, "put") not in sides
    assert {(25000, "call"), (25000, "put")} <= sides  # 25,000 is the strike nearest F = 25,012.4
    atm = b.atm_iv(qs, F_TRUE, T30, D30)
    assert atm.strike == 25000 and atm.sigma == pytest.approx(0.145, abs=1e-9) and atm.sides == 2


def test_atm_iv_uses_one_side_when_the_other_is_unusable() -> None:
    qs = [quote("call", 25000), b.OptionQuote(25000.0, "put", bid=5.0, ask=4.0), quote("call", 25100)]
    atm = b.atm_iv(qs, F_TRUE, T30, D30)
    assert atm.sigma == pytest.approx(0.14, abs=1e-9) and atm.sides == 1
    assert b.atm_iv([], F_TRUE, T30, D30).sigma is None


def test_expected_move_band_is_lognormal() -> None:
    lo, hi = b.expected_move(25000.0, 0.2, 0.25)
    assert lo == pytest.approx(25000 * math.exp(-0.1)) and hi == pytest.approx(25000 * math.exp(0.1))
    assert 25000 - lo < hi - 25000  # skewed up, unlike S +/- S sigma sqrt T
