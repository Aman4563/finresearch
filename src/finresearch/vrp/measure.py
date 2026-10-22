"""One session's 30-day model-free implied variance as India VIX builds it (P3.6, #389). Pure.

- Monthly expiries: the latest listed expiry of each calendar month (weeklies expire earlier in the same month).
- Terms (India VIX white paper 3.1): the near and next monthly expiries; when the near one has ROLL_SESSIONS or
  fewer sessions left after the day (the expiry session counted), the next and the far one. An expiry on the day
  itself is never a term (T = 0).
- Each term: fincalc.mfiv.term_variance on the day's market settles (fno_history.market_settle), the same-expiry
  future's settle as F (India VIX), else Cboe's parity forward; T = minutes from the close to the expiry's close /
  525,600; R = fno_history.RATE (India VIX uses NSE MIBOR; no dated MIBOR series is stored [U]).
- 30-day: fincalc.mfiv.thirty_day on the two terms (extrapolating when both are beyond 30 days).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import mfiv

ROLL_SESSIONS = (
    3  # India VIX: "with 3 trading days left to expiry, India VIX rolls to the next and far month"
)


@dataclass(frozen=True)
class Mf30:
    value: float | None
    reason: str | None
    near: mfiv.Term | None = None
    far: mfiv.Term | None = None


def monthly_expiries(expiries: Sequence[date]) -> list[date]:
    last: dict[tuple[int, int], date] = {}
    for e in expiries:
        key = (e.year, e.month)
        if key not in last or e > last[key]:
            last[key] = e
    return sorted(last.values())


def terms(day: date, monthly: Sequence[date], sessions: Sequence[date]) -> tuple[date, date] | None:
    """The near and next monthly expiries used on `day` (`sessions`: the session calendar, sorted)."""
    after = [e for e in monthly if e > day]
    if len(after) < 2:
        return None
    left = sum(1 for s in sessions if day < s <= after[0])
    if left <= ROLL_SESSIONS:
        after = after[1:]
        if len(after) < 2:
            return None
    return after[0], after[1]


def mf30(day: date, pair: tuple[date, date] | None, strikes: Mapping[date, Sequence[mfiv.Strike]],
         futures: Mapping[date, float], *, rate: float = fh.RATE, band: float | None = None,
         extend_flat: bool = False) -> Mf30:  # fmt: skip
    if pair is None:
        return Mf30(None, "fewer than two monthly expiries after the day")
    out = []
    for e in pair:
        n = mfiv.eod_minutes((e - day).days)
        tm = mfiv.term_variance(strikes.get(e, ()), n / mfiv.N365, rate, futures.get(e), band=band,
                                extend_flat=extend_flat)  # fmt: skip
        out.append((n, tm))
    (n1, a), (n2, b) = out
    if a.value is None or b.value is None:
        bad = a if a.value is None else b
        which = "near" if bad is a else "next"
        return Mf30(None, f"{which} term {pair[0] if bad is a else pair[1]}: {bad.reason}", a, b)
    v = mfiv.thirty_day(a.value, n1, b.value, n2)
    return Mf30(v, None if v is not None else "the 30-day combination is not positive", a, b)


def strikes_from_settles(
    rows: Sequence[tuple[float, str, float | None, float | None, int | None]],
) -> list[mfiv.Strike]:
    """(strike, right, settle, close, contracts) rows of one expiry -> Strike with a price only where the settle is
    a market price (fno_history.market_settle); every listed strike is kept so the two-misses stop rule sees it."""
    by: dict[float, dict[str, float | None]] = {}
    for k, right, settle, close, n in rows:
        ok = settle is not None and settle > 0 and fh.market_settle(settle, close, n)
        by.setdefault(k, {"call": None, "put": None})[right] = settle if ok else None
    return [mfiv.Strike(k, v["call"], v["put"]) for k, v in sorted(by.items())]
