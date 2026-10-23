"""The fixed probability panel (#376 (b)), registered in evals/experiments/card_ledger/ADDENDUM.md B1.3 before it ran.

Every stored NSE F&O bhavcopy session from PANEL_START logs, for NIFTY and BANKNIFTY at the near and the monthly
expiry, whatever the owner looked at (so the owner's choices cannot bias the sample):

- P(S_T < K) at the 10-, 16- and 25-delta puts and P(S_T > K) at the same calls, K by Black-76 forward delta at the
  ATM IV (fixed at the close, never snapped to a listed strike), and P(F e^-s <= S_T <= F e^s), s = sigma_ATM sqrt T;
  each under three lognormals around the forward F: `rn_atm` (ATM IV), `rw20` and `rw60` (realised volatility);
- each registered replay recipe's PoP under the card's models (`horserace.replay.card_at`, addendum A1).

Lognormal around F: P(S_T > K) = N(d2), d2 = (ln(F/K) - sigma^2 T / 2) / (sigma sqrt T) (Black 1976). Delta strike
from the undiscounted forward delta N(d1) = d: K = F exp(-N^-1(d) s + s^2 / 2) for a call, N^-1(1 - d) for a put.

Resolution: S_T = the expiry's final settlement price (`backtest.pricing.settlement_price` on the contract's last
stored session); recipes by the card ledger's exit replay (`fnobook.card_forecasts.simulate`). Unknown is never a
result: an unknown volatility is an unknown probability (None), an unknown forward or ATM IV voids the session's
strike events with the reason, an unknown settlement voids them at expiry. Market data only; its own table
(`prob_panel`), scored only here (`score`), never in the forecast ledger's headline."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from statistics import NormalDist
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.backtest.history import Day, History

PANEL_START = date(2026, 10, 23)  # B1.3: forward only, from the addendum's date
UNDERLYINGS = ("NIFTY", "BANKNIFTY")
DELTAS = (10, 16, 25)
MODELS = ("rn_atm", "rw20", "rw60")
RECIPE_MODELS = ("p_rw20", "p_rw60", "p_rw", "p_rn_atm")
TAIL_EVENTS = (*(f"put{d}" for d in DELTAS), *(f"call{d}" for d in DELTAS), "band1s")
WARMUP = timedelta(days=130)  # calendar days before a session: room for the 61 sessions RV60 needs
CLAIM_MIN = 100  # no calibration claim below this many independent events (PREREG "Claims")
_N = NormalDist()


def p_above(f: float, k: float, sigma: float | None, t: float) -> float | None:
    if not sigma or sigma <= 0 or t <= 0 or f <= 0 or k <= 0:
        return None
    sd = sigma * math.sqrt(t)
    return _N.cdf((math.log(f / k) - sd * sd / 2) / sd)


def delta_strike(right: str, f: float, s: float, delta: float) -> float:
    d1 = _N.inv_cdf(delta if right == "call" else 1 - delta)
    return f * math.exp(-d1 * s + s * s / 2)


def event_probability(event: str, inp: dict[str, Any], sigma: float | None) -> float | None:
    f, t = inp["forward"], inp["t"]
    if event == "band1s":
        lo, hi = inp["band"]
        a, b = p_above(f, lo, sigma, t), p_above(f, hi, sigma, t)
        return None if a is None or b is None else a - b
    p = p_above(f, inp["strike"], sigma, t)
    return None if p is None else (1 - p if event.startswith("put") else p)


def happened(event: str, inp: dict[str, Any], s_t: float) -> int:
    if event == "band1s":
        return int(inp["band"][0] <= s_t <= inp["band"][1])
    return int(s_t < inp["strike"] if event.startswith("put") else s_t > inp["strike"])


def expiries(x: Day) -> dict[date, str]:
    """B1.3: near = the earliest listed expiry after the session; monthly = the earliest with a future trading."""
    exps = [e for e in x.expiries() if e > x.day]
    out: dict[date, list[str]] = {}
    if exps:
        out.setdefault(exps[0], []).append("near")
    m = next((e for e in exps if x.is_monthly(e)), None)
    if m is not None:
        out.setdefault(m, []).append("monthly")
    return {e: ", ".join(k) for e, k in out.items()}


def tail_rows(x: Day, returns: Sequence[float], rate: float) -> list[dict[str, Any]]:
    """The strike and band events of one session (B1.3), one row per expiry x event."""
    from finresearch.backtest import pricing as px
    from finresearch.horserace import replay

    rows = []
    for e, kind in expiries(x).items():
        t = (e - x.day).days / 365
        fwd = px.forward(x, e, rate)
        atm = replay.atm_iv(x, e, fwd, t, rate) if fwd else None
        sig = {"rn_atm": atm, "rw20": replay.rv(returns, 20), "rw60": replay.rv(returns, 60)}
        base = {"forward": fwd, "t": t, "sigma": sig}
        why = "no forward at the close" if fwd is None else "no ATM IV at the close" if atm is None else None
        for ev in TAIL_EVENTS:
            inp = dict(base)
            if why is None:
                s = atm * math.sqrt(t)  # type: ignore[operator]
                if ev == "band1s":
                    inp["band"] = [fwd * math.exp(-s), fwd * math.exp(s)]  # type: ignore[operator]
                else:
                    right, d = ("put", int(ev[3:])) if ev.startswith("put") else ("call", int(ev[4:]))
                    inp["strike"] = delta_strike(right, fwd, s, d / 100)  # type: ignore[arg-type]
                probs = {m: event_probability(ev, inp, sig[m]) for m in MODELS}
            else:
                probs = dict.fromkeys(MODELS)
            rows.append({"session": x.day, "underlying": x.symbol, "expiry": e, "expiry_kind": kind, "event": ev,
                         "inputs": inp, "probabilities": probs, "status": "void" if why else "open",
                         "note": f"void: strike unknown, {why}" if why else None})  # fmt: skip
    return rows


def recipe_rows(x: Day, h: Any, nxt: date | None, eday: Any, returns: Sequence[float], ivs: Sequence[float],
                calendar: Any, spec_list: Sequence[Any] | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Each registered replay recipe whose entry window holds the session, as `replay.card_at` builds it (no
    tail book, no path simulation: only the card's own models are logged)."""
    from finresearch.horserace import replay

    rows = []
    for spec in spec_list if spec_list is not None else replay.replay_specs():
        if spec.underlying != x.symbol:
            continue
        c = replay.card_at(spec, x, h, nxt, eday, returns, ivs, calendar=calendar, tail=None, n_paths=1)
        if isinstance(c, str):
            continue  # outside the recipe's entry window, or no card (replay's counted skips)
        probs = {m: (c.forecasts.get(m).p if c.forecasts.get(m) else None) for m in RECIPE_MODELS}  # type: ignore[union-attr]
        rows.append({"session": x.day, "underlying": x.symbol, "expiry": c.expiry, "expiry_kind": "recipe",
                     "event": f"spec:{spec.key}", "inputs": {**c.inputs, "gate_pass": c.gate_pass},
                     "probabilities": probs, "status": "open", "note": None})  # fmt: skip
    return rows


def _logged(s: Session, sym: str, since: date) -> set[date]:
    from finresearch.db.models import ProbPanel

    q = select(ProbPanel.session).where(ProbPanel.underlying == sym, ProbPanel.session >= since).distinct()
    return set(s.scalars(q))


def record_pending(s: Session, history: History, *, start: date | None = None,
                   underlyings: Sequence[str] = UNDERLYINGS, spec_list: Sequence[Any] | None = None) -> int:  # fmt: skip
    """Log every stored session from `start` that has no panel rows yet (idempotent on session, underlying, expiry
    and event). Returns the rows written. Runs in the monitor's forecast step and `finresearch forecasts panel`."""
    from finresearch.backtest.history import Clocked
    from finresearch.backtest.specs import BOOK
    from finresearch.db.models import ProbPanel
    from finresearch.fincalc import bustime as bt
    from finresearch.horserace import replay

    start, n = start or PANEL_START, 0
    for sym in underlyings:
        last = history.latest(sym)
        if last is None or last < start:
            continue
        logged = _logged(s, sym, start)
        todo = [d for d in history.sessions(sym, start, last) if d not in logged]
        if not todo:
            continue
        days = history.sessions(sym, todo[0] - WARMUP, last)
        calendar = bt.Calendar.from_sessions(days)

        def eday(e: date, days: list[date] = days) -> date:
            return e if not days or e > days[-1] else days[bisect_right(days, e) - 1]

        h, prev, returns, ivs, want = Clocked(history), None, [], [], set(todo)
        for i, d in enumerate(days):
            h.advance(d)
            x = h.day(sym, d)
            if x is None:
                continue
            r = replay.same_instrument_return(prev, x) if prev is not None else None
            if r is not None:
                returns.append(r)
            if x.iv30 is not None:
                ivs.append(x.iv30)
            prev = x
            if d not in want:
                continue
            nxt = days[i + 1] if i + 1 < len(days) else None
            rows = tail_rows(x, returns, BOOK.rate) + recipe_rows(
                x, h, nxt, eday, returns, ivs, calendar, spec_list
            )
            for row in rows:
                stmt = (
                    insert(ProbPanel).values(**row).on_conflict_do_nothing(constraint="uq_prob_panel_event")
                )
                n += len(s.scalars(stmt.returning(ProbPanel.id)).all())
    s.flush()
    return n


def settlement(history: History, sym: str, expiry: date) -> tuple[float | None, str]:
    """(S_T, note) on the contract's last stored session; (None, why) when unknown. Raises NotYet until a session on
    or after the expiry is stored (a holiday can move the last session earlier: known only one session later)."""
    from finresearch.backtest import pricing as px
    from finresearch.signals.ledger import NotYet

    latest = history.latest(sym)
    if latest is None or latest < expiry:
        raise NotYet(f"no stored {sym} session on or after {expiry}")
    got = history.sessions(sym, expiry - timedelta(days=10), expiry)
    if not got:
        return None, f"no stored session in the 10 days before {expiry}"
    x = history.day(sym, got[-1])
    st = px.settlement_price(x, expiry) if x is not None else None
    if st is None:
        return None, f"no final settlement price on {got[-1]}"
    # expiry day itself not stored (a holiday, or a missed download): only the contract's last session carries one
    # uniform settle across its options, so anything else is not provably the last session
    if got[-1] != expiry and st[1] != "uniform option settle":
        return (
            None,
            f"{got[-1]} is not provably the contract's last session (expiry day {expiry} is not stored)",
        )
    return st[0], f"settled at {st[1]} {st[0]:,.2f} on {got[-1]}"


def resolve_due(s: Session, history: History, now: datetime) -> dict[str, int]:
    """Resolve every open panel row whose expiry has come and whose outcome is known in the stored data; NotYet rows
    stay open (`open` counts the due ones still waiting)."""
    from finresearch.db.models import ProbPanel
    from finresearch.fnobook.card_forecasts import simulate
    from finresearch.signals.ledger import NotYet

    out = {"resolved": 0, "void": 0, "open": 0}
    cache: dict[tuple[str, date], tuple[float | None, str] | None] = {}
    from finresearch.fincalc.dates import to_ist
    from finresearch.horserace.replay import LruHistory

    history = LruHistory(history)  # recipes on one expiry read the same sessions
    # like the card ledger (resolve_on = the expiry): nothing is tried before its expiry, so the hourly step stays cheap
    due = (ProbPanel.status == "open") & (ProbPanel.expiry <= to_ist(now).date())
    for r in s.scalars(select(ProbPanel).where(due).order_by(ProbPanel.id)):
        try:
            if r.event.startswith("spec:"):
                res = simulate(r.inputs, history)
                val, note, outcome = res.value, res.note, res.outcome
            else:
                key = (r.underlying, r.expiry)
                if key not in cache:
                    try:
                        cache[key] = settlement(history, r.underlying, r.expiry)
                    except NotYet:
                        cache[key] = None
                if cache[key] is None:
                    raise NotYet("waiting for the expiry")
                val, note = cache[key]  # type: ignore[misc]
                outcome = None if val is None else happened(r.event, r.inputs, val)
                note = note if val is not None else f"void: {note}"
        except NotYet:
            out["open"] += 1
            continue
        r.status = "resolved" if outcome is not None else "void"
        r.outcome, r.value, r.note, r.resolved_at = outcome, val, note, now
        out[r.status] += 1
    s.flush()
    return out


# --------------------------------------------------------------------------- scoring (read only)
def _kinds(kind: str) -> list[str]:
    return [k.strip() for k in kind.split(",")]


def first_events(rows: Sequence[Any]) -> dict[tuple[str, str, str], list[Any]]:
    """B1.3 independence: per (underlying, expiry kind, event) the FIRST logged row of each expiry, whatever its
    status (a void first row is counted, never replaced by a later one). A `near, monthly` row counts in both."""
    first: dict[tuple[str, str, str, date], Any] = {}
    for r in sorted(rows, key=lambda r: (r.session, r.id)):
        for k in _kinds(r.expiry_kind):
            first.setdefault((r.underlying, k, r.event, r.expiry), r)
    out: dict[tuple[str, str, str], list[Any]] = defaultdict(list)
    for (u, k, ev, _e), r in sorted(first.items(), key=lambda kv: (kv[0][:3], kv[0][3])):
        out[(u, k, ev)].append(r)
    return out


def _scores(p: list[float], o: list[int]) -> dict[str, Any]:
    from finresearch.evals.calibration import brier, log_loss

    n = len(p)
    return {"n": n, "brier": brier(p, o), "log": log_loss(p, o), "mean_p": sum(p) / n if n else None,
            "base_rate": sum(o) / n if n else None, "claim_allowed": n >= CLAIM_MIN}  # fmt: skip


def pit_histogram(rows: Sequence[Any], model: str, bins: int = 10) -> dict[str, Any]:
    """PIT u = N(z), z = [ln(S_T / F) + sigma^2 T / 2] / (sigma sqrt T), of one resolved row per expiry under `model`;
    rows whose sigma is unknown are counted as `unknown`."""
    counts, unknown = [0] * bins, 0
    for r in rows:
        sig, f, t = (r.inputs.get("sigma") or {}).get(model), r.inputs.get("forward"), r.inputs.get("t")
        if not sig or not f or not t or r.value is None:
            unknown += 1
            continue
        sd = sig * math.sqrt(t)
        u = _N.cdf((math.log(r.value / f) + sd * sd / 2) / sd)
        counts[min(int(u * bins), bins - 1)] += 1
    return {"bins": counts, "n": sum(counts), "unknown": unknown}


def score(s: Session) -> dict[str, Any]:
    """Brier and log score per underlying x expiry kind x event x model on the independent events, a PIT histogram
    of the terminal z per model, and the counts (open, void) behind them. Read only."""
    from finresearch.db.models import ProbPanel

    rows = list(s.scalars(select(ProbPanel)))
    groups, pits = [], []
    for (u, kind, ev), fs in sorted(first_events(rows).items()):
        done = [r for r in fs if r.status == "resolved" and r.outcome is not None]
        for m in RECIPE_MODELS if ev.startswith("spec:") else MODELS:
            known = [r for r in done if (r.probabilities or {}).get(m) is not None]
            sc = _scores([r.probabilities[m] for r in known], [int(r.outcome) for r in known])
            groups.append({"underlying": u, "expiry_kind": kind, "event": ev, "model": m, **sc,
                           "events": len(fs), "open": sum(r.status == "open" for r in fs),
                           "void": sum(r.status == "void" for r in fs),
                           "unknown_p": len(done) - len(known)})  # fmt: skip
        if (
            ev == "put25"
        ):  # one row per expiry carries the terminal z (every strike event shares F, sigma, S_T)
            for m in MODELS:
                pits.append({"underlying": u, "expiry_kind": kind, "model": m, **pit_histogram(done, m)})
    sessions = sorted({r.session for r in rows})
    return {"start": PANEL_START.isoformat(), "rows": len(rows), "sessions": len(sessions),
            "first_session": sessions[0].isoformat() if sessions else None,
            "last_session": sessions[-1].isoformat() if sessions else None,
            "claim_min": CLAIM_MIN, "groups": groups, "pit": pits}  # fmt: skip
