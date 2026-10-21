"""When saved evidence stops counting (#379): too old, or computed before the market's rules changed.

    stale  <=>  today − run day > MAX_EVIDENCE_AGE_DAYS  or  a regime change falls in (run day, today]

A run whose date is unknown is stale (unknown never reads as current). Stale evidence caps a verdict at WAIT with the
reason (signals.tradeplan); it is shown as unvalidated, never as fine.

Regime changes are the market-structure events that break F&O evidence:
- 20-Nov-2024: SEBI circular SEBI/HO/MRD/TPD/P/CIR/2024/132 of 1-Oct-2024 (cited in fnohist/derive.py) left one weekly
  index expiry per exchange (BANKNIFTY, FINNIFTY and MIDCPNIFTY weeklies ended) and raised the minimum contract value
  to ₹15 lakh, which changed lot sizes. 20-Nov-2024 is the date given in issue #379; not re-read from the circular.
- 1-Sep-2025: NIFTY's weekly expiry moved from Thursday to Tuesday (issue #379; PRODUCT_REQUIREMENTS DATA-005 checked
  NSE's live expiries were Tuesdays on 1-Oct-2026). The issue's regime split is "before and after 1-Sep-2025".
- STT changes: every distinct start date of an `stt_*` row in the dated charges table (fincalc/charges.py), so a new
  STT row starts a new regime without editing this file. Other charge rows carry "earliest date we can vouch for",
  not a change, so they are not regimes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from finresearch.fincalc import charges
from finresearch.fincalc.dates import to_ist

MAX_EVIDENCE_AGE_DAYS = 45  # issue #379's example max_age: a monthly re-run plus two weeks' slack
SEBI_2024 = ("https://www.sebi.gov.in/legal/circulars/oct-2024/measures-to-strengthen-equity-index-derivatives-"
             "framework-for-increased-investor-protection-and-market-stability_87208.html")  # fmt: skip


@dataclass(frozen=True)
class RegimeChange:
    day: date  # the first day of the new regime
    tag: str  # expiry_weekday | weekly_expiries | lot_size | stt
    what: str
    source: str


_FIXED = (
    RegimeChange(date(2024, 11, 20), "weekly_expiries", "one weekly index expiry per exchange (BANKNIFTY, FINNIFTY "
                 "and MIDCPNIFTY weeklies ended) and a ₹15 lakh minimum contract value (lot sizes changed)", SEBI_2024),
    RegimeChange(date(2025, 9, 1), "expiry_weekday", "NIFTY's weekly expiry moved from Thursday to Tuesday",
                 "issue #379; NSE expiry calendar"),
)  # fmt: skip


def regime_changes() -> list[RegimeChange]:
    """Every known regime change, oldest first: the fixed market-structure events plus the STT start dates."""
    # every STT row starts a change, the earliest included (Finance (No. 2) Act 2024 raised option STT from 1-Oct-2024)
    by_day: dict[date, list[charges.ChargeRate]] = {}
    for r in charges.RATES:
        if r.key.startswith("stt_"):
            by_day.setdefault(r.effective_from, []).append(r)
    stt = [RegimeChange(d, "stt", "STT rates changed (" + ", ".join(sorted(r.key for r in rows)) + ")", rows[0].source)
           for d, rows in by_day.items()]  # fmt: skip
    return sorted([*_FIXED, *stt], key=lambda c: c.day)


def run_day(created_at: str | datetime | None) -> date | None:
    """The IST date of a run's `created_at` (ISO string or aware datetime); None when missing or unreadable."""
    if created_at is None:
        return None
    try:
        dt = datetime.fromisoformat(created_at) if isinstance(created_at, str) else created_at
        return to_ist(dt).date()
    except (TypeError, ValueError):
        return None


def staleness(day: date | None, today: date, max_age: int = MAX_EVIDENCE_AGE_DAYS) -> list[str]:
    """Why evidence from a run on `day` is stale on `today` ([] = current)."""
    if day is None:
        return ["the run's date is unknown, so its evidence cannot be shown to be current"]
    why = []
    age = (today - day).days
    if age > max_age:
        why.append(f"the backtest run is {age} days old (evidence expires after {max_age} days): re-run it")
    why += [f"the market changed on {c.day:%d-%b-%Y} after the run ({c.what})"
            for c in regime_changes() if day < c.day <= today]  # fmt: skip
    return why
