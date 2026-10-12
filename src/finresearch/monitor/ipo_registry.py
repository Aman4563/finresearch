"""The IPO issue registry (#292): every current and upcoming NSE and BSE issue, refreshed twice a day by the monitor.

Sources (three list requests a refresh): NSE `ipo-current-issue` + `all-upcoming-issues?category=ipo` (mainboard and
SME, adapters.nse) and BSE `GetPublicIssue_par_updated` (IPO rows only; adapters.bse). A NEW row also reads its lot
from the issue page (NSE `ipo-detail`, cached 6 h by the adapter; BSE issue details for BSE rows), and an upcoming or
open row still without a lot is retried at most MAX_LOT_LOOKUPS per refresh. Every request goes through the shared
per-host rate limits (adapters.http).

Rows are keyed by exchange + NSE symbol or BSE IPO number. A row that drops off its exchange's lists is marked
`off_list` (never deleted), and only after that exchange's lists were all read: a failed fetch never marks anything.
A BSE row whose company is already on NSE (name match, adapters.bse.name_key, or the same symbol) is linked to it
(`duplicate_of`) and never alerted twice. One in-app alert (kind "new_issue", level info: not forwarded to the phone)
is raised per newly seen issue whose board the profile's `preferences.ipo_alert_boards` covers (default: mainboard).
The very first refresh of an exchange seeds the registry without alerts: those issues are already on the IPOs page.

Not gated on the trading calendar: the lists are static pages, and a missing holiday list must not pause discovery.
Each slot is claimed in `alert_eval_slot` ("ipo_registry:<day>:am|pm"), so two monitor processes never run it twice.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)

SLOTS = (
    ("am", time(8, 45)),
    ("pm", time(18, 0)),
)  # IST: before the bidding day, after the exchanges' evening updates
MAX_LOT_LOOKUPS = 10
ALERT_KIND = "new_issue"


@dataclass
class Listing:
    """One issue as an exchange list shows it, normalised."""

    exchange: str  # NSE | BSE
    issue_key: str
    name: str
    board: str  # mainboard | sme | other
    symbol: str | None = None
    series: str | None = None
    open_date: date | None = None
    close_date: date | None = None
    price_band: str | None = None
    price_low: Decimal | None = None
    price_high: Decimal | None = None
    lot_size: int | None = None
    listed_as: str = "upcoming"  # which list it came from: current (open) | upcoming
    exchange_status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def nse_board(series: str | None) -> str:
    s = (series or "").upper()
    return "mainboard" if s == "EQ" else "sme" if s == "SME" else "other"


def bse_board(platform: str | None) -> str:
    p = (platform or "").upper()
    return "mainboard" if p == "MAINBOARD" else "sme" if p == "SME" else "other"


def phase(open_date: date | None, close_date: date | None, today: date, listed_as: str) -> str:
    """upcoming | open | closed from the bidding dates; without dates, from the list the row came from."""
    if open_date and today < open_date:
        return "upcoming"
    if close_date and today > close_date:
        return "closed"
    if open_date and close_date:
        return "open"
    return "open" if listed_as == "current" else "upcoming"


def from_nse(issue: Any, listed_as: str) -> Listing:
    return Listing(exchange="NSE", issue_key=issue.symbol, symbol=issue.symbol, name=issue.company or issue.symbol,
                   board=nse_board(issue.series), series=issue.series, open_date=issue.issue_start,
                   close_date=issue.issue_end, price_band=(issue.price_band or None),
                   price_low=issue.price_low, price_high=issue.price_high, listed_as=listed_as,
                   exchange_status=(str(issue.status)[:40] if issue.status else None),
                   raw={k: v for k, v in (issue.raw or {}).items() if isinstance(v, str | int | float | None)})  # fmt: skip


def from_bse(issue: Any) -> Listing:
    return Listing(exchange="BSE", issue_key=str(issue.ipo_no), symbol=None, name=issue.company or str(issue.ipo_no),
                   board=bse_board(issue.platform), series=issue.platform, open_date=issue.issue_start,
                   close_date=issue.issue_end, price_band=(issue.price_band or None), price_low=issue.price_low,
                   price_high=issue.price_high, listed_as="current" if issue.status == "L" else "upcoming",
                   exchange_status=issue.status,
                   raw={k: v for k, v in (issue.raw or {}).items() if isinstance(v, str | int | float | None)})  # fmt: skip


@dataclass
class Sources:
    """Network access (tests pass fakes). Each list call raises on failure; lot lookups return None when unknown."""

    nse_current: Callable[[], Awaitable[list[Any]]]
    nse_upcoming: Callable[[], Awaitable[list[Any]]]
    bse_issues: Callable[[], Awaitable[list[Any]]]
    nse_lot: Callable[[str, str | None], Awaitable[int | None]]
    bse_detail: Callable[[str], Awaitable[Any]]  # -> BseIssueDetail (market_lot, symbol) or None


async def read_lists(src: Sources) -> tuple[list[Listing] | None, list[Listing] | None, list[str]]:
    """(NSE listings or None when a list failed, BSE listings or None, errors)."""
    errors: list[str] = []
    nse: list[Listing] | None = []
    for listed_as, fetch in (("current", src.nse_current), ("upcoming", src.nse_upcoming)):
        try:
            rows = await fetch()
        except Exception as e:
            errors.append(f"NSE {listed_as}: {type(e).__name__}: {e}"[:200])
            nse = None
            continue
        if nse is not None:
            seen = {x.issue_key for x in nse}
            nse += [from_nse(i, listed_as) for i in rows if i.symbol and i.symbol not in seen]
    try:
        bse = [from_bse(i) for i in await src.bse_issues() if i.issue_type == "IPO"]
    except Exception as e:
        errors.append(f"BSE: {type(e).__name__}: {e}"[:200])
        bse = None
    return nse, bse, errors


# --------------------------------------------------------------------------- storing


def _boards_wanted() -> str:
    """The profile's `preferences.ipo_alert_boards`; "mainboard" when the profile cannot be read."""
    try:
        from finresearch.db import session_scope
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return load_profile(s).preferences.ipo_alert_boards
    except Exception:
        log.warning("could not read the profile's IPO alert boards; using mainboard", exc_info=True)
        return "mainboard"


def _wanted(board: str, boards: str) -> bool:
    return boards == "all" or (boards == "mainboard" and board == "mainboard")


def _message(row: Any) -> str:
    board = {"mainboard": "mainboard", "sme": "SME"}.get(row.board, row.board)
    dates = (f"bidding {row.open_date:%d-%b} to {row.close_date:%d-%b-%Y}" if row.open_date and row.close_date
             else "dates not announced yet")  # fmt: skip
    band = f", price band {row.price_band}" if row.price_band else ""
    lot = ""
    if row.lot_size and row.price_high:
        lot = f", lot {row.lot_size} (₹{row.lot_size * row.price_high:,.0f} at the upper band)"
    elif row.lot_size:
        lot = f", lot {row.lot_size}"
    return f"New IPO on {row.exchange} ({board}): {row.name}, {dates}{band}{lot}."


def store(session, exchange: str, listings: list[Listing] | None, now: datetime) -> list[int]:
    """Upsert one exchange's listings; mark its missing rows off_list (only when `listings` is not None, i.e. every
    list of that exchange was read). Returns the ids of rows inserted now."""
    from sqlalchemy import select

    from finresearch.db.models import IpoIssue

    if listings is None:
        return []
    today = to_ist(now).date()
    existing = {
        r.issue_key: r for r in session.scalars(select(IpoIssue).where(IpoIssue.exchange == exchange))
    }
    new: list[int] = []
    for x in listings:
        r = existing.pop(x.issue_key, None)
        status = phase(x.open_date, x.close_date, today, x.listed_as)
        if r is None:
            r = IpoIssue(exchange=exchange, issue_key=x.issue_key, first_seen=now, lot_size=x.lot_size)
            session.add(r)
        r.symbol = x.symbol or r.symbol
        r.name, r.board, r.series = x.name[:300], x.board, (x.series or None) and str(x.series)[:10]
        r.open_date, r.close_date, r.price_band = (
            x.open_date,
            x.close_date,
            (x.price_band or None) and x.price_band[:80],
        )
        r.price_low, r.price_high = x.price_low, x.price_high
        r.lot_size = x.lot_size or r.lot_size
        r.status, r.exchange_status, r.last_seen, r.off_list_at, r.raw = (
            status,
            x.exchange_status,
            now,
            None,
            x.raw,
        )
        if r.id is None:
            session.flush()
            new.append(r.id)
    for r in existing.values():  # dropped off the lists: marked, never deleted
        if r.status != "off_list":
            r.status, r.off_list_at = "off_list", now
    return new


async def fill_lots(src: Sources, rows: list[Any]) -> int:
    """Lots (and a BSE row's NSE symbol) for upcoming/open rows that have none, at most MAX_LOT_LOOKUPS. A failed
    lookup leaves the lot unknown (None), never 0."""
    n = 0
    for r in [r for r in rows if r.lot_size is None and r.status in ("upcoming", "open")][:MAX_LOT_LOOKUPS]:
        try:
            if r.exchange == "NSE":
                r.lot_size = await src.nse_lot(r.issue_key, r.series)
            else:
                d = await src.bse_detail(r.issue_key)
                if d is not None:
                    r.lot_size = d.market_lot or None
                    r.symbol = r.symbol or (d.symbol or None)
            n += 1
        except Exception as e:
            log.info("IPO registry: no lot for %s %s (%s)", r.exchange, r.issue_key, type(e).__name__)
    return n


def link_duplicates(rows: list[Any]) -> None:
    """A BSE row for an issue that is on NSE too (same symbol or company name) points at the NSE row."""
    from finresearch.adapters.bse import name_key

    nse = [r for r in rows if r.exchange == "NSE"]
    by_symbol = {r.symbol.upper(): r for r in nse if r.symbol}
    by_name = {name_key(r.name): r for r in nse}
    for r in rows:
        if r.exchange != "BSE" or r.duplicate_of is not None:
            continue
        twin = (by_symbol.get(r.symbol.upper()) if r.symbol else None) or by_name.get(name_key(r.name))
        if twin is not None:
            r.duplicate_of = twin.id


def raise_alerts(session, new_ids: list[int], boards: str, now: datetime) -> int:
    """One alert per newly seen issue the preference covers. An issue already alerted on the other exchange (a BSE
    row linked to an NSE row, either way round) is not alerted again. `alerted_at` is set under a row-level guard, so
    a row is alerted at most once."""
    from sqlalchemy import select, update

    from finresearch.adapters.bse import name_key
    from finresearch.db.models import Alert, IpoIssue

    if boards == "off" or not new_ids:
        return 0
    rows = list(session.scalars(select(IpoIssue)))
    n = 0
    for r in [r for r in rows if r.id in set(new_ids)]:
        if r.duplicate_of is not None or not _wanted(r.board, boards):
            continue
        twins = [o for o in rows if o.id != r.id and o.exchange != r.exchange and o.alerted_at is not None
                 and (o.duplicate_of == r.id or name_key(o.name) == name_key(r.name))]  # fmt: skip
        if twins:
            continue
        got = session.execute(update(IpoIssue).where(IpoIssue.id == r.id, IpoIssue.alerted_at.is_(None))
                              .values(alerted_at=now).returning(IpoIssue.id)).first()  # fmt: skip
        if got is None:
            continue
        session.add(Alert(kind=ALERT_KIND, level="info", message=_message(r),
                          data={"path": "/ipos", "exchange": r.exchange, "symbol": r.symbol, "board": r.board,
                                "issue_id": r.id, "open_date": r.open_date.isoformat() if r.open_date else None}))  # fmt: skip
        n += 1
    return n


# --------------------------------------------------------------------------- refresh and schedule


async def refresh(now: datetime, src: Sources, *, boards: str | None = None) -> dict[str, Any]:
    """Read the lists, store them, fill lots, link duplicates and raise the new-issue alerts."""
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import IpoIssue

    nse, bse, errors = await read_lists(src)
    with session_scope() as s:
        first = {ex: not s.scalar(select(func.count()).select_from(IpoIssue).where(IpoIssue.exchange == ex))
                  for ex in ("NSE", "BSE")}  # fmt: skip
        new_nse, new_bse = store(s, "NSE", nse, now), store(s, "BSE", bse, now)
        rows = list(s.scalars(select(IpoIssue)))
        lots = await fill_lots(src, rows)
        link_duplicates(rows)
        s.flush()
        # the first refresh of an exchange only seeds the registry: those issues are already on the IPOs page
        new = [*([] if first["NSE"] else new_nse), *([] if first["BSE"] else new_bse)]
        alerted = raise_alerts(s, new, boards if boards is not None else _boards_wanted(), now)
    return {"nse": None if nse is None else len(nse), "bse": None if bse is None else len(bse),
            "new": len(new_nse) + len(new_bse), "alerted": alerted, "lot_lookups": lots, "errors": errors}  # fmt: skip


def live_sources(nse: Any, bse: Any) -> Sources:
    async def nse_lot(symbol: str, series: str | None) -> int | None:
        return (await nse.issue_terms(symbol, series)).lot_size  # disk-cached for 6 h by the adapter

    async def bse_detail(ipo_no: str):
        from finresearch.adapters.bse import DETAIL_TTL_S

        return await bse.issue_detail(ipo_no, cache_ttl=DETAIL_TTL_S)

    return Sources(nse_current=nse.current_issues, nse_upcoming=nse.upcoming_issues, bse_issues=bse.public_issues,
                   nse_lot=nse_lot, bse_detail=bse_detail)  # fmt: skip


def due_slot(now: datetime) -> str | None:
    """The latest of today's two slots (IST) whose time has passed."""
    ist = to_ist(now)
    passed = [name for name, at in SLOTS if ist.time() >= at]
    return f"ipo_registry:{ist.date().isoformat()}:{passed[-1]}" if passed else None


async def ipo_registry_step(now: datetime, src: Sources | None = None) -> dict[str, Any]:
    from finresearch.monitor.disclosures import claim, finish

    slot = due_slot(now)
    if slot is None or not claim(slot, now):
        return {}
    try:
        if src is not None:
            res = await refresh(now, src)
        else:
            from finresearch.adapters.bse import BseClient
            from finresearch.adapters.nse import NseClient

            async with NseClient() as nse, BseClient() as bse:
                res = await refresh(now, live_sources(nse, bse))
    except Exception as e:
        log.warning("IPO registry refresh failed; retried later", exc_info=True)
        finish(slot, {"error": f"{type(e).__name__}: {e}"[:300]}, now, failed=True)
        return {"ipo_registry": "failed"}
    # a list that failed is retried (the other exchange's rows are stored already)
    finish(slot, res, now, failed=bool(res["errors"]))
    return {"ipo_registry": res}
