"""Citable option-implied facts for research agents (#393): read-only, deterministic, market data only.

Each fact is computed from NSE's stored end-of-day F&O bhavcopy (fno_bhav) or the series derived from it (fno_iv_eod),
with fincalc.fno_history's conventions: Black-76 on the expiry's forward (the same-expiry future, else put-call parity,
else spot x e^(rT)), T = calendar days / 365, the constant RATE (6.5 %), settlement prices only where they are market
prices (`market_settle`). Never NSE's website option chain, the F&O book, the portfolio or any personal table.

- option_implied_move: the ATM 1-sigma move to an expiry, sigma_ATM x sqrt(T), and the ATM straddle (C + P) / F.
- option_iv30_percentile: the 30-day constant-maturity IV's percentile over the last 252 loaded sessions
  (fincalc.volatility.iv_stats_sessions; unknown below 80 % coverage).
- option_skew_25d: 25-delta put IV - 25-delta call IV of the near expiry (fincalc.volatility.skew_25d), with the
  strikes that bracket each delta.

Every answer carries its provenance (dataset, trade date, expiry, strikes, computation, METHOD_VERSION) and a
`citation` that save_claim re-runs (claims.save_claim, grade D when it reproduces the value). An unknown answer is
{"known": false, "reason": ...} and carries no number.

Scope (privacy): stock options are stored only for the stocks the user holds or watches (fnohist.store), so which
stock symbols answer would reveal the portfolio. These tools answer for the index underlyings and for the research
run's own NSE symbol only, and never list the stored underlyings. The module imports no personal package.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import Company, FnoBhav, FnoBhavDay, FnoIvEod, ResearchRun
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import volatility as vol
from finresearch.fincalc.dates import today_ist

INDEXES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")  # = fnohist.store.INDEX_OPTIONS (a test pins it)
METHOD_VERSION = "option_facts/1"
BHAV = "fno_bhav: NSE F&O bhavcopy, end-of-day settlement prices (stored, local)"
IV_EOD = "fno_iv_eod: daily IVs derived from fno_bhav by fincalc.fno_history (stored, local)"
RIGHTS = {"CE": "call", "PE": "put"}
TOOLS = ("option_implied_move", "option_iv30_percentile", "option_skew_25d")


def _unknown(reason: str) -> dict[str, Any]:
    return {"known": False, "reason": reason}


def _r(x: float, places: int = 4) -> str:
    return str(round(Decimal(str(x)), places))


def _scope(s: Session, run_id: int | None, symbol: str) -> str | None:
    """None when `symbol` may be answered for this run, else the (neutral) reason."""
    if symbol in INDEXES:
        return None
    run = s.get(ResearchRun, run_id) if run_id is not None else None
    co = s.get(Company, run.company_id) if run is not None else None
    if co is not None and (co.nse_symbol or "").upper() == symbol:
        return None
    return (f"{symbol} is not available to this run: these tools answer for {', '.join(INDEXES)} and the run's own "
            "NSE symbol only")  # fmt: skip


def _day(s: Session, symbol: str, trade_date: str | None) -> date | None:
    if trade_date:
        return date.fromisoformat(trade_date)
    return s.scalar(select(func.max(FnoBhav.day)).where(FnoBhav.symbol == symbol, FnoBhav.day <= today_ist()))


def _chain(s: Session, symbol: str, day: date):
    """(options by expiry, future settle by expiry, spot) of one stored underlying-day, filtered as derive_day does."""
    opts: dict[date, list[fh.OptQuote]] = defaultdict(list)
    futs: dict[date, float] = {}
    spot = None
    for kind, exp, strike, right, settle, close, contracts, und in s.execute(
        select(FnoBhav.instrument, FnoBhav.expiry, FnoBhav.strike, FnoBhav.option_type, FnoBhav.settle,
               FnoBhav.close, FnoBhav.contracts, FnoBhav.underlying)
        .where(FnoBhav.day == day, FnoBhav.symbol == symbol)
    ):  # fmt: skip
        if und is not None:
            spot = float(und)
        if settle is None or settle <= 0:
            continue
        if kind in ("IDF", "STF"):
            futs[exp] = float(settle)
        elif right in RIGHTS and fh.market_settle(float(settle), None if close is None else float(close), contracts):
            opts[exp].append(fh.OptQuote(float(strike), RIGHTS[right], float(settle)))
    return opts, futs, spot


def _source_url(s: Session, day: date) -> str | None:
    return s.scalar(select(FnoBhavDay.url).where(FnoBhavDay.day == day, FnoBhavDay.status == "loaded"))


def _parity_strike(quotes: list[fh.OptQuote]) -> float | None:
    """The strike fh.parity_forward uses (both sides traded, smallest |C - P|)."""
    calls = {q.strike: q.price for q in quotes if q.right == "call"}
    puts = {q.strike: q.price for q in quotes if q.right == "put"}
    both = [k for k in calls if k in puts]
    return min(both, key=lambda x: (abs(calls[x] - puts[x]), x)) if both else None


def _fitted(s: Session, symbol: str, day: date, expiry: str | None):
    """(ExpiryIv, its quotes, None) for the cited expiry, or the near one (the first fitted expiry at least
    fh.MIN_DTE days away); (None, None, reason) when it cannot be fitted."""
    opts, futs, spot = _chain(s, symbol, day)
    if not opts:
        return None, None, f"no traded {symbol} options with market settlement prices are stored for {day}"
    if expiry:
        e = date.fromisoformat(expiry)
        if e not in opts:
            return None, None, f"no traded {symbol} options expiring {e} with market settlement prices on {day}"
        x = fh.expiry_iv(day, e, opts[e], future=futs.get(e), spot=spot)
        if x is None:
            return None, None, (f"the {e} expiry on {day} cannot be fitted: no strike within "
                                f"{fh.ATM_BAND:.0%} of the forward with both call and put traded")  # fmt: skip
        return x, opts[e], None
    near = fh.day_summary(day, opts, futs, spot).near
    if near is None:
        return None, None, f"no {symbol} expiry at least {fh.MIN_DTE} days away could be fitted on {day}"
    return near, opts[near.expiry], None


def _provenance(s: Session, day: date, x: fh.ExpiryIv, strikes: list[float], computation: str) -> dict[str, Any]:
    return {"dataset": BHAV, "source_url": _source_url(s, day), "trade_date": day.isoformat(),
            "expiry": x.expiry.isoformat(), "days_to_expiry": x.dte, "forward": _r(x.forward, 2),
            "forward_source": x.forward_source, "strikes_used": strikes, "rate": fh.RATE,
            "computation": computation, "method_version": METHOD_VERSION}  # fmt: skip


def _citation(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"option_fact": {"tool": tool, "args": args}, "result_key": "<one of citable_keys>"}


def implied_move(s: Session, symbol: str, trade_date: str | None = None, expiry: str | None = None) -> dict[str, Any]:
    day = _day(s, symbol, trade_date)
    if day is None:
        return _unknown(f"no {symbol} F&O rows are stored")
    x, quotes, why = _fitted(s, symbol, day, expiry)
    if x is None:
        return _unknown(why)
    px = {(q.strike, q.right): q.price for q in quotes}
    t = x.dte / 365
    strikes = [x.atm_strike]
    if x.forward_source == "parity" and (k := _parity_strike(quotes)) is not None and k != x.atm_strike:
        strikes.append(k)
    straddle = px[(x.atm_strike, "call")] + px[(x.atm_strike, "put")]
    comp = (f"fincalc.fno_history.expiry_iv (Black-76 on the {x.forward_source} forward, rate {fh.RATE}, "
            f"T = {x.dte}/365): move_1sd_pct = ATM IV x sqrt(T); atm_straddle_pct = (C + P) / F at the ATM strike "
            "(settlement prices, discounted as traded)")  # fmt: skip
    return {"known": True, "symbol": symbol, "atm_iv_pct": _r(x.atm_iv), "move_1sd_pct": _r(x.atm_iv * math.sqrt(t)),
            "atm_straddle_pct": _r(straddle / x.forward * 100), "atm_straddle": _r(straddle, 2),
            "citable_keys": ["move_1sd_pct", "atm_straddle_pct", "atm_iv_pct"],
            "provenance": _provenance(s, day, x, strikes, comp),
            "citation": _citation("option_implied_move", {"symbol": symbol, "trade_date": day.isoformat(),
                                                          "expiry": x.expiry.isoformat()})}  # fmt: skip


_FNS = {"option_implied_move": implied_move}


def run(s: Session, tool: str, args: dict[str, Any], run_id: int | None = None) -> dict[str, Any]:
    """Run one option-fact tool (also save_claim's re-check). `args` may carry run_id, which sets the scope."""
    if tool not in _FNS:
        raise ValueError(f"unknown option fact tool {tool!r}; one of {sorted(_FNS)}")
    a = dict(args or {})
    run_id = a.pop("run_id", run_id)
    symbol = str(a.pop("symbol", "") or "").strip().upper()
    if not symbol:
        return _unknown("symbol is required")
    if (why := _scope(s, run_id, symbol)) is not None:
        return _unknown(why)
    try:
        return _FNS[tool](s, symbol, **a)
    except TypeError as e:
        raise ValueError(f"bad arguments for {tool}: {e}") from e
