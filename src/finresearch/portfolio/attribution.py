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
