"""IPO listing model: walk-forward validation and the deployable fit (docs/dev/RESEARCH_ROADMAP.md §D.1, item 14).

Targets (mainboard EQ issues with a final book and a listing-day open):
- y₁ = 1[listing open > issue price] → L2 logistic regression (`fincalc.ipo_model.fit_logistic`);
- y₂ = listing-open return, winsorised at the training 1st/99th percentiles → quantile regression at τ = 0.1/0.5/0.9.

Features (evidence: Neupane et al. [33]; regime break [35]):
    ln(1+QIB×), ln(1+NII×), ln(1+retail×)        final combined NSE+BSE book (activeCat)
    ln(public book ₹ cr)                           ex-anchor shares offered × issue price: the issue-size proxy
                                                   available for every row and live during bidding
    OFS share (+ missing flag)                     from NSE's issue-size text; imputed with the training median
    Nifty 50 20-session return to the issue close  knowable at the decision; 0 when missing
    IPO listings in the 90 days before the close   market heat
    post-Apr-2022 dummy                            NII allotment reform
The upper-band dummy is not used: nearly every book-built issue prices at the top.

Walk-forward: expanding window by listing year; train on years < t, test on year t, for t = 2019…2026. The reference
forecasts are what the app would otherwise show:
- the base-rate table: the Laplace-smoothed P(gain) of the test issue's QIB band × regime cell in the training data
  (falling back to the regime, then all training rows, when a cell has fewer than MIN_CELL issues);
- climatology: the training share of issues that opened above the issue price.

Pass bar (pre-registered in the roadmap): Brier skill > 0 against the BASE-RATE TABLE in at least 5 of the 7
complete test years 2019–2025 (2026 is partial and reported only). The penalty (λ = 1 on standardised features)
is fixed in advance, not tuned on the test years. If the bar is not met the signal uses the base-rate table.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from finresearch.fincalc import ipo as fipo
from finresearch.fincalc import ipo_model as m

ARTEFACT = Path(__file__).parent / "artefacts" / "ipo_model_walkforward.json"
TEST_YEARS = tuple(range(2019, 2027))
GATE_YEARS = tuple(range(2019, 2026))  # complete calendar years
GATE_MIN_PASS = 5
L2 = 1.0
TAUS = (0.1, 0.5, 0.9)
MIN_CELL = 5
FEATURES = ("ln_qib", "ln_nii", "ln_retail", "ln_book_cr", "ofs_share", "ofs_missing", "nifty20", "ipo_count_90d",
            "post_2022")  # fmt: skip
FEATURE_LABELS = {
    "ln_qib": "QIB subscription", "ln_nii": "NII subscription", "ln_retail": "Retail subscription",
    "ln_book_cr": "Public book size", "ofs_share": "OFS share of the issue", "ofs_missing": "OFS split unknown",
    "nifty20": "Nifty 50, 20 sessions", "ipo_count_90d": "IPO listings, last 90 days",
    "post_2022": "Post-Apr-2022 regime",
}  # fmt: skip


def _f(row: Any, key: str) -> float | None:
    v = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _year(row: Any) -> int | None:
    v = row.get("listing_date") if isinstance(row, dict) else getattr(row, "listing_date", None)
    if v is None:
        return None
    return v.year if isinstance(v, date) else int(str(v)[:4])


def usable(rows: Iterable[Any], series: str = "EQ") -> list[Any]:
    """Mainboard rows with a QIB book, NII and retail figures, a listing open, a public book and a regime flag."""
    out = []
    for r in rows:
        s = r.get("series") if isinstance(r, dict) else getattr(r, "series", None)
        if s != series:
            continue
        if any(
            _f(r, k) is None for k in ("qib_times", "nii_times", "retail_times", "return_open", "post_2022")
        ):
            continue
        if not _f(r, "public_book_cr") or _year(r) is None:
            continue
        out.append(r)
    return out


def raw_features(r: Any) -> dict[str, float | None]:
    """Model inputs before imputation; `ofs_share` and `nifty20` may be None."""
    return {"ln_qib": math.log1p(max(_f(r, "qib_times") or 0.0, 0.0)),
            "ln_nii": math.log1p(max(_f(r, "nii_times") or 0.0, 0.0)),
            "ln_retail": math.log1p(max(_f(r, "retail_times") or 0.0, 0.0)),
            "ln_book_cr": math.log(max(_f(r, "public_book_cr") or 1.0, 1e-3)),
            "ofs_share": _f(r, "ofs_share"),
            "nifty20": _f(r, "nifty_ret20_close"),
            "ipo_count_90d": _f(r, "ipo_count_90d") or 0.0,
            "post_2022": _f(r, "post_2022") or 0.0}  # fmt: skip


# ---------------------------------------------------------------- decision-time availability (#244, evals.timing)
# The decision: a retail bid must be placed before the UPI mandate cut-off, 17:00 IST on the issue's closing day
# (docs/dev/RESEARCH_ROADMAP.md §B "5 pm UPI cut-off"; db.models.IpoHistory). When each feature is first knowable:
#   subscription (QIB / NII / retail ×)  the FINAL combined book, published only after bidding closes at 17:00 —
#                                        after the decision (the harvest has no intraday history: roadmap item 15
#                                        archives it only from 28-Sep-2026); DECLARED below, so the model is labelled
#   Nifty 20-session return              to the closing day's close, 15:30 IST (NSE equity close)
#   IPO listings in the last 90 days     strictly before the closing day
#   public book, OFS share, regime       from the RHP / price-band notice, before the issue opens (the book uses the
#                                        issue price, the upper band for nearly every book-built issue)
DECISION = "issue close day, 17:00 IST (UPI mandate cut-off)"
FINAL_BOOK = ("ln_qib", "ln_nii", "ln_retail")  # the final combined book: published after the 17:00 close
POST_DECISION = FINAL_BOOK  # declared late features; anything late and not listed here raises LookAheadError


def _day(row: Any, key: str) -> date | None:
    v = row.get(key) if isinstance(row, dict) else getattr(row, key, None)
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def feature_timing(row: Any) -> tuple[list[Any], Any] | None:
    """(features with their availability, decision time) for one issue; None without a closing date."""
    from datetime import timedelta

    from finresearch.evals.timing import Feature
    from finresearch.fincalc.dates import ist_datetime

    close = _day(row, "ipo_end")
    if close is None:
        return None
    opened = ist_datetime(_day(row, "ipo_start") or close)
    after_close = ist_datetime(close, 17, 0) + timedelta(
        minutes=1
    )  # a lower bound: the final book comes later
    feats = [
        Feature(n, after_close, "final combined book, published after the 17:00 bid close")
        for n in FINAL_BOOK
    ]
    feats += [Feature("nifty20", ist_datetime(close, 15, 30), "Nifty 50 close on the closing day"),
              Feature("ipo_count_90d", ist_datetime(close), "listings strictly before the closing day"),
              *(Feature(n, opened, "RHP / price-band notice") for n in
                ("ln_book_cr", "ofs_share", "ofs_missing", "post_2022"))]  # fmt: skip
    return feats, ist_datetime(close, 17, 0)


def decision_point(rows: Iterable[Any]) -> dict[str, Any]:
    """Checks every row's features against the decision time (raising on an undeclared late feature) and says
    whether the model could be used at the decision point; it cannot while POST_DECISION is not empty."""
    from finresearch.evals.timing import TimingReport, check

    rep = TimingReport(DECISION)
    for r in rows:
        t = feature_timing(r)
        if t is not None:
            sym = r.get("symbol") if isinstance(r, dict) else getattr(r, "symbol", None)
            check(t[0], t[1], allow=POST_DECISION, report=rep, context=str(sym or "?"))
    return rep.to_dict()


def usable_at_decision() -> bool:
    """False while the model trains on features published after the decision (final subscription book)."""
    return not POST_DECISION


def matrix(feats: list[dict[str, float | None]], ofs_median: float) -> np.ndarray:
    rows = []
    for f in feats:
        ofs = f["ofs_share"]
        rows.append([f["ln_qib"], f["ln_nii"], f["ln_retail"], f["ln_book_cr"],
                     ofs_median if ofs is None else ofs, 1.0 if ofs is None else 0.0,
                     f["nifty20"] or 0.0, f["ipo_count_90d"], f["post_2022"]])  # fmt: skip
    return np.asarray(rows, dtype=float).reshape(len(rows), len(FEATURES))


def fit(rows: list[Any], *, l2: float = L2) -> dict[str, Any]:
    """Fit the logistic and quantile models on `rows`; returns a JSON-serialisable model."""
    feats = [raw_features(r) for r in rows]
    ofs = [f["ofs_share"] for f in feats if f["ofs_share"] is not None]
    ofs_median = float(np.median(ofs)) if ofs else 0.0
    x = matrix(feats, ofs_median)
    st = m.Standardizer.fit(x)
    z = st.transform(x)
    ret = np.asarray([_f(r, "return_open") for r in rows], dtype=float)
    y = (ret > 0).astype(float)
    lo, hi = np.quantile(ret, 0.01), np.quantile(ret, 0.99)
    rw = np.clip(ret, lo, hi)
    return {"features": list(FEATURES), "mean": st.mean.tolist(), "std": st.std.tolist(), "ofs_median": ofs_median,
            "l2": l2, "logistic": m.fit_logistic(z, y, l2=l2).tolist(),
            "quantile": {str(t): m.fit_quantile(z, rw, t, l2=l2).tolist() for t in TAUS},
            "winsor": [float(lo), float(hi)], "n": len(rows), "prevalence": float(y.mean())}  # fmt: skip


def predict(model: dict[str, Any], feats: list[dict[str, float | None]]) -> dict[str, np.ndarray]:
    x = matrix(feats, model["ofs_median"])
    z = (x - np.asarray(model["mean"])) / np.asarray(model["std"])
    out = {"p": m.predict_logistic(np.asarray(model["logistic"]), z)}
    qs = {t: m.predict_linear(np.asarray(b), z) for t, b in model["quantile"].items()}
    # quantile crossing is possible with separate fits: sort per row (Chernozhukov et al. 2010 rearrangement)
    stacked = np.sort(np.vstack([qs[str(t)] for t in TAUS]), axis=0)
    for i, t in enumerate(TAUS):
        out[f"q{int(t * 100)}"] = stacked[i]
    return out


def contributions(model: dict[str, Any], feat: dict[str, float | None]) -> dict[str, float]:
    """Each feature's term βⱼ·zⱼ in the logit for one issue (the intercept is the average training issue)."""
    x = matrix([feat], model["ofs_median"])[0]
    z = (x - np.asarray(model["mean"])) / np.asarray(model["std"])
    beta = np.asarray(model["logistic"])
    return {name: float(beta[i + 1] * z[i]) for i, name in enumerate(model["features"])}


# --- base-rate table forecasts ----------------------------------------------------------------------


def table_forecaster(train: list[Any]):
    """P(gain) from the QIB band × regime cell (Laplace-smoothed), with regime and overall fallbacks; also the cell's
    empirical 10/50/90% return quantiles for the pinball comparison."""
    cells: dict[tuple[str | None, bool], list[float]] = {}
    for r in train:
        band, post, ret = fipo.band_of(_f(r, "qib_times")), bool(_f(r, "post_2022")), _f(r, "return_open")
        for key in ((band, post), (None, post), (None, None)):
            cells.setdefault(key, []).append(ret)

    def forecast(r: Any) -> tuple[float, dict[str, float], str]:
        band, post = fipo.band_of(_f(r, "qib_times")), bool(_f(r, "post_2022"))
        for key, label in (((band, post), "cell"), ((None, post), "regime"), ((None, None), "all")):
            rets = cells.get(key, [])
            if len(rets) >= MIN_CELL:
                k = sum(1 for x in rets if x > 0)
                qs = {f"q{int(t * 100)}": fipo.quantile(rets, t) for t in TAUS}
                return fipo.smoothed_rate(k, len(rets)), qs, label
        return 0.5, {"q10": 0.0, "q50": 0.0, "q90": 0.0}, "none"

    return forecast


# --- walk-forward --------------------------------------------------------------------------------------


def _naive(test: list[Any], y: np.ndarray) -> dict[str, Any]:
    """Naive rules from the roadmap: apply when QIB > 10x, or when total > 20x. Hit rate = P(gain | applied)."""
    out = {}
    for name, key, cut in (("qib_gt_10x", "qib_times", 10.0), ("total_gt_20x", "total_times", 20.0)):
        applied = np.asarray([(_f(r, key) or 0.0) > cut for r in test])
        k, n = int(y[applied].sum()), int(applied.sum())
        out[name] = {"applied": n, "hit_rate": k / n if n else None,
                     "hit_rate_ci": fipo.wilson_interval(k, n), "skipped_gain_rate":
                     float(y[~applied].mean()) if (~applied).any() else None}  # fmt: skip
    return out


def walk_forward(rows: list[Any], *, years: Iterable[int] = TEST_YEARS, l2: float = L2) -> dict[str, Any]:
    data = usable(rows)
    folds: list[dict[str, Any]] = []
    pooled: dict[str, list[float]] = {"p_model": [], "p_table": [], "p_clim": [], "y": []}
    for t in years:
        train = [r for r in data if _year(r) < t]
        test = [r for r in data if _year(r) == t]
        if len(train) < 30 or not test:
            folds.append({"year": t, "n_test": len(test), "n_train": len(train), "skipped": "too few rows"})
            continue
        model = fit(train, l2=l2)
        feats = [raw_features(r) for r in test]
        pred = predict(model, feats)
        ret = np.asarray([_f(r, "return_open") for r in test])
        y = (ret > 0).astype(float)
        tab = table_forecaster(train)
        tf = [tab(r) for r in test]
        p_table = np.asarray([x[0] for x in tf])
        p_clim = np.full(len(test), model["prevalence"])
        bm, bt, bc = m.brier(pred["p"], y), m.brier(p_table, y), m.brier(p_clim, y)
        fold = {"year": t, "n_train": len(train), "n_test": len(test), "prevalence": float(y.mean()),
                "brier_model": bm, "brier_table": bt, "brier_climatology": bc,
                "bss_vs_table": m.brier_skill(bm, bt), "bss_vs_climatology": m.brier_skill(bm, bc),
                "bss_table_vs_climatology": m.brier_skill(bt, bc),
                "auc_model": m.auc(pred["p"], y), "auc_table": m.auc(p_table, y),
                "table_fallbacks": sum(1 for x in tf if x[2] != "cell"),
                "pinball_model": {}, "pinball_table": {}, "coverage_p10_p90_model": None,
                "naive_rules": _naive(test, y)}  # fmt: skip
        for tau in TAUS:
            key = f"q{int(tau * 100)}"
            fold["pinball_model"][key] = m.pinball(ret, pred[key], tau)
            fold["pinball_table"][key] = m.pinball(ret, [x[1][key] for x in tf], tau)
        inside = (ret >= pred["q10"]) & (ret <= pred["q90"])
        fold["coverage_p10_p90_model"] = float(inside.mean())
        folds.append(fold)
        pooled["p_model"] += pred["p"].tolist()
        pooled["p_table"] += p_table.tolist()
        pooled["p_clim"] += p_clim.tolist()
        pooled["y"] += y.tolist()
    gate = [f for f in folds if f["year"] in GATE_YEARS and "bss_vs_table" in f]
    passed_years = [f["year"] for f in gate if (f["bss_vs_table"] or 0) > 0]
    passes = len(passed_years) >= GATE_MIN_PASS
    y = pooled["y"]
    report: dict[str, Any] = {
        "method": "walk-forward, expanding window by listing year; L2 logistic (λ=1) + quantile regression "
                  "(τ=0.1/0.5/0.9) on standardised features; references: QIB-band × regime base-rate table "
                  "(Laplace-smoothed) and climatology",
        "features": list(FEATURES),
        "n_rows": len(data),
        "folds": folds,
        "gate": {"rule": f"Brier skill vs the base-rate table > 0 in >= {GATE_MIN_PASS} of the complete years "
                         f"{GATE_YEARS[0]}-{GATE_YEARS[-1]}",
                 "years_evaluated": [f["year"] for f in gate], "years_passed": passed_years, "passes": passes},
        "pooled": {"n": len(y), "brier_model": m.brier(pooled["p_model"], y) if y else None,
                   "brier_table": m.brier(pooled["p_table"], y) if y else None,
                   "brier_climatology": m.brier(pooled["p_clim"], y) if y else None,
                   "auc_model": m.auc(pooled["p_model"], y) if y else None,
                   "auc_table": m.auc(pooled["p_table"], y) if y else None,
                   "reliability_model": m.reliability(pooled["p_model"], y),
                   "reliability_table": m.reliability(pooled["p_table"], y)},
    }  # fmt: skip
    if report["pooled"]["brier_model"] is not None:
        p = report["pooled"]
        p["bss_vs_table"] = m.brier_skill(p["brier_model"], p["brier_table"])
        p["bss_vs_climatology"] = m.brier_skill(p["brier_model"], p["brier_climatology"])
    report["final_model"] = fit(data, l2=l2) if passes and len(data) >= 30 else None
    # trained on the final book: even a passing model is labelled and may only run as a shadow test (#244)
    report["decision_point"] = decision_point(data)
    if data:
        years_seen = sorted({_year(r) for r in data})
        report["data"] = {"first_year": years_seen[0], "last_year": years_seen[-1],
                          "last_listing": max(str(r.get("listing_date") if isinstance(r, dict) else r.listing_date)
                                              for r in data)}  # fmt: skip
    return report


def build_artefact(rows: list[Any]) -> dict[str, Any]:
    """The committed artefact: the walk-forward report (with the final model only if it passed the bar) plus a
    snapshot of the base-rate tables, which the API and signal use while the database has no harvested rows."""
    report = walk_forward(rows)
    eq = [r for r in rows if (r.get("series") if isinstance(r, dict) else r.series) == "EQ"]
    listed = [str(r.get("listing_date") if isinstance(r, dict) else r.listing_date) for r in eq
              if _f(r, "return_open") is not None]  # fmt: skip
    as_of = max(listed) if listed else None
    report["base_rates"] = {by: {**fipo.base_rates(eq, by=by), "as_of": as_of} for by in ("qib", "total")}
    return report


def summary(report: dict[str, Any]) -> dict[str, Any]:
    def rnd(v):
        return None if v is None else round(v, 4)

    return {"n_rows": report["n_rows"], "gate": report["gate"],
            "folds": [{k: (rnd(v) if isinstance(v, float) else v) for k, v in f.items()
                       if k in ("year", "n_train", "n_test", "prevalence", "brier_model", "brier_table",
                                "brier_climatology", "bss_vs_table", "bss_vs_climatology", "auc_model", "auc_table",
                                "coverage_p10_p90_model", "skipped")} for f in report["folds"]],
            "pooled": {k: rnd(v) for k, v in report["pooled"].items() if not isinstance(v, list)}}  # fmt: skip


def load_rows(path: Path | None = None) -> list[Any]:
    """Rows from a JSONL export, or from the ipo_history table."""
    if path is not None:
        from finresearch.evals.ipo_history import load_jsonl

        return load_jsonl(path)
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory

    with session_scope() as s:
        rows = s.scalars(select(IpoHistory)).all()
        return [
            {c.name: getattr(r, c.name) for c in IpoHistory.__table__.columns if c.name != "raw"}
            for r in rows
        ]


def load_artefact(path: Path = ARTEFACT) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
