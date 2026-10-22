"""Checks on the full equity bhavcopy (#373 (a), refseries.equity_eod), with the reconciliation that has to pass
before the stock peer build may switch to it (Settings.peer_price_source):

- rows_present: at least MIN_EQ_ROWS EQ-series rows (the 21-Oct-2026 file had 2,694; a truncated file or a block
  page that parsed would have far fewer).
- row_ranges: no close <= 0, high < low, or close outside [low, high].
- reupload (non-critical): the lineage table saw the day's file change after its first load.
- close_vs_quote (non-critical): every official close the peer build read from NSE's quote API for the day
  (signals.stock_peers rows' `quote_close`: all rows on the quote path, the re-quoted ones on the bhavcopy path)
  against the bhavcopy's close of the same stock and day, exact to the paisa (TOLERANCE). Unknown when nothing is
  comparable. Not critical: a mismatch says the two sources disagree, not which one is wrong.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finresearch.db.models import EquityEod
from finresearch.dq.base import Check

DATASET = "equity_eod"
MIN_EQ_ROWS = 1000
TOLERANCE = Decimal("0.005")  # both sources print rupees to two decimals: any real difference is >= 0.01


def equity_checks(s: Session, day: date) -> list[Check]:
    from finresearch.dq.ref import _reupload
    from finresearch.refseries.equity_eod import is_loaded

    if not is_loaded(s, day):
        return [Check(DATASET, n, "unknown", note=f"{day} is not loaded", critical=n in ("rows_present", "row_ranges"))
                for n in ("rows_present", "row_ranges", "reupload", "close_vs_quote")]  # fmt: skip
    n = s.scalar(
        select(func.count()).select_from(EquityEod).where(EquityEod.day == day, EquityEod.series == "EQ")
    )
    out = [Check(DATASET, "rows_present", "ok" if n >= MIN_EQ_ROWS else "fail", n, MIN_EQ_ROWS,
                 None if n >= MIN_EQ_ROWS else f"only {n} EQ rows")]  # fmt: skip
    bad = s.scalar(text("""
        SELECT count(*) FROM equity_eod WHERE day = :d AND (close <= 0 OR high < low OR close > high OR close < low)"""),
                   {"d": day})  # fmt: skip
    out.append(
        Check(
            DATASET,
            "row_ranges",
            "fail" if bad else "ok",
            bad,
            0,
            f"{bad} row(s) out of range" if bad else None,
        )
    )
    out.append(_reupload(s, DATASET, day))
    out.append(close_vs_quote(s, day))
    return out


def close_vs_quote(s: Session, day: date) -> Check:
    """The peer store's quote closes for `day` against the bhavcopy (module docstring)."""
    from finresearch.fincalc.price import OFFICIAL_CLOSE
    from finresearch.refseries.equity_eod import close_on
    from finresearch.signals import stock_peers

    data = stock_peers.load() or {}
    pairs, bad = 0, []
    for sym, row in sorted((data.get("rows") or {}).items()):
        qc = row.get("quote_close") or {}
        if qc.get("kind") != OFFICIAL_CLOSE or qc.get("day") != day.isoformat() or qc.get("price") is None:
            continue
        got = close_on(s, sym, day)
        if got is None:
            continue
        pairs += 1
        diff = Decimal(str(qc["price"])) - got[0]
        if abs(diff) > TOLERANCE:
            bad.append(f"{sym} quote {qc['price']} vs bhavcopy {got[0]}")
    if not pairs:
        return Check(
            DATASET, "close_vs_quote", "unknown", note=f"no quote close of {day} to compare", critical=False
        )
    note = "; ".join(bad[:5]) if bad else f"{pairs} stock(s) match exactly"
    return Check(DATASET, "close_vs_quote", "fail" if bad else "ok", len(bad), 0, note, critical=False)
