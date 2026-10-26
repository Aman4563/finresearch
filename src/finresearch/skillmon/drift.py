"""Feature drift of the direction model's and the F&O card's inputs (#378, skill-monitor-v2, registered in
skillmon/specs_v2.py): PSI of each input's live sample against its reference window, on the reference's decile bins.

Per input: the reference sample is every known value inside the model's reference window (at least
DRIFT.reference_min), the live sample its last DRIFT.live_values known values after the window. Too few known values on
either side read "insufficient data" with the reason, never ok; PSI >= DRIFT.watch is watch and >= DRIFT.act is act
(an alarm). Pure functions here; nothing reads the database."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date
from typing import Any

from finresearch.skillmon import specs_v2
from finresearch.skillmon import stats as st


def _known(xs: Sequence[float | None]) -> list[float]:
    return [float(x) for x in xs if x is not None and math.isfinite(float(x))]


def status_of(psi: float) -> str:
    d = specs_v2.DRIFT
    return "act" if psi >= d.act else "watch" if psi >= d.watch else "ok"


def input_psi(reference: Sequence[float | None], live: Sequence[float | None]) -> dict[str, Any]:
    """PSI of one input: `reference` = its values inside the reference window, `live` = its values after it, oldest
    first (None = unknown, dropped)."""
    d = specs_v2.DRIFT
    ref, tail = _known(reference), _known(live)[-d.live_values :]
    out: dict[str, Any] = {"psi": None, "status": "insufficient data", "n_ref": len(ref), "n_live": len(tail),
                           "bins": None, "why": None}  # fmt: skip
    if len(ref) < d.reference_min:
        out["why"] = f"{len(ref)} known reference values of the {d.reference_min} needed"
        return out
    if len(tail) < d.live_values:
        out["why"] = f"{len(tail)} known live values of the {d.live_values} needed"
        return out
    edges = st.decile_edges(ref)
    psi = st.psi(st.bin_counts(ref, edges), st.bin_counts(tail, edges), eps=d.eps)
    out.update(
        psi=psi, status=status_of(psi) if psi is not None else "insufficient data", bins=len(edges) + 1
    )
    return out


# --------------------------------------------------------------------------- the registered models on a panel
LABELS = {"direction": "F&O direction logistic", "card": "F&O card"}


def card_series(
    rows: Sequence[Any], feats: Sequence[dict[str, float | None]]
) -> dict[str, list[float | None]]:
    """specs_v2.CARD_INPUTS per session: iv_30, rv_20, rv_60 from the features, the IV percentile of iv_30 over its
    trailing 252 sessions (fincalc.volatility.iv_stats_sessions; None unless it reads ok) and iv_30 / rv_20."""
    from finresearch.fincalc import volatility as vol

    points = [(r.day, r.iv_30) for r in rows]
    pct: list[float | None] = []
    for i in range(len(rows)):
        s = vol.iv_stats_sessions(points[max(0, i + 1 - vol.WINDOW) : i + 1])
        pct.append(s.percentile if s.status == "ok" else None)
    iv, rv20 = [f["iv_30"] for f in feats], [f["rv_20"] for f in feats]
    return {"iv_30": iv, "rv_20": rv20, "rv_60": [f["rv_60"] for f in feats], "iv_pctile": pct,
            "iv_rv_ratio": [a / b if a is not None and b else None for a, b in zip(iv, rv20, strict=True)]}  # fmt: skip


def model_drift(
    model: str, days: Sequence[date], series: dict[str, Sequence[float | None]]
) -> dict[str, Any]:
    """Every input of `model` against its registered reference window (input_psi), with each live sample's span."""
    lo, hi = specs_v2.MODELS[model]["reference"]
    out: dict[str, Any] = {}
    for name in specs_v2.MODELS[model]["inputs"]:
        vals = series[name]
        ref = [v for d, v in zip(days, vals, strict=True) if lo <= d <= hi]
        live = [(d, v) for d, v in zip(days, vals, strict=True) if d > hi and v is not None]
        r = input_psi(ref, [v for _, v in live])
        span = live[-specs_v2.DRIFT.live_values :]
        out[name] = {**r, "live_from": span[0][0].isoformat() if span else None,
                     "live_to": span[-1][0].isoformat() if span else None}  # fmt: skip
    return out


def _ranked(inputs: dict[str, Any], status: str) -> list[dict[str, Any]]:
    """The inputs at `status`, largest PSI first."""
    hits = [{"input": n, "psi": r["psi"]} for n, r in inputs.items() if r["status"] == status]
    return sorted(hits, key=lambda h: -h["psi"])


def _group(model: str, sym: str, inputs: dict[str, Any] | None, why: str | None = None) -> dict[str, Any]:
    lo, hi = specs_v2.MODELS[model]["reference"]
    g: dict[str, Any] = {"model": model, "underlying": sym, "label": f"{LABELS[model]} · {sym}",
                         "reference": [lo.isoformat(), hi.isoformat()], "act": [], "watch": [], "insufficient": [],
                         "inputs": inputs or {}, "why": why}  # fmt: skip
    if inputs is None:
        return {**g, "status": "unknown"}
    g["act"], g["watch"] = _ranked(inputs, "act"), _ranked(inputs, "watch")
    g["insufficient"] = [n for n, r in inputs.items() if r["status"] == "insufficient data"]
    g["live_to"] = max((r["live_to"] for r in inputs.values() if r["live_to"]), default=None)
    g["status"] = ("act" if g["act"] else "watch" if g["watch"] else "insufficient data" if g["insufficient"]
                   else "ok")  # fmt: skip
    return g


def report(panels: dict[str, Sequence[Any]] | None = None) -> dict[str, Any]:
    """Drift of every registered model on every underlying. `panels` (symbol -> DayRows, oldest first) replaces the
    read-only loader (direction.data.load); an underlying whose panel cannot be read is unknown with the error."""
    from finresearch.direction.features import build_features

    groups = []
    for sym in sorted({u for m in specs_v2.MODELS.values() for u in m["underlyings"]}):
        models = [m for m, spec in specs_v2.MODELS.items() if sym in spec["underlyings"]]
        try:
            rows = _load(sym) if panels is None else panels[sym]
        except Exception as e:  # unknown, never ok: the reason goes in the report
            why = (
                f"no panel for {sym}" if isinstance(e, KeyError) else f"{sym}: {type(e).__name__}: {e}"[:300]
            )
            groups += [_group(m, sym, None, why) for m in models]
            continue
        days, feats = [r.day for r in rows], build_features(rows)
        series = {"direction": {k: [f[k] for f in feats] for k in specs_v2.MODELS["direction"]["inputs"]},
                  "card": card_series(rows, feats)}  # fmt: skip
        groups += [_group(m, sym, model_drift(m, days, series[m])) for m in models]
    # inputs by status, and the model-underlying groups whose panel could not be read
    counts = {k: sum(1 for g in groups for r in g["inputs"].values() if r["status"] == k)
              for k in ("act", "watch", "ok", "insufficient data")}  # fmt: skip
    counts["unknown groups"] = sum(g["status"] == "unknown" for g in groups)
    return {"groups": groups, "counts": counts, "alarms": sum(g["status"] == "act" for g in groups)}


def _load(symbol: str) -> list[Any]:
    from finresearch.direction import data

    return data.load(symbol)[0]
