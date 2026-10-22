"""Holdings-based Brinson-Fachler attribution of today's holdings by sector against the Nifty 50 (#348).

What it answers: over the window (1, 3 or 6 months, ending on the last stored bhavcopy session), how much of the gap
between today's holdings, held unchanged through the window, and the Nifty 50 came from sector weights (allocation),
from the stocks picked inside a sector (selection) and from both (interaction). `fincalc.brinson` does the arithmetic.

- Benchmark weights: NSE's free-float market cap of each current Nifty 50 constituent (adapters.nse_index_weights).
  The index is free-float market-cap weighted, so a constituent's weight is its share of the total; NSE Indices'
  capping (a stock at most 33 %, the top three at most 62 %) is not applied and is rarely binding for the Nifty 50.
  The market cap is moved to the window's last close (× close / the price NSE quoted it at) and back-drifted to the
  window's start by the constituent's return, so the start weights are exact if shares and free float did not change.
- Constituent returns: chained from the stored full bhavcopy (refseries.equity_eod) as Π close / previous close, so
  a split or bonus NSE adjusted the previous close for is handled; a one-day move beyond ±history.JUMP is treated as
  an unadjusted corporate action and makes that constituent unknown. Price returns (no dividends) on both sides.
- Holdings: each holding's unit-adjusted return from its own closes (the analytics price store), weights likewise
  back-drifted from its value at the window's last close. A holding without a return is left out and counted.
- Sectors are NSE's sector level for stocks (the same field on both sides); funds, ETFs, gold and bonds are grouped
  as in the concentration view (portfolio.limits.sector_label), which the Nifty 50 does not hold.

Caveats returned with the payload: trades during the window are not modelled (it is not your realised return);
index changes during the window are not modelled (hence at most six months: NSE reconstitutes the index
semi-annually). Unknown never becomes 0: a benchmark constituent without a weight or a return makes its sector line
and the totals unknown. Personal data stays on this machine and never reaches an LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fincalc import brinson
from finresearch.portfolio.history import JUMP


@dataclass(frozen=True)
class Member:
    """A benchmark constituent: NSE's free-float market cap at `last_price`, its close on the window's last session
    and its return over the window (None: unknown, with a reason)."""

    symbol: str
    name: str
    sector: str | None
    ffmc: float | None
    last_price: float | None
    close_end: float | None
    ret: float | None
    reason: str | None = None

    @property
    def mcap_end(self) -> float | None:
        if self.ffmc is None or not self.last_price or self.close_end is None:
            return None
        return self.ffmc * self.close_end / self.last_price


@dataclass(frozen=True)
class Holding:
    key: str
    name: str
    segment: str
    value_end: float | None  # units × close on the window's last session
    ret: float | None
    reason: str | None = None


def constituent_return(rows: Sequence[tuple[date, Decimal | None, Decimal | None]], s0: date, end: date
                       ) -> tuple[float | None, str | None]:  # fmt: skip
    """Return over (s0, end] from (day, close, previous close) rows: Π close / previous close over the rows after s0.
    Needs a row on s0 and on end; a one-day move beyond ±JUMP is unknown (a probable unadjusted corporate action)."""
    by = {d: (c, p) for d, c, p in rows if s0 <= d <= end}
    for d, which in ((s0, "first"), (end, "last")):
        if d not in by or by[d][0] is None:
            return None, f"no stored close on {d.isoformat()} (the window's {which} session)"
    growth = 1.0
    for d in sorted(by):
        if d == s0:
            continue
        c, p = by[d]
        if c is None or p is None or p <= 0:
            return None, f"no close or previous close on {d.isoformat()}"
        step = float(c / p)
        if abs(step - 1) > JUMP:
            return None, (f"a one-day move of {(step - 1) * 100:+.1f} % on {d.isoformat()}: probably an unadjusted "
                          "split or bonus").replace("+", "")  # fmt: skip
        growth *= step
    return growth - 1, None


def _dec(x: float) -> Decimal:
    return Decimal(repr(round(x, 12)))


def _f(x: Decimal | None, nd: int = 6) -> float | None:
    return None if x is None else round(float(x), nd)


def attribute(holdings: Sequence[Holding], bench: Sequence[Member]) -> dict[str, Any]:
    """Segments, Brinson-Fachler lines and totals (fractions); see the module docstring for the conventions."""
    # benchmark: start mcap = end mcap / (1 + r); a member without either makes its sector unknown
    b_unknown = [{"symbol": m.symbol, "name": m.name, "reason": m.reason or "no weight or return"}
                 for m in bench if m.mcap_end is None or m.ret is None]  # fmt: skip
    bad_sectors = {m.sector or "Unclassified" for m in bench if m.mcap_end is None or m.ret is None}
    b0: dict[str, float] = {}
    b1: dict[str, float] = {}
    for m in bench:
        if m.mcap_end is not None and m.ret is not None:
            k = m.sector or "Unclassified"
            b0[k] = b0.get(k, 0.0) + m.mcap_end / (1 + m.ret)
            b1[k] = b1.get(k, 0.0) + m.mcap_end
    b0_tot = sum(b0.values())
    # portfolio: holdings with a return; the rest are excluded and counted
    known = [h for h in holdings if h.ret is not None and h.value_end is not None and h.value_end > 0]
    excluded = [{"key": h.key, "name": h.name, "reason": h.reason or "no return over the window"}
                for h in holdings if h not in known]  # fmt: skip
    end_all = sum(h.value_end or 0.0 for h in holdings)
    p0: dict[str, float] = {}
    p1: dict[str, float] = {}
    for h in known:
        p0[h.segment] = p0.get(h.segment, 0.0) + h.value_end / (1 + h.ret)  # type: ignore[operator]
        p1[h.segment] = p1.get(h.segment, 0.0) + h.value_end  # type: ignore[operator]
    p0_tot = sum(p0.values())

    names = sorted({*b0, *bad_sectors, *p0})
    segs, reasons = [], {}
    for k in names:
        wp = _dec(p0[k] / p0_tot) if k in p0 and p0_tot else Decimal(0)
        rp = _dec(p1[k] / p0[k] - 1) if k in p0 else None
        if k in bad_sectors:
            wb = rb = None
            reasons[k] = "benchmark weight or return unknown for " + "; ".join(
                f"{u['symbol']}: {u['reason']}" for u in b_unknown if any(
                    m.symbol == u["symbol"] and (m.sector or "Unclassified") == k for m in bench))  # fmt: skip
        else:
            wb = _dec(b0[k] / b0_tot) if k in b0 and b0_tot else Decimal(0)
            rb = _dec(b1[k] / b0[k] - 1) if k in b0 else None
        segs.append(brinson.Segment(k, wp, wb, rp, rb))
    res = brinson.attribute(segs, reasons) if p0_tot else None
    lines = [] if res is None else [
        {"segment": x.name, "wp": _f(x.wp), "wb": _f(x.wb), "rp": _f(x.rp), "rb": _f(x.rb),
         "allocation": _f(x.allocation), "selection": _f(x.selection), "interaction": _f(x.interaction),
         "total": _f(x.total), "note": x.note, "reason": x.reason} for x in res.lines]  # fmt: skip
    totals = {k: None for k in ("portfolio_return", "benchmark_return", "active", "allocation", "selection",
                                "interaction")}  # fmt: skip
    if res is not None:
        totals = {"portfolio_return": _f(res.portfolio_return), "benchmark_return": _f(res.benchmark_return),
                  "active": _f(res.active), "allocation": _f(res.allocation), "selection": _f(res.selection),
                  "interaction": _f(res.interaction)}  # fmt: skip
    covered = sum(p1.values())
    return {"segments": lines, "totals": totals, "excluded": excluded, "benchmark_unknown": b_unknown,
            "coverage": round(covered / end_all, 4) if end_all else None,
            "complete": res is not None and res.complete and not excluded,
            "reason": None if res is not None else "no holding has a return over the window"}  # fmt: skip


# --------------------------------------------------------------------------- reading the stores (no writes)
@dataclass(frozen=True)
class Window:
    start: date | None
    end: date | None
    reason: str | None = None


def window(s: Any, today: date, *, days: int) -> Window:
    """The window's first and last stored bhavcopy sessions: the last loaded day on or before `today`, and the last
    loaded day on or before `days` calendar days earlier. Every weekday in between must be loaded or known to have no
    file (a holiday): a day never tried, or a failed one, would make the chained returns skip a day."""
    from datetime import timedelta

    from sqlalchemy import select

    from finresearch.db.models import EquityEodDay

    loaded = select(EquityEodDay.day).where(EquityEodDay.status == "loaded")
    end = s.scalar(loaded.where(EquityEodDay.day <= today).order_by(EquityEodDay.day.desc()).limit(1))
    if end is None:
        return Window(None, None, "no NSE bhavcopy is stored yet (finresearch equity backfill)")
    target = end - timedelta(days=days)
    start = s.scalar(loaded.where(EquityEodDay.day <= target).order_by(EquityEodDay.day.desc()).limit(1))
    if start is None:
        first = s.scalar(loaded.order_by(EquityEodDay.day).limit(1))
        return Window(None, end, f"the stored bhavcopy starts on {first.isoformat()}; this window needs a session "
                                 f"on or before {target.isoformat()}")  # fmt: skip
    seen = dict(s.execute(select(EquityEodDay.day, EquityEodDay.status)
                          .where(EquityEodDay.day.between(start, end))).all())  # fmt: skip
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5 and seen.get(d) not in ("loaded", "no_file"):
            return Window(start, end, f"the bhavcopy for {d.isoformat()} is not stored, so returns over the window "
                                      "would skip a day")  # fmt: skip
    return Window(start, end)


def members(s: Any, iw: Any, s0: date, end: date) -> list[Member]:
    """Each constituent of `iw` (adapters.nse_index_weights.IndexWeights) with its stored close on `end` and its
    chained return over (s0, end]."""
    from sqlalchemy import select

    from finresearch.db.models import EquityEod
    from finresearch.refseries.equity_eod import PRICE_SERIES

    syms = [m.symbol for m in iw.members]
    per: dict[str, dict[date, dict[str, tuple[Decimal | None, Decimal | None]]]] = {}
    q = select(
        EquityEod.symbol, EquityEod.day, EquityEod.series, EquityEod.close, EquityEod.prev_close
    ).where(EquityEod.symbol.in_(syms), EquityEod.day.between(s0, end), EquityEod.series.in_(PRICE_SERIES))
    for sym, d, series, c, p in s.execute(q):
        per.setdefault(sym, {}).setdefault(d, {})[series] = (c, p)
    out = []
    for m in iw.members:
        rows = [
            (d, *next(by[x] for x in PRICE_SERIES if x in by))
            for d, by in sorted(per.get(m.symbol, {}).items())
        ]
        ret, why = constituent_return(rows, s0, end)
        close_end = next((float(c) for d, c, _ in rows if d == end and c is not None), None)
        if m.ffmc is None:
            why = "NSE gave no free-float market cap"
        elif not m.last_price:
            why = "NSE gave no price for its market cap"
        out.append(Member(m.symbol, m.name, m.sector, m.ffmc, m.last_price, close_end, ret, why))
    return out


def index_return(s: Any, s0: date, end: date, symbol: str = "NIFTY") -> float | None:
    """NSE's own index price return over (s0, end] from the stored index close file; None when either close is missing."""
    from sqlalchemy import select

    from finresearch.db.models import IndexEod

    got = dict(s.execute(select(IndexEod.day, IndexEod.close)
                         .where(IndexEod.symbol == symbol, IndexEod.day.in_([s0, end]))).all())  # fmt: skip
    a, b = got.get(s0), got.get(end)
    return None if not a or b is None else float(b / a) - 1


STALE_DAYS = (
    7  # a price at most this many days before a window edge stands for that day (holidays, fund NAV dates)
)


def holding_return(closes: dict[date, float], factors: dict[date, float] | None, s0: date, end: date
                   ) -> tuple[float | None, float | None, str | None]:  # fmt: skip
    """(close at end, unit-adjusted return over (s0, end], reason): the last price on or before each edge (within
    STALE_DAYS), compounded through history.returns_of, which applies recorded splits and bonuses."""
    from datetime import timedelta

    from finresearch.portfolio.history import returns_of

    def at_or_before(d: date) -> date | None:
        ks = [k for k in closes if d - timedelta(days=STALE_DAYS) <= k <= d]
        return max(ks) if ks else None

    d0, d1 = at_or_before(s0), at_or_before(end)
    if d0 is None:
        return (
            None,
            None,
            f"no price on or within {STALE_DAYS} days before {s0.isoformat()} (bought or listed later?)",
        )
    if d1 is None:
        return None, None, f"no price on or within {STALE_DAYS} days before {end.isoformat()}"
    span = {k: v for k, v in closes.items() if d0 <= k <= d1}
    daily = returns_of(span, factors)
    if len(daily) < len(span) - 1:
        return closes[d1], None, f"a one-day move beyond ±{JUMP * 100:.0f} % with no recorded split or bonus"
    growth = 1.0
    for r in daily.values():
        growth *= 1 + r
    return closes[d1], growth - 1, None


# --------------------------------------------------------------------------- the payload
PERIODS = {
    "1m": 30,
    "3m": 91,
    "6m": 182,
}  # calendar days; at most six months (the index reconstitutes semi-annually)
INDEX_TOLERANCE = (
    0.005  # rebuilt vs NSE's own Nifty 50 return: more than half a percentage point apart is a warning
)
BENCHMARK = "Nifty 50 (price return)"
CAVEATS = [
    "Holdings-based: today's holdings as if held unchanged through the window; trades in the window are not modelled, "
    "so this is not your realised return.",
    "Benchmark weights are NSE's free-float market caps of today's Nifty 50 members, back-drifted to the window's "
    "start; index changes during the window and NSE Indices' capping are not modelled.",
    "Price returns on both sides (no dividends), so this differs from the NIFTYBEES total-return benchmark used by "
    "Performance.",
]


def _base(period: str) -> dict[str, Any]:
    from finresearch.portfolio.analytics import PRIVACY
    from finresearch.signals.base import DISCLAIMER

    return {"disclaimer": DISCLAIMER, "privacy": PRIVACY, "method": brinson.METHOD, "caveats": CAVEATS,
            "period": period, "periods": list(PERIODS), "available": False, "reason": None,
            "window": None, "benchmark": None, "segments": [], "totals": None, "excluded": [],
            "benchmark_unknown": [], "coverage": None, "complete": False}  # fmt: skip


def unavailable(period: str, reason: str) -> dict[str, Any]:
    return {**_base(period), "reason": reason}


async def load(
    h: Any, iw: Any, today: date, period: str, fetch: Any, store: Any, sector_of: Any
) -> dict[str, Any]:
    """The attribution payload for `period` (a PERIODS key). `fetch`/`store`: the analytics price fetcher and
    on-disk PriceStore for each holding's closes; `sector_of(symbol)`: NSE's sector of a stock outside the index."""
    from datetime import timedelta

    from finresearch.db import session_scope
    from finresearch.portfolio.history import Instrument
    from finresearch.portfolio.limits import sector_label

    with session_scope() as s:
        w = window(s, today, days=PERIODS[period])
        ms = members(s, iw, w.start, w.end) if w.reason is None else []
        idx = index_return(s, w.start, w.end) if w.reason is None else None
        s.rollback()  # reads only
    if w.reason is not None:
        return unavailable(period, w.reason)
    in_index = {m.symbol: m.sector for m in iw.members}
    holdings = []
    for p in (p for p in h.positions if p.value > 0):
        kind, ident = p.key.split(":", 1)
        inst = Instrument(p.key, "mf" if kind == "MF" else kind.lower(), ident, p.scheme)
        try:
            closes = await store.get(p.key, w.start - timedelta(days=STALE_DAYS + 3), w.end,
                                     lambda a, b, i=inst: fetch(i, a, b), today)  # fmt: skip
        except Exception:
            closes = {}
        c1, ret, why = holding_return(closes, h.factors.get(p.key), w.start, w.end)
        sym = p.nse_symbol or (ident if kind == "NSE" else None)
        nse_sector = None
        if p.asset_type == "stock" and sym:
            nse_sector = in_index[sym] if sym in in_index else await sector_of(sym)
        seg = sector_label(p.asset_type, nse_sector, sym or ident, p.name, p.tax_class)
        holdings.append(Holding(p.key, p.name, seg, None if c1 is None else p.units * c1, ret, why))
    out = {**_base(period), **attribute(holdings, ms), "available": True,
           "window": {"start": w.start.isoformat(), "end": w.end.isoformat()}}  # fmt: skip
    rebuilt = out["totals"]["benchmark_return"]
    diff = None if rebuilt is None or idx is None else round(rebuilt - idx, 6)
    warn = None
    if diff is not None and abs(diff) > INDEX_TOLERANCE:
        warn = (f"the rebuilt Nifty 50 return differs from NSE's own index by {diff * 100:+.2f} percentage points "
                f"(tolerance ±{INDEX_TOLERANCE * 100:.1f}): treat the attribution with care")  # fmt: skip
    out["benchmark"] = {"name": BENCHMARK, "members": len(iw.members), "rebuilt_return": rebuilt,
                        "index_return": None if idx is None else round(idx, 6), "difference": diff,
                        "tolerance": INDEX_TOLERANCE, "warning": warn,
                        "source": {"name": "NSE: Nifty 50 constituents with free-float market capitalisation",
                                   "url": iw.url, "as_of": iw.as_of.isoformat() if iw.as_of else None,
                                   "fetched_at": iw.fetched_at.isoformat() if iw.fetched_at else None}}  # fmt: skip
    if idx is None:
        out["benchmark"]["warning"] = (
            "NSE's Nifty 50 close is not stored for the window's edges: not cross-checked"
        )
    return out
