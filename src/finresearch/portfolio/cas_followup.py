"""Keeping mutual-fund units current between detailed CAS imports (#296).

* A depository (NSDL/CDSL) e-CAS lists holdings only. For each mutual-fund holding it records
  `meta.depository_units = {units, day, source, lot_units, scope}` and a newer `meta.statement_nav`, and when the units
  differ from the lots on the statement day it raises ONE alert per statement asking for a fresh detailed
  CAMS/KFintech CAS (the lots are built from transactions, which only that statement has).
* `last_covered_day`: the latest end of an imported CAMS/KFintech CAS period. A new CAS that starts after it leaves a
  gap; service.statement_gap_warnings compares its opening balances with the lots.
* `reminder_step`: once a month (IST), when the previous month is not covered yet, an in-app alert links the CAMS and
  KFintech mailback forms with the "from" date to ask for. The forms have a captcha and a user-chosen password, so
  nothing is submitted for the user.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, PortfolioHolding, PortfolioImport, PortfolioTxn
from finresearch.portfolio.importers import ImportResult, parse_day

UNITS_ALERT = "cas_units_mismatch"
REMINDER = "cas_reminder"
# the registrars' own request pages (checked 2026-10-09): Statement type "Detailed", a period, a password you choose
MAILBACK = (
    {"label": "CAMS mailback (CAS)", "url": "https://www.camsonline.com/Investors/Statements/Consolidated-Account-Statement"},
    {"label": "KFintech mailback (CAS)", "url": "https://mfs.kfintech.com/investor/General/ConsolidatedAccountStatement"},
)  # fmt: skip
RTA_SOURCES = ("CAMS", "KFINTECH")
TOL = Decimal("0.001")  # CAS units are printed to 3 decimals


def _is_fund(ikey: str) -> bool:
    # Indian mutual-fund units carry ISINs beginning "INF" (NSDL's ISIN structure: IN + issuer type F = mutual fund)
    return ikey.startswith("ISIN:INF") or ikey.startswith("MF:")


def _folio(v: str) -> str:
    return re.sub(r"\s+", "", v).lower()


def units_on(s: Session, holding_ids: list[int], day: date | None) -> Decimal:
    """Units the lots hold at the end of `day` (all of them when the day is unknown)."""
    from finresearch.portfolio.lots import build_lots
    from finresearch.portfolio.service import events_of

    total = Decimal(0)
    for hid in holding_ids:
        q = select(PortfolioTxn).where(PortfolioTxn.holding_id == hid)
        if day is not None:
            q = q.where(PortfolioTxn.day <= day)
        total += build_lots(events_of(s.scalars(q).all())).units
    return total


def record_depository(s: Session, res: ImportResult) -> list[dict[str, Any]]:
    """Store a depository statement's fund units (and newer NAV) on the matching holdings; return the funds whose
    units differ from the lots on the statement day (a fund the lots do not have at all included)."""
    day = parse_day(res.period[1]) if res.period and res.period[1] else None
    iso = day.isoformat() if day else None
    out: list[dict[str, Any]] = []
    for c in res.closing:
        if not _is_fund(c.ikey):
            continue
        isin = c.ikey[5:] if c.ikey.startswith("ISIN:") else None
        hs = list(s.scalars(select(PortfolioHolding).where(
            or_(PortfolioHolding.ikey == c.ikey, *([PortfolioHolding.isin == isin] if isin else [])))))  # fmt: skip
        scope = "instrument"
        if c.folio:
            same = [h for h in hs if _folio(h.account).endswith("folio" + _folio(c.folio))]
            if same:
                hs, scope = same, "folio"
        have = units_on(s, [h.id for h in hs], day)
        for h in hs:
            meta = dict(h.meta or {})
            prev = meta.get("depository_units") or {}
            if not prev or (iso and (prev.get("day") or "") <= iso):
                meta["depository_units"] = {"units": str(c.units), "day": iso, "source": res.source,
                                            "lot_units": str(have.quantize(TOL)), "scope": scope}  # fmt: skip
            nav = meta.get("statement_nav") or {}
            if c.nav is not None and iso and (not nav or nav.get("day", "") < iso):
                meta["statement_nav"] = {"nav": str(c.nav), "day": iso, "source": res.source}
            h.meta = meta
        if abs(have - c.units) > TOL:
            out.append({"name": c.name, "statement_units": str(c.units), "lot_units": str(have.quantize(TOL)),
                        "day": iso, "scope": scope})  # fmt: skip
    s.flush()
    return out


def units_alert(s: Session, res: ImportResult, diffs: list[dict[str, Any]], state: dict[str, Any]) -> bool:
    """One alert for a depository statement whose fund units differ from the lots; never twice for the same
    statement (`state["depository_alerted"]`, the inbox row's state, keyed by source and statement day)."""
    if not diffs:
        return False
    key = f"{res.source}:{diffs[0]['day'] or (res.period[1] if res.period else '')}"
    if state.get("depository_alerted") == key:
        return False
    names = ", ".join(d["name"] for d in diffs[:3]) + (
        f" and {len(diffs) - 3} more" if len(diffs) > 3 else ""
    )
    when = f" on {diffs[0]['day']}" if diffs[0]["day"] else ""
    s.add(Alert(kind=UNITS_ALERT, level="warn",
                message=f"{res.source} e-CAS{when}: units of {len(diffs)} fund(s) differ from your lots ({names}). "
                        "Import a fresh detailed CAMS/KFintech CAS so the transactions behind them are recorded.",
                data={"path": "/portfolio", "source": res.source, "day": diffs[0]["day"], "funds": diffs[:20],
                      "links": list(MAILBACK)}))  # fmt: skip
    state["depository_alerted"] = key
    return True


def last_covered_day(s: Session) -> date | None:
    """The latest statement-period end of an imported CAMS/KFintech CAS (None before the first)."""
    best: date | None = None
    for summ in s.scalars(select(PortfolioImport.summary).where(PortfolioImport.kind == "cas",
                                                                 PortfolioImport.source.in_(RTA_SOURCES))):  # fmt: skip
        period = (summ or {}).get("period") or []
        end = parse_day(period[1]) if len(period) > 1 and period[1] else None
        if end is not None and (best is None or end > best):
            best = end
    return best


def reminder_step(s: Session, now: datetime) -> dict[str, int]:
    """Once a calendar month (IST): when the detailed CAS imports stop before the previous month's end, an alert links
    the CAMS and KFintech mailback forms with the date to request from (the day after the last covered day)."""
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    month = f"{today:%Y-%m}"
    last = last_covered_day(s)
    if last is None or last >= today.replace(day=1) - timedelta(days=1):
        return {}
    for data in s.scalars(select(Alert.data).where(Alert.kind == REMINDER)):
        if (data or {}).get("month") == month:
            return {}
    start = last + timedelta(days=1)
    s.add(Alert(kind=REMINDER, level="info",
                message=f"Your mutual-fund transactions are recorded up to {last:%d %b %Y}. Request a Detailed CAS "
                        f"from {start:%d %b %Y} to today (CAMS and KFintech mailback forms), then drop the PDF in the "
                        "statement inbox.",
                data={"month": month, "from": start.isoformat(), "last_covered": last.isoformat(),
                      "links": list(MAILBACK), "path": "/profile#connections"}))  # fmt: skip
    s.flush()
    return {"cas_reminders": 1}
