"""The forward quote recorder (#363, P1.1): option quotes at registered times, for a measured cost model.

What: NIFTY and BANKNIFTY, the near and the next listed expiry (Groww's instruments file decides: NIFTY lists weekly
expiries, BANKNIFTY monthly), the 10 listed strikes either side of the one nearest the index LTP, calls and puts:
2 x 2 x 21 x 2 = 168 contracts per snapshot (fnohist.quotes.PER_SNAPSHOT).

When (IST, trading days; an unknown holiday list skips the day, monitor.market_days fails closed): from each of
fnohist.quotes.SNAPSHOT_TIMES (09:30, 12:00, 15:15, 15:25) for WINDOW. A sweep is claimed in `alert_eval_slot`
("quotes:<day>:<HHMM>", monitor.disclosures.claim/finish), so two monitor processes never record it twice. One
attempt per slot: a failed sweep's retry time (disclosures.RETRY_AFTER, 30 minutes) falls after its window closes,
and a quote taken late would no longer be the registered snapshot. Quotes are forward-only data: a missed slot is
lost and shows up in the day's checks.

How: one Groww LTP call for both indices, then one Get Quote call per contract, in sequence. Every call takes a slot
in Groww's shared Live Data bucket (adapters.groww_budget: 0.2 s apart for every process), so the monitor's LTP and
chain reads are never pushed past Groww's 300 a minute; on top of that the recorder waits SPACING_S between its own
calls, so it uses at most 240 of the 300 calls a minute and leaves the rest to the app's other Live Data reads. A
sweep is 169 calls (~50 s), 676 Groww calls a day. Each sweep stores its rows (fnohist.quotes.store) and its
data-quality checks (fnohist.quotes.checks) in the slot's result.

Fallback (`Deps.quotes_nse_fallback`, off unless switched on): when Groww cannot answer a sweep at all (no session,
no instruments file, no index LTP), NSE's option chain for each underlying and expiry: at most 4 requests a sweep,
16 a day, rows `source` "nse" (bid/ask from buyPrice1/sellPrice1, no quantities or depth). NSE's website terms
restrict automated collection (#362), so this is the owner's decision.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import IST, to_ist
from finresearch.fnohist import quotes as q
from finresearch.fnohist.quotes import EXPIRIES, SNAPSHOT_TIMES, STRIKES_EACH_SIDE, UNDERLYINGS

log = logging.getLogger(__name__)

# 15:25 + 5 min ends at the 15:30 close; 15:15 + 5 min ends before the 15:25 snapshot
WINDOW = timedelta(minutes=5)
SPACING_S = 0.25  # between the recorder's own calls: at most 240 of Groww's 300 Live Data calls a minute
SWEEP_TIMEOUT_S = 240.0  # a hung request never holds the background task; rows read until then are kept


@dataclass(frozen=True)
class Contract:
    underlying: str
    expiry: date
    strike: Decimal
    kind: str  # CE | PE
    trading_symbol: str | None  # None: not in Groww's instruments file (counted as a missing row)


def pick(strikes: list[Decimal], spot: Decimal) -> list[Decimal]:
    """The listed strike nearest `spot` (ties: the lower) and STRIKES_EACH_SIDE listed strikes either side."""
    s = sorted(strikes)
    if not s:
        return []
    i = min(range(len(s)), key=lambda j: (abs(s[j] - spot), s[j]))
    return s[max(0, i - STRIKES_EACH_SIDE) : i + STRIKES_EACH_SIDE + 1]


def select_contracts(inst: Any, underlying: str, spot: Decimal, today: date) -> list[Contract]:
    """The sweep's contracts for one underlying from Groww's instruments file (adapters.groww_market.Instruments)."""
    by_exp = inst.option_strikes(underlying)
    out = []
    for exp in sorted(e for e in by_exp if e >= today)[:EXPIRIES]:
        for k in pick(list(by_exp[exp]), spot):
            for kind in ("CE", "PE"):
                out.append(Contract(underlying, exp, k, kind, inst.fno_symbol(underlying, exp, k, kind)))
    return out


def due_slot(now: datetime, holidays: set[date] | None = None) -> tuple[datetime, str] | None:
    """(registered snapshot time, slot key) when a snapshot's window is open on a trading day, else None."""
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    for t in SNAPSHOT_TIMES:
        snap = datetime.combine(ist.date(), t, IST)
        if snap <= ist < snap + WINDOW:
            if not market_day(ist.date(), holidays, job="quote recorder"):
                return None
            return snap, f"quotes:{ist.date().isoformat()}:{t.hour:02d}{t.minute:02d}"
    return None


def _row(c: Contract, snap: datetime, requested_at: datetime, spot: Decimal | None, cols: dict[str, Any],
         source: str) -> dict[str, Any]:  # fmt: skip
    r = {"snapshot_at": snap, "underlying": c.underlying, "expiry": c.expiry, "strike": q.paise(c.strike),
         "option_type": c.kind, "requested_at": requested_at, "underlying_ltp": q.paise(spot), "source": source,
         **cols}  # fmt: skip
    r["flags"] = q.flags(r, requested_at)
    return r


async def sweep_groww(market: Any, snap: datetime, rows: list[dict[str, Any]], out: dict[str, Any],
                      spacing_s: float = SPACING_S) -> None:  # fmt: skip
    """Read one snapshot from Groww into `rows` (appended as they arrive, so a timeout keeps them). Raises
    ConnectorError when Groww cannot answer the sweep at all (no session, no instruments file, no index LTP)."""
    from finresearch.adapters.groww_market import exchange_symbol
    from finresearch.portfolio.connectors.base import ConnectorError

    if not market.token():
        raise ConnectorError("no valid Groww session")
    inst = await market.instruments()
    if inst is None:
        raise ConnectorError("Groww's instruments file is not available")
    keys = {}
    for sym, index_name in UNDERLYINGS:
        row = inst.index(index_name)
        if row is not None:
            keys[sym] = exchange_symbol(row)
    ltp = await market.ltp(list(keys.values()))
    spots = {sym: Decimal(str(ltp[k][0])) for sym, k in keys.items() if k in ltp}
    if not spots:
        raise ConnectorError("Groww gave no index LTP")
    today = to_ist(snap).date()
    contracts = [
        c for sym, _ in UNDERLYINGS if sym in spots for c in select_contracts(inst, sym, spots[sym], today)
    ]
    out["unresolved"] = [
        f"{c.underlying} {c.expiry} {c.strike} {c.kind}" for c in contracts if not c.trading_symbol
    ]
    out["failed"] = []
    for n, c in enumerate(x for x in contracts if x.trading_symbol):
        if n and spacing_s:
            await asyncio.sleep(spacing_s)
        requested_at = market.clock()
        try:
            payload = await market.quote(c.trading_symbol)
        except Exception as e:  # one contract: a missing row, counted by the checks
            log.info("Groww quote for %s failed: %s", c.trading_symbol, type(e).__name__)
            out["failed"].append(c.trading_symbol)
            continue
        rows.append(_row(c, snap, requested_at, spots[c.underlying], q.parse_groww(payload), "groww"))


async def sweep_nse(nse_chain: Any, market: Any, snap: datetime, rows: list[dict[str, Any]],
                    out: dict[str, Any]) -> None:  # fmt: skip
    """The fallback: one NSE chain per underlying and expiry (at most len(UNDERLYINGS) x EXPIRIES requests), the
    same ATM +/-10 strikes picked from the chain's own rows and underlying value. Expiries come from Groww's
    instruments file, which needs no session. NSE's chain gives no per-contract trade time: `exchange_at` is the
    chain's own timestamp, so the ATM freshness check on these rows ages the chain, not the last trade."""
    from finresearch.portfolio.connectors.base import ConnectorError

    inst = await market.instruments()
    if inst is None:
        raise ConnectorError("no instruments file for the NSE fallback's expiries")
    today, out["nse_requests"] = to_ist(snap).date(), 0
    for sym, _ in UNDERLYINGS:
        for exp in sorted(e for e in inst.option_strikes(sym) if e >= today)[:EXPIRIES]:
            requested_at = market.clock()
            out["nse_requests"] += 1
            try:
                chain = await nse_chain(sym, exp)
            except Exception as e:
                log.info("NSE chain for %s %s failed: %s", sym, exp, type(e).__name__)
                continue
            if chain is None or chain.underlying is None:
                continue
            by_strike = {r.strike: r for r in chain.rows}
            for k in pick(list(by_strike), chain.underlying):
                for kind, oq in (("CE", by_strike[k].call), ("PE", by_strike[k].put)):
                    if oq is None:
                        continue
                    cols = {"bid": q.paise(oq.bid), "ask": q.paise(oq.ask), "bid_qty": None, "ask_qty": None,
                            "depth_bid": None, "depth_ask": None, "ltp": q.paise(oq.last_price),
                            "oi": int(oq.oi) if oq.oi is not None else None,
                            "volume": int(oq.volume) if oq.volume is not None else None,
                            "iv": float(oq.iv) if oq.iv else None, "exchange_at": chain.as_of}  # fmt: skip
                    rows.append(_row(Contract(sym, exp, k, kind, None), snap, requested_at, chain.underlying, cols,
                                     "nse"))  # fmt: skip


async def quotes_step(now: datetime, *, market: Any = None, nse_chain: Any = None,
                      holidays: set[date] | None = None, spacing_s: float = SPACING_S) -> dict[str, Any]:  # fmt: skip
    """The monitor's quote work for this tick: claim the due snapshot's slot, sweep Groww (NSE's chain when
    `nse_chain` is given and Groww cannot answer at all), store the rows and the sweep's checks."""
    from finresearch.monitor.disclosures import claim, finish

    due = due_slot(now, holidays)
    if due is None or not claim(due[1], now):
        return {}
    snap, slot = due
    if market is None:
        from finresearch.adapters.groww_market import MARKET as market
    rows: list[dict[str, Any]] = []
    out: dict[str, Any] = {"source": "groww"}
    try:
        await asyncio.wait_for(sweep_groww(market, snap, rows, out, spacing_s), SWEEP_TIMEOUT_S)
    except TimeoutError:
        out["error"] = f"sweep timed out after {SWEEP_TIMEOUT_S:.0f} s"
    except Exception as e:
        out["error"] = f"Groww: {type(e).__name__}: {e}"[:300]
        if nse_chain is not None and not rows:
            out["source"] = "nse"
            try:
                await asyncio.wait_for(sweep_nse(nse_chain, market, snap, rows, out), SWEEP_TIMEOUT_S)
            except Exception as e2:
                out["error"] += f"; NSE: {type(e2).__name__}: {e2}"[:200]
    try:
        out["stored"] = q.store(rows)
    except Exception as e:
        log.warning("quote snapshot %s not stored", slot, exc_info=True)
        finish(slot, {**out, "error": f"store: {type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"quotes": "failed"}
    if not rows:
        log.warning("quote snapshot %s recorded nothing: %s", slot, out.get("error"))
        finish(slot, out, now, failed=True)
        return {"quotes": "failed"}
    out["checks"] = q.checks(rows, q.PER_SNAPSHOT)
    if not out["checks"]["ok"]:
        log.warning("quote snapshot %s failed its checks: %s", slot, out["checks"])
    finish(slot, out, now, failed=False)
    return {"quotes": {"stored": out["stored"], "ok": out["checks"]["ok"], "source": out["source"]}}
