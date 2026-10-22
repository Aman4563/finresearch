"""Each F&O underlying's registered beta to NIFTY (fincalc.beta, evals/experiments/book_betas/PREREG.md) from the
futures settles stored in fno_bhav (#394, #395). Reads only: nothing is cached or written, so a GET may call it."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, timedelta

from finresearch.fincalc import beta as fb

# enough calendar days for the registered 250-session window plus its first return's previous session (about 365
# calendar days hold 250 sessions); a shorter stored history simply leaves a shorter window
LOOKBACK_DAYS = 450


def load(underlyings: Iterable[str], today: date) -> dict[str, fb.BetaEstimate]:
    """{UNDERLYING: estimate} for every underlying other than NIFTY (whose beta is 1 by definition)."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav

    want = sorted({u.upper() for u in underlyings} - {"NIFTY"})
    if not want:
        return {}
    futs: dict[str, list[tuple[date, date, float]]] = defaultdict(list)
    lots: dict[str, dict[tuple[date, date], int]] = defaultdict(dict)
    with session_scope() as s:
        for sym, d, exp, settle, lot in s.execute(
            select(FnoBhav.symbol, FnoBhav.day, FnoBhav.expiry, FnoBhav.settle, FnoBhav.lot_size).where(
                FnoBhav.symbol.in_(["NIFTY", *want]), FnoBhav.instrument.in_(("IDF", "STF")),
                FnoBhav.option_type == "XX", FnoBhav.day.between(today - timedelta(days=LOOKBACK_DAYS), today))
        ):  # fmt: skip
            if settle is not None:
                futs[sym].append((d, exp, float(settle)))
            if lot:
                lots[sym][(d, exp)] = lot
    nifty = fb.returns_with_prev(futs.get("NIFTY", []), lots.get("NIFTY"))
    return {u: fb.estimate(u, nifty, fb.returns_with_prev(futs.get(u, []), lots.get(u)), today) for u in want}


def read(underlyings: Iterable[str], today: date) -> dict[str, fb.BetaEstimate]:
    """`load`, with a failed read making each beta unknown with the reason (never 0, never 1)."""
    try:
        return load(underlyings, today)
    except Exception as e:
        return {u: fb.unknown(u, f"the stored futures could not be read ({type(e).__name__})")
                for u in sorted({x.upper() for x in underlyings} - {"NIFTY"})}  # fmt: skip
