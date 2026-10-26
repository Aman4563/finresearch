"""What the data-quality pass found, for the brief, GET /api/data-health and the Data health card (#368). Reads the
stored dataset_status and dq_check rows only (the monitor writes them). No check run yet is 'unknown', never ok."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import DatasetStatus, DqCheck
from finresearch.dq.gate import DEPENDENTS, health

LABELS = {
    "fno_bhav": "F&O bhavcopy", "fno_iv_eod": "Derived F&O IV", "fno_participant_oi": "Participant OI",
    "fno_participant_vol": "Participant volume", "fii_stats": "FII derivative statistics", "equity_eod": "Equity closes and delivery (full bhavcopy)",
    "index_eod": "Index closes and India VIX", "span": "SPAN risk parameters", "option_quote": "Forward option quotes",
    "intraday_series": "Intraday index archive", "ipo_history": "IPO history", "ipo_issue": "IPO issue registry",
    "groww_close": "Groww vs official closes", "disk": "Disk space",
    "policy_rate": "RBI policy rates read from RBI's RSS feed",  # #492, dq.policy
    "broker_groww": "Groww reconciliation (positions and holdings vs the app's book; counts only)",
    # FBIL is paused (#476): nothing loads these and nothing reads them (#479), so they stay stale or never loaded
    "rate_gsec_par": "FBIL G-sec par curve (paused)", "rate_tbill": "FBIL T-bill rates (paused)",
    "rate_mibor_on": "FBIL overnight MIBOR (paused)", "rate_mibor_term": "FBIL term MIBOR (paused)",
}  # fmt: skip
RANK = {"fail": 0, "unknown": 1, "warn": 2, "ok": 3}


def _checks(s: Session, row: DatasetStatus) -> list[dict[str, Any]]:
    """The latest run's checks that did not pass, worst first."""
    rows = s.scalars(select(DqCheck).where(DqCheck.dataset == row.dataset, DqCheck.run_at == row.checked_at,
                                           DqCheck.status != "ok")).all()  # fmt: skip
    rows = sorted(rows, key=lambda c: (RANK.get(c.status, 9), not c.critical, c.check))
    return [{"check": c.check, "day": c.day.isoformat(), "status": c.status, "critical": c.critical,
             "value": c.value, "threshold": c.threshold, "note": c.note} for c in rows]  # fmt: skip


def data_health(s: Session) -> dict[str, Any]:
    rows = sorted(s.scalars(select(DatasetStatus)).all(), key=lambda r: (RANK.get(r.status, 9), r.dataset))
    sets = [{"dataset": r.dataset, "label": LABELS.get(r.dataset, r.dataset), "status": r.status,
             "day": r.day.isoformat(), "last_loaded": r.last_loaded.isoformat() if r.last_loaded else None,
             "lag_sessions": r.lag_sessions, "failed": list(r.failed or []),
             "checked_at": r.checked_at.isoformat() if r.checked_at else None, "checks": _checks(s, r)}
            for r in rows]  # fmt: skip
    status = sets[0]["status"] if sets else "unknown"
    deps = [{"page": page, "datasets": list(ds), **{k: v for k, v in health(s, ds).items() if k != "datasets"}}
            for page, ds in DEPENDENTS.items()]  # fmt: skip
    return {"status": status, "checked_at": max((d["checked_at"] or "" for d in sets), default=None) or None,
            "datasets": sets, "dependents": deps,
            "method": "Post-load checks over the stored files (freshness against each file's publication time and "
                      "NSE's holiday list, row counts, expected contracts, ranges, cross-source reconciliations, "
                      "option-price screens and control charts) and the Groww reconciliation (counts only), run by "
                      "the monitor at 21:00 and 08:00 IST. Unknown means not checked, never ok."}  # fmt: skip


def brief_lines(r: dict[str, Any]) -> list[dict[str, str]]:
    """Lines for the brief's health list: a red line per failed dataset, an info line per warning."""
    if not r["datasets"]:
        return [{"level": "info", "text": "Data-quality checks have not run yet: F&O and index figures are unverified "
                 "until the monitor's 21:00 pass."}]  # fmt: skip
    out = []
    for d in r["datasets"]:
        first = d["checks"][0] if d["checks"] else None
        why = f" ({first['check']}: {first['note']})" if first and first.get("note") else ""
        if d["status"] == "fail":
            pages = [x["page"] for x in r["dependents"] if d["dataset"] in x["datasets"]]
            tail = f"; {', '.join(pages)} shown as data unverified" if pages else ""
            out.append({"level": "warn", "text": f"{d['label']}: data check failed{why}{tail}."})
        elif d["status"] == "warn":
            out.append({"level": "info", "text": f"{d['label']}: check warning{why}."})
        elif d["status"] == "unknown":
            out.append(
                {"level": "info", "text": f"{d['label']}: not verified{why or ' (could not be checked)'}."}
            )
    return out
