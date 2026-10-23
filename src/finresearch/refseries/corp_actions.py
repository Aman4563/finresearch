"""The per-stock corporate-action factor history (#369, roadmap P1.7, slice 2): NSE's per-symbol corporate actions
(and BSE's, through the same terms parser) stored with their price and share factors (`corp_action`), so a back-
adjusted price is P_t / CF_t with CF_t computed on read (fincalc.corporate_actions.cumulative_factors).

Factors come from fincalc.corporate_actions.action_terms: bonus, split, consolidation and rights (TERP, priced from the
stored equity_eod close on the last cum day; `rebuild` re-prices them once that close is loaded) are "known"; a
demerger, a merger, a bare "Consolidation Of Shares" or anything unrecognised is "unknown" with a reason and no
factor (never a silent 1); dividends and meetings are "none" (factor 1 by definition). Delisting rows carry the exit
price in `terms` when the subject states one (fincalc.corporate_actions.delisting_return), never a factor.

Symbol changes come from NSE's symbol-change file (`load_symbol_changes`; source nse_symchg, symbol = the old symbol)
and join the factor histories of the old and new symbol (`fno_factors`, `factor_history`): legacy F&O days carry the
symbol of their time. A symbol later re-used by another company would wrongly join; the F&O use only applies a factor
the price move verifies (fincalc.fno_history.front_future_returns).

Collection (`backfill`): one request per F&O stock (the underlyings in fno_contract; the decision #362 says never widen
beyond what a feature needs), with data_rights.require("nse_corp_actions") first, the shared NSE budget, no downloads
between 09:00 and 16:00 IST on weekdays, a stop after REFUSALS_TO_STOP refusals in a row, and a manifest
(corp_action_fetch) so a re-run resumes. The raw JSON is kept under data/corp_actions/nse/ and each load gets a lineage
row (dq.lineage, dataset "nse_ca:<SYMBOL>"). NSE's endpoint without dates answered INFY's actions back to 2018 only
(tests/fixtures/nse/equity/actions_INFY.json), so the backfill passes from_date/to_date [U: the date parameters are
those of NSE's corporate-actions page and were not verified here]; `earliest_ex` in the manifest records the depth each
symbol actually got.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from finresearch.db.models import CorpAction, CorpActionFetch
from finresearch.fincalc.corporate_actions import ActionTerms, action_terms

DATASET = "nse_corp_actions"  # the data_rights name of the NSE per-symbol corporate actions
SYMBOL_CHANGE_DATASET = "nse_symbol_change"
PARSER_VERSION = "1"
SOURCES = ("nse", "bse")  # exchange actions; nse_symchg rows are symbol changes
CUM_LOOKBACK_DAYS = 10  # calendar days searched back from the ex-date for the last cum close


def subject_key(subject: str) -> str:
    return " ".join((subject or "").lower().split())[:300]


def _cum_close(s: Session, symbol: str, ex: date) -> Decimal | None:
    from finresearch.refseries.equity_eod import closes_between

    got = closes_between(s, symbol, ex - timedelta(days=CUM_LOOKBACK_DAYS), ex - timedelta(days=1))
    return got[-1][1] if got else None


def _terms(s: Session, symbol: str, subject: str, ex: date, face_value: Decimal | None,
           dividend: Decimal | None) -> ActionTerms:  # fmt: skip
    p_cum = _cum_close(s, symbol, ex) if "right" in subject.lower() else None
    return action_terms(subject, face_value=face_value, p_cum=p_cum, dividend=dividend)


def _values(t: ActionTerms) -> dict[str, Any]:
    return {"kind": t.kind[:60], "status": t.status, "price_factor": t.price_factor, "share_factor": t.share_factor,
            "dividend": t.dividend, "reason": None if t.reason is None else t.reason[:300], "terms": t.terms}  # fmt: skip


def _upsert(s: Session, values: dict[str, Any]) -> None:
    stmt = pg_insert(CorpAction).values(**values)
    keep = {k: v for k, v in values.items() if k not in ("source", "symbol", "ex_date", "subject_key")}
    s.execute(stmt.on_conflict_do_update(index_elements=["source", "symbol", "ex_date", "subject_key"],
                                         set_={**keep, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def _manifest(s: Session, source: str, symbol: str, **values: Any) -> None:
    stmt = pg_insert(CorpActionFetch).values(source=source, symbol=symbol, attempts=1, **values)
    s.execute(stmt.on_conflict_do_update(index_elements=["source", "symbol"], set_={
        **values, "attempts": CorpActionFetch.attempts + 1, "fetched_at": stmt.excluded.fetched_at}))  # fmt: skip


def mark_failed(s: Session, symbol: str, error: str, source: str = "nse") -> None:
    _manifest(s, source, symbol, status="failed", error=error[:300])


def load_nse(s: Session, symbol: str, rows: list[dict[str, Any]], *, url: str | None, body: bytes,
             source: str = "nse") -> int:  # fmt: skip
    """Upsert one symbol's NSE corporate-action rows (the caller's transaction) with their factors, mark the manifest
    and record lineage. A row without an ex-date is skipped (nothing to adjust). Returns the distinct actions stored."""
    from finresearch.adapters.nse_equity import CorporateAction, parse_nse_date
    from finresearch.dq.lineage import record_load
    from finresearch.fincalc.dates import today_ist

    seen: dict[tuple[date, str], dict[str, Any]] = {}
    for r in rows or []:
        a = CorporateAction.parse(r)
        if a.ex_date is None or not a.subject.strip():
            continue
        key = (a.ex_date, subject_key(a.subject))
        if key in seen:
            continue
        fv = _decimal(r.get("faceVal"))
        t = _terms(s, symbol, a.subject, a.ex_date, fv, a.dividend_per_share)
        seen[key] = {"source": source, "symbol": symbol, "ex_date": a.ex_date, "subject_key": key[1],
                     "record_date": parse_nse_date(r.get("recDate")), "subject": " ".join(a.subject.split()),
                     "isin": (r.get("isin") or None) and str(r["isin"])[:12], "face_value": fv, "source_url": url,
                     **_values(t)}  # fmt: skip
    for v in seen.values():
        _upsert(s, v)
    earliest = min((d for d, _ in seen), default=None)
    _manifest(s, source, symbol, status="loaded", url=url, rows=len(seen), earliest_ex=earliest, error=None)
    record_load(
        s, f"nse_ca:{symbol}"[:30], today_ist(), body, url=url, parser_version=PARSER_VERSION, rows=len(seen)
    )
    return len(seen)


def _decimal(v: Any) -> Decimal | None:
    try:
        d = Decimal(str(v).strip())
    except (ArithmeticError, ValueError):
        return None
    return d if d.is_finite() and d > 0 else None


def rebuild(s: Session) -> int:
    """Re-derive every stored exchange action's factor (after a parser change, or once the equity_eod close a rights
    issue needs is loaded). Returns the rows whose status or factor changed."""
    n = 0
    for r in s.scalars(select(CorpAction).where(CorpAction.source.in_(SOURCES))):
        t = _terms(s, r.symbol, r.subject, r.ex_date, r.face_value, r.dividend)
        if (t.status, t.price_factor) != (r.status, r.price_factor):
            n += 1
        for k, v in _values(t).items():
            setattr(r, k, v)
    return n


def load_symbol_changes(s: Session, text: str, *, url: str | None) -> int:
    """Store NSE's symbol-change file (old symbol, new symbol, date of change) as source nse_symchg rows (price factor
    1: a rename changes no price). The columns are found by name [U: header as recalled, SM_KEY_SYMBOL /
    SM_NEW_SYMBOL / SM_APPLICABLE_FROM; not verified against a downloaded file]; a file without them raises NseError
    rather than loading nothing."""
    from finresearch.adapters.nse import NseError
    from finresearch.adapters.nse_equity import parse_nse_date
    from finresearch.dq.lineage import record_load
    from finresearch.fincalc.dates import today_ist

    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    head = [h.strip().upper() for h in rows[0]] if rows else []

    def col(*names: str) -> int | None:
        return next((i for i, h in enumerate(head) if any(n in h for n in names)), None)

    old, new, when = col("KEY_SYMBOL", "OLD"), col("NEW_SYMBOL", "NEW"), col("APPLICABLE", "DATE")
    if old is None or new is None or when is None:
        raise NseError(f"symbol-change file without old/new/date columns (header {head[:6]})")
    n = 0
    for r in rows[1:]:
        if len(r) <= max(old, new, when):
            continue
        o, nw, d = r[old].strip().upper(), r[new].strip().upper(), parse_nse_date(r[when].strip().title())
        if not o or not nw or d is None or o == nw:
            continue
        subj = f"Symbol change {o} -> {nw}"
        _upsert(s, {"source": "nse_symchg", "symbol": o, "ex_date": d, "subject_key": subject_key(subj),
                    "subject": subj, "record_date": None, "isin": None, "face_value": None, "source_url": url,
                    "kind": "symbol_change", "status": "none", "price_factor": Decimal(1), "share_factor": Decimal(1),
                    "dividend": None, "reason": None, "terms": {"old_symbol": o, "new_symbol": nw}})  # fmt: skip
        n += 1
    record_load(
        s, "nse_symbol_change", today_ist(), text.encode(), url=url, parser_version=PARSER_VERSION, rows=n
    )
    return n


def aliases(s: Session, symbol: str) -> set[str]:
    """`symbol` and every symbol joined to it by a recorded symbol change (in either direction, transitively)."""
    edges: dict[str, set[str]] = {}
    for t in s.scalars(select(CorpAction.terms).where(CorpAction.source == "nse_symchg")):
        o, n = t.get("old_symbol"), t.get("new_symbol")
        if o and n:
            edges.setdefault(o, set()).add(n)
            edges.setdefault(n, set()).add(o)
    group, todo = {symbol}, [symbol]
    while todo:
        for x in edges.get(todo.pop(), ()):
            if x not in group:
                group.add(x)
                todo.append(x)
    return group


def _actions(
    s: Session, symbol: str, lo: date | None = None, hi: date | None = None
) -> dict[date, list[CorpAction]]:
    """Exchange actions of `symbol` and its aliases by ex-date, one row per (ex-date, subject); NSE's rows win over
    BSE's on an ex-date both list (the same action must not compound twice). Delistings are exits, not factors."""
    q = select(CorpAction).where(CorpAction.symbol.in_(aliases(s, symbol)), CorpAction.source.in_(SOURCES),
                                 CorpAction.kind != "delisting")  # fmt: skip
    if lo is not None:
        q = q.where(CorpAction.ex_date >= lo)
    if hi is not None:
        q = q.where(CorpAction.ex_date <= hi)
    by: dict[date, dict[str, list[CorpAction]]] = {}
    for r in s.scalars(q.order_by(CorpAction.ex_date, CorpAction.id)):
        per = by.setdefault(r.ex_date, {}).setdefault(r.source, [])
        if all(x.subject_key != r.subject_key for x in per):
            per.append(r)
    return {d: src.get("nse") or src.get("bse") or [] for d, src in by.items()}


def _day_factor(rows: list[CorpAction]) -> tuple[bool, Decimal | None, list[tuple[str, str]]]:
    """(is a factor day, product of the known factors or None when any action that day is unknown, unknown reasons)."""
    unknown = [(r.kind, r.reason or "unknown") for r in rows if r.status == "unknown"]
    known = [r.price_factor for r in rows if r.status == "known" and r.price_factor is not None]
    f = Decimal(1)
    for x in known:
        f *= x
    if unknown:
        return True, None, unknown
    return f != 1, f, []


def fno_factors(s: Session, symbols: list[str], lo: date, hi: date) -> dict[str, dict[date, float | None]]:
    """{symbol: {ex-date: price factor}} for the F&O return adjustment of days without lot data
    (fincalc.fno_history.front_future_returns `factors`). None = an action went ex but its factor is unknown (the day
    then stays unverified); days with only dividends or meetings are not listed. Symbols without actions are omitted."""
    out: dict[str, dict[date, float | None]] = {}
    for sym in symbols:
        got = {}
        for d, rows in _actions(s, sym, lo, hi).items():
            is_factor, f, _ = _day_factor(rows)
            if is_factor:
                got[d] = None if f is None else float(f)
        if got:
            out[sym] = got
    return out


@dataclass(frozen=True)
class FactorHistory:
    """A stock's known factor events (ex-date, f), ready for fincalc.corporate_actions.cumulative_factors /
    adjust_prices, and the ex-dates whose factor is unknown (ex-date, kind, reason): a back-adjusted series that spans
    one of them is incomplete and must say so."""

    symbol: str
    events: list[tuple[date, Decimal]]
    unknown: list[tuple[date, str, str]]


def factor_history(s: Session, symbol: str) -> FactorHistory:
    """The stored factor history of `symbol` (with its aliases). Offered to the registered stock studies (stock
    backtest, earnings surprise), which still use fincalc.signals.adjust_for_actions: switching them is a registered
    variant, not an in-place change."""
    events: list[tuple[date, Decimal]] = []
    unknown: list[tuple[date, str, str]] = []
    for d, rows in sorted(_actions(s, symbol).items()):
        is_factor, f, why = _day_factor(rows)
        if f is None:
            unknown += [(d, k, r) for k, r in why]
        elif is_factor:
            events.append((d, f))
    return FactorHistory(symbol, events, unknown)
