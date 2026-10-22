"""The 2022-26 card replay of the horse race (#377): one card per stored session for each registered index spec,
built from the bhavcopy at the close, forecast by the card's models and resolved by the card ledger's exit replay.
Registered in horserace/specs.py (`Replay`), post-hoc addendum A1 (horserace/addendum.py, ADDENDUM.md) and, for the
business-time challenger p_rw_bus (#384), addendum A2 (horserace/addendum_a2.py) and, for the Black-76 forward/IV
challenger p_rn_b76 (#385), addendum A3 (horserace/addendum_a3.py) and, for the SVI density challenger p_rn_svi
(#386), addendum A4 (horserace/addendum_a4.py), and for the exit-rule Monte Carlo p_path (#388), addendum A6
(horserace/addendum_a6.py); the item numbers below are the addenda's.

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
from finresearch.fincalc import black76 as b76
from finresearch.fincalc import bustime as bt
from finresearch.fincalc import charges as ch
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import options as o
from finresearch.fincalc import smile as sm
from finresearch.fincalc import svi
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc import volatility as vol
from finresearch.fincalc.dates import ist_datetime
from finresearch.fnobook import path_pop as pp
from finresearch.horserace import (
    addendum,
    addendum_a2,
    addendum_a3,
    addendum_a4,
    addendum_a6,
    prereg,
    race,
    specs,
)
from finresearch.horserace import scores as sc
from finresearch.horserace.race import Forecast

WARMUP_DAYS = 400  # A1.7, the engine's
DOTS = [(i + 0.5) / 20 for i in range(20)]


def require_registered() -> str:
    """The registration (specs.py) and addenda A1-A4 and A6 must each match their pinned hashes."""
    race.require_registered()
    h = addendum.addendum_hash()
    if h != prereg.ADDENDUM_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum.py hashes to {h[:12]}… but the pinned addendum hash is {prereg.ADDENDUM_HASH[:12]}…: "
            "the replay is refused. Record a changed addendum as a new ADDENDUM_VERSION and pin its hash first."
        )
    h2 = addendum_a2.addendum_hash()
    if h2 != prereg.ADDENDUM_A2_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum_a2.py (with fincalc/bustime's registered weights) hashes to {h2[:12]}… but the "
            f"pinned hash is {prereg.ADDENDUM_A2_HASH[:12]}…: the replay is refused. Register a new addendum first."
        )
    h3 = addendum_a3.addendum_hash()
    if h3 != prereg.ADDENDUM_A3_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum_a3.py (with fincalc/black76's registered constants) hashes to {h3[:12]}… but the "
            f"pinned hash is {prereg.ADDENDUM_A3_HASH[:12]}…: the replay is refused. Register a new addendum first."
        )
    h4 = addendum_a4.addendum_hash()
    if h4 != prereg.ADDENDUM_A4_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum_a4.py (with fincalc/svi's and smile's registered settings) hashes to {h4[:12]}… but "
            f"the pinned hash is {prereg.ADDENDUM_A4_HASH[:12]}…: the replay is refused. Register a new addendum first."
        )
    h6 = addendum_a6.addendum_hash()
    if h6 != prereg.ADDENDUM_A6_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum_a6.py (with the simulation settings and fnobook/path_pop's constants) hashes to "
            f"{h6[:12]}… but the pinned hash is {prereg.ADDENDUM_A6_HASH[:12]}…: the replay is refused. Register a new "
            "addendum first."
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


def market_settles(x: Day, expiry: date) -> dict[tuple[str, float], float]:
    """A3.2: (right, strike) -> settle for the expiry's options whose settle is a market price."""
    return {(r, k): q.settle for (e, r, k), q in x.options.items()
            if e == expiry and q.settle and fh.market_settle(q.settle, q.close, q.contracts)}  # fmt: skip


def svi_smile(x: Day, expiry: date, t: float, b3: dict[str, Any]) -> dict[str, Any]:
    """A4.2-A4.4: the raw-SVI smile of the expiry through the market settles' out-of-the-money IVs at A3's parity
    forward and discount factor; `params` None with `reason` when any step is unknown or the fit is refused."""
    out: dict[str, Any] = {"params": None, "n": 0, "rmse_vol": None, "flags": [], "atm": None, "rr25": None,
                           "bf25": None, "dropped": {}, "reason": None}  # fmt: skip
    if b3.get("forward") is None or b3.get("df") is None:
        return out | {"reason": f"no parity forward or discount factor: {b3.get('reason')}"}
    quotes = [b76.OptionQuote(k, r, price=p) for (r, k), p in market_settles(x, expiry).items()]  # type: ignore[arg-type]
    got = svi.fit_quotes(quotes, b3["forward"], t, b3["df"])
    f = got.fit
    out |= {"n": f.n, "rmse_vol": f.rmse_vol, "flags": list(f.flags), "dropped": got.dropped}
    if f.params is None:
        return out | {"reason": f"no smile: {f.reason}"}
    rb = sm.rr_bf(f.params, t)
    return out | {"params": vars(f.params).copy(), "atm": rb.atm, "rr25": rb.rr, "bf25": rb.bf}


def b76_atm(x: Day, expiry: date, t: float, ref_forward: float | None = None) -> dict[str, Any]:
    """A3.2-A3.6: the parity-regression forward and discount factor of the expiry over strikes whose call and put
    both have a market settle, then the ATM Black-76 IV at the strike nearest it; `sigma` None with `reason` when
    any step is unknown. `basis` is to `ref_forward` (the A1.5 forward; default the same-expiry future)."""
    settles = market_settles(x, expiry)
    strikes = sorted({k for (_, k) in settles if ("call", k) in settles and ("put", k) in settles})
    fit = b76.parity_fit([b76.ParityQuote(k, settles[("call", k)], settles[("put", k)]) for k in strikes], t)
    ref = ref_forward if ref_forward is not None else x.futures.get(expiry)
    out: dict[str, Any] = {"forward": fit.forward, "df": fit.df, "r_impl": fit.r_impl, "se_forward": fit.se_forward,
                           "n": fit.n, "strikes": list(fit.strikes), "basis": b76.basis(fit.forward, ref),
                           "atm_strike": None, "sigma": None, "reason": None}  # fmt: skip
    if fit.forward is None:
        return out | {"reason": f"no parity forward: {fit.reason}"}
    if fit.df is None:
        return out | {"reason": f"discount factor unknown: {fit.df_reason}"}
    quotes = [b76.OptionQuote(k, r, price=p) for (r, k), p in settles.items()]  # type: ignore[arg-type]
    atm = b76.atm_iv(quotes, fit.forward, t, fit.df)
    return out | {"atm_strike": atm.strike, "sigma": atm.sigma,
                  "reason": None if atm.sigma is not None else f"no ATM IV: {atm.reason}"}  # fmt: skip


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
    t_bus: float | None = None  # A2.2: business years, origin close to expiry close; None = unknown
    b76: dict[str, Any] | None = (
        None  # A3.6: the parity forward, DF, ATM IV and why unknown (logged, not scored)
    )
    svi: dict[str, Any] | None = None  # A4.4: the smile's parameters, fit and RR/BF, or why unknown (logged)
    path: dict[str, Any] | None = (
        None  # A6.7: p_path's Monte Carlo error, exit-reason shares, beta, why unknown
    )


def _exit_cost(legs: Sequence[o.Leg], expiry: date):
    longs = [x for x in legs if x.qty > 0]

    def exit_cost(p: float) -> float:  # STT on long options exercised in the money (the card's)
        return ch.exercise_stt(sum(x.qty * (max(0.0, p - x.strike) if x.right == "call" else max(0.0, x.strike - p))
                                   for x in longs), expiry)  # fmt: skip

    return exit_cost


def svi_forecast(legs: Sequence[o.Leg], expiry: date, spot: float, t: float, rate: float, entry_cost: float,
                 params: dict[str, float] | None) -> Forecast:  # fmt: skip
    """A4.1: PoP, EV and 20 dots under the smile's density around the A1.5 forward; unknown smile, unknown forecast."""
    if params is None:
        return Forecast(None)
    p, exit_cost = svi.Raw(**params), _exit_cost(legs, expiry)
    oc = sm.outcome(legs, spot, t, rate, p, entry_cost, exit_cost)
    dist = sm.pnl_distribution(legs, spot, t, rate, p, entry_cost, exit_cost, 2000)
    return Forecast(oc.pop, oc.ev, [round(v, 2) for v in o.weighted_quantiles(dist, DOTS)])


def models(legs: Sequence[o.Leg], expiry: date, spot: float, t: float, rate: float, entry_cost: float,
           sigmas: dict[str, float | None]) -> dict[str, Forecast]:  # fmt: skip
    """A1.6, A1.9: PoP and EV (fincalc.options.outcome) and 20 P&L dots per volatility; p_rw = the lower-EV of the
    realised-volatility cases. An unknown volatility gives an unknown forecast."""
    exit_cost = _exit_cost(legs, expiry)
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


def bus_sigmas(sigmas: dict[str, float | None], t_bus: float | None, t: float) -> dict[str, float | None]:
    """A2.1: each volatility rescaled so that sigma_eff^2 x t = sigma^2 x T_bus (unknown T_bus: every case unknown)."""
    return {k: bt.rescale_vol(v, t_bus, t) for k, v in sigmas.items()}


def path_forecast(spec: Spec, origin: date, inputs: dict[str, Any], picked: Sequence[tuple[Any, str]], fwd: float,
                  t: float, rate: float, fc: dict[str, Forecast], sig: dict[str, float | None],
                  pairs: Sequence[tuple[float, float]], sessions: Sequence[date], n_paths: int,
                  last: date | None = None) -> tuple[Forecast, dict[str, Any]]:  # fmt: skip
    """A6: the exit-rule Monte Carlo at the incumbent's volatility case, or Forecast(None) with the reason. `last`:
    the expiry's last session; a calendar that does not reach it cannot settle a path on the right day."""
    last = last or (sessions[-1] if sessions else None)
    seed = addendum_a6.card_seed(spec.key, origin)
    out: dict[str, Any] = {"n": n_paths, "seed": seed, "sigma_case": None, "beta": None, "reason": None}
    case = next((k for k in ("p_rw20", "p_rw60") if fc.get(k) is fc["p_rw"] and fc[k].p is not None), None)
    beta = pp.spot_vol_beta(pairs)
    ivs = [px.leg_iv(q, fwd, t, rate) for q, _ in picked]
    why = ("sigma unknown: no realised-volatility case" if case is None else
           f"beta unknown: fewer than {pp.BETA_MIN} paired IV and price changes" if beta is None else
           "a leg's origin IV is unknown" if any(v is None for v in ivs) else
           "no stored session before expiry" if not sessions else
           "stored calendar ends before the expiry's last session" if sessions[-1] != last else None)  # fmt: skip
    out.update(sigma_case=case, beta=beta)
    if why:
        return Forecast(None), out | {"reason": why}
    legs = [pp.PathLeg(q.right, q.strike, side, lg["entry"], iv, q.contracts)  # type: ignore[arg-type]
            for (q, side), lg, iv in zip(picked, inputs["legs"], ivs, strict=True)]  # fmt: skip
    mc = pp.monte_carlo(inputs, legs, sessions, f0=fwd, sigma=sig[case], beta=beta, rate=rate,  # type: ignore[arg-type,index]
                        n_paths=n_paths, seed=seed)  # fmt: skip
    out |= {"pop_se": mc.pop_se, "pop_ci": mc.pop_ci, "ev_se": mc.ev_se, "reasons": mc.reasons,
            "reasons_se": mc.reasons_se, "void": mc.void, "reason": None if mc.pop is not None else
            f"a path was void: {mc.void_reason}"}  # fmt: skip
    return (Forecast(mc.pop, mc.ev, mc.dots) if mc.pop is not None else Forecast(None)), out


def card_at(spec: Spec, x: Day, h: Clocked, nxt: date | None, eday: Any, returns: Sequence[float],
            ivs: Sequence[float], book: Book = BOOK, calendar: bt.Calendar | None = None,
            pairs: Sequence[tuple[float, float]] = (), sessions: Sequence[date] = (),
            n_paths: int = addendum_a6.N_PATHS) -> Card | str:  # fmt: skip
    """One card at this close (A1.1, A1.3-A1.10, A6), or the reason there is none. `pairs`: the (return, iv30
    change) pairs up to this close (A6.4); `sessions`: the stored sessions after it (A6.6)."""
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
    spot = px.discounted_spot(fwd, t, rate)
    fc = models(legs, expiry, spot, t, rate, entry_cost, sig)
    # A2: the business-time challenger, the same lower-EV rule over the RV cases on the registered prior clock
    close = ist_datetime(d, *bt.SESSION_CLOSE)
    t_bus = bt.business_years(expiry, close, calendar, bt.PRIOR, events=bt.EXCLUDED).value
    rw = bus_sigmas({k: sig[k] for k in ("p_rw20", "p_rw60")}, t_bus, t)
    fc[addendum_a2.CHALLENGER] = models(legs, expiry, spot, t, rate, entry_cost, rw)["p_rw"]
    # A3: the forward/IV challenger, A1.6's inputs with the ATM IV from the parity forward and Black-76 engine
    b3 = b76_atm(x, expiry, t, fwd)
    fc[addendum_a3.CHALLENGER] = models(legs, expiry, spot, t, rate, entry_cost, {"p_rn_atm": b3["sigma"]})[
        "p_rn_atm"
    ]
    # A4: the SVI density challenger, A1.6's inputs with the terminal distribution from the expiry's fitted smile
    s4 = svi_smile(x, expiry, t, b3)
    fc[addendum_a4.CHALLENGER] = svi_forecast(legs, expiry, spot, t, rate, entry_cost, s4["params"])
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
    # A6: the plan's own exits on simulated paths, at the volatility the incumbent chose
    last = eday(expiry)
    path_days = [s for s in sessions if d < s <= last]
    fc[addendum_a6.CHALLENGER], mc = path_forecast(spec, d, inputs, picked, fwd, t, rate, fc, sig, pairs, path_days,
                                                   n_paths, last)  # fmt: skip
    return Card(spec.key, sym, spec.kind, expiry, d, inputs, fc, gate, t_bus, b3, s4, mc)


def make_cards(history: History, start: date, end: date, spec_list: Sequence[Spec] | None = None,
               book: Book = BOOK, n_paths: int = addendum_a6.N_PATHS) -> tuple[list[Card], Counter]:  # fmt: skip
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
        calendar = bt.Calendar.from_sessions(days)  # A2.5: the stored sessions, as eday below

        def eday(e: date, days: list[date] = days) -> date:
            return e if not days or e > days[-1] else days[bisect_right(days, e) - 1]

        prev: Day | None = None
        returns: list[float] = []
        ivs: list[float] = []
        pairs: list[
            tuple[float, float]
        ] = []  # A6.4: (return, iv30 change / 100) on sessions where both are known
        for i, d in enumerate(days):
            h.advance(d)
            x = h.day(sym, d)
            if x is None:
                continue
            r = same_instrument_return(prev, x) if prev is not None else None
            if r is not None:
                returns.append(r)
                if prev is not None and prev.iv30 is not None and x.iv30 is not None:
                    pairs.append((r, (x.iv30 - prev.iv30) / 100))
            if x.iv30 is not None:
                ivs.append(x.iv30)
            prev = x
            if not start <= d <= end:
                continue
            nxt = days[i + 1] if i + 1 < len(days) else None
            for spec in group:
                c = card_at(
                    spec, x, h, nxt, eday, returns, ivs, book, calendar, pairs, days[i + 1 :], n_paths
                )
                if isinstance(c, str):
                    skips[f"{spec.key}: {c}"] += 1
                else:
                    cards.append(c)
    return cards, skips


# --------------------------------------------------------------------------- pass 2: outcomes
def resolve(cards: Sequence[Card], history: History) -> tuple[list[race.Event], Counter]:
    """A1.2, A1.11, A1.12: each card's set replayed by fnobook.card_forecasts.simulate; void and unfinished cards are
    counted, never events. `first` = the earliest resolved card per (underlying, expiry, structure)."""
    from finresearch.fnobook.card_forecasts import simulate
    from finresearch.signals.ledger import NotYet

    cached = LruHistory(history)
    counts: Counter = Counter()
    done: list[tuple[Card, Any]] = []
    for c in sorted(cards, key=lambda c: (c.origin, c.spec)):
        try:
            r = simulate(c.inputs, cached)
        except NotYet:
            counts["not yet"] += 1
            continue
        if r.outcome is None or r.value is None:
            counts["void"] += 1
            continue
        done.append((c, r))
    seen: set[tuple[str, date, str]] = set()
    events = []
    for c, r in done:
        key = (c.underlying, c.expiry, c.structure)
        events.append(race.Event(underlying=c.underlying, expiry=c.expiry, structure=c.structure, origin=c.origin,
                                 resolved_on=c.expiry, outcome=int(r.outcome), pnl=float(r.value),
                                 gate_pass=c.gate_pass, first=key not in seen, forecasts=dict(c.forecasts),
                                 exit_reason=r.note.split(" on ")[0] if r.note else None))  # fmt: skip
        seen.add(key)
    counts["events"] = len(events)
    return events, counts


def event_key(e: race.Event | Card) -> tuple[str, str, date, date]:
    """The card an event came from: (underlying, structure, expiry, origin)."""
    return e.underlying, e.structure, e.expiry, e.origin


def exit_reason_score(events: Sequence[race.Event], shares: dict[tuple, dict[str, float]],
                      window: str | None = None) -> dict[str, Any]:  # fmt: skip
    """A6.8: the multinomial log score (eps 1e-6) of the forecast exit-reason shares against trailing Dirichlet(1)
    frequencies over the four reason groups of events of the same underlying and structure resolved strictly before
    the origin (every event given, as race.with_trailing), scoring only the events in `window` (all when None) whose
    shares are known. Descriptive; lower is better."""
    scored = {id(e) for e in (race.in_window(events, window) if window else events)}
    cats = sorted(set(pp.REASONS.values()))
    model, base, seen = [], [], []
    for e in sorted(events, key=lambda e: e.origin):
        got = shares.get(event_key(e))
        if id(e) not in scored or not got or e.exit_reason is None:
            continue
        prior = [r for r in events if (r.underlying, r.structure) == (e.underlying, e.structure)
                 and r.resolved_on < e.origin and r.exit_reason is not None]  # fmt: skip
        counts = {c: 1 + sum(1 for r in prior if pp.REASONS.get(r.exit_reason) == c) for c in cats}
        total = sum(counts.values())
        model.append(got)
        base.append({c: k / total for c, k in counts.items()})
        seen.append(pp.REASONS.get(e.exit_reason, e.exit_reason))
    if not seen:
        return {"n": 0, "model": None, "base": None, "model_minus_base": None}
    m, b = sc.multinomial_log_score(model, seen), sc.multinomial_log_score(base, seen)
    return {"n": len(seen), "model": m, "base": b, "model_minus_base": m - b,
            "observed": dict(Counter(seen))}  # fmt: skip


class LruHistory:
    """A bounded cache of `day()` over another history: pass 2 reads each session once per open card, oldest
    origins first, so the last ~`size` sessions cover almost every read (DbHistory loads a whole session per call)."""

    def __init__(self, inner: History, size: int = 96) -> None:
        from collections import OrderedDict

        self.inner, self.size = inner, size
        self.cache: OrderedDict[tuple[str, date], Day | None] = OrderedDict()

    def day(self, symbol: str, d: date) -> Day | None:
        k = (symbol, d)
        if k in self.cache:
            self.cache.move_to_end(k)
            return self.cache[k]
        x = self.cache[k] = self.inner.day(symbol, d)
        if len(self.cache) > self.size:
            self.cache.popitem(last=False)
        return x

    def __getattr__(self, name: str) -> Any:  # sessions, latest, lot, iv_history, span_params
        return getattr(self.inner, name)


# --------------------------------------------------------------------------- the race on a replay window
def run_window(history: History, window: str, spec_list: Sequence[Spec] | None = None,
               n_paths: int = addendum_a6.N_PATHS) -> dict[str, Any]:  # fmt: skip
    """Cards from REPLAY.start (2022-04-01) to the window's end (else the latest stored session), resolved, then the
    registered race on the window (trailing rates see every earlier resolved card). Read only."""
    if window == "forward":
        raise ValueError("the forward window is registered as card-ledger events: use the ledger source")
    w = specs.WINDOWS.get(window)
    if w is None:
        raise ValueError(
            f"unknown window {window!r}: the replay serves design, seen and tuesday (addendum A1.13)"
        )
    start = date.fromisoformat(specs.REPLAY.start)
    end = date.fromisoformat(w["end"]) if w["end"] else date.max
    cards, skips = make_cards(history, start, end, spec_list, n_paths=n_paths)
    events, counts = resolve(cards, history)
    rep = race.run(events, window, challengers=addendum_a6.CHALLENGERS)
    # A6.7, A6.8: p_path's simulation error and exit-reason score on the window's events
    xs = race.in_window(events, window)
    by_card = {event_key(c): c.path for c in cards if c.path and c.path.get("reason") is None}
    known = [
        (by_card[k], e.forecasts[addendum_a6.CHALLENGER].p) for e in xs if (k := event_key(e)) in by_card
    ]
    ses = [mc["pop_se"] for mc, _ in known]
    mc_report = {"events": len(known), "unknown": len(xs) - len(known),
                 "mean_se": sum(ses) / len(ses) if ses else None, "max_se": max(ses, default=None),
                 "brier_noise": sum(p * (1 - p) / mc["n"] for mc, p in known) / len(known) if known else None,
                 "reasons": dict(Counter(c.path["reason"].split(":")[0] for c in cards
                                         if c.path and c.path.get("reason")))}  # fmt: skip
    gaps: dict[str, int | None] = {}  # A2.5: weekdays without a stored session (holidays and any missing day)
    for sym in sorted({s.underlying for s in spec_list or replay_specs()}):
        last = history.latest(sym)
        got = history.sessions(sym, start - timedelta(days=WARMUP_DAYS), last) if last else []
        cal = bt.Calendar.from_sessions(got)
        gaps[sym] = len(cal.holidays) if cal else None
    rep["replay"] = {"addendum": addendum.ADDENDUM_VERSION, "addendum_hash": addendum.addendum_hash(),
                     "cards": len(cards), "counts": dict(counts), "skips": dict(skips),
                     "addendum_a2": addendum_a2.ADDENDUM_VERSION, "addendum_a2_hash": addendum_a2.addendum_hash(),
                     "addendum_a3": addendum_a3.ADDENDUM_VERSION, "addendum_a3_hash": addendum_a3.addendum_hash(),
                     "p_rn_b76_unknown": sum(1 for c in cards if not c.b76 or c.b76["sigma"] is None),
                     "p_rn_b76_reasons": dict(Counter(c.b76["reason"].split(":")[0] for c in cards
                                                      if c.b76 and c.b76["reason"])),
                     "addendum_a4": addendum_a4.ADDENDUM_VERSION, "addendum_a4_hash": addendum_a4.addendum_hash(),
                     "p_rn_svi_unknown": sum(1 for c in cards if not c.svi or c.svi["params"] is None),
                     "p_rn_svi_reasons": dict(Counter(": ".join(c.svi["reason"].split(": ")[:2]) for c in cards
                                                      if c.svi and c.svi["reason"])),
                     "addendum_a6": addendum_a6.ADDENDUM_VERSION, "addendum_a6_hash": addendum_a6.addendum_hash(),
                     "n_paths": n_paths, "p_path_mc": mc_report,
                     "exit_reasons": exit_reason_score(events, {k: v["reasons"] for k, v in by_card.items()}, window),
                     "calendar_gap_weekdays": gaps, "t_bus_unknown": sum(1 for c in cards if c.t_bus is None),
                     "first_origin": min((c.origin for c in cards), default=None),
                     "last_origin": max((c.origin for c in cards), default=None)}  # fmt: skip
    return rep
