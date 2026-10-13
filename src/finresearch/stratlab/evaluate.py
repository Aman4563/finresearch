"""Backtest one Strategy Lab version (#318) with the #307 engine on the strategy's locked windows, then judge it with
the library's pass criteria and a deflated Sharpe whose N counts every variant ever evaluated out of sample.

`replay` runs the engine (the main replay from the in-sample start; out of sample also a stress replay with slippage
× 2) and computes the window statistics; it reads nothing after the in-sample end when `oos` is False. `finalize`
adds the deflated Sharpe (Bailey & López de Prado 2014, backtest.metrics) with N = the library's VARIANTS_TRIED +
the lab's counted variants, and V = the variance of the out-of-sample daily Sharpe ratios of the library's latest
evaluation and every counted lab variant (this one included). The windows of the library and of a lab strategy can
differ, so V is an approximation; with fewer than two trials it is unknown and so is the deflated Sharpe (a
criterion that cannot be read does not hold). Plain-language overfitting warnings come with every result.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date
from typing import Any

from finresearch.backtest import engine, metrics, specs
from finresearch.backtest import evaluate as lib
from finresearch.backtest.specs import CRITERIA, Criteria
from finresearch.stratlab.filters import Filters
from finresearch.stratlab.spec import RESULTS_NOTE, StrategySpec, book_for, n_filters, spec_hash, to_lab_spec
from finresearch.stratlab.split import Split

Progress = Callable[[float, str], None]


def replay(history: Any, s: StrategySpec, split: Split, *, key: str, name: str, oos: bool, crit: Criteria = CRITERIA,
           progress: Progress | None = None) -> dict[str, Any]:  # fmt: skip
    lab, book = to_lab_spec(s, key, name), book_for(s)
    latest = history.latest(s.underlying)
    if latest is None:
        return {"key": key, "name": name, "status": "no_data", "reasons": ["no stored F&O history"], "trades": []}
    hi = latest if oos else min(latest, split.is_end)
    main_share = 0.7 if oos else 1.0

    def hook(lo: float, share: float, label: str) -> Callable[[int, int, date], None] | None:
        if progress is None:
            return None
        return lambda i, n, d: progress(lo + share * i / max(n, 1), f"{label}: {d.isoformat()}")

    f = Filters(lab, book.rate)
    main = engine.run([lab], history, split.is_start, hi, book, warmup_days=specs.IV_WARMUP_DAYS,
                      data_end=None if oos else hi, gate=f.gate, on_day=f.on_day,
                      progress=hook(0.0, main_share, "replay"))[key]  # fmt: skip
    windows = {"is": lib.window(main["trades"], main["equity"], split.is_start, split.is_end, book, crit)}
    if oos:
        g = Filters(lab, book.rate)
        stress = engine.run([lab], history, split.oos_start, hi,
                            replace(book, slippage_mult=crit.stress_slippage_mult), warmup_days=specs.IV_WARMUP_DAYS,
                            gate=g.gate, on_day=g.on_day, progress=hook(main_share, 1 - main_share, "stress"))[key]  # fmt: skip
        windows["oos"] = lib.window(main["trades"], main["equity"], split.oos_start, hi, book, crit)
        windows["oos_stress"] = lib.window(stress["trades"], stress["equity"], split.oos_start, hi, book, crit)
    trades = main["trades"]
    return {
        "key": key, "name": name, "role": "lab", "kind": s.structure, "underlying": s.underlying,
        "spec": s.model_dump(), "spec_hash": spec_hash(s), "oos": oos, "book": asdict(book),
        "windows_dates": {"is": [split.is_start.isoformat(), split.is_end.isoformat()],
                          "oos": [split.oos_start.isoformat(), hi.isoformat()] if oos else None},
        "windows": windows, "trades": trades, "equity": lib.thin(main["equity"]), "skips": main["skips"],
        "entries_considered": main["entries_considered"], "open_at_end": main["open_at_end"],
        "by_year": metrics.breakdown(trades, lambda t: t["entry_day"][:4]),
        "by_iv_regime": metrics.breakdown(trades, lambda t: metrics.regime(t["iv_pct"])),
        "max_day_read": main["max_day_read"].isoformat() if main["max_day_read"] else None,
        "notes": [RESULTS_NOTE] if s.exit_before_results else [],
    }  # fmt: skip


def trials_variance(srs: list[float]) -> float | None:
    if len(srs) < 2:
        return None
    m = sum(srs) / len(srs)
    return sum((x - m) ** 2 for x in srs) / (len(srs) - 1)


def deflated(w: dict[str, Any], n_trials: int, var: float | None) -> float | None:
    if var is None or w.get("daily_sr") is None or w.get("skew") is None or w.get("kurtosis") is None:
        return None
    return metrics._r(metrics.deflated_sharpe(w["daily_sr"], w["days"] - 1, w["skew"], w["kurtosis"], n_trials, var), 4)


def finalize(rep: dict[str, Any], *, n_trials: int, var: float | None, oos_versions: int, n_lab: int,
             crit: Criteria = CRITERIA) -> dict[str, Any]:  # fmt: skip
    """Status, criteria and warnings. `oos_versions`: versions of this strategy evaluated out of sample so far (this
    one included); `n_lab`: lab variants counted across the app (this one included)."""
    if rep.get("status") == "no_data":
        return rep
    rep["n_trials"], rep["sr_trials_variance"] = n_trials, metrics._r(var, 10)
    if not rep["oos"]:
        rep.update(status="in_sample_only", reasons=["out of sample not run: tune freely here, it is not counted"])
        rep["warnings"] = warnings(rep, n_trials, oos_versions, n_lab, crit)
        return rep
    w = rep["windows"]["oos"]
    w["deflated_sharpe"] = deflated(w, n_trials, var)
    checks = lib.judge(rep["windows"]["is"], w, rep["windows"]["oos_stress"], crit)
    for c in checks:
        if c["criterion"] == "OOS deflated Sharpe":
            c["rule"] = (f">= {crit.min_deflated_sharpe:g} (N = {n_trials} variants: {specs.VARIANTS_TRIED} pre-registered "
                         f"+ {n_lab} from the lab)")  # fmt: skip
            if var is None:
                c["rule"] += "; unknown until two variants have out-of-sample results"
    failed = [c["criterion"] for c in checks if not c["pass"]]
    rep["criteria"] = checks
    rep["status"] = "candidate" if not failed else "fails"
    rep["reasons"] = [f"fails: {x}" for x in failed] or ["passes every library criterion out of sample"]
    rep["warnings"] = warnings(rep, n_trials, oos_versions, n_lab, crit)
    return rep


def warnings(rep: dict[str, Any], n_trials: int, oos_versions: int, n_lab: int, crit: Criteria) -> list[str]:
    """Overfitting warnings in plain words (shown above the numbers)."""
    out: list[str] = []
    s = StrategySpec.model_validate(rep["spec"])
    w_is, w_oos = rep["windows"]["is"], rep["windows"].get("oos")
    if oos_versions > 1:
        out.append(f"This strategy has had {oos_versions} versions tested out of sample. Only the first look at the "
                   "out-of-sample years was clean; changes made after seeing them fit those years too.")  # fmt: skip
    if rep["oos"]:
        out.append(f"{n_trials} variants have now been tried in this app ({specs.VARIANTS_TRIED} pre-registered + "
                   f"{n_lab} lab). The more variants tried, the luckier the best one looks: the deflated Sharpe "
                   "raises the bar with each.")  # fmt: skip
    k = n_filters(s)
    if k >= 2:
        out.append(f"{k} entry filters are switched on. Each filter is a knob that can fit past noise; results with "
                   "many filters and few trades rarely repeat.")  # fmt: skip
    n_tr = (w_oos or w_is)["trades"]
    if n_tr < crit.min_oos_trades:
        out.append(f"Only {n_tr} trades {'out of sample' if w_oos else 'in sample'} (the library asks for "
                   f"{crit.min_oos_trades}): too few to tell skill from luck.")  # fmt: skip
    if w_oos and w_is["sharpe"] is not None and w_oos["sharpe"] is not None and w_is["sharpe"] > 0:
        if w_oos["sharpe"] < 0.5 * w_is["sharpe"]:
            out.append(f"Out-of-sample Sharpe {w_oos['sharpe']:.2f} is less than half the in-sample {w_is['sharpe']:.2f}: "
                       "the classic sign of a fit to the in-sample years.")  # fmt: skip
    if w_oos and w_oos.get("deflated_sharpe") is None:
        out.append("The deflated Sharpe could not be computed (unknown), so the multiple-testing criterion does not hold.")
    nets = sorted((t["net"] for t in rep["trades"]), reverse=True)
    total = sum(nets)
    if total > 0 and len(nets) >= 10:
        top = sum(nets[: max(1, len(nets) // 10)])
        if top > total:
            out.append("The best 10 % of trades made more than the whole strategy: the rest lost money in total, so "
                       "the result rests on a few trades.")  # fmt: skip
    if s.exit_before_results:
        out.append(RESULTS_NOTE)
    return out
