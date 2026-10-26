"""Read-only broker reconciliation (#368, P1.6, the issue's reconciliation (e)): what Groww reported at its last sync
against the app's own book, from rows already stored. Nothing here calls Groww or imports a connector, an adapter or
fnobook: it reads broker_connection.state, broker_sync_log, fno_mark, alert_eval_slot and the portfolio's
transactions. Personal data: computed locally, never sent to a model and never logged; each dq_check note carries
counts only, and the per-instrument detail is `finresearch dq broker`, on this machine.

* fno_positions_vs_book: Groww's F&O positions as last read (state["fno_positions"]: the rows and when) against the
  app's real F&O book, the fno_mark rows (book 'real') the risk views read, for the IST session R of that read. The
  monitor marks session R after its bhavcopy from the latest read dated R (monitor.fno_marks), so legs match by
  trading symbol (leg_key "groww:<symbol>", cut at 80 like the marks) with the signed quantity exact (Groww rows of
  one symbol summed: one mark per symbol). Mismatches: a position not in the book, a book leg no longer at Groww, a
  quantity that differs. Unknown, never ok: Groww not connected; positions never read; a later session marked since R
  (the book moved on without a read); no completed marks for R (before that session's bhavcopy, or no session that
  day); a leg whose long/short is unknown on either side.
* holdings_vs_book: the holdings Groww reported at the last sync that read them (that sync's reconciliation rows:
  Groww's units per instrument) against the app's transactions replayed to the sync's snapshot day (Groww's holdings
  leave out the sync day's trades, so the day before, as merge.merge_sync compares them), plus holdings the app keeps
  in the Groww account that Groww did not report. A holding is found as the sync found it: its key, else its ISIN,
  NSE symbol, BSE code or scheme code, else the NSE symbol Groww names its rows by (connectors.groww.map_holding), in
  the Groww account. Unknown: no sync has read the holdings, the last one is older than STALE_AFTER, a row the sync
  could not decide itself (T1 shares, #282), or a list cut at RECON_CAP rows with no mismatch found in it.

Each check is critical within this dataset (an unknown leaves broker_groww unknown) but never fails: a mismatch is a
warning (the owner's choice for this slice), and no page reads broker_groww (gate.DEPENDENTS), so nothing is marked
'data unverified' by it yet.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import (
    AlertEvalSlot,
    BrokerConnection,
    BrokerSyncLog,
    FnoMark,
    PortfolioHolding,
    PortfolioTxn,
)
from finresearch.dq.base import Check
from finresearch.fincalc.dates import to_ist
from finresearch.portfolio.lots import build_lots
from finresearch.portfolio.service import events_of

log = logging.getLogger(__name__)

DATASET = "broker_groww"
CHECKS = ("fno_positions_vs_book", "holdings_vs_book")
KEY, ACCOUNT = "groww", "Groww"  # connectors.groww.GrowwConnector.key and .account
GROWW_HOLDINGS_INCLUDE_TODAY = False  # GrowwConnector.holdings_include_today
RECON_CAP = 300  # merge.MergeResult.as_dict keeps the first 300 reconciliation rows
UNITS_TOL = Decimal("0.001")  # merge.UNITS_TOL
STALE_AFTER = timedelta(days=4)  # a Friday sync still covers the Tuesday morning after a Monday holiday
LABELS = {"not_in_book": "not in the book", "not_at_broker": "no longer at Groww", "qty_differs": "quantity differs",
          "units_differ": "units differ"}  # fmt: skip


@dataclass
class Result:
    check: str
    asof: date | None = None
    compared: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)  # personal: kind, instrument, groww, book
    unverifiable: int = 0
    why: str | None = None  # why it is unknown (never personal)


def _num(v: Any) -> Decimal | None:
    try:
        return None if v is None else Decimal(str(v))
    except InvalidOperation:
        return None


def fno_positions(s: Session, now: datetime) -> Result:
    res = Result(CHECKS[0])
    conn = s.get(BrokerConnection, KEY)
    if conn is None or not conn.enabled:
        res.why = "Groww is not connected"
        return res
    got = (conn.state or {}).get("fno_positions") or {}
    if not got.get("at"):
        res.why = "Groww's F&O positions were never read"
        return res
    res.asof = r = to_ist(datetime.fromisoformat(got["at"])).date()
    done = [date.fromisoformat(x.removeprefix("fno_marks:")) for x in s.scalars(select(AlertEvalSlot.slot).where(
        AlertEvalSlot.slot.like("fno_marks:%"), AlertEvalSlot.result["status"].astext == "done"))]  # fmt: skip
    if done and max(done) > r:
        res.why = f"Groww's F&O positions were last read on {r}; the app's book has moved on to {max(done)}"
        return res
    if r not in done:
        res.why = f"no F&O marks for {r} yet (written after that session's bhavcopy; none on a day without a session)"
        return res
    groww: dict[str, Decimal | None] = {}
    for row in got.get("rows") or []:
        k = f"groww:{row.get('symbol') or ''}"[:80]
        q = _num(row.get("quantity")) if row.get("direction_known") else None
        groww[k] = (
            None if q is None or (k in groww and groww[k] is None) else (groww.get(k) or Decimal(0)) + q
        )
    book = dict(
        s.execute(select(FnoMark.leg_key, FnoMark.qty).where(FnoMark.day == r, FnoMark.book == "real")).all()
    )
    for k in sorted(set(groww) | set(book)):
        res.compared += 1
        g, b = groww.get(k), book.get(k)
        if k not in book:
            kind = "not_in_book"
        elif k not in groww:
            kind = "not_at_broker"
        elif g is None or b is None:
            res.unverifiable += 1
            continue
        elif g != b:
            kind = "qty_differs"
        else:
            continue
        res.mismatches.append({"kind": kind, "instrument": k.removeprefix("groww:"), "groww": g, "book": b})
    if res.unverifiable and not res.mismatches:
        res.why = (
            f"{res.unverifiable} position(s) with long or short unknown (Groww did not confirm the direction)"
        )
    return res


_IDS = {"ISIN": PortfolioHolding.isin, "NSE": PortfolioHolding.nse_symbol, "BSE": PortfolioHolding.bse_code,
        "MF": PortfolioHolding.scheme_code}  # fmt: skip


def _find(s: Session, ikey: str, name: str) -> PortfolioHolding | None:
    """The Groww-account holding the sync matched (portfolio.service.find_holding's order: key, then identifiers)."""
    kind, _, val = ikey.partition(":")
    for cond in (PortfolioHolding.ikey == ikey, _IDS[kind] == val if kind in _IDS and val else None,
                 PortfolioHolding.nse_symbol == name if name else None):  # fmt: skip
        if cond is not None and (h := s.scalars(select(PortfolioHolding).where(PortfolioHolding.account == ACCOUNT, cond)
                                                .order_by(PortfolioHolding.id)).first()):  # fmt: skip
            return h
    return None


def _units(s: Session, holding_id: int, as_of: date) -> Decimal:
    """Open units at the end of `as_of`: the holding's transactions up to that day replayed into FIFO lots."""
    txns = s.scalars(
        select(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id, PortfolioTxn.day <= as_of)
    ).all()
    return build_lots(events_of(txns)).units if txns else Decimal(0)


def holdings(s: Session, now: datetime) -> Result:
    res = Result(CHECKS[1])
    last = s.scalars(select(BrokerSyncLog).where(BrokerSyncLog.key == KEY, BrokerSyncLog.summary["reconciled"]
                                                 .astext.is_not(None))
                     .order_by(BrokerSyncLog.started_at.desc(), BrokerSyncLog.id.desc()).limit(1)).first()  # fmt: skip
    if last is None:
        res.why = "no Groww sync has read the holdings yet"
        return res
    synced = to_ist(last.started_at).date()
    if now - last.started_at > STALE_AFTER:
        res.why = f"Groww's holdings were last read on {synced}"
        return res
    res.asof = as_of = synced if GROWW_HOLDINGS_INCLUDE_TODAY else synced - timedelta(days=1)
    rows = list((last.summary or {}).get("reconciliation") or [])
    seen: set[int] = set()
    for r in (r for r in rows if r.get("account") == ACCOUNT):
        h = _find(s, str(r.get("ikey") or ""), str(r.get("name") or ""))
        seen |= {h.id} if h is not None else set()  # reported by Groww, even when its count is undecided
        if r.get("status") == "unknown":  # the sync could not tell whether Groww's count includes T1 shares
            res.unverifiable += 1
            continue
        book = _units(s, h.id, as_of) if h is not None else Decimal(0)
        _compare(res, str(r.get("name") or r.get("ikey")), _num(r.get("broker_units")) or Decimal(0), book)
    cut = len(rows) >= RECON_CAP
    if not cut:  # what the app holds in the Groww account that Groww did not report
        for h in s.scalars(select(PortfolioHolding).where(PortfolioHolding.account == ACCOUNT)):
            if h.id not in seen and (book := _units(s, h.id, as_of)) > UNITS_TOL:
                _compare(res, h.nse_symbol or h.name, Decimal(0), book)
    if not res.mismatches and (res.unverifiable or cut):
        res.why = (f"{res.unverifiable} holding(s) where Groww's own count was undecided (T1 shares, #282)"
                   if res.unverifiable else f"the sync kept only the first {RECON_CAP} rows: the rest are not "
                   "reconciled")  # fmt: skip
    return res


def _compare(res: Result, instrument: str, groww: Decimal, book: Decimal) -> None:
    res.compared += 1
    if abs(book - groww) <= UNITS_TOL:
        return
    kind = "not_in_book" if book <= UNITS_TOL else "not_at_broker" if groww <= UNITS_TOL else "units_differ"
    res.mismatches.append({"kind": kind, "instrument": instrument, "groww": groww, "book": book})


def to_check(res: Result) -> Check:
    """Counts only: an instrument, a quantity or a name never enters dq_check (personal data)."""
    what = "F&O positions read" if res.check == CHECKS[0] else "holdings of"
    if res.mismatches:
        n = Counter(m["kind"] for m in res.mismatches)
        kinds = ", ".join(f"{n[k]} {LABELS[k].replace('no longer at', 'not at') if res.check == CHECKS[1] else LABELS[k]}"
                          for k in LABELS if n[k])  # fmt: skip
        note = (f"{len(res.mismatches)} mismatch(es) between Groww's {what} {res.asof} and the app's book: {kinds} "
                "(details: finresearch dq broker)")  # fmt: skip
        return Check(DATASET, res.check, "warn", len(res.mismatches), 0, note)
    if res.why:
        return Check(DATASET, res.check, "unknown", None, 0, f"not verified: {res.why}")
    if res.check == CHECKS[0]:
        note = (f"{res.compared} Groww F&O position(s) read {res.asof} match the app's book for that session"
                if res.compared else f"no Groww F&O position read {res.asof} and none in the app's book")  # fmt: skip
    else:
        note = f"{res.compared} Groww holding(s) of {res.asof} match the app's book"
    return Check(DATASET, res.check, "ok", 0, 0, note)


def reconcile(s: Session, now: datetime) -> list[Result]:
    return [fno_positions(s, now), holdings(s, now)]


def broker_checks(s: Session, now: datetime) -> list[Check]:
    """Each check in its own savepoint; a crash is unknown with the exception's type only (its message or traceback
    could name an instrument), and is logged the same way."""
    out = []
    for name, fn in zip(CHECKS, (fno_positions, holdings), strict=True):
        try:
            with s.begin_nested():
                out.append(to_check(fn(s, now)))
        except Exception as e:
            log.warning("broker reconciliation %s could not run: %s", name, type(e).__name__)
            out.append(
                Check(
                    DATASET, name, "unknown", note=f"not verified: could not be checked ({type(e).__name__})"
                )
            )
    return out
