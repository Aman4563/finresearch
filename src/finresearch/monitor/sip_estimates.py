"""The monitor's nightly SIP-estimate pass (#297, portfolio.sip_estimates): expected instalments after each fund's
statement units, with units estimated from that day's AMFI NAV.

Once per NSE trading day from RUN_AFTER (IST), after AMFI has published the day's NAVs (by about 23:00 IST,
monitor.portfolio_daily.NAV_AT) and after the daily NAV pass. Holidays and weekends are skipped; an unknown holiday
list skips the run too (monitor.market_days fails closed). Claimed in `alert_eval_slot` ("sip_estimates:<day>"), so
two monitor processes never run it twice; a failed run is retried (monitor.disclosures.MAX_ATTEMPTS).

Cost: one AMFI one-day NAV history request (all AMCs) per instalment day still without units (at most
sip_estimates.NAV_RETRY_DAYS back), plus NAVAll for today's instalments when the day's history is not out yet; and,
when Zerodha is connected with a live session, two read-only Kite GETs (/mf/sips, /mf/orders). Nothing here writes a
transaction or calls a model; personal data stays in the local database.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

RUN_AFTER = time(23, 30)


def due_slot(now: datetime, holidays: set[date] | None = None) -> str | None:
    from finresearch.monitor.market_days import market_day

    ist = to_ist(now)
    if ist.time() < RUN_AFTER or not market_day(ist.date(), holidays, job="SIP estimates"):
        return None
    return f"sip_estimates:{ist.date().isoformat()}"


@dataclass
class Sources:
    """What the pass reads; tests replace these. None = the live source."""

    navs: Callable[[date], Awaitable[list[Any]]] | None = None  # day -> AMFI rows (SchemeNav) of that day
    kite: Callable[[datetime], Awaitable[tuple[list[Any], list[Any]] | None]] | None = (
        None  # (sips, orders) | None
    )


async def _live_navs(day: date) -> list[Any]:
    from finresearch.adapters.amfi import AmfiClient
    from finresearch.fincalc.dates import today_ist

    async with AmfiClient() as amfi:
        rows = [r for r in await amfi.history(day, day) if r.day == day]
    if not rows and day == today_ist():  # the day's history can lag NAVAll on the evening itself
        from finresearch.signals import fund

        rows = [r for r in await fund._schemes() if r.day == day]
    return rows


async def _live_kite(now: datetime) -> tuple[list[Any], list[Any]] | None:
    """Zerodha's Coin SIPs and last-7-day MF orders when Zerodha is connected with a live session, else None (the
    pass never logs in: Kite's login is a browser step)."""
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection
    from finresearch.portfolio.connectors.base import ConnectorError, redact
    from finresearch.portfolio.connectors.store import build_from, token_valid

    with session_scope() as s:
        row = s.get(BrokerConnection, "zerodha")
        if row is None or not row.enabled or not token_valid(row, now):
            return None
        cols = (row.key, dict(row.config or {}), row.token)
    conn = await asyncio.to_thread(build_from, *cols)  # Keychain reads, off the event loop
    try:
        return await conn.mf_sips(), await conn.mf_activity()
    except ConnectorError as e:
        raise RuntimeError(f"Kite MF read failed: {redact(str(e), *conn.secret_values())}") from None


def plans_for(data: Any, today: date, sips: list[Any] | None) -> tuple[list[Any], dict[int, str], list[str]]:
    """(plans, {holding id: ISIN} the Kite orders may update, notes). A Kite SIP on a fund's ISIN is its basis when
    exactly one open fund holding has that ISIN (several folios of one fund: which folio it buys into is unknown, so
    those fall back to the inference and a note says so); a PAUSED one plans nothing. Other funds: the inference.
    Notes name holding ids, never fund names: the pass's result is logged and stored in alert_eval_slot."""
    from finresearch.portfolio import sip_estimates as se

    open_mf = [h for h in data.holdings if h.asset_type == "mf"
               and sum((lot.open_quantity for lot in data.lots.get(h.id, [])), Decimal(0)) > 0]  # fmt: skip
    per_isin = Counter((h.isin or "").upper() for h in open_mf if h.isin)
    by_isin = {}
    for x in sips or []:
        if x.isin:
            by_isin.setdefault(x.isin, []).append(x)
    plans, kite_isins, notes = [], {}, []
    for h in open_mf:
        isin = (h.isin or "").upper()
        mine = by_isin.get(isin) or []
        if mine and per_isin[isin] == 1:
            kite_isins[h.id] = isin
            got = [p for x in mine if (p := se.plan_kite(h, x, today))]
            plans += got[:1]  # one SIP per fund is the basis; a second Kite SIP on the same fund is noted
            if len(got) > 1:
                notes.append(f"holding {h.id}: {len(got)} active Kite SIPs, the first is the basis")
            continue
        if mine:
            notes.append(
                f"holding {h.id}: a Kite SIP on an ISIN held in {per_isin[isin]} folios: inferred instead"
            )
        if (p := se.plan_inferred(h, data.txns.get(h.id, []), today)) is not None:
            plans.append(p)
    return plans, kite_isins, notes


async def run(today: date, now: datetime, src: Sources | None = None,
              holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    from finresearch.db import session_scope
    from finresearch.portfolio import sip_estimates as se
    from finresearch.portfolio.report import load

    src = src or Sources()
    out: dict[str, Any] = {}
    kite = None
    try:
        kite = await (src.kite or _live_kite)(now)
    except Exception as e:  # the inference still runs; the failure is reported, never treated as "no SIPs"
        out["kite_failed"] = str(e)[:300]
    out["basis"] = "kite+inference" if kite is not None else "inference"
    if holidays is None:
        from finresearch.adapters.nse_holidays import known_trading_holidays

        holidays = known_trading_holidays(today) or set()
    with session_scope() as s:
        data = load(s)
        plans, kite_isins, notes = plans_for(data, today, kite[0] if kite else None)
        out["pruned"] = se.prune(s, plans)  # projections tonight's plans no longer contain
        out["planned"] = se.store_plans(s, plans, holidays)
        if kite is not None:
            out |= se.apply_kite_orders(s, kite_isins, kite[1])
        out |= await se.fill_navs(s, today, src.navs or _live_navs)
    if notes:
        out["notes"] = notes[:20]
    return out


async def sip_estimates_step(now: datetime, src: Sources | None = None, *,
                             holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now, holidays)
    if slot is None or not claim(slot, now):
        return {}
    try:
        res = await run(to_ist(now).date(), now, src, holidays)
    except Exception as e:
        log.warning("SIP estimates failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"sip_estimates": "failed"}
    finish(slot, res, now, failed=bool(res.get("nav_failed")))
    return {"sip_estimates": res}
