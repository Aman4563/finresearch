"""The card horse race runner (#377): scores every forecaster on the same events and applies the pre-registered
adoption gate (horserace/specs.py, evals/experiments/card_horse_race/PREREG.md). Pure: the events come from an
event source (the card ledger now, the 2022-26 replay later), so nothing here reads the database.

Unknown never becomes a score: a forecaster with no probability for an event is counted as missing on it and the
comparison with the incumbent is paired on the events both have; a trailing rate with an unknown gate class is None.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from finresearch.direction import metrics as dm
from finresearch.horserace import prereg, specs
from finresearch.horserace import scores as sc


class PreregistrationError(RuntimeError):
    pass


def require_registered() -> str:
    h = specs.registration_hash()
    if h != prereg.HASH:
        raise PreregistrationError(
            f"horserace/specs.py hashes to {h[:12]}… but the registered hash is {prereg.HASH[:12]}…: the race is "
            "refused. Record the change in ADDENDUM.md as a new SPEC_VERSION and commit its hash first."
        )
    return h


@dataclass(frozen=True)
class Forecast:
    p: float | None  # P(event)
    ev: float | None = None  # Rs per set
    dots: Sequence[float] | None = None  # 20 P&L quantiles at (i + 0.5) / 20, Rs per set


@dataclass(frozen=True)
class Event:
    underlying: str
    expiry: date
    structure: str
    origin: date  # the forecast's as-of session
    resolved_on: date  # the session its outcome was known
    outcome: int
    pnl: float
    gate_pass: bool | None  # None: the gate class is unknown
    first: bool  # the first entry session of (underlying, expiry, structure): an independent event for tail tests
    forecasts: dict[str, Forecast] = field(default_factory=dict)
    exit_reason: str | None = None


def with_trailing(events: Iterable[Event]) -> list[Event]:
    """Add p_base and p_gate: (k + 1) / (n + 2) over events of the same underlying and structure (and gate class)
    resolved strictly before the origin. Sorted by origin."""
    a0, b0 = specs.TRAILING_PRIOR
    by_origin = sorted(events, key=lambda e: e.origin)
    by_resolved = sorted(by_origin, key=lambda e: e.resolved_on)
    base: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    gate: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    j, out = 0, []
    for e in by_origin:
        while j < len(by_resolved) and by_resolved[j].resolved_on < e.origin:
            r = by_resolved[j]
            for d, key in (
                (base, (r.underlying, r.structure)),
                (gate, (r.underlying, r.structure, r.gate_pass)),
            ):
                d[key][0] += r.outcome
                d[key][1] += 1
            j += 1
        k, n = base[(e.underlying, e.structure)]
        pb = Forecast((k + a0) / (n + a0 + b0))
        if e.gate_pass is None:
            pg = Forecast(None)
        else:
            k, n = gate[(e.underlying, e.structure, e.gate_pass)]
            pg = Forecast((k + a0) / (n + a0 + b0))
        out.append(replace(e, forecasts={**e.forecasts, "p_base": pb, "p_gate": pg}))
    return out


def in_window(events: Iterable[Event], window: str) -> list[Event]:
    w = specs.WINDOWS.get(window)
    if w is None:  # never "every event": the vault is registered and read elsewhere (fno_pop_calibration)
        raise ValueError(f"unknown window {window!r}: one of {', '.join(specs.WINDOWS)}")
    lo = date.fromisoformat(w["start"])
    hi = date.fromisoformat(w["end"]) if w["end"] else date.max
    return [e for e in events if lo <= e.origin <= hi]


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def tail(events: Sequence[Event], key: str) -> dict[str, Any]:
    """Tail coverage of a forecaster's dots on the independent events (`first`), ordered by origin: tests on
    >= TESTS.tail_min_events events, else the same numbers labelled descriptive."""
    s, t = specs.SCORES, specs.TESTS
    xs = sorted(
        (e for e in events if e.first and (f := e.forecasts.get(key)) and f.dots), key=lambda e: e.origin
    )
    n = len(xs)
    out: dict[str, Any] = {
        "n": n,
        "mode": "test" if n >= t.tail_min_events else "descriptive",
        "alpha": s.tail_alpha,
    }
    if not n:
        return out
    dots = [list(e.forecasts[key].dots or []) for e in xs]
    hits = [int(e.pnl < sc.var_es(d, s.tail_alpha)[0]) for e, d in zip(xs, dots, strict=True)]
    u = [sc.pit(d, e.pnl, s.pit_clip) for e, d in zip(xs, dots, strict=True)]
    out.update(kupiec=sc.kupiec(sum(hits), n, s.tail_alpha), christoffersen=sc.christoffersen(hits),
               berkowitz=sc.berkowitz(u), pit_clipped=sum(1 for v in u if v in (s.pit_clip, 1 - s.pit_clip)),
               z2=sc.acerbi_szekely_z2(dots, [e.pnl for e in xs], s.tail_alpha, s.z2_sims, s.seed))  # fmt: skip
    kp, bp = out["kupiec"]["p"], out["berkowitz"]["p"]
    out["passes"] = (
        out["mode"] == "test" and kp is not None and kp >= t.alpha and bp is not None and bp >= t.alpha
    )
    return out


def score(events: Sequence[Event], key: str) -> dict[str, Any]:
    """Every registered score of one forecaster on the events where it gave a probability."""
    s = specs.SCORES
    xs = [e for e in events if (f := e.forecasts.get(key)) is not None and f.p is not None]
    out: dict[str, Any] = {"n": len(xs), "missing": len(events) - len(xs)}
    if not xs:
        return out
    p, o = [e.forecasts[key].p for e in xs], [e.outcome for e in xs]
    out.update(brier=_mean(dm.brier_each(p, o)), log=_mean(dm.logloss_each(p, o, s.log_clip)),  # type: ignore[arg-type]
               murphy=sc.murphy(p, o, s.murphy_bins), reliability=dm.reliability(p, o, s.murphy_bins),  # type: ignore[arg-type]
               mean_p=_mean(p), base_rate=_mean([float(x) for x in o]))  # type: ignore[arg-type]  # fmt: skip
    with_dots = [e for e in xs if e.forecasts[key].dots]
    out["crps"] = _mean(
        [sc.crps(list(e.forecasts[key].dots or []), e.pnl, s.integration_nodes) for e in with_dots]
    )
    with_ev = [e for e in xs if e.forecasts[key].ev is not None]
    out["ev_calibration"] = sc.ev_calibration([e.forecasts[key].ev for e in with_ev], [e.pnl for e in with_ev],  # type: ignore[misc]
                                              [(e.underlying, e.expiry) for e in with_ev]) if with_ev else None  # fmt: skip
    out["tail"] = tail(xs, key)
    return out


def _loss(e: Event, key: str, kind: str) -> float | None:
    f = e.forecasts.get(key)
    if f is None or f.p is None:
        return None
    if kind == "brier":
        return (f.p - e.outcome) ** 2
    return sc.crps(list(f.dots), e.pnl, specs.SCORES.integration_nodes) if f.dots else None


def dm_test(events: Sequence[Event], key: str, kind: str) -> dict[str, Any]:
    """DM-HLN (one-sided: `key` better than the incumbent) on per-expiry mean losses of the paired events."""
    t = specs.TESTS
    by_exp: dict[date, list[tuple[float, float]]] = defaultdict(list)
    for e in events:
        a, b = _loss(e, specs.INCUMBENT, kind), _loss(e, key, kind)
        if a is not None and b is not None:
            by_exp[e.expiry].append((a, b))
    days = sorted(by_exp)
    ref = [sum(a for a, _ in by_exp[d]) / len(by_exp[d]) for d in days]
    mod = [sum(b for _, b in by_exp[d]) / len(by_exp[d]) for d in days]
    if len(days) < t.dm_min_expiries:
        return {"n": len(days), "mean_diff": _mean([a - b for a, b in zip(ref, mod, strict=True)]), "dm": None,
                "p": None, "why": f"fewer than {t.dm_min_expiries} paired expiries"}  # fmt: skip
    return dict(dm.diebold_mariano(ref, mod, t.dm_h, t.dm_lag))


def _adopt(key: str, und: str, window: str, challengers: Sequence[str], brier: dict[str, Any],
           tl: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    """The pre-registered gate (specs.ADOPTION) for one forecaster on one underlying."""
    t = specs.TESTS
    why = []
    if key not in challengers:
        why.append("a benchmark or current model, not a registered challenger: reported, never adopted")
    if window not in specs.PRIMARY_WINDOWS:
        why.append(f"window {window!r} is not primary")
    ph = brier.get("p_holm")
    if ph is None or ph >= t.alpha:
        why.append(f"Brier not better than {specs.INCUMBENT} at Holm p < {t.alpha} (p_holm = {ph})")
    if tl is None or tl.get("n", 0) < t.tail_min_events:
        why.append(f"fewer than {t.tail_min_events} independent events for the tail tests")
    elif not tl.get("passes"):
        why.append(
            f"tail coverage fails (Kupiec p = {tl['kupiec']['p']}, Berkowitz p = {tl['berkowitz']['p']})"
        )
    tail_summary = None if tl is None else {k: tl.get(k) for k in ("n", "mode", "passes")}
    return {"adopt": not why, "why": "; ".join(why) or "every condition holds", "tail": tail_summary}


def run(
    events: Iterable[Event], window: str, challengers: Sequence[str] = specs.CHALLENGERS
) -> dict[str, Any]:
    """The race on one window: trailing forecasters are built on ALL the events given (only earlier resolutions
    count), then every forecaster is scored per underlying, compared with the incumbent (DM-HLN per expiry, Holm
    across forecaster x underlying per score) and put through the adoption gate. Every forecaster is reported."""
    xs = in_window(with_trailing(events), window)
    unds = sorted({e.underlying for e in xs})
    per = {u: [e for e in xs if e.underlying == u] for u in unds}
    keys = sorted({k for e in xs for k in e.forecasts})
    rivals = [k for k in keys if k != specs.INCUMBENT]
    report: dict[str, Any] = {"version": specs.SPEC_VERSION, "registration_hash": specs.registration_hash(),
                              "window": window, "window_role": (specs.WINDOWS.get(window) or {}).get("role"),
                              "n_events": len(xs), "incumbent": specs.INCUMBENT, "challengers": list(challengers),
                              "forecasters": {k: {u: score(per[u], k) for u in unds} for k in keys}}  # fmt: skip
    report["dm"] = {}
    for kind in ("brier", "crps"):
        res = {k: {u: dm_test(per[u], k, kind) for u in unds} for k in rivals}
        adj = dm.holm({f"{k}|{u}": res[k][u]["p"] for k in rivals for u in unds})
        for k in rivals:
            for u in unds:
                res[k][u]["p_holm"] = adj[f"{k}|{u}"]
        report["dm"][kind] = res
    report["gate"] = {u: sc.gate_information([bool(e.gate_pass) for e in g], [e.pnl for e in g],
                                             [e.expiry for e in g])
                      for u in unds if (g := [e for e in per[u] if e.gate_pass is not None])}  # fmt: skip
    report["adoption"] = {
        k: {u: _adopt(k, u, window, challengers, report["dm"]["brier"][k][u],
                      report["forecasters"][k][u].get("tail")) for u in unds}
        for k in rivals if any(e.forecasts.get(k) and e.forecasts[k].dots for e in xs)
    }  # fmt: skip
    report["adopted"] = sorted(
        f"{k} ({u})" for k, v in report["adoption"].items() for u, a in v.items() if a["adopt"]
    )
    return report


def summary_rows(report: dict[str, Any]) -> list[list[str]]:
    """One row per forecaster x underlying: n, Brier, log, CRPS, DM p (Holm) against the incumbent, tail mode."""

    def f(x: Any, d: int = 4) -> str:
        return "—" if x is None else f"{x:.{d}f}"

    rows = []
    for k, per in sorted(report["forecasters"].items()):
        for u, s in sorted(per.items()):
            d = (report["dm"]["brier"].get(k) or {}).get(u) or {}
            tl = s.get("tail") or {}
            rows.append([k, u, str(s["n"]), str(s["missing"]), f(s.get("brier")), f(s.get("log")), f(s.get("crps"), 2),
                         "incumbent" if k == specs.INCUMBENT else f(d.get("p_holm"), 3),
                         f"{tl.get('mode', '—')} (n={tl.get('n', 0)})"])  # fmt: skip
    return rows
