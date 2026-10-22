"""The VRP study's inputs from the stored bhavcopy (P3.6, #389; evals/experiments/vrp_collectable/PREREG.md). Reads
only: fno_bhav's index futures and options of one underlying and index_eod's India VIX. Refuses to read before the
registration is intact (vrp/prereg.py) and refuses 2008-2021 rows without a live vault grant (vault.guard, #364).

One pass over the sessions in time order: each session's front-future bar (within contract, as volrace/data.py), its
30-day sigma^2_MF (measure.mf30 on the registered common range, and the flat-extension sensitivity on the full
strip) and India VIX close; at each monthly expiry that is a session, the straddle and 25-delta put trades to the next
monthly expiry (marked on every later session before it, settled by backtest.pricing.settlement_price on the
contract's last stored session) and the near-expiry z values.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import date, timedelta

from sqlalchemy import select, text

from finresearch.backtest import history, pricing
from finresearch.db.models import FnoBhav, IndexEod
from finresearch.fincalc import fno_history as fh
from finresearch.fincalc import mfiv
from finresearch.fincalc import realvol as rv
from finresearch.fnohist.derive import RIGHTS
from finresearch.vault.registry import VAULT_END, VaultSealed
from finresearch.vrp import collect as co
from finresearch.vrp import measure, prereg, run
from finresearch.vrp import study as st

CHUNK_DAYS = 31  # option rows are read a month at a time
VIX_SYMBOL = "INDIA VIX"
Lots = Callable[[str, date, date], int | None]


def _f(x) -> float | None:
    return None if x is None else float(x)


class _Open:
    """A trade being built: its marks so far and the latest stored chain of its expiry (for the settlement)."""

    def __init__(
        self, entry: date, expiry: date, strike: float, rights: tuple[str, ...], lot: int | None
    ) -> None:
        self.entry, self.expiry, self.strike, self.rights, self.lot = entry, expiry, strike, rights, lot
        self.marks: list[co.Mark] = []
        self.last: history.Day | None = None
        self.last_session: date | None = None  # the latest stored session on or before the expiry

    def trade(self) -> co.Trade:
        final = None
        if self.last is not None and self.last.day == self.last_session:  # never settle on an earlier chain
            got = pricing.settlement_price(self.last, self.expiry)
            final = None if got is None else got[0]
        return co.Trade(self.entry, self.expiry, self.strike, self.lot, tuple(self.marks), final, self.rights)


def _atm_strike(chain: dict[float, dict[str, tuple]], fwd: float) -> float | None:
    both = [k for k, v in chain.items() if all(r in v and v[r][3] for r in ("call", "put"))]
    return min(both, key=lambda k: (abs(k - fwd), k)) if both else None


def load(symbol: str, start: date, end: date, *, lots: Lots, band: float | None, url: str | None = None,
         grant: object = None) -> tuple[run.Inputs, dict]:  # fmt: skip
    """The study's inputs for `symbol` from `start` to `end` and the counts of what was unknown."""
    prereg.require_registered()
    if start <= VAULT_END:
        from finresearch.vault.guard import check_grant

        if not check_grant(grant, url):
            raise VaultSealed(f"{start} is in the 2008-2021 vault: the VRP study reads it only through "
                              "vault.guard.vault_read('vrp_primary', ...) (#364)")  # fmt: skip
    from finresearch.db import session_scope

    counts: Counter = Counter()
    with session_scope(url) as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        futs: dict[date, dict[date, tuple]] = defaultdict(dict)
        for d, e, o, h, lo, c, stl in s.execute(
            select(FnoBhav.day, FnoBhav.expiry, FnoBhav.open, FnoBhav.high, FnoBhav.low, FnoBhav.close,
                   FnoBhav.settle).where(FnoBhav.symbol == symbol, FnoBhav.option_type == "XX",
                                         FnoBhav.day.between(start, end))):  # fmt: skip
            futs[d][e] = (_f(o), _f(h), _f(lo), _f(c), _f(stl))
        vix = {d: float(v) for d, v in s.execute(select(IndexEod.day, IndexEod.close).where(
            IndexEod.symbol == VIX_SYMBOL, IndexEod.day.between(start, end), IndexEod.close.is_not(None)))}  # fmt: skip
        days = sorted(futs)
        monthly = measure.monthly_expiries(sorted({e for x in futs.values() for e in x}))
        starts = {e for e in monthly if e in futs}
        out_days: list[st.Day] = []
        opens: list[_Open] = []
        done: list[tuple[str, _Open]] = []
        pending_z: list[tuple[date, _Open, float, float | None, float, float | None]] = []
        lo = days[0] if days else end + timedelta(1)
        i = 0
        while lo <= (days[-1] if days else lo - timedelta(1)):
            hi = lo + timedelta(CHUNK_DAYS)
            opts: dict[date, dict[date, dict[float, dict[str, tuple]]]] = defaultdict(
                lambda: defaultdict(lambda: defaultdict(dict)))  # fmt: skip
            spot: dict[date, float] = {}
            for d, e, k, right, stl, close, n, und in s.execute(
                select(FnoBhav.day, FnoBhav.expiry, FnoBhav.strike, FnoBhav.option_type, FnoBhav.settle,
                       FnoBhav.close, FnoBhav.contracts, FnoBhav.underlying)
                .where(FnoBhav.symbol == symbol, FnoBhav.option_type.in_(tuple(RIGHTS)), FnoBhav.day >= lo,
                       FnoBhav.day < hi)):  # fmt: skip
                if und is not None:
                    spot[d] = float(und)
                sv, cv = _f(stl), _f(close)
                mkt = sv is not None and sv > 0 and fh.market_settle(sv, cv, n)
                opts[d][e][float(k)][RIGHTS[right]] = (sv, cv, n, mkt)
            while i < len(days) and days[i] < hi:
                d = days[i]
                rec, mf = _day(d, i, days, futs, opts.get(d, {}), vix, monthly, band, counts)
                out_days.append(rec)
                fut = {e: v[4] for e, v in futs[d].items() if v[4] and v[4] > 0}
                for t in opens:
                    _observe(t, d, symbol, opts.get(d, {}), fut, spot.get(d))
                opens = [t for t in opens if d < t.expiry]
                if d in starts:
                    new = _enter(d, symbol, monthly, opts.get(d, {}), fut, lots, counts)
                    for kind, t in new:
                        _observe(t, d, symbol, opts.get(d, {}), fut, spot.get(d))
                        opens.append(t)
                        done.append((kind, t))
                    pending_z.extend(_z_inputs(d, new, mf, fut, opts.get(d, {}), counts))
                i += 1
            lo = hi
    data_end = days[-1] if days else start
    straddles = [t.trade() if t.expiry <= data_end else _no_final(t) for k, t in done if k == "straddle"]
    puts = [t.trade() if t.expiry <= data_end else _no_final(t) for k, t in done if k == "put25"]
    z_mf, z_atm = {}, {}
    for d, t, f, v, tt, atm in pending_z:
        fin = t.trade().final if t.expiry <= data_end else None
        z_mf[d] = None if fin is None or v is None else math.log(fin / f) / math.sqrt(v * tt)
        z_atm[d] = (
            None if fin is None or atm is None else math.log(fin / f) / math.sqrt((atm / 100) ** 2 * tt)
        )
    return run.Inputs(out_days, starts, data_end, straddles, puts, z_mf, z_atm), dict(counts)


def _day(d: date, i: int, days: list[date], futs: dict, chains: dict, vix: dict, monthly: list[date],
         band: float | None, counts: Counter) -> tuple[st.Day, measure.Mf30]:  # fmt: skip
    live = [e for e in futs[d] if e >= d]
    bar = None
    if live:
        e = min(live)
        o, h, lo, c, _ = futs[d][e]
        prev = futs[days[i - 1]].get(e) if i > 0 else None
        bar = rv.Bar(d, o, h, lo, c, prev[3] if prev else None)
        counts["no_prev_close"] += bar.prev_close is None
        counts["invalid_bar"] += rv.bar_problem(bar) is not None
    else:
        counts["no_front_future"] += 1
    strikes = {e: [_strike(k, v) for k, v in sorted(ch.items())] for e, ch in chains.items()}
    fwd = {e: v[4] for e, v in futs[d].items() if v[4] and v[4] > 0}
    pair = measure.terms(d, monthly, days)
    mf = measure.mf30(d, pair, strikes, fwd, band=band)
    flat = measure.mf30(d, pair, strikes, fwd, extend_flat=True)
    counts["mf30_unknown"] += mf.value is None
    return st.Day(d, bar, mf.value, mf.reason, flat.value, vix.get(d)), mf


def _strike(k: float, v: dict[str, tuple]) -> mfiv.Strike:
    """A listed strike with a price only where the settle is a market price (fno_history.market_settle)."""
    c, p = v.get("call"), v.get("put")
    return mfiv.Strike(k, c[0] if c and c[3] else None, p[0] if p and p[3] else None)


def _quote(chain: dict, k: float, right: str) -> tuple:
    return chain.get(k, {}).get(right) or (None, None, None, False)


def _observe(t: _Open, d: date, symbol: str, chains: dict, fut: dict, spot: float | None) -> None:
    ch = chains.get(t.expiry, {})
    if d < t.expiry:
        c, p = _quote(ch, t.strike, "call"), _quote(ch, t.strike, "put")
        t.marks.append(co.Mark(d, fut.get(t.expiry), c[0], p[0], c[2], p[2]))
    if d <= t.expiry:
        t.last_session = d
    if d <= t.expiry and ch:
        opts = {(t.expiry, r, k): history.Quote(t.expiry, r, k, q[1], q[0], int(q[2] or 0), 0)
                for k, v in ch.items() for r, q in v.items()}  # fmt: skip
        t.last = history.Day(d, symbol, opts, {t.expiry: fut[t.expiry]} if t.expiry in fut else {}, spot)


def _enter(d: date, symbol: str, monthly: list[date], chains: dict, fut: dict, lots: Lots,
           counts: Counter) -> list[tuple[str, _Open]]:  # fmt: skip
    nxt = [e for e in monthly if e > d]
    if not nxt:
        counts["no_next_monthly"] += 1
        return []
    e = nxt[0]
    f, ch = fut.get(e), chains.get(e, {})
    lot = lots(symbol, e, d)
    k = _atm_strike(ch, f) if f else None
    counts["no_atm_strike"] += k is None
    puts = [(kk, v["put"][0], v["put"][2]) for kk, v in ch.items() if "put" in v and v["put"][3]]
    kp = co.delta_put_strike(puts, f, (e - d).days / 365, fh.RATE) if f else None
    counts["no_put25_strike"] += kp is None
    nan = float("nan")
    return [("straddle", _Open(d, e, nan if k is None else k, ("call", "put"), lot)),
            ("put25", _Open(d, e, nan if kp is None else kp, ("put",), lot))]  # fmt: skip


def _z_inputs(d: date, new: list[tuple[str, _Open]], mf: measure.Mf30, fut: dict, chains: dict,
              counts: Counter) -> list[tuple]:  # fmt: skip
    out = []
    for kind, t in new:
        f = fut.get(t.expiry)
        if kind != "straddle" or f is None:
            continue
        near = mf.near if mf.near is not None and mf.near.value is not None else None
        v = near.value if near is not None and abs(near.t * 365 - (t.expiry - d).days) < 1e-9 else None
        quotes = [
            fh.OptQuote(k, r, q[0])
            for k, ch in chains.get(t.expiry, {}).items()
            for r, q in ch.items()
            if q[3]
        ]
        ex = fh.expiry_iv(d, t.expiry, quotes, future=f)
        counts["z_no_atm_iv"] += ex is None
        out.append((d, t, f, v, (t.expiry - d).days / 365, None if ex is None else ex.atm_iv))
    return out


def _no_final(t: _Open) -> co.Trade:
    return co.Trade(t.entry, t.expiry, t.strike, t.lot, tuple(t.marks), None, t.rights)
