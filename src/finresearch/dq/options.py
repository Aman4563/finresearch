"""Option-price screens on the stored bhavcopy's settlement prices (#368, P1.6: the issue's "option screens on
market-settled rows"). Non-critical warnings with counts and examples: a screen points at a data fault (a unit slip,
swapped labels, a stale or mislabelled expiry), not at a trade.

Liquidity filter: an option is screened only when its settlement price is a market price (fincalc.fno_history.
market_settle, the rows the IV series uses: at least 10 contracts traded that day and the settle within 0.5 % + ₹0.05
of the close, so NSE's theoretical settles of untraded or thinly traded strikes stay out) and it expires after the day
(T = calendar days / 365, fno_history's clock; the expiry day itself is never screened).

Forward per (underlying, expiry): fincalc.black76.parity_fit over the strikes with both sides within 5 % of it, else
the same-expiry future's settle; with neither the expiry is not screened (counted in the notes), never passed.

Discount factor: none is assumed. Each bound is taken at the discount factor that makes it weakest over the rates
black76 itself accepts as plausible (R_IMPL_RANGE, -2 % to 20 %), so a flag means "outside the bounds at every such
rate". `ok` is that weaker claim, not "within the bounds at today's rate".

Noise model (the tolerance): settlement prices come from trades at different moments, so each may sit at a forward up
to NOISE (0.5 %) from the expiry's F: about two standard deviations of a 30-minute move at 15 % volatility (15 % / √252
/ √12.5 ≈ 0.27 %; NSE's 09:15-15:30 session is 12.5 half-hours). A combination Σ wⱼ pⱼ is flagged only beyond
ABS_TOL (two ₹0.05 ticks) + NOISE × F × Σ |wⱼ Δⱼ|, with Δⱼ the option's Black-76 forward delta at its own implied
volatility (DF 1), or 1 when no volatility solves (a corrupted price never shrinks its own tolerance).

* opt_bounds: DF·max(F − K, 0) ≤ C ≤ DF·F and DF·max(K − F, 0) ≤ P ≤ DF·K, each at its weakest DF.
* opt_strike_monotone: on each out-of-the-money side (calls at and above the strike nearest F, puts at and below it),
  consecutive screened strikes K1 < K2: C(K2) ≤ C(K1) ≤ C(K2) + DF (K2 − K1), P(K1) ≤ P(K2) ≤ P(K1) + DF (K2 − K1).
* opt_butterfly: the same sequences, consecutive K1 < K2 < K3: p(K2) ≤ w p(K1) + (1 − w) p(K3), w = (K3 − K2) / (K3 −
  K1) (convexity; the strikes need not be evenly spaced).
* opt_parity: the residual (C − P) − DF (F − K) at each strike of the parity fit, at the DF in the range nearest zero;
  and the parity forward against the future, flagged when |F_parity / F_future − 1| > NOISE.
* opt_calendar: at-the-money total variance σ²T (black76.atm_iv: both sides at the strike nearest F, within 3 %)
  non-decreasing across consecutive expiries at least fno_history.MIN_DTE (7) days out, the near one solved at the
  largest DF and the far one at the smallest (the pairing least likely to flag), with VOL_NOISE (1 vol point) on each
  IV: flagged when σn²Tn − σf²Tf > 2 VOL_NOISE (σn Tn + σf Tf). The mean of the call and put IVs cancels the forward's
  first-order error, so the slack is in volatility, not in the forward.

On NIFTY's 50- and 100-point strikes the noise model forgives convexity breaks below about 2 × NOISE × F per unit of
delta: the butterfly screen is sharpest on stock options and wide strikes. These are first settings, to be retuned on
the owner's replay (`finresearch dq replay`).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise

from sqlalchemy import text
from sqlalchemy.orm import Session

from finresearch.dq.base import Check
from finresearch.fincalc import black76
from finresearch.fincalc import fno_history as fh

DATASET = "fno_bhav"
CHECKS = ("opt_bounds", "opt_strike_monotone", "opt_butterfly", "opt_parity", "opt_calendar")
ABS_TOL = 2 * black76.TICK
NOISE = 0.005
VOL_NOISE = 0.01
R_LO, R_HI = black76.R_IMPL_RANGE
EXAMPLES = 3
_TYPE = {"call": "CE", "put": "PE"}
RIGHTS = {"CE": "call", "PE": "put"}


@dataclass(frozen=True)
class Opt:
    strike: float
    right: str  # call | put
    price: float  # a settlement price that is a market price


@dataclass(frozen=True)
class Chain:
    """One underlying's screened options of one expiry on one day, and the same-expiry future's settle (or None)."""

    symbol: str
    expiry: date
    dte: int  # calendar days to expiry
    options: tuple[Opt, ...]
    future: float | None = None


@dataclass
class Screen:
    tested: dict[str, int] = field(default_factory=lambda: dict.fromkeys(CHECKS, 0))
    found: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in CHECKS})
    chains: int = 0
    no_forward: int = 0


def option_delta(o: Opt, fwd: float, t: float) -> float:
    """|Black-76 forward delta| at the option's own implied volatility (DF 1); 1 when no volatility solves."""
    iv = black76.implied_vol(o.right, o.price, fwd, o.strike, t, 1.0)  # type: ignore[arg-type]
    if iv.sigma is None:
        return 1.0
    return abs(black76.forward_delta(o.right, fwd, o.strike, t, 1.0, iv.sigma))  # type: ignore[arg-type]


def tolerance(fwd: float, terms: Iterable[tuple[float, float]]) -> float:
    """ABS_TOL + NOISE × F × Σ |w Δ| over the (weight, delta) of each price in the tested combination."""
    return ABS_TOL + NOISE * fwd * sum(abs(w * d) for w, d in terms)


def df_range(t: float) -> tuple[float, float]:
    """The smallest and largest discount factor over black76.R_IMPL_RANGE."""
    return math.exp(-R_HI * t), math.exp(-R_LO * t)


def _label(c: Chain, k: float, right: str) -> str:
    return f"{c.symbol} {c.expiry:%d-%b-%Y} {k:g} {_TYPE[right]}"


def forward(c: Chain, t: float) -> tuple[float | None, black76.ParityFit | None]:
    """The parity forward (and its fit) when parity_fit finds one, else the future's settle, else None."""
    calls = {o.strike: o.price for o in c.options if o.right == "call"}
    puts = {o.strike: o.price for o in c.options if o.right == "put"}
    both = sorted(calls.keys() & puts.keys())
    fit = black76.parity_fit([black76.ParityQuote(k, calls[k], puts[k]) for k in both], t)
    if fit.forward is not None:
        return fit.forward, fit
    return (c.future if c.future and c.future > 0 else None), None


def screen(chains: Sequence[Chain]) -> Screen:
    res = Screen(chains=len(chains))
    by_symbol: dict[str, list[tuple[Chain, float, float]]] = defaultdict(list)
    for c in sorted(chains, key=lambda c: (c.symbol, c.expiry)):
        t = c.dte / 365
        opts = [o for o in c.options if o.price > 0 and o.strike > 0]
        fwd, fit = forward(c, t) if t > 0 and opts else (None, None)
        if fwd is None:
            res.no_forward += 1
            continue
        delta = {(o.strike, o.right): option_delta(o, fwd, t) for o in opts}
        _bounds(res, c, opts, fwd, t, delta)
        _strikes(res, c, opts, fwd, t, delta)
        if fit is not None:
            _parity(res, c, opts, fit, t, delta)
        by_symbol[c.symbol].append((c, fwd, t))
    for rows in by_symbol.values():
        _calendar(res, rows)
    return res


def _bounds(res: Screen, c: Chain, opts: list[Opt], fwd: float, t: float, delta: dict) -> None:
    lo_df, hi_df = df_range(t)
    for o in opts:
        lo = black76.bounds(o.right, fwd, o.strike, lo_df)[0]  # type: ignore[arg-type]
        hi = black76.bounds(o.right, fwd, o.strike, hi_df)[1]  # type: ignore[arg-type]
        tol = tolerance(fwd, [(1.0, delta[(o.strike, o.right)])])
        res.tested["opt_bounds"] += 1
        if o.price < lo - tol:
            res.found["opt_bounds"].append(f"{_label(c, o.strike, o.right)} {o.price:.2f} under {lo:.2f}")
        elif o.price > hi + tol:
            res.found["opt_bounds"].append(f"{_label(c, o.strike, o.right)} {o.price:.2f} over {hi:.2f}")


def _strikes(res: Screen, c: Chain, opts: list[Opt], fwd: float, t: float, delta: dict) -> None:
    """Monotonicity and convexity in strike on each out-of-the-money side (and the strike nearest F)."""
    atm = min({o.strike for o in opts}, key=lambda k: (abs(k - fwd), k))
    hi_df = df_range(t)[1]
    for right, side in (("call", [o for o in opts if o.right == "call" and o.strike >= atm]),
                        ("put", [o for o in opts if o.right == "put" and o.strike <= atm])):  # fmt: skip
        side.sort(key=lambda o: o.strike)
        d = [delta[(o.strike, right)] for o in side]
        for i, (a, b) in enumerate(pairwise(side)):
            tol = tolerance(fwd, [(-1.0, d[i]), (1.0, d[i + 1])])
            rise = b.price - a.price  # calls fall with K, puts rise
            order, slope = (rise, -rise) if right == "call" else (-rise, rise)
            res.tested["opt_strike_monotone"] += 1
            if order > tol:
                res.found["opt_strike_monotone"].append(f"{_label(c, b.strike, right)} {b.price:.2f} out of order "
                                                        f"with {a.strike:g} at {a.price:.2f}")  # fmt: skip
            elif slope - hi_df * (b.strike - a.strike) > tol:
                res.found["opt_strike_monotone"].append(f"{_label(c, b.strike, right)} {b.price:.2f} and {a.strike:g} at "
                                                        f"{a.price:.2f} differ by more than the strike gap")  # fmt: skip
        for i in range(len(side) - 2):
            a, b, z = side[i : i + 3]
            w = (z.strike - b.strike) / (z.strike - a.strike)
            chord = w * a.price + (1 - w) * z.price
            res.tested["opt_butterfly"] += 1
            if b.price - chord > tolerance(fwd, [(w, d[i]), (-1.0, d[i + 1]), (1 - w, d[i + 2])]):
                res.found["opt_butterfly"].append(f"{_label(c, b.strike, right)} {b.price:.2f} over its chord "
                                                  f"{chord:.2f}")  # fmt: skip


def _parity(res: Screen, c: Chain, opts: list[Opt], fit: black76.ParityFit, t: float, delta: dict) -> None:
    fwd = fit.forward or 0.0
    lo_df, hi_df = df_range(t)
    px = {(o.strike, o.right): o.price for o in opts}
    for k in fit.strikes:
        y = px[(k, "call")] - px[(k, "put")]
        lo, hi = y - lo_df * (fwd - k), y - hi_df * (fwd - k)
        off = 0.0 if lo * hi <= 0 else min(abs(lo), abs(hi))
        res.tested["opt_parity"] += 1
        if off > tolerance(fwd, [(1.0, delta[(k, "call")]), (-1.0, delta[(k, "put")])]):
            res.found["opt_parity"].append(
                f"{c.symbol} {c.expiry:%d-%b-%Y} {k:g} C - P {y:.2f} is {off:.2f} off parity"
            )
    b = black76.basis(fwd, c.future)
    if b is not None:
        res.tested["opt_parity"] += 1
        if abs(b) > NOISE:
            res.found["opt_parity"].append(f"{c.symbol} {c.expiry:%d-%b-%Y} parity forward {fwd:.2f} vs future "
                                           f"{c.future:.2f} (basis {b:+.2%})")  # fmt: skip


def _calendar(res: Screen, rows: list[tuple[Chain, float, float]]) -> None:
    """σ²T at the money must not fall from one expiry to the next (expiries at least MIN_DTE days out)."""
    pts = []
    for c, fwd, t in rows:
        if c.dte < fh.MIN_DTE:
            continue
        quotes = [black76.OptionQuote(o.strike, o.right, price=o.price) for o in c.options if o.price > 0]  # type: ignore[arg-type]
        lo_df, hi_df = df_range(t)
        near, far = black76.atm_iv(quotes, fwd, t, hi_df), black76.atm_iv(quotes, fwd, t, lo_df)
        if near.sides < 2 or far.sides < 2 or near.strike is None or abs(near.strike / fwd - 1) > fh.ATM_BAND:
            continue
        pts.append((c, t, near.sigma, far.sigma))
    for (n, tn, sn, _), (f, tf, _, sf) in pairwise(pts):
        res.tested["opt_calendar"] += 1
        wn, wf = sn * sn * tn, sf * sf * tf
        if wn - wf > 2 * VOL_NOISE * (sn * tn + sf * tf):
            res.found["opt_calendar"].append(f"{n.symbol} σ²T {n.expiry:%d-%b-%Y} {wn:.5f} over {f.expiry:%d-%b-%Y} "
                                             f"{wf:.5f}")  # fmt: skip


WHAT = {
    "opt_bounds": "screened options outside the no-arbitrage bounds at every rate from -2 % to 20 %",
    "opt_strike_monotone": "strike pairs out of order or further apart than the strike gap",
    "opt_butterfly": "strike triples above their chord (not convex in strike)",
    "opt_parity": "parity tests off (C - P against the fitted forward; parity forward against the future)",
    "opt_calendar": "expiry pairs whose at-the-money total variance falls",
}


def to_checks(res: Screen) -> list[Check]:
    """One non-critical check per screen: warn with the count and examples, ok, or unknown when nothing was tested."""
    skipped = f"; {res.no_forward} expiry(ies) without a forward not screened" if res.no_forward else ""
    out = []
    for name in CHECKS:
        n, bad = res.tested[name], res.found[name]
        thr = VOL_NOISE if name == "opt_calendar" else NOISE
        if not n:
            why = ("no market-settled option on this day" if not res.chains else
                   f"no forward for {res.no_forward} of {res.chains} expiry(ies)" if res.no_forward == res.chains
                   else "nothing to compare among the screened expiries")  # fmt: skip
            out.append(Check(DATASET, name, "unknown", None, thr, f"not tested: {why}", critical=False))
            continue
        note = f"{len(bad)} of {n} {WHAT[name]}" + (": " + "; ".join(bad[:EXAMPLES]) if bad else "") + skipped
        out.append(Check(DATASET, name, "warn" if bad else "ok", len(bad), thr, note, critical=False))
    return out


def option_checks(s: Session, day: date) -> list[Check]:
    """The screens over `day`'s stored rows: options through the liquidity filter, futures as the fallback forward."""
    rows = s.execute(text("""
        SELECT symbol, expiry, strike, option_type, settle, close, contracts FROM fno_bhav
        WHERE day = :d AND expiry > :d AND settle > 0"""), {"d": day}).all()  # fmt: skip
    fut: dict[tuple[str, date], float] = {}
    opts: dict[tuple[str, date], list[Opt]] = defaultdict(list)
    for sym, exp, k, typ, settle, close, n in rows:
        if typ == "XX":
            fut[(sym, exp)] = float(settle)
        elif typ in RIGHTS and fh.market_settle(float(settle), None if close is None else float(close), n):
            opts[(sym, exp)].append(Opt(float(k), RIGHTS[typ], float(settle)))
    chains = [
        Chain(sym, exp, (exp - day).days, tuple(v), fut.get((sym, exp))) for (sym, exp), v in opts.items()
    ]
    return to_checks(screen(chains))
