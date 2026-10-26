"""The Kupiec risk-model trigger (#378, skill-monitor-v2, registered in skillmon/specs_v2.py `RiskTrigger`): a VaR's
logged exceptions tested per calendar quarter with Kupiec's proportion-of-failures test (horserace.scores.kupiec), and
the mark "not for limits" once `consecutive` quarters in a row fail (p < alpha). The mark stays.

A quarter is tested only after it ended (IST) with none of its rows still open, and only with at least `min_days`
resolved days; otherwise it is pending or has insufficient data, and neither is ever a pass. Without a failing run the
trigger reads "insufficient data" below `consecutive` tested quarters, and "unknown" when a run of quarters with no
pass includes an untested or pending one (it could have been a failing run). Pure functions over `Obs` rows."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any, NamedTuple

from finresearch.horserace import scores as sc
from finresearch.portfolio import var_ledger
from finresearch.skillmon import specs_v2

NOMINAL = round(1 - var_ledger.LEVEL, 6)  # the VaR's exception rate, as the ledger logs it (P = 0.05)


class Obs(NamedTuple):
    day: date  # the forecast's own day (inputs.as_of, IST)
    status: str  # open | resolved | void
    outcome: int | None  # 1 = an exception
    id: int


def quarter_of(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def _label(q: tuple[int, int]) -> str:
    return f"{q[0]}-Q{q[1]}"


def quarters(rows: Sequence[Obs], today: date, rate: float = NOMINAL) -> list[dict[str, Any]]:
    """Every quarter from the first logged row's to the last that ended before `today`, oldest first, with its
    resolved days, exceptions, open rows, Kupiec LR and p, and status (fail, pass, insufficient data or pending)."""
    r = specs_v2.RISK
    by_day: dict[date, Obs] = {}
    for o in sorted(rows, key=lambda o: o.id):
        by_day[o.day] = o  # one outcome per day: the newest row
    out: list[dict[str, Any]] = []
    q, current = (quarter_of(min(by_day)) if by_day else None), quarter_of(today)
    while q is not None and q < current:
        days = [o for d, o in by_day.items() if quarter_of(d) == q]
        res = [o for o in days if o.status == "resolved" and o.outcome is not None]
        n, x = len(res), sum(int(o.outcome) for o in res)  # type: ignore[arg-type]
        row: dict[str, Any] = {"quarter": _label(q), "days": n, "exceptions": x,
                               "open": sum(o.status == "open" for o in days), "lr": None, "p": None}  # fmt: skip
        if row["open"]:
            row["status"] = "pending"
        elif n < r.min_days:
            row["status"] = "insufficient data"
        else:
            k = sc.kupiec(x, n, rate)
            row.update(lr=k["lr"], p=k["p"], status="fail" if k["p"] < r.alpha else "pass")
        out.append(row)
        q = (q[0] + 1, 1) if q[1] == 4 else (q[0], q[1] + 1)
    return out


def evaluate(rows: Sequence[Obs], today: date, rate: float = NOMINAL) -> dict[str, Any]:
    """The trigger over the whole record (module docstring): status, the run that decided it, and every quarter."""
    r = specs_v2.RISK
    c, qs = r.consecutive, quarters(rows, today, rate)
    tested = sum(q["status"] in ("fail", "pass") for q in qs)
    out: dict[str, Any] = {"quarters": qs, "tested": tested, "rate": rate, "run": None}
    runs = [qs[i : i + c] for i in range(len(qs) - c + 1)]
    if failing := next((w for w in runs if all(q["status"] == "fail" for q in w)), None):
        names = [q["quarter"] for q in failing]
        return {**out, "status": "not for limits", "run": names,
                "why": f"Kupiec p < {r.alpha} in {c} consecutive quarters ({names[0]} to {names[-1]}): the VaR's "
                       "exceptions did not come at its stated rate"}  # fmt: skip
    if tested < c:
        return {**out, "status": "insufficient data",
                "why": f"{tested} of the {c} tested quarters needed (a quarter is tested once it has ended with no "
                       f"open row and at least {r.min_days} resolved days)"}  # fmt: skip
    if unchecked := next((w for w in runs if all(q["status"] != "pass" for q in w)), None):
        names = [q["quarter"] for q in unchecked]
        return {**out, "status": "unknown", "run": names,
                "why": f"{names[0]} to {names[-1]} hold no passing quarter and include an untested or pending one, "
                       "so they could be a failing run"}  # fmt: skip
    return {**out, "status": "not triggered",
            "why": f"{tested} quarters tested; every {c} in a row hold a passing one"}  # fmt: skip


# --------------------------------------------------------------------------- the VaR ledger (read only)
def ledger_obs(s: Any) -> tuple[list[Obs], int]:
    """The forecast ledger's VaR rows (portfolio.var_ledger) as Obs by their own day, and how many have no readable
    day (counted, never silently dropped)."""
    from sqlalchemy import select

    from finresearch.db.models import Forecast

    q = select(Forecast).where(
        Forecast.asset == var_ledger.ASSET, Forecast.event_kind == var_ledger.EVENT_KIND
    )
    out, bad = [], 0
    for f in s.scalars(q):
        try:
            day = date.fromisoformat(str((f.inputs or {})["as_of"])[:10])
        except (KeyError, ValueError):
            bad += 1
            continue
        out.append(Obs(day, f.status, f.outcome, f.id))
    return out, bad


def marks(s: Any, today: date) -> dict[str, dict[str, Any]]:
    """The trigger for every registered risk model (specs_v2.RISK_MODELS), each scored on the VaR ledger's logged
    record. A row with no readable day could be an exception, so it never lets the trigger read "not triggered"."""
    obs, bad = ledger_obs(s)
    r = evaluate(obs, today)
    if bad:
        r["unreadable"] = bad
        if r["status"] == "not triggered":
            r.update(status="unknown", why=f"{bad} VaR-ledger row(s) have no readable day, so a quarter's count of "
                                            "exceptions is unknown")  # fmt: skip
    return {key: {**r, "model": key, "evidence": why} for key, why in specs_v2.RISK_MODELS.items()}
