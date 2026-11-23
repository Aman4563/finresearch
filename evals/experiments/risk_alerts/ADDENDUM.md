# Addendum to the risk alerts' registered values (#398): post hoc, and there is no PREREG.md

The risk alerts shipped on 22-Oct-2026 (PR #453, merge commit `75df78f8`) with their values "registered" in the
docstring of `src/finresearch/fincalc/risk_alerts.py` and in that PR's "needs decision" list. No `PREREG.md` was
written and no hash was pinned. This file starts the experiment's record; everything in it is post hoc.

## A1, 23-Nov-2026: the touch probability's record, and the exact formula as variant 2 (#520)

**Record.** `src/finresearch/fincalc/risk_alert_specs.py` (`risk-alert-touch-v2`, `VARIANTS_TRIED = 2`) holds both
variants of the touch formula and the definition of the replay that compares them. Its hash is pinned in
`risk_alert_prereg.py`:

| What | sha256 |
| --- | --- |
| the whole record | `c0ae2e722f941156cf3e3f7377d0484d708497332d900126c9c4dca4a5df7f16` |
| `touch-reflection-v1` | `fa8af077ae8554be57d4b63bc1255aec738949576a7337c39e44c2b5280adb40` |
| `touch-exact-v2` | `0efb05734c344497c63d8b8443667e48b5acb43856fc351242dfb08bdcd96997` |

Committed and pushed before any code that replays stored marks existed. No `fno_mark` row was read for it, in the
live database or any copy.

**Variant 1, `touch-reflection-v1` (as shipped, not edited).** `touch_prob`: min(1, 2 N(∓d2)), twice the
risk-neutral probability of expiring beyond the strike, with `TOUCH_LABEL` and `TOUCH_LEVEL = 0.60`. The record reads
the label and the level from `risk_alerts.py`, so an edit there moves the hash. It stays callable for the replay.

**Variant 2, `touch-exact-v2` (what the alert reads from this change).** `touch_prob_exact`, with
`TOUCH_LABEL_EXACT`, at the same level. Under Q the forward is a driftless lognormal martingale, so ln F is a Brownian
motion with drift −σ²/2, and its first-passage probability (reflection principle with drift) is, with
d1 = (ln(F/K) + σ²T/2) / (σ√T) and d2 = d1 − σ√T:

    K < F (a short put's strike):   N(−d2) + (F/K) N(−d1)
    K > F (a short call's strike):  N(d2)  + (F/K) N(d1)

The derivation is in the function's docstring. Against variant 1: exact = shortcut − put/K below the forward and
shortcut + call/K above it (undiscounted Black-76 prices), so the shortcut overstated a short put and understated a
short call.

**Why this is post hoc.** Variant 2 was chosen after the oracle suite (PR #511) measured variant 1 against QuantLib's
one-touch on 686 synthetic points (up to +0.097 below the forward and −0.116 above). The choice was made on
synthetic inputs and on the mathematics, not on any outcome of the stored history.

**Checks, all on synthetic inputs.** Hand-worked values at σ√T = 0.2 and |ln(K/F)| = 0.1 (0.64767 below, 0.58604
above; variant 1 gives 0.68916 and 0.54851); a brute-force Monte Carlo on a fine grid, extrapolated in √Δt
(`tests/test_touch_exact_mc.py`); QuantLib's `AnalyticDigitalAmericanEngine` at zero rates
(`oracle_tests/test_touch_quantlib.py`).

### The level: 0.60 is kept, and what it now means (needs the owner's decision)

`TOUCH_LEVEL` is not changed. Under variant 1 it was one number on both sides: a strike whose risk-neutral
probability of expiring in the money, N(∓d2), is 0.30. Under variant 2 it is not one number: the strike at which
p = 0.60 moves with σ√T, differently for puts and calls. Delta below is the undiscounted Black-76 forward delta,
N(−d1) for a put and N(d1) for a call.

| σ, days (σ√T) | Variant | Put strike vs F | Put N(−d2) | Put delta | Call strike vs F | Call N(d2) | Call delta |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 10 %, 7 (0.014) | 1 | −0.733 % | 0.300 | 0.295 | +0.719 % | 0.300 | 0.305 |
| | 2 | −0.728 % | 0.301 | 0.296 | +0.725 % | 0.299 | 0.304 |
| 15 %, 30 (0.043) | 1 | −2.320 % | 0.300 | 0.285 | +2.186 % | 0.300 | 0.315 |
| | 2 | −2.271 % | 0.304 | 0.289 | +2.238 % | 0.296 | 0.311 |
| 20 %, 60 (0.081) | 1 | −4.478 % | 0.300 | 0.272 | +4.002 % | 0.300 | 0.329 |
| | 2 | −4.308 % | 0.308 | 0.280 | +4.191 % | 0.292 | 0.321 |
| 30 %, 90 (0.149) | 1 | −8.535 % | 0.300 | 0.250 | +6.932 % | 0.300 | 0.354 |
| | 2 | −7.992 % | 0.314 | 0.263 | +7.600 % | 0.286 | 0.338 |
| 60 %, 90 (0.298) | 1 | −18.178 % | 0.300 | 0.205 | +11.835 % | 0.300 | 0.410 |
| | 2 | −16.266 % | 0.328 | 0.228 | +14.723 % | 0.271 | 0.378 |

So at 0.60 the two variants fire on different strikes: a short put between the two put columns (−2.320 % to −2.271 %
at 15 % and 30 days) fired under variant 1 and does not under variant 2; a short call between the two call columns
(+2.186 % to +2.238 %) fires only under variant 2. The band is narrower than one NIFTY strike step at index
volatility and a month, and several per cent wide for a 60 % stock at three months.

**Needs decision: keep 0.60 or re-centre.** Keeping it means "the exact risk-neutral chance of a touch is at least
60 %", with the delta equivalents above. Re-centring (one level, or one per side) would be a new registered variant
of the level; it is not done here.

### Continuous formula, daily closes

The formula assumes the barrier is watched continuously. The alert is evaluated once a night on the day's close. A
daily-close watch sees fewer touches (for a strike 5 % away, 30 days and 15 % volatility: 0.239 continuous against
about 0.194 over 21 equally spaced closes, by Monte Carlo), so the continuous figure is the larger of the two; it is
also the chance of an intraday touch, which is what a short strike's seller lives through. The label says so.
`touch_prob_exact(..., monitoring_dates=m)` applies the continuity correction of Broadie, Glasserman and Kou (1997),
"A continuity correction for discrete barrier options", *Mathematical Finance* 7(4), 325–349: the barrier is moved
away from the forward by exp(β σ √(T/m)), β = −ζ(½)/√(2π) ≈ 0.5826. Their Theorem 1.1 and Corollary 4.1 state it for
barrier and binary barrier option prices; using it for the plain touch probability is this module's reading, tested
against a daily-step Monte Carlo (it removes about 95 % of the gap at 21 dates). It is **off**: the alert does not
use it, real sessions are not equally spaced (weekends), and turning it on would be a new variant.

### The replay that compares the variants (definitions fixed here, before the code)

`finresearch fno risk-replay --touch [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--json]`, read-only. It decides nothing.

- **Series:** `fno_mark` rows of short option legs (paper and Groww) on every stored session in the range. The book
  is personal: only counts are printed, and nothing leaves the machine.
- **Inputs:** as the nightly alert reads them: forward = spot × exp(0.065 × t_years), the leg's own stored IV,
  t_years and strike; both variants on the same inputs.
- **Known leg-day:** a row whose direction is confirmed short and whose IV, spot, time and strike are stored and
  positive. Any other row is an unknown leg-day: counted, never "below the level".
- **At the level:** p ≥ 0.60, per variant. **Leg-days that change:** known leg-days at the level under exactly one
  variant, split by the strike's side of the forward and by which variant.
- **Alert-day:** a session with at least one known leg-day at the level under the variant. A session with only
  unknown leg-days is counted as unknown, not as "no alert". **Alert-days that change:** sessions that are an
  alert-day under exactly one variant.
- **Firings:** per leg (symbol, expiry, strike, right), rows in session order, as `alerts.risk.apply`: a known
  reading at the level fires unless the leg's last reading was also at the level; a reading below the level or an
  unknown one ends the standing state.
- **Criterion:** none. No count changes the formula or the level without a new registered variant.

The marks exist only since #383 (October 2026), so the replay is as deep as the book's own stored marks.

Reported beside the registered counts (noted 23-Nov-2026, before any run on stored marks): the first and last stored
session in the range, the registration hash, and the largest difference in probability between the two variants on
each side of the forward. None of them identifies a leg, a strike or an underlying.

### State carried across the switch

The rule's label and level are unchanged, so its signature in `alert_rule_state` is unchanged and standing state is
kept: a leg that stood fired under variant 1 does not fire again under variant 2, and clears quietly if the exact
probability is below 0.60. The alert-quality precision of the touch rule therefore mixes firings of both variants;
each alert's text names the formula that produced it.
