"""fincalc/svi.w and fincalc/smile (sigma, density, cdf) against QuantLib's SviSmileSection, with mpmath as referee.

Conventions pinned first:
* QuantLib's parameter order is `SviSmileSection(T, forward, [a, b, sigma, rho, m])`; the module's `Raw` is
  (a, b, m, rho, s) with s = QuantLib's sigma;
* k = ln(K / F); `SmileSection.variance(K)` is the TOTAL variance w(k), `volatility(K)` is sqrt(w / T);
* the module's `density(p, k)` is the density of k = ln(F_T / F); the density of F_T at K is density / K, which is what
  `SmileSection.density(K)` returns; `cdf(p, k)` is P(F_T < K) = 1 - (an undiscounted digital call).

QuantLib gets the density and the digital by FINITE DIFFERENCES of option prices, so it is a witness to about 1e-7,
not a referee. The referee is mpmath: the second and first strike derivatives of the 40-digit Black call priced at
the slice's own volatility (Breeden and Litzenberger 1978), which shares nothing with the module's closed form
g(k) / sqrt(2 pi w) exp(-d-^2 / 2) (Gatheral and Jacquier 2014, eq. 2.1-2.2).
"""

from __future__ import annotations

import math

import mpmath as mp
import QuantLib as ql

from finresearch.fincalc import smile, svi

mp.mp.dps = 40
FWD, T = 100.0, 0.5
SLICES = {
    "index, three months": svi.Raw(a=0.02, b=0.1, m=0.05, rho=-0.4, s=0.2),
    "weekly, steep skew": svi.Raw(a=0.0004, b=0.012, m=-0.01, rho=-0.7, s=0.03),
    "stock, near-symmetric": svi.Raw(a=0.01, b=0.25, m=0.0, rho=-0.1, s=0.35),
    # Gatheral and Jacquier (2014), Example 3.1: Vogt's smile, which has butterfly arbitrage (g < 0 near k = 0.8):
    # the closed form must follow the price curvature there too, negative density included
    "GJ Example 3.1 (arbitrageable)": svi.Raw(a=-0.0410, b=0.1331, m=0.3586, rho=0.3060, s=0.4153),
}
KS = [round(-1.2 + 0.05 * i, 2) for i in range(49)]


def section(p: svi.Raw) -> ql.SviSmileSection:
    return ql.SviSmileSection(T, FWD, [p.a, p.b, p.s, p.rho, p.m])


def _N(x):
    return mp.erfc(-x / mp.sqrt(2)) / 2


def mp_call(p: svi.Raw, strike):
    """The undiscounted call on a forward of 1 at the slice's own total variance, in 40 digits."""
    a, b, m, rho, s = (mp.mpf(v) for v in (p.a, p.b, p.m, p.rho, p.s))
    k = mp.log(strike)
    w = a + b * (rho * (k - m) + mp.sqrt((k - m) ** 2 + s * s))
    sd = mp.sqrt(w)
    d1 = -k / sd + sd / 2
    return _N(d1) - strike * _N(d1 - sd)


def test_total_variance_and_volatility_match_quantlib(oracle):
    """One line of arithmetic on each side: float rounding only (1e-13 relative)."""
    worst_w, worst_v, n = 0.0, 0.0, 0
    for p in SLICES.values():
        sec = section(p)
        for k in KS:
            n += 1
            strike = FWD * math.exp(k)
            worst_w = max(worst_w, abs(float(svi.w(p, k)) / sec.variance(strike) - 1))
            worst_v = max(worst_v, abs(smile.sigma(p, k, T) / sec.volatility(strike) - 1))
    oracle.check("svi.w", "QuantLib SviSmileSection.variance", grid=n, max_diff=worst_w, limit=1e-13,
                 why="relative (float rounding)")  # fmt: skip
    oracle.check("smile.sigma", "QuantLib SviSmileSection.volatility", grid=n, max_diff=worst_v, limit=1e-13,
                 why="relative (float rounding)")  # fmt: skip


def test_density_and_cdf_are_the_strike_derivatives_of_the_price(oracle):
    """Against 40-digit derivatives: K d2C/dK2 for the density of k and 1 + dC/dK for P(F_T < K). The closed forms
    add terms of order one (g) and a normal density, so rounding is absolute: 1e-12 of the slice's peak density,
    1e-12 of a probability. Vogt's slice has g < 0 on part of the grid: the density must be negative there on both
    sides, which is why an unchecked slice is refused upstream (svi.check)."""
    worst_d, worst_c, n, negative = (0.0, None), (0.0, None), 0, 0
    for name, p in SLICES.items():
        peak = max(abs(smile.density(p, k)) for k in KS)
        for k in KS:
            n += 1
            strike = mp.exp(mp.mpf(k))
            ref_d = strike * mp.diff(lambda x, p=p: mp_call(p, x), strike, 2)
            ref_c = 1 + mp.diff(lambda x, p=p: mp_call(p, x), strike, 1)
            negative += ref_d < 0
            assert (smile.density(p, k) < 0) == (ref_d < 0) or abs(ref_d) < 1e-12 * peak
            worst_d = max(
                worst_d, (float(abs(smile.density(p, k) - ref_d)) / peak, (name, k)), key=lambda x: x[0]
            )
            worst_c = max(worst_c, (float(abs(smile.cdf(p, k) - ref_c)), (name, k)), key=lambda x: x[0])
    assert negative > 0  # the arbitrageable slice really has a negative-density stretch on the grid
    oracle.check("smile.density", "mpmath K d2C/dK2, 40 digits", grid=n, max_diff=worst_d[0], limit=1e-12,
                 why="of the slice's peak density (float rounding)", worst=worst_d[1])  # fmt: skip
    oracle.check("smile.cdf", "mpmath 1 + dC/dK, 40 digits", grid=n, max_diff=worst_c[0], limit=1e-12,
                 why="absolute probability (float rounding)", worst=worst_c[1])  # fmt: skip


def test_density_and_cdf_match_quantlibs_finite_differences(oracle):
    """QuantLib differentiates its option prices numerically, so the tolerance is its truncation error, worked out
    from the slice rather than fitted: a central second difference with gap h is off by h^2/12 x p''(K), and a
    density of width W = F sqrt(w(0)) has |p''| <= peak / W^2 if it is Gaussian; 4x that covers SVI's skew
    (h^2 / (3 W^2) of the peak, plus 1e-7 for the rounding of the difference). The digital is a first difference:
    h^2/24 x p'(K), at most h^2 / (24 W^2) in probability with the same margin, plus 1e-9. With QuantLib's default
    gap of 1e-4 rounding dominates instead and its density is only good to about 1e-4 of the peak."""
    worst_d, worst_c, default_gap, n = (0.0, None), (0.0, None), 0.0, 0
    h_d, h_c = 1e-2, 1e-3
    for name, p in SLICES.items():
        sec = section(p)
        width = FWD * math.sqrt(float(svi.w(p, 0.0)))
        peak = max(abs(smile.density(p, k)) / (FWD * math.exp(k)) for k in KS)
        tol_d = (h_d**2 / (3 * width**2) + 1e-7) * peak
        tol_c = h_c**2 / (24 * width**2) + 1e-9
        for k in KS:
            n += 1
            strike = FWD * math.exp(k)
            ours = smile.density(p, k) / strike
            worst_d = max(
                worst_d, (abs(ours - sec.density(strike, 1.0, h_d)) / tol_d, (name, k)), key=lambda x: x[0]
            )
            default_gap = max(default_gap, abs(ours - sec.density(strike)) / peak)
            digital = 1 - sec.digitalOptionPrice(strike, ql.Option.Call, 1.0, h_c)
            worst_c = max(worst_c, (abs(smile.cdf(p, k) - digital) / tol_c, (name, k)), key=lambda x: x[0])
    oracle.check("smile.density / K", "QuantLib SviSmileSection.density (gap 0.01)", grid=n, max_diff=worst_d[0],
                 limit=1.0, why="x QuantLib's finite-difference truncation, h^2 / (3 W^2) of the peak", worst=worst_d[1])  # fmt: skip
    oracle.check("smile.cdf", "QuantLib 1 - digitalOptionPrice(call) (gap 0.001)", grid=n, max_diff=worst_c[0],
                 limit=1.0, why="x QuantLib's finite-difference truncation, h^2 / (24 W^2)", worst=worst_c[1])  # fmt: skip
    oracle.note("smile.density / K", "QuantLib SviSmileSection.density (default gap 1e-4)", grid=n,
                max_diff=default_gap, tolerance="none: of the peak density; rounding of a second difference at h = 1e-4",
                verdict="(c) oracle limit: QuantLib's density is a finite difference; mpmath agrees with in-house to 1e-12")  # fmt: skip
