"""fincalc/black76.py (price, vega, deltas, implied volatility) and smile.vanna / volga against QuantLib, with
40-digit arithmetic (mpmath) as the referee.

Conventions pinned first:
* the module takes the forward F and the discount factor DF; QuantLib's `blackFormula(type, K, F, stdDev, DF)` and
  `BlackCalculator(payoff, F, stdDev, DF)` take the same pair with stdDev = sigma sqrt(T);
* vega is per 1.00 of volatility on both sides (`BlackCalculator.vega(T)`; not per vol point, as vollib's is);
* `forward_delta` carries the discount factor, as `BlackCalculator.deltaForward()` does; `spot_delta` is
  `BlackCalculator.delta(spot)`.

Why a referee: QuantLib's cumulative normal switches to an asymptotic series once N(z) <= 1e-8 and is then good to
about 1e-7 relative, and it forms vega and a put's delta by subtraction. On the deep wings and minute-long expiries
this grid is made of, QuantLib is the LESS accurate side, so "differs from QuantLib" settles nothing there. mpmath
evaluates the same closed forms to 40 digits from the exact binary inputs, and each in-house value must sit within
the first-order rounding error of its own formula (4 ulp per elementary step) of that exact value.

Grid: forward 24,000, 16 strikes from 30 % to 300 % of it, 9 expiries from one minute to three years, 8 volatilities
from 1 % to 300 %, 4 discount factors from 0.70 to 1.02 (a negative rate), calls and puts: 9,216 points.
"""

from __future__ import annotations

import itertools
import math
import sys
from dataclasses import dataclass

import mpmath as mp
import QuantLib as ql

from finresearch.fincalc import black76 as b
from finresearch.fincalc import smile

mp.mp.dps = 40
EPS = sys.float_info.epsilon
MINUTE = 1 / (365 * 24 * 60)
F = 24000.0
MONEYNESS = [0.3, 0.5, 0.7, 0.85, 0.95, 0.99, 0.999, 1.0, 1.001, 1.01, 1.05, 1.15, 1.3, 1.5, 2.0, 3.0]
EXPIRIES = [MINUTE, 15 * MINUTE, 60 * MINUTE, 1 / 365, 7 / 365, 30 / 365, 0.25, 1.0, 3.0]
VOLS = [0.01, 0.05, 0.12, 0.2, 0.45, 0.9, 2.0, 3.0]
DFS = [0.7, 0.95, 1.0, 1.02]
TINY = mp.mpf(10) ** -290  # below this a double is denormal or zero: nothing to compare
CENTRAL = 5.5  # |d1|, |d2| <= 5.5 keeps N(d) above 1e-8, the branch where QuantLib's normal CDF is erf-based
QL_CDF_ABS, QL_CDF_TAIL = EPS / 2, 2e-7  # QuantLib's normal CDF: absolute in the centre, relative in the tail


def _N(x):
    return mp.erfc(-x / mp.sqrt(2)) / 2


def _n(x):
    return mp.exp(-x * x / 2) / mp.sqrt(2 * mp.pi)


@dataclass(frozen=True)
class Exact:
    """The closed forms in 40 digits, and for each the first-order float rounding of the in-house formula: every
    normal term to 4 ulp, and d1/d2 good to eps x (|d| + 1/stdDev) (the log of F/K is rounded absolutely)."""

    price: mp.mpf
    price_ulp: mp.mpf
    vega: mp.mpf
    vega_ulp: mp.mpf
    delta: mp.mpf
    delta_ulp: mp.mpf
    wing: mp.mpf  # DF (F N(-|d1|) + K N(-|d2|)): the two terms of the out-of-the-money twin
    d: float  # max(|d1|, |d2|)
    sd: float


def exact(right: str, k: float, t: float, df: float, s: float) -> Exact:
    f_, k_, t_, df_, s_ = (mp.mpf(x) for x in (F, k, t, df, s))
    sd = s_ * mp.sqrt(t_)
    d1 = (mp.log(f_ / k_) + sd * sd / 2) / sd
    d2 = d1 - sd
    sg = 1 if right == "call" else -1
    n1, n2 = _N(sg * d1), _N(sg * d2)
    wobble = abs(d1) + 1 / sd
    vega = df_ * f_ * _n(d1) * mp.sqrt(t_)
    return Exact(
        price=sg * df_ * (f_ * n1 - k_ * n2),
        price_ulp=4 * EPS * df_ * (f_ * (n1 + abs(d1) * _n(d1)) + k_ * (n2 + abs(d2) * _n(d2))),
        vega=vega,
        vega_ulp=4 * EPS * vega * (1 + abs(d1) * wobble),
        delta=sg * df_ * n1,
        delta_ulp=4 * EPS * df_ * (n1 + _n(d1) * wobble),
        wing=df_ * (f_ * _N(-abs(d1)) + k_ * _N(-abs(d2))),
        d=float(max(abs(d1), abs(d2))),
        sd=float(sd),
    )


def grid():
    for m, t, s, df, right in itertools.product(MONEYNESS, EXPIRIES, VOLS, DFS, ("call", "put")):
        yield right, F * m, t, df, s


def qtype(right: str):
    return ql.Option.Call if right == "call" else ql.Option.Put


class Worst:
    """The largest value seen and where."""

    def __init__(self):
        self.value, self.at, self.n = 0.0, None, 0

    def see(self, value, at=None):
        self.n += 1
        if float(value) > self.value:
            self.value, self.at = float(value), at


def test_quantlibs_normal_cdf_is_asymptotic_in_the_far_tail(oracle):
    """(c) oracle limitation, measured so the tolerances below are not guesses: QuantLib's cumulative normal against
    40 digits. Where N(z) > 1e-8 it is erf-based and good to half an ulp of 1 ABSOLUTELY (so 2e-9 relative by
    N = 1e-8); below that it switches to an asymptotic series whose relative error peaks near z = -5.62 at about
    1.2e-7 and dies away by z = -10. The in-house erfc form stays within 1e-12 relative down to z = -37."""
    cnd = ql.CumulativeNormalDistribution()
    central, tail, ours = Worst(), Worst(), Worst()
    for i in range(4501):
        z = -37 + i * 0.01
        ref = _N(mp.mpf(z))
        if ref > mp.mpf("1e-8"):
            central.see(abs(cnd(z) - ref), z)
        else:
            tail.see(abs(cnd(z) / ref - 1), z)
        ours.see(abs(b._cdf(z) / ref - 1), z)
    assert central.value < QL_CDF_ABS and 5e-8 < tail.value < QL_CDF_TAIL and -5.7 < tail.at < -5.5
    oracle.check("black76._cdf (z from -37 to 8)", "mpmath erfc, 40 digits", grid=ours.n, max_diff=ours.value,
                 limit=1e-12, why="relative (erfc keeps the lower tail)", worst=ours.at)  # fmt: skip
    oracle.note("QuantLib CumulativeNormalDistribution", "mpmath erfc, 40 digits", grid=tail.n, max_diff=tail.value,
                tolerance="none: QuantLib's own relative error where N(z) <= 1e-8 (z near -5.62)",
                verdict="(c) oracle limit: QuantLib's normal CDF is an asymptotic series in the far tail")  # fmt: skip


def test_price_is_exact_to_float_rounding(oracle):
    """The price against 40 digits, within the rounding of its own formula. The formula subtracts two terms that are
    nearly equal on a short-dated wing (their ratio is 1 - stdDev/d1), so a far-wing price is correct to rounding
    and still carries a large RELATIVE error: both numbers are reported."""
    ulps, rel = Worst(), Worst()
    for right, k, t, df, s in grid():
        x = exact(right, k, t, df, s)
        if x.price < TINY:
            continue
        got = b.price(right, F, k, t, df, s)
        ulps.see(abs(got - x.price) / x.price_ulp, (right, k, t, df, s))
        if x.price >= mp.mpf("1e-12") * df * F:  # a price of at least 1e-12 of the discounted forward
            rel.see(abs(got / x.price - 1), (right, k, t, df, s))
    oracle.check("black76.price", "mpmath, 40 digits", grid=ulps.n, max_diff=ulps.value, limit=1.0,
                 why="x the formula's first-order float rounding", worst=ulps.at)  # fmt: skip
    oracle.check("black76.price (prices >= 1e-12 DF F)", "mpmath, 40 digits", grid=rel.n, max_diff=rel.value,
                 limit=1e-10, why="relative (rounding x the cancellation d1/stdDev, <= 1e5 here)", worst=rel.at)  # fmt: skip


def test_price_matches_quantlib_within_quantlibs_own_accuracy(oracle):
    """Against `blackFormula`. In the centre both sides are plain double arithmetic on terms of size DF (F + K), so
    they agree to rounding (4 ulp of that, plus the in-house bound). On the wings QuantLib's tail CDF is good to
    2e-7 relative (measured above) on the two out-of-the-money terms, so that is the tolerance there. The largest
    RELATIVE difference is far bigger, and it is QuantLib's distance from the exact value, not ours."""
    centre, tail, rel = Worst(), Worst(), Worst()
    for right, k, t, df, s in grid():
        x = exact(right, k, t, df, s)
        ref = ql.blackFormula(qtype(right), k, F, x.sd, df)
        diff = abs(b.price(right, F, k, t, df, s) - ref)
        rounding = x.price_ulp + 4 * EPS * df * (F + k)
        if x.d <= CENTRAL:
            centre.see(diff / rounding, (right, k, t, df, s))
        else:
            tail.see(diff / (rounding + QL_CDF_TAIL * x.wing), (right, k, t, df, s))
        if x.price >= TINY:
            rel.see(diff / x.price, (right, k, t, df, s, float(abs(ref - x.price) / x.price)))
    oracle.check("black76.price (|d| <= 5.5)", "QuantLib blackFormula", grid=centre.n, max_diff=centre.value,
                 limit=1.0, why="x float rounding of both sides (4 ulp of DF (F + K))", worst=centre.at)  # fmt: skip
    oracle.check("black76.price (|d| > 5.5)", "QuantLib blackFormula", grid=tail.n, max_diff=tail.value, limit=1.0,
                 why="x (rounding + 2e-7 of the two wing terms): QuantLib's tail CDF", worst=tail.at)  # fmt: skip
    assert abs(rel.at[-1] / rel.value - 1) < 1e-3  # at the worst point the difference is QuantLib's own error
    oracle.note("black76.price (whole grid)", "QuantLib blackFormula", grid=rel.n, max_diff=rel.value,
                tolerance="none: largest relative difference; 40-digit arithmetic sides with the in-house price",
                verdict="(c) oracle limit: QuantLib's tail CDF (in-house is within rounding of exact)")  # fmt: skip


def test_vega_and_deltas(oracle):
    """Vega and the forward delta against 40 digits, within their own rounding; then against BlackCalculator, which
    holds ABSOLUTE accuracy only (it forms vega as F n(d1) d1' - K n(d2) d2' and a put's delta as N(d1) - 1): 1e-9
    of the greek's own scale (DF F sqrt T for vega, DF for a delta) is float rounding times that cancellation."""
    v_ulp, d_ulp, v_ql, d_ql, s_ql = Worst(), Worst(), Worst(), Worst(), Worst()
    spot = 23500.0
    for right, k, t, df, s in grid():
        x = exact(right, k, t, df, s)
        at = (right, k, t, df, s)
        vega, delta = b.vega(F, k, t, df, s), b.forward_delta(right, F, k, t, df, s)
        if x.vega >= TINY:
            v_ulp.see(abs(vega - x.vega) / x.vega_ulp, at)
        if abs(x.delta) >= TINY:
            d_ulp.see(abs(delta - x.delta) / x.delta_ulp, at)
        bc = ql.BlackCalculator(ql.PlainVanillaPayoff(qtype(right), k), F, x.sd, df)
        v_ql.see(abs(vega - bc.vega(t)) / (df * F * math.sqrt(t)), at)
        d_ql.see(abs(delta - bc.deltaForward()) / df, at)
        s_ql.see(abs(b.spot_delta(right, F, k, t, df, s, spot) - bc.delta(spot)) / (df * F / spot), at)
    for name, w in (("black76.vega", v_ulp), ("black76.forward_delta", d_ulp)):
        oracle.check(name, "mpmath, 40 digits", grid=w.n, max_diff=w.value, limit=1.0,
                     why="x the formula's first-order float rounding", worst=w.at)  # fmt: skip
    for name, call, w in (("black76.vega", "vega(T)", v_ql), ("black76.forward_delta", "deltaForward()", d_ql),
                          ("black76.spot_delta", "delta(spot)", s_ql)):  # fmt: skip
        oracle.check(name, f"QuantLib BlackCalculator.{call}", grid=w.n, max_diff=w.value, limit=1e-9,
                     why="of the greek's scale (QuantLib subtracts: absolute accuracy only)", worst=w.at)  # fmt: skip


def _otm(right: str, k: float) -> bool:
    return k == F or (right == "call") == (k > F)


def test_implied_vol_recovers_the_volatility_behind_an_exact_price(oracle):
    """The target is the 40-digit price rounded to the nearest double, so the true volatility is known and the
    in-house price function is not part of the oracle. Allowed error: what the solver states (PRICE_TOL relative to
    the out-of-the-money twin's price, or SIGMA_TOL in sigma) plus what a double can carry (half an ulp of the
    target, the rounding of the solver's own pricing and, for an in-the-money quote, the parity subtraction), all
    divided by the vega. An in-the-money quote whose time value is below the spacing of doubles at its price has no
    recoverable volatility: the module must say unknown (lower bound), never a number."""
    otm, itm, otm_abs, itm_abs, lost, tiny = Worst(), Worst(), Worst(), Worst(), 0, 0
    for right, k, t, df, s in grid():
        x = exact(right, k, t, df, s)
        twin = x if _otm(right, k) else exact("put" if right == "call" else "call", k, t, df, s)
        if twin.price < TINY:
            tiny += 1
            continue
        target = float(x.price)
        got = b.implied_vol(right, target, F, k, t, df)
        if got.sigma is None:
            assert not _otm(right, k) and "lower bound" in got.reason, (right, k, t, df, s, got.reason)
            assert twin.price <= 2 * math.ulp(target), (right, k, t, df, s, float(twin.price))
            lost += 1
            continue
        carry = math.ulp(target) / 2 + twin.price_ulp
        if not _otm(right, k):
            carry += math.ulp(target) / 2 + 4 * EPS * df * (F + k)
        allowed = (b.PRICE_TOL * twin.price + carry) / twin.vega + b.SIGMA_TOL
        err = abs(got.sigma - s)
        (otm if _otm(right, k) else itm).see(err / allowed, (right, k, t, df, s, got.sigma, got.iterations))
        (otm_abs if _otm(right, k) else itm_abs).see(err, (right, k, t, df, s, got.sigma))
        assert got.iterations <= b.MAX_ITER
    assert otm.n > 3500 and itm.n > 2000 and lost > 300 and tiny > 1000, (otm.n, itm.n, lost, tiny)
    why = "x (the solver's stated tolerance + what a double carries) / vega"
    oracle.check("black76.implied_vol (out of the money)", "mpmath price, known sigma", grid=otm.n,
                 max_diff=otm.value, limit=1.0, why=why, worst=otm.at)  # fmt: skip
    oracle.check("black76.implied_vol (out of the money)", "mpmath price, known sigma", grid=otm_abs.n,
                 max_diff=otm_abs.value, limit=1e-8, why="absolute, in volatility (1e-6 vol points)", worst=otm_abs.at)  # fmt: skip
    oracle.check("black76.implied_vol (in the money, via parity)", "mpmath price, known sigma", grid=itm.n,
                 max_diff=itm.value, limit=1.0, why=why, worst=itm.at)  # fmt: skip
    oracle.note("black76.implied_vol (in the money)", "mpmath price, known sigma", grid=itm_abs.n,
                max_diff=itm_abs.value, tolerance="none: largest absolute error in volatility, where the time value "
                "is a few ulp of the price (the quote cannot carry more)",
                verdict="(c) float limit of the input, not of the solver; the bound above holds")  # fmt: skip
    oracle.note("black76.implied_vol (in the money)", "mpmath price, known sigma", grid=lost, max_diff=lost,
                tolerance="none: count of quotes whose time value is below one ulp of the price",
                verdict="(b) convention: unknown (at the no-arbitrage lower bound), never a number")  # fmt: skip


def test_implied_vol_against_quantlibs_solver(oracle):
    """`blackFormulaImpliedStdDev` on the same exact out-of-the-money prices. In the centre both solvers must land
    within their tolerances of each other (QuantLib's 1e-12 on stdDev, its price rounding over the vega, and the
    in-house bound above). On the far wings QuantLib inverts its own tail CDF (2e-7), or gives up and returns 0."""
    centre, tail, ql_err, gave_up = Worst(), Worst(), Worst(), 0
    for right, k, t, df, s in grid():
        x = exact(right, k, t, df, s)
        if not _otm(right, k) or x.price < TINY:
            continue
        target = float(x.price)
        got = b.implied_vol(right, target, F, k, t, df).sigma
        ref = ql.blackFormulaImpliedStdDev(
            qtype(right), k, F, target, df, 0.0, 1.3 * x.sd, 1e-12, 1000
        ) / math.sqrt(t)
        if ref == 0:
            gave_up += 1
            assert x.d > CENTRAL
            continue
        rounding = math.ulp(target) + 2 * x.price_ulp + 4 * EPS * df * (F + k)
        allowed = (b.PRICE_TOL * x.price + rounding) / x.vega + b.SIGMA_TOL + 1e-12 / math.sqrt(t)
        if x.d <= CENTRAL:
            centre.see(abs(got - ref) / allowed, (right, k, t, df, s, got, ref))
        else:
            tail.see(abs(got / ref - 1), (right, k, t, df, s, got, ref))
            ql_err.see(abs(ref / s - 1), (right, k, t, df, s))
    oracle.check("black76.implied_vol (|d| <= 5.5)", "QuantLib blackFormulaImpliedStdDev", grid=centre.n,
                 max_diff=centre.value, limit=1.0, why="x (both solvers' tolerances + rounding / vega)", worst=centre.at)  # fmt: skip
    assert (
        tail.value < 2 * ql_err.value + 1e-8
    )  # the wing difference is QuantLib's own distance from the truth
    oracle.note("black76.implied_vol (|d| > 5.5)", "QuantLib blackFormulaImpliedStdDev", grid=tail.n,
                max_diff=tail.value, tolerance=f"none: largest relative difference; QuantLib is {ql_err.value:.1e} "
                f"from the true sigma there, and returned 0 for {gave_up} more prices",
                verdict="(c) oracle limit: QuantLib inverts its tail CDF; in-house is within its bound of the truth")  # fmt: skip


def test_vanna_and_volga(oracle):
    """smile.vanna (d delta / d sigma) and smile.volga (d vega / d sigma) on the central points, against mpmath's
    numerical derivatives of the 40-digit price (an independent route: no closed form shared) and BlackCalculator.

    (c) QuantLib 1.44's `volga(T)` is NOT d2V/dsigma2: it divides by stdDev where the formula divides by sigma, so it
    returns the true volga / sqrt(T) and is right only at T = 1. mpmath's second derivative sides with the in-house
    value; multiplied by sqrt(T), QuantLib's agrees too. Its vanna (with spot = forward) is fine."""

    def mp_price(fwd, k, t, df, sig):
        sd = sig * mp.sqrt(t)
        d1 = (mp.log(fwd / k) + sd * sd / 2) / sd
        return df * (fwd * _N(d1) - k * _N(d1 - sd))

    def mp_derivatives(k, t, df, s):
        args = [mp.mpf(v) for v in (k, t, df)]
        volga = mp.diff(lambda sig: mp_price(mp.mpf(F), *args, sig), mp.mpf(s), 2)
        vanna = mp.diff(lambda fwd, sig: mp_price(fwd, *args, sig), (mp.mpf(F), mp.mpf(s)), (1, 1))
        return vanna, volga

    van, vol, van_ql, vol_ql, vol_raw = Worst(), Worst(), Worst(), Worst(), Worst()
    for i, (right, k, t, df, s) in enumerate(grid()):
        x = exact(right, k, t, df, s)
        if right == "put" or x.d > CENTRAL or k == F:
            continue
        at = (k, t, df, s)
        vanna, volga = smile.vanna(F, k, t, df, s), smile.volga(F, k, t, df, s)
        v_scale, g_scale = (
            df / s,
            float(x.vega) / s * max(1.0, x.d * x.d),
        )  # the sizes of DF n d2/sigma, vega d1 d2/sigma
        bc = ql.BlackCalculator(ql.PlainVanillaPayoff(ql.Option.Call, k), F, x.sd, df)
        van_ql.see(abs(vanna - bc.vanna(F, t)) / v_scale, at)
        vol_ql.see(abs(volga - bc.volga(t) * math.sqrt(t)) / g_scale, at)
        if abs(volga) > 1e-6 * g_scale:
            vol_raw.see(abs(bc.volga(t) / volga - 1), at)
        if i % 5 == 0:  # the 40-digit derivatives are slow: every fifth point
            ref_v, ref_g = mp_derivatives(k, t, df, s)
            van.see(abs(vanna - ref_v) / v_scale, at)
            vol.see(abs(volga - ref_g) / g_scale, at)
    why = "of the greek's scale (float rounding of d1, d2)"
    oracle.check("smile.vanna", "mpmath d2V/dF dsigma, 40 digits", grid=van.n, max_diff=van.value, limit=1e-9,
                 why=why, worst=van.at)  # fmt: skip
    oracle.check("smile.volga", "mpmath d2V/dsigma2, 40 digits", grid=vol.n, max_diff=vol.value, limit=1e-9,
                 why=why, worst=vol.at)  # fmt: skip
    oracle.check("smile.vanna", "QuantLib BlackCalculator.vanna(F, T)", grid=van_ql.n, max_diff=van_ql.value,
                 limit=1e-9, why=why, worst=van_ql.at)  # fmt: skip
    oracle.check("smile.volga", "QuantLib BlackCalculator.volga(T) x sqrt(T)", grid=vol_ql.n, max_diff=vol_ql.value,
                 limit=1e-9, why=why, worst=vol_ql.at)  # fmt: skip
    assert vol_raw.value > 700  # one minute to expiry: 1 / sqrt(T) = 725
    oracle.note("smile.volga", "QuantLib BlackCalculator.volga(T) as returned", grid=vol_raw.n, max_diff=vol_raw.value,
                tolerance="none: largest relative difference (a factor 1/sqrt(T) at one minute to expiry)",
                verdict="(c) oracle defect: QuantLib 1.44 volga(T) = true volga / sqrt(T); mpmath sides with in-house")  # fmt: skip
