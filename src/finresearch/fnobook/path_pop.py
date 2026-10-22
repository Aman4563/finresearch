"""Probabilities that match the trade plan's exits (P3.5, #388): a Monte Carlo of the card's own exit rules.

Every probability the card shows is P(P&L > 0 at expiry), but the plan exits early (a profit target, a stop at a
multiple of the credit, a days-to-expiry exit, the last session before expiry day). Here each simulated path of the
card's expiry forward is run through `fnobook.card_forecasts.simulate`, the card ledger's exit replay (which is
`backtest.engine.manage`), so the exit rules have exactly one implementation: the path only supplies the marks.

Model (registered for the horse race as addendum A6, horserace/addendum_a6.py):
- Paths: the expiry's forward F on the stored session calendar, zero drift (the spot drifts at r, A1.6), lognormal
  with the card's volatility, total variance sigma^2 x t at expiry with t = calendar days / 365 (A1.6's clock), so the
  terminal distribution is the incumbent's and the only new thing is the path.
- Marks: each leg by Black-76 at its origin IV moved by the spot-vol rule sigma_t = iv + beta x ln(F_t / F_0)
  (floored at the solver's lower bound), discounted at r over the leg's remaining calendar time, rounded to the tick;
  the session's contracts are the leg's origin-session contracts (they pick the engine's exit slippage tier).
- Expiry day: settled at the path's forward (the expiring future's settle, as the engine does without option rows).

The smile (P3.3) and the fat-tailed path generator (P3.4) are not merged: both enter later through `leg_price` and
`forward_paths` as new registrations. Pure: nothing here reads or writes the database.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from finresearch.backtest.history import Day, Quote
from finresearch.fincalc import black76 as b76

TICK = 0.05  # NSE's option tick, rupees


@dataclass(frozen=True)
class PathLeg:
    right: str  # call | put
    strike: float
    side: str  # buy | sell
    entry: float  # the fill, rupees per unit
    iv: float  # Black-76 IV of the leg's reference price at the origin close (fraction)
    contracts: int  # traded on the origin session: the slippage tier of every simulated mark


def forward_paths(f0: float, sigma: float, times: np.ndarray, n: int, rng: np.random.Generator) -> np.ndarray:
    """n lognormal zero-drift paths of the forward at `times` (calendar years from the origin, increasing): shape
    (n, len(times)). ln F_t = ln F_0 - sigma^2 t / 2 + sigma W_t."""
    dt = np.diff(np.concatenate(([0.0], np.asarray(times, dtype=float))))
    if np.any(dt <= 0):
        raise ValueError("times must be positive and increasing")
    z = rng.standard_normal((n, len(dt)))
    steps = -0.5 * sigma**2 * dt + sigma * np.sqrt(dt) * z
    return f0 * np.exp(np.cumsum(steps, axis=1))


def leg_price(leg: PathLeg, fwd: float, f0: float, tau: float, rate: float, beta: float) -> float:
    """The leg's Black-76 value at forward `fwd` with `tau` calendar years left, at its origin IV moved by
    beta x ln(fwd / f0) and floored at black76.SIGMA_LO; tau <= 0 gives the intrinsic value."""
    sigma = max(b76.SIGMA_LO, leg.iv + beta * math.log(fwd / f0))
    return b76.price(leg.right, fwd, leg.strike, tau, math.exp(-rate * max(tau, 0.0)), sigma)  # type: ignore[arg-type]


def tick(price: float) -> float:
    """A simulated mark on NSE's tick grid, never below one tick (a traded quote needs a positive close)."""
    return max(TICK, round(round(price / TICK) * TICK, 2))


class PathHistory:
    """One simulated path as a `backtest.history.History` that `card_forecasts.simulate` reads like the bhavcopy.

    `sessions`: the stored sessions after the origin up to the expiry's last session (the exchange calendar, the one
    thing the engine may read ahead); `forwards[i]`: the path's forward at sessions[i]; `price(leg_i, i)`: leg leg_i's
    value at sessions[i]. Each session before the last carries every leg's quote (close = settle = the value on the
    tick grid, the leg's origin contracts); the last carries only the expiring future at the forward, which the engine
    settles at. A sentinel session after the expiry date (no data) makes an exit on the last session final."""

    def __init__(self, inp: dict[str, Any], legs: Sequence[PathLeg], sessions: Sequence[date],
                 forwards: Sequence[float], price: Callable[[int, int], float]) -> None:  # fmt: skip
        self.symbol, self.lot_size = inp["symbol"], inp.get("lot")
        self.expiry, self.origin = date.fromisoformat(inp["expiry"]), date.fromisoformat(inp["entry_day"])
        self.legs, self.price, self.forwards = legs, price, forwards
        self.sessions_ = list(sessions)
        if not self.sessions_ or self.sessions_[0] <= self.origin or self.sessions_[-1] > self.expiry:
            raise ValueError("sessions must run from after the origin to the expiry date")
        self.index = {d: i for i, d in enumerate(self.sessions_)}
        self.calendar = [self.origin, *self.sessions_, self.expiry + timedelta(days=1)]

    def sessions(self, symbol: str, start: date, end: date) -> list[date]:
        return [d for d in self.calendar if start <= d <= end] if symbol == self.symbol else []

    def latest(self, symbol: str) -> date | None:
        return self.calendar[-1] if symbol == self.symbol else None

    def lot(self, symbol: str, expiry: date, d: date) -> int | None:
        return self.lot_size

    def day(self, symbol: str, d: date) -> Day | None:
        i = self.index.get(d)
        if symbol != self.symbol or i is None:
            return None
        x = Day(d, symbol)
        if i == len(self.sessions_) - 1:
            x.futures[self.expiry] = float(self.forwards[i])
            return x
        for j, leg in enumerate(self.legs):
            px = tick(self.price(j, i))
            x.options[(self.expiry, leg.right, leg.strike)] = Quote(self.expiry, leg.right, leg.strike, px, px,
                                                                    leg.contracts, 0)  # fmt: skip
        return x
