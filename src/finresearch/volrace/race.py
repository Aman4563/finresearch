"""The volatility forecast race's walk-forward (P3.7, #390; evals/experiments/vol_forecast_race/PREREG.md). Pure: the
sessions come from volrace/data.py (or a test), so nothing here reads the database.

Everything is total log-return variance over the horizon. Origin t = a session's close; the target for horizon h is
h x Yang-Zhang over the bars of sessions t+1..t+h (within-contract front-future OHLC). Models fitted by regression are
refitted on the first session of each calendar month from the burn-in on, on an expanding window of origins s with
s + h <= the refit session (the purge), at most MAX_ROWS of the latest; forecasts at origins from that refit up to the
next use its fit. GARCH-type models refit on the same days on the last GARCH_WINDOW close-to-close returns.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

from finresearch.direction import metrics as dm
from finresearch.fincalc import realvol as rv
from finresearch.fincalc import taildist as td
from finresearch.volrace import stats

HORIZONS = (5, 10, 22)
TRAIL = 22  # the trailing-window estimators' window: the incumbent practice "trailing RV as the forecast"
MAX_ROWS = 5000  # OLS on at most 5,000 rows (the issue)
GARCH_WINDOW = 1000
DM_ALPHA = 0.05  # Holm-adjusted, across the horizons
ADOPT_MIN_HORIZONS = 2  # of 3
MCS_ALPHA, MCS_REPS, MCS_MEAN_BLOCK, SEED = 0.10, 10_000, 5.0, 390
MIN_MCS_ROWS = 10  # fewer non-overlapping windows where every model is known: no MCS (unknown)
CHALLENGER, BENCHMARK = "har_iv", "mkt"
MODELS = {
    "har_iv": "primary challenger: log HAR-IV, ln y = b0 + b_d ln d_t + b_w ln d^(5) + b_m ln d^(22) + b_iv ln IV^2_t",
    "mkt": "benchmark: ln y = a + b ln w_h, the bias-corrected horizon-matched market forecast",
    "har": "log HAR without IV",
    "iv_raw": "w_h itself, unadjusted",
    "ewma": "h x the RiskMetrics EWMA (lambda 0.94) one-step variance of close-to-close returns",
    "garch": "GARCH(1,1) QMLE, sum of the h-step variance path",
    "gjr": "GJR-GARCH(1,1) QMLE, sum of the h-step variance path (symmetric-shock persistence)",
    "cc22": "h x the 22-session close-to-close sample variance",
    "pk22": "h x the 22-session mean Parkinson variance",
    "gk22": "h x the 22-session mean Garman-Klass variance",
    "rs22": "h x the 22-session mean Rogers-Satchell variance",
    "yz22": "h x the 22-session Yang-Zhang variance",
}  # fmt: skip
TRAILING = {"cc22": rv.close_to_close_variance, "pk22": lambda b: rv.mean_of(rv.parkinson, b),
            "gk22": lambda b: rv.mean_of(rv.garman_klass, b), "rs22": lambda b: rv.mean_of(rv.rogers_satchell, b),
            "yz22": rv.yang_zhang}  # fmt: skip


@dataclass(frozen=True)
class Session:
    day: date
    bar: rv.Bar  # the front future's bar; prev_close is the same contract's close on the previous session
    term: tuple[tuple[int, float | None], ...]  # (calendar days to expiry, ATM IV %) of each fitted expiry


def horizon_variance(term: Sequence[tuple[int, float | None]], days: int) -> float | None:
    """Total implied variance to `days` calendar days (/365): linear in total variance between the bracketing
    expiries, flat at the shortest expiry's IV before it, unknown beyond the longest (never extrapolated)."""
    pts = sorted((d, (v / 100) ** 2 * d / 365) for d, v in term if d > 0 and v is not None and v > 0)
    if not pts or days <= 0:
        return None
    if days <= pts[0][0]:
        return pts[0][1] / pts[0][0] * days
    for (d0, w0), (d1, w1) in pairwise(pts):
        if d0 <= days <= d1:
            return w0 + (w1 - w0) * (days - d0) / (d1 - d0)
    return None


def target(ss: Sequence[Session], t: int, h: int) -> float | None:
    if t + h >= len(ss):
        return None
    v = rv.yang_zhang([s.bar for s in ss[t + 1 : t + h + 1]])
    return None if v is None else h * v


def implied(ss: Sequence[Session], t: int, h: int) -> float | None:
    """w_h at origin t: to the calendar date of session t + h (the NSE session calendar is published in advance)."""
    if t + h >= len(ss):
        return None
    return horizon_variance(ss[t].term, (ss[t + h].day - ss[t].day).days)


def refit_days(ss: Sequence[Session], burn_in: int = 0) -> list[int]:
    return [i for i in range(max(burn_in, 1), len(ss)) if ss[i].day.month != ss[i - 1].day.month]


def training_origins(ss: Sequence[Session], refit: int, h: int) -> list[int]:
    """Candidate training origins at a refit: every s with s + h <= refit (expanding; MAX_ROWS applies after the
    rows with unknown inputs are left out)."""
    return list(range(0, max(refit - h + 1, 0)))


Fitter = Callable[[Sequence[float], int], td.GjrFit]


def _fit(rows: list[tuple[list[float], float]]) -> rv.Ols | None:
    rows = rows[-MAX_ROWS:]
    return rv.ols([x for x, _ in rows], [y for _, y in rows]) if rows else None


def _ln(x: float | None) -> float | None:
    return math.log(x) if x is not None and x > 0 else None


def forecasts(ss: Sequence[Session], horizons: Sequence[int], *, burn_in: int,
              fit_garch: Fitter | None = None) -> dict[int, list[dict[str, float | None]]]:  # fmt: skip
    """Every model's forecast of total variance over each horizon at every origin (None before the first refit at or
    after `burn_in`, or when an input is unknown). `fit_garch` replaces both GARCH fitters (tests)."""
    n = len(ss)
    fitters: dict[str, Fitter] = {"garch": fit_garch or rv.garch11_fit,
                                  "gjr": fit_garch or (lambda r, w: td.gjr_fit(r, window=w))}  # fmt: skip
    daily = [rv.daily_variance(s.bar) for s in ss]
    rets = [rv.close_to_close(s.bar) for s in ss]
    known: list[float] = []  # known returns up to each origin (an unknown return is left out and counted)
    upto: list[int] = []
    for r in rets:
        if r is not None:
            known.append(r)
        upto.append(len(known))
    ew = td.ewma_variance(known) if len(known) >= td.BACKCAST_TAU else []
    har = [rv.har_row(daily, t) for t in range(n)]
    trail = {m: [None if t < TRAIL - 1 else f([s.bar for s in ss[t - TRAIL + 1 : t + 1]]) for t in range(n)]
             for m, f in TRAILING.items()}  # fmt: skip
    refits = refit_days(ss, burn_in)
    seg = {t: r for r, nxt in zip(refits, [*refits[1:], n], strict=True) for t in range(r, nxt)}
    garch: dict[int, dict[str, td.GjrParams | None]] = {
        r: {m: f(known[: upto[r]], GARCH_WINDOW).params for m, f in fitters.items()} for r in refits
    }
    next_s2: dict[tuple[int, str], float] = {}
    for t, r in seg.items():
        for m, p in garch[r].items():
            if p is not None:
                next_s2[(t, m)] = td.filter_returns(known[: upto[t]][-GARCH_WINDOW:], p).next_sigma2
    out: dict[int, list[dict[str, float | None]]] = {}
    for h in horizons:
        y = [target(ss, t, h) for t in range(n)]
        w = [implied(ss, t, h) for t in range(n)]
        iv2 = [
            None if w[t] is None else _ln(w[t] / ((ss[t + h].day - ss[t].day).days / 365)) for t in range(n)
        ]
        fits: dict[int, dict[str, rv.Ols | None]] = {}
        for r in refits:
            cand = [s for s in training_origins(ss, r, h) if _ln(y[s]) is not None]
            fits[r] = {
                "har": _fit([(har[s], _ln(y[s])) for s in cand if har[s] is not None]),  # type: ignore[misc]
                "har_iv": _fit([(har[s] + [iv2[s]], _ln(y[s])) for s in cand
                                if har[s] is not None and iv2[s] is not None]),  # type: ignore[misc, operator]
                "mkt": _fit([([_ln(w[s])], _ln(y[s])) for s in cand if _ln(w[s]) is not None]),  # type: ignore[misc]
            }  # fmt: skip
        rows: list[dict[str, float | None]] = []
        for t in range(n):
            f: dict[str, float | None] = dict.fromkeys(MODELS)
            if t in seg:
                ft = fits[seg[t]]
                if ft["har"] is not None and har[t] is not None:
                    f["har"] = rv.log_forecast(ft["har"], har[t])  # type: ignore[arg-type]
                if ft["har_iv"] is not None and har[t] is not None and iv2[t] is not None:
                    f["har_iv"] = rv.log_forecast(ft["har_iv"], har[t] + [iv2[t]])  # type: ignore[arg-type, operator]
                if ft["mkt"] is not None and _ln(w[t]) is not None:
                    f["mkt"] = rv.log_forecast(ft["mkt"], [_ln(w[t])])  # type: ignore[list-item]
                f["iv_raw"] = w[t]
                f["ewma"] = h * ew[upto[t]] if ew and upto[t] >= td.BACKCAST_TAU else None
                for m in ("garch", "gjr"):
                    p = garch[seg[t]][m]
                    if p is not None and (t, m) in next_s2:
                        f[m] = rv.garch_horizon_variance(p, next_s2[(t, m)], h)
                for m, v in trail.items():
                    f[m] = None if v[t] is None else h * v[t]
            rows.append(f)
        out[h] = rows
    return out


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def run(ss: Sequence[Session], horizons: Sequence[int] = HORIZONS, *, burn_in: int, fit_garch: Fitter | None = None,
        reps: int = MCS_REPS, seed: int = SEED) -> dict:  # fmt: skip
    """Score every model per horizon on non-overlapping h-session windows: the grid starts at the first origin at or
    after `burn_in` where the target, the challenger and the benchmark are all known and steps by h. DM-HLN
    (benchmark against challenger, one-sided, h = 1 as the windows do not overlap) on QLIKE, Holm across horizons;
    the 90 % MCS on QLIKE over the grid origins where every model is known; Mincer-Zarnowitz per model. The window
    passes when the challenger beats the benchmark at Holm p < 0.05 AND is in the MCS at >= 2 horizons."""
    n = len(ss)
    fc = forecasts(ss, horizons, burn_in=burn_in, fit_garch=fit_garch)
    out: dict = {"sessions": n, "first": ss[0].day.isoformat() if ss else None,
                 "last": ss[-1].day.isoformat() if ss else None, "burn_in": burn_in, "horizons": {}}  # fmt: skip
    raw_p: dict[str, float | None] = {}
    for h in horizons:
        y = [target(ss, t, h) for t in range(n)]
        f = fc[h]
        ok = [t for t in range(burn_in, n) if y[t] is not None and f[t][CHALLENGER] and f[t][BENCHMARK]]
        grid = list(range(ok[0], n, h)) if ok else []
        origins = [t for t in grid if y[t] is not None and f[t][CHALLENGER] and f[t][BENCHMARK]]
        lb = [rv.qlike(y[t], f[t][BENCHMARK]) for t in origins]
        lc = [rv.qlike(y[t], f[t][CHALLENGER]) for t in origins]
        d = dm.diebold_mariano_hln(lb, lc, 1)  # type: ignore[arg-type]
        raw_p[str(h)] = d["p"]  # type: ignore[assignment]
        models = {}
        for m in MODELS:
            ts = [t for t in grid if y[t] is not None and f[t][m] is not None and f[t][m] > 0]  # type: ignore[operator]
            models[m] = {
                "n": len(ts),
                "qlike": _mean([rv.qlike(y[t], f[t][m]) for t in ts]),  # type: ignore[misc]
                "mse": _mean([rv.mse(y[t], f[t][m]) for t in ts]),  # type: ignore[misc]
                "mz": rv.mincer_zarnowitz([y[t] for t in ts], [f[t][m] for t in ts]),
            }  # type: ignore[misc]
        full = [
            t for t in grid if y[t] is not None and all(f[t][m] is not None and f[t][m] > 0 for m in MODELS)
        ]  # type: ignore[operator]
        mc = None
        if len(full) >= MIN_MCS_ROWS:
            res = stats.mcs({m: [rv.qlike(y[t], f[t][m]) for t in full] for m in MODELS},  # type: ignore[misc]
                            alpha=MCS_ALPHA, reps=reps, mean_block=MCS_MEAN_BLOCK, seed=seed)  # fmt: skip
            mc = {
                "n": res.n,
                "included": list(res.included),
                "pvalues": res.pvalues,
                "order": list(res.order),
            }
        out["horizons"][h] = {
            "origins": origins, "grid": len(grid), "dm": d, "models": models, "mcs": mc,
            "mcs_rows_dropped": len(grid) - len(full),
            "unknown_targets": sum(1 for t in range(burn_in, n - h) if y[t] is None),
        }  # fmt: skip
    holm = dm.holm(raw_p)
    out["holm"] = {h: holm[str(h)] for h in horizons}
    wins = [h for h in horizons if (p := holm[str(h)]) is not None and p < DM_ALPHA
            and (mc := out["horizons"][h]["mcs"]) is not None and CHALLENGER in mc["included"]]  # fmt: skip
    out["winning_horizons"] = wins
    out["passes"] = len(wins) >= ADOPT_MIN_HORIZONS
    out["window_verdict"] = (
        f"{'passes' if out['passes'] else 'fails'}: {len(wins)} of {len(horizons)} horizons have the challenger "
        f"beating the benchmark (Holm p < {DM_ALPHA}) inside the {1 - MCS_ALPHA:.0%} MCS (needs "
        f"{ADOPT_MIN_HORIZONS}); adoption also needs the other registered window"
    )
    return out
