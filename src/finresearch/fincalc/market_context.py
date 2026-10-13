"""Market-context arithmetic for /fno (#317): moving averages and their slope, Wilder's ADX, a trend-regime label,
open-interest build-up classes, put-call ratios. Pure functions; signals.fno_context reads the data. These describe
the market; none of them is a trading signal (see signals.fno_context.EVIDENCE).

Definitions
- SMA(n): the mean of the last n closes. Slope: the SMA's % change over the last SLOPE_DAYS sessions. Distance: the
  close's % above (+) or below (−) the SMA.
- ADX(14), Wilder (1978, "New Concepts in Technical Trading Systems"): +DM = high − previous high when it is larger
  than previous low − low and positive (−DM the mirror); TR = max(high − low, |high − previous close|, |low −
  previous close|); each smoothed with Wilder's running sum (S = S − S/n + x); +DI = 100·+DM/TR, DX = 100·|+DI − −DI|
  ÷ (+DI + −DI); ADX = Wilder's average of DX (the first value is the mean of the first n DX). Needs high and low:
  without them it is not computed (a close-only number would not be ADX).
- Regime: "uptrend" when close > SMA50 > SMA200 and SMA50 rises, "downtrend" when close < SMA50 < SMA200 and SMA50
  falls, else "range / mixed"; strength from ADX (< 20 weak, 20-25 emerging, > 25 trending: the common practitioner reading [U]).
- OI build-up (the classic price × open-interest table): price up and OI up = long build-up; price down and OI up =
  short build-up; price up and OI down = short covering; price down and OI down = long unwinding. For an option the
  "price" is the option's premium, so a call's "long build-up" says new calls opened while the premium rose (every
  contract has a buyer and a seller), not that the underlying will rise. Either change unknown (or zero) = unknown / no change.
"""

from __future__ import annotations

from collections.abc import Sequence

SLOPE_DAYS = 10
ADX_N = 14


def sma(closes: Sequence[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def sma_slope(closes: Sequence[float], n: int, days: int = SLOPE_DAYS) -> float | None:
    """% change of SMA(n) over the last `days` sessions."""
    if len(closes) < n + days:
        return None
    now, then = sma(closes, n), sma(closes[:-days], n)
    return None if not then else (now / then - 1) * 100  # type: ignore[operator]


def distance(close: float, avg: float | None) -> float | None:
    return None if not avg else (close / avg - 1) * 100


def adx(
    high: Sequence[float], low: Sequence[float], close: Sequence[float], n: int = ADX_N
) -> dict[str, float] | None:
    """{"adx", "plus_di", "minus_di"} at the last bar; None with fewer than 2n + 1 bars."""
    m = len(close)
    if m < 2 * n + 1 or len(high) != m or len(low) != m:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, m):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm.append(up if up > down and up > 0 else 0.0)
        mdm.append(down if down > up and down > 0 else 0.0)
        tr.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    s_tr, s_p, s_m = sum(tr[:n]), sum(pdm[:n]), sum(mdm[:n])
    dxs: list[float] = []
    pdi = mdi = 0.0
    for i in range(n, len(tr) + 1):
        if i > n:
            s_tr, s_p, s_m = (
                s_tr - s_tr / n + tr[i - 1],
                s_p - s_p / n + pdm[i - 1],
                s_m - s_m / n + mdm[i - 1],
            )
        pdi, mdi = (100 * s_p / s_tr, 100 * s_m / s_tr) if s_tr else (0.0, 0.0)
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi else 0.0)
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return {"adx": a, "plus_di": pdi, "minus_di": mdi}


def regime(close: float, sma50: float | None, sma200: float | None, slope50: float | None,
           adx_value: float | None) -> dict[str, str]:  # fmt: skip
    if sma50 is None or sma200 is None or slope50 is None:
        trend = "unknown"
    elif close > sma50 > sma200 and slope50 > 0:
        trend = "uptrend"
    elif close < sma50 < sma200 and slope50 < 0:
        trend = "downtrend"
    else:
        trend = "range / mixed"
    strength = ("unknown" if adx_value is None else "weak" if adx_value < 20 else
                "emerging" if adx_value <= 25 else "trending")  # fmt: skip
    return {"trend": trend, "strength": strength}


BUILDUP_METHOD = ("Price up + OI up = long build-up; price down + OI up = short build-up; price up + OI down = short "
                  "covering; price down + OI down = long unwinding. Every new contract has a buyer and a seller, so "
                  "the label only says which side was paying up. For an option the price is its premium: a call's "
                  "long build-up is not a forecast that the underlying will rise.")  # fmt: skip
BUILDUP = {
    (1, 1): "long build-up",
    (-1, 1): "short build-up",
    (1, -1): "short covering",
    (-1, -1): "long unwinding",
}


def buildup(price_change: float | None, oi_change: float | None) -> str:
    """The build-up class from the price change and the OI change (module docstring)."""
    if price_change is None or oi_change is None:
        return "unknown"
    if price_change == 0 or oi_change == 0:
        return "no change"
    return BUILDUP[(1 if price_change > 0 else -1, 1 if oi_change > 0 else -1)]


def pcr(put: float | None, call: float | None) -> float | None:
    return None if put is None or not call else put / call
