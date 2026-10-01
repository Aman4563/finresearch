"""The monitor's daily disclosure refresh (finresearch.disclosures): no intraday polling.

When (IST, weekdays):
- "evening" pass from EVENING (19:30): every market-wide feed (ASM, GSM, F&O ban list for the next trade date, credit
  ratings, SEBI orders) and each held or watched NSE stock's feeds (pledge, SAST, insider filings, bulk/block deals),
  at most MAX_STOCKS stocks with `spacing_s` between them. NSE publishes the next day's ban list, the day's bulk and
  block deals and the surveillance lists after the close, and insider filings arrive into the late evening;
- "morning" pass from MORNING (08:00): the three lists that gate trading today (ASM, GSM, F&O ban) again, before the
  08:30 brief and the 09:15 open, in case the evening read failed or NSE published late.

Each pass is claimed in `alert_eval_slot` ("disclosures:evening:<day>"), so two monitor processes never run it twice;
a failed pass (or one where a market-wide feed failed) is retried up to MAX_ATTEMPTS times, RETRY_AFTER apart. One
failed feed never stops the others (disclosures.refresh records it as unavailable).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from finresearch.db import session_scope
from finresearch.db.models import AlertEvalSlot
from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

EVENING = time(19, 30)
MORNING = time(8, 0)
MAX_STOCKS = 40
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=30)
SPACING_S = 1.5
MORNING_FEEDS = ("asm", "gsm", "fno_ban")


def due_passes(now: datetime) -> list[tuple[str, str]]:
    ist = to_ist(now)
    if ist.weekday() >= 5:
        return []
    day = ist.date().isoformat()
    out = []
    if MORNING <= ist.time() < time(9, 15):
        out.append(("morning", f"disclosures:morning:{day}"))
    if ist.time() >= EVENING:
        out.append(("evening", f"disclosures:evening:{day}"))
    return out


def claim(slot: str, now: datetime) -> bool:
    """A new slot, or a failed one whose retry time has come (at most MAX_ATTEMPTS)."""
    with session_scope() as s:
        got = s.execute(insert(AlertEvalSlot).values(slot=slot, started_at=now, result={"status": "running", "attempts": 1})
                        .on_conflict_do_nothing(index_elements=["slot"]).returning(AlertEvalSlot.slot)).first()  # fmt: skip
        if got is not None:
            return True
        row = s.scalars(
            select(AlertEvalSlot).where(AlertEvalSlot.slot == slot).with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return False
        r = dict(row.result or {})
        if r.get("status") != "failed" or int(r.get("attempts", 1)) >= MAX_ATTEMPTS:
            return False
        if r.get("retry_at") and datetime.fromisoformat(r["retry_at"]) > now:
            return False
        row.result = {**r, "status": "running", "attempts": int(r.get("attempts", 1)) + 1}
        return True


def finish(slot: str, result: dict[str, Any], now: datetime, failed: bool) -> None:
    with session_scope() as s:
        row = s.get(AlertEvalSlot, slot)
        if row is None:
            return
        attempts = int((row.result or {}).get("attempts", 1))
        row.result = {"status": "failed" if failed else "done", "attempts": attempts, **result,
                      **({"retry_at": (now + RETRY_AFTER).isoformat()} if failed else {})}  # fmt: skip


async def run_pass(kind: str, now: datetime, *, spacing_s: float = SPACING_S,
                   retry_of: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """One pass. A retry (`retry_of` = the failed attempt's result) re-reads only what failed."""
    from finresearch.disclosures import refresh

    feeds = MORNING_FEEDS if kind == "morning" else refresh.MARKET_DATASETS
    if retry_of and retry_of.get("market_failed") is not None:
        feeds = tuple(retry_of["market_failed"])
    async with refresh._client() as d:  # one NSE session (one warm-up) for the whole pass
        return await _run(kind, now, feeds, d, spacing_s, retry_of)


async def _run(kind: str, now: datetime, feeds: tuple[str, ...], d: Any, spacing_s: float,
               retry_of: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    from finresearch.disclosures import refresh, store

    out: dict[str, Any] = {"market": await refresh.refresh_market(now, feeds, client=d) if feeds else {}}
    if kind == "evening":
        with session_scope() as s:
            t = store.tracked(s)
        # held stocks first, then watched; capped (the rest are read when their page is opened)
        order = sorted(t.stocks.items(), key=lambda kv: (not kv[1]["held"], kv[0]))[:MAX_STOCKS]
        redo = (retry_of or {}).get("stocks_failed")
        if redo is not None:
            order = [(k, v) for k, v in order if k in redo]
        failed: dict[str, list[str]] = {}
        for n, (sym, v) in enumerate(order):
            if n and spacing_s:
                await asyncio.sleep(spacing_s)
            datasets = tuple(redo[sym]) if redo is not None else refresh.STOCK_DATASETS
            res = await refresh.refresh_stock(sym, now, isin=v.get("isin"), datasets=datasets, client=d)
            if bad := [ds for ds, st in res.items() if st != "ok"]:
                failed[sym] = bad
        out["stocks_failed"] = failed
        out["stocks"] = len(order)
        out["skipped"] = max(0, len(t.stocks) - MAX_STOCKS)
    return out


async def disclosures_step(now: datetime, *, spacing_s: float = SPACING_S) -> dict[str, Any]:
    """The monitor's disclosure work for this tick: run whichever pass is due and not yet done."""
    out: dict[str, Any] = {}
    for kind, slot in due_passes(now):
        if not claim(slot, now):
            continue
        with session_scope() as s:
            row = s.get(AlertEvalSlot, slot)
            prior = dict(row.result or {}) if row else {}
        retry_of = prior if int(prior.get("attempts", 1)) > 1 and "market_failed" in prior else None
        try:
            res = await run_pass(kind, now, spacing_s=spacing_s, retry_of=retry_of)
        except Exception as e:
            log.warning("disclosure %s pass failed; retried later", kind, exc_info=True)
            finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
            out[kind] = "failed"
            continue
        market_failed = [ds for ds, st in res["market"].items() if st != "ok"]
        stocks_failed = res.get("stocks_failed") or {}
        finish(slot, {"market_failed": market_failed, "stocks_failed": stocks_failed}, now,
               failed=bool(market_failed or stocks_failed))  # fmt: skip
        out[kind] = res
    return out
