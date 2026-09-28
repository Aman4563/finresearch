"""Create or refresh a watch from NSE's issue information."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select

from finresearch.db import session_scope
from finresearch.db.models import Company, Watch
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
    period = parse_period(issue_info.get("Issue Period"))
    if period is None:
        raise ValueError("NSE issue information has no 'Issue Period'; cannot schedule the checks")
    open_date, close_date = period
    allotment, listing = expected_dates(close_date)
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise LookupError(f"unknown company {company_slug!r}")
        if not co.nse_symbol:
            raise ValueError(f"{company_slug} has no NSE symbol")
        w = s.scalar(select(Watch).where(Watch.company_id == co.id))
        if w is None:
            w = Watch(company_id=co.id, nse_symbol=co.nse_symbol, open_date=open_date, close_date=close_date,
                      allotment_date=allotment, listing_date=listing, meta={})  # fmt: skip
            s.add(w)
        else:
            w.open_date, w.close_date, w.active = open_date, close_date, True
            if not (w.meta or {}).get("listing_confirmed"):
                w.allotment_date, w.listing_date = allotment, listing
        w.anchor_shares = parse_anchor_shares(issue_info.get("Issue Size")) or w.anchor_shares
        s.flush()
        return watch_json(w, co)


async def watch_company(company_slug: str, *, fetch_detail=None) -> dict:
    """Fetch NSE's issue information for the company's symbol and create or refresh its watch."""
    from finresearch.adapters.nse import NseClient

    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise LookupError(f"unknown company {company_slug!r}")
        symbol = co.nse_symbol
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
            "company_name": co.name if co else None, "nse_symbol": w.nse_symbol,
            "open_date": iso(w.open_date), "close_date": iso(w.close_date),
            "allotment_date": iso(w.allotment_date), "listing_date": iso(w.listing_date),
            "anchor_shares": str(w.anchor_shares) if w.anchor_shares is not None else None, "active": w.active,
            "meta": w.meta or {}}  # fmt: skip


def watch_stock(company_slug: str) -> dict:
    """Start (or restart) the daily monitoring of a listed stock."""
    with session_scope() as s:
        co = s.scalar(select(Company).where(Company.slug == company_slug))
        if co is None:
            raise LookupError(f"unknown company {company_slug!r}")
        if not co.nse_symbol:
            raise ValueError(f"{company_slug} has no NSE symbol")
        w = s.scalar(select(Watch).where(Watch.company_id == co.id))
        if w is None:
            w = Watch(company_id=co.id, kind="stock", nse_symbol=co.nse_symbol, meta={})
            s.add(w)
        elif w.kind != "stock":
            raise ValueError(f"{company_slug} is already watched as an IPO; stop that watch first")
        w.active = True
        s.flush()
        return watch_json(w, co)
