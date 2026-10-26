"""The common-shock inputs of the replay on a stored day (#395 Stage 2): the open book's and one lot's P&L at each
registered NIFTY move, from this close to the next stored session's close (fincalc.book_scenarios.shock_pnls: full
revaluation, the registered spot-vol response, other underlyings at the worse end of their beta band). Legs are at
their marks (the day's reference prices); forwards are the engine's (backtest.pricing.forward at the Book's rate);
every expiry is discounted and every forward rolled at that one rate (Market.fixed_carry, this study's registered
rule: #480's stored carry needs the underlying's close, which the legacy 2022-24 bhavcopy does not carry). Betas are
the registered estimator's as of the session, from the front-future settles the replay has read so far."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta

from finresearch.backtest import engine
from finresearch.backtest import pricing as px
from finresearch.backtest.history import Day
from finresearch.fincalc import beta as fb
from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc.dates import session_clock

RULE = "riskbudget/specs.py: every forward rolled and every expiry discounted at the Book's rate"
LOOKBACK_DAYS = (
    450  # rows kept for the estimate: the registered 250-session window is about 365 calendar days
)


def legs(underlying: str, expiry: date, signed: Sequence[tuple[str, float, int]], x: Day) -> list[bs.BookLeg]:
    """Legs (right, strike, signed units) of one expiry at their marks on `x` (unknown mark: None, never 0)."""
    out = []
    for right, k, q in signed:
        quote = x.options.get((expiry, right, k))
        out.append(
            bs.BookLeg(underlying.upper(), expiry, right, k, q, None if quote is None else quote.ref, "ltp")
        )  # type: ignore[arg-type]
    return out


def position_legs(positions: Sequence[engine.Position]) -> list[bs.BookLeg]:
    """The open positions' legs at their last marks (the engine keeps a stale mark when a leg has no row)."""
    return [bs.BookLeg(p.symbol.upper(), p.expiry, lg.right, lg.strike, lg.sign * lg.qty, lg.mark, "ltp")  # type: ignore[arg-type]
            for p in positions for lg in p.legs]  # fmt: skip


def pnls(book: Sequence[bs.BookLeg], one: Sequence[bs.BookLeg], days: Mapping[str, Day | None], d: date,
         nxt: date, betas: Mapping[str, fb.BetaEstimate], moves: Sequence[float],
         rate: float) -> tuple[dict[float, float] | None, dict[float, float] | None, tuple[str, ...]]:  # fmt: skip
    """(L_book, L_1, why): each P&L at `moves` (None = unknown, with why). An empty book loses nothing."""
    keys = sorted({(x.underlying, x.expiry) for x in (*book, *one)})
    fwd = {}
    for u, e in keys:
        x = days.get(u)
        fwd[(u, e)] = px.forward(x, e, rate) if x is not None else None
    m = bs.Market(session_clock(d), rate, fwd, {k.upper(): v for k, v in betas.items()}).fixed_carry(
        rate, RULE
    )
    at = session_clock(nxt)
    l_book, why_b = (dict.fromkeys(moves, 0.0), ()) if not book else bs.shock_pnls(book, m, at, moves)
    l_one, why_1 = bs.shock_pnls(one, m, at, moves)
    return l_book, l_one, (*why_b, *why_1)


def beta_as_of(
    futs: Mapping[str, Sequence[tuple[date, date, float]]], underlying: str, d: date
) -> fb.BetaEstimate:
    """The registered estimate (fincalc.beta, book-beta-v1) of `underlying`'s beta to NIFTY as of `d` from the
    (day, expiry, settle) futures rows read so far; rows after `d` are never read. No lot adjustment: an index's lot
    revisions do not move its price, so the roll rule's lot factor cannot apply."""
    lo = d - timedelta(days=LOOKBACK_DAYS)
    pick = lambda u: [r for r in futs.get(u, ()) if lo <= r[0] <= d]  # noqa: E731
    return fb.estimate(
        underlying, fb.returns_with_prev(pick("NIFTY")), fb.returns_with_prev(pick(underlying)), d
    )
