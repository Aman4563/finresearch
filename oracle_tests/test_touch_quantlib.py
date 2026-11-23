"""fincalc/risk_alerts' two touch variants against the exact first-passage probability (QuantLib's one-touch engine).

Since #520 the alert reads `touch_prob_exact` (variant 2, registered in fincalc/risk_alert_specs.py), which must AGREE
with QuantLib to float rounding. `touch_prob` (variant 1, registered with #398 and kept unedited for the replay
comparison) is the stated shortcut 2 x P_Q(S_T beyond K) on a driftless lognormal forward ("driftless reflection"),
capped at 1; its gap stays measured here. The exact probability that a lognormal martingale F touches K before T is,
by the reflection principle with the -sigma^2/2 drift of ln F (Reiner and Rubinstein 1991, "Unscrambling the binary
code", Risk 4(9), the one-touch at zero rates):

    K < F:  N(-d2) + (F/K) N(-d1)          K > F:  N(d2) + (F/K) N(d1)

QuantLib's `AnalyticDigitalAmericanEngine` prices a cash-at-hit one-touch; with zero rate and zero dividend yield
(spot = forward, nothing discounted) its price IS that probability, which the test confirms before using it.

Finding of the first slice (#503), acted on by #520: the shortcut is not conservative on both sides. It OVERSTATES
the touch probability of a strike below the forward by (undiscounted put) / K and UNDERSTATES that of a strike above
it by (undiscounted call) / K. A short call was the leg where the alert fired later than the exact probability would.
Still open for the owner: the 0.60 level is unchanged, so the two variants fire on different strikes.
"""

from __future__ import annotations

import itertools
import math
from statistics import NormalDist

import QuantLib as ql

from finresearch.fincalc import black76 as b
from finresearch.fincalc import risk_alerts as ra

N = NormalDist().cdf
FWD = 24000.0
TODAY = ql.Date(2, 1, 2026)
MONEYNESS = [0.80, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 1.005, 1.01, 1.02, 1.03, 1.05, 1.10, 1.20]
VOLS = [0.08, 0.12, 0.15, 0.20, 0.30, 0.45, 0.60]
DAYS = [1, 2, 7, 14, 30, 60, 90]


def one_touch(strike: float, vol: float, days: int) -> float:
    ql.Settings.instance().evaluationDate = TODAY
    dc = ql.Actual365Fixed()
    zero = ql.YieldTermStructureHandle(ql.FlatForward(TODAY, 0.0, dc))
    vols = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(TODAY, ql.NullCalendar(), vol, dc))
    process = ql.BlackScholesMertonProcess(ql.QuoteHandle(ql.SimpleQuote(FWD)), zero, zero, vols)
    payoff = ql.CashOrNothingPayoff(ql.Option.Put if strike < FWD else ql.Option.Call, strike, 1.0)
    option = ql.VanillaOption(
        payoff, ql.AmericanExercise(TODAY, TODAY + days)
    )  # paid when the barrier is hit
    option.setPricingEngine(ql.AnalyticDigitalAmericanEngine(process))
    return option.NPV()


def closed_form(strike: float, vol: float, t: float) -> float:
    sd = vol * math.sqrt(t)
    d1 = (math.log(FWD / strike) + sd * sd / 2) / sd
    d2 = d1 - sd
    if strike < FWD:
        return N(-d2) + FWD / strike * N(-d1)
    return N(d2) + FWD / strike * N(d1)


def test_touch_variants_against_the_exact_one_touch(oracle):
    worst_cf, worst_v2, over, under, ident, n = 0.0, (0.0, None), (0.0, None), (0.0, None), 0.0, 0
    assert ra.TOUCH_VARIANTS[ra.TOUCH_VARIANT][0] is ra.touch_prob_exact  # what the alert reads
    for m, vol, days in itertools.product(MONEYNESS, VOLS, DAYS):
        n += 1
        k, t = FWD * m, days / 365
        exact = one_touch(k, vol, days)
        worst_cf = max(worst_cf, abs(exact - closed_form(k, vol, t)))
        v2 = ra.touch_prob_exact(FWD, k, vol, t)
        assert v2 is not None
        worst_v2 = max(worst_v2, (abs(v2 - exact), (m, vol, days, v2, exact)), key=lambda x: x[0])
        got = ra.touch_prob(FWD, k, vol, t)
        assert 0.0 <= exact <= 1.0 and 0.0 <= got <= 1.0
        gap = got - exact
        if (
            got < 1.0
        ):  # uncapped: the gap is exactly the undiscounted option over the strike, with the side's sign
            option = b.price("put" if k < FWD else "call", FWD, k, t, 1.0, vol) / k
            ident = max(ident, abs(gap - (option if k < FWD else -option)))
        if k < FWD:
            assert gap >= -1e-12  # never below the exact probability for a strike under the forward
            over = max(over, (gap, (m, vol, days, got, exact)), key=lambda x: x[0])
        else:
            assert gap <= 1e-12  # never above it for a strike over the forward
            under = max(under, (-gap, (m, vol, days, got, exact)), key=lambda x: x[0])
    oracle.check("reflection formula N(-d2) + (F/K) N(-d1)", "QuantLib AnalyticDigitalAmericanEngine, zero rates",
                 grid=n, max_diff=worst_cf, limit=1e-12, why="absolute probability (float rounding)")  # fmt: skip
    oracle.check("risk_alerts.touch_prob_exact (the alert's variant since #520)",
                 "QuantLib AnalyticDigitalAmericanEngine, zero rates", grid=n, max_diff=worst_v2[0], limit=1e-12,
                 why="absolute probability (float rounding)", worst=worst_v2[1])  # fmt: skip
    oracle.check("risk_alerts.touch_prob - exact", "(undiscounted Black-76 option) / K, signed by side", grid=n,
                 max_diff=ident, limit=1e-12, why="absolute: the shortcut's error is exactly this identity")  # fmt: skip
    oracle.note("risk_alerts.touch_prob (variant 1, strike below F)", "QuantLib one-touch", grid=n // 2,
                max_diff=over[0],
                tolerance=f"none: largest OVERstatement in probability, at {over[1][0]:.4g} F, {over[1][1]:.0%} vol, {over[1][2]} days",
                verdict="(b) the shortcut registered with #398 overstates by put / K; kept for the replay, not read by "
                        "the alert since #520")  # fmt: skip
    oracle.note("risk_alerts.touch_prob (variant 1, strike above F)", "QuantLib one-touch", grid=n // 2,
                max_diff=under[0],
                tolerance=f"none: largest UNDERstatement in probability, at {under[1][0]:.4g} F, {under[1][1]:.0%} vol, {under[1][2]} days",
                verdict="(b) the shortcut registered with #398 understates by call / K; kept for the replay, not read "
                        "by the alert since #520")  # fmt: skip


def test_where_the_touch_alert_fires_against_the_exact_probability(oracle):
    """The alert fires at TOUCH_LEVEL (0.60), unchanged by #520; until then it read the shortcut, now the exact
    variant. For a short call and a short put 30 days out at 15 % vol: the strike where the alert's variant reads 0.60
    must be where QuantLib's one-touch is 0.60, and the strike where the shortcut read 0.60 is measured against it
    (the two variants fire on different strikes; whether 0.60 stays is the owner's decision)."""
    vol, days = 0.15, 30
    t = days / 365

    def strike_where(f, lo, hi):
        for _ in range(200):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if f(mid) > ra.TOUCH_LEVEL else (lo, mid)
        return (lo + hi) / 2

    k_short = strike_where(lambda k: ra.touch_prob(FWD, k, vol, t), FWD * 1.0001, FWD * 1.5)
    k_exact = strike_where(lambda k: closed_form(k, vol, t), FWD * 1.0001, FWD * 1.5)
    exact_at_fire = closed_form(k_short, vol, t)
    assert exact_at_fire > ra.TOUCH_LEVEL and k_exact > k_short
    kp_short = strike_where(
        lambda k: -ra.touch_prob(FWD, k, vol, t) + 2 * ra.TOUCH_LEVEL, FWD * 0.5, FWD * 0.9999
    )
    exact_put = closed_form(kp_short, vol, t)
    assert exact_put < ra.TOUCH_LEVEL
    # the alert's own thresholds (variant 2): QuantLib's one-touch is 0.60 at those strikes
    kc_alert = strike_where(lambda k: ra.touch_prob_exact(FWD, k, vol, t), FWD * 1.0001, FWD * 1.5)
    kp_alert = strike_where(
        lambda k: -ra.touch_prob_exact(FWD, k, vol, t) + 2 * ra.TOUCH_LEVEL, FWD * 0.5, FWD * 0.9999
    )
    assert kc_alert > k_short and kp_alert > kp_short  # a call fires from further away, a put only nearer
    at_alert = max(abs(one_touch(k, vol, days) - ra.TOUCH_LEVEL) for k in (kc_alert, kp_alert))
    oracle.check("risk_alerts touch alert at its 0.60 level (variant 2; 30 days, 15 % vol)",
                 "QuantLib one-touch at the strikes where the alert turns on", grid=2, max_diff=at_alert, limit=1e-9,
                 why="absolute probability (the strikes are found by bisection)")  # fmt: skip
    oracle.note("variant 1 at the 0.60 level, short call (30 days, 15 % vol)", "exact one-touch probability", grid=1,
                max_diff=exact_at_fire - ra.TOUCH_LEVEL,
                tolerance=(f"none: where the shortcut reached 0.60 (strike {k_short / FWD - 1:+.2%} from F) the exact "
                           f"probability is {exact_at_fire:.3f}; the alert now turns on at {kc_alert / FWD - 1:+.2%}"),
                verdict="needs decision: keep 0.60 or re-centre (#520); the shortcut fired later on the call side")  # fmt: skip
    oracle.note("variant 1 at the 0.60 level, short put (30 days, 15 % vol)", "exact one-touch probability", grid=1,
                max_diff=ra.TOUCH_LEVEL - exact_put,
                tolerance=(f"none: where the shortcut reached 0.60 (strike {kp_short / FWD - 1:+.2%} from F) the exact "
                           f"probability is {exact_put:.3f}; the alert now turns on at {kp_alert / FWD - 1:+.2%}"),
                verdict="needs decision: keep 0.60 or re-centre (#520); the shortcut fired earlier on the put side")  # fmt: skip
