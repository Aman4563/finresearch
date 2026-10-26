"""One candidate's lots under a book's limits (#395 Stage 2), from the one sizing formula: fincalc.tradeplan.size (the
trade budget, B_book over the open risk, and the margin cap on real money) and tradeplan.shock_lots at the arm's
registered moves (called beside size(), whose own shock term always takes every card move), after the month stop
(tradeplan.OpenBook.month_stopped) and, for the card rule, the Kelly ceiling's zero. Pure; the replay gathers the
inputs. Unknown is never room: an unknown term sizes 0, and the binding term is named for the report."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from finresearch.backtest.specs import BOOK, CRITERIA, Book
from finresearch.fincalc import tradeplan as tp
from finresearch.riskbudget import specs

MONTH = "month stop (L_month): the month-to-date loss is at or beyond the stop"
BOOK_FULL, BOOK_UNKNOWN = "book budget (B_book): no room", "book budget (B_book): open risk unknown"
MARGIN_FULL, MARGIN_UNKNOWN = "margin cap (u_max): no room", "margin cap (u_max): margin unknown"
SHOCK_FULL, SHOCK_UNKNOWN = "common shock (B_shock): no room", "common shock (B_shock): unknown"
KELLY_LOW, KELLY_UNKNOWN = "Kelly ceiling: mu_lo <= 0", "Kelly ceiling: mu_lo unknown"


@dataclass(frozen=True)
class Sized:
    lots: int
    binding: (
        str | None
    )  # month | book | margin | shock | kelly: the term that cut the trade size (None: none did)
    reason: str  # the counted reason (no prices or dates); "" when nothing cut


def limit_lots(*, ml_one: float, costs_one: float, open_risk: float | None, margin_used: float | None,
               im_one: float | None, l_book: Mapping[float, float | None] | None,
               l_one: Mapping[float, float | None] | None, mtd: float | None,
               moves: Sequence[float] = specs.PRIMARY_MOVES, kelly: bool = False, mu_lo: float | None = None,
               book: Book = BOOK) -> Sized:  # fmt: skip
    """The lots of one set (lot) of a candidate: max loss `ml_one` and entry costs `costs_one`; the book's open risk,
    margin in use (the open entry margins), one lot's margin, the book's and one lot's P&L at each of `moves`, and the
    month-to-date P&L. The book is all cash: cash = capital, no collateral."""
    cap = book.capital
    ob = tp.OpenBook(capital=cap, open_risk=open_risk, mtd_paper=mtd,
                     funds=tp.Funds(cap, margin_used, 0.0, im_one))  # fmt: skip
    if ob.month_stopped():
        return Sized(0, "month", MONTH)
    s = tp.size(ml_one, costs_one, cap * book.max_loss_pct / 100, True, book.max_lots, ob)
    sn, _ = tp.shock_lots(l_book, l_one, ob.shock_budget, moves)
    stages: list[tuple[str, float, str]] = [
        ("book", s.suggested, BOOK_UNKNOWN if open_risk is None else BOOK_FULL),
        ("margin", s.real_lots, MARGIN_UNKNOWN if margin_used is None or im_one is None else MARGIN_FULL),
        ("shock", 0 if sn is None else sn, SHOCK_UNKNOWN if sn is None else SHOCK_FULL),
    ]
    if kelly:
        stages.append(("kelly", 0 if mu_lo is None or mu_lo <= 0 else math.inf,
                       KELLY_UNKNOWN if mu_lo is None else KELLY_LOW))  # fmt: skip
    lots, binding, why = s.multiple, None, ""
    for name, room, reason in stages:
        if room < lots:
            lots, binding, why = int(room), name, reason
    return Sized(lots, binding, why)


def kelly_mu_lo(trades: Sequence[Mapping[str, Any]]) -> float | None:
    """μ_lo for the card's Kelly ceiling: the lower end of the 95 % expiry-clustered bootstrap CI of the net ₹ per
    trade (backtest.metrics.cluster_bootstrap with the backtest's registered draws and seed, as the card's evidence
    row has it); None below specs.KELLY_MIN_TRADES trades."""
    from finresearch.backtest import metrics

    if len(trades) < specs.KELLY_MIN_TRADES:
        return None
    ci = metrics.cluster_bootstrap([float(t["net"]) for t in trades], [t["expiry"] for t in trades],
                                   CRITERIA.bootstrap_draws, CRITERIA.bootstrap_seed)  # fmt: skip
    return None if ci is None else ci[0]
