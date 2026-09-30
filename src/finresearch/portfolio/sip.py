"""SIP health: recurring mutual-fund purchases inferred from the transaction cadence (the CAS carries no mandate).

A holding is treated as a SIP when its last purchases (reinvested dividends excluded) are regular:
- at least MIN_INSTALMENTS buys,
- consecutive gaps of 25-35 days (monthly),
- each amount within ±25 % of the median amount (step-ups and small top-ups still count).

Status from the days since the last instalment (the research plan's "expected monthly buy absent for > 35 days"):
- "on track": ≤ 35 days;
- "missed": 36-65 days (one instalment is missing);
- "stopped": > 65 days (two or more are missing).

The next expected date is the last instalment plus one calendar month. Inference errors are possible for irregular
manual investing (say so in the UI); a SIP is a discipline, not an edge (lump sum beats averaging in rising markets
in expectation [U: Vanguard lump-sum vs DCA]).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from statistics import median
from typing import Any

from finresearch.fincalc.tax import add_months

MIN_INSTALMENTS = 3
GAP_MIN, GAP_MAX = 25, 35
ON_TRACK_DAYS, STOPPED_DAYS = 35, 65
AMOUNT_BAND = Decimal("0.25")


@dataclass
class Sip:
    holding_id: int
    name: str
    account: str
    amount: Decimal  # median instalment
    day_of_month: int
    instalments: int
    last: date
    next_expected: date
    days_since: int
    status: str  # on track | missed | stopped

    def json(self) -> dict[str, Any]:
        return {"holding_id": self.holding_id, "name": self.name, "account": self.account,
                "amount": float(self.amount), "day_of_month": self.day_of_month, "instalments": self.instalments,
                "last": self.last.isoformat(), "next_expected": self.next_expected.isoformat(),
                "days_since": self.days_since, "status": self.status}  # fmt: skip


def infer_sip(buys: list[tuple[date, Decimal]]) -> tuple[int, Decimal, int, date] | None:
    """(instalments, median amount, day of month, last instalment) of the longest regular monthly run ending at one
    of the latest three buys (so a one-off top-up after the last instalment does not hide the SIP), or None.
    `buys` = (day, amount) of purchases, any order."""
    rows = sorted((d, a) for d, a in buys if a and a > 0)
    if len(rows) < MIN_INSTALMENTS:
        return None
    best: list[tuple[date, Decimal]] = []
    for end in range(len(rows) - 1, max(-1, len(rows) - 4), -1):
        run = [rows[end]]
        for prev in reversed(rows[:end]):
            gap = (run[0][0] - prev[0]).days
            if gap < GAP_MIN:  # a second buy in the same month (a top-up) neither breaks nor extends the run
                continue
            if gap > GAP_MAX:
                break
            run.insert(0, prev)
        if len(run) < MIN_INSTALMENTS:
            continue
        med = Decimal(str(median([a for _, a in run])))
        if any(abs(a - med) > med * AMOUNT_BAND for _, a in run):
            continue
        if len(run) > len(best):
            best = run
    if not best:
        return None
    med = Decimal(str(median([a for _, a in best])))
    return len(best), med, int(median([d.day for d, _ in best])), best[-1][0]


def status_for(days_since: int) -> str:
    if days_since <= ON_TRACK_DAYS:
        return "on track"
    if days_since <= STOPPED_DAYS:
        return "missed"
    return "stopped"


def sip_health(holdings: list[Any], txns: dict[int, list[Any]], today: date) -> list[Sip]:
    """Inferred SIPs across mutual-fund holdings (a `report.Loaded`'s holdings and txns)."""
    out = []
    for h in holdings:
        if h.asset_type != "mf":
            continue
        buys = []
        for t in txns.get(h.id, []):
            if t.kind != "buy" or (t.meta or {}).get("reinvest"):
                continue
            amt = abs(t.amount) if t.amount is not None else (t.quantity or 0) * (t.price or 0)
            buys.append((t.day, Decimal(str(amt))))
        got = infer_sip(buys)
        if got is None:
            continue
        n, amount, dom, last = got
        days = (today - last).days
        out.append(
            Sip(h.id, h.name, h.account, amount, dom, n, last, add_months(last, 1), days, status_for(days))
        )
    return sorted(out, key=lambda s: ({"stopped": 0, "missed": 1, "on track": 2}[s.status], s.next_expected))
