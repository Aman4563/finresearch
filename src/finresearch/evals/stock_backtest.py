"""Walk-forward backtest of the stock signal's momentum + trend rule (docs/dev/RESEARCH_ROADMAP.md §D.2, item 13).

The rule was fixed before any result was seen (pre-registered here and in fincalc.signals); nothing is fitted, so
each month's decision uses only prices known at that month end:
- trend: the adjusted close is above its 200-day simple moving average;
- momentum score: 12-1 month return (close 21 trading days ago / close 252 trading days ago - 1) divided by the
  annualised volatility of the last 252 daily returns ("risk-adjusted momentum", roadmap §D.2);
- portfolio: on the last trading day of each month hold, equally weighted, the top quintile of the universe
  (k = round(N/5) names) ranked by the momentum score among the names in an uptrend; slots left empty when fewer
  names qualify stay in cash at 0 % (conservative: T-bills paid ~4-7 %);
- benchmark: the same universe equally weighted, rebalanced monthly (same costs), and the NIFTY 50 price index.
Returns are price returns (no dividends) for the stocks and for the NIFTY 50 price index alike.
Timing (a stated simplification): the rule reads the month-end close and trades at that same close. A real order
fills at the next open at the earliest; the 5 bp slippage per side stands in for that gap, not a measured one.

Costs per side (dated constants, as researched 30-Sep-2026, roadmap §D.2/§D.7): STT 0.1 % on delivery buys and
sells; stamp duty 0.015 % on buys; NSE transaction charge ~0.00297 % plus 18 % GST; SEBI fee 0.0001 %; brokerage
assumed 0 (discount-broker delivery); plus 0.05 % assumed slippage each side.

Probability buckets for the signal (also pre-registered): trend (above / below the 200-DMA) x momentum score
(negative < 0 <= moderate < 1 <= strong). For each month and stock, the event is "the next 12 months' price return
beats the NIFTY 50 price index's". Monthly windows overlap by 11 months, so Wilson intervals use an effective n of
n/12 (and stocks in the same month are correlated too, so even that is optimistic).

Survivorship bias: the universe is TODAY's NIFTY 50 list applied to the past. Stocks that fell out of the index
(or were delisted) are missing, and today's members are partly members because they did well. Absolute returns are
therefore biased up; the excess over the equal-weight universe is less affected, but not free of it.

    uv run python -m finresearch.evals.stock_backtest            # reads data/cache/stock_backtest, writes evals/
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from finresearch.evals.stock_harvest import ARTEFACT_DIR, DATA_DIR
from finresearch.fincalc.signals import (
    TRADING_DAYS,
    Adjusted,
    adjust_for_actions,
    momentum_12_1,
    realised_vol,
    sma,
    wilson,
)

RULE_VERSION = "momentum-trend v1 (pre-registered 30-Sep-2026)"
STT, STAMP_BUY, NSE_TXN, GST, SEBI_FEE, SLIPPAGE = 0.001, 0.00015, 0.0000297, 0.18, 0.000001, 0.0005
COST_BUY = STT + STAMP_BUY + NSE_TXN * (1 + GST) + SEBI_FEE + SLIPPAGE
COST_SELL = STT + NSE_TXN * (1 + GST) + SEBI_FEE + SLIPPAGE
MOM_BANDS = ((float("-inf"), 0.0, "negative"), (0.0, 1.0, "moderate"), (1.0, float("inf"), "strong"))
WARMUP = TRADING_DAYS + 1


def bucket_of(above_trend: bool, mom_score: float) -> str:
    band = next(name for lo, hi, name in MOM_BANDS if lo <= mom_score < hi)
    return f"{'up' if above_trend else 'down'}trend / {band} momentum"


BUCKETS = [bucket_of(t, m) for t in (True, False) for m in (-1.0, 0.5, 2.0)]


@dataclass
class Series:
    symbol: str
    adj: Adjusted
    index_of: dict[date, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.index_of = {d: i for i, d in enumerate(self.adj.days)}


@dataclass
class State:
    above_trend: bool
    mom: float
    vol: float

    @property
    def score(self) -> float:
        return self.mom / self.vol


def state_at(s: Series, i: int) -> State | None:
    """The rule's inputs at bar i (inclusive), or None when there is not enough clean history."""
    if i + 1 < WARMUP:
        return None
    c = s.adj.close[: i + 1]
    lo = s.adj.days[i + 1 - WARMUP]
    if any(lo <= d <= s.adj.days[i] for d in s.adj.anomalies):
        return None
    m, v, ma = momentum_12_1(c), realised_vol(c), sma(c, 200)
    if m is None or not v or ma is None:
        return None
    return State(c[-1] > ma, m, v)


def month_ends(days: Sequence[date]) -> list[date]:
    out = []
    for a, b in itertools.pairwise(days):
        if (a.year, a.month) != (b.year, b.month):
            out.append(a)
    return out


def _ret(s: Series, a: date, b: date) -> float | None:
    """Price return from the close on (or last before) a to the close on (or last before) b; None across an
    unexplained jump or outside the series."""
    ia, ib = _at(s, a), _at(s, b)
    if ia is None or ib is None or ib <= ia:
        return None
    if any(s.adj.days[ia] < d <= s.adj.days[ib] for d in s.adj.anomalies):
        return None
    return s.adj.close[ib] / s.adj.close[ia] - 1


def _at(s: Series, d: date) -> int | None:
    if d in s.index_of:
        return s.index_of[d]
    # last bar on or before d, within a week
    from bisect import bisect_right

    i = bisect_right(s.adj.days, d) - 1
    return i if i >= 0 and (d - s.adj.days[i]).days <= 7 else None


def run(universe: dict[str, Series], index: list[tuple[date, float]]) -> dict[str, Any]:
    """Monthly walk-forward. Returns the monthly records, portfolio statistics and the probability buckets."""
    idx_days = [d for d, _ in index]
    idx_close = dict(index)
    ends = month_ends(idx_days)
    months: list[dict[str, Any]] = []
    w_prev: dict[str, float] = {}
    ew_prev: dict[str, float] = {}
    bucket_obs: dict[str, list[tuple[date, str, float, float]]] = {b: [] for b in BUCKETS}
    for t, t1 in itertools.pairwise(ends):
        states: dict[str, State] = {}
        for sym, s in universe.items():
            i = s.index_of.get(t)
            if i is not None and (st := state_at(s, i)) is not None:
                states[sym] = st
        if len(states) < 10:
            continue
        # probability buckets: the next 12 months against the index
        t12 = ends[ends.index(t) + 12] if ends.index(t) + 12 < len(ends) else None
        if t12 is not None:
            r_idx12 = idx_close[t12] / idx_close[t] - 1
            for sym, st in states.items():
                r12 = _ret(universe[sym], t, t12)
                if r12 is not None:
                    bucket_obs[bucket_of(st.above_trend, st.score)].append((t, sym, r12, r_idx12))
        k = max(1, round(len(states) / 5))
        ranked = sorted(
            (sym for sym, st in states.items() if st.above_trend), key=lambda x: -states[x].score
        )[:k]
        rets = {sym: _ret(universe[sym], t, t1) for sym in states}
        rets = {sym: r for sym, r in rets.items() if r is not None}
        target = {sym: 1 / k for sym in ranked if sym in rets}
        ew_target = {sym: 1 / len(rets) for sym in rets}
        gross = sum(w * rets[sym] for sym, w in target.items())
        ew_gross = sum(w * rets[sym] for sym, w in ew_target.items())
        cost, turnover = _trade_cost(w_prev, target)
        ew_cost, ew_turn = _trade_cost(ew_prev, ew_target)
        net, ew_net = gross - cost, ew_gross - ew_cost
        w_prev = _drift(target, rets, gross)
        ew_prev = _drift(ew_target, rets, ew_gross)
        months.append({"month_end": t.isoformat(), "next": t1.isoformat(), "universe": len(states),
                       "qualifying": sum(1 for st in states.values() if st.above_trend), "held": sorted(target),
                       "strategy": net, "strategy_gross": gross, "equal_weight": ew_net,
                       "nifty50": idx_close[t1] / idx_close[t] - 1, "turnover": turnover, "cost": cost,
                       "ew_turnover": ew_turn})  # fmt: skip
    return {"months": months, "stats": portfolio_stats(months), "buckets": bucket_stats(bucket_obs),
            "by_period": _by_period(bucket_obs)}  # fmt: skip


def _trade_cost(before: dict[str, float], after: dict[str, float]) -> tuple[float, float]:
    buys = sum(max(0.0, after.get(s, 0.0) - before.get(s, 0.0)) for s in set(before) | set(after))
    sells = sum(max(0.0, before.get(s, 0.0) - after.get(s, 0.0)) for s in set(before) | set(after))
    return buys * COST_BUY + sells * COST_SELL, (buys + sells) / 2


def _drift(w: dict[str, float], rets: dict[str, float], gross: float) -> dict[str, float]:
    """Weights at the end of the month after prices moved (cash is the remainder)."""
    return {s: x * (1 + rets[s]) / (1 + gross) for s, x in w.items()} if gross > -1 else {}


# --------------------------------------------------------------------------- statistics
def cagr(rets: Sequence[float], periods_per_year: int = 12) -> float | None:
    if not rets:
        return None
    g = math.prod(1 + r for r in rets)
    return g ** (periods_per_year / len(rets)) - 1 if g > 0 else -1.0


def max_drawdown(rets: Sequence[float]) -> float:
    peak = level = 1.0
    worst = 0.0
    for r in rets:
        level *= 1 + r
        peak = max(peak, level)
        worst = min(worst, level / peak - 1)
    return worst


def ann_vol(rets: Sequence[float], periods_per_year: int = 12) -> float | None:
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    return math.sqrt(sum((x - m) ** 2 for x in rets) / (len(rets) - 1) * periods_per_year)


def newey_west_t(x: Sequence[float], lags: int = 6) -> float | None:
    """t-statistic of the mean of x with Newey-West (Bartlett) standard errors."""
    n = len(x)
    if n < lags + 2:
        return None
    m = sum(x) / n
    e = [v - m for v in x]
    s = sum(v * v for v in e) / n
    for lag in range(1, lags + 1):
        cov = sum(e[i] * e[i - lag] for i in range(lag, n)) / n
        s += 2 * (1 - lag / (lags + 1)) * cov
    return m / math.sqrt(s / n) if s > 0 else None


def portfolio_stats(months: list[dict[str, Any]]) -> dict[str, Any]:
    if not months:
        return {}
    st = [m["strategy"] for m in months]
    ew = [m["equal_weight"] for m in months]
    nf = [m["nifty50"] for m in months]
    ex = [a - b for a, b in zip(st, ew, strict=True)]
    hits = sum(1 for x in ex if x > 0)
    ci = wilson(hits, len(ex))
    c_s, c_e, c_n = cagr(st), cagr(ew), cagr(nf)
    return {"months": len(months), "from": months[0]["month_end"], "to": months[-1]["next"],
            "cagr": {"strategy": c_s, "equal_weight_universe": c_e, "nifty50_price": c_n,
                     "strategy_gross_of_costs": cagr([m["strategy_gross"] for m in months])},
            "excess_cagr_vs_equal_weight": c_s - c_e if c_s is not None and c_e is not None else None,
            "excess_cagr_vs_nifty50": c_s - c_n if c_s is not None and c_n is not None else None,
            "volatility": {"strategy": ann_vol(st), "equal_weight_universe": ann_vol(ew), "nifty50_price": ann_vol(nf)},
            "max_drawdown": {"strategy": max_drawdown(st), "equal_weight_universe": max_drawdown(ew),
                             "nifty50_price": max_drawdown(nf)},
            "monthly_hit_rate_vs_equal_weight": {"hits": hits, "n": len(ex), "rate": hits / len(ex),
                                                 "wilson95": list(ci) if ci else None},
            "annual_turnover": sum(m["turnover"] for m in months) / len(months) * 12,
            "annual_cost_drag": sum(m["cost"] for m in months) / len(months) * 12,
            "newey_west_t_excess_vs_equal_weight": newey_west_t(ex),
            "average_names_held": sum(len(m["held"]) for m in months) / len(months)}  # fmt: skip


def _bucket_row(obs: list[tuple[date, str, float, float]]) -> dict[str, Any]:
    n = len(obs)
    k = sum(1 for _, _, r, ri in obs if r > ri)
    n_eff = max(1, round(n / 12)) if n else 0
    ci = wilson(round(k / n * n_eff), n_eff) if n else None
    ex = [r - ri for _, _, r, ri in obs]
    mean = sum(ex) / n if n else None
    sd = math.sqrt(sum((x - mean) ** 2 for x in ex) / (n - 1)) if n > 1 and mean is not None else None
    return {"n": n, "hits": k, "p": k / n if n else None, "n_effective": n_eff,
            "wilson95": list(ci) if ci else None, "mean_excess_12m": mean, "sd_excess_12m": sd,
            "median_excess_12m": sorted(ex)[n // 2] if n else None}  # fmt: skip


def bucket_stats(obs: dict[str, list[tuple[date, str, float, float]]]) -> dict[str, Any]:
    allobs = [o for v in obs.values() for o in v]
    return {"all": _bucket_row(allobs), **{b: _bucket_row(v) for b, v in obs.items()}}


def _by_period(obs: dict[str, list[tuple[date, str, float, float]]]) -> dict[str, Any]:
    """Stability check: the same buckets in the first and second half of the sample."""
    days = sorted({o[0] for v in obs.values() for o in v})
    if not days:
        return {}
    mid = days[len(days) // 2]
    out = {}
    for name, keep in (
        (f"to {mid.isoformat()}", lambda d: d < mid),
        (f"from {mid.isoformat()}", lambda d: d >= mid),
    ):
        out[name] = {b: _bucket_row([o for o in v if keep(o[0])]) for b, v in obs.items()}
    return out


# --------------------------------------------------------------------------- data on disk
def load(data_dir: Path = DATA_DIR) -> tuple[dict[str, Series], list[tuple[date, float]], dict[str, Any]]:
    universe: dict[str, Series] = {}
    coverage: dict[str, Any] = {}
    for p in sorted((data_dir / "prices").glob("*.csv")):
        sym = p.stem
        rows = list(csv.DictReader(p.open()))
        if not rows:
            coverage[sym] = {"days": 0}
            continue
        days = [date.fromisoformat(r["date"]) for r in rows]
        close = [float(r["close"]) for r in rows]
        hi = [float(r["high"]) if r["high"] else None for r in rows]
        lo = [float(r["low"]) if r["low"] else None for r in rows]
        acts_path = data_dir / "actions" / f"{sym}.json"
        acts = json.loads(acts_path.read_text()) if acts_path.exists() else []
        actions = [(date.fromisoformat(a["ex_date"]), a["subject"]) for a in acts if a.get("ex_date")]
        adj = adjust_for_actions(days, close, actions, high=hi, low=lo)
        universe[sym] = Series(sym, adj)
        coverage[sym] = {"first": days[0].isoformat(), "last": days[-1].isoformat(), "days": len(days),
                         "splits_bonuses_applied": [[d.isoformat(), f] for d, f in adj.applied],
                         "unexplained_jumps": [d.isoformat() for d in adj.anomalies]}  # fmt: skip
    idx_rows = list(csv.DictReader((data_dir / "index_NIFTY50.csv").open()))
    index = [(date.fromisoformat(r["date"]), float(r["close"])) for r in idx_rows]
    return universe, index, coverage


def main() -> None:
    universe, index, coverage = load()
    res = run(universe, index)
    constituents = sorted(ARTEFACT_DIR.glob("nifty50_constituents_*.csv"))
    out = {
        "rule": RULE_VERSION,
        "generated": date.today().isoformat(),
        "universe": {"name": "NIFTY 50 (today's list applied to the past)",
                     "constituents_file": constituents[-1].name if constituents else None,
                     "symbols": len(universe), "coverage": coverage},
        "data": {"prices": "NSE historical trade data (EQ series), unadjusted; splits and bonuses adjusted from NSE "
                           "corporate actions (fincalc.signals.adjust_for_actions)",
                 "index": "NIFTY 50 price index, NSE historicalOR/indicesHistory (no total-return series there)",
                 "returns": "price returns, no dividends, for stocks and index alike"},
        "costs": {"buy": COST_BUY, "sell": COST_SELL,
                  "components": {"stt": STT, "stamp_buy": STAMP_BUY, "nse_txn": NSE_TXN, "gst_on_txn": GST,
                                 "sebi_fee": SEBI_FEE, "slippage": SLIPPAGE, "brokerage": 0}},
        "caveats": [
            "Survivorship bias: today's NIFTY 50 applied to the past; absolute returns are biased up.",
            "Monthly 12-month windows overlap, and stocks in the same month are correlated: the effective sample is "
            "far smaller than n. Wilson intervals use n/12 and are still optimistic.",
            "Price returns only (no dividends) on both sides; cash earns 0 %.",
            "Not investment advice; a personal research backtest, not a track record.",
        ],
        **res,
    }  # fmt: skip
    ARTEFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTEFACT_DIR / "results.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    s = res["stats"]
    print(json.dumps({k: s[k] for k in ("months", "from", "to", "cagr", "excess_cagr_vs_equal_weight",
                                        "max_drawdown", "monthly_hit_rate_vs_equal_weight")}, indent=1),
          file=sys.stderr)  # fmt: skip


if __name__ == "__main__":
    main()
