"""Derived series from the stored bhavcopy (#306): the daily end-of-day volatility row per underlying (fno_iv_eod,
computed by fincalc.fno_history) and the contract calendar with lot-size history (fno_contract).

SEBI's circular SEBI/HO/MRD/TPD/P/CIR/2024/132 of 1-Oct-2024 (https://www.sebi.gov.in/legal/circulars/oct-2024/
measures-to-strengthen-equity-index-derivatives-framework-for-increased-investor-protection-and-market-stability_87208.html)
limited weekly expiries to one benchmark index per exchange and raised the minimum contract value to ₹15 lakh, which
changed lot sizes; its effective dates were not re-read here. fno_contract records what the bhavcopies show instead
of encoding the rules: weekly versus monthly per expiry, and each contract's lot size by day, so the backtester uses
the expiries and lots that actually existed.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.db.models import FnoBhav, FnoContract, FnoIvEod
from finresearch.fincalc import fno_history as fh

RV_LOOKBACK_DAYS = 45  # calendar days of futures read for the 20-day realised volatility (~30 sessions)
MIN_GCD_POINTS = 10  # distinct open-interest figures needed before a lot size is inferred from their gcd
RIGHTS = {"CE": "call", "PE": "put"}


def _q(x: float | None, places: int = 3) -> Decimal | None:
    return None if x is None or not math.isfinite(x) else Decimal(str(round(x, places)))


def derive_day(s: Session, day: date, rate: float = fh.RATE) -> int:
    """Compute and upsert fno_iv_eod for every underlying with options stored on `day`. Returns rows written."""
    rows = s.execute(select(FnoBhav.symbol, FnoBhav.instrument, FnoBhav.expiry, FnoBhav.strike, FnoBhav.option_type,
                            FnoBhav.settle, FnoBhav.contracts, FnoBhav.underlying, FnoBhav.close)
                     .where(FnoBhav.day == day)).all()  # fmt: skip
    opts: dict[str, dict[date, list[fh.OptQuote]]] = defaultdict(lambda: defaultdict(list))
    futs: dict[str, dict[date, float]] = defaultdict(dict)
    spot: dict[str, float] = {}
    for sym, kind, exp, strike, right, settle, contracts, und, close in rows:
        if und is not None:
            spot[sym] = float(und)
        if settle is None or settle <= 0:
            continue
        if kind in ("IDF", "STF"):
            futs[sym][exp] = float(settle)
        elif right in RIGHTS and fh.market_settle(
            float(settle), None if close is None else float(close), contracts
        ):
            opts[sym][exp].append(fh.OptQuote(float(strike), RIGHTS[right], float(settle)))
    if not opts:
        return 0
    lo = day - timedelta(days=RV_LOOKBACK_DAYS)
    hist: dict[str, list[tuple[date, date, float]]] = defaultdict(list)
    for sym, d, exp, settle in s.execute(
        select(FnoBhav.symbol, FnoBhav.day, FnoBhav.expiry, FnoBhav.settle).where(
            FnoBhav.symbol.in_(list(opts)), FnoBhav.instrument.in_(("IDF", "STF")), FnoBhav.day.between(lo, day))
    ):  # fmt: skip
        if settle is not None:
            hist[sym].append((d, exp, float(settle)))
    n = 0
    for sym, by_exp in opts.items():
        sm = fh.day_summary(day, by_exp, futs.get(sym, {}), spot.get(sym), rate)
        rets = [r for d, r in fh.front_future_returns(hist.get(sym, [])) if d <= day]
        near = sm.near
        values: dict[str, Any] = {
            "near_expiry": near.expiry if near else None, "forward": _q(near.forward, 2) if near else None,
            "forward_source": near.forward_source if near else None,
            "atm_strike": _q(near.atm_strike, 2) if near else None, "near_iv": _q(near.atm_iv) if near else None,
            "next_expiry": sm.next.expiry if sm.next else None, "next_iv": _q(sm.next.atm_iv) if sm.next else None,
            "iv_30d": _q(sm.iv_30d), "iv_30d_method": sm.iv_30d_method,
            "skew_25d": _q(near.skew_25d) if near else None, "rv_20": _q(fh.realised_vol_pct(rets)),
            "underlying": _q(spot.get(sym), 2), "rate": Decimal(str(rate)),
        }  # fmt: skip
        stmt = pg_insert(FnoIvEod).values(symbol=sym, day=day, **values)
        s.execute(stmt.on_conflict_do_update(index_elements=["symbol", "day"],
                                             set_={**values, "computed_at": stmt.excluded.computed_at}))  # fmt: skip
        n += 1
    return n


def infer_lot(oi_values: list[int]) -> int | None:
    """The greatest common divisor of open-interest figures (each a whole number of lots); None with fewer than
    MIN_GCD_POINTS distinct values, where a common multiple of the lot is too likely."""
    vals = sorted({abs(v) for v in oi_values if v})
    if len(vals) < MIN_GCD_POINTS:
        return None
    return math.gcd(*vals) or None


def rebuild_contracts(s: Session, since: date | None = None) -> int:
    """Upsert fno_contract for every (symbol, expiry) expiring on or after `since` (all when None)."""
    q = select(FnoBhav.symbol, FnoBhav.expiry, FnoBhav.instrument, FnoBhav.day, FnoBhav.lot_size, FnoBhav.oi)
    if since is not None:
        q = q.where(FnoBhav.expiry >= since)
    agg: dict[tuple[str, date], dict[str, Any]] = {}
    for sym, exp, kind, d, lot, oi in s.execute(q):
        a = agg.setdefault(
            (sym, exp), {"first": d, "last": d, "fut": False, "index": False, "lots": {}, "oi": set()}
        )
        a["first"], a["last"] = min(a["first"], d), max(a["last"], d)
        a["fut"] |= kind in ("IDF", "STF")
        a["index"] |= kind in ("IDF", "IDO")
        if lot:
            a["lots"][d] = lot
        elif oi and len(a["oi"]) < 5000:
            a["oi"].add(oi)
    for (sym, exp), a in agg.items():
        changes, prev = [], None
        for d in sorted(a["lots"]):
            if a["lots"][d] != prev:
                changes.append([d.isoformat(), a["lots"][d]])
                prev = a["lots"][d]
        if changes:
            lot, source = changes[-1][1], "bhavcopy"
        else:
            lot = infer_lot(list(a["oi"]))
            source = "inferred_oi_gcd" if lot else None
        values = {"underlying_kind": "index" if a["index"] else "stock", "kind": "monthly" if a["fut"] else "weekly",
                  "first_seen": a["first"], "last_seen": a["last"], "lot_size": lot, "lot_source": source,
                  "lot_changes": changes if len(changes) > 1 else None}  # fmt: skip
        stmt = pg_insert(FnoContract).values(symbol=sym, expiry=exp, **values)
        s.execute(stmt.on_conflict_do_update(index_elements=["symbol", "expiry"], set_=values))
    return len(agg)


def iv_eod_series(s: Session, symbol: str) -> list[FnoIvEod]:
    return list(s.scalars(select(FnoIvEod).where(FnoIvEod.symbol == symbol.upper()).order_by(FnoIvEod.day)))


def contracts(s: Session, symbol: str) -> list[FnoContract]:
    return list(s.scalars(select(FnoContract).where(FnoContract.symbol == symbol.upper())
                          .order_by(FnoContract.expiry)))  # fmt: skip
