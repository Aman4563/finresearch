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

import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist, stdev
from typing import Any

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
    d = [b - a for a, b in itertools.pairwise(w)]
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


def gap_z(*, open_: float | None, prev_close: float | None, rets: Sequence[float | None]) -> Stat:
    """|ln(open / previous close)| / sigma_daily, sigma_daily the sample sd of the last SD_WINDOW close-to-close log
    returns up to the previous close (`rets`, old to new; same-contract pairs, see `front_future`)."""
    if open_ is None or prev_close is None or open_ <= 0 or prev_close <= 0:
        return Stat(None, "gap: the open or the previous close is unknown")
    w, why = _window(rets, SD_WINDOW, "gap")
    if w is None:
        return Stat(None, why)
    sigma = stdev(w)
    if sigma <= 0:
        return Stat(None, "gap: the past returns have no spread")
    g = math.log(open_ / prev_close)
    return Stat(abs(g) / sigma, f"open {open_:,.2f} vs previous close {prev_close:,.2f} ({g:+.2%}); daily sigma "
                f"{sigma:.2%} over {SD_WINDOW} sessions")  # fmt: skip


@dataclass(frozen=True)
class FutureDay:
    """One session of the front future: its open, the same contract's previous close, the overnight gap
    g = ln(open / previous close) and the close-to-close return ln(close / previous close)."""

    day: date
    expiry: date
    open: float | None
    prev_close: float
    gap: float | None
    ret: float | None
    close: float | None = None


def front_future(rows: Iterable[tuple[date, date, float | None, float | None]]) -> list[FutureDay]:
    """The front-future session series from (day, expiry, open, close) rows of every future of one underlying: each
    day's front is its nearest expiry on or after the day, paired with that same contract's previous stored close
    (never another contract's, so a roll adds no false gap). Days whose front has no previous close are left out."""
    by_day: dict[date, dict[date, tuple[float | None, float | None]]] = {}
    for d, e, op, cl in rows:
        by_day.setdefault(d, {})[e] = (op, cl)
    last_close: dict[date, float] = {}
    out: list[FutureDay] = []
    for d in sorted(by_day):
        live = [e for e in by_day[d] if e >= d]
        if live:
            e = min(live)
            op, cl = by_day[d][e]
            pc = last_close.get(e)
            if pc is not None and pc > 0:
                g = math.log(op / pc) if op and op > 0 else None
                r = math.log(cl / pc) if cl and cl > 0 else None
                out.append(FutureDay(d, e, op, pc, g, r, cl if cl and cl > 0 else None))
        for e, (_, cl) in by_day[d].items():
            if cl and cl > 0:
                last_close[e] = cl
    return out


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
    """ "fired" or "clear": a fired alert stays fired until u drops below `clear`; anything else fires at `fire`."""
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


# --------------------------------------------------------------------------- the acceptance replay (#398)
# Registered with this PR [U, for the owner's decision]: a firing is a false alarm when a synthetic short 30-day
# ATM straddle (struck at the firing day's front-future close, priced at that day's IV30 by Black-76) loses no more
# than FALSE_ALARM_LOSS of the forward within FOLLOW_SESSIONS sessions; the issue allows at most MAX_FALSE_ALARMS a
# year. Scenario days and the Feb-Mar 2026 episode are the issue's.
FALSE_ALARM_LOSS = 0.01
FOLLOW_SESSIONS = 2
STRADDLE_DAYS = 30
REPLAY_RATE = 0.065
MAX_FALSE_ALARMS = 12
SCENARIO_DAYS = (date(2024, 6, 4), date(2024, 8, 5), date(2025, 4, 7))
EPISODES = ((date(2026, 2, 1), date(2026, 3, 31)),)
SHOCK_RULES = ("iv_shock", "cum_shock", "gap")


@dataclass(frozen=True)
class Firing:
    day: date
    rules: tuple[str, ...]  # the shock rules that turned on that day
    values: dict[str, float]
    loss: (
        float | None
    )  # the synthetic straddle's loss over FOLLOW_SESSIONS, a fraction of the forward; None = no data
    false_alarm: bool | None


def straddle_loss(f0: float, iv0: float, f1: float, iv1: float, days: int) -> float:
    """(P1 - P0) / F0 for a short ATM straddle struck at F0: P0 at T = STRADDLE_DAYS / 365, P1 `days` later."""
    from finresearch.fincalc.book_scenarios import b76_price

    t0, t1 = STRADDLE_DAYS / 365, (STRADDLE_DAYS - days) / 365
    p0 = sum(b76_price(r, f0, f0, t0, REPLAY_RATE, iv0 / 100) for r in ("call", "put"))
    p1 = sum(b76_price(r, f1, f0, t1, REPLAY_RATE, iv1 / 100) for r in ("call", "put"))
    return (p1 - p0) / f0


def replay(ivs: Sequence[tuple[date, float | None]], fut: Sequence[FutureDay]) -> list[Firing]:
    """Every session's shock statistics from the history up to it (no look-ahead), the days a rule turned on (as the
    alert fires: on, after a session off or unknown), and the synthetic book's follow-through for each."""
    iv_day = {d: v for d, v in ivs}
    iv_vals = [v for _, v in ivs]
    iv_pos = {d: i for i, (d, _) in enumerate(ivs)}
    days = [x for x in fut if x.day in iv_day]
    on = dict.fromkeys(SHOCK_RULES, False)
    out: list[Firing] = []
    fpos = {x.day: i for i, x in enumerate(fut)}
    rets = [y.ret for y in fut]
    for j, x in enumerate(days):
        i = iv_pos[x.day]
        hist = iv_vals[max(0, i + 1 - SD_WINDOW - 2 * CUM_SESSIONS) : i + 1]
        k = fpos[x.day]
        stats = {"iv_shock": iv_shock(hist).value, "cum_shock": cum_shock(hist).value,
                 "gap": gap_z(open_=x.open, prev_close=x.prev_close, rets=rets[max(0, k - SD_WINDOW) : k]).value}  # fmt: skip
        new = []
        for r in SHOCK_RULES:
            v = stats[r]
            now_on = v is not None and v >= (GAP_Z if r == "gap" else SHOCK_Z)
            if now_on and not on[r]:
                new.append(r)
            on[r] = now_on
        if not new:
            continue
        loss = None
        if j + FOLLOW_SESSIONS < len(days):
            y = days[j + FOLLOW_SESSIONS]
            iv0, iv1 = iv_day[x.day], iv_day[y.day]
            if x.close and y.close and iv0 and iv1:
                loss = straddle_loss(x.close, iv0, y.close, iv1, (y.day - x.day).days)
        out.append(Firing(x.day, tuple(new), {r: v for r, v in stats.items() if v is not None}, loss,
                          None if loss is None else loss <= FALSE_ALARM_LOSS))  # fmt: skip
    return out


def replay_summary(firings: Sequence[Firing], *, scenario_days: Sequence[date] = SCENARIO_DAYS,
                   episodes: Sequence[tuple[date, date]] = EPISODES) -> dict[str, Any]:  # fmt: skip
    years: dict[str, dict[str, int]] = {}
    for f in firings:
        y = years.setdefault(str(f.day.year), {"firings": 0, "false_alarms": 0, "unscored": 0})
        y["firings"] += 1
        y["false_alarms"] += f.false_alarm is True
        y["unscored"] += f.false_alarm is None
    fired = {f.day for f in firings}
    return {"years": years,
            "scenarios": {d.isoformat(): d in fired for d in scenario_days},
            "episodes": {f"{a.isoformat()}..{b.isoformat()}": any(a <= d <= b for d in fired) for a, b in episodes},
            "max_false_alarms_per_year": MAX_FALSE_ALARMS,
            "within_false_alarm_limit": all(y["false_alarms"] <= MAX_FALSE_ALARMS for y in years.values())}  # fmt: skip
