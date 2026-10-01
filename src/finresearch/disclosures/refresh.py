"""Fetch disclosure feeds and store them. Every feed is read in its own try: one failure is recorded against that
feed ("unavailable" in the views, with the error) and never blanks the others or the last good read.

Request budget (all through the polite NSE client: one cookie session, NSE's 2 requests/second limit, plus
`spacing_s` between stocks in the daily pass):
- market-wide, once a day: ASM, GSM, F&O ban CSV, credit ratings (~0.5 MB), SEBI RSS: 5 requests;
- per held or watched NSE stock: pledge, SAST, the PIT filing index for 90 days, bulk and block deals for 90 days
  (5 requests) plus each PIT filing's XBRL not read before (at most MAX_FILINGS per stock per pass; filings are
  immutable and cached on disk).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from finresearch.adapters.nse_disclosures import NseDisclosures, issuer_code, window
from finresearch.db import session_scope
from finresearch.disclosures import store
from finresearch.fincalc.dates import to_ist
from finresearch.fincalc.disclosures import trade_day

log = logging.getLogger(__name__)

MARKET_DATASETS = ("asm", "gsm", "fno_ban", "credit_ratings", "sebi_orders")
STOCK_DATASETS = ("pledge", "pit", "sast", "deals")
PIT_DAYS = DEALS_DAYS = 90
MAX_FILINGS = 25  # PIT XBRLs read per stock per pass; the rest are read on the next pass ("pending")
MAX_FILINGS_ON_VIEW = 8  # a page view reads fewer (it waits on them); the remainder shows as "pending"
NEGATIVE_CACHE = timedelta(
    seconds=30
)  # a failed read is not retried sooner (unless the caller asks), DATA-004
FRESH = timedelta(hours=20)  # a good read younger than this is not re-fetched for a page view


@dataclass
class Sources:
    """Test seam: `client` builds the NSE disclosures client (an async context manager); `sebi` returns the parsed
    RSS ((orders, last build, Fetched)). None = live."""

    client: Callable[[], NseDisclosures] | None = None
    sebi: Callable[[], Awaitable[Any]] | None = None


SOURCES = Sources()


def _client() -> NseDisclosures:
    return (SOURCES.client or NseDisclosures)()


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"[:400]


# --------------------------------------------------------------------------- market-wide feeds
async def refresh_market(now: datetime, datasets: tuple[str, ...] = MARKET_DATASETS,
                         client: NseDisclosures | None = None) -> dict[str, str]:  # fmt: skip
    """Read the market-wide feeds; {dataset: "ok" | error}."""
    out: dict[str, str] = {}
    own = client is None
    d = client or _client()
    try:
        for ds in datasets:
            try:
                if ds == "sebi_orders":
                    await _sebi(now)
                else:
                    await _market_one(d, ds, now)
                out[ds] = "ok"
            except Exception as e:
                log.info("disclosure feed %s failed: %s", ds, e)
                with session_scope() as s:
                    store.record_error(s, ds, store.MARKET, _err(e), now)
                out[ds] = _err(e)
    finally:
        if own:
            await d.nse.aclose()
    return out


async def _market_one(d: NseDisclosures, ds: str, now: datetime) -> None:
    if ds in ("asm", "gsm"):
        f = await (d.asm() if ds == "asm" else d.gsm())
        published = max(
            (r.published_at for r in f.items if r.published_at), default=None
        )  # NSE's asmTime/gsmTime
        with session_scope() as s:
            store.record_ok(s, ds, store.MARKET, payload={"rows": f.items}, url=f.url,
                            as_of=to_ist(published or f.fetched_at).date().isoformat(), now=now)  # fmt: skip
    elif ds == "fno_ban":
        f = await d.fno_ban()
        with session_scope() as s:
            store.record_ok(s, ds, store.MARKET, payload=f.items, url=f.url, as_of=f.items.trade_date.isoformat(),
                            now=now)  # fmt: skip
    elif ds == "credit_ratings":
        f = await d.credit_ratings()
        rows = [{"symbol": r.symbol, "isin": r.isin, "issuer": issuer_code(r.isin), "day": r.rating_day,
                 "key": (r.isin, r.agency, r.rating_day, r.rating, r.action_raw, r.outlook, r.broadcast_at),
                 "data": r, "source_url": f.url} for r in f.items]  # fmt: skip
        with session_scope() as s:
            new = store.upsert_records(s, "rating", rows)
            latest = max((r.broadcast_at for r in f.items if r.broadcast_at), default=None)
            store.record_ok(s, ds, store.MARKET, payload={"n": len(f.items), "new": new}, url=f.url,
                            as_of=(latest or f.fetched_at).isoformat(), now=now)  # fmt: skip
    else:
        raise ValueError(f"unknown market feed {ds!r}")


async def _sebi(now: datetime) -> None:
    from finresearch.adapters.sebi_orders import RSS_URL, fetch_orders, match_orders

    orders, built, _resp = await (SOURCES.sebi or fetch_orders)()
    with session_scope() as s:
        names = company_names(s)
        hits = match_orders(orders, names)
        rows = [{"symbol": m.key, "day": m.order.day, "key": (m.order.link, m.key),
                 "data": {**m.order.model_dump(), "matched_name": m.name}, "source_url": m.order.link}
                for m in hits]  # fmt: skip
        new = store.upsert_records(s, "sebi_order", rows)
        # only counts are kept for the feed itself: unmatched titles (often private individuals) are never stored
        store.record_ok(s, "sebi_orders", store.MARKET, payload={"items": len(orders), "matched": len(hits), "new": new},
                        url=RSS_URL, as_of=(built or now).isoformat(), now=now)  # fmt: skip


def company_names(s: Any) -> dict[str, str]:
    """{instrument key: legal name} of tracked stocks, plus tracked bonds' issuers (the issuer name NSE's rating
    filings print for the bond's ISIN issuer code)."""
    from finresearch.db.models import DisclosureRecord

    t = store.tracked(s)
    out = {k: v["name"] for k, v in [*t.stocks.items(), *t.bse_only.items()] if v.get("name")}
    for isin in t.bonds:
        code = issuer_code(isin)
        if code is None:
            continue
        rec = (
            s.query(DisclosureRecord)
            .filter(DisclosureRecord.dataset == "rating", DisclosureRecord.issuer == code)
            .first()
        )
        name = (rec.data or {}).get("company") if rec else None
        if name:
            out[isin] = name
    return out


# --------------------------------------------------------------------------- per-stock feeds
async def refresh_stock(symbol: str, now: datetime, *, isin: str | None = None,
                        datasets: tuple[str, ...] = STOCK_DATASETS, client: NseDisclosures | None = None,
                        max_filings: int | None = None) -> dict[str, str]:  # fmt: skip
    """Read one NSE stock's feeds; {dataset: "ok" | error}."""
    symbol = symbol.upper()
    out: dict[str, str] = {}
    own = client is None
    d = client or _client()
    today = to_ist(now).date()
    try:
        for ds in datasets:
            try:
                if ds == "pit":
                    await _pit(d, symbol, isin, today, now, max_filings=max_filings or MAX_FILINGS)
                else:
                    await {"pledge": _pledge, "sast": _sast, "deals": _deals}[ds](d, symbol, isin, today, now)
                out[ds] = "ok"
            except Exception as e:
                log.info("disclosure feed %s for %s failed: %s", ds, symbol, e)
                with session_scope() as s:
                    store.record_error(s, ds, symbol, _err(e), now)
                out[ds] = _err(e)
    finally:
        if own:
            await d.nse.aclose()
    return out


async def _pledge(d: NseDisclosures, sym: str, isin: str | None, today, now: datetime) -> None:
    f = await d.pledge(sym)
    p = f.items
    with session_scope() as s:
        if p is not None and p.quarter_end is not None:
            store.upsert_records(s, "pledge", [{"symbol": sym, "isin": isin, "day": p.quarter_end,
                                                "key": (sym, p.quarter_end), "data": p, "source_url": f.url}],
                                 update=True)  # fmt: skip
        store.record_ok(s, "pledge", sym, payload={"latest": p, "no_record": p is None}, url=f.url,
                        as_of=p.quarter_end.isoformat() if p and p.quarter_end else None, now=now)  # fmt: skip


async def _pit(
    d: NseDisclosures, sym: str, isin: str | None, today, now: datetime, *, max_filings: int
) -> None:
    start, end = window(today, PIT_DAYS)
    idx = await d.pit_index(sym, start, end)
    with session_scope() as s:
        prev = store.feed(s, "pit", sym)
        read_before = set((prev.payload or {}).get("read") or []) if prev else set()
    filings = idx.items
    urls = {f.xml_url for f in filings if f.xml_url}
    read = {u for u in read_before if u in urls}
    failed: list[dict[str, str]] = []
    todo = [f for f in filings if f.xml_url and f.xml_url not in read][:max_filings]
    for f in todo:
        try:
            txns = await d.pit_filing(f.xml_url)
        except Exception as e:
            failed.append({"url": f.xml_url, "error": _err(e)[:200]})
            continue
        rows = []
        for i, t in enumerate(txns):
            data = {**t.model_dump(), "app_id": f.app_id, "prev_app_id": f.prev_app_id, "submission": f.submission,
                    "broadcast_at": f.broadcast_at, "filing_url": f.html_url or f.xml_url}  # fmt: skip
            rows.append({"symbol": sym, "isin": t.isin or isin, "day": trade_day(t.model_dump()),
                         "key": (sym, f.app_id, f.xml_url, i), "data": data, "source_url": f.xml_url})  # fmt: skip
        with session_scope() as s:
            store.upsert_records(s, "pit", rows)
        read.add(f.xml_url)
    no_xbrl = sum(1 for f in filings if not f.xml_url)
    pending = len(urls - read) - len(failed)
    with session_scope() as s:
        store.record_ok(s, "pit", sym, payload={"window": [start, end], "filings": len(filings), "read": sorted(read),
                                                "failed": failed, "pending": max(0, pending), "no_xbrl": no_xbrl},
                        url=idx.url, as_of=end.isoformat(), now=now)  # fmt: skip


async def _sast(d: NseDisclosures, sym: str, isin: str | None, today, now: datetime) -> None:
    f = await d.sast29(sym)
    rows = [{"symbol": sym, "isin": isin, "day": t.day,
             "key": (sym, t.acquirer, t.day, t.kind, t.shares, t.pct, t.filed_at), "data": t, "source_url": f.url}
            for t in f.items]  # fmt: skip
    with session_scope() as s:
        store.upsert_records(s, "sast", rows)
        store.record_ok(s, "sast", sym, payload={"n": len(rows)}, url=f.url, as_of=today.isoformat(), now=now)


async def _deals(d: NseDisclosures, sym: str, isin: str | None, today, now: datetime) -> None:
    start, end = window(today, DEALS_DAYS)
    rows, urls = [], []
    for kind in ("bulk", "block"):
        f = await d.deals(sym, kind, start, end)
        urls.append(f.url)
        rows += [{"symbol": sym, "isin": isin, "day": x.day,
                  "key": (kind, x.day, sym, x.client, x.side, x.quantity, x.price),
                  "data": {**x.model_dump(), "value_inr": x.value_inr}, "source_url": f.url} for x in f.items]  # fmt: skip
    with session_scope() as s:
        store.upsert_records(s, "deal", rows)
        store.record_ok(s, "deals", sym, payload={"window": [start, end], "n": len(rows), "urls": urls}, url=urls[0],
                        as_of=end.isoformat(), now=now)  # fmt: skip


# --------------------------------------------------------------------------- on demand (a stock page)
_LOCKS: dict[str, asyncio.Lock] = {}


def _due(row: Any, now: datetime, retry: bool, *, fresh: timedelta = FRESH) -> bool:
    if row is None or row.ok_at is None:
        recent_fail = row is not None and row.error_at is not None and now - row.error_at < NEGATIVE_CACHE
        return retry or not recent_fail
    if now - row.ok_at < fresh:
        return False
    recent_fail = (
        row.error_at is not None and row.error_at > row.ok_at and now - row.error_at < NEGATIVE_CACHE
    )
    return retry or not recent_fail


async def ensure_fresh(
    symbol: str | None, now: datetime, *, isin: str | None = None, retry: bool = False
) -> dict:
    """Refresh the market feeds and (for an NSE symbol) the stock's feeds that have no good read in the last
    FRESH hours. One refresh at a time per stock; a failure is not retried for NEGATIVE_CACHE unless `retry`."""
    today = to_ist(now).date()
    async with _LOCKS.setdefault(symbol or store.MARKET, asyncio.Lock()):
        with session_scope() as s:
            m_due = []
            for ds in MARKET_DATASETS:
                row = store.feed(s, ds)
                due = _due(row, now, retry)
                if ds == "fno_ban" and row is not None and row.as_of and row.as_of < today.isoformat():
                    due = due or _due(
                        row, now, retry, fresh=timedelta(minutes=30)
                    )  # today's list may be out now
                if due:
                    m_due.append(ds)
            s_due = [ds for ds in STOCK_DATASETS if symbol and _due(store.feed(s, ds, symbol), now, retry)]
        out: dict[str, Any] = {}
        if not m_due and not s_due:
            return out
        async with _client() as d:
            if m_due:
                out["market"] = await refresh_market(now, tuple(m_due), client=d)
            if s_due and symbol:
                out["stock"] = await refresh_stock(symbol, now, isin=isin, datasets=tuple(s_due), client=d,
                                                   max_filings=MAX_FILINGS_ON_VIEW)  # fmt: skip
        return out
