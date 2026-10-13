"""Strategy Lab → paper trading (#318): a version sent to paper trading becomes a rule set that is checked at every
stored close and recorded in the F&O paper book (#308), so it builds an honest forward record next to its backtest.

* Signals (evening, after the nightly bhavcopy): each stored close after the rule set was created is checked with the
  engine's own entry decision (`engine.try_enter`, the version's filters, sizing on its capital and the open signals
  of this rule set). A signal stores the legs picked at that close.
* Fills (in market hours): a pending signal is filled on a later session at live prices (`fnobook.service.quotes`:
  Groww LTP, else the option chain) moved by the paper book's default slippage, through `fnobook.book.record`. A
  signal not filled within FILL_WITHIN_DAYS calendar days, or whose expiry has come, is `missed` (kept, never
  deleted). Nothing is ever sent to a broker.
* Management (in market hours): the paper book marks open trades every few minutes; the version's rules close a
  lab trade at live prices (`book.square_off`): profit-take and stop on the marked gross P&L against the entry net
  premium, the days-to-expiry exit, the last session before expiry day, and (stocks) the session before a results
  date when the NSE calendar can be read. An unknown mark never fires a P&L rule. Index trades held to expiry are
  settled by the paper book itself (fnobook.service.settle_step).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from finresearch.backtest import engine
from finresearch.backtest.history import Clocked
from finresearch.backtest.specs import IV_WARMUP_DAYS
from finresearch.db import session_scope
from finresearch.db.models import LabPaperRule, LabPaperSignal, LabRun, LabStrategy, LabVersion, PaperStrategy
from finresearch.fincalc.dates import to_ist
from finresearch.stratlab import filters as flt
from finresearch.stratlab.spec import StrategySpec, book_for, to_lab_spec
from finresearch.stratlab.store import LabError

log = logging.getLogger(__name__)

FILL_WITHIN_DAYS = 4  # a signal of Friday's close can still fill on Monday or Tuesday (a holiday), not later
SIGNALS_EVERY = timedelta(minutes=30)
MANAGE_EVERY = timedelta(minutes=5)
_LAST: dict[str, datetime] = {}


def _due(key: str, now: datetime, every: timedelta) -> bool:
    last = _LAST.get(key)
    if last is not None and now - last < every:
        return False
    _LAST[key] = now
    return True


def _trading_day(d: date) -> bool:
    try:
        from finresearch.adapters.groww_market import _holidays, trading_day

        return trading_day(d, _holidays())
    except Exception:
        return d.weekday() < 5  # the holiday list unknown: weekdays (an exit may come a session late)


def next_session(d: date) -> date:
    d += timedelta(days=1)
    while not _trading_day(d):
        d += timedelta(days=1)
    return d


def expiry_day(e: date) -> date:
    """The last session on or before expiry date e (a holiday moves expiry earlier)."""
    while not _trading_day(e):
        e -= timedelta(days=1)
    return e


# --------------------------------------------------------------------------- the rule set
def create_rule(s: Any, version_id: int, now: datetime) -> LabPaperRule:
    v = s.get(LabVersion, version_id)
    if v is None:
        raise LabError(f"no lab version {version_id}")
    done = s.scalars(
        select(LabRun.id).where(LabRun.version_id == version_id, LabRun.status == "done")
    ).first()
    if done is None:
        raise LabError("run this version's backtest first: paper trading starts from a tested version")
    live = s.scalars(select(LabPaperRule).where(LabPaperRule.version_id == version_id,
                                                LabPaperRule.status == "active")).first()  # fmt: skip
    if live is not None:
        raise LabError(f"this version is already paper trading (rule set {live.id})")
    r = LabPaperRule(version_id=version_id, status="active", created_at=now, start_after=to_ist(now).date())
    s.add(r)
    s.flush()
    return r


def stop_rule(s: Any, rule_id: int, now: datetime) -> LabPaperRule:
    r = s.get(LabPaperRule, rule_id)
    if r is None:
        raise LabError(f"no paper rule set {rule_id}")
    if r.status == "active":
        r.status, r.stopped_at = "stopped", now
        r.note = "stopped: no new signals; open paper trades keep their rules until they close"
    return r


def _position(sig: LabPaperSignal) -> engine.Position:
    """An open signal as the engine's entry decision sees it (expiry, entry day and margin are what it reads)."""
    return engine.Position(f"rule{sig.rule_id}", "", sig.expiry, sig.day, (sig.expiry - sig.day).days, sig.lots,
                           sig.lot, [], float(sig.premium), None if sig.max_loss is None else float(sig.max_loss),
                           None if sig.margin is None else float(sig.margin), 0.0, None, sig.iv_pct)  # fmt: skip


def _open_signals(s: Any, rule_id: int) -> list[LabPaperSignal]:
    rows = s.scalars(select(LabPaperSignal).where(LabPaperSignal.rule_id == rule_id,
                                                  LabPaperSignal.status.in_(("pending", "filled")))).all()  # fmt: skip
    out = []
    for sig in rows:
        if sig.status == "filled":
            st = s.get(PaperStrategy, sig.paper_id) if sig.paper_id else None
            if st is None or st.status == "closed":
                continue
        out.append(sig)
    return out


def decide(history: Any, spec: StrategySpec, d: date, open_sigs: list[LabPaperSignal], last_entry: date | None,
           key: str) -> Any:  # fmt: skip
    """The engine's entry decision at the close of `d` for this rule set: a Position, or the refusal reasons."""
    lab, book = to_lab_spec(spec, key, key), book_for(spec)
    sym = spec.underlying
    h = Clocked(history)
    h.advance(d)
    x = h.day(sym, d)
    if x is None:
        return {"no rows for this close": 1}
    f = flt.Filters(lab, book.rate)
    f.closes = flt.price_series(history, sym, d - timedelta(days=IV_WARMUP_DAYS), d, book.rate)
    ivs = h.iv_history(sym, d - timedelta(days=IV_WARMUP_DAYS), d)
    if not ivs and x.iv30 is not None:
        ivs = [x.iv30]  # no derived IV series: today's IV alone, so an IV filter reads "insufficient history"
    st = engine.SpecState(lab, open=[_position(g) for g in open_sigs])
    st.last_entry = last_entry
    p = engine.try_enter(st, x, sym, h, ivs, book, next_session(d), expiry_day, f.gate)
    return p if p is not None else (dict(st.skips) or {"no expiry in the entry window": 1})


def _last_entry(s: Any, rule_id: int) -> date | None:
    return s.scalars(select(LabPaperSignal.day).where(LabPaperSignal.rule_id == rule_id,
                                                      LabPaperSignal.status.in_(("pending", "filled")))
                     .order_by(LabPaperSignal.day.desc()).limit(1)).first()  # fmt: skip


def _d(x: float | None) -> Decimal | None:
    return None if x is None else Decimal(str(round(x, 2)))


# --------------------------------------------------------------------------- 1. signals at each stored close
def signals_step(now: datetime, history: Any = None) -> dict[str, int]:
    """Check every active rule set at each stored close after the last one checked (blocking: run in a thread)."""
    if history is None:
        from finresearch.backtest.dbhistory import DbHistory

        history = DbHistory()
    today = to_ist(now).date()
    with session_scope() as s:
        rules = [(r.id, r.start_after, r.last_day, s.get(LabVersion, r.version_id))
                 for r in s.scalars(select(LabPaperRule).where(LabPaperRule.status == "active"))]  # fmt: skip
        todo = [(rid, after, last, StrategySpec.model_validate(v.spec)) for rid, after, last, v in rules]
    out = {"lab_signals": 0, "lab_closes_checked": 0}
    for rid, after, last, spec in todo:
        frm = max(after, last or after)
        latest = history.latest(spec.underlying)
        if latest is None or latest <= frm:
            continue
        for d in history.sessions(spec.underlying, frm + timedelta(days=1), latest):
            with session_scope() as s:
                r = s.get(LabPaperRule, rid, with_for_update=True)
                if r is None or r.status != "active" or (r.last_day is not None and r.last_day >= d):
                    continue
                got = decide(history, spec, d, _open_signals(s, rid), _last_entry(s, rid), f"rule{rid}")
                r.last_day = d
                out["lab_closes_checked"] += 1
                if isinstance(got, dict):
                    r.note = f"{d:%d-%b-%Y} close: no entry ({'; '.join(got)})"
                    continue
                late = (today - d).days > FILL_WITHIN_DAYS
                s.add(LabPaperSignal(rule_id=rid, day=d, status="missed" if late else "pending", expiry=got.expiry,
                                     lots=got.lots, lot=got.lot, premium=_d(got.premium), max_loss=_d(got.max_loss),
                                     margin=_d(got.margin), iv_pct=got.iv_pct,
                                     legs=[{"right": x.right, "strike": x.strike, "side": x.side, "ref": x.mark,
                                            "fill_assumed": x.entry} for x in got.legs],
                                     note=f"checked late: more than {FILL_WITHIN_DAYS} days after the close" if late
                                     else None))  # fmt: skip
                r.note = f"{d:%d-%b-%Y} close: signal ({got.expiry:%d-%b} expiry, {got.lots} lot(s))"
                out["lab_signals"] += 1
    return out


# --------------------------------------------------------------------------- 2. fills on a later session
def _label(s: Any, rule_id: int, d: date) -> str:
    r = s.get(LabPaperRule, rule_id)
    v = s.get(LabVersion, r.version_id)
    st = s.get(LabStrategy, v.strategy_id)
    return f"Lab #{st.id} {st.name} v{v.version} · rule {rule_id} · signal {d:%d-%b-%Y}"[:120]


async def fill_step(now: datetime, *, market: Any = None, sources: Any = None) -> dict[str, int]:
    """Fill pending signals of earlier closes at live prices (market hours only; service.quotes refuses otherwise)."""
    from finresearch.fnobook import book, service
    from finresearch.signals.fno import LegIn
    from finresearch.suggest.advisor import load_profile

    today = to_ist(now).date()
    with session_scope() as s:
        pend = [(g.id, g.rule_id, g.day, g.expiry, g.lots, g.lot, list(g.legs), s.get(LabVersion, s.get(
            LabPaperRule, g.rule_id).version_id).spec["underlying"]) for g in s.scalars(select(LabPaperSignal).where(
            LabPaperSignal.status == "pending", LabPaperSignal.day < today))]  # fmt: skip
    out = {"lab_filled": 0, "lab_missed": 0}
    for gid, rid, day, exp, lots, lot, legs, sym in pend:
        if (today - day).days > FILL_WITHIN_DAYS or exp <= today:
            with session_scope() as s:
                g = s.get(LabPaperSignal, gid)
                g.status = "missed"
                g.note = ("its expiry came before a fill" if exp <= today else
                          f"no fill within {FILL_WITHIN_DAYS} days of the signal close (no live price, or the app was off)")  # fmt: skip
            out["lab_missed"] += 1
            continue
        try:
            quotes, chain = await service.quotes(sym, exp, [(x["right"], float(x["strike"])) for x in legs], now,
                                                 market=market, sources=sources)  # fmt: skip
        except service.OutOfSession:
            return out
        with session_scope() as s:
            g = s.get(LabPaperSignal, gid, with_for_update=True)
            if g is None or g.status != "pending":
                continue
            try:
                st = book.record(s, symbol=sym, expiry=exp, lot=lot, quotes=quotes,
                                 legs_in=[LegIn(x["right"], float(x["strike"]), x["side"], lots) for x in legs],
                                 slippage_pct=book.DEFAULT_SLIPPAGE_PCT,
                                 brokerage=float(load_profile(s).fno_brokerage_per_order_inr), now=now,
                                 spot=float(chain.underlying) if chain is not None and chain.underlying else None,
                                 label=_label(s, rid, day))  # fmt: skip
            except ValueError as e:
                g.note = f"not filled yet at {to_ist(now):%d-%b %H:%M} IST: {e}"
                continue
            g.status, g.paper_id, g.note = "filled", st.id, None
            out["lab_filled"] += 1
    return out


# --------------------------------------------------------------------------- 3. the version's exit rules
def exit_reason(spec: StrategySpec, st: PaperStrategy, today: date, results: list[date] | None) -> str | None:
    """The engine's management rules (engine.exit_reason) on a paper trade's latest mark; `results`: upcoming results
    dates of a stock (None = unknown, so that rule cannot fire)."""
    from finresearch.fnobook import book

    gross = None if st.mtm_gross is None else float(st.mtm_gross)
    basis = abs(book.pp.net_premium(book.legs_of(st)))
    if gross is not None and basis > 0:
        if spec.profit_take_pct is not None and gross >= spec.profit_take_pct / 100 * basis:
            return "profit_take"
        if spec.stop_mult is not None and gross <= -spec.stop_mult * basis:
            return "stop"
    if spec.exit_dte is not None and (st.expiry - today).days <= spec.exit_dte:
        return "dte"
    nxt = next_session(today)
    if spec.exit_before_expiry_day and nxt >= expiry_day(st.expiry):
        return "before_expiry_day"
    if spec.exit_before_results and results and any(today < r <= nxt for r in results):
        return "before_results"
    return None


async def manage_step(now: datetime, *, market: Any = None, sources: Any = None) -> dict[str, int]:
    """Close lab paper trades whose rule fired, at live prices (square-off charges). Market hours only."""
    from finresearch.fnobook import book, service
    from finresearch.fnobook import events as ev

    today = to_ist(now).date()
    with session_scope() as s:
        rows = [(g.id, g.paper_id, StrategySpec.model_validate(s.get(LabVersion, s.get(LabPaperRule, g.rule_id)
                 .version_id).spec)) for g in s.scalars(select(LabPaperSignal).where(
                 LabPaperSignal.status == "filled", LabPaperSignal.exit_reason.is_(None)))]  # fmt: skip
    out = {"lab_closed": 0}
    for gid, pid, spec in rows:
        with session_scope() as s:
            st = s.get(PaperStrategy, pid)
            if st is None or st.status != book.OPEN:
                g = s.get(LabPaperSignal, gid)
                g.exit_reason = (st.close_reason if st is not None else None) or "closed"
                continue
            sym, exp, legs = st.symbol, st.expiry, [(x.right, float(x.strike)) for x in st.legs]
        results = None
        if spec.exit_before_results:
            got, _src = await ev.results_dates(sym, today, sources or service._sources())
            results = got
        with session_scope() as s:
            reason = exit_reason(spec, s.get(PaperStrategy, pid), today, results)
        if reason is None:
            continue
        try:
            quotes, _chain = await service.quotes(sym, exp, legs, now, market=market, sources=sources)
        except service.OutOfSession:
            return out
        with session_scope() as s:
            st = s.get(PaperStrategy, pid, with_for_update=True)
            g = s.get(LabPaperSignal, gid)
            try:
                book.square_off(st, [q.price for q in quotes], now)
            except ValueError as e:
                g.note = f"{reason}: not closed yet at {to_ist(now):%d-%b %H:%M} IST ({e})"
                continue
            g.exit_reason, g.note = reason, None
            out["lab_closed"] += 1
    return out


# --------------------------------------------------------------------------- the monitor step
async def step(
    now: datetime, *, history: Any = None, market: Any = None, sources: Any = None
) -> dict[str, int]:
    """The monitor tick's lab work: nothing (one query) without an active rule set or a lab trade still open."""
    from finresearch.fnobook import service

    with session_scope() as s:
        active = s.scalar(select(LabPaperRule.id).where(LabPaperRule.status == "active").limit(1))
        live = s.scalar(select(LabPaperSignal.id).where(LabPaperSignal.status.in_(("pending", "filled")),
                                                        LabPaperSignal.exit_reason.is_(None)).limit(1))  # fmt: skip
    if active is None and live is None:
        return {}
    out: dict[str, int] = {}
    if active is not None and _due("signals", now, SIGNALS_EVERY):
        out |= await asyncio.to_thread(signals_step, now, history)
    if live is not None and service._phase(now) in ("open", "closing") and _due("manage", now, MANAGE_EVERY):
        out |= await fill_step(now, market=market, sources=sources)
        out |= await manage_step(now, market=market, sources=sources)
    return {k: v for k, v in out.items() if v}
