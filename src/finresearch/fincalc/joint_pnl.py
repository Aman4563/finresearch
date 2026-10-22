"""The daily joint P&L of both books (#397 (b), evals/experiments/joint_risk/PREREG.md): the actual P&L between two
evenings and its explained parts, for the pre-registered forward test (`fincalc.joint_risk.forward_test`). Pure
arithmetic over plain numbers; the caller (portfolio.joint_pnl) reads the stored rows. Analysis only: nothing here
places, modifies or cancels an order, and it imports no personal-data package.

    holdings actual  = (V_d − V_{d−1}) − (I_d − I_{d−1})      canonical value history; I = cumulative net money in
    F&O actual       = Σ_j q_j (P_j,d − P_j,d−1)               fno_mark settles of the legs carried over the session
    holdings own     = Σ_i V_i,d−1 r_i,d                       the previous evening's values × each holding's own return
    holdings β       = D_holdings,d−1 × m                      the previous evening's Σ β_i V_i × NIFTY's move m

Anything unknown leaves its figure None with the reason, never 0. An empty book (known) is exactly 0. A leg present
on only one of the two evenings (opened or closed during the session) leaves the book's P&L unknown: its fill is not
in the marks, so the day's book P&L cannot be split from the trade's cash (a flow).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Mark:
    """One leg's evening mark (fno_mark): signed qty (None = direction unknown), settle price, underlying close, IV
    and T (years)."""

    leg_key: str
    symbol: str
    expiry: date
    right: str
    strike: float | None
    qty: int | None
    price: float | None
    spot: float | None
    iv: float | None
    t_years: float | None


@dataclass(frozen=True)
class SeriesDay:
    """One day of the canonical value history (portfolio.series): value, cumulative net money in, complete."""

    day: date
    value: float
    invested: float
    complete: bool


@dataclass(frozen=True)
class Snap:
    """One holding in an evening's snapshot: its key, value (₹) and 1-year beta to NIFTYBEES (None = unknown)."""

    key: str
    value: float
    beta: float | None


def carried(prev: Sequence[Mark], cur: Sequence[Mark], day: date) -> tuple[list[tuple[Mark, Mark]] | None, list[str]]:
    """The (previous, current) mark of every leg carried over the session ending on `day`. A leg that expired at or
    before the previous close carries nothing. None when the set of legs or a quantity changed."""
    p = {m.leg_key: m for m in prev if m.expiry >= day}
    c = {m.leg_key: m for m in cur}
    if set(p) != set(c) or any(p[k].qty != c[k].qty for k in p):
        return None, ["the F&O legs changed between the two evenings (a trade's fill is not in the marks)"]
    return [(p[k], c[k]) for k in sorted(p)], []


def fno_actual(prev: Sequence[Mark], cur: Sequence[Mark], day: date) -> tuple[float | None, list[str]]:
    pairs, why = carried(prev, cur, day)
    if pairs is None:
        return None, why
    if any(a.qty is None for a, _ in pairs):
        return None, ["a Groww leg's direction (long or short) is unknown"]
    gone = [a.leg_key for a, b in pairs if a.price is None or b.price is None]
    if gone:
        return None, [f"{k}: its settle price is unknown on one of the two evenings" for k in gone]
    return sum(a.qty * (b.price - a.price) for a, b in pairs), []  # type: ignore[operator,misc]


def holdings_actual(prev: SeriesDay | None, cur: SeriesDay | None) -> tuple[float | None, list[str]]:
    if prev is None or cur is None:
        return None, ["the value history has no row for one of the two days"]
    if not (prev.complete and cur.complete):
        return None, ["the value history priced a holding on a stale price on one of the two days"]
    return (cur.value - prev.value) - (cur.invested - prev.invested), []


def own_move(snap: Sequence[Snap], returns: Mapping[str, float | None]) -> tuple[float | None, list[str]]:
    why = [f"{h.key}: its own return that session is unknown" for h in snap if returns.get(h.key) is None]
    if why:
        return None, why
    return sum(h.value * returns[h.key] for h in snap), []  # type: ignore[operator,misc]


def beta_move(d_holdings: float | None, move: float | None) -> float | None:
    return None if d_holdings is None or move is None else d_holdings * move
