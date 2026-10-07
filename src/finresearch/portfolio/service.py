"""Portfolio storage: apply imports, manual edits and corporate actions to the database, and rebuild FIFO lots.

Transactions are the source of truth. After any change to a holding's transactions its lots and disposals are
rebuilt from scratch by `lots.build_lots`, so the derived tables can never drift from the transactions.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from finresearch.db.models import (
    PortfolioDisposal,
    PortfolioHolding,
    PortfolioImport,
    PortfolioLot,
    PortfolioTxn,
)
from finresearch.portfolio.dedupe import Check, check, check_fund_baselines, fund_baseline_holdings
from finresearch.portfolio.importers import (
    ClosingBalance,
    ImportedTxn,
    ImportResult,
    assign_dedupe_keys,
)
from finresearch.portfolio.lots import Event, LotBook, build_lots

UNITS_TOLERANCE = Decimal("0.001")  # CAS units are printed to 3 decimals


# --------------------------------------------------------------------------- lots
def events_of(txns: Iterable[PortfolioTxn]) -> list[Event]:
    return [Event(t.id, t.day, t.kind, t.quantity, t.price, t.amount, t.charges or Decimal(0), t.stt_paid,
                  dict(t.meta or {})) for t in txns]  # fmt: skip


def rebuild(s: Session, holding_id: int) -> LotBook:
    """Replace a holding's lots and disposals with a fresh FIFO replay of its transactions."""
    txns = s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id)).all()
    h = s.get(PortfolioHolding, holding_id)
    if (
        h is not None and h.asset_type == "mf"
    ):  # a broker fund baseline a CAS covers is ignored (portfolio.dedupe)
        from finresearch.portfolio.dedupe import superseded_baselines

        sup = superseded_baselines(s, h, txns)
        for t in txns:
            m = dict(t.meta or {})
            if sup.get(t.id) != m.get("superseded_by"):
                m.pop("superseded_by", None)
                t.meta = {**m, **({"superseded_by": sup[t.id]} if t.id in sup else {})}
    book = build_lots(events_of(txns))
    s.execute(delete(PortfolioDisposal).where(PortfolioDisposal.holding_id == holding_id))
    s.execute(delete(PortfolioLot).where(PortfolioLot.holding_id == holding_id))
    s.flush()
    rows: dict[int, PortfolioLot] = {}
    for lot in book.lots:
        row = PortfolioLot(holding_id=holding_id, txn_id=lot.txn_id, acquired=lot.acquired, origin=lot.origin,
                           quantity=lot.quantity, open_quantity=lot.open_quantity, cost_per_unit=lot.cost_per_unit,
                           stt_paid=lot.stt_paid)  # fmt: skip
        s.add(row)
        rows[id(lot)] = row
    s.flush()
    for d in book.disposals:
        s.add(PortfolioDisposal(holding_id=holding_id, txn_id=d.txn_id, lot_id=rows[id(d.lot)].id if d.lot else None,
                                acquired=d.acquired, sold=d.sold, quantity=d.quantity, cost=d.cost,
                                proceeds=d.proceeds, stt_paid=d.stt_paid,
                                origin="intraday" if d.intraday else d.origin))  # fmt: skip
    if h is not None:
        h.meta = {**(h.meta or {}), "lot_warnings": book.warnings[:20]}
        h.updated_at = func.now()
    return book


# --------------------------------------------------------------------------- holdings
def find_holding(s: Session, t: ImportedTxn, account: str | None = None) -> PortfolioHolding | None:
    """The holding `t` belongs to in `account` (default: its own), matched by key or by any shared identifier (a
    manual INFY buy vs the tradebook's ISIN row). `account=""` searches every account."""
    acc = t.account if account is None else account
    conds = [PortfolioHolding.account == acc] if acc else []
    h = s.scalars(select(PortfolioHolding).where(PortfolioHolding.ikey == t.ikey, *conds)
                  .order_by(PortfolioHolding.id)).first()  # fmt: skip
    if h is None:
        ids = [(col, v) for col, v in ((PortfolioHolding.isin, t.isin), (PortfolioHolding.nse_symbol, t.nse_symbol),
                                       (PortfolioHolding.bse_code, t.bse_code),
                                       (PortfolioHolding.scheme_code, t.scheme_code)) if v]  # fmt: skip
        if ids:
            from sqlalchemy import or_

            h = s.scalars(select(PortfolioHolding).where(*conds, or_(*[col == v for col, v in ids]))
                          .order_by(PortfolioHolding.id)).first()  # fmt: skip
    return h


def get_or_create_holding(s: Session, t: ImportedTxn) -> PortfolioHolding:
    h = find_holding(s, t)
    if h is None:
        h = PortfolioHolding(ikey=t.ikey, account=t.account, asset_type=t.asset_type, name=t.name[:300], isin=t.isin,
                             nse_symbol=t.nse_symbol, bse_code=t.bse_code, scheme_code=t.scheme_code, meta={})  # fmt: skip
        s.add(h)
        s.flush()
    else:  # fill identifiers a later file knows
        h.isin = h.isin or t.isin
        h.nse_symbol = h.nse_symbol or t.nse_symbol
        h.bse_code = h.bse_code or t.bse_code
        h.scheme_code = h.scheme_code or t.scheme_code
    if t.meta.get("cas_type") and not (h.meta or {}).get("cas_type"):
        h.meta = {**(h.meta or {}), "cas_type": t.meta["cas_type"]}
    return h


@dataclass
class Applied:
    added: int
    duplicates: int  # the same source imported it before (dedupe_key)
    holdings: set[int]
    cross_source: list[dict[str, Any]] = field(default_factory=list)  # another source has it: skipped
    conflicts: list[dict[str, Any]] = field(default_factory=list)  # partial overlaps: skipped, review
    superseded_baselines: list[dict[str, Any]] = field(default_factory=list)


class DuplicateEntry(ValueError):
    """A manual entry that another source already has (portfolio.dedupe). The message names that source."""


def _split(s: Session, txns: list[ImportedTxn]) -> tuple[list[tuple[ImportedTxn, str]], int, Check]:
    """(rows to add with their dedupe keys, same-source duplicates, the cross-source check of the rest)."""
    keys = assign_dedupe_keys(txns)
    existing = (
        set(s.scalars(select(PortfolioTxn.dedupe_key).where(PortfolioTxn.dedupe_key.in_(keys))))
        if keys
        else set()
    )
    fresh = [(t, k) for t, k in zip(txns, keys, strict=True) if k not in existing]
    chk = check(s, [t for t, _ in fresh])
    ok = {id(t) for t in chk.new}
    return [(t, k) for t, k in fresh if id(t) in ok], len(txns) - len(fresh), chk


def add_txns(s: Session, txns: list[ImportedTxn], import_id: int | None = None) -> Applied:
    """Add new rows: same-source duplicates (dedupe_key) and rows another source already has (portfolio.dedupe) are
    skipped; partial overlaps are skipped and reported as conflicts."""
    rows, dup, chk = _split(s, txns)
    baselines = check_fund_baselines(s, [t for t, _ in rows], chk)
    added, touched = 0, set()
    for t, key in rows:
        h = get_or_create_holding(s, t)
        s.add(PortfolioTxn(holding_id=h.id, import_id=import_id, day=t.day, kind=t.kind, quantity=t.quantity,
                           price=t.price, amount=t.amount, charges=t.charges, stt_paid=t.stt_paid, source=t.source,
                           dedupe_key=key, meta=t.meta))  # fmt: skip
        touched.add(h.id)
        added += 1
    s.flush()
    return Applied(added, dup, touched, chk.cross_source, chk.conflicts, baselines)


def reconcile(closing: list[ClosingBalance], units: dict[tuple[str, str], Decimal], *,
              by_instrument: bool = False) -> list[dict[str, Any]]:  # fmt: skip
    """Statement closing units vs the units the lots hold. `by_instrument` compares across accounts (a depository
    statement's demat account is not the broker account the tradebook created)."""
    if by_instrument:
        agg: dict[str, Decimal] = {}
        for (ik, _), u in units.items():
            agg[ik] = agg.get(ik, Decimal(0)) + u
    out = []
    for c in closing:
        have = agg.get(c.ikey, Decimal(0)) if by_instrument else units.get((c.ikey, c.account), Decimal(0))
        diff = have - c.units
        out.append({"name": c.name, "account": c.account, "ikey": c.ikey, "statement_units": str(c.units),
                    "lot_units": str(have.quantize(Decimal("0.001"))), "diff": str(diff.quantize(Decimal("0.001"))),
                    "ok": abs(diff) <= UNITS_TOLERANCE, "as_of": c.day.isoformat() if c.day else None})  # fmt: skip
    return out


def lot_units(s: Session, keys: Iterable[tuple[str, str]] | None = None) -> dict[tuple[str, str], Decimal]:
    q = (select(PortfolioHolding.ikey, PortfolioHolding.account, func.coalesce(func.sum(PortfolioLot.open_quantity), 0))
         .join(PortfolioLot, PortfolioLot.holding_id == PortfolioHolding.id, isouter=True)
         .group_by(PortfolioHolding.ikey, PortfolioHolding.account))  # fmt: skip
    got = {(ik, acc): Decimal(u) for ik, acc, u in s.execute(q)}
    return got if keys is None else {k: got.get(k, Decimal(0)) for k in keys}


def preview(s: Session, res: ImportResult) -> dict[str, Any]:
    """What an import would do, without writing: new vs duplicate rows per holding and the reconciliation of the
    statement's closing units against existing + new transactions (in memory). A row another source already has is
    "already present from <source>", never "new" (portfolio.dedupe)."""
    rows, dup, chk = _split(s, res.txns)
    new = [t for t, _ in rows]
    baselines = check_fund_baselines(s, new, chk)
    keys = assign_dedupe_keys(res.txns)
    seen = (
        set(s.scalars(select(PortfolioTxn.dedupe_key).where(PortfolioTxn.dedupe_key.in_(keys))))
        if keys
        else set()
    )
    status = {id(t): ("already imported" if k in seen else chk.status.get(id(t), "new"))
              for t, k in zip(res.txns, keys, strict=True)}  # fmt: skip
    groups: dict[tuple[str, str], list[ImportedTxn]] = {}
    for t in new:
        groups.setdefault((t.ikey, t.account), []).append(t)
    units: dict[tuple[str, str], Decimal] = {}
    holdings = []
    all_keys = set(groups) | {(c.ikey, c.account) for c in res.closing if not res.holdings_only}
    for key in sorted(all_keys):
        h = s.scalar(
            select(PortfolioHolding).where(
                PortfolioHolding.ikey == key[0], PortfolioHolding.account == key[1]
            )
        )
        old = (
            events_of(s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == h.id)).all())
            if h
            else []
        )
        rows = groups.get(key, [])
        fresh = [
            Event(None, t.day, t.kind, t.quantity, t.price, t.amount, t.charges, t.stt_paid, t.meta)
            for t in rows
        ]
        book = build_lots([*old, *fresh])
        units[key] = book.units
        name = rows[0].name if rows else (h.name if h else key[0])
        holdings.append({"name": name, "account": key[1], "ikey": key[0], "new_rows": len(rows),
                         "existing": h is not None, "units_after": str(book.units.quantize(Decimal("0.001"))),
                         "warnings": book.warnings[:5]})  # fmt: skip
    if res.holdings_only:
        units = lot_units(s)
    rec = reconcile(res.closing, units, by_instrument=res.holdings_only)
    return {"kind": res.kind, "source": res.source, "period": list(res.period) if res.period else None,
            "rows": len(res.txns), "new_rows": len(new), "duplicates": dup,
            "cross_source": chk.cross_source[:100], "conflicts": chk.conflicts[:100],
            "superseded_baselines": baselines[:50],
            "rows_preview": [{**_txn_preview(t), "status": status[id(t)]} for t in res.txns[:200]],
            "holdings": holdings, "reconciliation": rec, "reconciled": all(r["ok"] for r in rec),
            "warnings": res.warnings, "skipped": dict(res.skipped), "holdings_only": res.holdings_only,
            "sample": [_txn_preview(t) for t in new[:50]]}  # fmt: skip


def _txn_preview(t: ImportedTxn) -> dict[str, Any]:
    return {"day": t.day.isoformat(), "kind": t.kind, "name": t.name, "account": t.account,
            "quantity": _s(t.quantity), "price": _s(t.price), "amount": _s(t.amount)}  # fmt: skip


def apply(
    s: Session, res: ImportResult, *, filename: str, sha256: str, saved_path: str | None
) -> dict[str, Any]:
    """Write an import: the import row, new transactions (duplicates skipped), rebuilt lots and the reconciliation."""
    if res.holdings_only:
        raise ValueError(
            "a depository (NSDL/CDSL) statement has no transactions to import; use it to reconcile"
        )
    imp = PortfolioImport(kind=res.kind, source=res.source, filename=filename[:300], sha256=sha256,
                          saved_path=saved_path, summary={})  # fmt: skip
    s.add(imp)
    s.flush()
    applied = add_txns(s, res.txns, imp.id)
    for hid in applied.holdings:
        rebuild(s, hid)
    touched = [h for hid in applied.holdings if (h := s.get(PortfolioHolding, hid)) is not None]
    for hid in fund_baseline_holdings(s, touched) - applied.holdings:
        rebuild(s, hid)  # a broker fund baseline this CAS now covers (or no longer covers)
    for c in res.closing:  # remember the statement's own valuation (a fallback price) per holding
        h = s.scalar(
            select(PortfolioHolding).where(
                PortfolioHolding.ikey == c.ikey, PortfolioHolding.account == c.account
            )
        )
        if h is not None and c.nav is not None and c.day is not None:
            prev = (h.meta or {}).get("statement_nav") or {}
            if not prev or prev.get("day", "") <= c.day.isoformat():
                h.meta = {**(h.meta or {}), "statement_nav": {"nav": str(c.nav), "day": c.day.isoformat(),
                                                              "source": res.source}}  # fmt: skip
    s.flush()
    rec = reconcile(res.closing, lot_units(s, [(c.ikey, c.account) for c in res.closing]))
    imp.summary = {"period": list(res.period) if res.period else None, "rows": len(res.txns), "added": applied.added,
                   "duplicates": applied.duplicates, "cross_source": applied.cross_source[:100],
                   "conflicts": applied.conflicts[:100], "superseded_baselines": applied.superseded_baselines[:50],
                   "holdings": len(applied.holdings), "reconciliation": rec,
                   "reconciled": all(r["ok"] for r in rec), "warnings": res.warnings[:50],
                   "skipped": dict(res.skipped)}  # fmt: skip
    return {"import_id": imp.id, **imp.summary}


def delete_import(s: Session, import_id: int) -> int:
    imp = s.get(PortfolioImport, import_id)
    if imp is None:
        raise LookupError(f"unknown import {import_id}")
    hids = set(s.scalars(select(PortfolioTxn.holding_id).where(PortfolioTxn.import_id == import_id)))
    funds = (
        fund_baseline_holdings(s, [h for hid in hids if (h := s.get(PortfolioHolding, hid)) is not None])
        - hids
    )
    s.execute(delete(PortfolioTxn).where(PortfolioTxn.import_id == import_id))
    s.delete(imp)
    s.flush()
    for hid in hids:
        _rebuild_or_drop(s, hid)
    for hid in funds:  # a broker fund baseline the deleted CAS had superseded counts again
        rebuild(s, hid)
    return len(hids)


def _rebuild_or_drop(s: Session, holding_id: int) -> None:
    """Rebuild a holding's lots, or delete the holding when no transactions are left."""
    n = s.scalar(select(func.count()).select_from(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id))
    if not n:
        h = s.get(PortfolioHolding, holding_id)
        if h is not None:
            s.delete(h)
        return
    rebuild(s, holding_id)


# --------------------------------------------------------------------------- manual entries
def manual_txn(s: Session, body: dict[str, Any]) -> PortfolioTxn:
    """Add one transaction from the manual form. `holding_id` targets an existing holding; otherwise the instrument
    fields (asset_type, name, and a symbol/ISIN/scheme code) and `account` create or find one. A buy or sell that
    another source already has (portfolio.dedupe) raises DuplicateEntry unless `allow_duplicate` (a second, real
    trade the user confirms)."""
    hid = body.get("holding_id")
    if hid:
        h = s.get(PortfolioHolding, int(hid))
        if h is None:
            raise LookupError(f"unknown holding {hid}")
        t = ImportedTxn(account=h.account, asset_type=h.asset_type, name=h.name, day=body["day"], kind=body["kind"],
                        isin=h.isin, nse_symbol=h.nse_symbol, bse_code=h.bse_code, scheme_code=h.scheme_code,
                        source="manual")  # fmt: skip
    else:
        t = ImportedTxn(account=(body.get("account") or "Manual")[:80], asset_type=body["asset_type"],
                        name=body["name"], day=body["day"], kind=body["kind"], isin=body.get("isin") or None,
                        nse_symbol=(body.get("nse_symbol") or "").upper() or None, bse_code=body.get("bse_code") or None,
                        scheme_code=body.get("scheme_code") or None, source="manual")  # fmt: skip
        h = None
    if not body.get("allow_duplicate"):
        t.quantity, t.price, t.amount = body.get("quantity"), body.get("price"), body.get("amount")
        chk = check(s, [t])
        if not chk.new:
            hit = (chk.cross_source or chk.conflicts)[0]
            what = hit.get("label") or hit["why"]
            raise DuplicateEntry(f"{t.kind} of {_s(t.quantity)} on {t.day.isoformat()}: {what} (in "
                                 f"{', '.join(hit['accounts'])}). Add it anyway only if it is a second, real trade")  # fmt: skip
    if h is None:
        h = get_or_create_holding(s, t)
    meta = dict(body.get("meta") or {})
    row = PortfolioTxn(holding_id=h.id, day=body["day"], kind=body["kind"], quantity=body.get("quantity"),
                       price=body.get("price"), amount=body.get("amount"), charges=body.get("charges") or Decimal(0),
                       stt_paid=body.get("stt_paid", True), source="manual",
                       dedupe_key=_manual_key(), meta=meta, note=body.get("note"))  # fmt: skip
    s.add(row)
    s.flush()
    rebuild(s, h.id)
    return row


def _manual_key() -> str:
    import secrets

    return f"manual:{secrets.token_hex(24)}"


EDITABLE_TXN = ("day", "kind", "quantity", "price", "amount", "charges", "stt_paid", "note", "meta")


def update_txn(s: Session, txn_id: int, body: dict[str, Any]) -> PortfolioTxn:
    t = s.get(PortfolioTxn, txn_id)
    if t is None:
        raise LookupError(f"unknown transaction {txn_id}")
    for k in EDITABLE_TXN:
        if k in body:
            setattr(t, k, ({**(t.meta or {}), **(body[k] or {})} if k == "meta" else body[k]))
    s.flush()
    rebuild(s, t.holding_id)
    return t


def delete_txn(s: Session, txn_id: int) -> None:
    t = s.get(PortfolioTxn, txn_id)
    if t is None:
        raise LookupError(f"unknown transaction {txn_id}")
    hid = t.holding_id
    s.delete(t)
    s.flush()
    _rebuild_or_drop(s, hid)


EDITABLE_HOLDING = (
    "name",
    "nse_symbol",
    "bse_code",
    "scheme_code",
    "isin",
    "sector",
    "tax_class",
    "fmv_2018",
)


def update_holding(s: Session, holding_id: int, body: dict[str, Any]) -> PortfolioHolding:
    h = s.get(PortfolioHolding, holding_id)
    if h is None:
        raise LookupError(f"unknown holding {holding_id}")
    for k in EDITABLE_HOLDING:
        if k in body:
            setattr(h, k, body[k])
    if "flags" in body:  # SGB original-subscriber flag, listed override ...
        h.meta = {**(h.meta or {}), **{k: v for k, v in (body["flags"] or {}).items()
                                       if k in ("sgb_original_subscriber", "listed")}}  # fmt: skip
    h.updated_at = func.now()
    s.flush()
    return h


# --------------------------------------------------------------------------- corporate actions
_BONUS = re.compile(r"\bbonus\b[^0-9]*(\d+)\s*:\s*(\d+)", re.I)
_SPLIT = re.compile(r"(?:split|sub[- ]?division|splt).*?(?:from|frm)\s*(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)"
                    r".*?to\s*(?:rs|re)?\.?\s*(\d+(?:\.\d+)?)", re.I | re.S)  # fmt: skip


def parse_action(subject: str) -> list[tuple[str, dict[str, str]]]:
    """An exchange corporate-action subject -> [("bonus", {"a", "b"}), ("split", {"from", "to"})] (either, both or
    none). Same patterns as fincalc.signals.action_factor, but bonus and split stay separate: their tax treatment
    differs (a bonus is a new nil-cost lot; a split re-denominates the existing lots)."""
    out: list[tuple[str, dict[str, str]]] = []
    if m := _BONUS.search(subject):
        a, b = int(m.group(1)), int(m.group(2))
        if a > 0 and b > 0:
            out.append(("bonus", {"a": str(a), "b": str(b)}))
    if m := _SPLIT.search(subject):
        old, new = Decimal(m.group(1)), Decimal(m.group(2))
        if new > 0 and old > new:
            out.append(("split", {"from": str(old), "to": str(new)}))
    return out


def apply_actions(
    s: Session, holding: PortfolioHolding, actions: Iterable[tuple[date | None, str]]
) -> list[str]:
    """Add split/bonus events from exchange corporate actions (ex-date, subject) to a stock holding: only actions
    after its first acquisition, each once (NSE repeats some rows). Returns what was added."""
    first = s.scalar(select(func.min(PortfolioTxn.day)).where(PortfolioTxn.holding_id == holding.id,
                                                              PortfolioTxn.kind.in_(("buy", "opening"))))  # fmt: skip
    if first is None:
        return []
    added: list[str] = []
    seen: set[tuple[date, str]] = set()
    for ex, subject in actions:
        if ex is None or ex <= first:
            continue
        for kind, meta in parse_action(subject):
            key = (ex, kind)
            if key in seen:
                continue
            seen.add(key)
            dk = f"action:{holding.id}:{ex.isoformat()}:{kind}"
            if s.scalar(select(PortfolioTxn.id).where(PortfolioTxn.dedupe_key == dk)):
                continue
            s.add(PortfolioTxn(holding_id=holding.id, day=ex, kind=kind, source="nse_actions", dedupe_key=dk,
                               meta={**meta, "subject": " ".join(subject.split())[:200]}))  # fmt: skip
            added.append(f"{kind} {ex.isoformat()}")
    if added:
        s.flush()
        rebuild(s, holding.id)
    return added


def save_upload(content: bytes, sha: str, ext: str) -> str:
    """Keep an imported file under the gitignored portfolio dir (data/portfolio/imports/, owner-only permissions). A
    CAS PDF is kept exactly as uploaded, so it stays password-protected."""
    import os

    from finresearch.config import get_settings

    root = get_settings().portfolio_dir / "imports" / sha[:2]
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{sha}{ext}"
    if not path.exists():
        path.write_bytes(content)
        os.chmod(path, 0o600)
    return str(path)


def _s(v: Decimal | None) -> str | None:
    return None if v is None else format(v.normalize(), "f")
