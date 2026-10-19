"""The daily panel of one underlying and its point-in-time features and targets (#327, direction.specs).

`DayRow` holds what was known at one session's close. `features_at(rows, i)` reads only rows[i − LOOKBACK + 1 .. i];
`target(rows, i, h, event)` reads the h sessions after i and is used only as a label. `build_features(check_every=n)`
is the look-ahead guard: it recomputes features after replacing every later row with junk and fails on a difference.

Return series: `ret` is the log return from the previous stored session to this one, from (in order) a stored index
close (`index`), the bhavcopy's underlying value (`underlying`, 2024 on) or the front future (`future`, the same
contract on both days; fincalc.fno_history.front_future_returns). A missing return leaves every window that contains it
unknown, never filled.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from finresearch.direction import specs
from finresearch.fincalc import market_context as mc


@dataclass
class DayRow:
    day: date
    ret: float | None = None  # log return since the previous stored session
    ret_source: str | None = None  # index | underlying | future
    close: float | None = None  # index close or the bhavcopy's underlying value (informational)
    # the front future used for the day's return: its high/low/close and its close on the previous session (the same
    # contract), for a forward ratio-adjusted ADX
    fut_expiry: date | None = None
    fut_high: float | None = None
    fut_low: float | None = None
    fut_close: float | None = None
    fut_prev_close: float | None = None
    iv_30: float | None = None
    near_iv: float | None = None
    next_iv: float | None = None
    skew_25d: float | None = None
    put_oi: float | None = None
    call_oi: float | None = None
    fut_oi: float | None = None
    near_expiry: date | None = None  # the nearest option expiry on or after the day
    participants: dict[str, dict[str, int]] | None = (
        None  # {"FII": {...}, "Pro": {...}} (fno_participant_oi keys)
    )
    extra: dict[str, float] = field(default_factory=dict)


def _window(rets: Sequence[float | None], n: int) -> list[float] | None:
    """The last n returns, or None when fewer exist or one is missing."""
    if len(rets) < n:
        return None
    w = list(rets[-n:])
    return None if any(x is None for x in w) else w  # type: ignore[return-value]


def _ret(rets: Sequence[float | None], n: int) -> float | None:
    w = _window(rets, n)
    return None if w is None else sum(w)


def _dist_ma(rets: Sequence[float | None], n: int) -> float | None:
    """(close / SMA(n) − 1)·100 from returns alone: level[t − j] / level[t] = exp(−Σ the last j returns)."""
    w = _window(rets, n - 1)
    if w is None:
        return None
    rel, acc = [1.0], 0.0
    for x in reversed(w):
        acc += x
        rel.append(math.exp(-acc))
    return (1 / (sum(rel) / n) - 1) * 100


def _rv(rets: Sequence[float | None], n: int) -> float | None:
    w = _window(rets, n)
    if w is None:
        return None
    m = sum(w) / n
    return math.sqrt(sum((x - m) ** 2 for x in w) / (n - 1) * specs.YEAR_SESSIONS) * 100


def _adx(prefix: Sequence[DayRow]) -> float | None:
    """ADX(14) on the front future, forward ratio-adjusted at each roll (factor from both contracts' closes on the
    session before the roll), so the series up to day t uses only data up to day t. Restarts after a gap."""
    hs: list[float] = []
    ls: list[float] = []
    cs: list[float] = []
    factor, prev = 1.0, None
    for r in prefix:
        if r.fut_close is None or r.fut_high is None or r.fut_low is None:
            hs, ls, cs, factor, prev = [], [], [], 1.0, None
            continue
        if prev is not None and r.fut_expiry != prev.fut_expiry:
            if r.fut_prev_close and prev.fut_close:
                factor *= prev.fut_close / r.fut_prev_close
            else:
                hs, ls, cs, factor = [], [], [], 1.0
        hs.append(r.fut_high * factor)
        ls.append(r.fut_low * factor)
        cs.append(r.fut_close * factor)
        prev = r
    need = 4 * mc.ADX_N  # enough bars for Wilder's smoothing to settle
    if len(cs) < need:
        return None
    got = mc.adx(hs[-need * 3 :], ls[-need * 3 :], cs[-need * 3 :])
    return None if got is None else got["adx"]


def _participant(p: dict[str, dict[str, int]] | None, who: str, kind: str) -> float | None:
    r = (p or {}).get(who)
    if not r:
        return None
    if kind == "fut":
        tot = r["fut_idx_long"] + r["fut_idx_short"]
        return None if not tot else (r["fut_idx_long"] - r["fut_idx_short"]) / tot
    tot = r["opt_idx_call_long"] + r["opt_idx_call_short"] + r["opt_idx_put_long"] + r["opt_idx_put_short"]
    net = r["opt_idx_call_long"] - r["opt_idx_call_short"] - r["opt_idx_put_long"] + r["opt_idx_put_short"]
    return None if not tot else net / tot


def _pcr(r: DayRow) -> float | None:
    return mc.pcr(r.put_oi, r.call_oi)


def _log_chg(a: float | None, b: float | None) -> float | None:
    return None if not a or not b or a <= 0 or b <= 0 else math.log(a / b)


LOOKBACK = 400  # sessions read back from day t (the 200-session average and ADX's warm-up need fewer)


def features_at(rows: Sequence[DayRow], i: int) -> dict[str, float | None]:
    """Every feature of specs.FEATURES at the close of rows[i], from rows[i − LOOKBACK + 1 .. i] only."""
    prefix = rows[max(0, i + 1 - LOOKBACK) : i + 1]
    t = prefix[-1]
    rets = [r.ret for r in prefix]
    rv20 = _rv(rets, 20)
    ivs = [r.iv_30 for r in prefix[-252:] if r.iv_30 is not None]
    iv_rank = None
    if t.iv_30 is not None and len(ivs) >= 60:
        lo, hi = min(ivs), max(ivs)
        iv_rank = (t.iv_30 - lo) / (hi - lo) if hi > lo else 0.5
    p5 = prefix[-6] if len(prefix) >= 6 else None
    pcr_now, pcr_then = _pcr(t), (_pcr(p5) if p5 else None)
    dow = t.day.weekday()
    return {
        "ret_5": _ret(rets, 5), "ret_20": _ret(rets, 20), "ret_60": _ret(rets, 60),
        "dist_ma20": _dist_ma(rets, 20), "dist_ma50": _dist_ma(rets, 50), "dist_ma200": _dist_ma(rets, 200),
        "adx_14": _adx(prefix), "rv_20": rv20, "rv_60": _rv(rets, 60), "iv_30": t.iv_30, "iv_rank": iv_rank,
        "iv_rv_spread": None if t.iv_30 is None or rv20 is None else t.iv_30 - rv20,
        "term_slope": None if t.near_iv is None or t.next_iv is None else t.next_iv - t.near_iv,
        "skew_25d": t.skew_25d, "pcr_oi": pcr_now,
        "pcr_chg_5": None if pcr_now is None or pcr_then is None else pcr_now - pcr_then,
        "opt_oi_chg_5": _log_chg((t.put_oi or 0) + (t.call_oi or 0) if t.put_oi and t.call_oi else None,
                                 ((p5.put_oi or 0) + (p5.call_oi or 0)) if p5 and p5.put_oi and p5.call_oi else None),
        "fut_oi_chg_5": _log_chg(t.fut_oi, p5.fut_oi if p5 else None),
        "fii_fut_net": _participant(t.participants, "FII", "fut"), "fii_opt_net": _participant(t.participants, "FII", "opt"),
        "pro_fut_net": _participant(t.participants, "Pro", "fut"), "pro_opt_net": _participant(t.participants, "Pro", "opt"),
        "dte_near": None if t.near_expiry is None else float((t.near_expiry - t.day).days),
        "expiry_day": None if t.near_expiry is None else float(t.near_expiry == t.day),
        "dow_mon": float(dow == 0), "dow_tue": float(dow == 1), "dow_wed": float(dow == 2), "dow_thu": float(dow == 3),
    }  # fmt: skip


class LookAheadError(AssertionError):
    pass


def junk_after(rows: Sequence[DayRow], i: int) -> list[DayRow]:
    """rows with every row after i replaced by absurd values (the look-ahead guard's probe)."""
    junk = [DayRow(r.day, ret=9.9, ret_source="junk", iv_30=999.0, near_iv=1.0, next_iv=500.0, skew_25d=-50.0,
                   put_oi=1.0, call_oi=1e12, fut_oi=1.0, fut_expiry=r.day, fut_high=1e9, fut_low=1.0, fut_close=5.0,
                   fut_prev_close=7.0, near_expiry=r.day) for r in rows[i + 1 :]]  # fmt: skip
    return [*rows[: i + 1], *junk]


def build_features(rows: Sequence[DayRow], check_every: int = 0) -> list[dict[str, float | None]]:
    """features_at for every row. With `check_every` = n > 0 every n-th row (and the last) is recomputed after every
    later row is replaced by junk (`junk_after`); any difference raises LookAheadError. The evaluator runs it."""
    out = [features_at(rows, i) for i in range(len(rows))]
    if check_every > 0 and rows:
        for i in sorted({*range(0, len(rows), check_every), len(rows) - 1}):
            if features_at(junk_after(rows, i), i) != out[i]:
                raise LookAheadError(f"feature values at {rows[i].day} depend on later rows")
    return out


def sigma_h(iv_30: float | None, h: int) -> float | None:
    """σ over h sessions from the annual 30-day IV (%), as a log-return standard deviation."""
    return None if iv_30 is None or iv_30 <= 0 else iv_30 / 100 * math.sqrt(h / specs.YEAR_SESSIONS)


def forward_return(rows: Sequence[DayRow], i: int, h: int) -> float | None:
    """R = ln(S[i+h] / S[i]): the sum of the next h stored returns; None when one is missing or beyond the data."""
    if i + h >= len(rows):
        return None
    w = [rows[j].ret for j in range(i + 1, i + h + 1)]
    return None if any(x is None for x in w) else sum(w)  # type: ignore[arg-type]


def outcome(r: float, sig: float | None, event: str) -> int | None:
    if event == "up":
        return int(r > 0)
    if sig is None:
        return None
    return int(r > specs.K * sig) if event == "up_k" else int(r < -specs.K * sig)


def target(rows: Sequence[DayRow], i: int, h: int, event: str) -> int | None:
    """The event's outcome for day i (a label: reads rows after i)."""
    r = forward_return(rows, i, h)
    return None if r is None else outcome(r, sigma_h(rows[i].iv_30, h), event)
