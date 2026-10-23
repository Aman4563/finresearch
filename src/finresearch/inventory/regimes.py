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
from typing import Any

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
    also: tuple[
        str, ...
    ] = ()  # further tags the same event changed (the 2024 circular also changed lot sizes)

    @property
    def tags(self) -> tuple[str, ...]:
        return (self.tag, *self.also)


_FIXED = (
    RegimeChange(date(2024, 11, 20), "weekly_expiries", "one weekly index expiry per exchange (BANKNIFTY, FINNIFTY "
                 "and MIDCPNIFTY weeklies ended) and a ₹15 lakh minimum contract value (lot sizes changed)", SEBI_2024,
                 ("lot_size",)),
    RegimeChange(date(2025, 9, 1), "expiry_weekday", "NIFTY's weekly expiry moved from Thursday to Tuesday",
                 "issue #379; NSE expiry calendar"),
)  # fmt: skip


def regime_changes() -> list[RegimeChange]:
    """Every known regime change, oldest first: the fixed market-structure events plus the STT start dates."""
    # every STT row starts a change, the earliest included (dated from 2013 since #382; changes before a run's day
    # never make it stale, so only 1-Oct-2024 on can matter for this app's runs)
    by_day: dict[date, list[charges.ChargeRate]] = {}
    for r in charges.RATES:
        if r.key.startswith("stt_") and r.rate is not None:  # an unknown-period row (#365) is not a change
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


def staleness(day: date | None, today: date, max_age: int = MAX_EVIDENCE_AGE_DAYS,
              tags: dict[str, Any] | None = None) -> list[str]:  # fmt: skip
    """Why evidence from a run on `day` is stale on `today` ([] = current). `tags`: the run's stored regime tags
    (regime_state on its day; None for a run saved before they were stored, judged by its date alone)."""
    if day is None:
        return ["the run's date is unknown, so its evidence cannot be shown to be current"]
    why = tag_changes(tags, today) if tags is not None else []
    age = (today - day).days
    if age > max_age:
        why.append(f"the backtest run is {age} days old (evidence expires after {max_age} days): re-run it")
    why += [f"the market changed on {c.day:%d-%b-%Y} after the run ({c.what})"
            for c in regime_changes() if day < c.day <= today]  # fmt: skip
    return why


# the issue's regime split for re-runs: "before and after 20-Nov-2024 and 1-Sep-2025" (the fixed market-structure
# changes; STT changes date-stale a run but do not split the re-validation)
SPLIT_DAYS = tuple(c.day for c in _FIXED)

TAGS = ("expiry_weekday", "weekly_expiries", "lot_size", "stt")


def regime_state(day: date) -> dict[str, str]:
    """The regime each tag was in on `day`: the ISO day its last change started (on or before `day`), or
    "before <first change>" when none had happened yet. Stored on each saved run (#379), so a later change of any
    tag is visible from the run itself, not only from its date."""
    out: dict[str, str] = {}
    for tag in TAGS:
        days = sorted(c.day for c in regime_changes() if tag in c.tags)
        past = [d for d in days if d <= day]
        out[tag] = (
            past[-1].isoformat() if past else (f"before {days[0].isoformat()}" if days else "no change known")
        )
    return out


def tag_changes(tags: dict[str, Any], today: date) -> list[str]:
    """Why a run's stored regime tags differ from today's ([] = the same regime on every tag). A missing or
    unreadable tag is a difference: unknown never reads as current."""
    now = regime_state(today)
    if not isinstance(tags, dict):
        return [
            f"the run's stored regime tags are unreadable ({type(tags).__name__}), so its regime is unknown"
        ]
    return [f"the {t} regime changed since the run (run: {tags.get(t) or 'unknown'}, now: {now[t]})"
            for t in TAGS if tags.get(t) != now[t]]  # fmt: skip
