"""The stock factor diagnostics' study (#506, evals/experiments/stock_factor_diagnostics/PREREG.md): every registered
statistic from a panel (factordiag.panel), the two Benjamini-Hochberg families and the registered reading of each
outcome. `execute` is the owner's one-shot command: it checks the registration, reads the price cache and the two
pinned membership files, runs the look-ahead audit and writes results.json and RESULTS.md. Nothing here opens a
database or the network, and nothing changes a signal, a call or a card.

Unknown stays unknown: a month with too few names, a cell with too few months or blocks, a spread that is not
positive (no break-even cost) are None with the reason and the count, never 0.
"""

from __future__ import annotations

import csv
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from finresearch.evals import stock_backtest as bt
from finresearch.evals import stock_universe as su
from finresearch.factordiag import prereg, specs, stats
from finresearch.factordiag.panel import Members, Panel, Window

COMPONENTS = tuple(specs.COMPONENTS)
FAMILY_SIZE = {
    "A": len(COMPONENTS) * len(specs.HORIZONS),
    "B": len(COMPONENTS) * len(specs.FAMILY_B_HORIZONS),
}
RESULTS_JSON, RESULTS_MD = "results.json", "RESULTS.md"
SHORT_HORIZONS = (1, 3)  # the registered wording of the `short_lived` reading: "detected at 1 or 3 months"


class Refused(RuntimeError):
    pass


# --------------------------------------------------------------------------------------------- inputs (the only reads)
def load_membership(root: Path) -> Members:
    """Point-in-time Nifty 50 membership from the two pinned files of the base experiment."""
    with (root / specs.CONSTITUENTS_FILE).open() as f:
        listed = [r["Symbol"].strip() for r in csv.DictReader(f)]
    old_name = {v: k for k, v in su.DATA_SYMBOL.items()}  # the change log uses the symbols of the day
    periods, problems = su.reconstruct(
        [old_name.get(s, s) for s in listed], su.load_changes(root / specs.CHANGES_FILE)
    )
    if problems:
        raise Refused(f"the membership log is inconsistent: {problems[:3]}")
    return lambda t: su.members_on(periods, t)


def load_inputs(
    data_dir: Path, root: Path
) -> tuple[dict[str, bt.Series], bt.Series, Members, dict[str, Any]]:
    """The price series of the cache (stock_backtest.load), the market series and the membership function."""
    if not (data_dir / "prices").is_dir():
        raise Refused(f"no price cache at {data_dir}: run the command from the checkout that holds it, or pass "
                      "--data-dir")  # fmt: skip
    universe, _, coverage = bt.load(data_dir)
    market = universe.pop(su.MARKET, None)
    if market is None:
        raise Refused(f"{su.MARKET} history is missing from {data_dir}: it is the calendar")
    jumps = {k: len(v["unexplained_jumps"]) for k, v in coverage.items() if v.get("unexplained_jumps")}
    summary = {"symbols": len(universe), "calendar_from": market.adj.days[0].isoformat(),
               "calendar_to": market.adj.days[-1].isoformat(), "symbols_with_unexplained_jumps": len(jumps),
               "unexplained_jumps": sum(jumps.values())}  # fmt: skip
    return universe, market, load_membership(root), summary


# --------------------------------------------------------------------------------------------- one series
def series_stats(values: Sequence[float], h: int) -> dict[str, Any]:
    """The registered summary, Newey-West t and block interval of one monthly series at horizon h (its lag and its
    block); everything None, with the reason, below MIN_MONTHS values."""
    n, size = len(values), specs.BLOCK_MONTHS[h]
    out: dict[str, Any] = {"months": n, "unknown": None, "mean": None, "sd": None, "icir": None,
                           "share_positive": None, "newey_west": {"lag": specs.NW_LAG[h], "se": None, "t": None},
                           "interval": {"blocks": len(stats.blocks(n, size)), "block": size, "se": None, "lo": None,
                                        "hi": None, "mde": None}}  # fmt: skip
    if n < specs.MIN_MONTHS:
        out["unknown"] = f"only {n} evaluated months (needs {specs.MIN_MONTHS})"
        return out
    s = stats.summary(values)
    out.update(mean=s["mean"], sd=s["sd"], icir=s["icir"], share_positive=s["share_positive"],
               newey_west=stats.newey_west(values, specs.NW_LAG[h]),
               interval=stats.block_interval(values, size))  # fmt: skip
    return out


def ic_months(panel: Panel, component: str, window: Window) -> dict[str, Any]:
    """The monthly rank ICs of one component against one window's forward returns, over the months the window is
    evaluated; a month with fewer than MIN_NAMES names, or a constant side, is counted as unknown."""
    dates: list[date] = []
    ics: list[float] = []
    names: list[int] = []
    unknown: Counter[str] = Counter()
    for m in panel.months:
        r = m.returns.get(window)
        if r is None:
            continue
        s = m.scores[component]
        both = sorted(set(s) & set(r))
        if len(both) < specs.MIN_NAMES:
            unknown["too_few_names"] += 1
            continue
        ic = stats.spearman([s[n] for n in both], [r[n] for n in both])
        if ic is None:
            unknown["constant"] += 1
            continue
        dates.append(m.t)
        ics.append(ic)
        names.append(len(both))
    return {"dates": dates, "ic": ics, "names": names, "unknown_months": dict(unknown)}


def _range(xs: Sequence[int]) -> dict[str, float] | None:
    return {"min": min(xs), "median": statistics.median(xs), "max": max(xs)} if xs else None


def _flags(panel: Panel, window: Window) -> dict[str, int]:
    """Name-dates of one window whose return used a last close (ended) or is unknown (gap, jump)."""
    out: Counter[str] = Counter()
    for m in panel.months:
        out.update(m.flags.get(window, {}))
    return {k: out.get(k, 0) for k in ("ended", "gap", "jump")}


def ic_cell(panel: Panel, component: str, h: int) -> dict[str, Any]:
    """One (component, horizon) cell: the IC summary, family A's p, the two sub-periods and, at family B's horizons,
    the period difference. q-values are added by `run` once the whole family is known."""
    x = ic_months(panel, component, (0, h))
    dates, ic = x["dates"], x["ic"]
    size = specs.BLOCK_MONTHS[h]
    cell = series_stats(ic, h)
    known = cell["unknown"] is None
    cell.update(
        horizon=h, names=_range(x["names"]), unknown_months=x["unknown_months"], flags=_flags(panel, (0, h)),
        sign_flip=stats.sign_flip_p(ic, size) if known else stats.sign_flip_p([], size), q=None, detected=None,
        series=[[d.isoformat(), round(v, 6)] for d, v in zip(dates, ic, strict=True)],
    )  # fmt: skip
    first = [v for d, v in zip(dates, ic, strict=True) if d < specs.SPLIT]
    second = [v for d, v in zip(dates, ic, strict=True) if d >= specs.SPLIT]
    cell["sub_periods"] = {"split": specs.SPLIT.isoformat(), "first": series_stats(first, h),
                           "second": series_stats(second, h)}  # fmt: skip
    if h in specs.FAMILY_B_HORIZONS:
        enough = min(len(first), len(second)) >= specs.MIN_MONTHS
        diff = stats.period_permutation_p(first, second, size)
        if not enough:
            diff["p"] = None  # a period below MIN_MONTHS is unknown, whatever its blocks
        cell["period_difference"] = {**diff, "q": None, "detected": None}
    return cell


def decay_cell(panel: Panel, component: str, segment: Window) -> dict[str, Any]:
    """The IC against the return over one segment (a, b] months ahead, with horizon b's lag and blocks."""
    x = ic_months(panel, component, segment)
    return {
        **series_stats(x["ic"], segment[1]),
        "segment": list(segment),
        "unknown_months": x["unknown_months"],
    }


# --------------------------------------------------------------------------------------------- quintiles and costs
def quintile_stats(panel: Panel, component: str, h: int) -> dict[str, Any]:
    """Mean forward return per score quintile, the two spreads and the monotonicity measure at horizon h."""
    rows: list[list[float]] = []
    universe: list[float] = []
    dates: list[date] = []
    unknown: Counter[str] = Counter()
    for m in panel.months:
        r = m.returns.get((0, h))
        if r is None:
            continue
        s = m.scores[component]
        both = sorted(set(s) & set(r))
        if len(both) < specs.MIN_NAMES:
            unknown["too_few_names"] += 1
            continue
        q, rv = stats.quintiles([s[n] for n in both]), np.array([r[n] for n in both])
        if any(not np.any(q == k) for k in range(1, specs.QUANTILES + 1)):
            unknown["empty_quintile"] += 1
            continue
        rows.append([float(rv[q == k].mean()) for k in range(1, specs.QUANTILES + 1)])
        universe.append(float(rv.mean()))
        dates.append(m.t)
    out: dict[str, Any] = {"months": len(rows), "unknown_months": dict(unknown), "unknown": None, "mean_return": None,
                           "universe_mean": None, "top_minus_bottom": None, "top_minus_universe": None,
                           "monotonicity": None}  # fmt: skip
    if len(rows) < specs.MIN_MONTHS:
        out["unknown"] = f"only {len(rows)} evaluated months (needs {specs.MIN_MONTHS})"
        return out
    qm, um = np.array(rows), np.array(universe)
    means = qm.mean(axis=0)

    def spread(v: np.ndarray) -> dict[str, Any]:
        """The registered summary of one spread series, with the dated monthly series itself."""
        s = {k: x for k, x in series_stats(list(v), h).items() if k != "icir"}
        s["series"] = [[d.isoformat(), round(float(x), 6)] for d, x in zip(dates, v, strict=True)]
        return s

    out.update(
        mean_return=[float(x) for x in means], universe_mean=float(um.mean()),
        top_minus_bottom=spread(qm[:, -1] - qm[:, 0]), top_minus_universe=spread(qm[:, -1] - um),
        monotonicity={"rho": stats.spearman(list(range(1, specs.QUANTILES + 1)), list(means)),
                      "steps_up": int(np.sum(np.diff(means) > 0))},
    )  # fmt: skip
    return out


def turnover(panel: Panel, component: str, h: int) -> dict[str, Any]:
    """Mean one-way turnover of the top and bottom score quintiles between t and the month-end h months later: the
    share of the quintile at the later date that was not in it at t. Quintiles over every eligible name (no return
    needed); a pair of dates counts when both have MIN_NAMES names."""
    sets: list[dict[int, set[str]] | None] = []
    for m in panel.months:
        s = m.scores[component]
        if len(s) < specs.MIN_NAMES:
            sets.append(None)
            continue
        names = sorted(s)
        q = stats.quintiles([s[n] for n in names])
        sets.append({k: {n for n, v in zip(names, q, strict=True) if v == k} for k in (1, specs.QUANTILES)})
    tau: dict[int, list[float]] = {1: [], specs.QUANTILES: []}
    for a, b in zip(sets, sets[h:], strict=False):
        if a is None or b is None:
            continue
        for k, v in tau.items():
            if b[k]:
                v.append(1 - len(a[k] & b[k]) / len(b[k]))
    top, bottom = tau[specs.QUANTILES], tau[1]
    return {"top": float(np.mean(top)) if top else None, "bottom": float(np.mean(bottom)) if bottom else None,
            "pairs": len(top)}  # fmt: skip


def break_even(mean_spread: float | None, one_way_turnover: float | None) -> dict[str, Any]:
    """The cost per side at which the mean gross spread is zero: spread / (2 x turnover). Unknown, with the reason,
    when the spread is unknown or not positive, or the turnover is unknown or 0."""
    if mean_spread is None or one_way_turnover is None:
        return {"cost": None, "reason": "the spread or the turnover is unknown"}
    if mean_spread <= 0:
        return {"cost": None, "reason": "the mean gross spread is not positive: no cost level makes it zero"}
    if one_way_turnover <= 0:
        return {"cost": None, "reason": "the turnover is 0: costs never reach the spread"}
    return {"cost": mean_spread / (2 * one_way_turnover), "reason": None}


# --------------------------------------------------------------------------------------------- the registered reading
def _note(label: str, horizon: int | None = None, why: str | None = None) -> dict[str, Any]:
    return {"label": label, "horizon": horizon, "why": why, "text": specs.READING[label]}


def reading(comp: dict[str, Any], per_side_cost: float) -> list[dict[str, Any]]:
    """The registered reading of one component's outcome (specs.READING): one note per detected horizon, or
    `none_detected`, plus `short_lived` and `period_difference` where their registered conditions hold. A cell that
    is unknown gets no reading beyond `unknown`."""
    cells = {int(h): c for h, c in comp["horizons"].items()}
    known = {h: c for h, c in cells.items() if c["q"] is not None}
    notes = [_note("unknown", h, c["unknown"] or f"{c['sign_flip']['blocks']} blocks: no p")
             for h, c in cells.items() if c["q"] is None]  # fmt: skip
    detected = [h for h, c in known.items() if c["detected"]]
    if known and not detected:
        notes.append(_note("none_detected"))
    for h in detected:
        c = cells[h]
        if c["mean"] < 0:
            notes.append(_note("detected_negative", h))
            continue
        spread = c["quintiles"]["top_minus_universe"]
        lo = spread["interval"]["lo"] if spread else None
        cost = c["break_even"]["long_only"]["cost"]
        if lo is None:
            notes.append(_note("unknown", h, "detected, but the long-only spread's interval is unknown"))
        elif lo <= 0:
            notes.append(_note("not_captured", h))
        elif cost is None:
            notes.append(_note("unknown", h, "detected, but the break-even cost is unknown: "
                                             f"{c['break_even']['long_only']['reason']}"))  # fmt: skip
        else:
            notes.append(_note("eaten_by_costs" if cost < per_side_cost else "disagrees", h))
    last = max(specs.HORIZONS)
    tail = comp["decay"][_key(specs.SEGMENTS[-1])]["interval"]
    if (any(h in detected for h in SHORT_HORIZONS) and last in known and last not in detected
            and tail["lo"] is not None and tail["lo"] <= 0 <= tail["hi"]):  # fmt: skip
        notes.append(_note("short_lived"))
    notes += [_note("period_difference", h) for h, c in cells.items()
              if (c.get("period_difference") or {}).get("detected")]  # fmt: skip
    return notes


def _key(window: Window) -> str:
    return f"{window[0]}-{window[1]}"


# --------------------------------------------------------------------------------------------- the whole study
def run(
    panel: Panel, audit: dict[str, Any] | None = None, coverage: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Every registered statistic of the panel, the two families' q-values and each component's reading."""
    from finresearch.fincalc.dates import today_ist

    per_side = (bt.COST_BUY + bt.COST_SELL) / 2
    comps: dict[str, Any] = {}
    for name in COMPONENTS:
        cells = {}
        for h in specs.HORIZONS:
            cell = ic_cell(panel, name, h)
            q, tau = quintile_stats(panel, name, h), turnover(panel, name, h)
            both = None if tau["top"] is None or tau["bottom"] is None else tau["top"] + tau["bottom"]
            cell.update(quintiles=q, turnover=tau, break_even={
                "long_only": break_even((q["top_minus_universe"] or {}).get("mean"), tau["top"]),
                "long_short": break_even((q["top_minus_bottom"] or {}).get("mean"), both)})  # fmt: skip
            cells[str(h)] = cell
        comps[name] = {"description": specs.COMPONENTS[name], "horizons": cells,
                       "decay": {_key(s): decay_cell(panel, name, s) for s in specs.SEGMENTS}}  # fmt: skip
    families: dict[str, Any] = {}
    for fam, field, hs in (
        ("A", "sign_flip", specs.HORIZONS),
        ("B", "period_difference", specs.FAMILY_B_HORIZONS),
    ):
        ps = {(n, h): comps[n]["horizons"][str(h)][field]["p"] for n in COMPONENTS for h in hs}
        qs = stats.bh_qvalues(ps, m=FAMILY_SIZE[fam])
        for (n, h), q in qs.items():
            target = comps[n]["horizons"][str(h)] if fam == "A" else comps[n]["horizons"][str(h)][field]
            target.update(q=q, detected=None if q is None else q <= specs.FDR)
        families[fam] = {"size": FAMILY_SIZE[fam], "known": sum(p is not None for p in ps.values()),
                         "fdr": specs.FDR, "detected": [[n, h] for (n, h), q in qs.items()
                                                        if q is not None and q <= specs.FDR]}  # fmt: skip
    for comp in comps.values():
        comp["reading"] = reading(comp, per_side)
    months = panel.months
    eligible = [m.eligible for m in months if m.eligible]
    return {
        "study": specs.STUDY_ID, "spec_version": specs.SPEC_VERSION, "registration": prereg.HASH,
        "prereg_sha256": prereg.PREREG_SHA256, "label": specs.LABEL, "generated": today_ist().isoformat(),
        "audit": audit, "price_cache": coverage,
        "panel": {"decision_dates": len(months), "from": months[0].t.isoformat() if months else None,
                  "to": months[-1].t.isoformat() if months else None,
                  "months_with_eligible_names": len(eligible), "members": _range([m.members for m in months]),
                  "eligible": _range(eligible), "members_without_prices": panel.missing, "ended": panel.ended,
                  "flags": {_key(w): _flags(panel, w) for w in sorted({w for m in months for w in m.returns})}},
        "costs": {"buy": bt.COST_BUY, "sell": bt.COST_SELL, "per_side": per_side},
        "families": families, "components": comps, "unknown_components": specs.UNKNOWN_COMPONENTS,
        "reading_always": specs.READING["always"],
    }  # fmt: skip


def study(universe: dict[str, bt.Series], market: bt.Series, members: Members,
          coverage: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Panel, look-ahead audit (LookAheadError refuses the report) and statistics from in-memory series."""
    from finresearch.factordiag import panel as pn

    built = pn.build(universe, market, members)
    return run(built, pn.audit(universe, market, members, built), coverage)


def execute(
    data_dir: Path, out_dir: Path, *, overwrite: bool = False, root: Path | None = None
) -> dict[str, Any]:
    """The owner's one-shot command: registration check, the read of the price cache, the audit, the statistics, and
    results.json + RESULTS.md in `out_dir`. Refused when the registration moved, the cache is missing, or
    results.json already exists and `overwrite` is not set."""
    import json

    from finresearch.config import REPO_ROOT
    from finresearch.factordiag import report

    base = root or REPO_ROOT
    prereg.require_registered(base)  # before any price is read
    target = out_dir / RESULTS_JSON
    if target.exists() and not overwrite:
        raise Refused(f"{target} exists: the diagnostic was already run. Pass --overwrite to replace it (the result "
                      "is deterministic, so a re-run only differs when the price cache or the code changed)")  # fmt: skip
    universe, market, members, coverage = load_inputs(data_dir, base)
    res = study(universe, market, members, coverage)
    out_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(res, indent=1, default=str) + "\n")
    (out_dir / RESULTS_MD).write_text(report.render(res))
    return res
