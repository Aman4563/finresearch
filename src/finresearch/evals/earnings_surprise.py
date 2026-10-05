"""The earnings-surprise experiment (evals/experiments/earnings_surprise/PREREG.md and ADDENDUM.md), run on the data
evals.earnings_harvest put on disk. Writes results.json and RESULTS.md next to the pre-registration.

    uv run python -m finresearch.evals.earnings_surprise
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch.config import REPO_ROOT
from finresearch.evals.earnings_harvest import DATA_DIR, MARKET
from finresearch.evals.stock_backtest import COST_BUY, COST_SELL
from finresearch.fincalc import surprise as S
from finresearch.fincalc.signals import adjust_for_actions

OUT_DIR = REPO_ROOT / "evals" / "experiments" / "earnings_surprise"
FIRST_SEASON, LAST_SEASON = date(2021, 9, 30), date(2026, 3, 31)  # PREREG: Sep-2021 .. Mar-2026 quarter ends
REACTION, DRIFT = (0, 1), (2, 60)
T_BAR = 1.96
ROUND_TRIP = COST_BUY + COST_SELL
LOOKBACK_DAYS, MIN_REFERENCE = 365, 100  # tradable breakpoint: SUEs of the year before t0, at least 100


def load(data_dir: Path = DATA_DIR) -> dict[str, dict[str, Any]]:
    return {p.stem: json.loads(p.read_text()) for p in sorted((data_dir / "stocks").glob("*.json"))}


def adjusted_closes(d: dict[str, Any]) -> tuple[dict[date, float], list[date]]:
    """Split/bonus back-adjusted closes and the dates of unexplained ±35 % one-day moves."""
    days = [date.fromisoformat(x[0]) for x in d["prices"]]
    acts = [(date.fromisoformat(a), s) for a, s in d.get("actions") or []]
    adj = adjust_for_actions(days, [x[1] for x in d["prices"]], acts)
    return dict(zip(adj.days, adj.close, strict=True)), list(adj.anomalies)


def _end_of(day: date) -> datetime:
    return datetime.combine(day, datetime.max.time().replace(microsecond=0), S.IST)


def quarters(d: dict[str, Any], field: str) -> tuple[list[S.Quarter], dict[date, dict[str, Any]]]:
    """The stock's quarters for `field` ("eps" | "revenue") and, per quarter end, its event info. A quarter whose
    broadcast is past the legal deadline (ADDENDUM 1) is known from the deadline and is not an event."""
    out: list[S.Quarter] = []
    info: dict[date, dict[str, Any]] = {}
    for q in d["quarters"]:
        end = date.fromisoformat(q["quarter_end"])
        start = date.fromisoformat(q["start"]) if q.get("start") else None
        if start is not None and not 80 <= (end - start).days <= 100:
            continue  # a half-year "current period" (half-yearly filers), not a quarter
        announced = datetime.fromisoformat(q["announced"])
        plausible = S.plausible_announcement(end, announced)
        known = announced if plausible else _end_of(S.results_deadline(end))
        filed = datetime.fromisoformat(q["filed"]) if plausible else known
        out.append(S.Quarter(end, known, q.get(field), filed))
        info[end] = {"announced": announced, "has_time": q.get("has_time", True), "plausible": plausible,
                     "revised_only": q.get("revised_only", False)}  # fmt: skip
    return out, info


def membership() -> Callable[[date], frozenset[str]]:
    """Point-in-time Nifty 50 members (data symbols) at the close of a day."""
    from finresearch.evals import stock_universe as U

    periods, problems = U.reconstruct(U._current_list(), U.load_changes())
    if problems:
        raise RuntimeError(f"membership log inconsistent: {problems[:3]}")
    return lambda t: frozenset(U.data_symbol(s) for s in U.members_on(periods, t))


def build_events(data: dict[str, dict[str, Any]], members: Callable[[date], frozenset[str]],
                 first: date = FIRST_SEASON, last: date = LAST_SEASON) -> tuple[list[dict[str, Any]], dict[str, int]]:  # fmt: skip
    """One row per (stock, quarter end in [first, last]) with a plausible announcement: t0, SUE (EPS and revenue)
    and the abnormal returns. `drops` counts why candidates did not become usable events."""
    market, _ = adjusted_closes(data[MARKET])
    stocks = {s: d for s, d in data.items() if s != MARKET}
    sessions = sorted({date.fromisoformat(x[0]) for d in stocks.values() for x in d["prices"]})
    drops: dict[str, int] = {}

    def drop(why: str) -> None:
        drops[why] = drops.get(why, 0) + 1

    events: list[dict[str, Any]] = []
    for sym, d in sorted(stocks.items()):
        closes, anomalies = adjusted_closes(d)
        acts = [(date.fromisoformat(a), s) for a, s in d.get("actions") or []]
        eps_q, info = quarters(d, "eps")
        rev_q, _ = quarters(d, "revenue")
        for end, inf in sorted(info.items()):
            if not first <= end <= last:
                continue
            if not inf["plausible"]:
                drop("broadcast after the legal deadline (re-upload)")
                continue
            t0 = S.event_session(inf["announced"], inf["has_time"], sessions)
            if t0 is None:
                drop("t0 beyond the price calendar")
                continue
            if sym not in members(t0):
                drop("not a Nifty 50 member at t0")
                continue
            sue, why = S.sue(eps_q, end, acts)
            if sue is None:
                drop(f"no EPS SUE: {why.split(' (')[0] if why else '?'}")
                continue
            rsue, _ = S.sue(rev_q, end, per_share=False)
            i = sessions.index(t0)
            span = sessions[max(0, i - 1): i + DRIFT[1] + 1]
            bad = any(span[0] <= a <= span[-1] for a in anomalies)
            reaction = None if bad else S.abnormal_return(closes, market, sessions, t0, *REACTION)
            drift = None if bad else S.abnormal_return(closes, market, sessions, t0, *DRIFT)
            events.append({"symbol": sym, "quarter_end": end.isoformat(), "announced": inf["announced"].isoformat(),
                           "t0": t0.isoformat(), "month": t0.strftime("%Y-%m"), "sue": sue, "revenue_sue": rsue,
                           "reaction": reaction, "drift": drift, "anomaly": bad,
                           "revised_only": inf["revised_only"]})  # fmt: skip
    return events, drops


def assign_season_deciles(events: list[dict[str, Any]], key: str = "sue", out: str = "decile") -> None:
    """Deciles of `key` within each season (quarter end), among every event with a value (PREREG test 1)."""
    by: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        if e.get(key) is not None:
            by.setdefault(e["quarter_end"], []).append(e)
    for es in by.values():
        for e, dec in zip(es, S.deciles([e[key] for e in es]), strict=True):
            e[out] = dec


def assign_tradable(events: list[dict[str, Any]]) -> None:
    """Walk-forward breakpoint (PREREG test 2): the 90th percentile (statistics.quantiles, exclusive method) of every
    SUE with t0 in the 365 days strictly before this t0; no trade with fewer than 100 such SUEs."""
    for e in events:
        t0 = date.fromisoformat(e["t0"])
        lo = (t0 - timedelta(days=LOOKBACK_DAYS)).isoformat()
        ref = [x["sue"] for x in events if lo <= x["t0"] < e["t0"]]
        e["breakpoint"] = statistics.quantiles(ref, n=10)[8] if len(ref) >= MIN_REFERENCE else None
        e["trade"] = e["breakpoint"] is not None and e["sue"] >= e["breakpoint"]
