"""Create or refresh a watch from NSE's issue information (BSE's, for a BSE SME issue)."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select, update

from finresearch.db import session_scope
from finresearch.db.models import Company, MonitorJob, Watch
from finresearch.fincalc.dates import today_ist
from finresearch.monitor.phase import DONE, LISTING_KEYS, phase_of
from finresearch.monitor.plan import expected_dates

_PERIOD = re.compile(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s*to\s*(\d{1,2}-[A-Za-z]{3}-\d{4})")
_ANCHOR = re.compile(r"anchor[^0-9]*portion of\s*(\d[\d,]*)\s*equity shares", re.I)


def parse_period(text: str | None) -> tuple[date, date] | None:
    m = _PERIOD.search(text or "")
    if not m:
        return None
    a, b = (datetime.strptime(x, "%d-%b-%Y").date() for x in m.groups())
    return a, b


def parse_anchor_shares(issue_size: str | None) -> Decimal | None:
    m = _ANCHOR.search(issue_size or "")
    return Decimal(m.group(1).replace(",", "")) if m else None


def upsert_watch(company_slug: str, issue_info: dict[str, str]) -> dict:
    bse_ipo_no = int(issue_info["BSE IPO No"]) if issue_info.get("BSE IPO No") else None
    source = "BSE" if bse_ipo_no else "NSE"
    period = parse_period(issue_info.get("Issue Period"))
    if period is None:
        raise ValueError(f"{source} issue information has no 'Issue Period'; cannot schedule the checks")
    open_date, close_date = period
    allotment, listing = expected_dates(close_date)
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise LookupError(f"unknown company {company_slug!r}")
        symbol = co.nse_symbol or (issue_info.get("Symbol") if bse_ipo_no else None)
        if not symbol:
            raise ValueError(f"{company_slug} has no NSE symbol")
        w = s.scalar(select(Watch).where(Watch.company_id == co.id))
        if w is not None and w.kind != "ipo":
            if w.active:
                raise ValueError(f"{company_slug} is already watched as a stock; stop that watch first")
            _reset(s, w, "ipo")
        if w is None:
            w = Watch(company_id=co.id, nse_symbol=symbol, open_date=open_date, close_date=close_date,
                      allotment_date=allotment, listing_date=listing, meta={})  # fmt: skip
            s.add(w)
        else:
            w.open_date, w.close_date, w.active = open_date, close_date, True
            if not (w.meta or {}).get("listing_confirmed"):
                w.allotment_date, w.listing_date = allotment, listing
        w.anchor_shares = parse_anchor_shares(issue_info.get("Issue Size")) or w.anchor_shares
        if bse_ipo_no:  # subscription and listing checks then read BSE
            w.meta = {**(w.meta or {}), "bse_ipo_no": bse_ipo_no}
        s.flush()
        return watch_json(w, co)


async def watch_company(company_slug: str, *, fetch_detail=None, fetch_bse=None) -> dict:
    """Fetch NSE's issue information for the company's symbol and create or refresh its watch. A BSE-only SME
    issue (company meta `bse_ipo_no`, no NSE symbol) is watched from BSE's issue details instead."""
    from finresearch.adapters.nse import NseClient

    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise LookupError(f"unknown company {company_slug!r}")
        symbol, bse_ipo_no = co.nse_symbol, (co.meta or {}).get("bse_ipo_no")
    if not symbol and bse_ipo_no:
        if fetch_bse is None:
            from finresearch.adapters.bse import BseClient

            async with BseClient() as bse:
                detail = await bse.issue_detail(bse_ipo_no)
        else:
            detail = await fetch_bse(bse_ipo_no)
        return upsert_watch(company_slug, detail.issue_info())
    if not symbol:
        raise ValueError(f"{company_slug} has no NSE symbol")
    if fetch_detail is None:
        async with NseClient() as nse:
            detail = await nse.ipo_detail(symbol)
    else:
        detail = await fetch_detail(symbol)
    return upsert_watch(company_slug, detail.issue_info)


def watch_json(w: Watch, co: Company | None = None) -> dict:
    def iso(d):
        return d.isoformat() if d else None

    return {"id": w.id, "kind": w.kind, "company_id": w.company_id, "company": co.slug if co else None,
            "company_name": co.name if co else None, "nse_symbol": w.nse_symbol, "exchange": w.exchange or "NSE",
            "bse_code": w.bse_code, "key": w.key, "label": w.label,
            "open_date": iso(w.open_date), "close_date": iso(w.close_date),
            "allotment_date": iso(w.allotment_date), "listing_date": iso(w.listing_date),
            "anchor_shares": str(w.anchor_shares) if w.anchor_shares is not None else None, "active": w.active,
            "phase": phase_of(w, today_ist()), "meta": w.meta or {}}  # fmt: skip


def watch_stock(company_slug: str) -> dict:
    """Start (or restart) the daily monitoring of a listed stock: from NSE when the company has an NSE symbol, else
    from BSE by its scrip code (a BSE-only stock)."""
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise LookupError(f"unknown company {company_slug!r}")
        if not co.nse_symbol and not co.bse_code:
            raise ValueError(f"{company_slug} has no NSE symbol or BSE scrip code")
        exchange = "NSE" if co.nse_symbol else "BSE"
        w = s.scalar(select(Watch).where(Watch.company_id == co.id))
        if w is None:
            w = Watch(company_id=co.id, kind="stock", nse_symbol=co.nse_symbol, exchange=exchange,
                      bse_code=co.bse_code if exchange == "BSE" else None, meta={})  # fmt: skip
            s.add(w)
        elif w.kind != "stock":
            listed = (w.meta or {}).get("listing_confirmed") or phase_of(w, today_ist()) in DONE
            if w.active and not listed:
                raise ValueError(f"{company_slug} is already watched as an IPO; stop that watch first")
            _reset(s, w, "stock")  # stopped, or listed: the same row becomes a stock watch
        if exchange == "BSE":  # an NSE watch is never touched; a BSE-only company's watch reads BSE
            w.exchange, w.bse_code = "BSE", co.bse_code
        w.active = True
        s.flush()
        return watch_json(w, co)


def _cancel_pending(s, watch_id: int, keep: tuple[str, ...] = ()) -> None:
    now = datetime.now(UTC)
    s.execute(update(MonitorJob).where(MonitorJob.watch_id == watch_id, MonitorJob.status == "pending",
                                       MonitorJob.kind.not_in(keep))
              .values(status="cancelled", finished_at=now))  # fmt: skip


def _reset(s, w: Watch, kind: str) -> None:
    """Turn a watch into the other kind in place (company_id is unique).

    IPO -> stock (a listed or stopped IPO): the listing record is history, never wiped (#291). The row keeps its IPO
    dates and the listing keys stay at the top of `meta`, because the forecast resolver
    (signals.ledger.resolve_listing_gain) and the IPO backtest read `meta.listing_open` and `listing_date` from the
    watch; a full copy of the IPO's record goes to `meta.ipo`. Pending lock-in checks keep running (sync_slots keeps
    them on a stock watch); the other pending IPO checks are cancelled.

    stock -> IPO (a new issue by the same company): the old checks and dates go, and the previous IPO record moves to
    `meta.ipo_history`, so the new issue's listing is planned and recorded afresh."""
    old = dict(w.meta or {})
    if kind == "stock" and w.kind == "ipo":
        _cancel_pending(s, w.id, keep=("lockin",))
        record = {k: old[k] for k in (*LISTING_KEYS, "bse_ipo_no", "phase", "phase_at", "phase_history", "keep_watching")
                  if k in old}  # fmt: skip
        record |= {"symbol": w.nse_symbol, "anchor_shares": str(w.anchor_shares) if w.anchor_shares is not None else None,
                   **{k: getattr(w, k).isoformat() if getattr(w, k) else None
                      for k in ("open_date", "close_date", "allotment_date", "listing_date")}}  # fmt: skip
        w.kind, w.meta = kind, {**{k: old[k] for k in LISTING_KEYS if k in old}, "ipo": record}
        return
    _cancel_pending(s, w.id)
    history = list(old.get("ipo_history") or [])
    if old.get("ipo"):
        history.append(old["ipo"])
    w.kind, w.meta, w.anchor_shares = kind, ({"ipo_history": history} if history else {}), None
    w.open_date = w.close_date = w.allotment_date = w.listing_date = None


def stop_watch(watch_id: int) -> dict | None:
    """Deactivate a watch and cancel its pending checks. None if there is no such watch."""
    with session_scope() as s:
        w = s.get(Watch, watch_id)
        if w is None:
            return None
        w.active = False
        _cancel_pending(s, w.id)
        return {"id": w.id, "active": False}
