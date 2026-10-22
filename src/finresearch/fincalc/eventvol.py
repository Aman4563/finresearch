"""Event volatility (P3.8, #391): the implied move of a scheduled event from the option term structure, and the
history of realised event moves. Pure, no I/O.

Total implied variance to expiry j is w_j = sigma_j^2 x tau_j, with tau on a clock that holds no event variance (the
business clock with events EXCLUDED, or the calendar clock; the caller states which). The model is

    w(tau_j) = sigma_d^2 tau_j + sum_e J_e^2 x 1{T_j >= e}

* Two expiries both after the event: J^2 = (w1 tau2 - w2 tau1) / (tau2 - tau1) = (sigma1^2 - sigma2^2) / (1/tau1 -
  1/tau2), base variance sigma_d^2 = (w2 - w1) / (tau2 - tau1).
* One expiry before the event and one after (T1 < e <= T2): J^2 = tau2 x (sigma2^2 - sigma1^2).
* Three or more expiries: least squares with every J_e^2 >= 0 (`fit`).

J^2 is reported only when positive. When the term structure does not show the event (sigma1 <= sigma2 for two
expiries after it, a missing expiry, events between the same two expiries) the move is unknown with the reason,
never a zero move. The one-sigma move is J; the expected |move| of a normal move is sqrt(2/pi) J (about 0.80 J) and
its median 0.6745 J. Display only: nothing here feeds the card's probabilities (any forecast use needs its own
registration).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import NormalDist

MEAN_ABS = math.sqrt(2 / math.pi)  # E|Z| for a standard normal Z
MEDIAN_ABS = NormalDist().inv_cdf(0.75)  # median |Z| = 0.6745


@dataclass(frozen=True)
class Point:
    """One expiry's at-the-money implied volatility (annual, a fraction) and its time to expiry in years."""

    expiry: date
    tau: float | None
    sigma: float | None


@dataclass(frozen=True)
class Implied:
    j2: float | None  # the event's variance (a fraction squared, not annualised); None = unknown (see reason)
    base_var: float | None = None  # sigma_d^2, annual
    method: str = ""
    reason: str | None = None

    @property
    def move(self) -> float | None:
        """The implied one-sigma event move (a log-return fraction)."""
        return None if self.j2 is None else math.sqrt(self.j2)

    @property
    def expected_abs(self) -> float | None:
        return None if self.j2 is None else MEAN_ABS * math.sqrt(self.j2)

    @property
    def median_abs(self) -> float | None:
        return None if self.j2 is None else MEDIAN_ABS * math.sqrt(self.j2)


def _usable(*ps: Point) -> str | None:
    for p in ps:
        if p.sigma is None or p.tau is None or not math.isfinite(p.sigma) or not math.isfinite(p.tau):
            return f"no implied volatility for the {p.expiry} expiry"
        if p.sigma <= 0 or p.tau <= 0:
            return f"the {p.expiry} expiry has no time or volatility left"
    if len({p.expiry for p in ps}) < len(ps) or len({p.tau for p in ps}) < len(ps):
        return "the expiries are not distinct: no term structure"
    return None


def two_after(near: Point, far: Point) -> Implied:
    """Both expiries after the event (near before far)."""
    if bad := _usable(near, far):
        return Implied(None, method="two_after", reason=bad)
    w1, w2 = near.sigma**2 * near.tau, far.sigma**2 * far.tau  # type: ignore[operator]
    base = (w2 - w1) / (far.tau - near.tau)  # type: ignore[operator]
    gap = near.sigma**2 - far.sigma**2  # type: ignore[operator]  # the sign test is exact on the IVs themselves
    j2 = gap / (1 / near.tau - 1 / far.tau)  # type: ignore[operator]
    if gap <= 0 or base <= 0:
        return Implied(None, method="two_after",
                       reason="the event cannot be identified from these expiries: the near IV is not above the "
                              "far IV by enough to imply an event premium")  # fmt: skip
    return Implied(j2, base, "two_after")


def straddling(before: Point, after: Point) -> Implied:
    """One expiry before the event and one after it: the earlier expiry's variance is all base variance."""
    if bad := _usable(before, after):
        return Implied(None, method="straddling", reason=bad)
    base = before.sigma**2  # type: ignore[operator]
    gap = after.sigma**2 - base  # type: ignore[operator]
    j2 = after.tau * gap  # type: ignore[operator]
    if gap <= 0:
        return Implied(None, base, "straddling",
                       "the event cannot be identified: the expiry after it is not priced above the one before")  # fmt: skip
    return Implied(j2, base, "straddling")
