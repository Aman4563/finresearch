"""The portfolio's canonical value history (#239): one series that performance, risk, the drawdown alert, the brief and
digest value change and the net-worth history all read.

The decision. The **reconstructed** history (portfolio.history.build: every transaction replayed through the FIFO
lots and valued at each day's official close or NAV) is canonical for any past day. It is rebuilt whenever the
transactions change, so a backdated import or an edited trade corrects every earlier day, and its prices are the
exchange's published closes, the same for every reader.

`portfolio_snapshot` rows are the **"as shown" audit record**: what GET /api/portfolio or the daily pass displayed at
that moment (live last-traded prices, statement prices, unpriced holdings counted as nothing), with `complete`. They
are never recomputed. They still answer "now" questions where the close does not exist yet: today's headline value,
the current allocation (allocation_drift_pp) and the dashboard strip. Past = reconstruction, now = as shown.

Where the series lives. history.build is async and needs the network; the alert metrics, brief, digest and wealth
pages are synchronous and read only the database. So every build is persisted here, in portfolio.cache under
KEY (no schema change), by its two builders: the performance/risk API (api.portfolio_analytics) and the monitor's
daily pass after the close (monitor.portfolio_daily). A stored series whose transaction fingerprint no longer matches
the database is out of date and is not used (None with the reason): there is no silent fallback to snapshots.

Reconciliation (`reconcile`) compares each snapshot with the series on the same day and reports every day where they
differ by more than the tolerance, with the most likely reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from sqlalchemy.orm import Session

from finresearch.portfolio import cache

KEY = "cache:history"
# Tolerance for the snapshot-vs-reconstruction check: a snapshot taken during the session uses the last traded price
# and the reconstruction the official close, which for a diversified portfolio differ by well under 1 % on most days;
# the ₹ floor keeps a tiny portfolio's rounding from being reported.
TOLERANCE_PCT = 1.0
TOLERANCE_INR = 100.0
CLOSE_IST = time(15, 30)  # NSE's session ends; a snapshot written before it used intraday prices
NOT_BUILT = ("no reconstructed value history yet: it is built by the daily portfolio pass after the close "
             "(`finresearch serve`) or when the Performance tab is opened")  # fmt: skip


@dataclass
class Series:
    days: list[date]
    value: list[float]
    invested: list[float]
    complete: list[bool]
    fingerprint: str = ""
    built_on: str | None = None
    start_reason: str = ""
    excluded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def rows(
        self, since: date | None = None, *, complete_only: bool = True
    ) -> list[tuple[date, float, float]]:
        """(day, value, cumulative net invested) rows, the shape metrics.drawdown and digest.value_change take."""
        return [(d, v, i) for d, v, i, c in zip(self.days, self.value, self.invested, self.complete, strict=True)
                if (since is None or d >= since) and (c or not complete_only)]  # fmt: skip

    def value_on(self, day: date) -> tuple[float, bool, date] | None:
        """(value, complete, series day) on the last series day on or before `day`; None before the series."""
        got = None
        for i, d in enumerate(self.days):
            if d > day:
                break
            got = (self.value[i], self.complete[i], d)
        return got


def to_json(hist: Any, fingerprint: str, today: date) -> dict[str, Any]:
    return {"days": [d.isoformat() for d in hist.days], "value": [round(v, 2) for v in hist.value],
            "invested": [round(v, 2) for v in hist.invested], "complete": list(hist.complete),
            "fingerprint": fingerprint, "built_on": today.isoformat(), "start_reason": hist.start_reason,
            "excluded": list(hist.excluded), "warnings": list(hist.warnings)}  # fmt: skip


def from_history(hist: Any, fingerprint: str, today: date) -> Series:
    return _parse(to_json(hist, fingerprint, today))


def save(s: Session, hist: Any, fingerprint: str, today: date) -> bool:
    """Persist a built history (portfolio.history.History) as the canonical series. A build without a series (no
    transactions, no prices) is not stored: it says nothing about past values. Returns whether it was stored."""
    if not getattr(hist, "ok", False):
        return False
    cache.write(s, KEY, to_json(hist, fingerprint, today))
    return True


def _parse(raw: dict[str, Any]) -> Series:
    return Series([date.fromisoformat(d) for d in raw["days"]], [float(x) for x in raw["value"]],
                  [float(x) for x in raw["invested"]], [bool(x) for x in raw["complete"]],
                  raw.get("fingerprint", ""), raw.get("built_on"), raw.get("start_reason", ""),
                  list(raw.get("excluded") or []), list(raw.get("warnings") or []))  # fmt: skip


def current_fingerprint(s: Session) -> str:
    from finresearch.portfolio.history import fingerprint, holdings_from
    from finresearch.portfolio.report import load

    return fingerprint(holdings_from(load(s)))


def load(s: Session, fingerprint: str | None = None) -> tuple[Series | None, str | None]:
    """(the canonical series, None) or (None, why it cannot be used). `fingerprint` is the current transactions'
    (computed when not given)."""
    raw = cache.read(s, KEY)
    if not raw.get("days"):
        return None, NOT_BUILT
    ser = _parse(raw)
    fp = fingerprint if fingerprint is not None else current_fingerprint(s)
    if ser.fingerprint != fp:
        return None, (f"the value history built on {ser.built_on} is out of date (transactions changed since); it is "
                      "rebuilt by the next daily pass or when the Performance tab is opened")  # fmt: skip
    return ser, None


@dataclass
class Snap:
    """What reconcile needs of a portfolio_snapshot row."""

    day: date
    value: float
    complete: bool
    updated_at: datetime | None = None  # when it was written (aware)


def _reason(sn: Snap, ser: Series, i: int, added: list[tuple[date, datetime]]) -> str:
    from finresearch.fincalc.dates import to_ist

    if not sn.complete:
        return ("the snapshot was incomplete (a holding without a current price or cost, or valued at an old "
                "statement price)")  # fmt: skip
    if not ser.complete[i]:
        return "the reconstruction used a price more than 10 days old on this day"
    if ser.excluded:
        return "the reconstruction leaves out holdings without a price history: " + "; ".join(ser.excluded)
    if sn.updated_at is not None:
        late = [d for d, at in added if d <= sn.day and at > sn.updated_at]
        if late:
            return (f"{len(late)} transaction(s) dated on or before this day were added after the snapshot was taken "
                    "(a backdated import or edit): the reconstruction includes them")  # fmt: skip
    if ser.days[i] != sn.day:
        return (
            f"no prices on {sn.day.isoformat()} (not a trading day): compared with the close of {ser.days[i]}"
        )
    if sn.updated_at is not None:
        ist = to_ist(sn.updated_at)
        if ist.date() == sn.day and ist.time() < CLOSE_IST:
            return "the snapshot was taken during the session (last traded prices); the reconstruction uses the close"
    return ("the price basis differs: the snapshot used the price shown at the time (last trade, previous close or a "
            "statement price), the reconstruction the official close or NAV")  # fmt: skip


def reconcile(ser: Series, snaps: list[Snap], added: list[tuple[date, datetime]], today: date,
              tol_pct: float = TOLERANCE_PCT, tol_inr: float = TOLERANCE_INR) -> dict[str, Any]:  # fmt: skip
    """Every snapshot day where the "as shown" value and the reconstructed value differ by more than both
    `tol_pct` % and ₹`tol_inr`, with the most likely reason. `added`: (transaction day, created_at) of every
    transaction. Snapshots outside the series, and today's before the series has today's close, are not compared."""
    checked, differ = 0, []
    first, last = ser.days[0], ser.days[-1]
    for sn in sorted(snaps, key=lambda x: x.day):
        if sn.day < first or sn.day > last or (sn.day == today and last != today):
            continue
        i = max(k for k, d in enumerate(ser.days) if d <= sn.day)
        rec = ser.value[i]
        checked += 1
        diff = sn.value - rec
        pct = diff / rec * 100 if rec else None
        if abs(diff) <= tol_inr or (pct is not None and abs(pct) <= tol_pct):
            continue
        differ.append({"day": sn.day.isoformat(), "snapshot": round(sn.value, 2), "reconstructed": round(rec, 2),
                       "series_day": ser.days[i].isoformat(), "diff": round(diff, 2),
                       "diff_pct": None if pct is None else round(pct, 2), "reason": _reason(sn, ser, i, added)})  # fmt: skip
    return {"checked": checked, "differ": differ, "ok": not differ, "tolerance_pct": tol_pct,
            "tolerance_inr": tol_inr, "canonical": "reconstructed history (transactions × official closes/NAVs)",
            "note": "Snapshots are the value as shown at the time; past values are read from the reconstruction."}  # fmt: skip


def reconcile_db(s: Session, ser: Series, today: date) -> dict[str, Any]:
    from sqlalchemy import select

    from finresearch.db.models import PortfolioSnapshot, PortfolioTxn

    snaps = [Snap(x.day, float(x.value), bool(x.complete), x.updated_at)
             for x in s.scalars(select(PortfolioSnapshot).order_by(PortfolioSnapshot.day))]  # fmt: skip
    added = [
        (d, at) for d, at in s.execute(select(PortfolioTxn.day, PortfolioTxn.created_at)) if at is not None
    ]
    return reconcile(ser, snaps, added, today)
