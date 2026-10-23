"""The stored history the F&O book's historical scenarios read (#394, fincalc.book_history): each underlying's
front-future log returns within one contract (fincalc.beta.returns_with_prev: the registered roll rule, lot changes
adjusted, outliers quarantined as missing), its IV30 (fno_iv_eod.iv_30d, vol points), and NIFTY's overnight gaps and
expiry-day moves (fincalc.risk_alerts.front_future: the same contract's previous close). Reads only, up to `today`:
nothing is cached or written, so a GET and the nightly risk pass may call it. The app's store holds 2022 on; the
2008-2021 vault lives in its own database and is never read here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from finresearch.fincalc import beta as fb
from finresearch.fincalc import book_history as bh
from finresearch.fincalc import risk_alerts as ra


@dataclass(frozen=True)
class History:
    series: dict[str, bh.Series] = field(default_factory=dict)  # absent = nothing stored
    gaps: list[float] = field(default_factory=list)  # NIFTY's overnight gaps, ln(open / previous close)
    expiry_returns: list[float] = field(default_factory=list)  # NIFTY's close-to-close log moves on expiry days
    span: tuple[date, date] | None = None  # NIFTY's first and last stored session


def load(s: Any, underlyings: Iterable[str], today: date) -> History:
    from sqlalchemy import select

    from finresearch.db.models import FnoBhav, FnoIvEod

    want = sorted({u.upper() for u in underlyings} | {"NIFTY"})
    futs: dict[str, list[tuple[date, date, float]]] = defaultdict(list)
    lots: dict[str, dict[tuple[date, date], int]] = defaultdict(dict)
    oc: list[tuple[date, date, float | None, float | None]] = []
    rows = s.execute(select(FnoBhav.symbol, FnoBhav.day, FnoBhav.expiry, FnoBhav.settle, FnoBhav.lot_size,
                            FnoBhav.open, FnoBhav.close)
                     .where(FnoBhav.symbol.in_(want), FnoBhav.instrument.in_(("IDF", "STF")),
                            FnoBhav.option_type == "XX", FnoBhav.day <= today))  # fmt: skip
    for sym, d, exp, settle, lot, op, cl in rows:
        if settle is not None:
            futs[sym].append((d, exp, float(settle)))
        if lot:
            lots[sym][(d, exp)] = lot
        if sym == "NIFTY":
            oc.append((d, exp, None if op is None else float(op), None if cl is None else float(cl)))
    ivs: dict[str, dict[date, float]] = defaultdict(dict)
    for sym, d, v in s.execute(select(FnoIvEod.symbol, FnoIvEod.day, FnoIvEod.iv_30d)
                               .where(FnoIvEod.symbol.in_(want), FnoIvEod.day <= today, FnoIvEod.iv_30d.is_not(None))):  # fmt: skip
        ivs[sym][d] = float(v)
    series = {}
    for u in want:
        rets = {d1: (d0, r) for d0, d1, r in fb.returns_with_prev(futs.get(u, []), lots.get(u))}
        if rets or ivs.get(u):
            series[u] = bh.Series(rets, dict(ivs.get(u, {})))
    front = ra.front_future(oc)
    gaps = [x.gap for x in front if x.gap is not None]
    exp_rets = [x.ret for x in front if x.day == x.expiry and x.ret is not None]
    days = sorted({d for d, _, _ in futs.get("NIFTY", [])})
    return History(series, gaps, exp_rets, (days[0], days[-1]) if days else None)
