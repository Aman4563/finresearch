"""The 2022-26 card replay of the horse race (#377): one card per stored session for each registered index spec,
built from the bhavcopy at the close, forecast by the card's models and resolved by the card ledger's exit replay.
Registered in horserace/specs.py (`Replay`) and post-hoc addendum A1 (horserace/addendum.py, ADDENDUM.md); the item
numbers below are the addendum's.

Two passes. (1) Forecasting, oldest session first, through `backtest.history.Clocked` advanced to the origin close,
so a card can read nothing later than its origin (LookAheadError otherwise): expiry (A1.4), legs (A1.5), fills
(A1.1), RV20/RV60 on same-instrument returns (A1.7), ATM IV (A1.8), PoP/EV/dots per model (A1.6, A1.9) and the gate
proxy (A1.10). (2) Outcomes on the raw history with `fnobook.card_forecasts.simulate` (A1.2, A1.11): void and
unfinished cards are counted, never events. Nothing here writes to the database.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from finresearch.backtest import engine
from finresearch.backtest import pricing as px
from finresearch.backtest.history import Clocked, Day, History
from finresearch.backtest.specs import BOOK, SPECS, Book, Spec
from finresearch.fincalc import charges as ch
from finresearch.fincalc import options as o
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc import volatility as vol
from finresearch.horserace import addendum, prereg, race, specs
from finresearch.horserace.race import Forecast

WARMUP_DAYS = 400  # A1.7, the engine's
DOTS = [(i + 0.5) / 20 for i in range(20)]


def require_registered() -> str:
    """Both the registration (specs.py) and addendum A1 must match their pinned hashes."""
    race.require_registered()
    h = addendum.addendum_hash()
    if h != prereg.ADDENDUM_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum.py hashes to {h[:12]}… but the pinned addendum hash is {prereg.ADDENDUM_HASH[:12]}…: "
            "the replay is refused. Record a changed addendum as a new ADDENDUM_VERSION and pin its hash first."
        )
    return h


def replay_specs() -> list[Spec]:
    return [s for s in SPECS if s.key in specs.REPLAY.specs]


# --------------------------------------------------------------------------- market inputs at the origin close
def same_instrument_return(a: Day, b: Day) -> float | None:
    """A1.7: ln(b / a) on the underlying value when both sessions have it, else on the nearest future present on
    both; None when neither (never a splice across instruments)."""
    if a.spot and b.spot:
        return math.log(b.spot / a.spot)
    common = sorted(e for e in a.futures if e in b.futures and a.futures[e] and b.futures[e])
    return math.log(b.futures[common[0]] / a.futures[common[0]]) if common else None


def rv(returns: Sequence[float], n: int) -> float | None:
    """Sample sd of the last n returns x sqrt(252); None with fewer than n."""
    if len(returns) < n:
        return None
    r = list(returns)[-n:]
    m = sum(r) / n
    return math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1) * 252)


def atm_iv(x: Day, expiry: date, fwd: float, t: float, rate: float) -> float | None:
    """A1.8: the mean Black-76 IV of the call and put at the listed strike nearest the forward (either alone)."""
    strikes = {k for (e, _, k) in x.options if e == expiry}
    if not strikes:
        return None
    k = min(strikes, key=lambda s: (abs(s - fwd), s))
    ivs = [iv for r in ("call", "put") if (q := x.options.get((expiry, r, k))) is not None
           and (iv := px.leg_iv(q, fwd, t, rate)) is not None]  # fmt: skip
    return sum(ivs) / len(ivs) if ivs else None


@dataclass(frozen=True)
class Card:
    spec: str
    underlying: str
    structure: str
    expiry: date
    origin: date
    inputs: dict[str, Any]  # fnobook.card_forecasts.simulate's inputs (one set)
    forecasts: dict[str, Forecast]
    gate_pass: bool | None


def models(legs: Sequence[o.Leg], expiry: date, spot: float, t: float, rate: float, entry_cost: float,
           sigmas: dict[str, float | None]) -> dict[str, Forecast]:  # fmt: skip
    """A1.6, A1.9: PoP and EV (fincalc.options.outcome) and 20 P&L dots per volatility; p_rw = the lower-EV of the
    realised-volatility cases. An unknown volatility gives an unknown forecast."""
    longs = [x for x in legs if x.qty > 0]

    def exit_cost(p: float) -> float:  # STT on long options exercised in the money (the card's)
        return ch.exercise_stt(sum(x.qty * (max(0.0, p - x.strike) if x.right == "call" else max(0.0, x.strike - p))
                                   for x in longs), expiry)  # fmt: skip

    out: dict[str, Forecast] = {}
    for key, sigma in sigmas.items():
        if not sigma:
            out[key] = Forecast(None)
            continue
        oc = o.outcome(legs, spot, t, sigma, rate, rate, entry_cost, exit_cost, steps=4000)
        dist = o.pnl_distribution(legs, spot, t, sigma, rate, entry_cost, exit_cost, 2000)
        out[key] = Forecast(oc.pop, oc.ev, [round(v, 2) for v in o.weighted_quantiles(dist, DOTS)])
    rws = [out[k] for k in ("p_rw20", "p_rw60") if out.get(k) and out[k].p is not None]
    out["p_rw"] = min(rws, key=lambda f: f.ev) if rws else Forecast(None)  # type: ignore[arg-type,return-value]
    return out


def card_at(spec: Spec, x: Day, h: Clocked, nxt: date | None, eday: Any, returns: Sequence[float],
            ivs: Sequence[float], book: Book = BOOK) -> Card | str:  # fmt: skip
    """One card at this close (A1.1, A1.3-A1.10), or the reason there is none."""
    d, sym, rate = x.day, x.symbol, book.rate
    exps = [e for e in x.expiries() if spec.dte_min <= (e - d).days <= spec.dte_max
            and (spec.expiry_kind == "any" or x.is_monthly(e))]  # fmt: skip
    if not exps:
        return "no expiry in the DTE window"
    expiry = exps[0]
    if not spec.hold_to_expiry and nxt is not None and nxt >= eday(expiry):
        return "no session left before expiry day"
    fwd = px.forward(x, expiry, rate)
    if fwd is None:
        return "no forward"
    picked = engine.choose_legs(spec, x, expiry, fwd, book)
    if isinstance(picked, str):
        return picked
    if any(q.settle is None or q.settle <= 0 for q, _ in picked):
        return "no settlement price for a leg"
    lot = h.lot(sym, expiry, d)
    if not lot:
        return "lot size unknown"
    t = (expiry - d).days / 365
    fills = [(q, side, px.fill(q.settle, side, q.contracts, book)) for q, side in picked]  # type: ignore[arg-type]
    legs = [o.Leg(q.right, q.strike, f, lot if side == "buy" else -lot) for q, side, f in fills]  # type: ignore[arg-type]
    entry_cost = ch.order_costs([ch.TradeLeg("option", side, f, lot) for _, side, f in fills], d,  # type: ignore[arg-type]
                                book.brokerage_per_order).total  # fmt: skip
    sig = {"p_rw20": rv(returns, 20), "p_rw60": rv(returns, 60), "p_rn_atm": atm_iv(x, expiry, fwd, t, rate)}
    fc = models(legs, expiry, px.discounted_spot(fwd, t, rate), t, rate, entry_cost, sig)
    plan = tp.spec_exits(spec)
    stats = vol.iv_stats(ivs) if x.iv30 is not None else None
    pct = stats.percentile if stats else None
    gate = None if spec.iv_pct_min is None or pct is None else pct >= spec.iv_pct_min
    inputs = {"spec": spec.key, "symbol": sym, "expiry": expiry.isoformat(), "structure": spec.kind, "lot": lot,
              "entry_day": d.isoformat(), "start_date": d.isoformat(), "unknown": {},
              "legs": [{"right": q.right, "strike": q.strike, "side": side, "lots": 1, "entry": f}
                       for q, side, f in fills],
              "exit_plan": {"profit_pct": plan.profit_pct, "stop_pct": plan.stop_pct, "exit_dte": plan.exit_dte,
                            "hold_to_expiry": plan.hold_to_expiry}}  # fmt: skip
    return Card(spec.key, sym, spec.kind, expiry, d, inputs, fc, gate)


def make_cards(history: History, start: date, end: date, spec_list: Sequence[Spec] | None = None,
               book: Book = BOOK) -> tuple[list[Card], Counter]:  # fmt: skip
    """Pass 1: every session from `start` to `end` (or the latest stored), one card per spec, oldest first."""
    from bisect import bisect_right

    cards: list[Card] = []
    skips: Counter = Counter()
    groups: dict[str, list[Spec]] = {}
    for s in spec_list or replay_specs():
        groups.setdefault(s.underlying, []).append(s)
    for sym, group in groups.items():
        h = Clocked(history)
        last = history.latest(sym)
        days = h.sessions(sym, start - timedelta(days=WARMUP_DAYS), last) if last else []

        def eday(e: date, days: list[date] = days) -> date:
            return e if not days or e > days[-1] else days[bisect_right(days, e) - 1]

        prev: Day | None = None
        returns: list[float] = []
        ivs: list[float] = []
        for i, d in enumerate(days):
            h.advance(d)
            x = h.day(sym, d)
            if x is None:
                continue
            r = same_instrument_return(prev, x) if prev is not None else None
            if r is not None:
                returns.append(r)
            if x.iv30 is not None:
                ivs.append(x.iv30)
            prev = x
            if not start <= d <= end:
                continue
            nxt = days[i + 1] if i + 1 < len(days) else None
            for spec in group:
                c = card_at(spec, x, h, nxt, eday, returns, ivs, book)
                if isinstance(c, str):
                    skips[f"{spec.key}: {c}"] += 1
                else:
                    cards.append(c)
    return cards, skips
