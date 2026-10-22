"""Validation simulations of #381's acceptance (a)-(c). Seeded and pure; `finresearch fno backtest recal-sim` runs them
and writes JSON (evals/experiments/fno_backtest/ADDENDUM.md records the results).

(b) Interval coverage: the 95 % CI of a known mean (0) on AR(1), GARCH(1,1)-t and AR(1)+GARCH-t series, for four
    methods: i.i.d. percentile bootstrap, stationary-bootstrap percentile, stationary-bootstrap BCa
    (inference.cluster_bca with one observation per cluster) and the Newey-West (Bartlett) t-interval. Target 93-97 %.
(c) DM size: d_t = an equal-weight MA(h−1) of i.i.d. N(0, 1) shocks (the overlap of h-step forecast errors; mean 0, so
    the null holds), one-sided test at nominal 5 %: the registered Bartlett variance over max(h − 1, 5) lags vs the
    rectangular variance over h − 1 lags (direction.metrics.diebold_mariano_hln). Not the review's critic2/checks.py,
    which is not in this repository: this is our own Monte Carlo of the same question.
(a) DSR rule: programmes of N correlated strategies built from a base daily P&L matrix (the real out-of-sample seller
    P&L when a history database is given; else a synthetic negatively skewed series) by a joint stationary
    bootstrap: strategy j = ρ · (its base column on a common resample) + √(1 − ρ²) · (the same column on its own
    resample), demeaned, then strategy 0 gets the planted annual Sharpe (0 = every strategy null). Each rule declares
    strategies: DSR rules (v_cand, v_null, max; the registered v1 is v_cand with N = all strategies, identical here
    because every simulated trial has a known SR) declare the best-SR strategy when its DSR >= 0.95; Romano & Wolf
    (2005) stepdown on studentised means declares every strategy it rejects at FWER 5 %; Hansen's (2005) SPA_c
    declares the best-t strategy when p < 0.05. FWER = P(a null strategy is declared); power = P(strategy 0 is).
    Bootstrap t-statistics are studentised by each resample's own sd (no HAC), a stated simplification.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise
from typing import Any

import numpy as np

from finresearch.backtest import inference
from finresearch.backtest.metrics import N01
from finresearch.direction import metrics as dmm


# --------------------------------------------------------------------------- (b) coverage
def simulate_series(kind: str, n: int, rng: np.random.Generator, phi: float = 0.3, df: int = 5) -> np.ndarray:
    """A mean-0 series: iid | ar1 | garch_t | ar1_garch_t (GARCH(1,1) ω 0.05, α 0.10, β 0.85, unit variance;
    Student-t shocks with `df` degrees of freedom scaled to unit variance)."""
    burn = 200
    m = n + burn
    if kind in ("garch_t", "ar1_garch_t"):
        z = rng.standard_t(df, m) / math.sqrt(df / (df - 2))
        e, s2 = np.empty(m), 1.0
        for t in range(m):
            e[t] = math.sqrt(s2) * z[t]
            s2 = 0.05 + 0.10 * e[t] ** 2 + 0.85 * s2
    else:
        e = rng.standard_normal(m)
    if kind in ("ar1", "ar1_garch_t"):
        x = np.empty(m)
        x[0] = e[0]
        for t in range(1, m):
            x[t] = phi * x[t - 1] + e[t]
        e = x
    return e[burn:]


def intervals(
    x: np.ndarray, draws: int, seed: int, level: float = 0.95
) -> dict[str, tuple[float, float] | None]:
    n = len(x)
    rng = np.random.default_rng(seed)
    a = (1 - level) / 2
    iid = x[rng.integers(0, n, size=(draws, n))].mean(axis=1)
    L = inference.block_len(n, 1)
    st = x[inference.stationary_indices(n, float(L), draws, rng)].mean(axis=1)
    bca = inference.cluster_bca(list(x), list(range(n)), draws, seed + 1, level, mean_block=float(L))
    se = math.sqrt(dmm.newey_west_var(list(x), inference.nw_lag(n)))
    z = N01.inv_cdf(1 - a)
    return {
        "iid_percentile": (float(np.quantile(iid, a)), float(np.quantile(iid, 1 - a))),
        "stationary_percentile": (float(np.quantile(st, a)), float(np.quantile(st, 1 - a))),
        "stationary_bca": None if bca["lo"] is None else (bca["lo"], bca["hi"]),
        "hac_t": (float(x.mean()) - z * se, float(x.mean()) + z * se),
    }


def coverage(kinds: Sequence[str], n: int, reps: int, draws: int, seed: int) -> dict[str, dict[str, Any]]:
    """Share of `reps` series whose CI holds the true mean 0, per DGP and method (an unknown CI counts as a miss)."""
    out: dict[str, dict[str, Any]] = {}
    for kind in kinds:
        rng = np.random.default_rng(seed)
        hits: dict[str, int] = {}
        for r in range(reps):
            for m, ci in intervals(simulate_series(kind, n, rng), draws, seed + 7 * r).items():
                hits[m] = hits.get(m, 0) + int(ci is not None and ci[0] <= 0 <= ci[1])
        out[kind] = {m: h / reps for m, h in hits.items()} | {"n": n, "reps": reps, "draws": draws}
    return out


# --------------------------------------------------------------------------- (c) DM size
def dm_size(h: int, n: int, reps: int, seed: int, alpha: float = 0.05) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    rej = {"bartlett_registered": 0, "rectangular_h_minus_1": 0}
    unknown = 0
    lag_reg = max(h - 1, 5)  # direction.specs.dm_lag
    for _ in range(reps):
        u = rng.standard_normal(n + h - 1)
        d = np.convolve(u, np.ones(h), mode="valid")  # MA(h-1): sums of h consecutive shocks
        ref, mod = list(d), [0.0] * n
        p1 = dmm.diebold_mariano(ref, mod, h, lag_reg)["p"]
        p2 = dmm.diebold_mariano_hln(ref, mod, h)["p"]
        rej["bartlett_registered"] += int(p1 is not None and p1 < alpha)
        rej["rectangular_h_minus_1"] += int(p2 is not None and p2 < alpha)
        unknown += int(p2 is None)
    return {"h": h, "n": n, "reps": reps, "nominal": alpha, **{k: v / reps for k, v in rej.items()},
            "rectangular_unknown_share": unknown / reps}  # fmt: skip


# --------------------------------------------------------------------------- (a) DSR rule
def synthetic_base(t: int, k: int, seed: int) -> np.ndarray:
    """A seller-like daily return matrix (t × k): small steady gains with rare large losses (negative skew)."""
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0005, 0.004, (t, k))
    crash = rng.random((t, k)) < 0.02
    return np.where(crash, x - rng.exponential(0.02, (t, k)), x)


def programme(
    base: np.ndarray, n: int, planted_sr: float, rho: float, rng: np.random.Generator
) -> np.ndarray:
    """One simulated programme (module docstring (a)): a t × n daily return matrix, strategy 0 planted."""
    t, k = base.shape
    base = base - base.mean(
        axis=0
    )  # every column's resampling distribution has mean 0: each strategy is null
    L = float(inference.block_len(t, 1))
    common = inference.stationary_indices(t, L, 1, rng)[0]
    own = inference.stationary_indices(t, L, n, rng)
    cols = [j % k for j in range(n)]
    x = np.column_stack([rho * base[common, c] + math.sqrt(1 - rho * rho) * base[own[j], c]
                         for j, c in enumerate(cols)])  # fmt: skip
    sd = x.std(axis=0, ddof=1)
    x[:, 0] += planted_sr / math.sqrt(252) * sd[0]
    return x


def _moments(x: np.ndarray) -> list[dict[str, float]]:
    mu, sd = x.mean(axis=0), x.std(axis=0, ddof=1)
    z = (x - mu) / x.std(axis=0, ddof=0)
    sk, ku = (z**3).mean(axis=0), (z**4).mean(axis=0)
    return [{"daily_sr": float(mu[j] / sd[j]), "days": x.shape[0] + 1, "skew": float(sk[j]), "kurtosis": float(ku[j])}
            for j in range(x.shape[1])]  # fmt: skip


def declared(x: np.ndarray, draws: int, rng: np.random.Generator, alpha: float = 0.05) -> dict[str, set[int]]:
    """Strategies each rule declares (module docstring (a))."""
    t, n = x.shape
    w = _moments(x)
    srs = [m["daily_sr"] for m in w]
    best = int(np.argmax(srs))
    out: dict[str, set[int]] = {}
    for rule in inference.DSR_RULES:
        d = inference.dsr_v2(w[best], srs, rule)["dsr"]
        out[f"dsr_{rule}"] = {best} if d is not None and d >= 1 - alpha else set()
    mu, sd = x.mean(axis=0), x.std(axis=0, ddof=1)
    tstat = math.sqrt(t) * mu / sd
    idx = inference.stationary_indices(t, float(inference.block_len(t, 1)), draws, rng)
    bm = np.stack([x[i].mean(axis=0) for i in idx])  # draws × n, one joint resample per draw
    bs = np.sqrt(np.maximum(np.stack([(x[i] ** 2).mean(axis=0) for i in idx]) - bm**2, 1e-300) * t / (t - 1))
    # Romano-Wolf stepdown: the 1 - alpha quantile of max over the remaining nulls of the centred bootstrap t
    tb = math.sqrt(t) * (bm - mu) / bs  # studentised with each resample's own sd
    rejected: set[int] = set()
    while True:
        keep = [j for j in range(n) if j not in rejected]
        if not keep:
            break
        crit = float(np.quantile(tb[:, keep].max(axis=1), 1 - alpha))
        new = {j for j in keep if tstat[j] > crit}
        if not new:
            break
        rejected |= new
    out["romano_wolf"] = rejected
    # Hansen (2005) SPA_c: Z* = d* - g_c(d-bar), g_c(x) = x 1{x >= -sqrt(sd^2/t * 2 log log t)}
    thr = -np.sqrt(sd**2 / t * 2 * math.log(math.log(t)))
    g = np.where(mu >= thr, mu, 0.0)
    stat = max(float(tstat.max()), 0.0)
    boot = np.maximum((math.sqrt(t) * (bm - g) / bs).max(axis=1), 0.0)
    p = float(np.mean(boot >= stat)) if stat > 0 else 1.0
    out["spa"] = {int(np.argmax(tstat))} if p < alpha else set()
    return out


def dsr_rule_sim(base: np.ndarray, n: int, planted_sr: float, programmes: int, draws: int, seed: int,
                 rho: float = 0.5) -> dict[str, Any]:  # fmt: skip
    rng = np.random.default_rng(seed)
    fw: dict[str, int] = {}
    pw: dict[str, int] = {}
    for _ in range(programmes):
        x = programme(base, n, planted_sr, rho, rng)
        for rule, dec in declared(x, draws, rng).items():
            nulls = dec - ({0} if planted_sr > 0 else set())
            fw[rule] = fw.get(rule, 0) + int(bool(nulls))
            pw[rule] = pw.get(rule, 0) + int(planted_sr > 0 and 0 in dec)
    return {"n": n, "planted_sr": planted_sr, "programmes": programmes, "draws": draws, "rho": rho,
            "t": base.shape[0], "fwer": {k: v / programmes for k, v in fw.items()},
            "power": {k: v / programmes for k, v in pw.items()} if planted_sr > 0 else None}  # fmt: skip


def seller_returns(history: Any) -> tuple[np.ndarray, list[str]]:
    """The registered candidates' (the sellers') out-of-sample daily returns on the days every one of them has
    (engine.run as evaluate runs it, same-print fills): a t × k matrix and the spec keys. Needs the registration."""
    from finresearch.backtest import engine, evaluate, specs

    evaluate.require_registered()
    pairs = [(s, engine.resolve(s, None)) for s in specs.SPECS if s.role == "candidate"]
    runnable = [c for _, c in pairs if c is not None]
    ends = [history.latest(c.underlying) for c in runnable]
    end = max((d for d in ends if d), default=specs.IS_END)
    res = engine.run(runnable, history, specs.IS_START, end, specs.BOOK, warmup_days=specs.IV_WARMUP_DAYS)
    series = {}
    for s, c in pairs:
        if c is None or s.key not in res:
            continue
        eq = [(d, v) for d, v in res[s.key]["equity"] if d >= specs.OOS_START]
        if len(eq) > 1:  # an underlying with no stored history (no_data in evaluate) is left out
            series[s.key] = {b[0]: b[1] / a[1] - 1 for a, b in pairwise(eq) if a[1] > 0}
    days = sorted(set.intersection(*(set(v) for v in series.values()))) if series else []
    keys = sorted(series)
    return np.array([[series[k][d] for k in keys] for d in days]), keys
