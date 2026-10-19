"""Synthetic daily panels for the direction model's tests (#327): a made-up index on weekdays with a seeded random walk,
a flat-ish implied volatility, random option open interest and a front future. `signal` plants a known edge: the next
session's return loads on today's put-call ratio (pcr_oi), so a working model must find it and a broken one cannot.
No network, no database, no real prices."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from finresearch.direction.features import DayRow


def weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def last_thursday(d: date) -> date:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)
    return nxt - timedelta(days=(nxt.weekday() - 3) % 7)


def panel(start: date = date(2022, 1, 3), end: date = date(2025, 6, 30), *, seed: int = 1, signal: float = 0.0,
          iv: float = 15.0, rv_ratio: float = 1.0) -> list[DayRow]:  # fmt: skip
    """`signal` = the correlation between today's (pcr − 1)/0.1 and the next session's return; `rv_ratio` = realised ÷
    implied volatility (below 1 = a variance risk premium)."""
    rnd = random.Random(seed)
    rows: list[DayRow] = []
    level, prev_pcr, fut = 20000.0, 1.0, None
    for d in weekdays(start, end):
        sig_d = iv / 100 * rv_ratio / math.sqrt(252)
        z = (prev_pcr - 1) / 0.1
        ret = sig_d * (signal * z + math.sqrt(max(0.0, 1 - signal * signal)) * rnd.gauss(0, 1))
        level *= math.exp(ret)
        exp_d = last_thursday(d) if d <= last_thursday(d) else last_thursday(d + timedelta(days=10))
        f = level * math.exp(0.065 * (exp_d - d).days / 365)
        pcr = 1 + 0.1 * rnd.gauss(0, 1)
        call_oi = 1e6 * (1 + 0.1 * rnd.random())
        r = DayRow(d, ret=ret if rows else None, ret_source="underlying" if rows else None, close=level,
                   fut_expiry=exp_d, fut_high=f * (1 + abs(rnd.gauss(0, 0.004))), fut_low=f * (1 - abs(rnd.gauss(0, 0.004))),
                   fut_close=f, fut_prev_close=(fut[1] if fut and fut[0] == exp_d else f / math.exp(ret)),
                   iv_30=iv + rnd.gauss(0, 0.5), near_iv=iv, next_iv=iv + 0.5, skew_25d=2.0 + rnd.gauss(0, 0.3),
                   put_oi=call_oi * pcr, call_oi=call_oi, fut_oi=5e5 * (1 + 0.1 * rnd.random()),
                   near_expiry=d + timedelta(days=(3 - d.weekday()) % 7))  # fmt: skip
        r.extra["underlying"] = level
        rows.append(r)
        prev_pcr, fut = pcr, (exp_d, f)
    return rows
