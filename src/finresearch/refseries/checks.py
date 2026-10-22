"""Read-only acceptance checks for the stored index closes (#366), run by `finresearch audit index-eod` after a
backfill. They read the database only; no network.

* underlying_vs_close: the F&O bhavcopy's underlying price (UDiFF files, 2024 on) against the index file's close for
  the same day and F&O symbol. Acceptance: 100 % equal to 0.01, or every exception listed and explained.
* vix_vs_iv30: India VIX against the app's NIFTY 30-day IV (fno_iv_eod.iv_30d): the correlation of daily changes
  (acceptance >= 0.9) and the level gap's median and MAD (fincalc.refseries.vix_vs_iv30).
* par3m_vs_tbill: FBIL's 3-month par yield (as a continuous rate) against its 3-month T-bill benchmark: within 15 bp.
"""

from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finresearch.fincalc import refseries as rs

FNO_INDICES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50")
TOLERANCE = 0.01


def underlying_vs_close(s: Session, symbols: tuple[str, ...] = FNO_INDICES, start: date = date(2024, 1, 1),
                        end: date = date(2999, 1, 1)) -> dict[str, Any]:  # fmt: skip
    """Pairs, exact matches (|diff| <= 0.01), the mismatches, and bhavcopy days with no stored close (missing)."""
    rows = s.execute(text("""
        SELECT b.day, b.symbol, b.und, b.und_min, i.close
        FROM (SELECT day, symbol, max(underlying) AS und, min(underlying) AS und_min FROM fno_bhav
              WHERE symbol = ANY(:syms) AND day BETWEEN :a AND :b AND underlying IS NOT NULL
              GROUP BY day, symbol) b
        LEFT JOIN index_eod i ON i.day = b.day AND i.symbol = b.symbol
        ORDER BY b.day, b.symbol"""), {"syms": list(symbols), "a": start, "b": end}).all()  # fmt: skip
    pairs = exact = 0
    mismatches: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for d, sym, und, und_min, close in rows:
        if close is None:
            missing.append({"day": d.isoformat(), "symbol": sym})
            continue
        pairs += 1
        diff = float(und) - float(close)
        if abs(diff) <= TOLERANCE + 1e-9 and und == und_min:
            exact += 1
        else:
            mismatches.append({"day": d.isoformat(), "symbol": sym, "underlying": float(und), "close": float(close),
                               "diff": round(diff, 4)} | ({"underlying_min": float(und_min)} if und != und_min else {}))  # fmt: skip
    return {"pairs": pairs, "exact": exact, "exact_share": None if not pairs else exact / pairs,
            "mismatches": mismatches, "missing_close": missing, "tolerance": TOLERANCE}  # fmt: skip


def vix_vs_iv30(s: Session, start: date = date(2013, 1, 1), end: date = date(2999, 1, 1)) -> dict[str, Any]:
    """India VIX close vs NIFTY's iv_30d (both annual vol points) on the days both are stored."""
    rows = s.execute(text("""
        SELECT i.day, i.close, v.iv_30d FROM index_eod i JOIN fno_iv_eod v ON v.day = i.day AND v.symbol = 'NIFTY'
        WHERE i.symbol = 'INDIA VIX' AND i.day BETWEEN :a AND :b ORDER BY i.day"""), {"a": start, "b": end}).all()  # fmt: skip
    kept = [(d, float(c), float(iv)) for d, c, iv in rows if c is not None and iv is not None]
    res = rs.vix_vs_iv30([v for _, v, _ in kept], [iv for _, _, iv in kept])
    return res | {"first_day": kept[0][0].isoformat() if kept else None,
                  "last_day": kept[-1][0].isoformat() if kept else None}  # fmt: skip


TBILL_TOLERANCE_BP = 15


def par3m_vs_tbill(
    s: Session, start: date = date(2015, 1, 1), end: date = date(2999, 1, 1)
) -> dict[str, Any]:
    """#366: the 3-month rate from FBIL's G-sec par curve, r = 2 ln(1 + y_0.25 / 2), against FBIL's 3-month T-bill
    benchmark converted on refseries.rate_lookup's stated basis, on the days both are stored. Acceptance: within
    TBILL_TOLERANCE_BP. Reports the pairs, how many are within, the median gap and the ten widest gaps (bp)."""
    from finresearch.refseries.rate_lookup import TBILL_BASIS

    rows = s.execute(text("""
        SELECT p.day, p.rate, t.rate, t.tenor_years FROM rate_curve p
        JOIN rate_curve t ON t.day = p.day AND t.series = 'tbill' AND t.tenor = '3M'
        WHERE p.series = 'gsec_par' AND p.tenor_years = 0.25 AND p.day BETWEEN :a AND :b ORDER BY p.day"""),
                     {"a": start, "b": end}).all()  # fmt: skip
    gaps = []
    for d, par, tb, years in rows:
        days = round(float(years) * 365)
        r_tb = math.log1p(float(tb) * days / 365) * 365 / days
        gaps.append((d, (rs.short_rate_proxy(float(par)) - r_tb) * 1e4))
    within = sum(1 for _, g in gaps if abs(g) <= TBILL_TOLERANCE_BP)
    worst = sorted(gaps, key=lambda x: -abs(x[1]))[:10]
    return {"pairs": len(gaps), "within": within, "within_share": within / len(gaps) if gaps else None,
            "tolerance_bp": TBILL_TOLERANCE_BP,
            "median_gap_bp": statistics.median(g for _, g in gaps) if gaps else None,
            "worst": [{"day": d.isoformat(), "gap_bp": round(g, 2)} for d, g in worst],
            "tbill_basis": TBILL_BASIS}  # fmt: skip
