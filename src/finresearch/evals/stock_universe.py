"""Stock backtest on a point-in-time Nifty 50 universe (S1), with the pre-registered variants S3 (market trend filter),
S4 (low-volatility sleeve) and S8 (momentum + low-vol ensemble). Pre-registration:
evals/experiments/stock_pit_universe/PREREG.md; research: portfolio-insights research §2.2, roadmap §D.2.

Membership comes from NSE Indices press releases (committed change log `nifty50_changes.csv`, one row per inclusion
or exclusion with its press-release URL). It is rebuilt backwards from the NSE constituent list fetched on
30-Sep-2026, checking at each step that an included stock is in the later set, an excluded one is not, and the set
size is 50 (51 while Tata Motors DVR was a member).

Harness (as momentum-trend v1, `evals.stock_backtest`): month-end decisions and trades at the close, v1 costs, price
returns, cash at 0 %. Calendar: NIFTYBEES trading days (the NSE index endpoint has holes). Eligible at t: members at t
with ≥ 252 clean days of history. Benchmark: eligible members, equal weight, monthly, same costs.

Strategies: B0 = v1 unchanged; V1 = B0 only when NIFTYBEES > its 200-day average (else cash); V2 = lowest-volatility
quintile; V3 = top quintile of the average of the momentum rank and the low-volatility rank.

Pass bar per variant: net excess vs the equal-weight PIT universe > 0 and Newey–West t > 2.39 (two-sided Bonferroni
for 3 tests at 5 %). The deflated Sharpe ratio (Bailey & López de Prado 2014) with N = 4 trials is reported.

    uv run python -m finresearch.evals.stock_universe
"""

from __future__ import annotations

import csv
import itertools
import json
import math
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from statistics import NormalDist
from typing import Any

from finresearch.config import REPO_ROOT
from finresearch.evals.stock_backtest import (
    COST_BUY,
    COST_SELL,
    Series,
    State,
    _drift,
    _ret,
    _trade_cost,
    load,
    month_ends,
    portfolio_stats,
    state_at,
)
from finresearch.evals.stock_harvest import ARTEFACT_DIR, DATA_DIR
from finresearch.fincalc.signals import sma

OUT_DIR = REPO_ROOT / "evals" / "experiments" / "stock_pit_universe"
CHANGES = OUT_DIR / "nifty50_changes.csv"
MARKET = "NIFTYBEES"
# the symbol NSE serves (and the harvest saved) the history under, for members whose symbol changed
DATA_SYMBOL = {"TATAMOTORS": "TMPV", "ZOMATO": "ETERNAL", "MCDOWELL-N": "UNITDSPR", "IBULHSGFIN": "SAMMAANCAP",
               "INFRATEL": "INDUSTOWER"}  # fmt: skip
T_CRIT = NormalDist().inv_cdf(1 - 0.05 / 6)  # two-sided Bonferroni, 3 tests, α = 0.05 → 2.394
N_TRIALS = 4
EULER_GAMMA = 0.5772156649015329
VARIANTS = ("V1", "V2", "V3")
NAMES = {"B0": "momentum-trend v1 (baseline, point in time)", "V1": "v1 + market trend filter (S3)",
         "V2": "low-volatility quintile (S4)", "V3": "momentum + low-vol rank ensemble (S8)",
         "B0_survivors": "v1 on today's list (survivorship twin, descriptive)"}  # fmt: skip


# --------------------------------------------------------------------------------------------- membership
@dataclass(frozen=True)
class Change:
    effective: date
    kind: str  # "include" | "exclude"
    symbol: str
    source: str


def load_changes(path: Path = CHANGES) -> list[Change]:
    with path.open() as f:
        return [Change(date.fromisoformat(r["effective"]), r["change"], r["symbol"], r["source"])
                for r in csv.DictReader(f)]  # fmt: skip


def reconstruct(current: Iterable[str], changes: Sequence[Change], *,
                allowed_sizes: tuple[int, ...] = (50, 51)) -> tuple[list[tuple[date, frozenset[str]]], list[str]]:  # fmt: skip
    """Undo `changes` backwards from `current` (the set after the last change). Returns the periods as
    (effective date, set from that date on), oldest first, where the first entry's date is date.min, and the list of
    invariant violations (empty when the log is consistent)."""
    s = set(current)
    problems: list[str] = []
    by_date: dict[date, list[Change]] = {}
    for c in changes:
        by_date.setdefault(c.effective, []).append(c)
    periods: list[tuple[date, frozenset[str]]] = []
    for d in sorted(by_date, reverse=True):
        if len(s) not in allowed_sizes:
            problems.append(f"{d}: {len(s)} members after the change")
        periods.append((d, frozenset(s)))
        for c in by_date[d]:
            if c.kind == "include":
                if c.symbol not in s:
                    problems.append(f"{d}: included {c.symbol} is not in the later set")
                s.discard(c.symbol)
            elif c.kind == "exclude":
                if c.symbol in s:
                    problems.append(f"{d}: excluded {c.symbol} is still in the later set")
                s.add(c.symbol)
            else:
                problems.append(f"{d}: unknown change {c.kind!r}")
    if len(s) not in allowed_sizes:
        problems.append(f"before {min(by_date) if by_date else 'start'}: {len(s)} members")
    periods.append((date.min, frozenset(s)))
    return list(reversed(periods)), problems


def members_on(periods: Sequence[tuple[date, frozenset[str]]], t: date) -> frozenset[str]:
    """The set in force at the close of t (changes effective on or before t applied)."""
    out = periods[0][1]
    for d, s in periods:
        if d <= t:
            out = s
    return out


def data_symbol(sym: str) -> str:
    return DATA_SYMBOL.get(sym, sym)


# --------------------------------------------------------------------------------------------- strategies
Selector = Callable[[dict[str, State], bool], list[str]]


def _k(n: int) -> int:
    return max(1, round(n / 5))


def select_b0(states: dict[str, State], market_up: bool) -> list[str]:
    """v1: top quintile (k of ALL eligible) by risk-adjusted momentum among names above their 200-DMA."""
    k = _k(len(states))
    up = sorted((s for s, st in states.items() if st.above_trend), key=lambda s: (-states[s].score, s))
    return up[:k]


def select_v1(states: dict[str, State], market_up: bool) -> list[str]:
    return select_b0(states, market_up) if market_up else []


def select_v2(states: dict[str, State], market_up: bool) -> list[str]:
    k = _k(len(states))
    return sorted(states, key=lambda s: (states[s].vol, s))[:k]


def ranks(values: dict[str, float], *, descending: bool) -> dict[str, float]:
    """1 = best; ties share the average rank."""
    order = sorted(values, key=lambda s: -values[s] if descending else values[s])
    out: dict[str, float] = {}
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for x in order[i : j + 1]:
            out[x] = (i + j) / 2 + 1
        i = j + 1
    return out


def select_v3(states: dict[str, State], market_up: bool) -> list[str]:
    k = _k(len(states))
    rm = ranks({s: st.score for s, st in states.items()}, descending=True)
    rv = ranks({s: st.vol for s, st in states.items()}, descending=False)
    return sorted(states, key=lambda s: ((rm[s] + rv[s]) / 2, s))[:k]


SELECTORS: dict[str, Selector] = {"B0": select_b0, "V1": select_v1, "V2": select_v2, "V3": select_v3}


# --------------------------------------------------------------------------------------------- the walk
def market_trend(market: Series, t: date) -> bool | None:
    i = market.index_of.get(t)
    if i is None or i + 1 < 200:
        return None
    c = market.adj.close[: i + 1]
    ma = sma(c, 200)
    return None if ma is None else c[-1] > ma


def run_pit(universe: dict[str, Series], market: Series, members: Callable[[date], Iterable[str]],
            selector: Selector) -> dict[str, Any]:  # fmt: skip
    """Monthly walk-forward on a point-in-time universe. `members(t)` gives the PIT symbols at month-end t."""
    ends = month_ends(market.adj.days)
    months: list[dict[str, Any]] = []
    w_prev: dict[str, float] = {}
    ew_prev: dict[str, float] = {}
    coverage = {"member_months": 0, "with_prices": 0, "eligible": 0, "missing": {}}
    for t, t1 in itertools.pairwise(ends):
        mem = sorted(members(t))
        states: dict[str, State] = {}
        for sym in mem:
            s = universe.get(data_symbol(sym))
            coverage["member_months"] += 1
            if s is None or s.adj.days[0] > t:
                coverage["missing"][sym] = coverage["missing"].get(sym, 0) + 1
                continue
            coverage["with_prices"] += 1
            i = s.index_of.get(t)
            if i is not None and (st := state_at(s, i)) is not None:
                states[sym] = st
        coverage["eligible"] += len(states)
        up = market_trend(market, t)
        if len(states) < 10 or up is None:
            continue
        held = selector(states, up)
        rets = {sym: _ret(universe[data_symbol(sym)], t, t1) for sym in states}
        rets = {sym: r for sym, r in rets.items() if r is not None}
        k = _k(len(states))
        target = {sym: 1 / k for sym in held if sym in rets}  # as v1: a slot left empty stays in cash
        ew_target = {sym: 1 / len(rets) for sym in rets}
        gross = sum(w * rets[sym] for sym, w in target.items())
        ew_gross = sum(w * rets[sym] for sym, w in ew_target.items())
        cost, turnover = _trade_cost(w_prev, target)
        ew_cost, ew_turn = _trade_cost(ew_prev, ew_target)
        w_prev, ew_prev = _drift(target, rets, gross), _drift(ew_target, rets, ew_gross)
        m_ret = _ret(market, t, t1)
        months.append({"month_end": t.isoformat(), "next": t1.isoformat(), "members": len(mem), "universe": len(states),
                       "market_up": up, "held": sorted(target), "strategy": gross - cost, "strategy_gross": gross,
                       "equal_weight": ew_gross - ew_cost, "nifty50": m_ret if m_ret is not None else 0.0,
                       "turnover": turnover, "cost": cost, "ew_turnover": ew_turn})  # fmt: skip
    return {"months": months, "coverage": coverage}


# --------------------------------------------------------------------------------------------- statistics
def _moments(x: Sequence[float]) -> tuple[float, float, float, float]:
    """mean, sample sd, skewness, kurtosis (non-excess; 3 for a normal) — population moments for the shape."""
    n = len(x)
    m = sum(x) / n
    sd = math.sqrt(sum((v - m) ** 2 for v in x) / (n - 1))
    m2 = sum((v - m) ** 2 for v in x) / n
    m3 = sum((v - m) ** 3 for v in x) / n
    m4 = sum((v - m) ** 4 for v in x) / n
    return m, sd, (m3 / m2**1.5 if m2 > 0 else 0.0), (m4 / m2**2 if m2 > 0 else 3.0)


def sharpe(x: Sequence[float]) -> float | None:
    """Per-period Sharpe ratio mean/sd (of excess returns here)."""
    if len(x) < 3:
        return None
    m, sd, _, _ = _moments(x)
    return m / sd if sd > 0 else None


def expected_max_sharpe(var_sr: float, n_trials: int) -> float:
    """SR₀ = √V[SR]·((1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(N·e))), the expected maximum Sharpe of N unskilled trials
    (Bailey & López de Prado 2014, eq. for the deflated Sharpe benchmark); 0 for a single trial."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    z = NormalDist().inv_cdf
    return math.sqrt(var_sr) * (
        (1 - EULER_GAMMA) * z(1 - 1 / n_trials) + EULER_GAMMA * z(1 - 1 / (n_trials * math.e))
    )


def probabilistic_sharpe(sr: float, sr0: float, n: int, skew: float, kurt: float) -> float:
    """PSR(SR₀) = Φ((SR − SR₀)·√(n−1) / √(1 − skew·SR + (kurt−1)/4·SR²)), per-period SR, non-excess kurtosis."""
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    if denom <= 0 or n < 2:
        return float("nan")
    return NormalDist().cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(denom))


def deflated_sharpe(
    x: Sequence[float], trial_sharpes: Sequence[float], n_trials: int = N_TRIALS
) -> dict[str, Any]:
    sr = sharpe(x)
    if sr is None:
        return {"sharpe": None, "dsr": None}
    _, _, sk, ku = _moments(x)
    ts = [s for s in trial_sharpes if s is not None]
    var = sum((s - sum(ts) / len(ts)) ** 2 for s in ts) / (len(ts) - 1) if len(ts) > 1 else 0.0
    sr0 = expected_max_sharpe(var, n_trials)
    return {"sharpe_monthly": sr, "sharpe_annualised": sr * math.sqrt(12), "skew": sk, "kurtosis": ku,
            "var_trial_sharpes": var, "sr0_monthly": sr0, "n_trials": n_trials,
            "dsr": probabilistic_sharpe(sr, sr0, len(x), sk, ku)}  # fmt: skip


def summarise(months: list[dict[str, Any]]) -> dict[str, Any]:
    out = portfolio_stats(months)
    ex = [m["strategy"] - m["equal_weight"] for m in months]
    out["mean_monthly_excess_vs_equal_weight"] = sum(ex) / len(ex) if ex else None
    out["months_in_cash"] = sum(1 for m in months if not m["held"])
    half = len(months) // 2
    out["halves"] = {}
    for name, part in (("first", months[:half]), ("second", months[half:])):
        if part:
            ps = portfolio_stats(part)
            out["halves"][name] = {"from": ps["from"], "to": ps["to"], "cagr": ps["cagr"],
                                   "excess_cagr_vs_equal_weight": ps["excess_cagr_vs_equal_weight"],
                                   "newey_west_t": ps["newey_west_t_excess_vs_equal_weight"]}  # fmt: skip
    return out


def evaluate(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ex = {k: [m["strategy"] - m["equal_weight"] for m in r["months"]] for k, r in results.items()}
    trials = [sharpe(ex[k]) for k in ("B0", *VARIANTS) if k in ex]
    verdicts = {}
    for k in ("B0", *VARIANTS):
        if k not in results:
            continue
        s = results[k]["stats"]
        t = s.get("newey_west_t_excess_vs_equal_weight")
        excess = s.get("excess_cagr_vs_equal_weight")
        verdicts[k] = {"excess_cagr_vs_equal_weight": excess, "newey_west_t": t, "t_critical": T_CRIT,
                       "deflated_sharpe": deflated_sharpe(ex[k], trials),
                       "passes": (k != "B0" and excess is not None and excess > 0 and t is not None and t > T_CRIT)}  # fmt: skip
    return verdicts


# --------------------------------------------------------------------------------------------- main
def _current_list() -> list[str]:
    path = sorted(ARTEFACT_DIR.glob("nifty50_constituents_*.csv"))[-1]
    with path.open() as f:
        cur = [r["Symbol"].strip() for r in csv.DictReader(f)]
    inverse = {v: k for k, v in DATA_SYMBOL.items()}
    return [inverse.get(s, s) for s in cur]  # the log uses the symbols of the day


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.2f} %"


def render(out: dict[str, Any]) -> str:
    cov = out["coverage"]
    lines = ["# Point-in-time Nifty 50 backtest: results", "",
             f"Generated {out['generated']} by `finresearch.evals.stock_universe`. Pre-registration: `PREREG.md` "
             "(committed before this run).", "",
             f"Membership: {out['membership']['changes']} changes from NSE Indices press releases, invariant violations: "
             f"{len(out['membership']['problems'])}. Price coverage: {cov['with_prices']} of {cov['member_months']} "
             f"member-months ({cov['with_prices'] / max(1, cov['member_months']) * 100:.1f} %); missing: "
             f"{', '.join(f'{k} ({v} mo)' for k, v in sorted(cov['missing'].items())) or 'none'}.", "",
             "| Strategy | Months | CAGR net | EW PIT universe | Excess vs EW | NW t | Max DD | Turnover/yr | "
             "Deflated SR | Passes |", "|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
    for k, r in out["strategies"].items():
        s, v = r["stats"], out["verdicts"].get(k)
        dsr = (v or {}).get("deflated_sharpe", {}).get("dsr")
        lines.append(f"| {NAMES[k]} | {s['months']} | {_pct(s['cagr']['strategy'])} | "
                     f"{_pct(s['cagr']['equal_weight_universe'])} | {_pct(s['excess_cagr_vs_equal_weight'])} | "
                     f"{s['newey_west_t_excess_vs_equal_weight'] or 0:.2f} | {_pct(s['max_drawdown']['strategy'])} | "
                     f"{s['annual_turnover']:.2f} | {'—' if dsr is None else f'{dsr:.3f}'} | "
                     f"{'—' if v is None or k == 'B0' else ('**yes**' if v['passes'] else 'no')} |")  # fmt: skip
    lines += ["", f"Bar: excess > 0 and Newey–West t > {T_CRIT:.3f} (Bonferroni, 3 variants). Deflated Sharpe with "
              f"N = {N_TRIALS} trials is reported, not gated.", "", "## Halves", "",
              "| Strategy | Half | Period | Excess CAGR vs EW | NW t |", "|---|---|---|---|---|"]  # fmt: skip
    for k, r in out["strategies"].items():
        for h, d in r["stats"]["halves"].items():
            lines.append(f"| {k} | {h} | {d['from']} → {d['to']} | {_pct(d['excess_cagr_vs_equal_weight'])} | "
                         f"{(d['newey_west_t'] or 0):.2f} |")  # fmt: skip
    jumps = {
        k: v["unexplained_jumps"] for k, v in out["price_coverage"].items() if v.get("unexplained_jumps")
    }
    b0 = out["strategies"]["B0"]["stats"]["cagr"]
    lines += ["", "## Limits", "",
              f"- Eligible member-months (≥ 252 clean days of history at the month end): {cov['eligible']} of "
              f"{cov['member_months']} ({cov['eligible'] / max(1, cov['member_months']) * 100:.1f} %).",
              f"- NIFTYBEES (the market-filter series and the price reference) CAGR over the same months: "
              f"{_pct(b0['nifty50_price'])}.",
              "- A price jump with no matching split/bonus in NSE's corporate actions is treated as a data error: "
              "returns across it are dropped for 252 days (v1's rule). That also drops some REAL moves, e.g. "
              "YESBANK in March 2020, from the strategy and the benchmark alike; it flatters the equal-weight "
              f"universe slightly. Symbols affected: {', '.join(f'{k} ({len(v)})' for k, v in sorted(jumps.items()))}.",
              "- Price returns without dividends; cash at 0 %; a monthly close-to-close trade with 5 bp slippage.",
              "- Delisted members (HDFC, CAIRN, RANBAXY, IDFC, TATAMTRDVR, JPASSOCIAT) drop out in the month their "
              "trading stops (their last partial month is not counted)."]  # fmt: skip
    lines += ["", "## Decision", "", out["decision"], ""]
    return "\n".join(lines)


def main() -> None:
    universe, _, coverage = load(DATA_DIR)
    market = universe.pop(MARKET, None)
    if market is None:
        raise SystemExit(f"{MARKET} history missing: run the harvest with --symbols {MARKET}")
    changes = load_changes()
    periods, problems = reconstruct(_current_list(), changes)
    results: dict[str, dict[str, Any]] = {}
    for k, sel in SELECTORS.items():
        r = run_pit(universe, market, lambda t: members_on(periods, t), sel)
        results[k] = {"stats": summarise(r["months"]), "months": r["months"], "coverage": r["coverage"]}
    today_list = _current_list()
    surv = run_pit(universe, market, lambda t: today_list, select_b0)
    results["B0_survivors"] = {"stats": summarise(surv["months"]), "months": surv["months"],
                               "coverage": surv["coverage"]}  # fmt: skip
    verdicts = evaluate(results)
    passing = [k for k in VARIANTS if verdicts[k]["passes"]]
    decision = (f"Ship {passing[0]} ({NAMES[passing[0]]}) per the pre-registered order." if passing else
                "No variant passed the bar: signals/stock.py stays rule_based with no edge vs the universe; only its "
                "survivorship caveat now cites this point-in-time rerun.")  # fmt: skip
    cov = results["B0"]["coverage"]
    with (OUT_DIR / "membership_monthly.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["month_end", "members", "eligible", "market_up"])
        for m in results["B0"]["months"]:
            w.writerow([m["month_end"], m["members"], m["universe"], m["market_up"]])
    out = {"generated": date.today().isoformat(), "prereg": "PREREG.md",
           "membership": {"changes": len(changes), "problems": problems,
                          "periods": [[d.isoformat() if d != date.min else "start", sorted(s)] for d, s in periods]},
           "coverage": cov, "price_coverage": coverage, "costs": {"buy": COST_BUY, "sell": COST_SELL},
           "t_critical": T_CRIT, "strategies": {k: {"name": NAMES[k], "stats": r["stats"]} for k, r in results.items()},
           "verdicts": verdicts, "passing": passing, "decision": decision,
           "monthly": {k: [{"m": m["month_end"], "s": round(m["strategy"], 6), "ew": round(m["equal_weight"], 6),
                            "held": m["held"]} for m in r["months"]] for k, r in results.items()}}  # fmt: skip
    (OUT_DIR / "results.json").write_text(json.dumps(out, indent=1, default=str) + "\n")
    (OUT_DIR / "RESULTS.md").write_text(render(out))
    print(render(out), file=sys.stderr)
    print(json.dumps({k: (v["excess_cagr_vs_equal_weight"], v["newey_west_t"]) for k, v in verdicts.items()}),
          file=sys.stderr)  # fmt: skip


if __name__ == "__main__":
    main()
