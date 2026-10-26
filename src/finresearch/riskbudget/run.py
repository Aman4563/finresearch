"""The seen-period risk-budget study's run (#395 Stage 2; evals/experiments/risk_budget_seen/PREREG.md): the
registration is checked before the history is touched, the four registered sellers are replayed on one book over the
registered period (riskbudget.replay), and the report holds aggregates only (no trade rows), labelled seen. Nothing is
written to any database, no card or limit changes, and the 2008-2021 vault is never opened (DbHistory refuses a
database holding vault-period days without a grant)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from finresearch.backtest.specs import BOOK, CRITERIA, Book, by_key
from finresearch.riskbudget import prereg, replay, specs, stats

STUDY_PATHS = ("src/finresearch/riskbudget", "src/finresearch/fincalc/tradeplan.py",
               "src/finresearch/fincalc/book_scenarios.py", "src/finresearch/fincalc/beta.py",
               "src/finresearch/fincalc/margin_approx.py")  # fmt: skip


class Refused(RuntimeError):
    """The run cannot be made as registered; nothing was replayed."""


def last_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def open_history(url: str | None) -> Any:
    """The F&O history to replay, read only, with the stored NSE SPAN ranges (the registered margins). A database
    without the span_param table is refused: falling back to the assumed ranges everywhere is not the registration."""
    from sqlalchemy import inspect

    from finresearch.backtest.dbhistory import DbHistory
    from finresearch.db import get_engine

    with get_engine(url).connect() as c:
        if not inspect(c).has_table("span_param"):
            raise Refused("the history database has no span_param table: the registered margins read the stored NSE "
                          "SPAN ranges (#370); name the database that holds them with --history-url")  # fmt: skip
    return DbHistory(url, span_ranges=specs.SPAN_RANGES)


def summary(arm: replay.Arm, book: Book = BOOK) -> dict[str, Any]:
    """One arm's aggregates: entries, closed trades and the expectancy with its CI as the backtest reports it, the
    worst month, the drawdown, open risk, and what the limits refused or cut (no trade rows)."""
    from finresearch.backtest import metrics

    trades = arm.trades()
    ts = metrics.trade_stats(trades, CRITERIA.bootstrap_draws, CRITERIA.bootstrap_seed)
    ms = stats.months(arm.equity, book.capital)
    risk = [r for _, r in arm.risk]
    no_risk = None in risk or not risk  # an unknown open risk (or no session): unknown, never 0
    return {
        "entries": len(arm.entries), "closed_trades": ts["trades"], "expectancy": ts["expectancy"],
        "ci_low": ts["ci_low"], "ci_high": ts["ci_high"], "t_stat": ts["t_stat"], "win_rate": ts["win_rate"],
        "total_net": ts["total_net"], "worst_trade": ts["worst_trade"], "worst_month": stats.worst_month(ms),
        "max_drawdown": stats.max_drawdown([x for _, p in ms for x in p]),
        "mean_open_risk": None if no_risk else sum(risk) / len(risk),  # type: ignore[arg-type]
        "max_open_risk": None if no_risk else max(risk),  # type: ignore[type-var]
        "lots_taken": sum(e["lots"] for e in arm.entries),
        "entries_below_trade_size": sum(e["lots"] < e["trade_lots"] for e in arm.entries),
        "refused": dict(arm.refused), "cut": dict(arm.cut),
        "engine_skips": {k: dict(st.skips) for k, st in arm.states.items()},
        "entries_on_span_file": sum(bool(e["span_file"]) for e in arm.entries),
        "entries_on_assumed_ranges": sum(not e["span_file"] for e in arm.entries),
        "period_end_trades": sum(t["reason"] == "period_end" for t in trades),
        "by_spec": {k: {"entries": sum(e["spec"] == k for e in arm.entries), "closed_trades": len(st.trades),
                        "total_net": round(sum(t["net"] for t in st.trades), 2)} for k, st in arm.states.items()},
    }  # fmt: skip


def _unknown_primary(months: int, why: str) -> dict[str, Any]:
    cell = {"budgeted": None, "control": None, "delta": None, "p": None, "passes": None}
    return {
        "months": months,
        "passes": False,
        "reason": why,
        "worst_month": dict(cell),
        "max_drawdown": dict(cell),
    }


def report(rep: replay.Replay, *, book: Book = BOOK, period_end: date = specs.PERIOD_END) -> dict[str, Any]:
    """The registered tests on a replay with all three arms: the primary (budgeted against the size-matched control),
    the secondary (trades taken), and what is only reported (the raw cut, the card rule, each arm's aggregates)."""
    cap, arms = book.capital, rep.arms
    u, b, k = arms["uncapped"], arms["budgeted"], arms.get("card_rule")
    out: dict[str, Any] = {
        "label": specs.LABEL, "first_session": rep.first and rep.first.isoformat(),
        "last_session": rep.last and rep.last.isoformat(), "sessions": rep.sessions,
        "partial": rep.last is None or rep.last < last_weekday(period_end),
        "arms": {name: summary(arm, book) for name, arm in arms.items()},
    }  # fmt: skip
    ms_b = stats.months(b.equity, cap)
    kk = stats.control_k(b.risk, u.risk)
    out["control"] = {"k": kk, "rule": specs.ARM_RULES["control"]}
    if kk is None:
        out["primary"] = _unknown_primary(len(ms_b), "k unknown (an unknown open risk, or nothing ever open in the "
                                                     "uncapped book), which fails")  # fmt: skip
    else:
        ms_c = stats.months(stats.scaled(u.equity, kk, cap), cap)
        out["control"] |= {"worst_month": stats.worst_month(ms_c),
                           "max_drawdown": stats.max_drawdown([x for _, p in ms_c for x in p])}  # fmt: skip
        out["primary"] = stats.primary(ms_b, ms_c)
    out["secondary"] = stats.secondary(len(b.entries), len(u.entries))
    a = out["arms"]
    tails = lambda x, y: {key: {x: a[x][key], y: a[y][key]} for key in ("worst_month", "max_drawdown")}  # noqa: E731
    out["raw_cut"] = {"gated": False, **tails("budgeted", "uncapped")}
    if k is not None:
        out["card_rule"] = {"gated": False, "trades_fall": stats.secondary(len(k.entries), len(u.entries))["fall"],
                            **tails("card_rule", "uncapped")}  # fmt: skip
    out["verdict"] = verdict(out)
    return out


def _pct(x: float | None) -> str:
    return "unknown" if x is None else f"{x:.1%}"


def verdict(r: dict[str, Any]) -> str:
    p, s = r["primary"], r["secondary"]
    head = (f"PARTIAL: the stored history ends on {r['last_session']}, before the period's last weekday; not the "
            "registered run. " if r["partial"] else "")  # fmt: skip
    if p["worst_month"]["p"] is None:
        prim = f"UNKNOWN ({p['reason']})"
    else:
        prim = (f"{'PASSES' if p['passes'] else 'FAILS'} (worst month p = {p['worst_month']['p']:.4f}, "
                f"max drawdown p = {p['max_drawdown']['p']:.4f}, each must be < {specs.ALPHA})")  # fmt: skip
    sec = ("UNKNOWN (no uncapped entry), which fails" if s["fall"] is None else
           f"{'passes' if s['passes'] else 'fails'} (trades taken fell {_pct(s['fall'])}; at most "
           f"{s['max_fall']:.0%})")  # fmt: skip
    return (f"{head}Seen, secondary period. Primary (budgeted against the size-matched control): {prim}. Secondary: "
            f"{sec}. Nothing is adopted; the registered test of the limits is the vault read "
            f"({specs.VAULT_STUDY}), which this study does not perform.")  # fmt: skip


def study(history: Any, *, source: str) -> dict[str, Any]:
    """The registered study on `history` (read only): refused before any read unless the registration holds."""
    from finresearch.config import REPO_ROOT
    from finresearch.fincalc.dates import today_ist
    from finresearch.inventory import provenance

    reg = prereg.require_registered()
    rep = replay.replay(history, [by_key(k) for k in specs.SPEC_KEYS], specs.PERIOD_START, specs.PERIOD_END)
    return {"study": specs.STUDY_ID, "spec_version": specs.SPEC_VERSION, "registration": reg,
            "prereg_sha256": prereg.PREREG_SHA256, "source": source, "run_on": today_ist().isoformat(),
            **report(rep), "provenance": {**provenance.code_stamp(),
                                          "study_code_hash": provenance.code_hash(REPO_ROOT, STUDY_PATHS)}}  # fmt: skip
