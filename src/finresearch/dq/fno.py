"""Post-load checks on a stored F&O bhavcopy day (#368): what the parse-time checks cannot see, read from the database
after the load. One SQL pass over the day and the previous loaded day (a few thousand rows each), well inside the
nightly budget. Thresholds are the issue's where it states one; the others are named constants here.

* rows_total_z / rows_kept_z: robust z of the manifest's row counts against the last WINDOW loaded days (|z| > 6).
* futures_continuity: every symbol with a live future at t-1 (expiry after t-1) still has a future at t (a dropped
  symbol). A stock that left F&O keeps its contracts until their expiry, so this does not false-alarm on exits.
* expiry_continuity: every index future and index option expiry live at t-1 is still there at t (a missing expiry).
* expired_contracts: no stored contract expired before t (expiring OI kept).
* stale_settle: futures whose settlement price is identical to t-1's (a duplicated or stale day). Futures settle at a
  new price every session; far OTM options sit at the 0.05 floor for weeks, so they are not counted.
* oi_lot_multiple: open interest is in units and a whole number of lots (adapters.nse_fno_bhav) - a units/lots swap.
* turnover_units: a traded future's turnover / (contracts x lot) is its average price, inside [low, high] (+/-2 %) - a
  lakh/rupee swap. Needs the lot size, so UDiFF days (2024 on) only; older days are unknown.
* price_jump: the nearest future's close moved more than JUMP from t-1 (a bonus or split not yet adjusted, or a bad
  row). Not critical: a real corporate action is data, not an error.
* row_ranges: high < low, negative OI or close, a non-positive settle, underlying or lot.
* reupload / duplicate_body: from the lineage table (dq.lineage).
"""

from __future__ import annotations

import math
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finresearch.db.models import DatasetLoad, FnoBhavDay
from finresearch.dq import stats
from finresearch.dq.base import Check, share_status

DATASET = "fno_bhav"
WINDOW = 60
STALE_SHARE = 0.5
LOT_SHARE = 0.05
TURNOVER_SHARE = 0.05
TURNOVER_BAND = 0.02
JUMP = 0.30
CHECKS = ("rows_total_z", "rows_kept_z", "futures_continuity", "expiry_continuity", "expired_contracts",
          "stale_settle", "oi_lot_multiple", "turnover_units", "price_jump", "row_ranges", "reupload",
          "duplicate_body")  # fmt: skip
NON_CRITICAL = {"price_jump", "reupload"}


def _c(check: str, status: str, value: float | None = None, threshold: float | None = None,
       note: str | None = None) -> Check:  # fmt: skip
    return Check(DATASET, check, status, value, threshold, note, critical=check not in NON_CRITICAL)


def bhav_checks(s: Session, day: date) -> list[Check]:
    m = s.get(FnoBhavDay, day)
    if m is None or m.status != "loaded":
        return [_c(c, "unknown", note=f"{day} is not loaded") for c in CHECKS]
    hist = s.scalars(select(FnoBhavDay).where(FnoBhavDay.status == "loaded", FnoBhavDay.day < day)
                     .order_by(FnoBhavDay.day.desc()).limit(WINDOW)).all()  # fmt: skip
    prev = hist[0].day if hist else None
    out = []
    for col in ("rows_total", "rows_kept"):
        x, h = getattr(m, col), [getattr(r, col) for r in hist if getattr(r, col) is not None]
        z = stats.robust_z(float(x), [float(v) for v in h]) if x is not None else None
        out.append(_c(f"{col}_z", stats.z_status(z), z, stats.Z_LIMIT, f"{col} {x} vs {len(h)} prior days"))
    out += _continuity(s, day, prev) + _contents(s, day, prev) + _lineage(s, day)
    return out


def _continuity(s: Session, day: date, prev: date | None) -> list[Check]:
    if prev is None:
        return [
            _c(c, "unknown", note="no earlier loaded day")
            for c in ("futures_continuity", "expiry_continuity")
        ]
    q = {"d": day, "p": prev}
    gone = s.scalars(text("""
        SELECT DISTINCT symbol FROM fno_bhav WHERE day = :p AND instrument IN ('IDF', 'STF') AND expiry > :p
        EXCEPT SELECT DISTINCT symbol FROM fno_bhav WHERE day = :d AND instrument IN ('IDF', 'STF')
        ORDER BY 1"""), q).all()  # fmt: skip
    lost = s.execute(text("""
        SELECT DISTINCT symbol, instrument, expiry FROM fno_bhav WHERE day = :p AND instrument IN ('IDF', 'IDO')
               AND expiry > :p
        EXCEPT SELECT DISTINCT symbol, instrument, expiry FROM fno_bhav WHERE day = :d
        ORDER BY 1, 2, 3"""), q).all()  # fmt: skip
    return [
        _c("futures_continuity", "fail" if gone else "ok", len(gone), 0,
           f"no future at {day} for: {', '.join(gone[:8])}" if gone else None),
        _c("expiry_continuity", "fail" if lost else "ok", len(lost), 0,
           "missing: " + ", ".join(f"{a} {b} {e}" for a, b, e in lost[:6]) if lost else None),
    ]  # fmt: skip


def _contents(s: Session, day: date, prev: date | None) -> list[Check]:
    r = s.execute(text("""
        SELECT count(*) FILTER (WHERE expiry < :d),
               count(*) FILTER (WHERE high < low OR oi < 0 OR close < 0 OR underlying <= 0 OR lot_size <= 0
                                OR (option_type = 'XX' AND settle <= 0)),
               count(*) FILTER (WHERE lot_size > 0 AND oi > 0),
               count(*) FILTER (WHERE lot_size > 0 AND oi > 0 AND oi % lot_size <> 0),
               count(*) FILTER (WHERE option_type = 'XX' AND lot_size > 0 AND contracts > 0 AND turnover > 0
                                AND low > 0),
               count(*) FILTER (WHERE option_type = 'XX' AND lot_size > 0 AND contracts > 0 AND turnover > 0
                                AND low > 0 AND (turnover / (contracts * lot_size) < low * (1 - :band)
                                                 OR turnover / (contracts * lot_size) > high * (1 + :band)))
        FROM fno_bhav WHERE day = :d"""), {"d": day, "band": TURNOVER_BAND}).one()  # fmt: skip
    expired, bad, n_lot, bad_lot, n_turn, bad_turn = r
    out = [_c("expired_contracts", "fail" if expired else "ok", expired, 0,
              f"{expired} contract(s) expired before {day}" if expired else None),
           _c("row_ranges", "fail" if bad else "ok", bad, 0, f"{bad} row(s) out of range" if bad else None)]  # fmt: skip
    st, v = share_status(bad_lot, n_lot, LOT_SHARE)
    out.append(_c("oi_lot_multiple", st, v, LOT_SHARE, f"{bad_lot}/{n_lot} rows' OI is not whole lots"))
    st, v = share_status(bad_turn, n_turn, TURNOVER_SHARE)
    out.append(_c("turnover_units", st, v, TURNOVER_SHARE, f"{bad_turn}/{n_turn} futures' average price outside "
                  "[low, high]" if n_turn else "no lot sizes (pre-2024 file): not testable"))  # fmt: skip
    if prev is None:
        return out + [_c(c, "unknown", note="no earlier loaded day") for c in ("stale_settle", "price_jump")]
    same, n_fut = s.execute(text("""
        SELECT count(*) FILTER (WHERE a.settle = b.settle), count(*)
        FROM fno_bhav a JOIN fno_bhav b ON b.day = :p AND b.symbol = a.symbol AND b.instrument = a.instrument
             AND b.expiry = a.expiry AND b.strike = a.strike AND b.option_type = a.option_type
        WHERE a.day = :d AND a.option_type = 'XX' AND a.settle IS NOT NULL"""), {"d": day, "p": prev}).one()  # fmt: skip
    st, v = share_status(same, n_fut, STALE_SHARE)
    out.append(_c("stale_settle", st, v, STALE_SHARE, f"{same}/{n_fut} futures settle exactly as on {prev}"))
    jumps = s.execute(text("""
        WITH near AS (SELECT DISTINCT ON (day, symbol) day, symbol, close FROM fno_bhav
                      WHERE day IN (:d, :p) AND option_type = 'XX' AND close > 0 AND expiry > :p
                      ORDER BY day, symbol, expiry)
        SELECT a.symbol, a.close / b.close FROM near a JOIN near b ON b.symbol = a.symbol AND b.day = :p
        WHERE a.day = :d ORDER BY 1"""), {"d": day, "p": prev}).all()  # fmt: skip
    big = [(sym, float(x)) for sym, x in jumps if abs(math.log(float(x))) > math.log(1 + JUMP)]
    out.append(_c("price_jump", "warn" if big else ("ok" if jumps else "unknown"), len(big), JUMP,
                  ", ".join(f"{sym} x{x:.2f}" for sym, x in big[:6]) or None))  # fmt: skip
    return out


def _lineage(s: Session, day: date) -> list[Check]:
    from finresearch.dq.lineage import reuploads

    n = reuploads(s, DATASET, day)
    out = [_c("reupload", "warn" if n else "ok", n, 0, f"the {day} file changed {n} time(s) after its first load"
              if n else None)]  # fmt: skip
    sha = s.scalars(select(DatasetLoad.sha256).where(DatasetLoad.dataset == DATASET, DatasetLoad.day == day)
                    .order_by(DatasetLoad.id.desc()).limit(1)).first()  # fmt: skip
    if sha is None:
        return [
            *out,
            Check(DATASET, "duplicate_body", "unknown", note="no lineage for this load", critical=False),
        ]
    other = s.scalars(select(DatasetLoad.day).where(DatasetLoad.dataset == DATASET, DatasetLoad.sha256 == sha,
                                                    DatasetLoad.day != day).limit(1)).first()  # fmt: skip
    return [*out, _c("duplicate_body", "fail" if other else "ok", None, None,
                     f"same file body as {other}" if other else None)]  # fmt: skip
