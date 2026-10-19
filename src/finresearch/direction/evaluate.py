"""Walk-forward evaluation of the pre-registered direction models (#327, direction.specs) and the acceptance verdict.

walk_forward(): for one underlying, horizon and event, every session from TRAIN_START gets three forecasts made with
what was known at its close. The logistic model is refitted on the first session of each month on the rows whose
outcome was known by then (i + h ≤ the refit session; checked, LookAheadError otherwise); the base rate uses the last
`window` outcomes known by the day itself; the implied model needs only the day's IV.
evaluate(): the in-sample and out-of-sample windows on the days where all three forecasts and the outcome exist, the
metrics, Diebold-Mariano tests, Holm adjustment across the 24 logistic cells, regimes, and each cell's status:
`accepted` when every criterion holds, else `no_edge` with the failed criteria in words.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any

from finresearch.direction import metrics, models, prereg, specs
from finresearch.direction.features import DayRow, LookAheadError, build_features, target


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    h = specs.registration_hash()
    if h != prereg.HASH:
        raise PreregistrationError(
            f"direction/specs.py hashes to {h[:12]}… but the registered hash is {prereg.HASH[:12]}…: out-of-sample "
            "evaluation refused. Register a changed spec as a new variant (specs.py docstring) first."
        )
    return h


@dataclass
class Pred:
    day: date
    i: int
    y: int | None
    p: dict[str, float | None]


@dataclass
class WalkForward:
    preds: list[Pred]
    refits: list[dict[str, Any]] = field(default_factory=list)
    last_fit: models.LogitFit | None = None


def labels(rows: Sequence[DayRow], h: int, event: str) -> list[int | None]:
    return [target(rows, i, h, event) for i in range(len(rows))]


def refit_points(rows: Sequence[DayRow], start: date = specs.TRAIN_START) -> list[int]:
    """The first stored session of each calendar month on or after `start`."""
    out = []
    for i, r in enumerate(rows):
        if r.day < start:
            continue
        if not out or (r.day.year, r.day.month) != (rows[out[-1]].day.year, rows[out[-1]].day.month):
            out.append(i)
    return out


def train_set(y: Sequence[int | None], rows: Sequence[DayRow], h: int, at: int) -> list[int]:
    """Rows whose outcome was known at the close of session `at` (i + h ≤ at), from TRAIN_START."""
    idx = [
        i for i in range(len(rows)) if i + h <= at and y[i] is not None and rows[i].day >= specs.TRAIN_START
    ]
    if idx and max(idx) + h > at:
        raise LookAheadError(f"training row {rows[max(idx)].day} has an outcome after {rows[at].day}")
    return idx


def walk_forward(rows: Sequence[DayRow], feats: Sequence[dict[str, float | None]], h: int, event: str,
                 y: Sequence[int | None] | None = None) -> WalkForward:  # fmt: skip
    y = labels(rows, h, event) if y is None else y
    points = refit_points(rows)
    out = WalkForward([])
    # base rate: the outcomes known at each day, in the order they became known (i + h ≤ day)
    known: list[int] = []
    nxt = 0
    for bi, r0 in enumerate(points):
        r1 = points[bi + 1] if bi + 1 < len(points) else len(rows)
        tr = train_set(y, rows, h, r0)
        fit = None
        if len(tr) >= specs.LOGIT.min_train and len({y[i] for i in tr}) == 2:
            fit = models.fit_logit([feats[i] for i in tr], [y[i] for i in tr], h)  # type: ignore[misc]
            out.last_fit = fit
        out.refits.append({"day": rows[r0].day.isoformat(), "train_n": len(tr), "fitted": fit is not None,
                           "converged": None if fit is None else fit.converged})  # fmt: skip
        for i in range(r0, r1):
            while nxt + h <= i:
                if y[nxt] is not None:
                    known.append(y[nxt])  # type: ignore[arg-type]
                nxt += 1
            p = {"base_rate": models.base_rate_p(known), "implied": models.implied_p(rows[i].iv_30, h, event),
                 "logistic": None if fit is None else fit.predict(feats[i])}  # fmt: skip
            out.preds.append(Pred(rows[i].day, i, y[i], p))
    return out


MODEL_KEYS = ("base_rate", "implied", "logistic")
COMPARISONS = (("logistic", "implied"), ("logistic", "base_rate"), ("base_rate", "implied"))


def _r(x: float | None, d: int = 5) -> float | None:
    return None if x is None else round(float(x), d)


def _regime_iv(f: dict[str, float | None]) -> str:
    v = f.get("iv_rank")
    return "unknown" if v is None else "low IV" if v < 1 / 3 else "mid IV" if v < 2 / 3 else "high IV"


def _regime_trend(f: dict[str, float | None]) -> str:
    a, b = f.get("dist_ma50"), f.get("dist_ma200")
    if a is None or b is None:
        return "unknown"
    return "uptrend" if a > 0 and b > 0 else "downtrend" if a < 0 and b < 0 else "mixed"


def window_stats(sample: list[Pred], h: int, bins: int) -> dict[str, Any]:
    o = [int(x.y) for x in sample]  # type: ignore[arg-type]
    out: dict[str, Any] = {"n": len(sample), "from": sample[0].day.isoformat() if sample else None,
                           "to": sample[-1].day.isoformat() if sample else None, "models": {}, "tests": {}}  # fmt: skip
    if not sample:
        return out
    loss = {}
    for k in MODEL_KEYS:
        p = [float(x.p[k]) for x in sample]  # type: ignore[arg-type]
        s = metrics.summary(p, o, bins)
        out["models"][k] = {kk: (_r(v) if isinstance(v, float) else v) for kk, v in s.items()}
        loss[k] = (metrics.brier_each(p, o), metrics.logloss_each(p, o))
    for a, b in COMPARISONS:
        ba, bb = out["models"][a]["brier"], out["models"][b]["brier"]
        la, lb = out["models"][a]["log_loss"], out["models"][b]["log_loss"]
        dm = metrics.diebold_mariano(loss[b][0], loss[a][0], h, specs.dm_lag(h))
        out["tests"][f"{a}_vs_{b}"] = {"brier_skill": _r(metrics.skill(ba, bb)),
                                       "log_loss_skill": _r(metrics.skill(la, lb)),
                                       "dm": _r(dm["dm"], 4), "p": _r(dm["p"], 6), "lag": dm["lag"]}  # fmt: skip
    return out


def regimes(sample: list[Pred], feats: Sequence[dict[str, float | None]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for name, fn in (("iv", _regime_iv), ("trend", _regime_trend)):
        groups: dict[str, list[Pred]] = {}
        for x in sample:
            groups.setdefault(fn(feats[x.i]), []).append(x)
        rows = []
        for g, xs in sorted(groups.items()):
            o = [int(x.y) for x in xs]  # type: ignore[arg-type]
            b = {k: metrics.mean(metrics.brier_each([float(x.p[k]) for x in xs], o)) for k in MODEL_KEYS}  # type: ignore[arg-type]
            rows.append({"regime": g, "n": len(xs), "base_rate": _r(metrics.mean([float(v) for v in o])),
                         "hit_rate_logistic": _r(metrics.hit_rate([float(x.p["logistic"]) for x in xs], o)),  # type: ignore[arg-type]
                         "bss_logistic_vs_implied": _r(metrics.skill(b["logistic"], b["implied"])),
                         "bss_logistic_vs_base_rate": _r(metrics.skill(b["logistic"], b["base_rate"]))})  # fmt: skip
        out[name] = rows
    return out


def evaluate_cell(rows: Sequence[DayRow], feats: Sequence[dict[str, float | None]], h: int, event: str,
                  oos: bool) -> dict[str, Any]:  # fmt: skip
    wf = walk_forward(rows, feats, h, event)
    common = [x for x in wf.preds if x.y is not None and all(x.p[k] is not None for k in MODEL_KEYS)]
    is_s = [x for x in common if specs.TRAIN_START <= x.day <= specs.IS_END]
    oos_s = [x for x in common if x.day >= specs.OOS_START] if oos else []
    bins = specs.CRITERIA.reliability_bins
    cell: dict[str, Any] = {"horizon": h, "event": event, "event_text": specs.EVENT_TEXT[event],
                            "is": window_stats(is_s, h, bins),
                            "oos": window_stats(oos_s, h, bins) if oos else None,
                            "regimes_oos": regimes(oos_s, feats) if oos else None,
                            "refits": len(wf.refits), "refits_fitted": sum(r["fitted"] for r in wf.refits),
                            "last_fit": wf.last_fit.to_json() if wf.last_fit else None,
                            "predictions_oos": [[x.day.isoformat(), _r(x.p["base_rate"], 4), _r(x.p["implied"], 4),
                                                 _r(x.p["logistic"], 4), x.y] for x in oos_s]}  # fmt: skip
    return cell


def judge(cells: list[dict[str, Any]], crit: specs.Criteria = specs.CRITERIA) -> None:
    """Holm across the logistic cells per comparison, then each cell's status and reasons (in place)."""
    keyed = {f"{c['symbol']}:{c['horizon']}:{c['event']}": c for c in cells}
    for ref in crit.must_beat:
        name = f"logistic_vs_{ref}"
        adj = metrics.holm(
            {k: ((c["oos"] or {}).get("tests") or {}).get(name, {}).get("p") for k, c in keyed.items()}
        )
        for k, c in keyed.items():
            if c["oos"] and name in c["oos"]["tests"]:
                c["oos"]["tests"][name]["p_holm"] = _r(adj.get(k), 6)
    for c in cells:
        o = c["oos"]
        checks = []

        def chk(name: str, value: Any, ok: bool, rule: str, checks: list = checks) -> None:
            checks.append({"criterion": name, "value": value, "pass": bool(ok), "rule": rule})

        n = o["n"] if o else 0
        chk("OOS days", n, n >= crit.min_oos_n, f">= {crit.min_oos_n}")
        for ref in crit.must_beat:
            t = (o or {}).get("tests", {}).get(f"logistic_vs_{ref}", {})
            bss, ph = t.get("brier_skill"), t.get("p_holm")
            chk(f"Brier skill vs {ref.replace('_', ' ')}", bss, bss is not None and bss > 0, "> 0")
            chk(
                f"DM p vs {ref.replace('_', ' ')} (Holm, one-sided)",
                ph,
                ph is not None and ph < crit.alpha,
                f"< {crit.alpha:g}",
            )
        slope = ((o or {}).get("models", {}).get("logistic") or {}).get("calibration_slope")
        chk(
            "Calibration slope",
            slope,
            slope is not None and crit.slope_low <= slope <= crit.slope_high,
            f"{crit.slope_low:g} to {crit.slope_high:g}",
        )
        c["criteria"] = checks
        failed = [x["criterion"] for x in checks if not x["pass"]]
        c["status"] = "accepted" if not failed else "no_edge"
        c["reasons"] = ["passes every pre-registered criterion out of sample"] if not failed else \
            [f"fails: {x}" for x in failed]  # fmt: skip


def evaluate(panels: dict[str, Sequence[DayRow]], *, source: str, oos: bool = True,
             data_notes: dict[str, Any] | None = None, check_every: int = 25) -> dict[str, Any]:  # fmt: skip
    """The full report. `oos=False` never reads an outcome after IS_END: the panels are cut there first."""
    reg = require_registered() if oos else specs.registration_hash()
    cells: list[dict[str, Any]] = []
    unders: dict[str, Any] = {}
    for sym, rows in panels.items():
        rows = list(rows) if oos else [r for r in rows if r.day <= specs.IS_END]
        feats = build_features(rows, check_every=check_every)
        last = rows[-1].day if rows else None
        unders[sym] = {"sessions": len(rows), "first": rows[0].day.isoformat() if rows else None,
                       "last": last.isoformat() if last else None,
                       "return_sources": _count(r.ret_source for r in rows),
                       "feature_coverage": {k: round(sum(f[k] is not None for f in feats) / max(len(feats), 1), 3)
                                            for k in specs.FEATURES},
                       **((data_notes or {}).get(sym) or {})}  # fmt: skip
        for h in specs.HORIZONS:
            for ev in specs.EVENTS:
                c = evaluate_cell(rows, feats, h, ev, oos)
                c["symbol"] = sym
                cells.append(c)
    if oos:
        judge(cells)
    accepted = [
        f"{c['symbol']} h={c['horizon']} {c['event']}" for c in cells if c.get("status") == "accepted"
    ]
    psym, ph, pev = specs.PRIMARY
    primary = next((c for c in cells if (c["symbol"], c["horizon"], c["event"]) == (psym, ph, pev)), None)
    return {
        "version": specs.SPEC_VERSION, "registration_hash": reg, "registered_hash": prereg.HASH,
        "registered_on": prereg.REGISTERED_ON, "oos_evaluated": oos, "source": source,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "windows": {"train_start": specs.TRAIN_START.isoformat(), "is_end": specs.IS_END.isoformat(),
                    "oos_start": specs.OOS_START.isoformat()},
        "criteria": asdict(specs.CRITERIA), "k": specs.K, "horizons": list(specs.HORIZONS), "events": list(specs.EVENTS),
        "underlyings": unders, "cells": cells, "accepted": accepted,
        "primary": None if primary is None else {"symbol": psym, "horizon": ph, "event": pev,
                                                 "status": primary.get("status")},
        "verdict": ("accepted: " + ", ".join(accepted)) if accepted else
                   ("no edge: no logistic cell passed the pre-registered out-of-sample criteria" if oos else
                    "in-sample only"),
    }  # fmt: skip


def _count(xs) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in xs:
        out[str(x)] = out.get(str(x), 0) + 1
    return out
