"""The 2022-26 card replay of the horse race (#377): one card per stored session for each registered index spec,
built from the bhavcopy at the close, forecast by the card's models and resolved by the card ledger's exit replay.
Registered in horserace/specs.py (`Replay`), post-hoc addendum A1 (horserace/addendum.py, ADDENDUM.md) and, for the
business-time challenger p_rw_bus (#384), addendum A2 (horserace/addendum_a2.py) and, for the Black-76 forward/IV
challenger p_rn_b76 (#385), addendum A3 (horserace/addendum_a3.py) and, for the tail-aware challengers p_rw_emp and
p_rw_fhs (#387), addendum A5 (horserace/addendum_a5.py); the item numbers below are the addenda's.

Two passes. (1) Forecasting, oldest session first, through `backtest.history.Clocked` advanced to the origin close,
so a card can read nothing later than its origin (LookAheadError otherwise): expiry (A1.4), legs (A1.5), fills
(A1.1), RV20/RV60 on same-instrument returns (A1.7), ATM IV (A1.8), PoP/EV/dots per model (A1.6, A1.9) and the gate
proxy (A1.10); a TailBook per underlying keeps the A5.1 z sample and the A5.4 GJR fit from the sessions already
read. (2) Outcomes on the raw history with `fnobook.card_forecasts.simulate` (A1.2, A1.11): void and
unfinished cards are counted, never events. Nothing here writes to the database.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import numpy as np

from finresearch.backtest import engine
from finresearch.backtest import pricing as px
from finresearch.backtest.history import Clocked, Day, History
from finresearch.backtest.specs import BOOK, SPECS, Book, Spec
from finresearch.fincalc import black76 as b76
from finresearch.fincalc import bustime as bt
from finresearch.fincalc import charges as ch
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import options as o
from finresearch.fincalc import taildist as td
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc import volatility as vol
from finresearch.fincalc.dates import ist_datetime
from finresearch.horserace import addendum, addendum_a2, addendum_a3, addendum_a5, prereg, race, specs
from finresearch.horserace.race import Forecast

WARMUP_DAYS = 400  # A1.7, the engine's
DOTS = [(i + 0.5) / 20 for i in range(20)]


def require_registered() -> str:
    """The registration (specs.py) and addenda A1, A2, A3 and A5 must each match their pinned hashes."""
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
    h5 = addendum_a5.addendum_hash()
    if h5 != prereg.ADDENDUM_A5_HASH:
        raise race.PreregistrationError(
            f"horserace/addendum_a5.py (with fincalc/taildist's fitted settings) hashes to {h5[:12]}… but the "
            f"pinned hash is {prereg.ADDENDUM_A5_HASH[:12]}…: the replay is refused. Register a new addendum first."
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


def b76_atm(x: Day, expiry: date, t: float, ref_forward: float | None = None) -> dict[str, Any]:
    """A3.2-A3.6: the parity-regression forward and discount factor of the expiry over strikes whose call and put
    both have a market settle, then the ATM Black-76 IV at the strike nearest it; `sigma` None with `reason` when
    any step is unknown. `basis` is to `ref_forward` (the A1.5 forward; default the same-expiry future)."""
    settles = {(r, k): q.settle for (e, r, k), q in x.options.items()
               if e == expiry and q.settle and fh.market_settle(q.settle, q.close, q.contracts)}  # fmt: skip
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
    tail: dict[str, Any] | None = (
        None  # A5.5: bucket, z count, kappa, ES97.5 and why unknown (logged, not scored)
    )


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


def bus_sigmas(sigmas: dict[str, float | None], t_bus: float | None, t: float) -> dict[str, float | None]:
    """A2.1: each volatility rescaled so that sigma_eff^2 x t = sigma^2 x T_bus (unknown T_bus: every case unknown)."""
    return {k: bt.rescale_vol(v, t_bus, t) for k, v in sigmas.items()}


# --------------------------------------------------------------------------- A5: the tail-aware challengers
@dataclass(frozen=True)
class ExerciseSttCost:
    """A1.6's exit cost, exercise STT on in-the-money longs (a rate on intrinsic value from 1-Sep-2019), on a price
    or a whole array of prices (taildist.sample_outcome takes the array: `vectorised`)."""

    rate: float
    longs: tuple[o.Leg, ...]
    vectorised = True

    def __call__(self, p: Any) -> Any:
        tot = 0.0
        for x in self.longs:
            tot = tot + x.qty * (
                np.maximum(p - x.strike, 0.0) if x.right == "call" else np.maximum(x.strike - p, 0.0)
            )
        return self.rate * tot


def exercise_stt_cost(legs: Sequence[o.Leg], expiry: date) -> ExerciseSttCost:
    return ExerciseSttCost(
        ch.exercise_stt(1.0, expiry), tuple(x for x in legs if x.qty > 0 and x.right != "future")
    )


def card_rng(symbol: str, spec: str, origin: date, expiry: date, part: str) -> np.random.Generator:
    """A5.4: a generator per card from sha256("SEED|underlying|spec|origin|expiry|part"), so a card's draws do not
    depend on which other specs or sessions run."""
    key = f"{addendum_a5.SEED}|{symbol}|{spec}|{origin.isoformat()}|{expiry.isoformat()}|{part}"
    return np.random.default_rng(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big"))


class TailBook:
    """Per underlying, oldest session first: the A5.1 z sample (entries pending until their expiry settles) and the
    A5.4 GJR fit. `observe` reads only the session it is given."""

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.pending: dict[tuple[str, date], tuple[float, float, float]] = {}
        self.seen: set[tuple[str, date]] = set()
        self.z: dict[str, list[tuple[date, float]]] = {b: [] for b in addendum_a5.BUCKETS}
        self.gjr: td.GjrFit | None = None
        self.month: tuple[int, int] | None = None

    def observe(self, x: Day, eday: Any) -> None:
        d = x.day
        for e in x.expiries():
            dte = (e - d).days
            for b, (lo, hi, kind) in addendum_a5.BUCKETS.items():
                key = (b, e)
                if key in self.seen or not lo <= dte <= hi or (kind == "monthly" and not x.is_monthly(e)):
                    continue
                fwd = px.forward(x, e, self.rate)
                sigma = atm_iv(x, e, fwd, dte / 365, self.rate) if fwd else None
                if fwd and sigma:
                    self.pending[key] = (fwd, sigma, dte / 365)
                    self.seen.add(key)
        for key in [k for k in self.pending if eday(k[1]) <= d]:
            fwd, sigma, t = self.pending.pop(key)
            if eday(key[1]) == d:
                st = px.settlement_price(x, key[1])
                z = td.terminal_z(st[0] if st else None, fwd, sigma, t)
                if z is not None:
                    self.z[key[0]].append((d, z))

    def refit(self, d: date, returns: Sequence[float]) -> None:
        if (d.year, d.month) != self.month:
            self.month = (d.year, d.month)
            self.gjr = td.gjr_fit(returns)


def _fc(m: td.ModelForecast) -> Forecast:
    if m.outcome is None:
        return Forecast(None)
    return Forecast(m.outcome.pop, m.outcome.ev, [round(v, 2) for v in m.outcome.dots])


def tail_forecasts(spec: Spec, x: Day, legs: Sequence[o.Leg], expiry: date, fwd: float, t: float, rate: float,
                   entry_cost: float, sigma: float | None, tail: TailBook,
                   returns: Sequence[float]) -> tuple[dict[str, Forecast], dict[str, Any]]:  # fmt: skip
    """A5.3-A5.5: p_rw_emp and p_rw_fhs for one card, and what is logged about them."""
    d, dte = x.day, (expiry - x.day).days
    bucket = next((b for b, (lo, hi, _) in addendum_a5.BUCKETS.items() if lo <= dte <= hi), None)
    sample = [(s, z) for s, z in tail.z[bucket] if s < d] if bucket else []  # A5.2: settled before the origin
    xc = exercise_stt_cost(legs, expiry)
    if bucket is None:
        emp = td.ModelForecast(None, None, None, None, 0, f"{dte} days to expiry: outside every bucket")
        fhs = td.ModelForecast(None, None, None, None, 0, emp.reason)
    else:
        emp = td.empirical_forecast(legs, fwd, sigma, t, rate, entry_cost, xc, [z for _, z in sample], boot=0,
                                    rng=card_rng(x.symbol, spec.key, d, expiry, "emp"))  # fmt: skip
        fit = tail.gjr
        fhs = td.fhs_forecast(legs, fwd, sigma, t, rate, entry_cost, xc, list(returns)[-td.GJR_WINDOW:],
                              fit.params if fit else None, steps=td.weekday_steps(d, expiry), kap=emp.kappa,
                              rng=card_rng(x.symbol, spec.key, d, expiry, "fhs"))  # fmt: skip
        if fhs.reason == "no filter parameters":
            fhs = td.ModelForecast(None, None, None, emp.kappa, fhs.n,
                                   f"no filter parameters: {fit.reason if fit else 'no fit yet'}")  # fmt: skip
    gjr = tail.gjr.params if tail.gjr and tail.gjr.params else None
    log = {"bucket": bucket, "n_z": len(sample), "kappa": emp.kappa,
           "last_settled": max((s for s, _ in sample), default=None),
           "es_emp": emp.outcome.es if emp.outcome else None, "es_fhs": fhs.outcome.es if fhs.outcome else None,
           "reason_emp": emp.reason, "reason_fhs": fhs.reason,
           "gjr": None if gjr is None else {"omega": gjr.omega, "alpha": gjr.alpha, "gamma": gjr.gamma,
                                            "beta": gjr.beta}}  # fmt: skip
    return {addendum_a5.EMPIRICAL: _fc(emp), addendum_a5.FHS: _fc(fhs)}, log


def card_at(spec: Spec, x: Day, h: Clocked, nxt: date | None, eday: Any, returns: Sequence[float],
            ivs: Sequence[float], book: Book = BOOK, calendar: bt.Calendar | None = None,
            tail: TailBook | None = None) -> Card | str:  # fmt: skip
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
    # A5: the tail-aware challengers, A1.6's inputs with the A1.8 ATM IV scaling the empirical shape
    tlog = None
    if tail is not None:
        tfc, tlog = tail_forecasts(
            spec, x, legs, expiry, fwd, t, rate, entry_cost, sig["p_rn_atm"], tail, returns
        )
        fc.update(tfc)
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
    return Card(spec.key, sym, spec.kind, expiry, d, inputs, fc, gate, t_bus, b3, tlog)


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
        calendar = bt.Calendar.from_sessions(days)  # A2.5: the stored sessions, as eday below

        def eday(e: date, days: list[date] = days) -> date:
            return e if not days or e > days[-1] else days[bisect_right(days, e) - 1]

        prev: Day | None = None
        tail = TailBook(book.rate)  # A5.1, A5.4
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
            tail.observe(x, eday)  # settles z on d; a card at d reads only z settled before d (A5.2)
            tail.refit(d, returns)
            if not start <= d <= end:
                continue
            nxt = days[i + 1] if i + 1 < len(days) else None
            for spec in group:
                c = card_at(spec, x, h, nxt, eday, returns, ivs, book, calendar, tail)
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
def run_window(history: History, window: str, spec_list: Sequence[Spec] | None = None) -> dict[str, Any]:
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
    cards, skips = make_cards(history, start, end, spec_list)
    events, counts = resolve(cards, history)
    rep = race.run(events, window, challengers=addendum_a5.CHALLENGERS)
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
                     "addendum_a5": addendum_a5.ADDENDUM_VERSION, "addendum_a5_hash": addendum_a5.addendum_hash(),
                     **{f"{k}_unknown": sum(1 for c in cards if c.forecasts.get(k, Forecast(None)).p is None)
                        for k in (addendum_a5.EMPIRICAL, addendum_a5.FHS)},
                     **{f"{k}_reasons": dict(Counter(c.tail[f"reason_{r}"].split(":")[0] for c in cards
                                                     if c.tail and c.tail[f"reason_{r}"]))
                        for k, r in ((addendum_a5.EMPIRICAL, "emp"), (addendum_a5.FHS, "fhs"))},
                     "calendar_gap_weekdays": gaps, "t_bus_unknown": sum(1 for c in cards if c.t_bus is None),
                     "first_origin": min((c.origin for c in cards), default=None),
                     "last_origin": max((c.origin for c in cards), default=None)}  # fmt: skip
    return rep
