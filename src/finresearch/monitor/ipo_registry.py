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

SLOTS = (("am", time(8, 45)), ("pm", time(18, 0)))  # IST: before the bidding day, after the exchanges' evening updates
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
