"""One risk view across the investment portfolio and the F&O book (#397). Pure arithmetic; the caller gathers each
holding's value and beta and the book's legs and market (fincalc.book_scenarios). Analysis only: nothing here places,
modifies or cancels an order, and it imports no personal-data package (the caller passes plain numbers).

    D_total = Σ_i β_i V_i  +  Σ_j β_u(j) Δ_j F_j q_j          (₹ per unit relative NIFTY move; × 0.01 per 1 %)
    L(m)    = Σ_i β_i V_i m  +  PnL_FnO(m, dσ(m))              (one session from now)

The holdings' β_i are each holding's 1-year beta to NIFTYBEES (portfolio.analytics, MIN_BETA days); the book's β_u are
the registered betas of each underlying's front future to NIFTY's (fincalc.beta; NIFTY = 1), and PnL_FnO is the book's
full revaluation (book_scenarios.shock_pnls: every leg repriced, the spot-vol response, each underlying at the worse end
of its beta band). The two betas are against different series (an ETF's closes and the index's front future): close,
not the same benchmark, and said so wherever the figures are shown.

A holding without a beta, an unpriced leg, an unknown underlying beta or an unread book leaves the combined figure
None with the reasons, never 0. An empty book (nothing open, and that is known) contributes exactly 0.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from finresearch.fincalc import book_scenarios as bs

HEDGE_SHARE = 0.25  # #397: the costed hedge calculator is built only once F&O is >= 25 % of the L(-10 %) loss
HEDGE_MOVE = -0.10
# the registered shock moves (fincalc.tradeplan.SHOCK_MOVES: -3, -5, -10, +5 %) plus a Feb-Mar 2026-sized fall
MOVES = (-0.15, -0.10, -0.05, -0.03, 0.05)
BETA_NOTE = ("Holdings use each holding's 1-year beta to NIFTYBEES (the Nifty 50 ETF's closes); F&O underlyings use "
             "their registered beta to NIFTY's front future (NIFTY = 1). Close, but not the same benchmark.")  # fmt: skip


@dataclass(frozen=True)
class Holding:
    """One holding: today's value (₹), its beta to NIFTYBEES (None = unknown) and how the beta was found (or why it
    is unknown)."""

    key: str
    name: str
    value: float
    beta: float | None
    how: str


@dataclass(frozen=True)
class Book:
    """The open F&O book: its legs (None = unknown, with `reason`; [] = nothing open) and the market they price on."""

    legs: Sequence[bs.BookLeg] | None
    market: bs.Market
    reason: str | None = None


def _holdings(hs: Sequence[Holding], factor: float) -> tuple[float | None, list[str]]:
    """Σ β_i V_i × factor over holdings with a value, or None with each beta-less holding named."""
    why = [f"{h.name}: no beta ({h.how})" for h in hs if h.value > 0 and h.beta is None]
    if why:
        return None, why
    return sum(h.beta * h.value * factor for h in hs if h.value > 0), []  # type: ignore[operator]


def _book_reason(b: Book) -> list[str]:
    return [] if b.legs is not None else [b.reason or "the F&O book could not be read: its risk is unknown"]


def _sum(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


@dataclass(frozen=True)
class Delta:
    """D_total and its two terms in ₹ per unit relative NIFTY move; None = unknown (`reasons`)."""

    holdings: float | None
    fno: float | None
    total: float | None
    reasons: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.total is not None


def d_total(hs: Sequence[Holding], b: Book) -> Delta:
    hv, reasons = _holdings(hs, 1.0)
    fno: float | None
    if b.legs is None:
        fno, reasons = None, reasons + _book_reason(b)
    elif not b.legs:
        fno = 0.0
    else:
        ex = bs.exposures(b.legs, b.market)
        fno = ex.nifty_rs_delta
        if fno is None:
            reasons += [r for r in ex.nifty_delta_reasons if r != "NIFTY's level is unknown"]
    return Delta(hv, fno, _sum(hv, fno), tuple(reasons))


@dataclass(frozen=True)
class Loss:
    """L(m): the holdings' and the book's P&L (₹) at a NIFTY move m one session from now, their sum, and F&O's share
    of the joint loss (None when the joint figure is unknown or not a loss: `share_reason`)."""

    move: float
    holdings: float | None
    fno: float | None
    total: float | None
    reasons: tuple[str, ...]
    fno_share: float | None
    share_reason: str | None


def losses(hs: Sequence[Holding], b: Book, at: datetime, moves: Sequence[float] = MOVES) -> list[Loss]:
    """L(m) for each move. `at` is the horizon clock (book_scenarios.horizon_clock(now, 1, holidays))."""
    fno: dict[float, float] | None
    if b.legs is None:
        fno, freasons = None, _book_reason(b)
    elif not b.legs:
        fno, freasons = dict.fromkeys(moves, 0.0), []
    else:
        fno, fr = bs.shock_pnls(b.legs, b.market, at, moves)
        freasons = list(fr)
    out = []
    for m in moves:
        hv, hreasons = _holdings(hs, m)
        f = None if fno is None else fno[m]
        tot = _sum(hv, f)
        share, why = None, None
        if tot is None:
            why = "the joint figure is unknown"
        elif tot >= 0:
            why = "the joint figure is not a loss at this move"
        else:
            share = f / tot  # type: ignore[operator]
        out.append(Loss(m, hv, f, tot, tuple(hreasons + freasons), share, why))
    return out


@dataclass(frozen=True)
class HedgeTrigger:
    """Whether F&O's share of the L(-10 %) loss reaches HEDGE_SHARE (#397's trigger for the costed hedge calculator);
    `fired` None = unknown."""

    share: float | None
    fired: bool | None
    threshold: float
    reasons: tuple[str, ...]


def hedge_trigger(ls: Sequence[Loss], move: float = HEDGE_MOVE) -> HedgeTrigger:
    lo = next((x for x in ls if x.move == move), None)
    if lo is None:
        return HedgeTrigger(None, None, HEDGE_SHARE, (f"L({move:+.0%}) was not computed",))
    if lo.total is None:
        return HedgeTrigger(None, None, HEDGE_SHARE, lo.reasons)
    if lo.fno_share is None:  # no joint loss at -10 %: F&O is no share of it
        return HedgeTrigger(0.0, False, HEDGE_SHARE, ())
    return HedgeTrigger(lo.fno_share, lo.fno_share >= HEDGE_SHARE, HEDGE_SHARE, ())
