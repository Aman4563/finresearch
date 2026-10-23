"""The card ledger's scoring report (#376), as registered: evals/experiments/card_ledger/PREREG.md and ADDENDUM.md
B1.1 (the statistics), B1.2 (the override log), B1.3 (the panel, scored in signals.prob_panel).

Events are horserace.ledger_events' (each resolved card, its real-world and risk-neutral forecasts paired; `first` =
the PREREG's independence rule); scores are horserace.scores', skillmon.stats' and evals.calibration's, reused. Every
statistic is None (unknown) where it is undefined, never 0; void and open cards are counted, never scored. Read only:
the GET and the CLI call `report`, which never writes. Personal (it reads the override log): never imported by a
prompt-building or agent-tool package."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

REGISTERED = date(2026, 10, 22)  # PREREG.md: cards logged before it are reported separately
MODELS = {"p_rw": "real-world PoP (the card's headline)", "p_rn_atm": "risk-neutral PoP (benchmark)"}
CLAIM_MIN = 100  # PREREG "Claims": no calibration claim below this many independent events per symbol
BSS_REPS, BSS_SEED = 2000, 376
PIT_BINS, P10 = 10, 0.10


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def bss(events: Sequence[Any]) -> dict[str, Any]:
    """Primary 1: 1 - Brier(p_rw) / Brier(p_rn_atm) on the first events where both are known, in origin order."""
    from finresearch.skillmon import stats

    xs = sorted((e for e in events if e.first and _p(e, "p_rw") is not None and _p(e, "p_rn_atm") is not None),
                key=lambda e: (e.origin, e.underlying, e.expiry))  # fmt: skip
    pm, pb, o = [_p(e, "p_rw") for e in xs], [_p(e, "p_rn_atm") for e in xs], [e.outcome for e in xs]
    return stats.bss_ci(pm, pb, o, h=1, reps=BSS_REPS, seed=BSS_SEED)  # type: ignore[arg-type]


def cohort(events: Sequence[Any]) -> dict[str, Any]:
    """Primary 2: mean P&L per set (ENTER or NO PROVEN EDGE) - (WAIT or AVOID), expiry-clustered (B1.1)."""
    from finresearch.horserace import scores as sc

    xs = [e for e in events if e.first and e.gate_pass is not None]
    out: dict[str, Any] = {"unknown_class": sum(1 for e in events if e.first and e.gate_pass is None),
                           "mean_pass": _mean([e.pnl for e in xs if e.gate_pass]),
                           "mean_fail": _mean([e.pnl for e in xs if not e.gate_pass])}  # fmt: skip
    return out | sc.gate_information(
        [bool(e.gate_pass) for e in xs], [e.pnl for e in xs], [e.expiry for e in xs]
    )


def _p(e: Any, key: str) -> float | None:
    f = e.forecasts.get(key)
    return None if f is None else f.p


def model_scores(events: Sequence[Any], key: str) -> dict[str, Any]:
    """Secondary (B1.1): Brier, log score and reliability bins on the first events; PIT and p10 coverage of the
    model's logged P&L dots."""
    from finresearch.evals.calibration import brier, log_loss, reliability_bins
    from finresearch.horserace import scores as sc
    from finresearch.horserace.specs import SCORES

    xs = [e for e in events if e.first and _p(e, key) is not None]
    p, o = [_p(e, key) for e in xs], [e.outcome for e in xs]
    dotted = [e for e in events if e.first and (f := e.forecasts.get(key)) is not None and f.dots]
    u = [sc.pit(list(e.forecasts[key].dots), e.pnl, SCORES.pit_clip) for e in dotted]
    hist = [0] * PIT_BINS
    for v in u:
        hist[min(int(v * PIT_BINS), PIT_BINS - 1)] += 1
    below = sum(1 for e in dotted if e.pnl < sc.quantile_fn(list(e.forecasts[key].dots))(P10))
    n = len(dotted)
    return {"model": key, "label": MODELS.get(key, key), "n": len(xs), "brier": brier(p, o),  # type: ignore[arg-type]
            "log": log_loss(p, o, SCORES.log_clip), "mean_p": _mean(p), "base_rate": _mean([float(y) for y in o]),  # type: ignore[arg-type]
            "reliability": [asdict(b) for b in reliability_bins(p, o)],  # type: ignore[arg-type]
            "pit": {"n": n, "bins": hist, "clipped": sum(1 for v in u if v in (SCORES.pit_clip, 1 - SCORES.pit_clip)),
                    "no_dots": len(xs) - sum(1 for e in xs if e.forecasts[key].dots)},
            "p10": {"n": n, "below": below, "share": below / n if n else None, "expected": P10,
                    "p": sc.binom_two_sided(below, n, P10) if n else None}}  # fmt: skip


def claims(events: Sequence[Any]) -> list[dict[str, Any]]:
    """Per underlying: the independent events with a real-world probability, and whether a claim is allowed."""
    unds = sorted({e.underlying for e in events})
    out = []
    for u in unds:
        n = sum(1 for e in events if e.underlying == u and e.first and _p(e, "p_rw") is not None)
        out.append({"underlying": u, "n_independent": n, "claim_allowed": n >= CLAIM_MIN})
    return out


def section(events: Sequence[Any]) -> dict[str, Any]:
    return {"events": len(events), "independent": sum(1 for e in events if e.first), "bss": bss(events),
            "cohort": cohort(events), "models": [model_scores(events, k) for k in MODELS],
            "claims": claims(events)}  # fmt: skip


def split(events: Sequence[Any]) -> dict[str, Any]:
    """B1.1: before / after registration by the card's IST logging date, side by side, and pooled (never the
    headline). `first` stays the pooled independence rule: a cluster's first card decides which half holds it."""
    return {"registered": REGISTERED.isoformat(),
            "before": section([e for e in events if e.origin < REGISTERED]),
            "after": section([e for e in events if e.origin >= REGISTERED]),
            "pooled": section(list(events))}  # fmt: skip


GROUPS = (("traded_against", "traded", True), ("traded_with", "traded", False),
          ("skipped_enter", "skipped", True), ("skipped_other", "skipped", False))  # fmt: skip


def override_pnl(decisions: Sequence[dict[str, Any]], outcomes: dict[tuple[int, int], dict[str, Any]]
                 ) -> dict[str, Any]:  # fmt: skip
    """B1.2: the card ledger's simulated net P&L per set (never the owner's fills) per decision group, with the
    counts behind it. `outcomes`: (card_log id, card index) -> {status, value, cluster} of the card's real-world
    forecast; a decided card with no forecast (a waiting card, or not recorded yet) is counted as `no_forecast`."""
    out: dict[str, Any] = {}
    for name, decision, override in GROUPS:
        ds = [d for d in decisions if d["decision"] == decision and d["override"] is override]
        got = [outcomes.get((d["card_log_id"], d["card_index"])) for d in ds]
        done = [g for g in got if g and g["status"] == "resolved" and g["value"] is not None]
        out[name] = {"decisions": len(ds), "resolved": len(done), "mean_pnl": _mean([g["value"] for g in done]),
                     "open": sum(1 for g in got if g and g["status"] == "open"),
                     "void": sum(1 for g in got if g and g["status"] == "void"),
                     "no_forecast": sum(1 for g in got if g is None),
                     "clusters": len({g["cluster"] for g in done})}  # fmt: skip
    return out


def _outcomes(s: Session) -> tuple[dict[tuple[int, int], dict[str, Any]], dict[str, int]]:
    """Each card's real-world forecast (status, value, cluster) and the ledger's card counts by status."""
    from finresearch.db.models import Forecast
    from finresearch.fnobook.card_forecasts import EVENT_KIND, SOURCE
    from finresearch.signals import ledger
    from finresearch.signals.tradeplan import PLAN_METHOD

    out, counts = {}, {"open": 0, "resolved": 0, "void": 0, "no_probability": 0}
    q = select(Forecast).where(Forecast.event_kind == EVENT_KIND, Forecast.method == PLAN_METHOD)
    for f in s.scalars(q):
        counts[f.status] = counts.get(f.status, 0) + 1
        counts["no_probability"] += f.probability is None
        key = (int(f.source.removeprefix(SOURCE)), int((f.inputs or {}).get("card_index", 0)))
        out[key] = {"status": f.status, "value": f.resolution_value, "cluster": ledger._cluster(f)}
    return out, counts


def report(s: Session) -> dict[str, Any]:
    """The whole report (read only): the card statistics split by registration, the override log's rate and P&L,
    and the fixed panel's scores."""
    from finresearch.fnobook import overrides as ov
    from finresearch.horserace import ledger_events
    from finresearch.signals import prob_panel

    outcomes, counts = _outcomes(s)
    decisions = ov.latest(s)
    return {"prereg": "evals/experiments/card_ledger/PREREG.md", "addendum": "evals/experiments/card_ledger/ADDENDUM.md",
            "claim_min": CLAIM_MIN, "cards": counts, **split(ledger_events.load(s)),
            "overrides": {**ov.rate(decisions), "pnl": override_pnl(decisions, outcomes),
                          "rows": len(rows := ov.history(s)), "recent": rows[-50:][::-1]},  # newest 50 of `rows`
            "panel": prob_panel.score(s)}  # fmt: skip
