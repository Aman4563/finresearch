"""Portfolio metrics for the alert rules (finresearch.alerts.portfolio contract) and the value history behind them.

    alert_metrics(session) -> {"allocation_drift_pp": (value, source), "ltcg_headroom_inr": (...), "drawdown_pct": (...)}

Synchronous and offline: it reads the database only. Market values come from the latest snapshot written when the
portfolio was last valued (the /portfolio page or GET /api/portfolio), so the source string always says its date.

- ltcg_headroom_inr: the unused s.112A / s.198 exemption in the current financial year, after this year's realised
  equity LTCG and set-off (fincalc.tax.fy_tax). Needs no prices.
- allocation_drift_pp: the largest |weight - target| in percentage points across asset classes, from the latest
  complete snapshot and the targets saved on /portfolio (Allocation tab).
- drawdown_pct: how far a time-weighted value index sits below its peak, in % (0 at a new high). The index chains
  day-to-day returns with net new money removed ((V_t - flow_t) / V_{t-1}), so buying more or selling does not look
  like a gain or a loss. Only days on which every holding was priced count.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioSetting, PortfolioSnapshot

ASSET_CLASSES = (
    "Stocks",
    "Equity funds",
    "Debt funds",
    "Gold & international funds",
    "Sovereign Gold Bonds",
    "Other",
)


def get_targets(s: Session) -> dict[str, float]:
    row = s.get(PortfolioSetting, "targets")
    return {k: float(v) for k, v in ((row.value or {}) if row else {}).items() if k in ASSET_CLASSES}


def set_targets(s: Session, targets: dict[str, Any]) -> dict[str, float]:
    clean = {k: round(float(v), 2) for k, v in targets.items() if k in ASSET_CLASSES and v not in (None, "")}
    if any(v < 0 or v > 100 for v in clean.values()):
        raise ValueError("each target must be between 0 and 100 %")
    total = sum(clean.values())
    if clean and abs(total - 100) > 0.5:
        raise ValueError(f"targets must add up to 100 % (they add up to {total:g} %)")
    row = s.get(PortfolioSetting, "targets")
    if row is None:
        s.add(PortfolioSetting(key="targets", value=clean))
    else:
        row.value = clean
    s.flush()
    return clean


def drift(by_asset: dict[str, float], targets: dict[str, float]) -> list[dict[str, Any]]:
    """Weight vs target per asset class (pp), largest gap first. Empty without targets or value."""
    total = sum(by_asset.values())
    if not targets or total <= 0:
        return []
    rows = []
    for k in sorted(set(targets) | set(by_asset)):
        w = by_asset.get(k, 0.0) / total * 100
        t = targets.get(k, 0.0)
        rows.append({"label": k, "weight_pct": round(w, 2), "target_pct": t, "drift_pp": round(w - t, 2)})
    return sorted(rows, key=lambda r: (-abs(r["drift_pp"]), -r["drift_pp"]))  # ties: overweight first


def record_snapshot(s: Session, day: date, value: float, invested: float, by_asset: dict[str, float],
                    complete: bool) -> None:  # fmt: skip
    """Upsert the day's value (the latest valuation of the day wins)."""
    vals = {"day": day, "value": Decimal(str(round(value, 2))), "invested": Decimal(str(round(invested, 2))),
            "by_asset": {k: round(v, 2) for k, v in by_asset.items()}, "complete": complete}  # fmt: skip
    stmt = insert(PortfolioSnapshot).values(**vals)
    s.execute(
        stmt.on_conflict_do_update(index_elements=["day"], set_={k: v for k, v in vals.items() if k != "day"})
    )


def drawdown(snaps: list[tuple[date, float, float]]) -> tuple[float | None, str]:
    """(drawdown %, how) from (day, value, cumulative net invested) rows in date order."""
    rows = [r for r in snaps if r[1] > 0]
    if len(rows) < 2:
        return None, "needs at least two days of complete valuations (open /portfolio on different days)"
    index, peak = 1.0, 1.0
    for (_, v0, i0), (_, v1, i1) in pairwise(rows):
        index *= (v1 - (i1 - i0)) / v0
        peak = max(peak, index)
    return round(
        (index / peak - 1) * 100, 2
    ), f"time-weighted value index over {len(rows)} valuation days ({rows[0][0]} to {rows[-1][0]})"


def alert_metrics(session: Session) -> dict[str, tuple[Decimal | None, str]]:
    from finresearch.fincalc.dates import fiscal_year, today_ist
    from finresearch.fincalc.tax import fy_label
    from finresearch.portfolio.report import disposal_rows, load
    from finresearch.portfolio.tax import fy_summary

    out: dict[str, tuple[Decimal | None, str]] = {}
    data = load(session)
    if not data.holdings:
        reason = "the portfolio is not set up yet: no holdings (import them on /portfolio)"
        return {k: (None, reason) for k in ("allocation_drift_pp", "ltcg_headroom_inr", "drawdown_pct")}
    fy = fiscal_year(today_ist())
    summary = fy_summary(disposal_rows(data), fy, Decimal("0.30"))  # the headroom does not depend on the slab
    out["ltcg_headroom_inr"] = (Decimal(str(summary["exemption"]["remaining"])),
                                f"{fy_label(fy)}: ₹{summary['exemption']['used']:,.0f} of ₹{summary['exemption']['limit']:,.0f} "
                                "used by realised equity LTCG (fincalc.tax)")  # fmt: skip
    snaps = session.scalars(select(PortfolioSnapshot).where(PortfolioSnapshot.complete.is_(True))
                            .order_by(PortfolioSnapshot.day)).all()  # fmt: skip
    if not snaps:
        out["allocation_drift_pp"] = (
            None,
            "no complete valuation yet (open /portfolio once every holding has a price)",
        )
    else:
        last = snaps[-1]
        targets = get_targets(session)
        rows = drift({k: float(v) for k, v in (last.by_asset or {}).items()}, targets)
        if not rows:
            out["allocation_drift_pp"] = (None, "no target allocation set (Portfolio → Allocation → Targets)")
        else:
            top = rows[0]
            out["allocation_drift_pp"] = (Decimal(str(abs(top["drift_pp"]))),
                                          f"{top['label']} {top['weight_pct']:g} % vs target {top['target_pct']:g} % "
                                          f"(valuation of {last.day.isoformat()})")  # fmt: skip
    dd, how = drawdown([(x.day, float(x.value), float(x.invested)) for x in snaps])
    out["drawdown_pct"] = (None if dd is None else Decimal(str(dd)), how)
    return out
