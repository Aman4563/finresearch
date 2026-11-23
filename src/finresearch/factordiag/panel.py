"""The panel of the stock factor diagnostics (#506, evals/experiments/stock_factor_diagnostics/PREREG.md): each
component's score as of every decision date and the forward returns after it, from in-memory price series
(stock_backtest.Series) and a point-in-time membership function. This module opens no file: the command's loader
(study.load_inputs) hands it the series, and tests hand it generated ones.

Nothing after the decision date t enters a score, and nothing before t enters a forward return: `audit` recomputes
both on altered series and refuses (LookAheadError) when either rule is broken. Unknown is never 0: a return across a
gap or an unexplained jump is absent and counted; a stock that stopped trading is held in cash from its last close
and counted as `ended`.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date

from finresearch.evals.stock_backtest import Series, month_ends, state_at
from finresearch.evals.stock_universe import data_symbol, ranks
from finresearch.factordiag import specs
from finresearch.fincalc.signals import Adjusted, sma

Window = tuple[int, int]  # (a, b]: from the month-end a months after t to the one b months after it
WINDOWS: tuple[Window, ...] = tuple(dict.fromkeys([*((0, h) for h in specs.HORIZONS), *specs.SEGMENTS]))
Inputs = dict[str, float]
InputsFn = Callable[[Series, int], Inputs | None]
ReturnFn = Callable[[Series, date, date, date], tuple[float | None, str | None]]
Members = Callable[[date], Iterable[str]]


class LookAheadError(RuntimeError):
    pass


@dataclass
class Month:
    t: date
    members: int
    eligible: int
    scores: dict[str, dict[str, float]]  # component -> data symbol -> score (every eligible name)
    # window -> data symbol -> known forward return; a window whose end is beyond the calendar has no entry
    returns: dict[Window, dict[str, float]] = field(default_factory=dict)
    # window -> how many names' returns were flagged ended, gap or jump
    flags: dict[Window, dict[str, int]] = field(default_factory=dict)


@dataclass
class Panel:
    months: list[Month]
    ended: dict[str, int]  # data symbol -> decision dates on which one of its prices was its last close
    missing: dict[str, int]  # member symbol -> member-months without any price series


# --------------------------------------------------------------------------------------------- scores at t
def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


def name_inputs(s: Series, i: int) -> Inputs | None:
    """One name's inputs at bar i from closes up to and including it, or None when the base experiment's rule
    (stock_backtest.state_at: WARMUP bars, no unexplained jump among them) leaves it ineligible."""
    st = state_at(s, i)
    if st is None:
        return None
    c = s.adj.close[: i + 1]
    ma = sma(c, specs.TREND_WINDOW)
    if not ma:
        return None
    return {"mom_score": st.score, "trend_distance": c[-1] / ma - 1, "vol": st.vol}


def component_scores(inputs: dict[str, Inputs]) -> dict[str, dict[str, float]]:
    """The five registered scores over the names eligible at one date (higher = better)."""
    mom = {n: x["mom_score"] for n, x in inputs.items()}
    vol = {n: x["vol"] for n, x in inputs.items()}
    trend = {n: x["trend_distance"] for n, x in inputs.items()}
    # the base experiment's rank routine: 1 = best, ties share the average rank
    rm, rv = ranks(mom, descending=True), ranks(vol, descending=False)
    return {
        "mom_score": mom,
        "trend_distance": trend,
        "low_vol": {n: -v for n, v in vol.items()},
        "mom_lowvol_rank": {n: -(rm[n] + rv[n]) / 2 for n in inputs},
        "composite_price": {n: specs.MOM_POINTS * _clip(mom[n] / specs.MOM_SCALE)
                            + specs.TREND_POINTS * _clip(trend[n] / specs.TREND_SCALE) for n in inputs},
    }  # fmt: skip


# --------------------------------------------------------------------------------------------- returns after t
def price_at(s: Series, d: date) -> tuple[int | None, str | None]:
    """(bar index, flag) of the price at month-end d: the bar on d or the last one within PRICE_WINDOW_DAYS before
    it; a series with no such bar and none after d has stopped trading and gives its last bar, flagged `ended`; one
    that resumes after d gives (None, `gap`)."""
    days = s.adj.days
    i = bisect_right(days, d) - 1
    if i >= 0 and (d - days[i]).days <= specs.PRICE_WINDOW_DAYS:
        return i, None
    if i >= 0 and i == len(days) - 1:
        return i, "ended"
    return None, "gap"


def forward_return(s: Series, t: date, ta: date, tb: date) -> tuple[float | None, str | None]:
    """(R, flag): R = P(tb) / P(ta) - 1 for a name eligible at t, with P(t) the close on t itself. R is None with
    flag `gap` or `jump` when unknown; flag `ended` marks a known return that used a last close."""
    ia, fa = (s.index_of.get(t), None) if ta == t else price_at(s, ta)
    ib, fb = price_at(s, tb)
    if ia is None or ib is None:
        return None, "gap"
    if any(s.adj.days[ia] < d <= s.adj.days[ib] for d in s.adj.anomalies):
        return None, "jump"
    return s.adj.close[ib] / s.adj.close[ia] - 1, ("ended" if "ended" in (fa, fb) else None)


# --------------------------------------------------------------------------------------------- the panel
def _eligible(universe: dict[str, Series], members: Iterable[str], t: date, inputs_fn: InputsFn,
              missing: Counter | None = None) -> tuple[dict[str, Inputs], dict[str, Series]]:  # fmt: skip
    """Inputs and series, by data symbol, of the members at t that are eligible there."""
    inputs: dict[str, Inputs] = {}
    series: dict[str, Series] = {}
    for sym in sorted(members):
        ds = data_symbol(sym)
        s = universe.get(ds)
        if s is None or s.adj.days[0] > t:
            if missing is not None:
                missing[sym] += 1
            continue
        i = s.index_of.get(t)
        x = inputs_fn(s, i) if i is not None else None
        if x is not None:
            inputs[ds], series[ds] = x, s
    return inputs, series


def build(universe: dict[str, Series], market: Series, members: Members, *, inputs_fn: InputsFn = name_inputs,
          return_fn: ReturnFn = forward_return) -> Panel:  # fmt: skip
    """The panel over every month-end of the market calendar. `members(t)` gives the point-in-time symbols at t."""
    ends = month_ends(market.adj.days)
    months: list[Month] = []
    ended: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for k, t in enumerate(ends):
        mem = list(members(t))
        inputs, series = _eligible(universe, mem, t, inputs_fn, missing)
        month = Month(t, len(mem), len(inputs), component_scores(inputs))
        ended_here: set[str] = set()
        for a, b in WINDOWS:
            if k + b >= len(ends):
                continue  # the window ends beyond the calendar: not evaluated
            known: dict[str, float] = {}
            flags: Counter[str] = Counter()
            for ds, s in series.items():
                r, flag = return_fn(s, t, ends[k + a], ends[k + b])
                if flag:
                    flags[flag] += 1
                if flag == "ended":
                    ended_here.add(ds)
                if r is not None:
                    known[ds] = r
            month.returns[a, b], month.flags[a, b] = known, dict(flags)
        ended.update(ended_here)
        months.append(month)
    return Panel(months, dict(ended), dict(missing))


# --------------------------------------------------------------------------------------------- the look-ahead audit
def _cut(s: Series, i: int) -> Series:
    """The series as it stood at the close of bar i: nothing after it."""
    a, t = s.adj, s.adj.days[i]
    return Series(s.symbol, Adjusted(a.days[: i + 1], a.close[: i + 1], a.high[: i + 1], a.low[: i + 1],
                                     [x for x in a.applied if x[0] <= t], [d for d in a.anomalies if d <= t]))  # fmt: skip


def _past_doubled(s: Series, i: int) -> Series:
    """The series with every close before bar i doubled: a forward return from bar i on must not notice."""
    a = s.adj
    return Series(s.symbol, Adjusted(a.days, [c * 2 if j < i else c for j, c in enumerate(a.close)], a.high, a.low,
                                     a.applied, a.anomalies))  # fmt: skip


def audit(universe: dict[str, Series], market: Series, members: Members, panel: Panel, *,
          inputs_fn: InputsFn = name_inputs, return_fn: ReturnFn = forward_return) -> dict[str, int | bool]:  # fmt: skip
    """The registered look-ahead audit on every AUDIT_EVERY-th decision date and the last: every score recomputed
    from the series cut at t must equal the panel's, and every forward return recomputed after doubling each close
    before t must equal the panel's. LookAheadError on the first mismatch."""
    ends = month_ends(market.adj.days)
    picks = sorted({*range(0, len(ends), specs.AUDIT_EVERY), len(ends) - 1}) if ends else []
    n_scores = n_returns = 0
    for k in picks:
        t, month = ends[k], panel.months[k]
        cut = {ds: _cut(s, s.index_of[t]) for ds, s in universe.items() if t in s.index_of}
        try:
            inputs, _ = _eligible(cut, members(t), t, inputs_fn)
        except IndexError as e:
            raise LookAheadError(f"{t}: a score read a bar after the decision date ({e})") from e
        if component_scores(inputs) != month.scores:
            raise LookAheadError(f"{t}: a score changes when the bars after the decision date are removed")
        n_scores += sum(len(v) for v in month.scores.values())
        for ds in month.scores["mom_score"]:
            changed = _past_doubled(universe[ds], universe[ds].index_of[t])
            for (a, b), known in month.returns.items():
                r, _ = return_fn(changed, t, ends[k + a], ends[k + b])
                if r != known.get(ds):
                    raise LookAheadError(f"{t}: the forward return of {ds} over ({a}, {b}] months changes when the "
                                         "closes before the decision date are changed")  # fmt: skip
                n_returns += 1
    return {"dates": len(picks), "score_checks": n_scores, "return_checks": n_returns, "passed": True}
