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
from finresearch.evals.timing import Feature, check
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
            # point-in-time invariant (#244): the surprise is known from the broadcast, by the close of the event
            # session t0 (the drift trade enters at the next close)
            check([Feature("sue", inf["announced"], "NSE broadcast time")],
                  datetime.combine(t0, S.MARKET_CLOSE, S.IST), context=f"{sym} {end}")  # fmt: skip
            sue, why = S.sue(eps_q, end, acts)
            if sue is None:
                drop(f"no EPS SUE: {why.split(' (')[0] if why else '?'}")
                continue
            rsue, _ = S.sue(rev_q, end, per_share=False)
            i = sessions.index(t0)
            span = sessions[max(0, i - 1) : i + DRIFT[1] + 1]
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


def _est(e: S.Estimate | None) -> dict[str, Any] | None:
    return None if e is None else {"value": e.value, "se": e.se, "t": e.t, "n": e.n, "clusters": e.clusters}


def spread(events: list[dict[str, Any]], dec: str = "decile", y: str = "drift") -> S.Estimate | None:
    """mean y of the top decile minus the bottom decile, clustered by the month of t0."""
    hi = [e for e in events if e.get(dec) == 10 and e.get(y) is not None]
    lo = [e for e in events if e.get(dec) == 1 and e.get(y) is not None]
    if len(hi) < 2 or len(lo) < 2:
        return None
    return S.clustered_diff(
        [e[y] for e in hi], [e["month"] for e in hi], [e[y] for e in lo], [e["month"] for e in lo]
    )


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def evaluate(events: list[dict[str, Any]]) -> dict[str, Any]:
    assign_season_deciles(events)
    assign_season_deciles(events, "revenue_sue", "revenue_decile")
    assign_tradable(events)
    seasons = sorted({e["quarter_end"] for e in events})
    halves = {"first": set(seasons[: len(seasons) // 2]), "second": set(seasons[len(seasons) // 2 :])}
    primary = spread(events)
    by_half = {h: spread([e for e in events if e["quarter_end"] in ss]) for h, ss in halves.items()}
    trades = [e for e in events if e["trade"] and e["drift"] is not None]
    for e in trades:
        e["net"] = e["drift"] - ROUND_TRIP
    tradable = (
        S.clustered_mean([e["net"] for e in trades], [e["month"] for e in trades])
        if len(trades) > 1
        else None
    )
    trade_halves = {
        h: _mean([e["net"] for e in trades if e["quarter_end"] in ss]) for h, ss in halves.items()
    }
    primary_pass = bool(primary and primary.value > 0 and primary.t is not None and primary.t > T_BAR)
    tradable_pass = bool(tradable and tradable.value > 0 and tradable.t is not None and tradable.t > T_BAR
                         and all(v is not None and v > 0 for v in trade_halves.values()))  # fmt: skip
    deciles = []
    for k in range(1, 11):
        es = [e for e in events if e.get("decile") == k]
        deciles.append({"decile": k, "n": len(es),
                        "mean_sue": _mean([e["sue"] for e in es]),
                        "drift": _mean([e["drift"] for e in es if e["drift"] is not None]),
                        "reaction": _mean([e["reaction"] for e in es if e["reaction"] is not None])})  # fmt: skip
    sues = sorted(e["sue"] for e in events)
    return {
        "seasons": seasons, "halves": {h: sorted(ss) for h, ss in halves.items()},
        "n_events": len(events), "n_with_drift": sum(e["drift"] is not None for e in events),
        "n_stocks": len({e["symbol"] for e in events}),
        "primary": _est(primary), "primary_by_half": {h: _est(v) for h, v in by_half.items()},
        "reaction_spread": _est(spread(events, y="reaction")),
        "revenue_spread": _est(spread(events, "revenue_decile")),
        "tradable": _est(tradable), "tradable_by_half": trade_halves, "n_trades": len(trades),
        "round_trip_cost": ROUND_TRIP, "deciles": deciles,
        "passes": {"primary": primary_pass, "tradable": tradable_pass, "promote": primary_pass and tradable_pass},
        "reference": {"n": len(sues), "cutoffs": statistics.quantiles(sues, n=10) if len(sues) >= 10 else [],
                      "period": f"{seasons[0]}..{seasons[-1]}" if seasons else None,
                      "universe": "point-in-time Nifty 50"},
    }  # fmt: skip


def _pp(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.2f} pp"


def _t(e: dict[str, Any] | None) -> str:
    return "—" if not e or e["t"] is None else f"{e['t']:.2f}"


def render(out: dict[str, Any]) -> str:
    r = out["results"]
    cov = out["coverage"]
    p, tr = r["primary"], r["tradable"]
    lines = [
        "# Earnings surprise (SUE) and post-announcement drift: results", "",
        f"Generated {out['generated']} by `finresearch.evals.earnings_surprise`. Pre-registration: `PREREG.md`; "
        "changes made before any outcome was computed: `ADDENDUM.md` (re-uploaded broadcast dates; NIFTYBEES as the "
        "benchmark).", "",
        f"**Verdict: {'PASSES' if r['passes']['promote'] else 'does not pass'}.** Primary test "
        f"{'passes' if r['passes']['primary'] else 'fails'}; tradable test "
        f"{'passes' if r['passes']['tradable'] else 'fails'}. "
        + ("SUE may enter the stock signal in a separate PR." if r["passes"]["promote"] else
           "SUE stays informational (\"experimental — not part of the signal\"); signals/stock.py is unchanged."), "",
        "## Coverage", "",
        f"- Stocks harvested: {cov['harvested']} of {cov['universe']} point-in-time Nifty 50 members since Apr-2021 "
        f"(not harvested: {', '.join(cov['missing']) or 'none'}). Fewer than the 13 quarters a SUE needs (quarters "
        "read): " + (", ".join(f"{k} {v}" for k, v in sorted(cov.get("thin", {}).items())) or "none") + ".",
        f"- Events with an EPS SUE: {r['n_events']} from {r['n_stocks']} stocks over {len(r['seasons'])} seasons "
        f"({r['seasons'][0] if r['seasons'] else '—'} to {r['seasons'][-1] if r['seasons'] else '—'}); "
        f"with a complete [+2,+60] window: {r['n_with_drift']}.",
        "- Candidates dropped: " + "; ".join(f"{k}: {v}" for k, v in sorted(out["drops"].items())) + ".", "",
        "## Tests (bar: t > 1.96)", "",
        "| Test | Estimate | Clustered SE | t | Events | Months | Passes |", "|---|---|---|---|---|---|---|",
    ]  # fmt: skip
    for name, e, ok in (("Primary: drift [+2,+60] D10 - D1 (gross)", p, r["passes"]["primary"]),
                        ("Tradable: long top decile, net of round trip", tr, r["passes"]["tradable"])):  # fmt: skip
        lines.append(f"| {name} | {_pp(e and e['value'])} | {_pp(e and e['se'])} | {_t(e)} | "
                     f"{e['n'] if e else 0} | {e['clusters'] if e else 0} | {'yes' if ok else 'no'} |")  # fmt: skip
    lines += ["", f"Round trip cost: {r['round_trip_cost'] * 100:.3f} % (evals.stock_backtest COST_BUY + COST_SELL). "
              f"Tradable trades: {r['n_trades']}; mean net by half: "
              + ", ".join(f"{h} {_pp(v)}" for h, v in r["tradable_by_half"].items()) + ".", "",
              "## Descriptive (not gated)", "",
              "| Half | Seasons | D10 - D1 drift | t |", "|---|---|---|---|"]  # fmt: skip
    for h, e in r["primary_by_half"].items():
        ss = r["halves"][h]
        lines.append(
            f"| {h} | {ss[0] if ss else '—'} to {ss[-1] if ss else '—'} | {_pp(e and e['value'])} | {_t(e)} |"
        )
    rs, vs = r["reaction_spread"], r["revenue_spread"]
    lines += ["", f"- [0,+1] reaction D10 - D1: {_pp(rs and rs['value'])} (t {_t(rs)}).",
              f"- Revenue SUE, drift D10 - D1: {_pp(vs and vs['value'])} (t {_t(vs)}).", "",
              "| SUE decile | Events | Mean SUE | Mean [0,+1] | Mean [+2,+60] |", "|---|---|---|---|---|"]  # fmt: skip
    for d in r["deciles"]:
        ms = "—" if d["mean_sue"] is None else f"{d['mean_sue']:+.2f}"
        lines.append(f"| {d['decile']} | {d['n']} | {ms} | {_pp(d['reaction'])} | {_pp(d['drift'])} |")
    lines += ["", "## Reading (written after the run; post hoc, not a test)", "",
              "- A null at this power: the clustered SE of D10 - D1 is about 1.2 pp, so a drift of 2-3 pp could exist "
              "and not be detected. The two halves point in opposite directions (-3.0 pp, t -1.64; +3.0 pp, t 2.18); "
              "the second half alone is not a pre-registered test and one half of two clearing 1.96 is expected by "
              "chance about one time in ten.",
              "- The decile means are not monotonic in SUE for either window, and the [0,+1] reaction spread is "
              "small (+0.5 pp): for Nifty 50 names the seasonal random walk is a weak proxy for the news the market "
              "trades on (no consensus data)."]  # fmt: skip
    lines += ["", "## Limits", ""] + [f"- {x}" for x in out["limits"]]
    return "\n".join(lines) + "\n"


LIMITS = [
    "Universe: the point-in-time Nifty 50 only (large caps, about 50 events a season, so a decile is about five "
    "stocks). Post-earnings drift is usually reported to be stronger in small caps; NIFTY 500 was not harvested.",
    "History: NSE serves results XBRL from about the Sep-2018 quarter (older filings are HTML and were not parsed), "
    "so the sample is about 19 seasons and the test has low power (PREREG: minimum detectable effect ≈ 4-6 pp).",
    "SUE is a seasonal random walk on basic EPS as first reported; no analyst consensus is available. One-off items "
    "(exceptional gains, impairments) are inside EPS and count as surprise.",
    "Announcement time is NSE's broadcast time. A re-upload past the legal deadline is detected and dropped "
    "(ADDENDUM 1); one inside the deadline is not detectable and would make t0 late.",
    "Returns are market-adjusted (no beta) price returns; dividends are excluded from stocks while NIFTYBEES tracks "
    "the index's total return (ADDENDUM 2), a ≈ 0.3 pp drag on every 60-session window that cancels in D10 - D1.",
    "Standard errors are clustered by the calendar month of t0; windows that overlap across months are only "
    "partly covered by that clustering.",
]


def main() -> None:
    from finresearch.evals.earnings_harvest import universe

    data = load()
    if MARKET not in data:
        raise SystemExit(
            f"{MARKET} (the benchmark) is not harvested yet: run finresearch.evals.earnings_harvest"
        )
    events, drops = build_events(data, membership())
    results = evaluate(events)
    uni = [s for s in universe() if s != MARKET]
    out = {
        "generated": date.today().isoformat(),
        "coverage": {"universe": len(uni), "harvested": sum(s in data for s in uni),
                     "missing": [s for s in uni if s not in data],
                     "thin": {s: len(data[s]["quarters"]) for s in uni if s in data and len(data[s]["quarters"]) < 13}},
        "drops": drops, "results": results, "limits": LIMITS,
        "events": [{k: (round(v, 6) if isinstance(v, float) else v) for k, v in e.items()} for e in events],
    }  # fmt: skip
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "results.json").write_text(json.dumps(out, indent=1) + "\n")
    (OUT_DIR / "RESULTS.md").write_text(render(out))
    print((OUT_DIR / "RESULTS.md").read_text()[:1500])


if __name__ == "__main__":
    main()
