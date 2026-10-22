"""The volatility race's sessions from the stored bhavcopy (P3.7, #390; evals/experiments/vol_forecast_race/PREREG.md).
Reads only: fno_bhav's index futures and options of one underlying. Refuses to read before the registration is
intact (volrace/prereg.py) and refuses the 2008-2021 vault (read only through vault.guard, #364).

Bars: each stored session's front future is its nearest expiry on or after the session, with the previous close of
the SAME contract on the immediately previous stored session (none: unknown, counted). Implied term: the ATM Black-76
IV of every expiry fincalc.fno_history.day_summary can fit from that session's market settles (as fnohist.derive).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from finresearch.db.models import FnoBhav
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import realvol as rv
from finresearch.fnohist.derive import RIGHTS
from finresearch.vault.registry import VAULT_END, VaultSealed
from finresearch.volrace import prereg, race

CHUNK_DAYS = 31  # option rows are read a month at a time


def _f(x) -> float | None:
    return None if x is None else float(x)


def load_sessions(
    s: DbSession, symbol: str, start: date, end: date
) -> tuple[list[race.Session], dict[str, int]]:
    """The race's sessions of `symbol` from `start` to `end` and the counts of what was unknown."""
    prereg.require_registered()
    if start <= VAULT_END:
        raise VaultSealed(f"{start} is in the 2008-2021 vault: the race reads it only through "
                          "vault.guard.vault_read('vol_forecast_race', ...) (#364)")  # fmt: skip
    futs: dict[date, dict[date, tuple]] = defaultdict(dict)
    for d, e, o, h, lo, c, st in s.execute(
        select(FnoBhav.day, FnoBhav.expiry, FnoBhav.open, FnoBhav.high, FnoBhav.low, FnoBhav.close,
               FnoBhav.settle).where(
            FnoBhav.symbol == symbol, FnoBhav.option_type == "XX", FnoBhav.day.between(start, end))
    ):  # fmt: skip
        futs[d][e] = (_f(o), _f(h), _f(lo), _f(c), _f(st))
    days = sorted(futs)
    counts = {"no_prev_close": 0, "no_iv": 0, "invalid_bar": 0}
    out: list[race.Session] = []
    terms = _terms(s, symbol, days, futs)
    for i, d in enumerate(days):
        live = [e for e in futs[d] if e >= d]
        if not live:
            continue
        e = min(live)
        o, h, lo, c, _ = futs[d][e]
        prev = futs[days[i - 1]].get(e) if i > 0 else None
        pc = prev[3] if prev else None
        bar = rv.Bar(d, o, h, lo, c, pc)
        counts["no_prev_close"] += pc is None
        counts["invalid_bar"] += rv.bar_problem(bar) is not None
        term = terms.get(d, ())
        counts["no_iv"] += not term
        out.append(race.Session(d, bar, term))
    return out, counts


def _terms(s: DbSession, symbol: str, days: list[date], futs: dict) -> dict[date, tuple]:
    out: dict[date, tuple] = {}
    if not days:
        return out
    lo = days[0]
    while lo <= days[-1]:
        hi = lo + timedelta(CHUNK_DAYS)
        opts: dict[date, dict[date, list[fh.OptQuote]]] = defaultdict(lambda: defaultdict(list))
        spot: dict[date, float] = {}
        for d, e, k, right, settle, close, contracts, und in s.execute(
            select(FnoBhav.day, FnoBhav.expiry, FnoBhav.strike, FnoBhav.option_type, FnoBhav.settle, FnoBhav.close,
                   FnoBhav.contracts, FnoBhav.underlying)
            .where(FnoBhav.symbol == symbol, FnoBhav.option_type.in_(tuple(RIGHTS)), FnoBhav.day >= lo,
                   FnoBhav.day < hi)
        ):  # fmt: skip
            if und is not None:
                spot[d] = float(und)
            if settle is not None and settle > 0 and fh.market_settle(float(settle), _f(close), contracts):
                opts[d][e].append(fh.OptQuote(float(k), RIGHTS[right], float(settle)))
        for d, by_exp in opts.items():
            fwd = {
                e: v[4] for e, v in futs.get(d, {}).items() if v[4] and v[4] > 0
            }  # settles, as fnohist.derive
            sm = fh.day_summary(d, by_exp, fwd, spot.get(d))
            out[d] = tuple((x.dte, x.atm_iv) for x in sm.expiries)
        lo = hi
    return out
