"""The daily F&O mark table (#383, P2.8): after NSE's bhavcopy is loaded, one `fno_mark` row per leg open at that
session's close, paper (open paper trades) and real (the Groww position legs the caller resolved). P&L attribution
(`fnobook.review`) and P4.4's forward test read these rows; they are written once and never updated.

* price: the contract's settlement price in the stored bhavcopy (NSE's daily settlement price for the contract),
  else its close (labelled); no stored row = unknown (NULL with the reason), never 0.
* spot: the bhavcopy's underlying value (UDiFF, 2024 on), else NSE's index close (index_eod) or the stock's EQ close
  (equity_eod); else unknown.
* T: 15:30 IST that day to 15:30 IST on expiry (fincalc.dates); IV: Black-Scholes implied by price and spot at RATE;
  unknown on expiry day (T = 0), for a future, or when the price is outside the no-arbitrage bounds.
Local only: the F&O book is personal data and never reaches a model."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import EquityEod, FnoBhav, FnoMark, IndexEod, PaperStrategy
from finresearch.fincalc import options as o
from finresearch.fincalc.dates import session_clock, to_ist, years_to_expiry

RATE = 0.065  # fno_mark rows are write-once IVs implied at this rate; kept fixed (the live greeks use signals.rates)
_TYPE = {"call": "CE", "put": "PE", "future": "XX"}


@dataclass(frozen=True)
class RealLeg:
    """One Groww F&O position leg, resolved through the instruments file; qty is signed units, None = unknown."""

    trading_symbol: str
    underlying: str
    expiry: date
    right: str
    strike: float | None
    qty: int | None


def _bhav(s: Session, day: date, sym: str, expiry: date, right: str, strike: float | None) -> FnoBhav | None:
    k = Decimal(0) if right == "future" else Decimal(str(strike))
    return s.scalars(select(FnoBhav).where(FnoBhav.day == day, FnoBhav.symbol == sym, FnoBhav.expiry == expiry,
                                           FnoBhav.strike == k, FnoBhav.option_type == _TYPE[right])).first()  # fmt: skip


def _spot(s: Session, day: date, sym: str, row: FnoBhav | None) -> tuple[float | None, str | None]:
    if row is not None and row.underlying:
        return float(row.underlying), "NSE bhavcopy underlying value"
    u = s.scalar(select(FnoBhav.underlying).where(FnoBhav.day == day, FnoBhav.symbol == sym,
                                                  FnoBhav.underlying.is_not(None)).limit(1))  # fmt: skip
    if u:
        return float(u), "NSE bhavcopy underlying value"
    c = s.scalar(select(IndexEod.close).where(IndexEod.day == day, IndexEod.symbol == sym))
    if c:
        return float(c), "NSE index close"
    c = s.scalar(
        select(EquityEod.close).where(EquityEod.day == day, EquityEod.symbol == sym, EquityEod.series == "EQ")
    )
    return (float(c), "NSE equity close (EQ)") if c else (None, None)


def _mark(s: Session, day: date, *, book: str, leg_key: str, sym: str, expiry: date, right: str,
          strike: float | None, qty: int | None, paper_id: int | None = None) -> dict[str, Any]:  # fmt: skip
    row = _bhav(s, day, sym, expiry, right, strike)
    price, src, why = None, None, []
    if row is not None and row.settle:
        price, src = float(row.settle), "NSE bhavcopy settle"
    elif row is not None and row.close:
        price, src = float(row.close), "NSE bhavcopy close (no settle)"
    else:
        why.append(
            f"no stored bhavcopy row for {sym} {expiry:%d-%b-%Y} {strike or ''} {right} on {day:%d-%b-%Y}"
        )
    spot, spot_src = _spot(s, day, sym, row)
    if spot is None:
        why.append(f"no stored close of {sym} on {day:%d-%b-%Y}")
    t = years_to_expiry(expiry, session_clock(day))
    iv = None
    if right != "future" and price and spot and t > 0:
        iv = o.implied_vol(right, price, spot, float(strike), t, RATE)  # type: ignore[arg-type]
        if iv is None:
            why.append("the price is outside the no-arbitrage bounds: no implied volatility")
    elif right != "future" and t <= 0:
        why.append("expiry day: no implied volatility")
    n = lambda x, p: None if x is None else Decimal(str(round(x, p)))  # noqa: E731
    return dict(day=day, book=book, leg_key=leg_key, paper_id=paper_id, position_key=f"{sym}:{expiry.isoformat()}",
                symbol=sym, expiry=expiry, right=right, strike=None if strike is None else Decimal(str(strike)),
                qty=qty, price=n(price, 2), price_source=src, spot=n(spot, 2), spot_source=spot_src, iv=n(iv, 6),
                t_years=n(max(t, 0.0), 8), note="; ".join(why) or None)  # fmt: skip


def _open_on(st: PaperStrategy, day: date) -> bool:
    """Open at that session's close: opened on or before it, not closed on or before it, not past expiry."""
    return (to_ist(st.opened_at).date() <= day <= st.expiry
            and (st.closed_at is None or to_ist(st.closed_at).date() > day))  # fmt: skip


def record(s: Session, day: date, real_legs: Sequence[RealLeg] = ()) -> dict[str, int]:
    """Write the marks of `day` that are not written yet (rows already there are left as they are)."""
    vals = []
    for st in s.scalars(select(PaperStrategy).where(PaperStrategy.expiry >= day).order_by(PaperStrategy.id)):
        if not _open_on(st, day):
            continue
        for x in st.legs:
            k = None if x.right == "future" else float(x.strike)
            vals.append(_mark(s, day, book="paper", leg_key=f"paper_leg:{x.id}", sym=st.symbol, expiry=st.expiry,
                              right=x.right, strike=k, qty=x.qty if x.side == "buy" else -x.qty, paper_id=st.id))  # fmt: skip
    for r in real_legs:
        if r.expiry >= day:
            vals.append(_mark(s, day, book="real", leg_key=f"groww:{r.trading_symbol}"[:80], sym=r.underlying,
                              expiry=r.expiry, right=r.right, strike=r.strike, qty=r.qty))  # fmt: skip
    out = {"paper": 0, "real": 0, "unpriced": 0}
    for v in vals:
        stmt = insert(FnoMark).values(**v).on_conflict_do_nothing(constraint="uq_fno_mark_day_leg")
        if s.scalars(stmt.returning(FnoMark.id)).first() is not None:
            out[v["book"]] += 1
            out["unpriced"] += v["price"] is None
    s.flush()
    return out
