"""The one-book replay of the registered sellers (#395 Stage 2; evals/experiments/risk_budget_seen/PREREG.md): one
pass over the stored sessions of every underlying, oldest first, through backtest.history.Clocked. At each close every
arm manages all its open positions (backtest.engine.manage), then considers each spec's entry in order
(riskbudget.entry, the engine's own entry split at its sizing): the uncapped arm takes the registered trade size; the
budgeted and card-rule arms size it by the book's limits (riskbudget.limits). The engine's margin rule then applies to
the whole book. The period's last session takes no entry and squares off what is still open (reason period_end).
Equity and open risk are recorded per arm at every session of the period after its entries."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise
from typing import Any

from finresearch.backtest import engine
from finresearch.backtest.history import Clocked, Day, History
from finresearch.backtest.specs import BOOK, IV_WARMUP_DAYS, Book, Spec
from finresearch.fincalc import tradeplan as tp
from finresearch.riskbudget import entry, limits, shock
from finresearch.riskbudget import specs as rspecs


@dataclass
class Arm:
    rule: str  # uncapped | budgeted | card_rule
    states: dict[str, engine.SpecState]  # per spec key, in entry order
    equity: list[tuple[date, float]] = field(default_factory=list)
    risk: list[tuple[date, float | None]] = field(
        default_factory=list
    )  # open risk OR after each close's entries
    entries: list[dict[str, Any]] = field(default_factory=list)
    refused: Counter = field(
        default_factory=Counter
    )  # book-level refusals by reason (the engine's: in st.skips)
    cut: Counter = field(
        default_factory=Counter
    )  # entries taken below the trade size, by the term that cut them
    month: tuple[int, int] | None = None
    month_base: float = 0.0  # E at the previous calendar month's last session (the capital before the first)

    def open(self) -> list[engine.Position]:
        return [p for st in self.states.values() for p in st.open]

    def trades(self) -> list[dict[str, Any]]:
        return [t for st in self.states.values() for t in st.trades]

    def value(self, capital: float) -> float:
        realised = sum(st.realised for st in self.states.values())
        return capital + realised + sum(p.gross() - p.entry_costs for p in self.open())

    def open_risk(self) -> float | None:
        return _total(p.max_loss for p in self.open())

    def margin_used(self) -> float | None:
        return _total(p.margin for p in self.open())

    def roll_month(self, d: date, capital: float) -> None:
        if (d.year, d.month) != self.month:
            self.month_base = self.equity[-1][1] if self.equity else capital
            self.month = (d.year, d.month)


def _total(xs: Any) -> float | None:
    out = 0.0
    for x in xs:
        if x is None:
            return None  # unknown is never counted as 0
        out += x
    return out


@dataclass
class Replay:
    arms: dict[str, Arm]
    first: date | None  # the period's first and last session
    last: date | None
    sessions: int


def _expiry_day(days: list[date]) -> Callable[[date], date]:
    """engine.run's rule: the last stored session on or before an expiry date (the date itself beyond the calendar)."""
    return lambda e: e if not days or e > days[-1] else days[bisect_right(days, e) - 1]


class _Run:
    """What the arms share in one replay: the clock, each underlying's stored calendar, the IV readings and futures
    settles read so far (the IV filter and the betas), today's days, and caches."""

    def __init__(self, history: History, specs_: Sequence[Spec], start: date, end: date, book: Book,
                 rules: Sequence[str], warmup_days: int) -> None:  # fmt: skip
        self.h, self.book = Clocked(history), book
        # NIFTY is always read when another underlying is replayed: every beta is to NIFTY's front future
        unds = [s.underlying for s in specs_]
        self.syms = list(dict.fromkeys([*unds, *(["NIFTY"] if any(u != "NIFTY" for u in unds) else [])]))
        lo = start - timedelta(days=warmup_days)
        self.cal = {u: self.h.sessions(u, lo, history.latest(u) or lo) for u in self.syms}
        self.nxt = {u: dict(pairwise(v)) for u, v in self.cal.items()}  # each underlying's own next session
        self.eday = {u: _expiry_day(v) for u, v in self.cal.items()}
        self.sessions = sorted({d for v in self.cal.values() for d in v if d <= end})
        self.nxt_book = dict(
            pairwise(self.sessions)
        )  # the shock's horizon: the next stored session of the book
        order = sorted(
            rules, key=lambda r: r != "uncapped"
        )  # the card rule's Kelly reads uncapped's closed trades
        self.arms = {r: Arm(r, {s.key: engine.SpecState(s) for s in specs_}) for r in order}
        self.ivs: dict[str, list[float]] = {u: [] for u in self.syms}
        self.futs: dict[str, list[tuple[date, date, float]]] = {u: [] for u in self.syms}
        self.days: dict[str, Day | None] = {}
        self.last_day: dict[str, Day] = {}
        self._betas: dict[tuple[str, date], Any] = {}
        self._mu: dict[tuple[str, int], float | None] = {}

    def read(self, d: date) -> None:
        self.h.advance(d)
        self.days = {u: self.h.day(u, d) if d in self.cal[u] else None for u in self.syms}
        for u, x in self.days.items():
            if x is not None:
                if x.iv30 is not None:
                    self.ivs[u].append(x.iv30)
                self.futs[u].extend((d, e, f) for e, f in x.futures.items())
                self.last_day[u] = x

    def beta(self, u: str, d: date) -> Any:
        if (u, d) not in self._betas:
            self._betas[(u, d)] = shock.beta_as_of(self.futs, u, d)
        return self._betas[(u, d)]

    def mu_lo(self, key: str) -> float | None:
        arm = self.arms.get("uncapped")
        if arm is None:
            return None
        trades = arm.states[key].trades  # closed at or before this session: uncapped is managed first
        if (key, len(trades)) not in self._mu:
            self._mu[(key, len(trades))] = limits.kelly_mu_lo(trades)
        return self._mu[(key, len(trades))]

    def size(self, arm: Arm, c: entry.Candidate, x: Day, d: date) -> limits.Sized:
        card = arm.rule == "card_rule"
        moves = tp.SHOCK_MOVES if card else rspecs.PRIMARY_MOVES
        book_legs = shock.position_legs(arm.open())
        one = shock.legs(c.symbol, c.expiry, [(q.right, q.strike, c.lot if side == "buy" else -c.lot)
                                              for q, side in c.picked], x)  # fmt: skip
        unds = {leg.underlying for leg in (*book_legs, *one)} - {"NIFTY"}
        l_book, l_one, why = shock.pnls(book_legs, one, self.days, d, self.nxt_book[d],
                                        {u: self.beta(u, d) for u in unds}, moves, self.book.rate)  # fmt: skip
        s = limits.limit_lots(ml_one=None if c.loss_unit is None else c.loss_unit * c.lot, costs_one=c.one[1],  # type: ignore[arg-type]
                              open_risk=arm.open_risk(), margin_used=arm.margin_used(), im_one=c.one[2],
                              l_book=l_book, l_one=l_one, mtd=arm.value(self.book.capital) - arm.month_base,
                              moves=moves, kelly=card, mu_lo=self.mu_lo(c.spec.key) if card else None,
                              book=self.book)  # fmt: skip
        if s.binding == "shock" and (l_book is None or l_one is None):
            why_kind = " (beta)" if any("beta" in w for w in why) else " (a price or forward)"
            s = limits.Sized(s.lots, s.binding, s.reason + why_kind)
        return s

    def enter(self, arm: Arm, st: engine.SpecState, d: date) -> None:
        u = st.spec.underlying
        x = self.days.get(u)
        if x is None:
            return
        c = entry.candidate(st, x, u, self.h, self.ivs[u], self.book, self.nxt[u].get(d), self.eday[u])
        if c is None:
            return
        lots = c.n_trade
        if arm.rule != "uncapped":
            s = self.size(arm, c, x, d)
            if s.binding is not None:
                if s.lots < 1:
                    arm.refused[s.reason] += 1
                    return
                arm.cut[s.binding] += 1
            lots = s.lots
        fit = entry.fit_margin(c, lots, arm.open(), self.book)
        if isinstance(fit, str):
            st.skips[fit] += 1
            return
        if fit[0] < lots:
            arm.cut["the engine's margin rule"] += 1
        p = entry.place(st, c, *fit)
        arm.entries.append({"day": d.isoformat(), "spec": p.spec, "lots": p.lots, "trade_lots": c.n_trade,
                            "span_file": c.scan is not None})  # fmt: skip

    def square_off(self, arm: Arm) -> None:
        for st in arm.states.values():
            for p in list(st.open):
                x = self.days.get(p.symbol) or self.last_day[p.symbol]
                tr = engine.square_off(p, x, engine.mark(p, x), "period_end", self.book)
                st.trades.append(tr)
                st.realised += tr["net"]
                st.open.remove(p)


def replay(history: History, specs_: Sequence[Spec], start: date, end: date, *, book: Book = BOOK,
           rules: Sequence[str] = rspecs.ARMS, warmup_days: int = IV_WARMUP_DAYS) -> Replay:  # fmt: skip
    """Replay `specs_` (in entry order) concurrently on one book per arm in `rules` from `start` to the last stored
    session on or before `end` (module docstring); the sessions from `warmup_days` before `start` feed the IV filter
    and the betas only."""
    run = _Run(history, specs_, start, end, book, rules, warmup_days)
    period = [d for d in run.sessions if d >= start]
    if not period:
        return Replay(run.arms, None, None, 0)
    for d in run.sessions:
        run.read(d)
        if d < start:
            continue
        for arm in run.arms.values():
            arm.roll_month(d, book.capital)
            for st in arm.states.values():
                x = run.days.get(st.spec.underlying)
                if x is not None:
                    u = st.spec.underlying
                    engine.manage(st, x, run.nxt[u].get(d), book, run.eday[u])
            if d == period[-1]:
                run.square_off(arm)
            else:
                for st in arm.states.values():
                    run.enter(arm, st, d)
            arm.equity.append((d, arm.value(book.capital)))
            arm.risk.append((d, arm.open_risk()))
    return Replay(run.arms, period[0], period[-1], len(period))
