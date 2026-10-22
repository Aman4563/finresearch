"""The app's own Black-76 engine (#385, P3.2): one pricing convention for European options on a forward, parameterised
by the forward F and the discount factor DF to the option's payment date, never by an assumed rate.

    C = DF [F N(d1) - K N(d2)],  P = DF [K N(-d2) - F N(-d1)],  d1 = [ln(F/K) + sigma^2 T/2] / (sigma sqrt T)

T is whatever clock the caller's sigma is quoted on (calendar years, or business years from fincalc.bustime): only
the total variance sigma^2 T enters a price. With DF = e^(-rT) and F = S e^((r - q)T) it is Black-Scholes with the
dividend yield q, so the spot delta is e^(-qT) N(d1) = (F DF / S) N(d1) with no q assumed.

Pure functions; nothing here reads a rate. A caller without a dated rate or an identified parity discount factor has
no DF, and every function that needs one then answers unknown with the reason (`reason`), never a default rate.
The live card and the registered backtest still price with fincalc.options / fno_history; switching them is the
horse race's decision (#377).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from finresearch.fincalc.options import Right

# The implied-volatility solver: a Newton iteration kept inside a bracket that always holds the root (a step that
# leaves the bracket, or a vega too small to divide by, is replaced by bisection). Stated tolerances:
SIGMA_LO, SIGMA_HI = 1e-4, 10.0  # the bracket: 0.01 % to 1,000 % a year; a price outside it is unknown
PRICE_TOL = (
    1e-10  # converged when |model - target| <= PRICE_TOL x the out-of-the-money twin's price (relative)
)
SIGMA_TOL = 1e-10  # ... or when the bracket is narrower than this (annual volatility, as a fraction)
MAX_ITER = 100  # Newton halves the bracket at worst, so 100 steps cannot run out on a valid bracket
MAX_DF = (
    1.05  # a discount factor above this (a rate below about -5 % over a year) is refused as an input error
)


def _cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))  # erfc keeps the lower tail accurate (1 - erf cancels)


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _intrinsic(right: Right, fwd: float, strike: float) -> float:
    return max(0.0, fwd - strike) if right == "call" else max(0.0, strike - fwd)


def _d1(fwd: float, strike: float, t: float, sigma: float) -> float:
    sd = sigma * math.sqrt(t)
    return (math.log(fwd / strike) + sd * sd / 2) / sd


def price(right: Right, fwd: float, strike: float, t: float, df: float, sigma: float) -> float:
    """The Black-76 value; sigma = 0 or T = 0 gives the discounted intrinsic value."""
    if sigma <= 0 or t <= 0:
        return df * _intrinsic(right, fwd, strike)
    d1 = _d1(fwd, strike, t, sigma)
    d2 = d1 - sigma * math.sqrt(t)
    if right == "call":
        return df * (fwd * _cdf(d1) - strike * _cdf(d2))
    return df * (strike * _cdf(-d2) - fwd * _cdf(-d1))


def vega(fwd: float, strike: float, t: float, df: float, sigma: float) -> float:
    """dPrice/dsigma per unit of sigma (1.00 = 100 vol points): DF F n(d1) sqrt T, the same for a call and a put."""
    if sigma <= 0 or t <= 0:
        return 0.0
    return df * fwd * _pdf(_d1(fwd, strike, t, sigma)) * math.sqrt(t)


def forward_delta(right: Right, fwd: float, strike: float, t: float, df: float, sigma: float) -> float:
    """dPrice/dF: DF N(d1) for a call, -DF N(-d1) for a put."""
    d1 = _d1(fwd, strike, t, sigma)
    return df * _cdf(d1) if right == "call" else -df * _cdf(-d1)


def spot_delta(
    right: Right, fwd: float, strike: float, t: float, df: float, sigma: float, spot: float
) -> float:
    """dPrice/dS with the carry the forward implies: e^(-qT) N(d1) = (F DF / S) N(d1) for a call."""
    return forward_delta(right, fwd, strike, t, df, sigma) * fwd / spot


def bounds(right: Right, fwd: float, strike: float, df: float) -> tuple[float, float]:
    """No-arbitrage bounds of a European option on a forward: DF x intrinsic < price < DF x F (call) or DF x K
    (put). A price on or outside them has no implied volatility."""
    return df * _intrinsic(right, fwd, strike), df * (fwd if right == "call" else strike)


def carry(fwd: float, spot: float, t: float) -> float:
    """c = ln(F/S) / T, the annual carry the forward implies."""
    return math.log(fwd / spot) / t


def implied_q(rate: float | None, fwd: float, spot: float, t: float) -> float | None:
    """The implied dividend yield q = r - c; unknown without a rate."""
    return None if rate is None else rate - carry(fwd, spot, t)


def expected_move(spot: float, sigma: float, t: float) -> tuple[float, float]:
    """The lognormal one-sigma band S e^(-sigma sqrt T), S e^(+sigma sqrt T) (not the symmetric S +/- S sigma sqrt T)."""
    sd = sigma * math.sqrt(t)
    return spot * math.exp(-sd), spot * math.exp(sd)


@dataclass(frozen=True)
class IvResult:
    """sigma (annual fraction) or None with `reason`; `price_error` = |model - target| at sigma; `iterations`."""

    sigma: float | None
    reason: str | None = None
    price_error: float | None = None
    iterations: int = 0


def _finite_positive(*xs: float | None) -> bool:
    return all(x is not None and math.isfinite(x) and x > 0 for x in xs)


def implied_vol(
    right: Right, target: float, fwd: float, strike: float, t: float, df: float | None
) -> IvResult:
    """The volatility that reproduces `target`, or unknown with the reason: bad inputs, a price on or outside the
    no-arbitrage bounds, or outside the [SIGMA_LO, SIGMA_HI] bracket. An in-the-money option is solved on its
    out-of-the-money twin through parity (C - P = DF (F - K)), whose time value is the same and is not swamped by
    the intrinsic value."""
    if not _finite_positive(fwd, strike, t, df) or target is None or not math.isfinite(target):
        return IvResult(None, "forward, strike, time and discount factor must be known and positive")
    if df > MAX_DF:  # type: ignore[operator]
        return IvResult(None, f"discount factor {df:g} is above {MAX_DF:g}: an input error")
    lo, hi = bounds(right, fwd, strike, df)  # type: ignore[arg-type]
    if target <= lo:
        return IvResult(None, f"price {target:g} is at or below the no-arbitrage lower bound {lo:g}")
    if target >= hi:
        return IvResult(None, f"price {target:g} is at or above the no-arbitrage upper bound {hi:g}")
    side: Right = right
    p = target
    if _intrinsic(right, fwd, strike) > 0:  # in the money: the other side at the same strike
        side = "put" if right == "call" else "call"
        p = target - df * (fwd - strike) if right == "call" else target + df * (fwd - strike)  # type: ignore[operator]
    tol = PRICE_TOL * p  # relative to the time value: an absolute tolerance would be loose on a deep wing
    a, b = SIGMA_LO, SIGMA_HI
    ga = price(side, fwd, strike, t, df, a) - p  # type: ignore[arg-type]
    if ga > 0:
        return IvResult(
            None, f"price {target:g} is below the value at sigma {SIGMA_LO:g} (outside the bracket)"
        )
    if price(side, fwd, strike, t, df, b) - p < 0:  # type: ignore[arg-type]
        return IvResult(
            None, f"price {target:g} is above the value at sigma {SIGMA_HI:g} (outside the bracket)"
        )
    # start: the Brenner-Subrahmanyam ATM approximation sigma ~ sqrt(2 pi / T) x price / (DF F), kept in the bracket
    s = min(b, max(a, math.sqrt(2 * math.pi / t) * p / (df * fwd)))  # type: ignore[operator]
    step_before = b - a
    for i in range(1, MAX_ITER + 1):
        g = price(side, fwd, strike, t, df, s) - p  # type: ignore[arg-type]
        if abs(g) <= tol:
            return IvResult(s, None, abs(g), i)
        if g > 0:
            b = s
        else:
            a = s
        if b - a < SIGMA_TOL:
            return IvResult(s, None, abs(g), i)
        v = vega(fwd, strike, t, df, s)  # type: ignore[arg-type]
        nxt = s - g / v if v > 0 else math.nan
        # Newton only while it stays inside the bracket and its step is under half the one before (Numerical
        # Recipes' rtsafe); otherwise bisect, so a flat far wing still ends within MAX_ITER
        if a < nxt < b and abs(nxt - s) < step_before / 2:
            step_before, s = abs(nxt - s), nxt
        else:
            step_before, s = (b - a) / 2, (a + b) / 2
    return IvResult(None, f"no convergence in {MAX_ITER} steps")


# --------------------------------------------------------------------------- one forward per expiry: parity regression
# y_i = C_i - P_i = DF F - DF K_i + e_i over strikes with both sides and |ln K/F| <= PARITY_BAND: slope b = -DF,
# intercept a = DF F, so F = -a/b and r_impl = -ln(DF)/T. Weighted least squares with w_i = 1/(s_C^2 + s_P^2) when
# every strike has both bid-ask spreads (else equal weights: end-of-day settles carry no spread), made robust by
# Huber IRLS on the standardised residuals. The intercept (F) is well identified on a few strikes; the slope (DF) is
# not, so DF and r_impl are reported only when their standard error and level are plausible.
PARITY_BAND = 0.05  # |ln K/F| up to 5 % (issue #385)
MIN_STRIKES = 3  # at least one residual degree of freedom
HUBER_C = 1.345  # the usual 95 %-efficiency tuning constant (Huber 1981)
HUBER_ITER = 50
BAND_PASSES = 5  # re-select the strikes around the fitted forward until the set is stable
R_IMPL_RANGE = (-0.02, 0.20)  # an implied rate outside this is not a rate but a mis-specified fit
R_SE_MAX = 0.01  # r_impl is reported only with a standard error under 1 rate point


@dataclass(frozen=True)
class ParityQuote:
    strike: float
    call: float  # mid (or a settle that is a market price)
    put: float
    call_spread: float | None = None  # ask - bid; None = unknown
    put_spread: float | None = None


@dataclass(frozen=True)
class ParityFit:
    forward: float | None
    df: float | None  # None when the slope is not identified (df_reason) or the fit failed (reason)
    r_impl: float | None
    n: int = 0
    strikes: tuple[float, ...] = ()
    weights: str = "equal"  # equal | spread
    resid_sd: float | None = None
    se_forward: float | None = None
    se_r: float | None = None
    reason: str | None = None  # why there is no forward
    df_reason: str | None = None  # why there is no DF / r_impl while the forward is known


def _wls(
    xs: list[float], ys: list[float], ws: list[float]
) -> tuple[float, float, float, float, float, float]:
    """Weighted straight line y = a + b x: (a, b, var_a, var_b, cov_ab, s^2) with s^2 = sum w e^2 / (n - 2)."""
    sw = sum(ws)
    xm, ym = (
        sum(w * x for w, x in zip(ws, xs, strict=True)) / sw,
        sum(w * y for w, y in zip(ws, ys, strict=True)) / sw,
    )
    sxx = sum(w * (x - xm) ** 2 for w, x in zip(ws, xs, strict=True))
    bb = sum(w * (x - xm) * (y - ym) for w, x, y in zip(ws, xs, ys, strict=True)) / sxx
    aa = ym - bb * xm
    s2 = sum(w * (y - aa - bb * x) ** 2 for w, x, y in zip(ws, xs, ys, strict=True)) / (len(xs) - 2)
    return aa, bb, s2 * (1 / sw + xm * xm / sxx), s2 / sxx, -xm * s2 / sxx, s2


def _huber(
    xs: list[float], ys: list[float], base: list[float]
) -> tuple[float, float, float, float, float, float]:
    ws = list(base)
    fit = _wls(xs, ys, ws)
    for _ in range(HUBER_ITER):
        u = [(y - fit[0] - fit[1] * x) * math.sqrt(w) for x, y, w in zip(xs, ys, base, strict=True)]
        scale = 1.4826 * sorted(abs(v) for v in u)[len(u) // 2]  # MAD of the standardised residuals
        if scale <= 0:
            break
        ws = [w * min(1.0, HUBER_C * scale / abs(v)) if v else w for w, v in zip(base, u, strict=True)]
        new = _wls(xs, ys, ws)
        done = abs(new[0] - fit[0]) <= 1e-12 * max(1.0, abs(fit[0])) and abs(new[1] - fit[1]) <= 1e-14
        fit = new
        if done:
            break
    return fit


def parity_fit(quotes: list[ParityQuote], t: float, *, band: float = PARITY_BAND) -> ParityFit:
    """The expiry's forward (and, when identified, DF and r_impl) from every strike with both sides priced."""
    qs = [q for q in quotes if _finite_positive(q.strike, q.call, q.put)]
    if len(qs) < MIN_STRIKES or not _finite_positive(t):
        return ParityFit(
            None, None, None, len(qs), reason=f"fewer than {MIN_STRIKES} strikes with both sides priced"
        )
    spread = all(_finite_positive(q.call_spread, q.put_spread) for q in qs)
    near = min(qs, key=lambda q: (abs(q.call - q.put), q.strike))
    fwd, used = (
        near.strike + near.call - near.put,
        (),
    )  # the single-strike start (DF ~ 1 is fine for choosing a band)
    for _ in range(BAND_PASSES):
        pick = tuple(q for q in qs if abs(math.log(q.strike / fwd)) <= band)
        if len(pick) < MIN_STRIKES:
            return ParityFit(None, None, None, len(pick), tuple(q.strike for q in pick),
                             reason=f"fewer than {MIN_STRIKES} strikes within {band:.0%} of the forward")  # fmt: skip
        if pick == used:
            break
        used = pick
        k0 = sum(q.strike for q in pick) / len(pick)  # centred strikes: the intercept is DF (F - k0)
        xs = [q.strike - k0 for q in pick]
        ys = [q.call - q.put for q in pick]
        base = [1 / (q.call_spread**2 + q.put_spread**2) for q in pick] if spread else [1.0] * len(pick)  # type: ignore[operator]
        a, bb, va, vb, cab, s2 = _huber(xs, ys, base)
        if not bb < 0:
            return ParityFit(None, None, None, len(pick), tuple(q.strike for q in pick),
                             reason="the parity slope is not negative (no discount factor)")  # fmt: skip
        fwd = k0 - a / bb
    d = -bb
    se_f = math.sqrt(max(0.0, va / bb**2 + a * a * vb / bb**4 - 2 * a * cab / bb**3))
    r, se_r = -math.log(d) / t, math.sqrt(vb) / (d * t)
    why = None
    if se_r > R_SE_MAX:
        why = f"the implied rate's standard error {se_r:.2%} is above {R_SE_MAX:.0%}: the slope is not identified"
    elif not R_IMPL_RANGE[0] <= r <= R_IMPL_RANGE[1]:
        why = f"the implied rate {r:.2%} is outside {R_IMPL_RANGE[0]:.0%} to {R_IMPL_RANGE[1]:.0%}"
    return ParityFit(fwd, None if why else d, None if why else r, len(used), tuple(q.strike for q in used),
                     "spread" if spread else "equal", math.sqrt(s2), se_f, se_r, None, why)  # fmt: skip


def basis(f_parity: float | None, f_future: float | None) -> float | None:
    """(F_parity - F_future) / F_future, the relative basis to log; unknown when either is."""
    if not _finite_positive(f_parity, f_future):
        return None
    return (f_parity - f_future) / f_future  # type: ignore[operator]


# --------------------------------------------------------------------------- the app's own IVs from mids
# IV of the mid m = (bid + ask)/2 on the out-of-the-money side (both sides at the strike nearest F), each with an
# error band dsigma = (ask - bid) / (2 vega) and a tick band TICK / vega. Crossed, locked, one-sided and zero-bid
# quotes are dropped, and so is an IV whose band exceeds MAX_BAND. A single market price without bid/ask (an
# end-of-day settle) gets an IV with an unknown spread band (None), never a zero one.
TICK = 0.05  # NSE's option tick, rupees
MAX_BAND = 0.02  # 2 vol points (issue #385)


@dataclass(frozen=True)
class OptionQuote:
    strike: float
    right: Right
    bid: float | None = None
    ask: float | None = None
    price: float | None = None  # a market price used when there is no bid/ask (e.g. a settle that traded)


@dataclass(frozen=True)
class QuoteIv:
    strike: float
    right: Right
    mid: float | None
    sigma: float | None
    band: float | None = None  # spread band in volatility (fraction); None = unknown
    tick_band: float | None = None
    reason: str | None = None


def quote_iv(q: OptionQuote, fwd: float, t: float, df: float | None) -> QuoteIv:
    def out(mid: float | None, reason: str, band: float | None = None) -> QuoteIv:
        return QuoteIv(q.strike, q.right, mid, None, band, None, reason)

    if q.bid is not None or q.ask is not None:
        if q.bid is None or q.bid <= 0:
            return out(None, "no bid")
        if q.ask is None or q.ask <= 0:
            return out(None, "no ask")
        if q.ask < q.bid:
            return out(None, "crossed quote (ask below bid)")
        if q.ask == q.bid:
            return out(None, "locked quote (ask equals bid)")
        mid, spread = (q.bid + q.ask) / 2, q.ask - q.bid
    elif q.price is not None and q.price > 0:
        mid, spread = q.price, None
    else:
        return out(None, "no price")
    if df is None:
        return out(mid, "discount factor unknown (no dated rate and no identified parity discount factor)")
    got = implied_vol(q.right, mid, fwd, q.strike, t, df)
    if got.sigma is None:
        return out(mid, got.reason or "no implied volatility")
    v = vega(fwd, q.strike, t, df, got.sigma)
    if v <= 0:
        return out(mid, "vega is zero at the solution")
    band = None if spread is None else spread / (2 * v)
    if band is not None and band > MAX_BAND:
        return out(
            mid, f"spread band {band * 100:.1f} vol points is above {MAX_BAND * 100:g} vol points", band
        )
    return QuoteIv(q.strike, q.right, mid, got.sigma, band, TICK / v)


def otm_ivs(quotes: list[OptionQuote], fwd: float, t: float, df: float | None) -> list[QuoteIv]:
    """Every out-of-the-money quote's IV (puts below F, calls above) plus both sides at the strike nearest F."""
    if not quotes:
        return []
    atm = min({q.strike for q in quotes}, key=lambda k: (abs(k - fwd), k))
    keep = [q for q in quotes if q.strike == atm or (q.right == "put") == (q.strike < fwd)]
    return [quote_iv(q, fwd, t, df) for q in sorted(keep, key=lambda q: (q.strike, q.right))]


@dataclass(frozen=True)
class AtmIv:
    strike: float | None
    sigma: float | None
    sides: int = 0
    reason: str | None = None


def atm_iv(quotes: list[OptionQuote], fwd: float, t: float, df: float | None) -> AtmIv:
    """The mean of the call and put IVs at the strike nearest F (either alone when only one is usable)."""
    if not quotes:
        return AtmIv(None, None, 0, "no quotes")
    k = min({q.strike for q in quotes}, key=lambda s: (abs(s - fwd), s))
    rows = [quote_iv(q, fwd, t, df) for q in quotes if q.strike == k]
    ok = [r.sigma for r in rows if r.sigma is not None]
    if not ok:
        return AtmIv(k, None, 0, "; ".join(f"{r.right}: {r.reason}" for r in rows))
    return AtmIv(k, sum(ok) / len(ok), len(ok))


# --------------------------------------------------------------------------- dated rates
@dataclass(frozen=True)
class DatedDf:
    df: float | None
    rate: float | None  # continuous
    source: str | None = None
    reason: str | None = None


def dated_df(answer: dict | None, t: float) -> DatedDf:
    """DF = e^(-rT) from a dated-rate answer (refseries.rate_lookup.par_rate_on / tbill_rate_on or
    signals.rates.risk_free_on). An unknown answer, or one that is a fallback (risk_free()'s fixed 6.5 % or its
    stale curve), gives no discount factor with the reason: the caller shows unknown, never a default."""
    if not answer:
        return DatedDf(None, None, None, "no dated rate")
    if answer.get("fallback"):
        return DatedDf(None, None, None, f"a fallback rate is not a dated rate ({answer.get('source')})")
    r = answer.get("rate")
    if answer.get("known") is False or r is None or not math.isfinite(r):
        return DatedDf(None, None, None, answer.get("note") or "the dated rate is unknown")
    return DatedDf(math.exp(-r * t), r, f"{answer.get('source')} (as of {answer.get('as_of')})")


def rate_gap(r_dated: float | None, r_impl: float | None) -> float | None:
    """r_dated - r_impl, the cross-check of a dated rate against the parity-implied one; unknown when either is."""
    return None if r_dated is None or r_impl is None else r_dated - r_impl
