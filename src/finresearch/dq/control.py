"""Control charts over daily data-quality series (#368, P1.6): the small, lasting shifts a per-day check cannot see (a
parser that starts dropping a few percent of contracts, a forward or rate change that biases IV30 by a vol point, a
run of settles that barely moved). Non-critical: a chart says "this series left its range", not which value is wrong.

Baseline: the BASE (40) sessions before the last RUN (20), centre = median and σ = 1.4826 × MAD (robust, as
dq.stats). The chart runs over the last RUN sessions from the centre, on u = (x − centre) / σ. Fewer than BASE + RUN
sessions, or a flat baseline (MAD 0), is unknown, never ok.

* EWMA (Roberts 1959): z_i = λ u_i + (1 − λ) z_(i−1), out of control when |z| > L √(λ / (2 − λ)) (the asymptotic
  limit). λ = 0.2, L = 2.962: an in-control average run length of 500 sessions for independent normal data (Lucas &
  Saccucci 1990, Technometrics 32(1); reproduced for this module with the Brook & Evans 1972 Markov chain).
* Tabular CUSUM (Page 1954): C+_i = max(0, C+_(i−1) + u_i − k), C−_i = max(0, C−_(i−1) − u_i − k), out of control
  when either exceeds h. k = 0.5, h = 5: a two-sided in-control average run length of 465 (Montgomery, Introduction to
  Statistical Quality Control, Table 9.3; Siegmund's approximation and the Brook & Evans chain agree).

Event semantics: a chart warns on the session its statistic crosses the limit (inside on the session before). While
it stays beyond, later sessions are ok with "beyond its limit since D" in the note and the statistic in `value`, so a
lasting shift (a new contract cycle, a volatility regime) is reported once, not every day; it re-arms once back inside
(or once the baseline has absorbed the new level). These series are autocorrelated and move with the market, so the
run lengths are a scale, not a promise: the owner's replay (`finresearch dq replay`) measures the real warning rates.

Series (non-critical checks of the dataset they read):
* cc_rows_total, cc_rows_kept (fno_bhav): log of the manifest's row counts (NSE's file; what the store kept). CUSUM:
  a parser that loses a slice of contracts shows as a lasting step.
* cc_settle_dispersion (fno_bhav): log of 1.4826 × the cross-sectional MAD of the front futures' log settle changes
  from the previous loaded session (each within one contract, the earliest expiry after that session; at least
  MIN_SYMBOLS futures). EWMA. A stale or half-copied day pulls it down, a day from the wrong date pushes it up; a day
  where no settle moved is charted at the FLOOR, far below, never as minus infinity.
* cc_iv30 (fno_iv_eod): log of NIFTY's IV30. EWMA. It moves with the market (read it beside vix_iv30_gap).
* cc_iv30_vix_gap (fno_iv_eod): India VIX − NIFTY IV30 in vol points (what vix_iv30_gap tests day by day). EWMA: a
  lasting drift is the IV computation moving away from the exchange's own index, not the market moving.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finresearch.db.models import FnoBhavDay, FnoIvEod, IndexEod
from finresearch.dq.base import Check
from finresearch.dq.stats import MAD_SCALE

BASE, RUN = 40, 20
LAMBDA, L_EWMA = 0.2, 2.962
K_CUSUM, H_CUSUM = 0.5, 5.0
FNO_CHARTS = ("cc_rows_total", "cc_rows_kept", "cc_settle_dispersion")
IV_CHARTS = ("cc_iv30", "cc_iv30_vix_gap")
MIN_SYMBOLS = 20
FLOOR = 1e-6
IV_SYMBOL = "NIFTY"


@dataclass(frozen=True)
class Chart:
    status: str  # ok / warn / unknown
    stat: float | None = None  # EWMA z, or the larger CUSUM side, on the last session
    limit: float | None = None
    centre: float | None = None
    sigma: float | None = None
    beyond_since: int | None = (
        None  # run index (0 = the first of the RUN sessions) where the current excursion began
    )
    note: str | None = None


def chart(kind: str, values: Sequence[float]) -> Chart:
    """`kind` ewma | cusum over `values` (oldest first; the last is the session being checked)."""
    if len(values) < BASE + RUN:
        return Chart("unknown", note=f"needs {BASE + RUN} sessions, has {len(values)}")
    xs = list(values)[-(BASE + RUN) :]
    base, run = xs[:BASE], xs[BASE:]
    centre = statistics.median(base)
    sigma = MAD_SCALE * statistics.median([abs(x - centre) for x in base])
    if not sigma > 0 or not math.isfinite(sigma):
        return Chart("unknown", centre=centre, note="the baseline is flat (MAD 0): no scale to chart against")
    path = []
    if kind == "ewma":
        limit, z = L_EWMA * math.sqrt(LAMBDA / (2 - LAMBDA)), 0.0
        for x in run:
            z = LAMBDA * (x - centre) / sigma + (1 - LAMBDA) * z
            path.append(z)
        out = [abs(z) > limit for z in path]
    elif kind == "cusum":
        limit, hi, lo = H_CUSUM, 0.0, 0.0
        for x in run:
            u = (x - centre) / sigma
            hi, lo = max(0.0, hi + u - K_CUSUM), max(0.0, lo - u - K_CUSUM)
            path.append(hi if hi >= lo else -lo)
        out = [abs(c) > limit for c in path]
    else:
        raise ValueError(f"unknown chart {kind!r}")
    stat = path[-1]
    since = None
    if out[-1]:
        since = len(out) - 1
        while since > 0 and out[since - 1]:
            since -= 1
    crossed = out[-1] and (len(out) == 1 or not out[-2])
    return Chart("warn" if crossed else "ok", stat, limit, centre, sigma, since)


def _check(dataset: str, name: str, kind: str, series: list[tuple[date, float]], day: date, show) -> Check:
    """The chart of `series` (oldest first) as `day`'s check; unknown when `day` has no value."""
    if not series or series[-1][0] != day:
        return Check(dataset, name, "unknown", note=f"no value for {day}", critical=False)
    c = chart(kind, [v for _, v in series])
    if c.status == "unknown":
        return Check(dataset, name, "unknown", note=c.note, critical=False)
    head = f"{kind.upper()} {c.stat:+.2f} vs limit {c.limit:.2f}; {show(series[-1][1])}, {BASE}-session median {show(c.centre)}"
    if c.status == "warn":
        note = f"crossed its limit on {day}: {head}"
    elif c.beyond_since is not None:
        since = series[-RUN:][c.beyond_since][0]
        note = f"beyond its limit since {since}{' or earlier' if c.beyond_since == 0 else ''}: {head}"
    else:
        note = head
    return Check(dataset, name, c.status, round(c.stat or 0.0, 4), c.limit, note, critical=False)


_DISPERSION = text("""
    WITH days AS (
        SELECT day, prev FROM (SELECT day, lag(day) OVER (ORDER BY day) AS prev FROM fno_bhav_day
                               WHERE status = 'loaded' AND day <= :d) x
        ORDER BY day DESC LIMIT :n),
    f AS (SELECT day, symbol, expiry, settle FROM fno_bhav
          WHERE option_type = 'XX' AND settle > 0 AND day IN (SELECT day FROM days UNION SELECT prev FROM days)),
    r AS (SELECT DISTINCT ON (d.day, a.symbol) d.day, ln(a.settle / b.settle)::float8 AS r
          FROM days d JOIN f a ON a.day = d.day
          JOIN f b ON b.day = d.prev AND b.symbol = a.symbol AND b.expiry = a.expiry
          WHERE a.expiry > d.prev
          ORDER BY d.day, a.symbol, a.expiry),
    m AS (SELECT day, percentile_cont(0.5) WITHIN GROUP (ORDER BY r) AS med, count(*) AS n FROM r GROUP BY day)
    SELECT r.day, percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(r.r - m.med)), min(m.n)
    FROM r JOIN m USING (day) GROUP BY r.day ORDER BY r.day""")


def bhav_charts(s: Session, day: date) -> list[Check]:
    rows = s.execute(select(FnoBhavDay.day, FnoBhavDay.rows_total, FnoBhavDay.rows_kept)
                     .where(FnoBhavDay.status == "loaded", FnoBhavDay.day <= day)
                     .order_by(FnoBhavDay.day.desc()).limit(BASE + RUN)).all()[::-1]  # fmt: skip
    total = [(d, math.log(n)) for d, n, _ in rows if n and n > 0]
    kept = [(d, math.log(n)) for d, _, n in rows if n and n > 0]
    spread = [(d, math.log(max(MAD_SCALE * float(mad), FLOOR)))
              for d, mad, n in s.execute(_DISPERSION, {"d": day, "n": BASE + RUN}) if mad is not None and n >= MIN_SYMBOLS]  # fmt: skip

    def count(x: float) -> str:
        return f"{math.exp(x):,.0f} rows"

    def pct(x: float) -> str:
        return f"spread {math.exp(x):.3%}"

    return [_check("fno_bhav", "cc_rows_total", "cusum", total, day, count),
            _check("fno_bhav", "cc_rows_kept", "cusum", kept, day, count),
            _check("fno_bhav", "cc_settle_dispersion", "ewma", spread, day, pct)]  # fmt: skip


def iv_charts(s: Session, day: date) -> list[Check]:
    ivs = s.execute(select(FnoIvEod.day, FnoIvEod.iv_30d)
                    .where(FnoIvEod.symbol == IV_SYMBOL, FnoIvEod.day <= day, FnoIvEod.iv_30d > 0)
                    .order_by(FnoIvEod.day.desc()).limit(BASE + RUN)).all()[::-1]  # fmt: skip
    gaps = s.execute(select(IndexEod.day, IndexEod.close, FnoIvEod.iv_30d)
                     .join(FnoIvEod, (FnoIvEod.day == IndexEod.day) & (FnoIvEod.symbol == IV_SYMBOL))
                     .where(IndexEod.symbol == "INDIA VIX", IndexEod.day <= day, IndexEod.close.is_not(None),
                            FnoIvEod.iv_30d.is_not(None))
                     .order_by(IndexEod.day.desc()).limit(BASE + RUN)).all()[::-1]  # fmt: skip

    def vol(x: float) -> str:
        return f"IV30 {math.exp(x):.2f} %"

    def gap(x: float) -> str:
        return f"VIX - IV30 {x:+.2f} vol pts"

    return [_check("fno_iv_eod", "cc_iv30", "ewma", [(d, math.log(float(v))) for d, v in ivs], day, vol),
            _check("fno_iv_eod", "cc_iv30_vix_gap", "ewma", [(d, float(v) - float(i)) for d, v, i in gaps], day, gap)]  # fmt: skip
