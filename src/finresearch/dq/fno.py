"""Post-load checks on a stored F&O bhavcopy day (#368): what the parse-time checks cannot see, read from the database
after the load. One SQL pass over the day and the previous loaded day (a few thousand rows each), well inside the
nightly budget. Thresholds are the issue's where it states one; the others are named constants here.

* rows_total_z / rows_kept_z: robust z of the manifest's row counts against the last WINDOW loaded days (|z| > 6).
* futures_continuity: every symbol with a live future at t-1 (expiry on or after t) still has a future at t (a
  dropped symbol). A stock that left F&O keeps its contracts until their expiry, so this does not false-alarm on exits.
* symbol_changed (not critical): a vanished symbol whose open futures reappear under one new symbol with the same
  expiries, lot and price (pair_renames: L&TFH -> LTF in 2024, TATAMOTORS -> TMPV in 2025) is a rename, not a drop.
* expiry_continuity: every index future and index option expiry live at t-1 that had open interest, or is one of the
  two nearest, is still there at t (a missing expiry). A far expiry with no OI and no trades is not stored
  (fnohist.store drops dead rows), so its absence is not a loss.
* expiry_moved (not critical): a missing expiry that fell between the two sessions, moved to a date within
  MOVE_DAYS, or is an NSE holiday (classify_lost_expiries; MIDCPNIFTY's 22-Jan-2024 expiry on a special holiday).
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
from finresearch.dq import options, stats
from finresearch.dq.base import Check, share_status, soft

DATASET = "fno_bhav"
WINDOW = 60
STALE_SHARE = 0.5
LOT_SHARE = 0.05
TURNOVER_SHARE = 0.05
TURNOVER_BAND = 0.02
JUMP = 0.30
MOVE_DAYS = 3  # an expiry NSE shifts for a holiday moves to an adjacent session
CHECKS = ("rows_total_z", "rows_kept_z", "futures_continuity", "symbol_changed", "expiry_continuity", "expiry_moved",
          "expired_contracts", "stale_settle", "oi_lot_multiple", "turnover_units", "price_jump", "row_ranges", "reupload",
          "duplicate_body")  # fmt: skip
SOFT = options.CHECKS  # non-critical screens over the same rows, each group guarded (base.soft)
NON_CRITICAL = {"price_jump", "reupload", "symbol_changed", "expiry_moved", *SOFT}


def _c(check: str, status: str, value: float | None = None, threshold: float | None = None,
       note: str | None = None) -> Check:  # fmt: skip
    return Check(DATASET, check, status, value, threshold, note, critical=check not in NON_CRITICAL)


def bhav_checks(s: Session, day: date, holidays: set[date] | None = None) -> list[Check]:
    m = s.get(FnoBhavDay, day)
    if m is None or m.status != "loaded":
        return [_c(c, "unknown", note=f"{day} is not loaded") for c in CHECKS + SOFT]
    hist = s.scalars(select(FnoBhavDay).where(FnoBhavDay.status == "loaded", FnoBhavDay.day < day)
                     .order_by(FnoBhavDay.day.desc()).limit(WINDOW)).all()  # fmt: skip
    prev = hist[0].day if hist else None
    out = []
    for col in ("rows_total", "rows_kept"):
        x, h = getattr(m, col), [getattr(r, col) for r in hist if getattr(r, col) is not None]
        z = stats.robust_z(float(x), [float(v) for v in h]) if x is not None else None
        out.append(_c(f"{col}_z", stats.z_status(z), z, stats.Z_LIMIT, f"{col} {x} vs {len(h)} prior days"))
    out += _continuity(s, day, prev, holidays) + _contents(s, day, prev) + _lineage(s, day)
    return out + soft(s, DATASET, options.CHECKS, options.option_checks, day)


def _continuity(s: Session, day: date, prev: date | None, holidays: set[date] | None = None) -> list[Check]:
    names = ("futures_continuity", "symbol_changed", "expiry_continuity", "expiry_moved")
    if prev is None:
        return [_c(c, "unknown", note="no earlier loaded day") for c in names]
    q = {"d": day, "p": prev}
    # one row per (day, symbol, instrument, expiry): OI, lot and close summed/taken over strikes
    rows = s.execute(text("""
        SELECT day, symbol, instrument, expiry, sum(oi), max(lot_size), max(close) FILTER (WHERE option_type = 'XX')
        FROM fno_bhav WHERE day IN (:p, :d) AND instrument IN ('IDF', 'STF', 'IDO') AND expiry >= :p
        GROUP BY 1, 2, 3, 4"""), q).all()  # fmt: skip
    at = {prev: [r for r in rows if r[0] == prev], day: [r for r in rows if r[0] == day]}
    fut = {d: [r for r in at[d] if r[2] != "IDO"] for d in at}
    # a symbol whose live future (expiry on or after `day`) at t-1 has no future at t
    was = {r[1] for r in fut[prev] if r[3] >= day}
    now = {r[1] for r in fut[day]}
    pairs, gone = pair_renames(_fut_profile(fut[prev], day, sorted(was - now)),
                               _fut_profile(fut[day], day, sorted(now - {r[1] for r in at[prev]})))  # fmt: skip
    groups = {
        (r[1], r[2], r[3]): (int(r[4] or 0)) for r in at[prev] if r[2] in ("IDF", "IDO") and r[3] > prev
    }
    lost, moved, idle = classify_lost_expiries(
        groups, {(r[1], r[2], r[3]) for r in at[day]}, prev, day, holidays
    )
    return [
        _c("futures_continuity", "fail" if gone else "ok", len(gone), 0,
           f"no future at {day} for: {', '.join(gone[:8])}" if gone else None),
        _c("symbol_changed", "warn" if pairs else "ok", len(pairs), 0,
           ", ".join(f"symbol changed {a}→{b}" for a, b in pairs[:6]) or None),
        _c("expiry_continuity", "fail" if lost else "ok", len(lost), 0,
           "missing: " + ", ".join(f"{a} {b} {e}" for a, b, e in lost[:6]) if lost
           else (f"{idle} far expiry(ies) with no open interest at {prev} not required" if idle else None)),
        _c("expiry_moved", "warn" if moved else "ok", len(moved), 0, "; ".join(moved[:4]) or None),
    ]  # fmt: skip


def _fut_profile(rows, day: date, symbols: list[str]) -> dict[str, tuple]:
    """symbol -> (instrument, live expiries, lot, nearest future's close) for the given symbols."""
    out = {}
    for sym in symbols:
        mine = sorted((r for r in rows if r[1] == sym and r[3] >= day), key=lambda r: r[3])
        if mine:
            out[sym] = (mine[0][2], tuple(r[3] for r in mine), mine[0][5], mine[0][6])
    return out


def pair_renames(gone: dict[str, tuple], new: dict[str, tuple]) -> tuple[list[tuple[str, str]], list[str]]:
    """Pairs a vanished symbol A with a symbol B new at t when exactly one B matches A and A is B's only match:
    same instrument, same live expiries, same lot size (both unknown counts as same) and the nearest future's close
    within JUMP of A's. NSE renames (L&TFH -> LTF, TATAMOTORS -> TMPV) carry the open contracts over unchanged; a
    vanished symbol with no such twin stays an unexplained drop."""

    def twin(a: tuple, b: tuple) -> bool:
        if a[:3] != b[:3]:
            return False
        if a[3] is None or b[3] is None or a[3] <= 0 or b[3] <= 0:
            return False
        return abs(math.log(float(b[3]) / float(a[3]))) <= math.log(1 + JUMP)

    match = {a: [b for b in new if twin(pa, new[b])] for a, pa in gone.items()}
    used = [b for bs in match.values() for b in bs]
    pairs = [(a, bs[0]) for a, bs in match.items() if len(bs) == 1 and used.count(bs[0]) == 1]
    paired = {a for a, _ in pairs}
    return pairs, [a for a in gone if a not in paired]


def classify_lost_expiries(groups: dict[tuple[str, str, date], int], today: set[tuple[str, str, date]], prev: date,
                           day: date, holidays: set[date] | None = None) -> tuple[list, list[str], int]:  # fmt: skip
    """The index expiries live at t-1 (`groups`: (symbol, instrument, expiry) -> open interest) that are missing at t,
    split into (unexplained losses, explained moves, count of idle far expiries not required).

    Required: open interest > 0 at t-1 (the store drops rows with no OI and no trades, fnohist.store, so a contract
    with OI must reappear) or one of the symbol's two nearest expiries. Explained: the expiry date fell between the two
    sessions (a special holiday such as 22-Jan-2024: NSE settled it on the earlier session); the same symbol and
    instrument gained an expiry within MOVE_DAYS that it did not have at t-1 (NSE moved the date); or the date is an
    NSE trading holiday (when the list is known)."""
    rank: dict[tuple[str, str, date], int] = {}
    for sym, inst in {(a, b) for a, b, _ in groups}:
        for i, e in enumerate(sorted(e for a, b, e in groups if (a, b) == (sym, inst))):
            rank[(sym, inst, e)] = i
    new = today - set(groups)
    lost, moved, idle = [], [], 0
    for key in sorted(set(groups) - today):
        sym, inst, e = key
        if e < day:
            moved.append(f"{sym} {inst} {e} fell between sessions {prev} and {day}")
            continue
        if groups[key] <= 0 and rank[key] >= 2:
            idle += 1
            continue
        to = sorted(x for a, b, x in new if (a, b) == (sym, inst) and 0 < abs((x - e).days) <= MOVE_DAYS)
        if to:
            moved.append(f"{sym} {inst} expiry moved {e}→{to[0]}")
        elif holidays is not None and e in holidays:
            moved.append(f"{sym} {inst} {e} is an NSE holiday")
        else:
            lost.append(key)
    return lost, moved, idle


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
