"""In-sample / out-of-sample evaluation of the pre-registered specs against the stated pass criteria (#307).

One continuous replay from IS_START to the latest stored day (plus a stress replay with slippage × 2); windows are
slices of it: a trade belongs to the window of its entry day, equity statistics use the window's calendar days.
No parameter is fitted, so "walk-forward" is the same frozen spec judged year by year (`by_year`).
Status: `candidate` when every criterion holds out of sample; `fails` otherwise, with the failed criteria in words;
`no_data` when the underlying has no stored history. A candidate is evidence, never a guarantee.
`fill` (#382, P2.7a): the registered `same_print` fills, or a next-print sensitivity (engine.FILLS). A next-print
report is judged by the same criteria but its status is `sensitivity`, never `candidate`: it is compared with the
same-print report (backtest.compare), and backtest.store.latest_run never serves it as the evidence.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime
from typing import Any

from finresearch.backtest import engine, metrics, prereg, specs
from finresearch.backtest.history import History
from finresearch.backtest.specs import BOOK, CRITERIA, Book, Criteria, Spec


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    h = specs.registration_hash()
    if h != prereg.HASH:
        raise PreregistrationError(
            f"specs.py hashes to {h[:12]}… but the registered hash is {prereg.HASH[:12]}…: out-of-sample evaluation "
            "refused. Register a changed spec as a new variant (specs.py docstring) before evaluating it out of sample."
        )
    return h


def window(trades: list[dict[str, Any]], equity: list[tuple[date, float]], lo: date, hi: date, book: Book,
           crit: Criteria) -> dict[str, Any]:  # fmt: skip
    tr = [t for t in trades if lo <= date.fromisoformat(t["entry_day"]) <= hi]
    eq = [(d, v) for d, v in equity if lo <= d <= hi]
    return {"from": lo.isoformat(), "to": hi.isoformat(),
            **metrics.trade_stats(tr, crit.bootstrap_draws, crit.bootstrap_seed),
            **metrics.equity_stats(eq, book.capital)}  # fmt: skip


def judge(w_is: dict[str, Any], w_oos: dict[str, Any], stress: dict[str, Any], crit: Criteria) -> list[dict]:
    """Every criterion with its value and whether it holds (unknown = does not hold)."""

    def c(name: str, value: Any, ok: bool | None, rule: str) -> dict[str, Any]:
        return {"criterion": name, "value": value, "pass": bool(ok), "rule": rule}

    e, t, lo = w_oos["expectancy"], w_oos["t_stat"], w_oos["ci_low"]
    dd, dsr = w_oos["max_drawdown_pct"], w_oos.get("deflated_sharpe")
    return [
        c("OOS trades", w_oos["trades"], w_oos["trades"] >= crit.min_oos_trades, f">= {crit.min_oos_trades}"),
        c("OOS expectancy after costs (₹/trade)", e, e is not None and e > 0, "> 0"),
        c("OOS t-stat (expiry-clustered)", t, t is not None and t >= crit.min_t_stat, f">= {crit.min_t_stat:g}"),
        c(f"OOS {crit.bootstrap_ci:.0%} bootstrap CI low (₹/trade)", lo, lo is not None and lo > 0, "> 0"),
        c("OOS max drawdown (% of capital)", dd, dd is not None and dd <= crit.max_drawdown_pct,
          f"<= {crit.max_drawdown_pct:g}"),
        c("OOS deflated Sharpe", dsr, dsr is not None and dsr >= crit.min_deflated_sharpe,
          f">= {crit.min_deflated_sharpe:g} (N = {specs.VARIANTS_TRIED} variants)"),
        c("In-sample expectancy (₹/trade)", w_is["expectancy"],
          w_is["expectancy"] is not None and w_is["expectancy"] > 0, "> 0"),
        c(f"OOS expectancy with slippage × {crit.stress_slippage_mult:g}", stress["expectancy"],
          stress["expectancy"] is not None and stress["expectancy"] > 0, "> 0"),
    ]  # fmt: skip


def overlay(trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Covered call / protective put: the held stock alone vs the stock with the overlay, over the same periods."""
    rows = [t for t in trades if t.get("stock_pnl") is not None]
    if not rows:
        return None
    stock = [t["stock_pnl"] for t in rows]
    both = [t["stock_pnl"] + t["net"] for t in rows]
    return {"periods": len(rows), "stock_total": round(sum(stock), 2), "with_overlay_total": round(sum(both), 2),
            "stock_worst": round(min(stock), 2), "with_overlay_worst": round(min(both), 2),
            "stock_cvar_5": metrics._r(metrics.cvar(stock), 2), "with_overlay_cvar_5": metrics._r(metrics.cvar(both), 2),
            "note": "Per holding period: the stock leg is priced with the same-expiry future (carry included)."}  # fmt: skip


def thin(equity: list[tuple[date, float]], points: int = 600) -> list[list[Any]]:
    step = max(1, len(equity) // points)
    keep = equity[::step] + ([equity[-1]] if equity and (len(equity) - 1) % step else [])
    return [[d.isoformat(), round(v, 2)] for d, v in keep]


def evaluate(history: History, *, stock: str | None, source: str, oos: bool = True, book: Book = BOOK,
             crit: Criteria = CRITERIA, spec_list: tuple[Spec, ...] = specs.SPECS,
             fill: str = "same_print") -> dict[str, Any]:  # fmt: skip
    """The full report (module docstring). `oos=False` replays the in-sample window only and never reads a day after
    IS_END; `oos=True` requires the registration (require_registered)."""
    reg = require_registered() if oos else specs.registration_hash()
    pairs = [(s, engine.resolve(s, stock)) for s in spec_list]
    runnable = [c for _, c in pairs if c is not None]
    latest = {c.underlying: history.latest(c.underlying) for c in runnable}
    last_all = max((d for d in latest.values() if d), default=specs.IS_END)
    entries_end = last_all if oos else specs.IS_END
    data_end = None if oos else specs.IS_END
    kw = dict(warmup_days=specs.IV_WARMUP_DAYS, data_end=data_end, fill=fill)
    main = engine.run(runnable, history, specs.IS_START, entries_end, book, **kw)  # type: ignore[arg-type]
    stress = (engine.run(runnable, history, specs.OOS_START, entries_end,
                         replace(book, slippage_mult=crit.stress_slippage_mult), **kw) if oos else {})  # type: ignore[arg-type]  # fmt: skip
    out = []
    for s, c in pairs:
        row: dict[str, Any] = {"key": s.key, "name": s.name, "role": s.role, "kind": s.kind,
                               "underlying": c.underlying if c else None, "spec": asdict(s),
                               "spec_hash": specs.spec_hash(s)}  # fmt: skip
        r = main.get(s.key)
        if c is None or r is None or not latest.get(c.underlying):
            why = (
                "no stock options are stored, so the overlay rule has no stock to test (covered only by the "
                "synthetic tests)"
                if s.underlying == "STOCK_RULE"
                else "no stored option history for this underlying"
            )
            row.update(status="no_data", reasons=[why])
            out.append(row)
            continue
        hi = latest[c.underlying] or specs.IS_END
        w_is = window(r["trades"], r["equity"], specs.IS_START, specs.IS_END, book, crit)
        windows = {"is": w_is}
        if oos:
            windows["oos"] = window(r["trades"], r["equity"], specs.OOS_START, hi, book, crit)
            sr = stress[s.key]
            windows["oos_stress"] = window(sr["trades"], sr["equity"], specs.OOS_START, hi, book, crit)
        row.update(windows=windows, trades=r["trades"], equity=thin(r["equity"]), skips=r["skips"],
                   notes=r["notes"], open_at_end=r["open_at_end"], entries_considered=r["entries_considered"],
                   by_year=metrics.breakdown(r["trades"], lambda t: t["entry_day"][:4]),
                   by_iv_regime=metrics.breakdown(r["trades"], lambda t: metrics.regime(t["iv_pct"])),
                   overlay=overlay(r["trades"]) if s.role == "overlay" else None,
                   max_day_read=r["max_day_read"].isoformat() if r["max_day_read"] else None)  # fmt: skip
        out.append(row)
    _finish(out, oos, crit, fill)
    return {
        "version": specs.SPEC_VERSION, "registration_hash": reg, "registered_hash": prereg.HASH,
        "registered_on": prereg.REGISTERED_ON, "variants_tried": specs.VARIANTS_TRIED, "oos_evaluated": oos,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"), "source": source, "fill": fill,
        "windows": {"is": [specs.IS_START.isoformat(), specs.IS_END.isoformat()],
                    "oos": [specs.OOS_START.isoformat(), last_all.isoformat()] if oos else None},
        "stock_rule_symbol": stock, "book": asdict(book), "criteria": asdict(crit),
        "deflated_sharpe_source": specs.DEFLATED_SHARPE_SOURCE, "strategies": out,
    }  # fmt: skip


def _finish(rows: list[dict[str, Any]], oos: bool, crit: Criteria, fill: str = "same_print") -> None:
    """Deflated Sharpe across the trials of the out-of-sample window, then each spec's verdict."""
    if not oos:
        for r in rows:
            if r.get("status") != "no_data":
                r.update(status="in_sample_only", reasons=["out-of-sample not evaluated"])
        return
    srs = [
        r["windows"]["oos"]["daily_sr"] for r in rows if r.get("windows") and r["windows"]["oos"]["daily_sr"]
    ]
    mean = sum(srs) / len(srs) if srs else 0.0
    var = sum((x - mean) ** 2 for x in srs) / (len(srs) - 1) if len(srs) > 1 else 0.0
    for r in rows:
        if r.get("status") == "no_data":
            continue
        w = r["windows"]["oos"]
        dsr = None
        if w["daily_sr"] is not None and w["skew"] is not None:
            dsr = metrics.deflated_sharpe(w["daily_sr"], w["days"] - 1, w["skew"], w["kurtosis"],
                                          specs.VARIANTS_TRIED, var)  # fmt: skip
        w["deflated_sharpe"] = metrics._r(dsr, 4)
        w["sr_trials_variance"] = metrics._r(var, 10)
        checks = judge(r["windows"]["is"], w, r["windows"]["oos_stress"], crit)
        r["criteria"] = checks
        failed = [x["criterion"] for x in checks if not x["pass"]]
        if r["role"] == "overlay" and not crit.judge_overlays:
            r["status"] = "overlay_reported"
            r["reasons"] = [
                "an overlay on a held stock: reported with its evidence, never a candidate (at most one "
                "trade a month cannot reach the trade-count criterion)"
            ] + [f"would fail: {x}" for x in failed]
            continue
        r["status"] = "candidate" if not failed else "fails"
        r["reasons"] = [f"fails: {x}" for x in failed] or [
            "passes every pre-registered criterion out of sample"
        ]
        if fill != "same_print":  # a registered sensitivity (#382): never a candidate by itself
            r["status"] = "sensitivity"
            r["reasons"] = [f"{fill} fills: a sensitivity of the same-print verdict, never a candidate"] + r[
                "reasons"
            ]
