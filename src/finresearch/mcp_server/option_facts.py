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

import itertools
import math
import os
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
from finresearch.fincalc.options import greeks
from finresearch.mcp_server.config import RUN_ENV  # set per agent task by write_mcp_config(run_id=...)

INDEXES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")  # = fnohist.store.INDEX_OPTIONS (a test pins it)
METHOD_VERSION = "option_facts/1"
BHAV = "fno_bhav: NSE F&O bhavcopy, end-of-day settlement prices (stored, local)"
IV_EOD = "fno_iv_eod: daily IVs derived from fno_bhav by fincalc.fno_history (stored, local)"
RIGHTS = {"CE": "call", "PE": "put"}
TOOLS = ("option_implied_move", "option_iv30_percentile", "option_skew_25d")


def bound_run_id() -> int | None:
    """The research run this MCP server process serves (agents.runner writes it into the server's environment)."""
    v = os.environ.get(RUN_ENV, "").strip()
    return int(v) if v.isdigit() else None


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
        elif right in RIGHTS and fh.market_settle(
            float(settle), None if close is None else float(close), contracts
        ):
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
            return (
                None,
                None,
                f"no traded {symbol} options expiring {e} with market settlement prices on {day}",
            )
        x = fh.expiry_iv(day, e, opts[e], future=futs.get(e), spot=spot)
        if x is None:
            return None, None, (f"the {e} expiry on {day} cannot be fitted: no strike within "
                                f"{fh.ATM_BAND:.0%} of the forward with both call and put traded")  # fmt: skip
        return x, opts[e], None
    near = fh.day_summary(day, opts, futs, spot).near
    if near is None:
        return None, None, f"no {symbol} expiry at least {fh.MIN_DTE} days away could be fitted on {day}"
    return near, opts[near.expiry], None


def _provenance(
    s: Session, day: date, x: fh.ExpiryIv, strikes: list[float], computation: str
) -> dict[str, Any]:
    return {"dataset": BHAV, "source_url": _source_url(s, day), "trade_date": day.isoformat(),
            "expiry": x.expiry.isoformat(), "days_to_expiry": x.dte, "forward": _r(x.forward, 2),
            "forward_source": x.forward_source, "strikes_used": strikes, "rate": fh.RATE,
            "computation": computation, "method_version": METHOD_VERSION}  # fmt: skip


def _citation(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"option_fact": {"tool": tool, "args": args}, "result_key": "<one of citable_keys>"}


def implied_move(
    s: Session, symbol: str, trade_date: str | None = None, expiry: str | None = None
) -> dict[str, Any]:
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


def iv30_percentile(s: Session, symbol: str, trade_date: str | None = None) -> dict[str, Any]:
    day = (
        date.fromisoformat(trade_date)
        if trade_date
        else s.scalar(
            select(func.max(FnoIvEod.day)).where(FnoIvEod.symbol == symbol, FnoIvEod.day <= today_ist())
        )
    )
    if day is None:
        return _unknown(f"no {symbol} implied-volatility rows are stored")
    sessions = list(s.scalars(select(FnoBhavDay.day).where(FnoBhavDay.status == "loaded", FnoBhavDay.day <= day)
                              .order_by(FnoBhavDay.day.desc()).limit(vol.WINDOW)))[::-1]  # fmt: skip
    if not sessions or sessions[-1] != day:
        return _unknown(f"{day} is not a loaded F&O bhavcopy session")
    ivs = dict(s.execute(select(FnoIvEod.day, FnoIvEod.iv_30d).where(
        FnoIvEod.symbol == symbol, FnoIvEod.day.between(sessions[0], day))).all())  # fmt: skip
    st = vol.iv_stats_sessions([(d, None if ivs.get(d) is None else float(ivs[d])) for d in sessions])
    if st.status != "ok" or st.percentile is None or st.rank is None:
        return _unknown(f"the {symbol} IV30 percentile on {day} is unknown: {st.status}")
    method = s.scalar(select(FnoIvEod.iv_30d_method).where(FnoIvEod.symbol == symbol, FnoIvEod.day == day))
    comp = (f"fincalc.volatility.iv_stats_sessions over the last {vol.WINDOW} loaded bhavcopy sessions to the trade "
            f"date (one point per session, missing IVs left out; unknown below {vol.MIN_COVERAGE:.0%} coverage): "
            "percentile = share of earlier IVs in the window below the trade date's x 100; rank = (IV - min) / "
            "(max - min) x 100")  # fmt: skip
    prov = {"dataset": IV_EOD, "source_url": _source_url(s, day), "trade_date": day.isoformat(),
            "iv_30d_method": method, "window_first": sessions[0].isoformat(), "window_last": day.isoformat(),
            "sessions": len(sessions), "n": st.n, "coverage": _r(st.n / vol.WINDOW, 3), "low": _r(st.low, 3),
            "high": _r(st.high, 3), "computation": comp, "method_version": METHOD_VERSION}  # fmt: skip
    return {"known": True, "symbol": symbol, "iv30_pct": _r(st.current, 3), "iv30_percentile": _r(st.percentile * 100),
            "iv30_rank": _r(st.rank * 100), "citable_keys": ["iv30_percentile", "iv30_rank", "iv30_pct"],
            "provenance": prov,
            "citation": _citation("option_iv30_percentile", {"symbol": symbol, "trade_date": day.isoformat()})}  # fmt: skip


def _bracket(points: list[tuple[float, float]], target: float) -> list[float] | None:
    """The two strikes whose (delta, strike) points bracket `target` (as volatility._iv_at_delta interpolates)."""
    pts = sorted(points)
    for (d0, k0), (d1, k1) in itertools.pairwise(pts):
        if d0 <= target <= d1:
            return sorted({k0, k1})
    return None


def skew_25d(s: Session, symbol: str, trade_date: str | None = None) -> dict[str, Any]:
    day = _day(s, symbol, trade_date)
    if day is None:
        return _unknown(f"no {symbol} F&O rows are stored")
    x, quotes, why = _fitted(s, symbol, day, None)
    if x is None:
        return _unknown(why)
    if x.skew_25d is None:
        return _unknown(f"the {x.expiry} chain on {day} does not span both 25-delta points")
    t, f = x.dte / 365, x.forward
    deltas: dict[str, list[tuple[float, float]]] = {"call": [], "put": []}
    for q in quotes:
        iv = fh.black76_iv(q.right, q.price, f, q.strike, t, fh.RATE)
        if iv is not None and 0.005 < iv < 4.99:  # as fh.expiry_iv
            deltas[q.right].append((greeks(q.right, f, q.strike, t, fh.RATE, iv, fh.RATE).delta, q.strike))
    calls, puts = _bracket(deltas["call"], 0.25), _bracket(deltas["put"], -0.25)
    stored = s.scalar(select(FnoIvEod.skew_25d).where(FnoIvEod.symbol == symbol, FnoIvEod.day == day))
    comp = (f"fincalc.volatility.skew_25d on the near expiry (first fitted expiry >= {fh.MIN_DTE} days): Black-76 "
            "IV per traded strike, each strike's delta at its own IV, IV linear in delta at +0.25 (calls) and "
            "-0.25 (puts); skew = put IV - call IV in volatility points")  # fmt: skip
    prov = _provenance(s, day, x, sorted({*(calls or []), *(puts or [])}), comp)
    prov.update({"call_25d_strikes": calls, "put_25d_strikes": puts,
                 "stored_skew_25d": None if stored is None else str(stored)})  # fmt: skip
    return {"known": True, "symbol": symbol, "skew_25d": _r(x.skew_25d), "citable_keys": ["skew_25d"],
            "provenance": prov,
            "citation": _citation("option_skew_25d", {"symbol": symbol, "trade_date": day.isoformat()})}  # fmt: skip


_FNS = {
    "option_implied_move": implied_move,
    "option_iv30_percentile": iv30_percentile,
    "option_skew_25d": skew_25d,
}


def run(s: Session, tool: str, args: dict[str, Any], run_id: int | None = None) -> dict[str, Any]:
    """Run one option-fact tool (also save_claim's re-check). `run_id` is the trusted run that sets the scope: the
    server's binding (bound_run_id) or the claim's own run, never a value from the agent's arguments."""
    if tool not in _FNS:
        raise ValueError(f"unknown option fact tool {tool!r}; one of {sorted(_FNS)}")
    a = dict(args or {})
    if "run_id" in a:
        raise ValueError("run_id is not an option-fact argument: the run is bound by the server")
    symbol = str(a.pop("symbol", "") or "").strip().upper()
    if not symbol:
        return _unknown("symbol is required")
    if (why := _scope(s, run_id, symbol)) is not None:
        return _unknown(why)
    try:
        return _FNS[tool](s, symbol, **a)
    except TypeError as e:
        raise ValueError(f"bad arguments for {tool}: {e}") from e
