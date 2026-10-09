"""Merge what a broker reports into the portfolio without ever double counting or overwriting the user's data.

Transactions stay the source of truth (portfolio.service). A sync adds rows through the same import pipeline as the
CSV/CAS importers (idempotent `dedupe_key`, one `portfolio_import` row per sync, lots rebuilt), labelled with the
connector's source (`groww_api`, `zerodha_api`, ...) in the broker's own account ("Groww", "Zerodha", ... — the same
label its tradebook importer uses, so FIFO continues across CSV and API rows in one demat account).

Merge rules (in order):

1. **Baseline** — a broker holding whose instrument has no transactions in that account, and no open units in any
   non-broker account (Manual, CAS folios), gets one `opening` row: the broker's quantity at its average price,
   acquisition date unknown (``meta.cost_basis = "broker_average"``: P&L works, the tax term says "date unknown" until
   the user enters it). It is marked ``statement_opening``: once older transactions are imported for that holding (a
   full tradebook), `lots.build_lots` ignores the baseline automatically. Baselines are a separate import row, so
   "delete import" undoes them in one click. Baseline day = the snapshot day (or the previous day when the broker's
   holdings exclude today's buys), and trades on or before it are not added again for that holding.
   When the broker also returned trade history for that instrument (Upstox: 3 financial years; Dhan: a date range),
   the trades are used instead: no baseline when they explain the whole quantity, else a baseline only for the
   residual units, dated the day before the first trade (cost = the broker's average × quantity less the trades'
   cost when there were no sales in the window, otherwise unknown).
2. **Trades** — each executed equity fill becomes a buy/sell. Re-syncing the same trade is a no-op (`dedupe_key` on the
   broker's trade id). Rows another source already has are skipped by the cross-source reconciliation that every
   import path shares (portfolio.dedupe, #236; tradebook and CAS uploads, the inbox, manual entries, holdings
   statements and this sync). Per instrument (ISIN / symbol / scheme code), account, day and side, incoming rows are
   compared with *other-source* rows of the same account, and with manual entries in any account:
   a. the same exchange trade id, or the same order id with equal total units → already present;
   b. two different exchange trade ids (Zerodha, Dhan: the exchange's own ids) → different trades, never merged
      (Groww's API ids are Groww's order ids, Upstox's unverified: there only a shared value counts);
   c. the same units at a price within 0.5 % → already present, one row against one row, so two genuine buys of 10
      against one stored buy of 10 leave one new;
   d. what is left adding up to the same units at the same average price (fills vs one order) → already present;
   e. anything else left on both sides is a partial overlap: a **conflict**, not added, reported for review.
   Two broker accounts are never matched (the same buy in two demat accounts is two trades). Previews say "already
   present from <source>" for such rows, never "new"; a manual entry that matches is refused unless confirmed.
3. **Never overwrite** — a sync never edits or deletes rows from another source (manual, CAS, CSV). Differences
   between the broker's quantities and the app's lots are reported (sync log + Connections page), never "fixed".
4. **Positions and funds** are stored on the connection for display; they never become lots.
5. **Mutual funds from a broker** (Kite Coin) are reconciled by instrument across every account (a CAS is the better
   source for fund history); a baseline is created only for a fund the app does not hold anywhere. When a CAS imported
   later covers the baseline's units on its day, the baseline is superseded (`meta.superseded_by`, set by
   service.rebuild; lots, XIRR and history ignore it; deleting the CAS brings it back); a CAS with fewer units is a
   conflict. The baseline's units, cost and date are never edited (only that derived marker is set and cleared).
6. **Unsupported corporate actions** (#237): a reconciliation row whose holding has an unresolved demerger, rights
   issue, merger ... (service.record_unsupported) names it in `pending_action`: it may explain the difference.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioHolding, PortfolioImport, PortfolioLot, PortfolioTxn
from finresearch.portfolio.connectors.base import BrokerHolding, BrokerTrade
from finresearch.portfolio.dedupe import instrument_holdings
from finresearch.portfolio.importers import ImportedTxn, instrument_key
from finresearch.portfolio.lots import Event, build_lots, superseded_openings
from finresearch.portfolio.service import actions_of, add_txns, events_of, find_holding, rebuild

UNITS_TOL = Decimal("0.001")
BROKER_ACCOUNTS = {"Groww", "Zerodha", "Upstox", "Dhan"}


@dataclass
class MergeResult:
    added: int = 0
    duplicates: int = 0  # same source, already imported (idempotent re-sync)
    updated: int = 0  # an order read again with more fills: its stored row updated in place (upsert_orders)
    cross_source: list[dict[str, Any]] = field(default_factory=list)  # skipped: another source has it
    conflicts: list[dict[str, Any]] = field(default_factory=list)  # partial overlaps: not added, review
    covered_by_baseline: int = 0
    baselines: list[dict[str, Any]] = field(default_factory=list)
    baseline_skipped: list[dict[str, Any]] = field(default_factory=list)
    history_used: list[dict[str, Any]] = field(
        default_factory=list
    )  # holdings rebuilt from the broker's trades
    import_ids: list[int] = field(default_factory=list)
    reconciliation: list[dict[str, Any]] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        rec_bad = [r for r in self.reconciliation if not r["ok"]]
        return {"added": self.added, "duplicates": self.duplicates, "updated": self.updated,
                "cross_source": self.cross_source[:50],
                "conflicts": self.conflicts[:50], "covered_by_baseline": self.covered_by_baseline,
                "baselines": self.baselines[:100], "baseline_skipped": self.baseline_skipped[:50],
                "history_used": self.history_used[:100],
                "import_ids": self.import_ids, "reconciliation": self.reconciliation[:300],
                "reconciled": not rec_bad, "differences": len(rec_bad), "skipped": self.skipped}  # fmt: skip


# --------------------------------------------------------------------------- mapping (pure)
def holding_txn(h: BrokerHolding, *, account: str, source: str, day: date) -> ImportedTxn:
    """The baseline row for a broker holding (see rule 1)."""
    amount = h.quantity * h.avg_price if h.avg_price is not None else None
    return ImportedTxn(account=account, asset_type=h.asset_type, name=h.name or h.symbol or h.isin or "?", day=day,
                       kind="opening", quantity=h.quantity, price=h.avg_price, amount=amount, source=source,
                       isin=h.isin, nse_symbol=h.symbol if h.asset_type == "stock" else None, bse_code=h.bse_code,
                       scheme_code=h.scheme_code,
                       meta={"statement_opening": True, "baseline": True, "exchange": h.exchange,
                             **({"cost_basis": "broker_average"} if h.avg_price is not None else {}),
                             "note": "broker holdings baseline: average cost, purchase date unknown"},
                       ext=f"baseline:{day.isoformat()}")  # fmt: skip


def trade_txn(t: BrokerTrade, *, account: str, source: str) -> ImportedTxn:
    meta = {"exchange": t.exchange, "order_id": t.order_id, "trade_id": t.trade_id, "product": t.product,
            "via": "api"}  # fmt: skip
    return ImportedTxn(account=account, asset_type="stock", name=t.name or t.symbol or t.isin or "?", day=t.day,
                       kind="buy" if t.side == "buy" else "sell", quantity=t.quantity, price=t.price,
                       amount=t.quantity * t.price, source=source, isin=t.isin, nse_symbol=t.symbol,
                       meta={k: v for k, v in meta.items() if v not in (None, "")},
                       ext=f"{t.exchange or ''}:{t.trade_id or ''}:{t.order_id or ''}:{t.executed_at or ''}")  # fmt: skip


def _same_instrument(t: BrokerTrade, h: BrokerHolding) -> bool:
    if t.isin and h.isin:
        return t.isin == h.isin
    return bool(t.symbol and h.symbol and t.symbol.upper() == h.symbol.upper())


def holding_ikey(h: BrokerHolding) -> str:
    return instrument_key(h.asset_type, h.isin, h.symbol, h.bse_code, h.scheme_code, h.name)


# --------------------------------------------------------------------------- database helpers
_all_matches = instrument_holdings  # every holding of the instrument, in any account


def _open_units(s: Session, holding_id: int) -> Decimal:
    return Decimal(s.scalar(select(func.coalesce(func.sum(PortfolioLot.open_quantity), 0))
                            .where(PortfolioLot.holding_id == holding_id)) or 0)  # fmt: skip


def _txn_count(s: Session, holding_id: int) -> int:
    return int(
        s.scalar(select(func.count()).select_from(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id))
    )


def _baseline_day(s: Session, holding_id: int) -> date | None:
    """The latest broker baseline of a holding, whichever source made it (API sync or a holdings statement)."""
    rows = s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id)).all()
    skip = superseded_openings(
        rows
    )  # a baseline the lots ignore (older history imported since) covers nothing
    days = [
        r.day
        for i, r in enumerate(rows)
        if r.kind == "opening" and (r.meta or {}).get("baseline") and i not in skip
    ]
    return max(days) if days else None


def _new_import(s: Session, kind: str, source: str, label: str, now: datetime) -> PortfolioImport:
    sha = hashlib.sha256(f"{source}|{kind}|{now.isoformat()}|{secrets.token_hex(8)}".encode()).hexdigest()
    imp = PortfolioImport(kind=kind, source=source[:20], filename=f"{label} {kind} {now:%Y-%m-%d %H:%M}"[:300],
                          sha256=sha, saved_path=None, summary={})  # fmt: skip
    s.add(imp)
    s.flush()
    return imp


def _probe(units: Decimal, price: Decimal, day: date) -> Event:
    return Event(-1, day, "opening", units, price, None,
                 meta={"statement_opening": True, "cost_basis": "broker_average"})  # fmt: skip


def opening_price(events: list[Event], units: Decimal, h: BrokerHolding, day: date) -> Decimal | None:
    """The per-unit cost of `units` held before `events` such that, after a FIFO replay, the open lots cost what the
    broker says (its quantity × average price). The open cost is linear in that price: one replay at price 1 gives
    the open cost per rupee of opening price (the opening lot's open units, divided by any split inside the history,
    since a split divides the cost per unit). When every one of those older units was sold inside the history, the
    broker's average is the best estimate."""
    if h.avg_price is None:
        return None
    book = build_lots([_probe(units, Decimal(1), day), *events])
    mine = [lot for lot in book.lots if lot.txn_id == -1]
    left = sum((lot.open_quantity for lot in mine), Decimal(0))
    rest = [lot for lot in book.open_lots if lot.txn_id != -1]
    if any(lot.cost_per_unit is None for lot in rest):
        return None
    if left <= UNITS_TOL:
        return h.avg_price
    per_rupee = sum((lot.open_cost or Decimal(0) for lot in mine), Decimal(0))
    p = (
        h.quantity * h.avg_price - sum((lot.open_cost or Decimal(0) for lot in rest), Decimal(0))
    ) / per_rupee
    return p.quantize(Decimal("0.0001")) if p > 0 else None


def older_units(events: list[Event], held: Decimal, day: date) -> Decimal:
    """How many units held before `events` (an opening on `day`) make the FIFO replay end with `held` units. Without
    corporate actions that is held - (units the history leaves) + (units it sold beyond its own buys). A split or
    bonus inside the history multiplies the older units too, so the count is solved on the replay itself (units at
    the end are affine in the opening units: two replays give the slope)."""
    book = build_lots(events)
    oversold = sum(
        (d.quantity for d in book.disposals if d.lot is None), Decimal(0)
    )  # sales of the older units
    x1 = held - book.units + oversold
    if x1 <= UNITS_TOL:
        return x1
    u1 = build_lots([_probe(x1, Decimal(0), day), *events]).units
    if abs(u1 - held) <= UNITS_TOL:
        return x1
    u2 = build_lots([_probe(2 * x1, Decimal(0), day), *events]).units
    slope = (u2 - u1) / x1
    if slope <= 0:
        return x1
    return (x1 + (held - u1) / slope).quantize(Decimal("0.000001"))


def _older_units(
    s: Session, holding_id: int, h: BrokerHolding, *, account: str, source: str
) -> ImportedTxn | None:
    """A baseline for the units the broker holds that the account's history does not explain, dated the day before
    that history starts. None when the history explains them, or an opening balance already stands for them (an
    opening the lots ignore, superseded by older history, does not)."""
    txns = s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id)).all()
    if not txns:
        return None
    skip = superseded_openings(txns)
    if any(t.kind == "opening" and i not in skip for i, t in enumerate(txns)):
        return None
    evs = events_of(txns)
    day = min(t.day for t in txns) - timedelta(days=1)
    residual = older_units(evs, h.quantity, day)
    if residual <= UNITS_TOL:
        return None
    price = opening_price(evs, residual, h, day)
    older = holding_txn(
        replace(h, quantity=residual, avg_price=price), account=account, source=source, day=day
    )
    older.meta["note"] = ("units held before the imported trade history: " + (
        "cost estimated from the broker's average price" if price is not None else "cost unknown"))  # fmt: skip
    return older


def statement_day(filename: str, when: datetime) -> date:
    """The date a broker holdings statement is as of: the yyyy-mm-dd in its filename (Groww:
    Stocks_Holdings_Statement_<id>_<yyyy-mm-dd>.xlsx), else the IST calendar day of `when` (the upload or the file's
    modification time). Never the UTC day: between 00:00 and 05:30 IST that is yesterday, which would date the
    baseline a day early and treat that day's trades as already covered by it. Never after `when`'s IST day."""
    from finresearch.fincalc.dates import to_ist

    day = to_ist(when).date()
    dated = re.findall(r"(20\d\d-\d\d-\d\d)", filename)
    try:
        return min(date.fromisoformat(dated[-1]), day) if dated else day
    except ValueError:
        return day


def remember_statement_prices(s: Session, *, account: str, holdings: list[BrokerHolding], day: date,
                              label: str) -> None:  # fmt: skip
    """Keep each holding's closing price from the broker's statement: valuation falls back to it when no exchange
    prices the stock (unlisted shares, an NCD that has not traded)."""
    for h in holdings:
        if h.last_price is None or h.last_price <= 0:
            continue
        mine = find_holding(s, holding_txn(h, account=account, source="", day=day))
        prev = ((mine.meta or {}).get("statement_price") or {}) if mine is not None else {}
        if (
            mine is not None and str(prev.get("day") or "") <= day.isoformat()
        ):  # an older statement never wins
            mine.meta = {**(mine.meta or {}), "statement_price": {"price": str(h.last_price), "day": day.isoformat(),
                                                                  "source": label}}  # fmt: skip


@dataclass
class Upserted:
    updated: int = 0
    same: int = 0
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    holdings: set[int] = field(default_factory=set)


def _q6(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.000001"))  # portfolio_txn keeps 6 decimals


def upsert_orders(s: Session, rows: list[ImportedTxn], *, account: str,
                  source: str) -> tuple[list[ImportedTxn], Upserted]:  # fmt: skip
    """Rows of a broker that reports orders, not fills (Groww: filled quantity and average fill price *so far*). A
    row whose order id this source already stored in this account is that same order read again: unchanged → a
    duplicate; more fills (or a corrected day) → the stored row takes the new quantity, price, amount and day, so an
    order synced at 40 filled and again at 100 is one row of 100, never 140 (#266). Returns the rows still to add.
    Two stored rows for one order (left by syncs before #266) are not guessed at: reported as a conflict."""
    out, up = [], Upserted()
    for t in rows:
        oid = str((t.meta or {}).get("order_id") or "")
        if not oid:
            out.append(t)
            continue
        old = s.scalars(select(PortfolioTxn).join(PortfolioHolding, PortfolioHolding.id == PortfolioTxn.holding_id)
                        .where(PortfolioTxn.source == source, PortfolioHolding.account == account,
                               PortfolioTxn.kind == t.kind, PortfolioTxn.meta["order_id"].astext == oid)
                        .order_by(PortfolioTxn.id)).all()  # fmt: skip
        if not old:
            out.append(t)
            continue
        if len(old) > 1:
            up.conflicts.append({"name": t.name, "account": account, "day": t.day.isoformat(), "kind": t.kind,
                                 "units": str(t.quantity), "other_units": str(sum(o.quantity or 0 for o in old)),
                                 "sources": [source],
                                 "why": f"order {oid} is stored {len(old)} times (synced before its fills were "
                                        "complete): delete the extra rows, then sync again"})  # fmt: skip
            continue
        row = old[0]
        if (_q6(row.quantity), _q6(row.price), row.day) == (_q6(t.quantity), _q6(t.price), t.day):
            up.same += 1
            continue
        row.quantity, row.price, row.amount, row.day = t.quantity, t.price, t.amount, t.day
        row.meta = {**(row.meta or {}), **t.meta}
        key = t.dedupe_key(0)
        if s.scalar(select(PortfolioTxn.id).where(PortfolioTxn.dedupe_key == key)) is None:
            row.dedupe_key = key  # a later identical read is then a plain same-source duplicate too
        up.updated += 1
        up.holdings.add(row.holding_id)
    s.flush()
    return out, up


# --------------------------------------------------------------------------- the merge
def merge_sync(s: Session, *, account: str, source: str, label: str, holdings: list[BrokerHolding],
               trades: list[BrokerTrade], today: date, now: datetime, holdings_include_today: bool = True,
               mf_holdings: list[BrokerHolding] | None = None, allow_baseline: bool = True,
               order_keyed: bool = False) -> MergeResult:  # fmt: skip
    """`order_keyed`: the broker reports one row per order, not per fill (BrokerConnector.fills_aggregated), so
    a stored row of the same order id is updated in place instead of a second row being added (upsert_orders)."""
    res = MergeResult()
    snap_day = today if holdings_include_today else today - timedelta(days=1)
    touched: set[int] = set()

    # 1. baselines (before trades: a holding with no history is represented by the snapshot, trades up to it included)
    base_rows: list[ImportedTxn] = []
    fresh_baseline: dict[str, date] = {}
    for h in holdings if allow_baseline else []:
        if h.quantity <= 0:
            continue
        t = holding_txn(h, account=account, source=source, day=snap_day)
        mine = find_holding(s, t)
        if mine is not None and _txn_count(s, mine.id):
            # the account already has history for it (an earlier tradebook): only the units held before that history
            # are missing, if any (a tradebook that starts after the first purchase). Skipped when this sync brings
            # trades for it too: those are not in the lots yet, so the gap cannot be measured here.
            fresh = any(_same_instrument(tr, h) for tr in trades)
            older = None if fresh else _older_units(s, mine.id, h, account=account, source=source)
            if older is not None:
                base_rows.append(older)
                res.history_used.append({"name": t.name, "trades": _txn_count(s, mine.id),
                                         "baseline_units": str(older.quantity)})  # fmt: skip
            continue
        elsewhere = [x for x in _all_matches(s, t) if x.account != account and x.account not in BROKER_ACCOUNTS
                     and _open_units(s, x.id) > UNITS_TOL]  # fmt: skip
        if elsewhere:
            res.baseline_skipped.append({"name": t.name, "ikey": t.ikey, "broker_units": str(h.quantity),
                                         "held_in": sorted({x.account for x in elsewhere}),
                                         "why": "also held in another account in the app: move or delete that entry "
                                                "if it is this holding, then sync again"})  # fmt: skip
            continue
        hist = [tr for tr in trades if _same_instrument(tr, h) and tr.day <= snap_day]
        if hist:  # the broker gave trade history for it: use it, and a baseline only for the units before it
            net = sum((tr.quantity if tr.side == "buy" else -tr.quantity for tr in hist), Decimal(0))
            residual = h.quantity - net
            if abs(residual) <= UNITS_TOL:
                res.history_used.append({"name": t.name, "trades": len(hist), "baseline_units": "0"})
                continue  # the trades explain the whole holding: no baseline at all
            if residual > 0:
                first = min(tr.day for tr in hist) - timedelta(days=1)
                price = None
                if h.avg_price is not None and not any(tr.side == "sell" for tr in hist):
                    rest_cost = h.quantity * h.avg_price - sum(
                        (tr.quantity * tr.price for tr in hist), Decimal(0)
                    )
                    price = (rest_cost / residual).quantize(Decimal("0.0001")) if rest_cost > 0 else None
                older = holding_txn(replace(h, quantity=residual, avg_price=price), account=account, source=source,
                                    day=first)  # fmt: skip
                older.meta["note"] = ("units held before the broker's trade history: " + (
                    "cost from the broker's average less the trades" if price is not None else "cost unknown"))  # fmt: skip
                base_rows.append(older)
                fresh_baseline[t.ikey] = first
                res.history_used.append(
                    {"name": t.name, "trades": len(hist), "baseline_units": str(residual)}
                )
                continue
            # the trades add up to more than the broker holds (a sale not in its holdings yet): snapshot only
        base_rows.append(t)
        fresh_baseline[t.ikey] = snap_day
    for h in mf_holdings or []:
        if h.quantity <= 0:
            continue
        t = holding_txn(h, account=f"{account} MF", source=source, day=snap_day)
        if any(_open_units(s, x.id) > UNITS_TOL or _txn_count(s, x.id) for x in _all_matches(s, t)):
            continue  # the fund is already tracked (usually from a CAS): reconcile by instrument only
        if allow_baseline:
            base_rows.append(t)
    if base_rows:
        imp = _new_import(s, "baseline", source, label, now)
        applied = add_txns(s, base_rows, imp.id)
        touched |= applied.holdings
        if applied.added:
            res.import_ids.append(imp.id)
            res.baselines += [{"name": t.name, "units": str(t.quantity), "day": t.day.isoformat(),
                               "avg_price": str(t.price) if t.price else None} for t in base_rows]  # fmt: skip
            imp.summary = {"rows": len(base_rows), "added": applied.added, "baseline": True, "day": snap_day.isoformat(),
                           "note": "broker holdings snapshot at average cost (purchase dates unknown)"}  # fmt: skip
        else:
            s.delete(imp)

    # 2. trades
    txns = [trade_txn(t, account=account, source=source) for t in trades]
    keep: list[ImportedTxn] = []
    groups: dict[tuple[str, date, str], list[ImportedTxn]] = defaultdict(list)
    for t in txns:
        groups[(t.ikey, t.day, t.kind)].append(t)
    for (_ik, day, _kind), rows in groups.items():
        h = find_holding(s, rows[0])
        if h is None:
            keep += rows
            continue
        bday = fresh_baseline.get(rows[0].ikey) or _baseline_day(s, h.id)
        if bday is not None and day <= bday:
            res.covered_by_baseline += len(rows)
            continue
        keep += rows  # rows other sources already have are caught by add_txns (portfolio.dedupe, rule 2)
    if order_keyed and keep:
        keep, up = upsert_orders(s, keep, account=account, source=source)
        res.updated += up.updated
        res.duplicates += up.same
        res.conflicts += up.conflicts
        touched |= up.holdings
    if keep:
        imp = _new_import(s, "api", source, label, now)
        applied = add_txns(s, keep, imp.id)
        res.added += applied.added
        res.duplicates += applied.duplicates
        res.cross_source += applied.cross_source
        res.conflicts += applied.conflicts
        touched |= applied.holdings
        if applied.added:
            res.import_ids.append(imp.id)
            days = [t.day for t in keep]
            imp.summary = {"rows": len(keep), "added": applied.added, "duplicates": applied.duplicates,
                           "period": [min(days).isoformat(), max(days).isoformat()]}  # fmt: skip
        else:
            s.delete(imp)
    s.flush()
    for hid in touched:
        rebuild(s, hid)
    s.flush()

    # 3. reconciliation (never "fixed" automatically). A snapshot that excludes today's trades is compared with the
    # lots as of its own day: today's buys and sells are not in it yet, which is no difference.
    res.reconciliation = reconcile_snapshot(s, account=account, holdings=holdings,
                                            as_of=None if holdings_include_today else snap_day)  # fmt: skip
    if mf_holdings:
        res.reconciliation += reconcile_snapshot(s, account=f"{account} MF", holdings=mf_holdings,
                                                 by_instrument=True)  # fmt: skip
    return res


def _units_as_of(s: Session, holding_id: int, as_of: date | None) -> Decimal:
    """The holding's open units now (its lots), or at the end of `as_of` (a FIFO replay of the rows up to that day)."""
    if as_of is None:
        return _open_units(s, holding_id)
    txns = s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id,
                                                PortfolioTxn.day <= as_of)).all()  # fmt: skip
    return build_lots(events_of(txns)).units if txns else Decimal(0)


def reconcile_snapshot(s: Session, *, account: str, holdings: list[BrokerHolding], by_instrument: bool = False,
                       as_of: date | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Broker quantity vs the app's open lot units, per instrument; plus instruments the app holds in this account
    that the broker no longer reports. `as_of`: the day the broker's snapshot stands for when it excludes today's
    trades (BrokerConnector.holdings_include_today False): the app's units are then those at the end of that day."""
    if by_instrument:
        as_of = None  # fund holdings (Kite Coin) are reported as of now
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    for h in holdings:
        t = holding_txn(h, account=account, source="", day=date.today())
        if by_instrument:
            matches = _all_matches(s, t)
        else:
            one = find_holding(s, t)
            matches = [one] if one else []
        have = sum((_units_as_of(s, x.id, as_of) for x in matches), Decimal(0))
        seen |= {x.id for x in matches}
        diff = have - h.quantity
        acts = "; ".join(a["reason"] for x in matches for a in actions_of(x.meta)) or None
        out.append({"name": t.name, "ikey": t.ikey, "account": account, "broker_units": str(h.quantity),
                    "app_units": str(have.quantize(Decimal("0.001"))), "diff": str(diff.quantize(Decimal("0.001"))),
                    "ok": abs(diff) <= UNITS_TOL,
                    "status": "ok" if abs(diff) <= UNITS_TOL else ("missing_in_app" if not matches else "differs"),
                    "broker_avg_price": str(h.avg_price) if h.avg_price is not None else None,
                    "pending_action": acts if abs(diff) > UNITS_TOL else None})  # fmt: skip
    if not by_instrument:
        for x in s.scalars(select(PortfolioHolding).where(PortfolioHolding.account == account)):
            if x.id in seen:
                continue
            units = _units_as_of(s, x.id, as_of)
            if units > UNITS_TOL:
                out.append({"name": x.name, "ikey": x.ikey, "account": account, "broker_units": "0",
                            "app_units": str(units.quantize(Decimal("0.001"))),
                            "diff": str(units.quantize(Decimal("0.001"))), "ok": False, "status": "not_at_broker",
                            "broker_avg_price": None,
                            "pending_action": "; ".join(a["reason"] for a in actions_of(x.meta)) or None})  # fmt: skip
    return out
