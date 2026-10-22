"""P4.5 (#398): the risk alerts' maths. Pure arithmetic; the caller (alerts.risk) gathers the series. Alerts only:
nothing here places, modifies or cancels an order.

Registered values, from the issue's reviewed text (20-Oct-2026, "Maths"):
    U = loss / limit >= FIRE_U fires; clears below CLEAR_U                       (hysteresis)
    IV shock: z = dIV30_t / sd_252(dIV30) >= SHOCK_Z
    cumulative shock: the 5-session change in IV30 >= SHOCK_Z x sd_252 of 5-session changes
    gap: |ln(open / previous close)| / sigma_daily >= GAP_Z
    touch probability P(min_t S_t <= K) ~ 2 x P_Q(S_T <= K) (driftless reflection), at the strike's own IV,
      labelled risk-neutral, against TOUCH_LEVEL
    gap distribution g = ln(O_t / C_t-1): P(|g| > distance), against GAP_STOP_LEVEL
    precision = actioned / fired; suggest muting informational rules with precision < MUTE_PRECISION after
      >= MUTE_MIN_FIRED firings. Limit, margin, ES and gap-through-stop alerts are never suggested for muting.
TOUCH_LEVEL and GAP_STOP_LEVEL are not given by the issue ("against a registered level"): they are registered here
[U: set by this PR, listed for the owner's decision]. Every statistic needs its full window; a shorter or gapped
history is unknown (None with the reason), never "no shock".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from statistics import NormalDist, stdev

FIRE_U, CLEAR_U = 0.8, 0.7
SHOCK_Z = 3.0
SD_WINDOW = 252
CUM_SESSIONS = 5
GAP_Z = 3.0
TOUCH_LEVEL = 0.60  # [U] about a 0.30-delta short strike (touch ~ 2 x terminal probability)
GAP_STOP_LEVEL = 0.05  # [U] a 1-in-20 chance that one overnight gap alone jumps past the short strike
MIN_GAPS = 250  # about a year of stored gaps before the distribution is read
MUTE_PRECISION, MUTE_MIN_FIRED = 0.20, 10
NEVER_MUTE = frozenset({"limit", "margin", "es", "gap_stop"})
TOUCH_LABEL = ("risk-neutral (Q) probability that the underlying touches the strike before expiry, from the strike's "
               "own IV (driftless reflection, 2 x P_Q(S_T beyond K)): not a forecast of the real-world chance")  # fmt: skip


@dataclass(frozen=True)
class Stat:
    """A statistic (None = unknown) and its inputs, or why it is unknown."""

    value: float | None
    detail: str


def _window(xs: Sequence[float | None], n: int, what: str) -> tuple[list[float] | None, str]:
    if len(xs) < n:
        return None, f"{what}: {len(xs)} sessions stored, {n} needed for the {SD_WINDOW}-session window"
    w = list(xs[-n:])
    if any(x is None for x in w):
        return None, f"{what}: a session inside the {SD_WINDOW}-session window has no value"
    return w, ""  # type: ignore[return-value]


def iv_shock(ivs: Sequence[float | None]) -> Stat:
    """z = today's IV30 change / the sample sd of the SD_WINDOW changes before it. `ivs`: consecutive sessions, old to
    new, the last one today."""
    w, why = _window(ivs, SD_WINDOW + 2, "IV30 shock")
    if w is None:
        return Stat(None, why)
    d = [b - a for a, b in zip(w, w[1:], strict=False)]
    sd = stdev(d[:-1])
    if sd <= 0:
        return Stat(None, "IV30 shock: the past changes have no spread")
    return Stat(d[-1] / sd, f"IV30 {w[-2]:.2f} -> {w[-1]:.2f} ({d[-1]:+.2f} vol pts); sd of {SD_WINDOW} daily "
                f"changes {sd:.2f}")  # fmt: skip


def cum_shock(ivs: Sequence[float | None]) -> Stat:
    """The CUM_SESSIONS-session IV30 change over the sample sd of the SD_WINDOW such changes ending at or before
    t - CUM_SESSIONS (none overlaps today's window)."""
    k = CUM_SESSIONS
    w, why = _window(ivs, SD_WINDOW + 2 * k, "cumulative IV30 shock")
    if w is None:
        return Stat(None, why)
    c = [w[i] - w[i - k] for i in range(k, len(w))]
    sd = stdev(c[-SD_WINDOW - k : -k])
    if sd <= 0:
        return Stat(None, "cumulative IV30 shock: the past changes have no spread")
    return Stat(c[-1] / sd, f"IV30 {w[-1 - k]:.2f} -> {w[-1]:.2f} over {k} sessions ({c[-1]:+.2f} vol pts); sd of "
                f"{SD_WINDOW} {k}-session changes {sd:.2f}")  # fmt: skip


def gap_z(*, open_: float | None, prev_close: float | None, closes: Sequence[float | None]) -> Stat:
    """|ln(open / previous close)| / sigma_daily, sigma_daily the sample sd of the SD_WINDOW close-to-close log
    returns up to the previous close (`closes` ends with it)."""
    if open_ is None or prev_close is None or open_ <= 0 or prev_close <= 0:
        return Stat(None, "gap: the open or the previous close is unknown")
    w, why = _window(closes, SD_WINDOW + 1, "gap")
    if w is None:
        return Stat(None, why)
    sigma = stdev([math.log(b / a) for a, b in zip(w, w[1:], strict=False)])
    if sigma <= 0:
        return Stat(None, "gap: the past returns have no spread")
    g = math.log(open_ / prev_close)
    return Stat(abs(g) / sigma, f"open {open_:,.2f} vs previous close {prev_close:,.2f} ({g:+.2%}); daily sigma "
                f"{sigma:.2%} over {SD_WINDOW} sessions")  # fmt: skip


def gap_tail(gaps: Sequence[float], distance: float) -> tuple[float | None, int]:
    """(share of stored overnight gaps with |g| > distance, sample size); None below MIN_GAPS."""
    n = len(gaps)
    if n < MIN_GAPS:
        return None, n
    return sum(abs(g) > distance for g in gaps) / n, n


def touch_prob(forward: float, strike: float, vol: float, t_years: float) -> float:
    """~ 2 x P_Q(S_T beyond K) on the forward, lognormal at `vol`: 2 N(-d2) for a strike below F, 2 N(d2) above,
    capped at 1 (see TOUCH_LABEL)."""
    if t_years <= 0 or vol <= 0 or strike == forward:
        return 1.0
    d2 = (math.log(forward / strike) - vol * vol * t_years / 2) / (vol * math.sqrt(t_years))
    p = NormalDist().cdf(-d2) if strike < forward else NormalDist().cdf(d2)
    return min(1.0, 2 * p)


def hysteresis(prev: str, u: float, fire: float = FIRE_U, clear: float = CLEAR_U) -> str:
    """"fired" or "clear": a fired alert stays fired until u drops below `clear`; anything else fires at `fire`."""
    if prev == "fired":
        return "fired" if u >= clear else "clear"
    return "fired" if u >= fire else "clear"


def precision(actioned: int, fired: int) -> float | None:
    return None if fired <= 0 else actioned / fired


def suggest_mute(category: str, *, fired: int, actioned: int) -> bool:
    if category in NEVER_MUTE or fired < MUTE_MIN_FIRED:
        return False
    p = precision(actioned, fired)
    return p is not None and p < MUTE_PRECISION
