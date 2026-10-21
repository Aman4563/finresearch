"""The live skill monitor (#378): every cell of the forecast ledger evaluated under the registration
(skillmon/specs.py, pinned by prereg.py). Pure over `cells.CellData`; `report` reads the database and writes nothing.

Below the trigger (specs.TRIGGER.full_events resolved independent events, or a real-money cell) a cell gets only its
calibration-in-the-large and the promotion BSS CI, and reads "insufficient data": a cell is never "ok" for want of
outcomes. At the trigger: Spiegelhalter's Z, the Murphy decomposition and the two-sided Bernoulli CUSUM, each with
its registered alarm rule. Nothing here changes a call or promotes a model."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from finresearch.fincalc.dates import to_ist
from finresearch.horserace import scores as sc
from finresearch.skillmon import cells, prereg, specs
from finresearch.skillmon import stats as st


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    h = specs.registration_hash()
    if h != prereg.HASH:
        raise PreregistrationError(
            f"skillmon/specs.py hashes to {h[:12]}… but the registered hash is {prereg.HASH[:12]}…: the monitor is "
            "refused. Record the change in ADDENDUM.md as a new SPEC_VERSION and commit its hash first."
        )
    return h


def cell_key(c: cells.Cell) -> str:
    return f"{c.method}|{c.event_kind}|{c.underlying}"


def label(c: cells.Cell) -> str:
    from finresearch.inventory.registry import entry_for_method

    e = entry_for_method(c.method)
    name = e.name if e else (c.method if len(c.method) <= 48 else c.method[:47] + "…")
    return f"{name} · {c.event_kind}" + ("" if c.underlying == "*" else f" · {c.underlying}")


def _skill(d: cells.CellData, n_independent: int) -> dict[str, Any]:
    if d.benchmark is None:
        return {"status": "no benchmark", "benchmark": None}
    s = specs.SKILL
    pm, pb, o = ([x[i] for x in d.pairs] for i in range(3))
    r = st.bss_ci(pm, pb, o, h=d.h, reps=s.reps, seed=s.seed, level=s.level)
    if n_independent < specs.TRIGGER.promotion_events or r["lo"] is None:
        promo = "insufficient data"
    else:
        promo = "CI above 0" if r["lo"] > 0 else "CI not above 0"
    return {"status": "computed" if r["bss"] is not None else "unknown", "benchmark": d.benchmark, "h": d.h,
            "unpaired": d.unpaired, "mismatched": d.mismatched, "promotion": promo, **r}  # fmt: skip


def evaluate(d: cells.CellData) -> dict[str, Any]:
    c, t, cal = d.cell, specs.TRIGGER, specs.CALIBRATION
    p = [float(f.probability) for f in d.independent]
    o = [int(f.outcome) for f in d.independent]
    n = len(o)
    full = n >= t.full_events or cell_key(c) in t.real_money_cells
    out: dict[str, Any] = {"cell": {"method": c.method, "event_kind": c.event_kind, "underlying": c.underlying},
                           "key": cell_key(c), "label": label(c), "asset": d.asset, "logged": d.logged, "open": d.open,
                           "void": d.void, "independent": n, "trigger": t.full_events,
                           "mode": "full" if full else "on read", "citl": st.citl(p, o), "skill": _skill(d, n),
                           "alarms": []}  # fmt: skip
    if not full:
        out.update(status="insufficient data", why=f"{n} of {t.full_events} resolved independent events")
        return out
    z = st.spiegelhalter(p, o)
    cus = st.two_sided_cusum(p, o, h_up=cal.cusum_h_up, h_down=cal.cusum_h_down, r_up=cal.cusum_r_up,
                             r_down=cal.cusum_r_down)  # fmt: skip
    out.update(spiegelhalter=z, murphy=sc.murphy(p, o, cal.murphy_bins), cusum=cus)
    alarms = out["alarms"]
    if (a := cus["alarm"]) is not None:
        when = to_ist(d.independent[a["at"]].resolved_at).date().isoformat()
        more = "more" if a["side"] == "up" else "less"
        alarms.append({"check": "cusum", "side": a["side"], "at": a["at"], "resolved_on": when,
                       "text": f"outcomes came {more} often than forecast (CUSUM crossed at event {a['at'] + 1}, "
                               f"resolved {when})"})  # fmt: skip
    if z["p"] is not None and z["p"] < cal.spiegelhalter_alarm_p:
        alarms.append({"check": "spiegelhalter", "text": f"miscalibrated (Spiegelhalter Z {z['z']:+.2f}, "
                       f"p {z['p']:.4f})"})  # fmt: skip
    sk = out["skill"]
    if sk.get("hi") is not None and sk["hi"] < 0:
        alarms.append({"check": "skill", "text": f"worse than its benchmark (BSS {sk['bss']:+.3f}, CI upper "
                       f"{sk['hi']:+.3f})"})  # fmt: skip
    unknown = z["z"] is None or (d.benchmark is not None and sk.get("lo") is None)
    out["status"] = "alarm" if alarms else "unknown" if unknown else "ok"
    return out


def evaluate_all(data: list[cells.CellData], now: datetime) -> dict[str, Any]:
    out = [evaluate(d) for d in data]
    counts = {k: sum(1 for x in out if x["status"] == k) for k in specs.STATUSES}
    return {"version": specs.SPEC_VERSION, "registration_hash": prereg.HASH, "generated_at": now.isoformat(),
            "cells": out, "counts": counts, "alarms": sum(len(x["alarms"]) for x in out)}  # fmt: skip


def report(s: Session, now: datetime) -> dict[str, Any]:
    """Every cell, read only (refused while the registration differs from its pinned hash)."""
    require_registered()
    return evaluate_all(cells.load(s), now)
