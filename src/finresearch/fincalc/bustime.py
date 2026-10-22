"""Business-time clock for option variance (P3.1, #384): sessions, overnight gaps, weekend and holiday days and event
days each carry their own share of variance, instead of every calendar day weighing 1/365.

    T_bus = [tau_today + n_s + w_o n_o + w_w n_w + sum_e J_e] / Y,     Y = 252 + 252 w_o + 113 w_w

in units of one open-to-close session's variance. n_s = full sessions after now up to and including the expiry
session; n_o = overnight gaps ahead (one before each session's open, so n_o = n_s, plus today's own gap before the
open); n_w = weekend and holiday calendar days; J_e = an event day's extra variance. tau_today = the rest of today's
session, linear in minutes (09:15-15:30 IST = 375 minutes, NSE equity derivatives trading hours; the issue's
optional intraday U-shape is not built). Y makes a year of 252 sessions, 252 overnights and 113 non-trading days
equal 1 (the issue writes 251 overnights; 252 keeps a full calendar year exactly 1), so a volatility annualised over
close-to-close returns x sqrt(252) is already sigma per business-time year.

Variance accrues as a lump at the open for an overnight gap and evenly over a non-trading calendar day. An event's
J_e counts until the close of its India day (its time of day is not modelled).

Unknown stays unknown: no holiday calendar covering [today, expiry], an event calendar not covering the expiry, or an
expiry day that is not a session gives value None with the reason, never "no holiday" or "no event". Pure, no I/O
except `nse_calendar`, which reads the cached NSE holiday lists.

This is a library and a horse-race challenger (horserace/addendum_a2.py); the live card keeps its calendar clock
(fincalc.dates.years_to_expiry) until the race adopts something.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

from finresearch.fincalc.dates import IST, ist_datetime, to_ist

SESSION_OPEN, SESSION_CLOSE = (
    (9, 15),
    (15, 30),
)  # NSE equity derivatives trading hours (as fincalc.dates.EXPIRY_CLOSE)
SESSION_MINUTES = 375
SESSIONS_PER_YEAR, OVERNIGHTS_PER_YEAR, NONTRADING_PER_YEAR = 252, 252, 113  # 252 + 113 = 365 calendar days


@dataclass(frozen=True)
class Weights:
    overnight: float  # w_o: one close-to-open gap's variance / one open-to-close session's
    nontrading: float  # w_w: one weekend or holiday calendar day's variance / one open-to-close session's
    source: str = ""


# The registered prior (horserace addendum A2, #384): w_o = 0, so a "session" is a whole close-to-close trading day
# (its overnight gap folded in), and each weekend or holiday day weighs 0.10 of one. The issue's registered fallback
# is 0.1-0.3 of an open-to-close session; 0.10 of a close-to-close day is 0.10 x (1 + w_o) of a session, i.e. 0.10-0.14
# for any w_o in 0-0.4, inside it. Consistent with (not estimated from) US evidence that a whole weekend's
# close-to-close variance is only about 10-20 % above one weekday's, i.e. 0.05-0.10 of a day per weekend day:
# French and Roll (1986), JFE 17, 5-26, as summarised by Fortune (1998), "Weekends Can Be Rough", Federal Reserve
# Bank of Boston WP 98-6, pp. 1 and 3. Not tuned on any FinResearch data.
PRIOR: Final = Weights(
    overnight=0.0, nontrading=0.10, source="registered prior, horserace addendum A2 (#384)"
)


@dataclass(frozen=True)
class Calendar:
    """Trading days are the weekdays in [known_from, known_through] not in `holidays`; outside that range unknown."""

    known_from: date
    known_through: date
    holidays: frozenset[date] = field(default_factory=frozenset)

    def covers(self, a: date, b: date) -> bool:
        return self.known_from <= a and b <= self.known_through

    def is_session(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    @classmethod
    def from_sessions(cls, sessions: Iterable[date]) -> Calendar | None:
        """From a list of sessions known to be complete (the replay's stored sessions): every weekday between the
        first and the last that is not listed is a holiday. A missing stored day cannot be told from a holiday."""
        s = sorted(set(sessions))
        if not s:
            return None
        n = (s[-1] - s[0]).days
        listed = set(s)
        hol = frozenset(
            d for i in range(n + 1) if (d := s[0] + timedelta(days=i)).weekday() < 5 and d not in listed
        )
        return cls(s[0], s[-1], hol)


@dataclass(frozen=True)
class EventVariance:
    """Event days' extra variance J_e (session units) on their India day; complete through `covered_through`."""

    covered_through: date
    extra: Mapping[date, float] = field(default_factory=dict)


class _Excluded:
    def __repr__(self) -> str:
        return "EXCLUDED"


EXCLUDED: Final = _Excluded()  # the caller's registration leaves event variance out (P3.8): not "no events"


@dataclass(frozen=True)
class BusinessT:
    value: float | None  # years of business time; None = unknown (see reason)
    reason: str | None = None
    today: float = (
        0.0  # session units left today (gap + session before the open, the session's rest during it)
    )
    sessions: int = 0
    overnights: int = 0
    nontrading: float = 0.0  # non-trading calendar days ahead (today's remaining fraction included)
    events: float = 0.0


def units_per_year(w: Weights) -> float:
    return SESSIONS_PER_YEAR + OVERNIGHTS_PER_YEAR * w.overnight + NONTRADING_PER_YEAR * w.nontrading


def business_years(expiry: date, now: datetime, calendar: Calendar | None, w: Weights,
                   *, events: EventVariance | _Excluded | None) -> BusinessT:  # fmt: skip
    """T_bus from `now` (aware) to 15:30 IST on `expiry`. `events`: EXCLUDED when the caller's registration leaves
    event variance out; None (the event calendar could not be read) is unknown."""
    now = to_ist(now)
    today, end = now.date(), ist_datetime(expiry, *SESSION_CLOSE)
    if now > end:
        return BusinessT(None, "expired: now is after 15:30 IST on the expiry day")
    if calendar is None or not calendar.covers(today, expiry):
        return BusinessT(None, f"NSE holiday list unknown for part of {today} to {expiry}")
    if not calendar.is_session(expiry):
        return BusinessT(None, f"expiry day {expiry} is not a session (NSE moves such an expiry)")
    if events is None:
        return BusinessT(None, "event calendar unknown")
    if isinstance(events, EventVariance) and events.covered_through < expiry:
        return BusinessT(None, f"event calendar covers only through {events.covered_through}, not {expiry}")

    t_open, t_close = ist_datetime(today, *SESSION_OPEN), ist_datetime(today, *SESSION_CLOSE)
    tau, nontrading = 0.0, 0.0
    if calendar.is_session(today):
        if now < t_open:
            tau = 1 + w.overnight
        elif now < t_close:
            tau = (t_close - now).total_seconds() / 60 / SESSION_MINUTES
    else:
        midnight = datetime(today.year, today.month, today.day, tzinfo=IST) + timedelta(days=1)
        nontrading = (midnight - now).total_seconds() / 86400
    sessions = 0
    for i in range(1, (expiry - today).days + 1):
        if calendar.is_session(today + timedelta(days=i)):
            sessions += 1
        else:
            nontrading += 1
    ev = 0.0
    if isinstance(events, EventVariance):
        ev = sum(j for d, j in events.extra.items() if today < d <= expiry or (d == today and now < t_close))
    units = tau + sessions + w.overnight * sessions + w.nontrading * nontrading + ev
    return BusinessT(units / units_per_year(w), None, tau, sessions, sessions, nontrading, ev)


def rescale_vol(sigma: float | None, t_from: float | None, t_to: float | None) -> float | None:
    """The volatility that gives the same total variance on another clock: sigma x sqrt(t_from / t_to). The issue's
    quoted business vol is rescale_vol(sigma_cal, T_cal, T_bus); a business-time sigma priced on the calendar clock
    is rescale_vol(sigma_bus, T_bus, T_cal). None when any input is unknown or not positive."""
    if sigma is None or t_from is None or t_to is None or sigma <= 0 or t_from <= 0 or t_to <= 0:
        return None
    return sigma * (t_from / t_to) ** 0.5


def nse_calendar(start: date, end: date, state_dir: Path | None = None) -> Calendar | None:
    """The cached NSE trading-holiday lists for every year from `start` to `end` (adapters.nse_holidays); None when
    any of those years is not cached (NSE publishes only the current year's list)."""
    from finresearch.adapters.nse_holidays import known_trading_holidays

    hol: set[date] = set()
    for y in range(start.year, end.year + 1):
        got = known_trading_holidays(date(y, 1, 1), state_dir)
        if got is None:
            return None
        hol |= got
    return Calendar(date(start.year, 1, 1), date(end.year, 12, 31), frozenset(hol))


# --------------------------------------------------------------------------- estimators (not run here)
# The issue's estimates on 2022-01-03 to 2024-06-30 only: the owner's one-shot read of the stored front-future OHLC
# (fno_bhav open/close, within one contract, never across a roll). Their output would be registered as a new horse
# race addendum before any later window is read; nothing in the code calls them on data.
def _var(xs: Collection[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return sum((x - m) ** 2 for x in xs) / (n - 1)


def overnight_weight(gaps: Collection[float], sessions: Collection[float]) -> float | None:
    """w_o = Var(ln open / previous close) / Var(ln close / open), sample variances; the gaps should exclude those
    spanning a weekend or holiday (those carry w_w too). None with fewer than 2 of either or no session variance."""
    vg, vs = _var(gaps), _var(sessions)
    return None if vg is None or not vs else vg / vs


def nontrading_weight(pairs: Collection[tuple[float, int]]) -> float | None:
    """Weight of one non-trading calendar day in units of one close-to-close trading day: OLS of r^2 on k (the
    non-trading days a close-to-close return spans), b / a. Multiply by (1 + w_o) for open-to-close session units.
    None when k does not vary or the intercept is not positive."""
    n = len(pairs)
    if n < 2:
        return None
    ks = [float(k) for _, k in pairs]
    ys = [r * r for r, _ in pairs]
    mk, my = sum(ks) / n, sum(ys) / n
    sxx = sum((k - mk) ** 2 for k in ks)
    if not sxx:
        return None
    b = sum((k - mk) * (y - my) for k, y in zip(ks, ys, strict=True)) / sxx
    a = my - b * mk
    return b / a if a > 0 else None
