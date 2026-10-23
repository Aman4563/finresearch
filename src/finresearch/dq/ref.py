"""Checks on the participant OI, derived IV, index close and SPAN datasets (#368), with the issue's reconciliations:

(a) participant OI: per futures bucket b, D_b = sum over participants of long_b - sum over the bhavcopy's futures in b
    with expiry > day of OI / lot (contracts); pass if |D_b| <= max(10, 1e-4 x total). Options are not reconciled: the
    bhavcopy store keeps only INDEX_OPTIONS' and tracked stocks' options. Days without lot sizes (pre-2024) are unknown.
    Also the identity sum(long_b) = sum(short_b) over the four participants (every contract has two sides).
    Participant trading volume (#371) gets the same presence and identity checks (every trade has a buyer and a
    seller) but no bhavcopy reconciliation. Both get a non-critical `reupload` check from the lineage table
    (unknown for a day loaded before lineage existed).
(b) the UDiFF underlying price vs the index file's close, exact to 0.01 (refseries.checks.underlying_vs_close).
(c) d_t = VIX_t - IV30_t, flagged when |d_t - median(d)| > 4 x MAD(d) over the previous WINDOW pairs. Not critical:
    a gap says the two measures disagree, not which one is wrong.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from finresearch.db.models import (
    DatasetLoad,
    FiiStats,
    FnoBhavDay,
    FnoIvEod,
    FnoParticipantOi,
    FnoParticipantVol,
    FoVolt,
    IndexEod,
    IndexEodDay,
    SpanDay,
)
from finresearch.dq import stats
from finresearch.dq.base import Check, share_status
from finresearch.refseries import checks as rc

PARTICIPANTS = ("Client", "DII", "FII", "Pro")
LONG_SHORT = (("fut_idx_long", "fut_idx_short"), ("fut_stk_long", "fut_stk_short"),
              ("opt_idx_call_long", "opt_idx_call_short"), ("opt_idx_put_long", "opt_idx_put_short"),
              ("opt_stk_call_long", "opt_stk_call_short"), ("opt_stk_put_long", "opt_stk_put_short"),
              ("total_long", "total_short"))  # fmt: skip
INDEX_SYMBOLS = ("NIFTY", "BANKNIFTY", "INDIA VIX")
# the index rows something reads (marks, direction, event vol, VRP, underlying_vs_close: by F&O symbol, or VIX): the
# strict, critical row_ranges rule applies to these only
USED_INDICES = (*rc.FNO_INDICES, "INDIA VIX")
VIX_RANGE = (
    5.0,
    100.0,
)  # a sanity band, not a market view: India VIX has never closed outside it (its 2008 peak was below 100)
IV_RANGE = (1.0, 200.0)
RV_STOCK_MAX = 100.0  # the stock rv_20 > 100 % defect after contract adjustments (#368 problem list)
FLAT_SHARE = 0.5
WINDOW = 60


def _tol(total: float) -> float:
    return max(10.0, 1e-4 * total)


def _reupload(s: Session, dataset: str, day: date) -> Check:
    from finresearch.dq.lineage import reuploads

    loads = s.scalar(select(func.count()).select_from(DatasetLoad).where(
        DatasetLoad.dataset == dataset, DatasetLoad.day == day))  # fmt: skip
    if not loads:
        return Check(dataset, "reupload", "unknown", note="no lineage for this load", critical=False)
    n = reuploads(s, dataset, day)
    return Check(dataset, "reupload", "warn" if n else "ok", n, 0,
                 f"the {day} file changed {n} time(s) after its first load" if n else None, critical=False)  # fmt: skip


def _participant_rows(
    s: Session, table, dataset: str, day: date, extra: tuple[str, ...]
) -> tuple[list, list[Check]]:
    """The day's rows and the presence, long/short identity and reupload checks (all unknown when not loaded)."""
    rows = s.scalars(select(table).where(table.day == day)).all()
    if not rows:
        names = ("participants_present", "long_short_identity", "reupload", *extra)
        return rows, [Check(dataset, n, "unknown", note=f"{day} is not loaded", critical=n != "reupload")
                      for n in names]  # fmt: skip
    got = sorted(r.client_type for r in rows)
    out = [Check(dataset, "participants_present", "ok" if got == sorted(PARTICIPANTS) else "fail",
                 len(got), 4, None if got == sorted(PARTICIPANTS) else f"rows: {', '.join(got)}")]  # fmt: skip
    worst = max(
        (abs(sum(getattr(r, lo) for r in rows) - sum(getattr(r, sh) for r in rows)), lo)
        for lo, sh in LONG_SHORT
    )
    tot = sum(r.total_long for r in rows)
    out.append(Check(dataset, "long_short_identity", "fail" if worst[0] > _tol(tot) else "ok",
                     worst[0], _tol(tot), f"largest gap in {worst[1].removesuffix('_long')}" if worst[0] else None))  # fmt: skip
    return rows, [*out, _reupload(s, dataset, day)]


def fii_stats_checks(s: Session, day: date) -> list[Check]:
    """NSE's FII derivative statistics (#371), all non-critical (read by nothing yet): the four aggregate categories
    are present, and no category shows a value without contracts (impossible; the reverse is not: a few cheap option
    contracts can round to 0.00 crore), plus the lineage re-upload check. Unknown when the day is not loaded."""
    from finresearch.adapters.nse_fii_stats import REQUIRED

    ds = "fii_stats"
    rows = s.scalars(select(FiiStats).where(FiiStats.day == day)).all()
    if not rows:
        return [Check(ds, n, "unknown", note=f"{day} is not loaded", critical=False)
                for n in ("categories_present", "value_identity", "reupload")]  # fmt: skip
    have = {r.category for r in rows}
    missing = [c for c in REQUIRED if c not in have]
    bad = [f"{r.category} {side}" for r in rows for side in ("buy", "sell", "oi")
           if getattr(r, f"{side}_contracts") == 0 and getattr(r, f"{side}_amt_cr") != 0]  # fmt: skip
    return [
        Check(ds, "categories_present", "fail" if missing else "ok", len(REQUIRED) - len(missing), len(REQUIRED),
              f"missing: {', '.join(missing)}" if missing else None, critical=False),
        Check(ds, "value_identity", "fail" if bad else "ok", len(bad), 0,
              f"a value with no contracts: {', '.join(bad)}" if bad else None, critical=False),
        _reupload(s, ds, day),
    ]  # fmt: skip


def participant_vol_checks(s: Session, day: date) -> list[Check]:
    return _participant_rows(s, FnoParticipantVol, "fno_participant_vol", day, ())[1]


def participant_checks(s: Session, day: date) -> list[Check]:
    rows, out = _participant_rows(
        s, FnoParticipantOi, "fno_participant_oi", day, ("bhav_fut_idx", "bhav_fut_stk")
    )
    if not rows:
        return out
    m = s.get(FnoBhavDay, day)
    for bucket, inst in (("fut_idx", "IDF"), ("fut_stk", "STF")):
        name = f"bhav_{bucket}"
        if m is None or m.status != "loaded":
            out.append(Check("fno_participant_oi", name, "unknown", note=f"no bhavcopy for {day}"))
            continue
        n, unknown_lot, lots = s.execute(text("""
            SELECT count(*), count(*) FILTER (WHERE lot_size IS NULL OR lot_size <= 0),
                   sum(oi::numeric / NULLIF(lot_size, 0))
            FROM fno_bhav WHERE day = :d AND instrument = :i AND expiry > :d"""), {"d": day, "i": inst}).one()  # fmt: skip
        if not n or unknown_lot:
            out.append(Check("fno_participant_oi", name, "unknown", note="lot sizes unknown (pre-2024 file)"))
            continue
        long_ = sum(getattr(r, f"{bucket}_long") for r in rows)
        d = float(long_) - float(lots)
        out.append(Check("fno_participant_oi", name, "fail" if abs(d) > _tol(long_) else "ok", d, _tol(long_),
                         f"participants {long_} vs bhavcopy {float(lots):.0f} contracts"))  # fmt: skip
    return out


def index_checks(s: Session, day: date) -> list[Check]:
    names = ("expected_symbols", "row_ranges", "other_row_ranges", "underlying_vs_close")
    m = s.get(IndexEodDay, day)
    if m is None or m.status != "loaded":
        return [Check("index_eod", n, "unknown", note=f"{day} is not loaded", critical=n != "other_row_ranges")
                for n in names]  # fmt: skip
    have = set(
        s.scalars(select(IndexEod.symbol).where(IndexEod.day == day, IndexEod.symbol.in_(INDEX_SYMBOLS)))
    )
    miss = [x for x in INDEX_SYMBOLS if x not in have]
    out = [Check("index_eod", "expected_symbols", "fail" if miss else "ok", len(miss), 0,
                 f"missing: {', '.join(miss)}" if miss else None)]  # fmt: skip
    used = {"d": day, "used": list(USED_INDICES), "lo": VIX_RANGE[0], "hi": VIX_RANGE[1]}
    bad = s.scalar(text("""
        SELECT count(*) FROM index_eod WHERE day = :d AND symbol = ANY(:used)
               AND (close <= 0 OR high < low OR close > high OR close < low
                    OR (symbol = 'INDIA VIX' AND (close < :lo OR close > :hi)))"""), used)  # fmt: skip
    out.append(Check("index_eod", "row_ranges", "fail" if bad else "ok", bad, 0,
                     f"{bad} row(s) of an index the app reads out of range" if bad else None))  # fmt: skip
    # the ~100 other indices in NSE's file: nothing reads them, so a defect in NSE's own row is shown, not gated.
    # A dividend-points index accumulates from 0 each April and is printed with zero/blank high-low: close >= 0 only.
    odd = s.execute(text("""
        SELECT symbol FROM index_eod WHERE day = :d AND NOT (symbol = ANY(:used))
               AND (close < 0 OR (symbol NOT LIKE '%DIVIDEND POINTS%' AND high > 0 AND low > 0
                                  AND (high < low OR close > high OR close < low)))
        ORDER BY 1"""), used).scalars().all()  # fmt: skip
    out.append(Check("index_eod", "other_row_ranges", "warn" if odd else "ok", len(odd), 0,
                     ", ".join(odd[:6]) or None, critical=False))  # fmt: skip
    r = rc.underlying_vs_close(s, start=day, end=day)
    bad_n = len(r["mismatches"])
    st = "unknown" if not r["pairs"] else ("fail" if bad_n else "ok")
    out.append(Check("index_eod", "underlying_vs_close", st, bad_n, rc.TOLERANCE,
                     "; ".join(f"{x['symbol']} {x['diff']:+}" for x in r["mismatches"][:5])
                     or (None if r["pairs"] else "no UDiFF underlying for this day")))  # fmt: skip
    return out


def iv_checks(s: Session, day: date) -> list[Check]:
    rows = s.scalars(select(FnoIvEod).where(FnoIvEod.day == day)).all()
    out = [Check("fno_iv_eod", "present", "ok" if any(r.symbol == "NIFTY" for r in rows) else "fail",
                 len(rows), None, None if rows else f"no derived IV for {day}")]  # fmt: skip
    ivs = [float(v) for r in rows for v in (r.iv_30d, r.near_iv, r.next_iv) if v is not None]
    bad = sum(1 for r in rows if any(v is not None and not IV_RANGE[0] < float(v) < IV_RANGE[1]
                                     for v in (r.iv_30d, r.near_iv, r.next_iv)))  # fmt: skip
    out.append(
        Check("fno_iv_eod", "iv_range", "unknown" if not ivs else ("fail" if bad else "ok"), bad, IV_RANGE[1])
    )
    hi = [r.symbol for r in rows if r.symbol not in rc.FNO_INDICES and r.rv_20 is not None
          and float(r.rv_20) > RV_STOCK_MAX]  # fmt: skip
    out.append(Check("fno_iv_eod", "stock_rv20_high", "warn" if hi else "ok", len(hi), RV_STOCK_MAX,
                     ", ".join(hi[:6]) or None, critical=False))  # fmt: skip
    meth = [r.iv_30d_method for r in rows if r.iv_30d is not None]
    st, v = share_status(sum(1 for x in meth if x == "flat"), len(meth), FLAT_SHARE)
    out.append(
        Check("fno_iv_eod", "flat_iv30_share", "warn" if st == "fail" else st, v, FLAT_SHARE, critical=False)
    )
    out.append(_vix_gap(s, day))
    return out


def _vix_gap(s: Session, day: date) -> Check:
    pairs = s.execute(select(IndexEod.day, IndexEod.close, FnoIvEod.iv_30d)
                      .join(FnoIvEod, (FnoIvEod.day == IndexEod.day) & (FnoIvEod.symbol == "NIFTY"))
                      .where(IndexEod.symbol == "INDIA VIX", IndexEod.day <= day, IndexEod.close.is_not(None),
                             FnoIvEod.iv_30d.is_not(None))
                      .order_by(IndexEod.day.desc()).limit(WINDOW + 1)).all()  # fmt: skip
    if not pairs or pairs[0][0] != day:
        return Check(
            "fno_iv_eod", "vix_iv30_gap", "unknown", note="no VIX and IV30 pair for this day", critical=False
        )
    gaps = [float(v) - float(iv) for _, v, iv in pairs]
    st = stats.mad_status(gaps[0], gaps[1:], k=4.0)
    return Check("fno_iv_eod", "vix_iv30_gap", "warn" if st == "fail" else st, round(gaps[0], 3), 4.0,
                 f"VIX - IV30 {gaps[0]:+.2f} vol pts vs {len(gaps) - 1} prior days", critical=False)  # fmt: skip


def span_checks(s: Session, day: date) -> list[Check]:
    m = s.get(SpanDay, day)
    if m is None or m.status != "loaded":
        return [
            Check("span", n, "unknown", note=f"{day} is not loaded")
            for n in ("row_ranges", "fo_volt_present")
        ]
    bad = s.scalar(text("SELECT count(*) FROM span_param WHERE day = :d AND (price_scan <= 0 OR vol_scan <= 0 "
                        "OR vol_scan > 1 OR price <= 0)"), {"d": day})  # fmt: skip
    nv = s.scalar(select(func.count()).select_from(FoVolt).where(FoVolt.day == day))
    return [Check("span", "row_ranges", "fail" if bad else "ok", bad, 0, f"{bad} row(s) out of range" if bad else None),
            Check("span", "fo_volt_present", "ok" if nv else "fail", nv, None, None if nv else "no FOVOLT rows")]  # fmt: skip
