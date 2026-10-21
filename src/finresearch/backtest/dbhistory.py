"""`History` over the #306 tables of any database (the app's, or a read-only copy such as finresearch_fnohist).

Every session runs `SET TRANSACTION READ ONLY` first: the backtester never writes to the history it replays.
IV: fno_iv_eod.iv_30d of that day when the database has a derived series for the underlying; when it has none at all
(a copy without the derived table), the 30-day IV is computed from that day's own prices with the same function the
nightly derivation uses (fincalc.fno_history.day_summary), so it stays point-in-time either way. A day missing from
an existing series is unknown (None), never filled from a neighbour.
Lot size: the row's own lot (UDiFF, 2024 on), else fno_contract's lot changes on or before the day, else its single
inferred lot (pre-2024: the gcd of open interest, fnohist.derive.infer_lot). Unknown stays None and the trade is
skipped. The inferred lot is a property of the contract, which NSE publishes before trading starts, so reading it is
not look-ahead in market data.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from finresearch.backtest.history import RIGHTS, Day, Quote, within
from finresearch.db.models import FnoBhav, FnoContract, FnoIvEod
from finresearch.fincalc import fno_history as fh

SPAN_MAX_AGE_DAYS = 45  # a monthly SPAN sample covers the following weeks


class DbHistory:
    def __init__(self, url: str | None = None, span_ranges: bool = False) -> None:
        self.url = url
        self.span_ranges = (
            span_ranges  # #370: margin from the stored NSE SPAN ranges (opt-in, see span_params)
        )
        self._has_iv: dict[str, bool] = {}
        self._contracts: dict[str, dict[date, tuple[int | None, list[Any] | None]]] = {}
        self._row_lots: dict[tuple[str, date, date], int] = {}

    @contextmanager
    def _s(self) -> Iterator[Session]:
        from finresearch.db import session_scope

        with session_scope(self.url) as s:
            s.execute(text("SET TRANSACTION READ ONLY"))
            yield s

    def span_params(self, symbol: str, d: date) -> dict[str, Any] | None:
        """The newest stored NSE SPAN ranges on or before `d` within SPAN_MAX_AGE_DAYS (the backfill samples one file
        a month), when built with span_ranges=True; else None, and margins use margin_approx's labelled assumptions.
        Off by default: it changes the registered backtest's margin and so its lot caps (a decision, #370)."""
        if not self.span_ranges:
            return None
        from finresearch.fnohist import span

        with self._s() as s:
            return span.params_for(s, symbol, d, max_age_days=SPAN_MAX_AGE_DAYS)

    def sessions(self, symbol: str, start: date, end: date) -> list[date]:
        with self._s() as s:
            return list(s.scalars(select(FnoBhav.day).distinct()
                                  .where(FnoBhav.symbol == symbol, FnoBhav.day.between(start, end))
                                  .order_by(FnoBhav.day)))  # fmt: skip

    def latest(self, symbol: str) -> date | None:
        with self._s() as s:
            return s.scalar(select(func.max(FnoBhav.day)).where(FnoBhav.symbol == symbol))

    def has_iv(self, symbol: str) -> bool:
        if symbol not in self._has_iv:
            try:
                with self._s() as s:
                    n = s.scalar(select(func.count()).select_from(FnoIvEod)
                                 .where(FnoIvEod.symbol == symbol, FnoIvEod.iv_30d.is_not(None)))  # fmt: skip
            except SQLAlchemyError:
                n = 0
            self._has_iv[symbol] = bool(n)
        return self._has_iv[symbol]

    def day(self, symbol: str, d: date) -> Day | None:
        cols = (FnoBhav.instrument, FnoBhav.expiry, FnoBhav.strike, FnoBhav.option_type, FnoBhav.close,
                FnoBhav.settle, FnoBhav.contracts, FnoBhav.oi, FnoBhav.underlying, FnoBhav.lot_size)  # fmt: skip
        with self._s() as s:
            rows = s.execute(select(*cols).where(FnoBhav.symbol == symbol, FnoBhav.day == d,
                                                 FnoBhav.expiry <= d + timedelta(days=75))).all()  # fmt: skip
            iv = None
            if rows and self.has_iv(symbol):
                v = s.scalar(select(FnoIvEod.iv_30d).where(FnoIvEod.symbol == symbol, FnoIvEod.day == d))
                iv = None if v is None else float(v)
        if not rows:
            return None
        out = Day(d, symbol)
        for kind, exp, k, right, close, settle, n, oi, und, lot in rows:
            if und is not None:
                out.spot = float(und)
            if kind in ("IDF", "STF"):
                if settle is not None and settle > 0:
                    out.futures[exp] = float(settle)
            elif right in RIGHTS and within(d, exp):
                q = Quote(exp, RIGHTS[right], float(k), None if close is None else float(close),
                          None if settle is None else float(settle), int(n or 0), int(oi or 0), lot)  # fmt: skip
                out.options[(exp, q.right, q.strike)] = q
                if lot:
                    self._row_lots[(symbol, exp, d)] = int(lot)
        out.iv30 = iv if self.has_iv(symbol) else computed_iv30(out)
        return out

    def iv_history(self, symbol: str, start: date, end: date) -> list[float]:
        """The stored 30-day IVs from `start` to `end` (oldest first); [] without a derived series."""
        if not self.has_iv(symbol):
            return []
        with self._s() as s:
            return [float(v) for v in s.scalars(select(FnoIvEod.iv_30d).where(
                FnoIvEod.symbol == symbol, FnoIvEod.day.between(start, end), FnoIvEod.iv_30d.is_not(None))
                .order_by(FnoIvEod.day))]  # fmt: skip

    def lot(self, symbol: str, expiry: date, d: date) -> int | None:
        if (symbol, expiry, d) in self._row_lots:
            return self._row_lots[(symbol, expiry, d)]
        if symbol not in self._contracts:
            with self._s() as s:
                self._contracts[symbol] = {
                    e: (lot, ch) for e, lot, ch in s.execute(select(FnoContract.expiry, FnoContract.lot_size,
                                                                     FnoContract.lot_changes)
                                                              .where(FnoContract.symbol == symbol))}  # fmt: skip
        got = self._contracts[symbol].get(expiry)
        if got is None:
            return None
        lot, changes = got
        if changes:
            live = [int(x) for day, x in changes if date.fromisoformat(day) <= d]
            return live[-1] if live else int(changes[0][1])
        return lot


def computed_iv30(x: Day) -> float | None:
    """The 30-day IV of one day from its own traded options (fincalc.fno_history; see the module docstring)."""
    opts: dict[date, list[fh.OptQuote]] = {}
    for q in x.options.values():
        if q.settle and fh.market_settle(q.settle, q.close, q.contracts):
            opts.setdefault(q.expiry, []).append(fh.OptQuote(q.strike, q.right, q.settle))  # type: ignore[arg-type]
    if not opts:
        return None
    return fh.day_summary(x.day, opts, x.futures, x.spot).iv_30d


def top_stock(h: DbHistory, start: date, end: date) -> str | None:
    """specs' STOCK_RULE: the stock underlying with the most option contracts traded from `start` to `end` (the
    in-sample window) among those stored; None when no stock options are stored."""
    with h._s() as s:
        row = s.execute(select(FnoBhav.symbol, func.sum(FnoBhav.contracts).label("n"))
                        .where(FnoBhav.instrument == "STO", FnoBhav.day.between(start, end))
                        .group_by(FnoBhav.symbol).order_by(func.sum(FnoBhav.contracts).desc(), FnoBhav.symbol)
                        .limit(1)).first()  # fmt: skip
    return row[0] if row and row[1] else None
