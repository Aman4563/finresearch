"""ELSS lock-in: which units of an Equity Linked Savings Scheme can be redeemed today, and when the rest unlock (pure).

The rule. Investments under the Equity Linked Savings Scheme, 2005 (Ministry of Finance Notification No. 226/2005
dated 3-11-2005, the scheme that makes ELSS units eligible for s.80C) "shall be locked-in for a period of 3 years
from the date of allotment of units" (Nippon India ELSS Tax Saver Fund SID dated 28-Nov-2025, cover page). Each
allotment is locked separately: "Unit holders will not be able to redeem the Units under the Scheme for a period of
3 years from the date of allotment of respective Units" (Parag Parikh ELSS Tax Saver Fund SID dated 28-Nov-2025,
item XX). So, per FIFO lot of portfolio.lots:
- every SIP instalment and every lump sum is its own allotment and its own lock (a `buy` lot each);
- IDCW (dividend) reinvestment units are a new allotment on the reinvestment date and are locked 3 years from it:
  "The amount invested in the scheme, including IDCW amount reinvested ... shall be subject to a lock-in of 3 years"
  (Nippon SID, risk factor g(i)); the importer records them as a `buy` lot dated the reinvestment;
- a switch-in from another scheme is a purchase (an allotment on the switch date), locked like any purchase; a
  switch-out is a redemption and needs the lock-in completed: "Redemption and Switch Out shall be subject to
  completion of lock in period of 3 years" (PPFAS SID, Redemption);
- bonus units: neither SID says. They are treated as a new allotment on the bonus date (portfolio.lots dates a bonus
  lot that way), i.e. locked 3 years from it [unverified]: the stricter reading, so the app never calls a unit
  redeemable that the registrar would refuse. Growth-only ELSS plans issue no bonus units in practice;
- units arriving by a scheme merger (CAS SWITCH_IN_MERGER) keep the original units' dates; the importer records them
  as an opening balance with an unknown date, so they count as "unknown", never as free, until the date is entered;
- after the death of the investor the nominee may redeem after 1 year (both SIDs): not modelled.

Redemptions are matched first-in-first-out, so the open lots that remain are the newest; the locked ones are the
newest of those. A sale of up to the unlocked units therefore takes only unlocked lots, and the units that can be
redeemed today are exactly the unlocked ones.

The day. "A period of 3 years from the date of allotment" excludes the allotment day (General Clauses Act 1897,
s.9: "from" excludes the first day), so the period ends on the third anniversary and units are redeemable from the
day after it: allotted 10-Jan-2023 → redeemable from 11-Jan-2026. This is the same "held more than N months"
convention as the tax holding periods (fincalc.tax.is_long_term, portfolio.tax_watch.lt_date). A registrar may free
units on the anniversary itself; the app then shows them as locked for one extra day (the safe side) [unverified].
A 29-Feb allotment unlocks on 1-Mar (fincalc.tax.add_months falls back to 28-Feb).

Detection. AMFI's scheme category (NAVAll headings, read 1-Oct-2026) spells ELSS three ways: "Equity Scheme - ELSS",
"Equity Schemes - ELSS- Tax Saver Fund" and "ELSS" (close-ended series), so any category containing "ELSS" counts.
Passive ELSS index funds are filed under "Index Funds - Equity Funds" / "Other Scheme - Index Funds" (360 ONE ELSS
Tax Saver Nifty 50 Index Fund, Zerodha ELSS Tax Saver Nifty LargeMidcap 250 Index Fund) and are recognised by "ELSS"
in the name, marked unverified. "Tax saver" alone counts only when no category is known.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from finresearch.fincalc.tax import add_months

ZERO = Decimal(0)
EPS = Decimal("0.0005")  # units below this are rounding noise (portfolio.lots.EPS)
LOCK_YEARS = 3
LOCK_MONTHS = 12 * LOCK_YEARS
RULE = ("ELSS units are locked for 3 years from the allotment of each lot (every SIP instalment, IDCW reinvestment "
        "and switch-in separately); redeemable from the day after the third anniversary")  # fmt: skip
SOURCE = ("Equity Linked Savings Scheme, 2005 (Notification No. 226/2005 dated 3-11-2005) as quoted in the Nippon "
          "India and PPFAS ELSS SIDs (28-Nov-2025)")  # fmt: skip
BONUS_NOTE = "bonus units are taken as allotted on the bonus date (locked 3 years from it) [unverified]"
UNKNOWN_NOTE = ("units with an unknown purchase date (a statement's opening balance) cannot be dated: enter the date "
                "on the holding to know whether they are free")  # fmt: skip
NAME_ONLY_WORDS = ("tax saver", "taxsaver", "tax saving")


class LotLike(Protocol):
    acquired: date | None
    open_quantity: Decimal
    origin: str


@dataclass(frozen=True)
class Detection:
    verified: bool  # True: AMFI's category says ELSS; False: recognised by the scheme name only
    why: str

    def json(self) -> dict[str, Any]:
        return {"verified": self.verified, "why": self.why}


def detect(asset_type: str, name: str, category: str | None) -> Detection | None:
    """Is this holding an ELSS? AMFI's category first, the scheme name as the fallback (unverified)."""
    if asset_type != "mf":
        return None
    cat = (category or "").strip()
    if "elss" in cat.lower():
        return Detection(True, f"AMFI category {cat}")
    n = f" {(name or '').lower()} "
    if "elss" in n:
        where = f"AMFI category {cat}, not ELSS (passive ELSS funds are filed as index funds)" if cat else \
            "no AMFI category known"  # fmt: skip
        return Detection(False, f"'ELSS' in the scheme name; {where} [unverified]")
    if not cat and any(w in n for w in NAME_ONLY_WORDS):
        return Detection(False, "'tax saver' in the scheme name; no AMFI category known [unverified]")
    return None


def unlock_date(acquired: date) -> date:
    """The first day a lot allotted on `acquired` can be redeemed: the day after the third anniversary."""
    return add_months(acquired, LOCK_MONTHS) + timedelta(days=1)


def _units(x: Decimal) -> float:
    return round(float(x), 4)


def _value(units: Decimal, price: Decimal | None) -> float | None:
    return None if price is None else round(float(units * price), 2)


def lockin(lots: Iterable[LotLike], today: date, price: Decimal | None = None,
           detection: Detection | None = None) -> dict[str, Any]:  # fmt: skip
    """Locked, unlocked and undated units of one ELSS holding on `today`, the next unlock and the unlock schedule by
    month. Values are units × `price` (None without a price). `sellable_units` = the unlocked units (see the module
    note on FIFO)."""
    open_lots = [lot for lot in lots if lot.open_quantity > EPS]
    locked = unlocked = unknown = ZERO
    rows, by_day = [], defaultdict(lambda: ZERO)
    bonus = False
    for lot in sorted(open_lots, key=lambda x: (x.acquired is not None, x.acquired or date.min)):
        q = Decimal(lot.open_quantity)
        if lot.acquired is None:
            unknown += q
            rows.append({"acquired": None, "origin": lot.origin, "units": _units(q), "unlocks": None,
                         "status": "unknown"})  # fmt: skip
            continue
        free = unlock_date(lot.acquired)
        is_locked = today < free
        if is_locked:
            locked += q
            by_day[free] += q
        else:
            unlocked += q
        bonus = bonus or (lot.origin == "bonus" and is_locked)
        rows.append({"acquired": lot.acquired.isoformat(), "origin": lot.origin, "units": _units(q),
                     "unlocks": free.isoformat(), "status": "locked" if is_locked else "unlocked"})  # fmt: skip
    months: dict[str, dict[str, Any]] = {}
    for d in sorted(by_day):
        m = months.setdefault(d.strftime("%Y-%m"), {"month": d.strftime("%Y-%m"), "first": d.isoformat(),
                                                    "units": ZERO, "lots": 0})  # fmt: skip
        m["units"] += by_day[d]
        m["lots"] += sum(1 for r in rows if r["unlocks"] == d.isoformat() and r["status"] == "locked")
    schedule = [
        {**m, "units": _units(m["units"]), "value": _value(m["units"], price)} for m in months.values()
    ]
    nxt = None
    if by_day:
        d = min(by_day)
        nxt = {"day": d.isoformat(), "days": (d - today).days, "units": _units(by_day[d]),
               "value": _value(by_day[d], price)}  # fmt: skip
    notes = []
    if unknown > EPS:
        notes.append(UNKNOWN_NOTE)
    if bonus:
        notes.append(BONUS_NOTE)
    if detection is not None and not detection.verified:
        notes.append("recognised as ELSS by its name only: check the scheme's AMFI category")
    return {
        "detected": detection.json() if detection else None,
        "as_of": today.isoformat(), "price": None if price is None else float(price),
        "locked_units": _units(locked), "locked_value": _value(locked, price),
        "unlocked_units": _units(unlocked), "unlocked_value": _value(unlocked, price),
        "unknown_units": _units(unknown), "unknown_value": _value(unknown, price),
        "sellable_units": _units(unlocked), "next_unlock": nxt, "schedule": schedule, "lots": rows,
        "rule": RULE, "source": SOURCE, "notes": notes,
    }  # fmt: skip


def split_units(lots: Iterable[LotLike], today: date) -> tuple[Decimal, Decimal, Decimal]:
    """(unlocked, locked, unknown-date) open units as Decimals, for callers that cap a sale."""
    u = lk = un = ZERO
    for lot in lots:
        q = Decimal(lot.open_quantity)
        if q <= EPS:
            continue
        if lot.acquired is None:
            un += q
        elif today < unlock_date(lot.acquired):
            lk += q
        else:
            u += q
    return u, lk, un


def category_of(h: Any, valuation: dict[str, Any] | None = None, category: str | None = None) -> str | None:
    """The best AMFI category known for a holding: a fresh price lookup's, else the monitor's daily valuation's
    (cache:valuation), else the holding's own column."""
    if category:
        return category
    row = ((valuation or {}).get("holdings") or {}).get(str(h.id)) or {}
    return row.get("category") or getattr(h, "category", None)
