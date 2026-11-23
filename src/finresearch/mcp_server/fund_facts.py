"""A citable mutual-fund fact for research agents (#505): the outcome of a monthly SIP, from AMFI NAVs only.

The fund prompts asked for "fincalc funds.sip_outcome", which takes a full NAV series: an agent holds only the
60-point sample amfi_nav_history returns, and no claim could bind hundreds of NAVs. The `fund_sip_outcome` tool
(mcp_server.server) therefore fetches the scheme's NAV history from AMFI itself, as amfi_nav_history does, and keeps
the series it used as a WebSnapshot under a key no web page can have (`series_key`: fetch_page and the exchange tools
only store http(s) URLs, so nothing else can write a series). Every answer is computed here from that stored series:

- the tool's answer, with the NAV date and NAV of each instalment and of the valuation, and a `citation`;
- save_claim's re-run (claims.save_claim, grade D only when it reproduces the value) and `evidence regrade`, which
  find the series by key AND sha256, so a re-run never touches the network and never reads another series.

An unknown answer is {"known": false, "reason": ...} and carries no number: a scheme younger than the SIP period (its
early instalments would all be bought on the first NAV date), NAVs missing around an instalment or the end date.

Privacy: AMFI NAVs are public and the same for every investor. Nothing here reads a personal table or package.
"""

from __future__ import annotations

import calendar
import json
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import WebSnapshot
from finresearch.fincalc import funds
from finresearch.fincalc.dates import add_years
from finresearch.fincalc.numbers import require_price

TOOLS = ("fund_sip_outcome",)
METHOD_VERSION = "fund_facts/1"
SOURCE = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"  # AMFI's NAV history report
DATASET = "AMFI NAV history report, the scheme's NAVs for the SIP period (the series used is stored, local)"
# NAVs fetched before `start`: one on or before the first instalment date shows the scheme already existed then
LOOKBACK_DAYS = 10
# an instalment bought (or a valuation dated) more than this many days from its date means NAVs are missing: the
# longest run of days without a NAV is a weekend plus market holidays
MAX_GAP_DAYS = 10
CITABLE = ("xirr_pct", "value", "invested", "gain")
Nav = tuple[date, Decimal]


def _unknown(reason: str) -> dict[str, Any]:
    return {"known": False, "reason": reason}


def window(latest: date | None, years: int | None, start: str | None, end: str | None) -> tuple[date, date]:
    """(start, end) of the SIP: `end` defaults to the scheme's latest NAV date, `start` to `years` before `end`.
    Exactly one of `years` and `start` is given. ValueError otherwise, or when the dates are not in order."""
    if (years is None) == (start is None):
        raise ValueError("give either years (a SIP over the last N years) or start (YYYY-MM-DD), not both")
    e = date.fromisoformat(end) if end else latest
    if e is None:
        raise ValueError("the scheme has no dated NAV in AMFI's file: give end (YYYY-MM-DD)")
    if years is not None and (isinstance(years, bool) or not isinstance(years, int) or not 1 <= years <= 30):
        raise ValueError("years must be a whole number between 1 and 30")
    s = date.fromisoformat(start) if start else add_years(e, -years)
    if s >= e:
        raise ValueError(f"start {s} must be before end {e}")
    return s, e


def series_key(scheme_code: str, start: date, end: date) -> str:
    """The stored series' name. Not a URL on purpose: see the module docstring."""
    return f"amfi-nav-series:{scheme_code}:{start.isoformat()}:{end.isoformat()}"


def store_series(session: Session, scheme: dict[str, Any], start: date, end: date, navs: list[Nav],
                 run_id: int | None) -> tuple[dict[str, Any], str]:  # fmt: skip
    """Keep the NAVs a SIP answer is computed from. Returns (the series as stored, its sha256)."""
    from finresearch.verify import web

    rows = sorted({d: v for d, v in navs if d <= end}.items())
    text = json.dumps({"scheme": scheme, "start": start.isoformat(), "end": end.isoformat(), "source": SOURCE,
                       "navs": [[d.isoformat(), str(v)] for d, v in rows]}, sort_keys=True)  # fmt: skip
    snap = web.store(session, series_key(str(scheme["scheme_code"]), start, end), text, run_id=run_id,
                     content_type="application/json")  # fmt: skip
    return json.loads(text), snap.sha256


def load_series(session: Session, code: str, start: date, end: date, sha256: str) -> dict[str, Any] | None:
    """The stored series with exactly this key and sha256 (of any age and run: it is public data, named by its
    content), or None."""
    text = session.scalar(select(WebSnapshot.text).where(
        WebSnapshot.url == series_key(code, start, end), WebSnapshot.sha256 == sha256).limit(1))  # fmt: skip
    return None if text is None else json.loads(text)


def due_dates(start: date, end: date, day_of_month: int) -> list[date]:
    """The instalment dates from `start` to `end`: `day_of_month` of each month, or the month's last day when the
    month has no such day (fincalc.funds.sip_outcome's rule)."""
    out, y, m = [], start.year, start.month
    while True:
        due = date(y, m, min(day_of_month, calendar.monthrange(y, m)[1]))
        if due > end:
            return out
        if due >= start:
            out.append(due)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def sip(series: dict[str, Any], sha256: str, amount: Any, day_of_month: int = 1) -> dict[str, Any]:
    """The SIP outcome over a stored series (`store_series` / `load_series`): fincalc.funds.sip_outcome on its NAVs,
    with the NAV date of every instalment. Raises ValueError for a bad amount or day."""
    amt = require_price(amount, "amount")
    if isinstance(day_of_month, bool) or not isinstance(day_of_month, int) or not 1 <= day_of_month <= 31:
        raise ValueError("day_of_month must be a whole number from 1 to 31")
    code = str(series["scheme"]["scheme_code"])
    start, end = date.fromisoformat(series["start"]), date.fromisoformat(series["end"])
    navs = [(date.fromisoformat(d), Decimal(v)) for d, v in series["navs"]]
    if len(navs) < 2:
        return _unknown(f"AMFI's NAV history has {len(navs)} NAVs for scheme {code} up to {end}")
    dues = due_dates(start, end, day_of_month)
    if not dues:
        return _unknown(f"no instalment date (day {day_of_month} of a month) falls between {start} and {end}")
    if navs[0][0] > dues[0]:
        return _unknown(f"the NAV history starts on {navs[0][0]}, after the first instalment date {dues[0]}: the "
                        "scheme is younger than the SIP period (or its early NAVs are missing); choose a later start")  # fmt: skip
    fills: list[tuple[date, date, Decimal]] = []  # (instalment date, NAV date, NAV)
    for due in dues:
        fill = next(((d, v) for d, v in navs if d >= due), None)
        if fill is None:  # the last instalment date has no NAV yet: it is not invested (as sip_outcome)
            break
        if (fill[0] - due).days > MAX_GAP_DAYS:
            return _unknown(f"no NAV within {MAX_GAP_DAYS} days of the instalment date {due} (the next one is dated "
                            f"{fill[0]}): NAVs are missing")  # fmt: skip
        fills.append((due, *fill))
    last_day, last_nav = navs[-1]
    if (end - last_day).days > MAX_GAP_DAYS:
        return _unknown(f"the latest NAV on or before {end} is dated {last_day}, more than {MAX_GAP_DAYS} days "
                        "earlier: NAVs are missing or the scheme has stopped publishing")  # fmt: skip
    try:
        o = funds.sip_outcome(navs, amt, start, end, day_of_month)
    except ValueError as e:  # no instalment inside the history, or no rate solves the cash flows
        return _unknown(f"the SIP outcome cannot be computed: {e}")
    units = Decimal(0)
    for _, _, nav in fills:
        units += amt / nav
    if o.instalments != len(fills) or o.units != units:  # the dates reported must be the ones fincalc used
        raise RuntimeError("fund_sip_outcome: the instalment dates disagree with fincalc.funds.sip_outcome")
    value = _q(o.value, 2)
    args = {"scheme_code": code, "amount": str(amt), "start": start.isoformat(), "end": end.isoformat(),
            "day_of_month": day_of_month, "series_sha256": sha256}  # fmt: skip
    comp = (f"fincalc.funds.sip_outcome: {o.instalments} instalments of {amt}, each at the first NAV on or after day "
            f"{day_of_month} of the month; value = units x NAV of {last_day}; xirr_pct = the annualised rate (days / "
            "365) at which the instalments and the value net to zero")  # fmt: skip
    return {"known": True, "scheme_code": code, "scheme": series["scheme"].get("name"),
            "plan": series["scheme"].get("plan"), "option": series["scheme"].get("option"),
            "instalments": o.instalments, "invested": str(_q(o.invested, 2)), "value": str(value),
            "gain": str(value - _q(o.invested, 2)), "units": str(_q(o.units, 4)), "xirr_pct": str(_q(o.xirr * 100, 4)),
            "valuation": {"nav_date": last_day.isoformat(), "nav": str(last_nav)},
            "instalment_navs": [[due.isoformat(), d.isoformat(), str(v)] for due, d, v in fills],
            "instalment_navs_columns": ["instalment_date", "nav_date", "nav"],
            "instalment_dates_without_a_nav_yet": [d.isoformat() for d in dues[len(fills):]],
            "citable_keys": list(CITABLE),
            "provenance": {"dataset": DATASET, "source_url": SOURCE, "series_key": series_key(code, start, end),
                           "series_sha256": sha256, "nav_points": len(navs), "nav_first": navs[0][0].isoformat(),
                           "nav_last": last_day.isoformat(), "start": start.isoformat(), "end": end.isoformat(),
                           "computation": comp, "method_version": METHOD_VERSION},
            "citation": {"fund_fact": {"tool": "fund_sip_outcome", "args": args},
                         "result_key": "<one of citable_keys>"}}  # fmt: skip


def _q(x: Decimal, places: int) -> Decimal:
    from finresearch.fincalc.numbers import round_half_up

    return round_half_up(x, places)


_ARGS = {"scheme_code", "amount", "start", "end", "day_of_month", "series_sha256"}


def run(s: Session, tool: str, args: dict[str, Any], run_id: int | None = None) -> dict[str, Any]:
    """Run a fund-fact tool again on its stored series (save_claim's check, `evidence regrade`): no network. `args`
    are the ones in the `citation` the tool returned. `run_id` is unused: NAVs are public, the same for every run."""
    if tool not in TOOLS:
        raise ValueError(f"unknown fund fact tool {tool!r}; one of {sorted(TOOLS)}")
    a = dict(args or {})
    if set(a) - _ARGS or not {"scheme_code", "amount", "start", "end", "series_sha256"} <= set(a):
        raise ValueError("cite the `citation` fund_sip_outcome returned: its args are scheme_code, amount, start, "
                         "end, day_of_month and series_sha256")  # fmt: skip
    try:
        start, end = date.fromisoformat(str(a["start"])), date.fromisoformat(str(a["end"]))
    except ValueError as e:
        raise ValueError(f"start and end are YYYY-MM-DD dates: {e}") from e
    series = load_series(s, str(a["scheme_code"]), start, end, str(a["series_sha256"]))
    if series is None:
        raise ValueError(f"no stored NAV series for scheme {a['scheme_code']} from {start} to {end} with that "
                         "series_sha256: call fund_sip_outcome and cite the citation it returns")  # fmt: skip
    return sip(series, str(a["series_sha256"]), a["amount"], a.get("day_of_month", 1))
