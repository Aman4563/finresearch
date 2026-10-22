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
from datetime import date, datetime

from finresearch.fincalc import book_scenarios as bs

RATE = 0.065  # fnobook.marks.RATE: the rate fno_mark's IVs were implied at; every revaluation here uses it


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


def carried(
    prev: Sequence[Mark], cur: Sequence[Mark], day: date
) -> tuple[list[tuple[Mark, Mark]] | None, list[str]]:
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


# --------------------------------------------------------------------------- the book's explained P&L
def fno_model(
    legs: Sequence[bs.BookLeg], m: bs.Market, at: datetime, move: float
) -> tuple[float | None, list[str]]:
    """The joint view's model of the session (L(m)'s F&O term at the realised NIFTY move): the previous evening's legs
    fully revalued at `at` (one session on) with each underlying at β × move (the worse end of its band), the
    spot-vol response and no extra vol shift (book_scenarios.shock_pnls at one move). [] = exactly 0."""
    if not legs:
        return 0.0, []
    c = bs.revalue(legs, m, move, 0.0, at, response=True, horizon="shock")
    return c.pnl, list(c.reasons)


def fno_market(prev: Sequence[Mark], cur: Sequence[Mark], day: date) -> tuple[float | None, list[str]]:
    """The previous evening's legs repriced at the day's own spot and implied IVs (posttrade.revaluation per
    underlying). The marks' IVs are implied from the settles, so this reproduces the actual up to rounding: kept
    beside the model as a check of the marks, never as the test's explained figure (ADDENDUM.md)."""
    from finresearch.fincalc import posttrade as pt

    pairs, why = carried(prev, cur, day)
    if pairs is None:
        return None, why
    if any(a.qty is None for a, _ in pairs):
        return None, ["a Groww leg's direction (long or short) is unknown"]
    groups: dict[str, list[tuple[Mark, Mark]]] = {}
    for a, b in pairs:
        groups.setdefault(a.symbol, []).append((a, b))
    total = 0.0
    for sym, g in sorted(groups.items()):
        a0, b0 = g[0]
        if None in (a0.spot, b0.spot, a0.t_years, b0.t_years):
            return None, [f"{sym}: its close or T is unknown on one of the two evenings"]
        legs = [pt.MarkLeg(a.right, a.strike or 0.0, a.qty, a.iv, b.iv) for a, b in g]  # type: ignore[arg-type]
        r = pt.revaluation(legs, s0=a0.spot, s1=b0.spot, t0=a0.t_years, t1=b0.t_years, rate=RATE, actual=0.0)  # type: ignore[arg-type]
        if r is None:
            return None, [f"{sym}: a leg's implied volatility is unknown"]
        total += r.model
    return total, []


# --------------------------------------------------------------------------- one day
Part = tuple[float | None, Sequence[str]]


@dataclass(frozen=True)
class Day:
    """One session's joint P&L. `usable` = NIFTY's move, the actual (holdings + F&O) and the explained (holdings' own
    move + the model) are all known; `reasons` say why not. The side figures (`holdings_beta`, `fno_market`) are
    reported beside the test and never decide usability (their reasons go to `notes`)."""

    day: date
    prev_day: date
    nifty_move: float | None
    holdings_actual: float | None
    fno_actual: float | None
    actual: float | None
    holdings_own: float | None
    fno_model: float | None
    explained: float | None
    holdings_beta: float | None
    fno_market: float | None
    usable: bool
    reasons: tuple[str, ...]
    notes: tuple[str, ...]


def _add(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


def day(d: date, prev: date, *, nifty_move: float | None, holdings: Part, fno: Part, own: Part,
        beta: float | None, model: Part, market: Part) -> Day:  # fmt: skip
    reasons = [] if nifty_move is not None else ["NIFTY's close on one of the two days is not stored"]
    for _, why in (holdings, fno, own, model):
        reasons += [w for w in why if w not in reasons]
    actual, explained = _add(holdings[0], fno[0]), _add(own[0], model[0])
    usable = nifty_move is not None and actual is not None and explained is not None
    return Day(d, prev, nifty_move, holdings[0], fno[0], actual, own[0], model[0], explained, beta, market[0],
               usable, tuple(reasons), tuple(market[1]))  # fmt: skip
