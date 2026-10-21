"""Load one underlying's daily panel (direction.features.DayRow) from the F&O history tables of any database: the
app's own, or a read-only market-data copy (`--history-url`). Every session runs `SET TRANSACTION READ ONLY` first.

Sessions are the days with stored bhavcopy rows for the underlying. Per session: the 30-day IV, near/next ATM IV and
25-delta skew (fno_iv_eod); total put / call option OI and futures OI and the nearest option expiry (fno_bhav); the
front future's high/low/close; the bhavcopy's underlying value (2024 on); FII and Pro participant OI
(fno_participant_oi, market-wide); a stored index close (`index_eod`) when the database has that table.
The daily return (features.DayRow.ret): index close, else underlying value, else the front future (the earliest expiry
after the previous session with a settlement price on both days), each needing both sessions.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from finresearch.direction.features import DayRow

PARTICIPANT_COLS = ("fut_idx_long", "fut_idx_short", "opt_idx_call_long", "opt_idx_call_short", "opt_idx_put_long",
                    "opt_idx_put_short")  # fmt: skip


@contextmanager
def read_only(url: str | None = None) -> Iterator[Session]:
    from finresearch.db import session_scope

    with session_scope(url) as s:
        s.execute(text("SET TRANSACTION READ ONLY"))
        yield s


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _has_table(s: Session, name: str) -> bool:
    return bool(s.execute(text("SELECT to_regclass(:n) IS NOT NULL"), {"n": f"public.{name}"}).scalar())


def load(
    symbol: str, url: str | None = None, until: date | None = None
) -> tuple[list[DayRow], dict[str, Any]]:
    """(rows oldest first, data notes) for `symbol`; `until` cuts the panel (a live as-of day)."""
    sym = symbol.upper()
    cut = until or date(2999, 1, 1)
    with read_only(url) as s:
        agg = s.execute(text("""
            SELECT day,
                   sum(oi) FILTER (WHERE instrument IN ('IDO','STO') AND option_type = 'PE') AS put_oi,
                   sum(oi) FILTER (WHERE instrument IN ('IDO','STO') AND option_type = 'CE') AS call_oi,
                   sum(oi) FILTER (WHERE instrument IN ('IDF','STF')) AS fut_oi,
                   max(underlying) AS und,
                   min(expiry) FILTER (WHERE instrument IN ('IDO','STO') AND expiry >= day) AS near_exp
            FROM fno_bhav WHERE symbol = :s AND day <= :cut GROUP BY day ORDER BY day"""),
                        {"s": sym, "cut": cut}).all()  # fmt: skip
        futs = s.execute(text("""SELECT day, expiry, high, low, close, settle FROM fno_bhav
                                 WHERE symbol = :s AND instrument IN ('IDF','STF') AND day <= :cut"""),
                         {"s": sym, "cut": cut}).all()  # fmt: skip
        iv = {r[0]: r for r in s.execute(text("""SELECT day, iv_30d, near_iv, next_iv, skew_25d, underlying
                                                 FROM fno_iv_eod WHERE symbol = :s AND day <= :cut"""),
                                         {"s": sym, "cut": cut})}  # fmt: skip
        idx: dict[date, float] = {}
        if _has_table(s, "index_eod"):
            idx = {d: float(c) for d, c in s.execute(text("SELECT day, close FROM index_eod WHERE symbol = :s "
                                                          "AND day <= :cut AND close IS NOT NULL"),
                                                     {"s": sym, "cut": cut})}  # fmt: skip
        parts: dict[date, dict[str, dict[str, int]]] = {}
        if _has_table(s, "fno_participant_oi"):
            cols = ", ".join(PARTICIPANT_COLS)
            for row in s.execute(text(f"SELECT day, client_type, {cols} FROM fno_participant_oi "
                                      "WHERE client_type IN ('FII','Pro') AND day <= :cut"), {"cut": cut}):  # fmt: skip
                parts.setdefault(row[0], {})[row[1]] = {
                    k: int(v) for k, v in zip(PARTICIPANT_COLS, row[2:], strict=True)
                }
    by_day: dict[date, dict[date, tuple]] = {}
    for d, e, hi, lo, cl, st in futs:
        by_day.setdefault(d, {})[e] = (_f(hi), _f(lo), _f(cl), _f(st))
    rows: list[DayRow] = []
    checks = {"pairs": 0, "sign_agree": 0, "diff_sq": 0.0, "fut": [], "und": []}
    prev: DayRow | None = None
    for d, put_oi, call_oi, fut_oi, und, near_exp in agg:
        ivr = iv.get(d)
        u = _f(und) if und is not None else (_f(ivr[5]) if ivr is not None else None)
        r = DayRow(d, close=idx.get(d, u), iv_30=_f(ivr[1]) if ivr else None, near_iv=_f(ivr[2]) if ivr else None,
                   next_iv=_f(ivr[3]) if ivr else None, skew_25d=_f(ivr[4]) if ivr else None, put_oi=_f(put_oi),
                   call_oi=_f(call_oi), fut_oi=_f(fut_oi), near_expiry=near_exp, participants=parts.get(d))  # fmt: skip
        r.extra["underlying"] = u  # type: ignore[assignment]
        fut_ret = None
        if prev is not None:
            p0, p1 = by_day.get(prev.day, {}), by_day.get(d, {})
            e = set_front_future(r, p0, p1, prev.day)
            if e is not None:
                fut_ret = math.log(p1[e][3] / p0[e][3])  # type: ignore[operator]
            u0 = prev.extra.get("underlying")
            if d in idx and prev.day in idx:
                r.ret, r.ret_source = math.log(idx[d] / idx[prev.day]), "index"
            elif u and u0:
                r.ret, r.ret_source = math.log(u / u0), "underlying"
            elif fut_ret is not None:
                r.ret, r.ret_source = fut_ret, "future"
            if fut_ret is not None and u and u0:
                ur = math.log(u / u0)
                checks["pairs"] += 1
                checks["sign_agree"] += (ur > 0) == (fut_ret > 0)
                checks["diff_sq"] += (ur - fut_ret) ** 2
                checks["fut"].append(fut_ret)
                checks["und"].append(ur)
        rows.append(r)
        prev = r
    return rows, {"return_proxy_check": _proxy_check(checks), "index_eod": bool(idx),
                  "participant_days": sum(1 for r in rows if r.participants)}  # fmt: skip


def set_front_future(r: DayRow, p0: dict[date, tuple], p1: dict[date, tuple], prev_day: date) -> date | None:
    """Set r's front-future fields from the previous and this session's futures ({expiry: (high, low, close, settle)}):
    the earliest expiry after the previous session with a settlement on both days. Returns that expiry (or None)."""
    common = sorted(e for e in p1 if e in p0 and e > prev_day and p0[e][3] and p1[e][3])
    if not common:
        return None
    e = common[0]
    hi, lo, cl, st = p1[e]
    r.fut_expiry, r.fut_high, r.fut_low, r.fut_close = e, hi, lo, cl or st
    r.fut_prev_close = p0[e][2] or p0[e][3]
    return e


def _proxy_check(c: dict[str, Any]) -> dict[str, Any] | None:
    """How well the front future's daily return stands in for the index's, on the days that have both (2024 on)."""
    n = c["pairs"]
    if n < 2:
        return None
    f, u = c["fut"], c["und"]
    mf, mu = sum(f) / n, sum(u) / n
    cov = sum((a - mf) * (b - mu) for a, b in zip(f, u, strict=True))
    vf, vu = sum((a - mf) ** 2 for a in f), sum((b - mu) ** 2 for b in u)
    return {"days": n, "sign_agreement": round(c["sign_agree"] / n, 4),
            "correlation": round(cov / math.sqrt(vf * vu), 4) if vf > 0 and vu > 0 else None,
            "rmse_bp": round(math.sqrt(c["diff_sq"] / n) * 1e4, 2),
            "note": "front-future vs underlying daily log return on days with both (the proxy is used before 2024)"}  # fmt: skip
