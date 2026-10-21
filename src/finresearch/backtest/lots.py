"""Dated market lots of NSE's index derivatives, 2008-2021 (#365, P1.3), from NSE's F&O circulars.

The legacy bhavcopy carries no lot size, and the gcd of open interest (fnohist.derive.infer_lot) returns a common
multiple whenever few distinct OI figures exist. For vault days the backtest reads lots from this table only
(backtest.dbhistory.DbHistory.lot): an unsourced or ambiguous lot is None and the trade is skipped (PREREG.md §6:
unknown lot = no entry). It lives in backtest/ so the vault engine pin (PREREG.md §2.7, vault.registry.engine_hash)
covers it.

How NSE applied a revision (each row quotes its circular):
- "all": every contract has the new lot from `effective`. FAOP/28360 (16-Dec-2014) clarified that the Oct-2014 NIFTY
  cut skipped long-term options (expiry more than 3 months away): each kept 50 until it became the far month.
- "far month": only contracts expiring from `first_expiry` (the far month introduced on `effective`) get the new
  lot; the near and mid months keep the old one until they expire. Weeklies follow `weekly_first_expiry` where the
  circular states it. Long-term NIFTY options that existed on `effective` switch on `long_dated_switch`.
Months-ahead counts monthly contracts from the near month (the current month until its last Thursday): a contract 0-2
months ahead is near, mid or far; more is long-term. A monthly expiry moved to Wednesday by a holiday shifts that
count by one for that one Thursday [U].
Sources were downloaded and read on 22-Oct-2026 (https://nsearchives.nseindia.com/content/circulars/<file>).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

Status = Literal["verified", "inferred"]
TABLE_START, TABLE_END = (
    date(2008, 1, 1),
    date(2021, 12, 31),
)  # the vault; later days use the bhavcopy's own lots
LONG_DATED = frozenset({"NIFTY"})  # index with long-term (quarterly / half-yearly) options in these years
_C = "https://nsearchives.nseindia.com/content/circulars/"


@dataclass(frozen=True)
class Revision:
    symbol: str
    lot: int
    effective: date
    circular: str
    source: str
    status: Status
    first_expiry: date | None = None  # None: all contracts; else the far-month rule
    weekly_first_expiry: date | None = None  # far month: weeklies expiring on or after it get the new lot
    long_dated_switch: date | None = (
        None  # far month: the day existing long-term options switch (None: unknown)
    )
    unknown_expiries: tuple[date, date] | None = (
        None  # contracts expiring in this range: lot unknown from effective
    )
    unknown_weeklies: tuple[date, date] | None = (
        None  # weeklies expiring in this range: lot unknown from effective
    )
    long_term_wait: bool = False  # all contracts, but long-term options only once they are the far month
    note: str = ""


@dataclass(frozen=True)
class Weeklies:
    first_trade: date
    first_expiry: date
    circular: str
    source: str


def _last_thursday(y: int, m: int) -> date:
    d = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - 3) % 7)


def months_ahead(expiry: date, day: date) -> int:
    near = day.year * 12 + day.month - 1 + (day > _last_thursday(day.year, day.month))
    return expiry.year * 12 + expiry.month - 1 - near


def _long_term(symbol: str, expiry: date, day: date, kind: str | None) -> bool:
    return symbol in LONG_DATED and kind != "weekly" and months_ahead(expiry, day) > 2


def _in(x: date, rng: tuple[date, date] | None) -> bool:
    return rng is not None and rng[0] <= x <= rng[1]


def _apply(r: Revision, expiry: date, day: date, kind: str | None, old: int | None) -> int | None:
    if (
        r.first_expiry is None
    ):  # all contracts; long-term NIFTY options wait until they are the far month (FAOP/28360)
        return old if r.long_term_wait and _long_term(r.symbol, expiry, day, kind) else r.lot
    if _in(expiry, r.unknown_expiries) or (kind == "weekly" and _in(expiry, r.unknown_weeklies)):
        return None
    if kind == "weekly":
        start = r.weekly_first_expiry or r.first_expiry.replace(day=1)
        return r.lot if expiry >= start else old
    if expiry < r.first_expiry:
        return old
    if _long_term(r.symbol, expiry, r.effective, kind) and expiry.month in (
        3,
        6,
        9,
        12,
    ):  # listed before effective
        if r.long_dated_switch is None:
            return None
        return r.lot if day >= r.long_dated_switch else old
    return r.lot


def lot_for(symbol: str, expiry: date, day: date, kind: str | None = None) -> int | None:
    """The lot of `symbol`'s contract expiring on `expiry`, on trade day `day`; `kind` is fno_contract's (weekly,
    monthly, long-dated). None when unsourced, ambiguous, outside 2008-2021 or not in the table."""
    if not TABLE_START <= day <= TABLE_END:
        return None
    lot: int | None = None
    for r in REVISIONS:
        if r.symbol != symbol:
            continue
        if day < r.effective:
            break
        lot = _apply(r, expiry, day, kind, lot)
    return lot


_R = Revision
REVISIONS: tuple[Revision, ...] = tuple(sorted((
    _R("NIFTY", 50, date(2007, 2, 23), "NSE/FAOP/8483", _C + "faop8483.htm", "verified",
       note="NSE/F&O/010/2007 of 6-Feb-2007: 100 -> 50 'for all contracts' from 23-Feb-2007"),
    _R("BANKNIFTY", 25, date(2007, 12, 28), "NSE/FAOP/9871", _C + "faop9871.htm", "verified",
       note="NSE/F&O/108/2007 of 7-Dec-2007, Annexure-2: 50 -> 25 'for all contracts' from 28-Dec-2007"),
    _R("BANKNIFTY", 50, date(2008, 12, 26), "NSE/FAOP/11720", _C + "faop11720.htm", "verified",
       first_expiry=date(2009, 3, 26),
       note="NSE/F&O/128/2008 of 2-Dec-2008: 25 -> 50, 'only the far month contract i.e. March 2009 expiry'; Jan and "
            "Feb 2009 keep 25"),
    _R("BANKNIFTY", 25, date(2010, 4, 30), "NSE/FAOP/14481", _C + "faop14481.pdf", "verified",
       note="Circular 030 of 31-Mar-2010: 50 -> 25, 'All contracts ... with effect from April 30, 2010'"),
    _R("NIFTY", 25, date(2014, 10, 31), "NSE/FAOP/27733", _C + "FAOP27733.pdf", "verified",
       long_term_wait=True,
       note="069/2014 of 30-Sep-2014: 50 -> 25 for all contracts from 31-Oct-2014, except long-term options, which "
            "stay 50 until they become the far month (clarification NSE/FAOP/28360, 16-Dec-2014)"),
    _R("NIFTY", 75, date(2015, 8, 28), "NSE/FAOP/30449", _C + "FAOP30449.pdf", "verified",
       first_expiry=date(2015, 11, 26), long_dated_switch=date(2015, 9, 28),
       note="071/2015 of 7-Aug-2015: 25 -> 75 from the Nov-2015 far month, Sep and Oct 2015 keep 25; long-term "
            "options (expiry > 3 months, Dec-2015 included) 50 -> 75 from 28-Sep-2015 (NSE/FAOP/30450, 072/2015)"),
    _R("BANKNIFTY", 30, date(2015, 8, 28), "NSE/FAOP/30449", _C + "FAOP30449.pdf", "verified",
       first_expiry=date(2015, 11, 26),
       note="071/2015 of 7-Aug-2015: 25 -> 30 from the Nov-2015 far month; Sep and Oct 2015 keep 25"),
    _R("BANKNIFTY", 40, date(2016, 4, 29), "NSE/FAOP/32134", _C + "FAOP32134.pdf", "verified",
       first_expiry=date(2016, 7, 28), weekly_first_expiry=date(2016, 7, 1),
       note="034/2016 of 31-Mar-2016: 30 -> 40 from the Jul-2016 far month; May and Jun 2016 keep 30. Weeklies: "
            "'30 units for all expiries of the month of June 2016 and 40 units for all expiries from the month of "
            "July 2016' (NSE/FAOP/32329)"),
    _R("BANKNIFTY", 20, date(2018, 10, 26), "NSE/FAOP/39012", _C + "FAOP39012.pdf", "verified",
       first_expiry=date(2019, 1, 31), weekly_first_expiry=date(2019, 1, 1),
       unknown_expiries=(date(2018, 10, 26), date(2018, 12, 31)),
       note="91/2018 of 28-Sep-2018: 40 -> 20 'after expiry of October 2018 contracts'. It names Jan-2019 as the "
            "first revised month and Nov/Dec 2018 as keeping 40, but under 'in case of upward revision' (this was "
            "a cut): Nov and Dec 2018 contracts (monthly and weekly) are unknown from 26-Oct-2018"),
    _R("BANKNIFTY", 25, date(2020, 5, 4), "NSE/FAOP/44039", _C + "FAOP44039.pdf", "verified",
       first_expiry=date(2020, 7, 30), weekly_first_expiry=date(2020, 8, 1),
       unknown_weeklies=(date(2020, 6, 26), date(2020, 7, 31)),
       note="35/2020 of 31-Mar-2020: 20 -> 25 from the Jul-2020 far month; May and Jun 2020 keep 20. Silent on "
            "weeklies: July-2020 weeklies are unknown; later weeklies were listed after the revision (inferred)"),
    _R("FINNIFTY", 40, date(2021, 1, 11), "NSE/FAOP/46603", _C + "FAOP46603.pdf", "verified",
       note="108/2020 of 10-Dec-2020: launched 11-Jan-2021 (as FINANCIALS, renamed FINNIFTY by NSE/FAOP/46924), "
            "lot 40 for futures and options; unchanged by NSE/FAOP/47854"),
    _R("NIFTY", 50, date(2021, 4, 30), "NSE/FAOP/47854", _C + "FAOP47854.pdf", "verified",
       first_expiry=date(2021, 7, 29), weekly_first_expiry=date(2021, 8, 1), long_dated_switch=date(2021, 6, 25),
       note="28/2021 of 31-Mar-2021: 75 -> 50 from the Jul-2021 monthly; May and Jun 2021 keep 75; 'August 2021 "
            "Weekly expiry & beyond' revised (July-2021 weeklies keeping 75 is inferred from that); long-term "
            "options 75 -> 50 'after expiry of June 2021 contracts (i.e. June 25, 2021)'"),
), key=lambda r: r.effective))  # fmt: skip

WEEKLIES_FROM: dict[str, Weeklies] = {
    "BANKNIFTY": Weeklies(date(2016, 5, 27), date(2016, 6, 2), "NSE/FAOP/32329", _C + "FAOP32329.pdf"),
    "NIFTY": Weeklies(date(2019, 2, 11), date(2019, 2, 14), "NSE/FAOP/39894", _C + "FAOP39894.pdf"),
    "FINNIFTY": Weeklies(date(2021, 1, 11), date(2021, 1, 14), "NSE/FAOP/46603", _C + "FAOP46603.pdf"),
}  # the first weekly expiry per index (FINNIFTY started with weekly and monthly contracts at once)
