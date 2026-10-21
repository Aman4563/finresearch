"""`History` over the #306 tables of any database (the app's, or a read-only copy such as finresearch_fnohist).

Every session runs `SET TRANSACTION READ ONLY` first: the backtester never writes to the history it replays.
IV: fno_iv_eod.iv_30d of that day when the database has a derived series for the underlying; when it has none at all
(a copy without the derived table), the 30-day IV is computed from that day's own prices with the same function the
nightly derivation uses (fincalc.fno_history.day_summary), so it stays point-in-time either way. A day missing from
an existing series is unknown (None), never filled from a neighbour.
Lot size: on a vault day (2008-2021) the dated circular table only (backtest.lots, #365); later, the row's own lot
(UDiFF, 2024 on), else fno_contract's lot changes on or before the day, else its single
inferred lot (pre-2024: the gcd of open interest, fnohist.derive.infer_lot). Unknown stays None and the trade is
skipped. The inferred lot is a property of the contract, which NSE publishes before trading starts, so reading it is
not look-ahead in market data.
The vault (#364): a database whose stored F&O history starts on or before vault.registry.VAULT_END (31-Dec-2021) is
the sealed 2008-2021 vault; it is read only with a live grant from vault.guard.vault_read for this URL, else every
read raises VaultSealed (checked once per instance, and again on each vault-period day returned).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from finresearch.backtest import lots
from finresearch.backtest.history import RIGHTS, Day, Quote, within
from finresearch.db.models import FnoBhav, FnoContract, FnoIvEod
from finresearch.fincalc import fno_history as fh
from finresearch.vault.registry import VAULT_END, VaultSealed

SPAN_MAX_AGE_DAYS = 45  # a monthly SPAN sample covers the following weeks


class DbHistory:
    def __init__(self, url: str | None = None, span_ranges: bool = False, vault_grant: Any = None) -> None:
        self.url = url
        self.vault_grant = vault_grant  # vault.guard.Grant for a registered vault read (#364)
        self._sealed_checked = False
        self._earliest: date | None = None  # the first stored F&O day, read once
        self.span_ranges = (
            span_ranges  # #370: margin from the stored NSE SPAN ranges (opt-in, see span_params)
        )
        self._has_iv: dict[str, bool] = {}
        self._contracts: dict[str, dict[date, tuple[int | None, list[Any] | None, str | None]]] = {}
        self._row_lots: dict[tuple[str, date, date], int] = {}

    @contextmanager
    def _s(self) -> Iterator[Session]:
        from finresearch.db import session_scope

        with session_scope(self.url) as s:
            s.execute(text("SET TRANSACTION READ ONLY"))
            if not self._sealed_checked:
                self._earliest = s.scalar(select(func.min(FnoBhav.day)))
                self._sealed_checked = True
            self._vault_check(self._earliest)  # every session: a grant closed since refuses further reads
            yield s

    def _vault_check(self, earliest: date | None) -> None:
        """VaultSealed when `earliest` is a vault-period day and this instance has no live grant for its URL."""
        from finresearch.vault.guard import check_grant

        if earliest is not None and earliest <= VAULT_END and not check_grant(self.vault_grant, self.url):
            raise VaultSealed(f"this database holds F&O rows from {earliest:%d-%b-%Y}, inside the sealed 2008-2021 "
                              "vault: read it only through finresearch.vault.guard.vault_read (#364)")  # fmt: skip

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
                FnoBhav.settle, FnoBhav.contracts, FnoBhav.oi, FnoBhav.underlying, FnoBhav.lot_size, FnoBhav.open,
                FnoBhav.high, FnoBhav.low)  # fmt: skip
        with self._s() as s:
            rows = s.execute(select(*cols).where(FnoBhav.symbol == symbol, FnoBhav.day == d,
                                                 FnoBhav.expiry <= d + timedelta(days=75))).all()  # fmt: skip
            iv = None
            if rows and self.has_iv(symbol):
                v = s.scalar(select(FnoIvEod.iv_30d).where(FnoIvEod.symbol == symbol, FnoIvEod.day == d))
                iv = None if v is None else float(v)
        if not rows:
            return None
        if d <= VAULT_END:
            self._vault_check(d)
        out = Day(d, symbol)
        for kind, exp, k, right, close, settle, n, oi, und, lot, o, hi, lo in rows:
            if und is not None:
                out.spot = float(und)
            if kind in ("IDF", "STF"):
                if settle is not None and settle > 0:
                    out.futures[exp] = float(settle)
            elif right in RIGHTS and within(d, exp):
                ohl = (
                    (_px(o), _px(hi), _px(lo)) if n and close else (None, None, None)
                )  # untraded: no trade prices
                q = Quote(exp, RIGHTS[right], float(k), None if close is None else float(close),
                          None if settle is None else float(settle), int(n or 0), int(oi or 0), lot, *ohl)  # fmt: skip
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
            rows = s.execute(select(FnoIvEod.day, FnoIvEod.iv_30d).where(
                FnoIvEod.symbol == symbol, FnoIvEod.day.between(start, end), FnoIvEod.iv_30d.is_not(None))
                .order_by(FnoIvEod.day)).all()  # fmt: skip
        if rows and rows[0][0] <= VAULT_END:
            self._vault_check(rows[0][0])
        return [float(v) for _, v in rows]

    def lot(self, symbol: str, expiry: date, d: date) -> int | None:
        if (
            d <= VAULT_END
        ):  # the vault: NSE's circulars only (backtest.lots), never the file or the gcd of OI (#365)
            return lots.lot_for(symbol, expiry, d, self._contract(symbol, expiry)[2])
        if (symbol, expiry, d) in self._row_lots:
            return self._row_lots[(symbol, expiry, d)]
        got = self._contract(symbol, expiry)
        if got == (None, None, None):
            return None
        lot, changes, _kind = got
        if changes:
            live = [int(x) for day, x in changes if date.fromisoformat(day) <= d]
            return live[-1] if live else int(changes[0][1])
        return lot

    def _contract(self, symbol: str, expiry: date) -> tuple[int | None, list[Any] | None, str | None]:
        """fno_contract's (lot, lot changes, kind) for one contract; all None when it has no row."""
        if symbol not in self._contracts:
            with self._s() as s:
                self._contracts[symbol] = {
                    e: (lot, ch, k) for e, lot, ch, k in s.execute(
                        select(FnoContract.expiry, FnoContract.lot_size, FnoContract.lot_changes, FnoContract.kind)
                        .where(FnoContract.symbol == symbol))}  # fmt: skip
        return self._contracts[symbol].get(expiry, (None, None, None))


def _px(v: Any) -> float | None:
    return float(v) if v is not None and v > 0 else None


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
