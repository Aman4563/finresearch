"""E-IPO-1: post-hoc calibration of the IPO listing model, walk-forward (pre-registered in
evals/experiments/ipo_calibration/PREREG.md; portfolio-insights research §2.1).

The base model and the base-rate table are exactly `evals.ipo_model`'s. For every listing year t with ≥ 20 training
rows, fit on years < t and forecast year t: these out-of-fold (OOF) forecasts are the only data a calibrator may be
fitted on. For gate year t (2019–2025) each calibrator is fitted on OOF forecasts from years < t (≥ 30 of them, else
the uncalibrated model is used) and applied to year t. Variants: T (temperature), P (Platt, smoothed targets) and
S (λ-blend of model and table), from `evals.calibration_policy`.

Pass bar per variant: Brier skill vs the table > 0 in ≥ 5 of the 7 years AND pooled 2019–2025 Brier below the
table's. A passing variant must pass again with the two features the live signal cannot supply yet (OFS share,
Nifty 20-session return) blanked at prediction time, before `signals/ipo.py` may use it.

    uv run python -m finresearch.evals.ipo_calibration --data ../finresearch/data/ipo_history.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from finresearch.config import REPO_ROOT
from finresearch.evals import calibration_policy as cp
from finresearch.evals import ipo_model as im
from finresearch.fincalc import ipo_model as m

OUT_DIR = REPO_ROOT / "evals" / "experiments" / "ipo_calibration"
GATE_YEARS = im.GATE_YEARS
REPORT_YEARS = (*GATE_YEARS, 2026)
MIN_TRAIN_OOF = 20
MIN_POOL = 30
VARIANTS = ("T", "S", "P")  # also the pre-registered shipping priority
SIGNAL_ARTEFACT = Path(__file__).parent / "artefacts" / "ipo_calibrated.json"
# True since the IPO signal computes the OFS share and the Nifty 20-session return live (ADDENDUM.md)
LIVE_FEATURES_FILLED = True
VARIANT_NAMES = {"T": "temperature scaling", "S": "shrinkage blend to the table", "P": "Platt scaling",
                 "raw": "uncalibrated model", "table": "base-rate table"}  # fmt: skip


@dataclass
class Oof:
    year: int
    p_model: float
    p_table: float
    y: int


def _blank_live_gaps(feat: dict[str, Any]) -> dict[str, Any]:
    """Features as the live signal builds them today: OFS share unknown, Nifty 20-session return unknown."""
    return {**feat, "ofs_share": None, "nifty20": None}


def oof_forecasts(rows: list[Any], *, live_parity: bool = False, min_train: int = MIN_TRAIN_OOF) -> list[Oof]:
    data = im.usable(rows)
    years = sorted({im._year(r) for r in data})
    out: list[Oof] = []
    for t in years:
        train = [r for r in data if im._year(r) < t]
        test = [r for r in data if im._year(r) == t]
        if len(train) < min_train or not test:
            continue
        model = im.fit(train)
        feats = [im.raw_features(r) for r in test]
        if live_parity:
            feats = [_blank_live_gaps(f) for f in feats]
        p = im.predict(model, feats)["p"]
        tab = im.table_forecaster(train)
        for r, pm in zip(test, p, strict=True):
            out.append(Oof(t, float(pm), float(tab(r)[0]), int(im._f(r, "return_open") > 0)))
    return out


def _fit_variant(
    v: str, pool: list[Oof]
) -> tuple[Callable[[np.ndarray, np.ndarray], np.ndarray], dict[str, float]]:
    pm = np.asarray([o.p_model for o in pool])
    pt = np.asarray([o.p_table for o in pool])
    y = np.asarray([o.y for o in pool], dtype=float)
    if v == "T":
        t = cp.fit_temperature(pm, y)
        return (lambda a, b: cp.apply_temperature(a, t)), {"T": t}
    if v == "P":
        a_, b_ = cp.fit_platt(pm, y)
        return (lambda a, b: cp.apply_platt(a, a_, b_)), {"a": a_, "b": b_}
    if v == "S":
        lam = cp.fit_blend(pm, pt, y)
        return (lambda a, b: lam * a + (1 - lam) * b), {"lambda": lam}
    raise ValueError(v)


def _metrics(p: np.ndarray, y: np.ndarray, bs_table: float) -> dict[str, Any]:
    bs = m.brier(p, y)
    return {"brier": bs, "bss_vs_table": m.brier_skill(bs, bs_table), "auc": m.auc(p, y),
            "log_loss": cp.log_loss(p, y)}  # fmt: skip


def evaluate(oof: list[Oof], *, years: tuple[int, ...] = REPORT_YEARS, gate_years: tuple[int, ...] = GATE_YEARS,
             min_pool: int = MIN_POOL) -> dict[str, Any]:  # fmt: skip
    folds: list[dict[str, Any]] = []
    pooled: dict[str, list[float]] = {k: [] for k in ("y", "table", "raw", *VARIANTS)}
    for t in years:
        test = [o for o in oof if o.year == t]
        if not test:
            continue
        pool = [o for o in oof if o.year < t]
        pm = np.asarray([o.p_model for o in test])
        pt = np.asarray([o.p_table for o in test])
        y = np.asarray([o.y for o in test], dtype=float)
        bs_t = m.brier(pt, y)
        fold: dict[str, Any] = {"year": t, "n_test": len(test), "n_pool": len(pool), "prevalence": float(y.mean()),
                                "brier_table": bs_t, "brier_climatology_in_sample": float(y.mean() * (1 - y.mean())),
                                "raw": _metrics(pm, y, bs_t)}  # fmt: skip
        preds = {"raw": pm, "table": pt}
        for v in VARIANTS:
            if len(pool) >= min_pool:
                fn, params = _fit_variant(v, pool)
                p = np.clip(fn(pm, pt), 0.0, 1.0)
            else:
                p, params = pm, {"fallback": "pool < 30: uncalibrated"}
            preds[v] = p
            fold[v] = {**_metrics(p, y, bs_t), "params": params}
        folds.append(fold)
        if t in gate_years:
            pooled["y"] += y.tolist()
            for k, p in preds.items():
                pooled[k] += np.asarray(p).tolist()
    y = np.asarray(pooled["y"])
    bs_table = m.brier(pooled["table"], y) if len(y) else None
    summary: dict[str, Any] = {"n": len(y), "brier_table": bs_table, "auc_table": m.auc(pooled["table"], y),
                               "log_loss_table": cp.log_loss(pooled["table"], y),
                               "reliability_table": m.reliability(pooled["table"], y)}  # fmt: skip
    verdicts: dict[str, Any] = {}
    for v in ("raw", *VARIANTS):
        p = np.asarray(pooled[v])
        summary[v] = {**_metrics(p, y, bs_table), "reliability": m.reliability(p, y)}
        gated = [f for f in folds if f["year"] in gate_years]
        passed = [f["year"] for f in gated if (f[v]["bss_vs_table"] or 0) > 0]
        verdicts[v] = {"years_passed": passed, "years_evaluated": [f["year"] for f in gated],
                       "pooled_brier_better": summary[v]["brier"] < bs_table,
                       "passes": len(passed) >= im.GATE_MIN_PASS and summary[v]["brier"] < bs_table}  # fmt: skip
    return {"folds": folds, "pooled_2019_2025": summary, "verdicts": verdicts}


def deployed_calibrator(oof: list[Oof], variant: str) -> dict[str, Any]:
    """The calibrator the signal would use: fitted on every resolved OOF forecast."""
    _, params = _fit_variant(variant, oof)
    return {"variant": variant, "name": VARIANT_NAMES[variant], "params": params, "n_fit": len(oof),
            "years": [min(o.year for o in oof), max(o.year for o in oof)]}  # fmt: skip


def run(rows: list[Any], *, live_features_filled: bool | None = None) -> dict[str, Any]:
    """The pre-registered run. `live_features_filled` (default: LIVE_FEATURES_FILLED) says whether the live signal
    now supplies the two features the parity check blanks; then that check is vacuous and is reported only
    (ADDENDUM.md, the PREREG's named follow-up)."""
    filled = LIVE_FEATURES_FILLED if live_features_filled is None else live_features_filled
    oof = oof_forecasts(rows)
    main = evaluate(oof)
    out: dict[str, Any] = {"experiment": "E-IPO-1 post-hoc calibration", "prereg": "PREREG.md",
                           "oof_years": sorted({o.year for o in oof}), "n_oof": len(oof), **main,
                           "live_features_filled": filled}  # fmt: skip
    # the model's subscription features are the FINAL book, published after the 17:00 UPI cut-off (#244): whatever
    # passes, it is labelled and may only run as a shadow test (scored live on the book seen before the cut-off)
    out["decision_point"] = im.decision_point(im.usable(rows))
    passing = [v for v in VARIANTS if main["verdicts"][v]["passes"]]
    out["passing_variants"] = passing
    out["live_parity"] = None
    out["decision"] = {"ship": None, "reason": "no variant passed the pre-registered bar; signals unchanged"}
    if passing:
        oof_live = oof_forecasts(rows, live_parity=True)
        live = evaluate(oof_live)
        out["live_parity"] = {"verdicts": live["verdicts"], "pooled_2019_2025": live["pooled_2019_2025"],
                              "folds": live["folds"], "informational_only": filled}  # fmt: skip
        shippable = [v for v in passing if live["verdicts"][v]["passes"]]
        if filled:
            v = passing[0]
            out["decision"] = {"ship": v, "mode": "shadow",
                               "reason": f"{VARIANT_NAMES[v]} passed the bar; the live signal now supplies the OFS share "
                               "and the Nifty 20-session return (ADDENDUM.md), so the parity check is vacuous and "
                               "reported only. Coordinator decision on #151: it runs as a SHADOW test beside the "
                               "base-rate call, switching only under SHADOW.md's pre-registered criterion",
                               "calibrator": deployed_calibrator(oof, v)}  # fmt: skip
        elif shippable:
            v = shippable[0]
            out["decision"] = {"ship": v, "reason": f"{VARIANT_NAMES[v]} passed the bar and the live-parity check",
                               "calibrator": deployed_calibrator(oof_live, v)}  # fmt: skip
            if not out["decision_point"]["usable_at_decision"]:
                out["decision"] |= {"mode": "shadow", "reason": out["decision"]["reason"] + "; it "
                                    + out["decision_point"]["label"] + ", so it runs as a shadow test only"}  # fmt: skip
        else:
            out["decision"] = {"ship": None, "reason": "passed the bar but not the live-parity check "
                               f"({', '.join(passing)}); signals unchanged until the live features are filled"}  # fmt: skip
    return out


def signal_artefact(rows: list[Any], res: dict[str, Any], data_note: str) -> dict[str, Any]:
    """What `signals/ipo.py` reads (as a shadow test since #151): the calibrator, the model refitted on every usable row, the variant's
    walk-forward verdict and its pooled metrics and reliability bins computed on the CALIBRATED forecasts."""
    v = res["decision"]["ship"]
    if v is None:
        return {"ship": None, "reason": res["decision"]["reason"], "generated": date.today().isoformat()}
    p = res["pooled_2019_2025"]
    verdict = res["verdicts"][v]
    return {"ship": v, "mode": res["decision"].get("mode", "live"), "name": VARIANT_NAMES[v],
            "calibrator": res["decision"]["calibrator"],
            "final_model": im.fit(im.usable(rows)), "n_rows": len(im.usable(rows)), "min_cell": im.MIN_CELL,
            "gate": {"rule": f"Brier skill vs the base-rate table > 0 in >= {im.GATE_MIN_PASS} of the complete years "
                             f"{GATE_YEARS[0]}-{GATE_YEARS[-1]} AND pooled {GATE_YEARS[0]}-{GATE_YEARS[-1]} Brier below the "
                             "table's", **verdict},
            "pooled": {"n": p["n"], "brier": p[v]["brier"], "brier_table": p["brier_table"],
                       "bss_vs_table": p[v]["bss_vs_table"], "auc": p[v]["auc"], "auc_table": p["auc_table"],
                       "log_loss": p[v]["log_loss"], "reliability": p[v]["reliability"]},
            "folds": [{"year": f["year"], "n_test": f["n_test"], "brier_table": f["brier_table"],
                       "brier": f[v]["brier"], "bss_vs_table": f[v]["bss_vs_table"]} for f in res["folds"]],
            "variants_tested": list(VARIANTS), "data": data_note, "generated": date.today().isoformat(),
            "decision_point": res.get("decision_point") or im.decision_point(im.usable(rows)),
            "source": "evals/experiments/ipo_calibration (PREREG.md, ADDENDUM.md, RESULTS.md)"}  # fmt: skip


def load_signal_artefact(path: Path | None = None) -> dict[str, Any] | None:
    try:
        return json.loads((path or SIGNAL_ARTEFACT).read_text())
    except (OSError, ValueError):
        return None


def _r(x: Any) -> str:
    return "—" if x is None else f"{x:.4f}" if isinstance(x, float) else str(x)


def render(res: dict[str, Any], data_note: str) -> str:
    lines = ["# E-IPO-1 results: post-hoc calibration of the IPO listing model", "",
             f"Generated {date.today().isoformat()} by `finresearch.evals.ipo_calibration` from {data_note}. "
             "Pre-registration: `PREREG.md` (committed before this run).", "",
             "## Brier skill vs the base-rate table, by test year (positive = better than the table)", "",
             "| Year | n test | n pool | Table Brier | Raw model | T | S | P |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for f in res["folds"]:
        tag = "" if f["year"] in GATE_YEARS else " (partial, not gated)"
        lines.append(f"| {f['year']}{tag} | {f['n_test']} | {f['n_pool']} | {_r(f['brier_table'])} | "
                     + " | ".join(_r(f[v]["bss_vs_table"]) for v in ("raw", *VARIANTS)) + " |")  # fmt: skip
    p = res["pooled_2019_2025"]
    lines += ["", f"## Pooled 2019–2025 (n = {p['n']})", "", "| Forecast | Brier | BSS vs table | AUC | Log loss |",
              "|---|---|---|---|---|",
              f"| Base-rate table | {_r(p['brier_table'])} | 0 | {_r(p['auc_table'])} | {_r(p['log_loss_table'])} |"]  # fmt: skip
    for v in ("raw", *VARIANTS):
        lines.append(f"| {VARIANT_NAMES[v]} ({v}) | {_r(p[v]['brier'])} | {_r(p[v]['bss_vs_table'])} | "
                     f"{_r(p[v]['auc'])} | {_r(p[v]['log_loss'])} |")  # fmt: skip
    lines += ["", "## Verdicts (bar: BSS > 0 in ≥ 5 of 7 years AND pooled Brier below the table's)", "",
              "| Variant | Years passed | Pooled Brier better | Passes |", "|---|---|---|---|"]  # fmt: skip
    for v, d in res["verdicts"].items():
        lines.append(f"| {VARIANT_NAMES[v]} ({v}) | {len(d['years_passed'])}/7 {d['years_passed']} | "
                     f"{'yes' if d['pooled_brier_better'] else 'no'} | {'**yes**' if d['passes'] else 'no'} |")  # fmt: skip
    lines += ["", "## Fitted parameters by year (fitted on earlier OOF years only)", "", "| Year | T | Platt a, b | λ |",
              "|---|---|---|---|"]  # fmt: skip
    for f in res["folds"]:
        t, pp, s = f["T"]["params"], f["P"]["params"], f["S"]["params"]
        lines.append(
            f"| {f['year']} | {_r(t.get('T'))} | {_r(pp.get('a'))}, {_r(pp.get('b'))} | {_r(s.get('lambda'))} |"
        )
    lines += ["", "## Reliability, pooled 2019–2025 (equal-count bins: mean forecast → observed)", ""]
    for v in ("raw", *VARIANTS):
        bins = ", ".join(f"{b['mean_p']:.2f}→{b['observed']:.2f}" for b in p[v]["reliability"])
        lines.append(f"- {VARIANT_NAMES[v]}: {bins}")
    lines.append(
        f"- base-rate table: {', '.join(f'{b["mean_p"]:.2f}→{b["observed"]:.2f}' for b in p['reliability_table'])}"
    )
    if res.get("live_parity"):
        note = (
            " (informational: the live signal now supplies both, ADDENDUM.md)"
            if res.get("live_features_filled")
            else ""
        )
        lines += [
            "",
            f"## Live-parity check (OFS share and Nifty 20-session return blanked at prediction){note}",
            "",
        ]
        for v, d in res["live_parity"]["verdicts"].items():
            lines.append(f"- {VARIANT_NAMES[v]} ({v}): {len(d['years_passed'])}/7 years, pooled Brier better: "
                         f"{d['pooled_brier_better']}, passes: {d['passes']}")  # fmt: skip
    lines += ["", "## Decision", "", f"- Ship: {res['decision']['ship'] or 'nothing'}"
              + (f" (mode: {res['decision']['mode']})" if res["decision"].get("mode") else ""),
              f"- Reason: {res['decision']['reason']}", ""]  # fmt: skip
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=None, help="JSONL export; default: the ipo_history table")
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    a = ap.parse_args(argv)
    rows = im.load_rows(a.data)
    note = "the ipo_history table"
    if a.data is not None:
        note = f"`{a.data.name}` (sha256 {hashlib.sha256(a.data.read_bytes()).hexdigest()[:16]}…)"
    res = run(rows)
    res["data"] = note
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "results.json").write_text(json.dumps(res, indent=1, default=float) + "\n")
    (a.out / "RESULTS.md").write_text(render(res, note))
    SIGNAL_ARTEFACT.write_text(json.dumps(signal_artefact(rows, res, note), indent=1, default=float) + "\n")
    print(render(res, note))


if __name__ == "__main__":
    main()
