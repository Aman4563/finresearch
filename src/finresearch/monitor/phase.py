"""The lifecycle phase of an IPO watch (#291), kept in `Watch.meta["phase"]` (no migration).

    announced -> open -> closed -> allotted -> listed -> archived      (or withdrawn: it never listed)

The first five follow the watch's dates and the listing check: `listed` means the monitor recorded a listing-day price
(`meta.listing_open` / `listing_close`), never just "the expected listing date has passed", so a listing that NSE
moves keeps the watch `allotted` until it happens. `archived` and `withdrawn` are sticky and set only by `phase_step`.

The phase is separate from `Watch.active`, which still means "has planned checks": a listed or archived IPO keeps its
lock-in checks (up to six months after allotment) and the scheduler runs them as before. Readers that show live IPOs
(digest, brief, alert targets, the dashboard and the IPO page) skip `DONE` phases; the web app stops polling them.

An unknown input never moves a phase: holidays not cached for the year, no issue price for the gain, an NSE list or
quote that failed. Such a watch simply stays where it is until the next pass.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from finresearch.db import session_scope
from finresearch.db.models import Company, Forecast, PortfolioHolding, PortfolioLot, Watch
from finresearch.fincalc.dates import business_days_between, to_ist, today_ist

log = logging.getLogger(__name__)

PHASES = ("announced", "open", "closed", "allotted", "listed", "archived", "withdrawn")
# still an IPO in progress: polled by the web app, in the brief and the digest
LIVE = frozenset({"announced", "open", "closed", "allotted"})
DONE = frozenset({"listed", "archived", "withdrawn"})
# an open forecast on the instrument (e.g. listing_gain waiting to resolve) holds a listed watch at most this long
ARCHIVE_MAX_DAYS = 60
# a watch past its expected listing date by this many trading days with no listing is checked against NSE's lists
WITHDRAWN_AFTER_TRADING_DAYS = 10
# the listing record a stock watch keeps when an IPO watch becomes one (monitor.watch._reset): the forecast resolver
# (signals.ledger.resolve_listing_gain) and the IPO backtest read `listing_open` from the watch's meta
LISTING_KEYS = ("listing_open", "listing_close", "listing_confirmed", "expected_listing_date", "listing_gain_pct",
                "issue_price_upper")  # fmt: skip


def date_phase(w: Watch, today: date) -> str | None:
    """The phase from the watch's dates and recorded listing prices; None when the dates are unknown."""
    meta = w.meta or {}
    if meta.get("listing_open") is not None or meta.get("listing_close") is not None:
        return "listed"
    if w.open_date is None or w.close_date is None:
        return None
    if today < w.open_date:
        return "announced"
    if today <= w.close_date:
        return "open"
    if w.allotment_date is None or today < w.allotment_date:
        return "closed"
    return "allotted"


def phase_of(w: Watch, today: date) -> str | None:
    """The phase to show: the stored sticky phase (archived, withdrawn), else the one the dates give. None for a stock
    watch (an IPO watch converted to a stock keeps its IPO record in `meta.ipo`)."""
    if w.kind != "ipo":
        return None
    stored = (w.meta or {}).get("phase")
    if stored in ("archived", "withdrawn"):
        return stored
    return date_phase(w, today) or stored


def _instrument_keys(w: Watch) -> list[str]:
    return [k for k in {w.nse_symbol, w.key} if k]


def open_forecasts(s: Session, w: Watch) -> int:
    """Open forecasts on the watch's instrument (any asset, e.g. the IPO listing gain or a stock signal)."""
    keys = _instrument_keys(w)
    if not keys:
        return 0
    return s.scalar(select(func.count()).select_from(Forecast)
                    .where(Forecast.status == "open", Forecast.instrument.in_(keys))) or 0  # fmt: skip


def archive_ready(s: Session, w: Watch, today: date, trading_days: int,
                  holidays: set[date] | None) -> tuple[bool, str]:  # fmt: skip
    """listed -> archived: the listing-day close is recorded, `trading_days` exchange days have passed since the
    listing, and no forecast on the instrument is still open (or ARCHIVE_MAX_DAYS calendar days have passed)."""
    meta = w.meta or {}
    if meta.get("listing_close") is None:
        return False, "the listing-day close is not recorded"
    if w.listing_date is None:
        return False, "no listing date"
    if holidays is None:
        return False, "the exchange holidays for this year are not cached (trading days unknown)"
    passed = business_days_between(w.listing_date, today, holidays)
    if passed < trading_days:
        return False, f"{passed} of {trading_days} trading days since the listing"
    n_open = open_forecasts(s, w)
    if n_open and (today - w.listing_date).days < ARCHIVE_MAX_DAYS:
        return False, f"{n_open} open forecast(s) on {w.nse_symbol}"
    return True, f"{passed} trading days since the listing" + (f"; {n_open} forecast(s) still open after "
                                                               f"{ARCHIVE_MAX_DAYS} days" if n_open else "")  # fmt: skip


def is_held(s: Session, w: Watch) -> bool:
    """Open lots of the instrument in the portfolio, matched by the company's ISIN or the NSE symbol. Local data only:
    the answer (a boolean) decides whether an archived IPO becomes a stock watch."""
    co = s.get(Company, w.company_id)
    isin = (co.isin if co else None) or ((co.meta or {}).get("isin") if co else None)
    match = [PortfolioHolding.isin == isin] if isin else []
    if w.nse_symbol:
        match += [PortfolioHolding.nse_symbol == w.nse_symbol, PortfolioHolding.ikey == f"NSE:{w.nse_symbol}"]
    if not match:
        return False
    q = (select(func.count()).select_from(PortfolioLot).join(PortfolioHolding, PortfolioHolding.id == PortfolioLot.holding_id)
         .where(PortfolioLot.open_quantity > 0, PortfolioHolding.asset_type == "stock", or_(*match)))  # fmt: skip
    return bool(s.scalar(q))


def _listing_gain(s: Session, w: Watch, meta: dict[str, Any]) -> None:
    """Record the listing gain vs the upper price band for the watch card, once it can be computed."""
    if meta.get("listing_gain_pct") is not None or meta.get("listing_open") is None:
        return
    from finresearch.fincalc.ipo import listing_gain
    from finresearch.monitor.jobs import _upper_band

    upper = _upper_band(s, w)
    if not upper:
        return  # unknown issue price: the card shows the gain as unknown
    meta["issue_price_upper"] = str(upper)
    meta["listing_gain_pct"] = f"{listing_gain(upper, Decimal(meta['listing_open'])) * 100:.2f}"


async def _never_listed(w: Watch, deps: Any, lists: dict[str, Any]) -> bool | None:
    """True when NSE shows no trace of the issue: absent from its current, upcoming and past issue lists, and its quote
    has no listing date. None (unknown) when a source failed or is not configured. A BSE SME issue is not on NSE's
    lists, so it is never judged here."""
    if (w.meta or {}).get("bse_ipo_no") or not w.nse_symbol or getattr(deps, "ipo_symbols", None) is None:
        return None
    if "symbols" not in lists:
        try:
            lists["symbols"] = await deps.ipo_symbols()
        except Exception:
            log.warning(
                "NSE issue lists unavailable; withdrawn IPOs are checked on a later pass", exc_info=True
            )
            lists["symbols"] = None
    symbols = lists["symbols"]
    if symbols is None or w.nse_symbol in symbols:
        return None if symbols is None else False
    from finresearch.adapters.http import is_transient

    try:
        q = await deps.quote(w.nse_symbol)
    except Exception as e:
        return None if is_transient(e) else True  # no quote at all (e.g. NSE's 404 for an unknown symbol)
    return q is None or q.listing_date is None


async def phase_step(now: datetime, deps: Any = None) -> dict[str, int]:
    """Move every IPO watch to its current phase (stopped ones too: their phase is still shown), archive listed
    watches that are done, mark issues that never listed as withdrawn, and turn an archived watch the investor holds
    (or chose to keep watching) into a stock watch. Runs before `sync_slots` so a converted watch is planned at once."""
    from finresearch.adapters.nse_holidays import known_trading_holidays
    from finresearch.monitor.scheduler import watch_windows

    today = to_ist(now).date()
    trading_days = watch_windows().ipo_archive_trading_days
    holidays = known_trading_holidays(today)
    out = {"ipo_phase_changed": 0, "ipo_archived": 0, "ipo_withdrawn": 0, "ipo_converted": 0}
    convert: list[str] = []
    lists: dict[str, Any] = {}
    with session_scope() as s:
        for w in s.scalars(select(Watch).where(Watch.kind == "ipo").order_by(Watch.id)):
            meta = dict(w.meta or {})
            if meta.get("phase") in ("archived", "withdrawn"):
                continue
            new = date_phase(w, today)
            if new == "listed":
                _listing_gain(s, w, meta)
                if archive_ready(s, w, today, trading_days, holidays)[0]:
                    new = "archived"
            elif (new in ("closed", "allotted") and w.listing_date is not None and holidays is not None
                  and meta.get("withdrawn_checked") != today.isoformat()
                  and business_days_between(w.listing_date, today, holidays) > WITHDRAWN_AFTER_TRADING_DAYS):  # fmt: skip
                gone = await _never_listed(w, deps, lists)
                if gone is not None:
                    meta["withdrawn_checked"] = today.isoformat()
                if gone:
                    new = "withdrawn"
            if new is not None and new != meta.get("phase"):
                meta["phase"], meta["phase_at"] = new, today.isoformat()
                meta["phase_history"] = [
                    *(meta.get("phase_history") or []),
                    {"phase": new, "day": today.isoformat()},
                ]
                out["ipo_phase_changed"] += 1
                if new == "archived":
                    out["ipo_archived"] += 1
                    if w.active and (meta.get("keep_watching") or is_held(s, w)):
                        co = s.get(Company, w.company_id)
                        convert.append(co.slug)
                elif new == "withdrawn":
                    out["ipo_withdrawn"] += 1
                    _withdraw(s, w, meta)
            if meta != (w.meta or {}):
                w.meta = meta
    for slug in convert:  # after the commit above: watch_stock opens its own session
        from finresearch.monitor.watch import watch_stock

        try:
            watch_stock(slug)
            out["ipo_converted"] += 1
        except (LookupError, ValueError):
            log.warning("archived IPO %s could not become a stock watch", slug, exc_info=True)
    return out


def _withdraw(s: Session, w: Watch, meta: dict[str, Any]) -> None:
    """The issue never listed: its remaining checks (listing retries, lock-ins of shares that do not exist) go."""
    from finresearch.monitor.jobs import alert
    from finresearch.monitor.watch import _cancel_pending

    w.active = False
    _cancel_pending(s, w.id)
    alert(s, w, "ipo_withdrawn", f"{w.nse_symbol} has not listed {WITHDRAWN_AFTER_TRADING_DAYS}+ trading days after "
          f"its expected listing date ({w.listing_date}) and is no longer on NSE's issue lists: treated as withdrawn. "
          "Its remaining checks are cancelled; check the registrar for any refund.", "warn")  # fmt: skip


# the /api/watches order within active and stopped watches: issues still taking bids, then waiting for allotment or
# listing, then announced; stock watches; then recently listed and the archive
PHASE_RANK = {
    "open": 0,
    "closed": 1,
    "allotted": 2,
    "announced": 3,
    None: 4,
    "listed": 5,
    "archived": 6,
    "withdrawn": 7,
}


def sort_key(w: Watch, phase: str | None) -> tuple:
    return (not w.active, PHASE_RANK.get(phase, 4), -(w.close_date or date.min).toordinal(), w.id)


def set_keep_watching(watch_id: int, keep: bool) -> dict | None:
    """The investor's "keep watching after listing" choice for an IPO watch. Ticked on an archived watch that is still
    active, it becomes a stock watch at once (otherwise on archive). None if there is no such watch."""
    from finresearch.monitor.watch import watch_json, watch_stock

    with session_scope() as s:
        w = s.get(Watch, watch_id)
        if w is None:
            return None
        if w.kind != "ipo":
            raise ValueError(
                "only an IPO watch has a keep-watching choice; this one is already a stock watch"
            )
        w.meta = {**(w.meta or {}), "keep_watching": keep}
        co = s.get(Company, w.company_id)
        convert = keep and w.active and phase_of(w, today_ist()) == "archived"
        if not convert:
            return watch_json(w, co)
        slug = co.slug
    return watch_stock(slug)
