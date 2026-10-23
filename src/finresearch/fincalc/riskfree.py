"""The risk-free rate without FBIL (#479, the owner's decision of 23-Oct-2026): an owner-entered rate, else the RBI
repo-rate proxy, else unknown. Pure: the caller passes the owner's entries (signals.rates reads them from the
`owner_rate` table) and the policy rates are fincalc.policy_rates' hand-typed table. Nothing is fetched from anyone.

Conventions. RBI quotes T-bill cut-offs as simple yields on an Actual/365 basis, y = (100 - P)/P x 365/D, and the
policy repo rate as a simple annual rate. Every rate here is such a yield y for a term of D days (D = T x 365,
at least 1). Option maths needs it continuously compounded, z = ln(1 + y D/365) / (D/365); as an effective annual
rate it is (1 + y D/365)^(365/D) - 1. RBI's 91-day cut-off of 5.7281 % on 14-Oct-2026 is 5.6876 % continuous and
5.8525 % effective annual (research note "Indian risk free rate choices", 23-Oct-2026).

Tier "owner": a yield the owner typed in (signals.rates / the POST API / `finresearch rates enter`), for example
RBI's 91-day cut-off on an auction day. The latest entry per tenor on or before the day is used as typed while the
repo rate has not moved since its date, else shifted by the repo change since then (an MPC hold moves nothing).
Between entered tenors the yield is linear in D; outside them it is held flat.

Tier "proxy" ("RBI repo-rate proxy"): y = the repo rate in force on the day + SPREAD_BP. The spread is 0 bp at every
tenor, a stated choice, not a measurement:
* up to 91 days: the 91-day T-bill yield tracks the repo rate with a spread that changes sign with banking-system
  liquidity, so no fixed spread is right across 2015-2026: about 81 bp below repo in 2020's surplus, 52 bp above in
  Jan-2024's deficit, and 2-9 bp above on average in 2026's four policy periods (the research note above). Expect
  an error of about +/-25 bp, at worst about +/-80 bp.
* 182 and 364 days (and beyond): held flat at the repo rate, which leaves out the term premium: RBI's cut-offs of
  14-Oct-2026 were 6.2896 % (182 days) and 6.4891 % (364 days) at a repo rate of 5.50 %, 79 and 99 bp over it, and
  in easing cycles longer bills trade below repo. One auction is not a basis for a spread, so none is typed in; the
  owner-entered tier carries observed 182/364-day yields, and every answer beyond 91 days says it is held flat.

Unknown: before policy_rates.START (01-Jan-2015), with the reason; never a fixed rate.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from finresearch.fincalc import policy_rates as P

BASIS = "simple yield, Actual/365 (RBI's T-bill and repo convention)"
SOURCE = "RBI repo-rate proxy"
SPREAD_BP = 0  # every tenor (see the module docstring)
FLAT_BEYOND = 91  # days: no term spread is added beyond this tenor
MAX_TENOR_DAYS = 364  # the longest owner-entered tenor (RBI's longest T-bill)


@dataclass(frozen=True)
class OwnerRate:
    day: date  # the day the yield was observed (an auction day, say)
    tenor_days: int
    yld: Decimal  # simple, Actual/365, a fraction: 0.057281 = 5.7281 %
    source: str | None = None  # what the owner typed as its source
    entered_at: datetime | None = None


def tenor_days(t_years: float) -> int:
    return max(1, round(float(t_years) * 365))


def continuous(y: float, days: int) -> float:
    """The continuously compounded rate equal to a simple Actual/365 yield `y` for `days` days."""
    return math.log1p(y * days / 365) * 365 / days


def effective_annual(y: float, days: int) -> float:
    """The effective annual rate of a simple Actual/365 yield `y` for `days` days, compounded at that term."""
    return (1 + y * days / 365) ** (365 / days) - 1


def _pct(x: float | Decimal) -> str:
    return f"{float(x) * 100:.2f} %"


def _flat_note(days: int) -> str:
    if days <= FLAT_BEYOND:
        return ""
    return (f"; held flat beyond {FLAT_BEYOND} days: no term spread is added (RBI's 182/364-day cut-offs were 79/99 "
            "bp over repo on 14-Oct-2026)")  # fmt: skip


def _base(days: int) -> dict[str, Any]:
    return {"tenor_days": days, "tenor_years": days / 365, "par_yield": None, "basis": BASIS, "fallback": False,
            "shift_bp": None, "url": None, "stale": False}  # fmt: skip


def unknown(day: date, days: int, why: str | None = None) -> dict[str, Any]:
    why = why or (f"{day:%d-%b-%Y} is before {P.START:%d-%b-%Y}, where the RBI policy-rate table starts: the "
                  "risk-free rate is unknown")  # fmt: skip
    return _base(days) | {"known": False, "tier": None, "rate": None, "yield": None, "as_of": None,
                          "source": None, "note": why}  # fmt: skip


def _known(tier: str, y: float, days: int, as_of: date, source: str, note: str, **extra: Any) -> dict[str, Any]:
    r = continuous(y, days)
    note = f"{note}, {BASIS.split(' (')[0]} -> {r * 100:.2f} % continuous{_flat_note(days)}"
    return _base(days) | {"known": True, "tier": tier, "rate": r, "yield": y, "as_of": as_of.isoformat(),
                          "source": source, "note": note} | extra  # fmt: skip


def proxy(day: date, days: int, *, before: bool = False) -> dict[str, Any]:
    """The RBI repo-rate proxy for `days` days on `day` (see the module docstring)."""
    row = P.in_force(day, before=before)
    if row is None:
        return unknown(day, days)
    y = float(row.repo) + SPREAD_BP / 10000
    src = f"{SOURCE}: repo {_pct(row.repo)} in force from {row.effective:%d-%b-%Y}"
    note = f"repo {_pct(row.repo)} + {SPREAD_BP} bp at {days} days"
    return _known("proxy", y, days, row.effective, src, note, url=row.url,
                  policy={"repo": float(row.repo), "floor": float(row.floor), "floor_kind": row.floor_kind,
                          "msf": float(row.msf), "effective": row.effective.isoformat(), "title": row.title})  # fmt: skip


def _nodes(entries: Sequence[OwnerRate], day: date, before: bool) -> list[tuple[OwnerRate, Decimal]]:
    """The latest entry per tenor usable on `day`, each with the repo change since its own date."""
    latest: dict[int, OwnerRate] = {}
    for e in entries:
        if e.day < P.START or e.day > day or (before and e.day == day):
            continue
        if e.tenor_days not in latest or e.day > latest[e.tenor_days].day:
            latest[e.tenor_days] = e
    out = []
    for t in sorted(latest):
        shift = P.repo_change(latest[t].day, day, before=before)
        if shift is not None:
            out.append((latest[t], shift))
    return out


def owner(day: date, days: int, entries: Sequence[OwnerRate], *, before: bool = False) -> dict[str, Any] | None:
    """The owner-entered tier for `days` days on `day`, or None when no entry is usable."""
    nodes = _nodes(entries, day, before)
    if not nodes:
        return None
    lo = max((n for n in nodes if n[0].tenor_days <= days), default=nodes[0], key=lambda n: n[0].tenor_days)
    hi = min((n for n in nodes if n[0].tenor_days >= days), default=nodes[-1], key=lambda n: n[0].tenor_days)
    used = [lo] if lo is hi else [lo, hi]
    ys = [float(e.yld + s) for e, s in used]
    if len(used) == 1:
        y = ys[0]
    else:
        w = (days - lo[0].tenor_days) / (hi[0].tenor_days - lo[0].tenor_days)
        y = ys[0] + (ys[1] - ys[0]) * w
    parts = [f"{e.tenor_days}D {_pct(e.yld)} entered for {e.day:%d-%b-%Y}"
             + (f", {float(s) * 10000:+.0f} bp (the repo change since)" if s else ", no repo change since")
             for e, s in used]  # fmt: skip
    shifts = {round(float(s) * 10000) for _, s in used}
    how = "interpolated at" if len(used) == 2 else "held at" if used[0][0].tenor_days != days else "at"
    src = "owner-entered rate: " + "; ".join(f"{e.tenor_days}D for {e.day:%d-%b-%Y}" for e, _ in used)
    cites = "; ".join(e.source for e, _ in used if e.source) or None
    return _known("owner", y, days, min(e.day for e, _ in used), src, f"{'; '.join(parts)}; {how} {days} days",
                  shift_bp=shifts.pop() if len(shifts) == 1 else None, url=cites)  # fmt: skip


def answer(day: date, t_years: float, entries: Sequence[OwnerRate] = (), *, before: bool = False) -> dict[str, Any]:
    """The risk-free rate for a term of `t_years` on `day`: the owner's entries, else the RBI repo-rate proxy, else
    unknown. `before`: only what was known at the start of `day` (no entry or decision dated `day`)."""
    days = tenor_days(t_years)
    if day < P.START:
        return unknown(day, days)
    return owner(day, days, entries, before=before) or proxy(day, days, before=before)
