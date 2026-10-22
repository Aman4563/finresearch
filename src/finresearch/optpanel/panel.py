"""One session of the cross-sectional options panel (P1.12, #374; evals/experiments/stock_option_vrp/PREREG.md §2),
pure: a day's parsed bhavcopy rows in, one row per underlying, the moneyness-filtered contract rows and the day's
stock-option OI totals out. The realised volatility needs the futures history and is added by optpanel.store.

Universe: every stock-option underlying in the file plus params.INDEX_UNDERLYINGS' index options, from the file alone
(no held or watched list is read). Engines are fincalc.fno_history's (the fno_iv_eod engine): ATM IV, term structure,
IV30 and skew from market settles; forwards from the same-expiry future, else parity, else spot. Anything that cannot
be fitted is None and counted, never 0.
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from finresearch.adapters.nse_fno_bhav import FoRow
from finresearch.fincalc import fno_history as fh
from finresearch.optpanel import params

RIGHTS = {"CE": "call", "PE": "put"}


def keep(kind: str, sym: str) -> bool:
    """The rows the panel parses: every stock option and future, and the index underlyings it adds."""
    return kind in ("STO", "STF") or (kind in ("IDO", "IDF") and sym in params.INDEX_UNDERLYINGS)


def bucket_label(i: int) -> str:
    """The i-th ln(K/F) bucket (0 = below the first edge) as a stable key, e.g. '-0.03..0.03'."""
    e = params.BUCKET_EDGES
    if i == 0:
        return f"<{e[0]:g}"
    if i == len(e):
        return f">={e[-1]:g}"
    return f"{e[i - 1]:g}..{e[i]:g}"


@dataclass
class DayPanel:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # symbol -> panel values
    kept: list[FoRow] = field(default_factory=list)  # moneyness-filtered contract rows
    totals: dict[str, int | None] = field(default_factory=dict)  # stock-option OI in contracts, non-expiring
    counts: dict[str, int] = field(default_factory=lambda: {"no_iv": 0, "no_forward_expiries": 0})


def _f(x) -> float | None:
    return None if x is None else float(x)


def _totals(day: date, rows: list[FoRow]) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for right, key in (("CE", "sto_call_oi_contracts"), ("PE", "sto_put_oi_contracts")):
        n: int | None = 0
        for r in rows:
            if r.instrument != "STO" or r.right != right or r.expiry <= day or not r.oi:
                continue
            if not r.lot_size:
                n = None  # a lot size is unknown: the total is unknown, never a partial sum
                break
            n += r.oi // r.lot_size
        out[key] = n
    return out


def compute_day(day: date, rows: list[FoRow]) -> DayPanel:
    out = DayPanel(totals=_totals(day, rows))
    by_sym: dict[str, list[FoRow]] = defaultdict(list)
    for r in rows:
        if keep(r.instrument, r.symbol) and r.day == day:
            by_sym[r.symbol].append(r)
    for sym, rs in sorted(by_sym.items()):
        opts = [r for r in rs if r.right in RIGHTS and r.expiry >= day]
        if not opts:
            continue
        out.rows[sym] = _underlying(day, rs, opts, out)
    return out


def _underlying(day: date, rs: list[FoRow], opts: list[FoRow], out: DayPanel) -> dict[str, Any]:
    futs = {r.expiry: float(r.settle) for r in rs if r.right == "XX" and r.settle and r.settle > 0}
    spot = next((float(r.underlying) for r in rs if r.underlying is not None and r.underlying > 0), None)
    market: dict[date, list[fh.OptQuote]] = defaultdict(list)
    for r in opts:
        if r.settle and r.settle > 0 and fh.market_settle(float(r.settle), _f(r.close), r.contracts):
            market[r.expiry].append(fh.OptQuote(float(r.strike), RIGHTS[r.right], float(r.settle)))
    sm = fh.day_summary(day, market, futs, spot)
    fitted = {x.expiry: x.forward for x in sm.expiries}
    expiries = sorted({r.expiry for r in opts})
    fwd: dict[date, float] = {}
    for e in expiries:
        if e in fitted:
            fwd[e] = fitted[e]
        elif (
            got := fh.forward_for(market.get(e, []), (e - day).days / 365, fh.RATE, futs.get(e), spot)
        ) is not None:
            fwd[e] = got[0]
        else:
            out.counts["no_forward_expiries"] += 1
    near_two = set(expiries[: params.ROWS_EXPIRIES])
    buckets: dict[str, dict[str, dict[str, int]]] = {"call": {}, "put": {}}
    liq = 0
    for r in opts:
        f = fwd.get(r.expiry)
        if f is None or r.strike <= 0:
            continue
        m = math.log(float(r.strike) / f)
        b = buckets[RIGHTS[r.right]].setdefault(
            bucket_label(bisect.bisect_right(params.BUCKET_EDGES, m)), {"oi": 0, "contracts": 0}
        )
        b["oi"] += r.oi or 0
        b["contracts"] += r.contracts or 0
        if abs(float(r.strike) / f - 1) <= params.LIQ_BAND:
            liq += r.contracts or 0
        if (
            r.expiry in near_two
            and abs(m) <= params.ROWS_LN_BAND
            and ((r.oi or 0) > 0 or (r.contracts or 0) > 0)
        ):
            out.kept.append(r)
    near, nxt = sm.near, sm.next
    if near is None:
        out.counts["no_iv"] += 1
    legs: dict[str, FoRow] = {}
    if near is not None:
        legs = {r.right: r for r in opts if r.expiry == near.expiry and float(r.strike) == near.atm_strike}
    call, put = legs.get("CE"), legs.get("PE")
    return {
        "near_expiry": near.expiry if near else None, "near_iv": near.atm_iv if near else None,
        "next_expiry": nxt.expiry if nxt else None, "next_iv": nxt.atm_iv if nxt else None,
        "iv_30d": sm.iv_30d, "iv_30d_method": sm.iv_30d_method, "skew_25d": near.skew_25d if near else None,
        "forward": near.forward if near else None, "forward_source": near.forward_source if near else None,
        "straddle_expiry": near.expiry if near and call and put else None,
        "straddle_strike": call.strike if call and put else None,
        "straddle_call": call.settle if call and put else None, "straddle_put": put.settle if call and put else None,
        "straddle_call_contracts": call.contracts if call and put else None,
        "straddle_put_contracts": put.contracts if call and put else None,
        "buckets": buckets, "liq_contracts": liq, "spot": spot,
    }  # fmt: skip
