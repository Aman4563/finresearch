"""End-of-day implied and realised volatility from NSE's F&O bhavcopy (#306). Pure functions; finresearch.fnohist
feeds them one underlying-day of stored rows.

Model: Black-76 on the forward of each expiry (European options on a forward; Indian index and stock options are
European): C = e^(−rT)[F·N(d1) − K·N(d2)], d1 = [ln(F/K) + σ²T/2]/(σ√T). It equals Black–Scholes with the spot
replaced by F and the dividend yield set to r, so `fincalc.options` prices and inverts it. Using the forward removes
the need for a dividend assumption. The forward of an expiry is, in order:
  1. `future`: the settlement price of the future with the same expiry (monthly expiries have one);
  2. `parity`: put-call parity at the traded strike where |C − P| is smallest, F = K + e^(rT)(C − P);
  3. `spot`: the bhavcopy's underlying value × e^(rT) (UDiFF days only; dividend-free assumption, stated).
Prices are settlement prices of contracts that traded that day (a contract with no trade carries NSE's theoretical
settlement price, which would only return NSE's own model volatility).

T = calendar days from the trade date to expiry / 365 (fincalc.options convention), so the expiry day itself (T = 0)
is never used. The rate is a constant (RATE, the monitor's 6.5 %): Black-76 IV depends on r only through the
discount factor e^(−rT), so ±1 % in r moves a 30-day ATM IV by well under 0.1 vol point.

- ATM IV: mean of the call and put IV at the traded strike nearest the forward (either alone if only one traded).
- Near / next: the first expiry at least MIN_DTE (7) days away (as monitor.iv) and the one after it.
- 30-day constant maturity: linear interpolation of total variance σ²T between the expiries bracketing 30 days;
  flat (the first expiry's IV) when every expiry is beyond 30 days; None when every expiry is shorter.
- Skew: 25-delta put IV − 25-delta call IV of the near expiry (fincalc.volatility.skew_25d with Black-76 deltas).
- Realised volatility: 20-day close-to-close of the front future's settlement price, each return taken within one
  contract (no roll jump), sample standard deviation × √252. Futures cover the whole history (the legacy file has no
  underlying price); the front future tracks the underlying's daily moves up to its small carry.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from finresearch.fincalc.options import Right, bs_price, implied_vol
from finresearch.fincalc.volatility import StrikeIv, skew_25d

RATE = 0.065
MIN_DTE = 7
TARGET_DTE = 30
RV_WINDOW = 20


def black76_price(right: Right, fwd: float, strike: float, t_years: float, rate: float, vol: float) -> float:
    return bs_price(right, fwd, strike, t_years, rate, vol, div_yield=rate)


def black76_iv(
    right: Right, price: float, fwd: float, strike: float, t_years: float, rate: float
) -> float | None:
    """Annual volatility (fraction) reproducing `price` under Black-76; None outside no-arbitrage bounds."""
    if price <= 0 or fwd <= 0 or strike <= 0 or t_years <= 0:
        return None
    return implied_vol(right, price, fwd, strike, t_years, rate, div_yield=rate)


@dataclass(frozen=True)
class OptQuote:
    strike: float
    right: Right
    price: float  # settlement price of a contract that traded


def parity_forward(quotes: Sequence[OptQuote], t_years: float, rate: float) -> float | None:
    """F = K + e^(rT)(C − P) at the strike with both sides traded and the smallest |C − P| (the most ATM)."""
    calls = {q.strike: q.price for q in quotes if q.right == "call"}
    puts = {q.strike: q.price for q in quotes if q.right == "put"}
    both = [k for k in calls if k in puts]
    if not both:
        return None
    k = min(both, key=lambda x: (abs(calls[x] - puts[x]), x))
    return k + math.exp(rate * t_years) * (calls[k] - puts[k])


@dataclass(frozen=True)
class ExpiryIv:
    expiry: date
    dte: int
    forward: float
    forward_source: str  # future / parity / spot
    atm_strike: float
    atm_iv: float  # annual %
    skew_25d: float | None  # vol points


def forward_for(quotes: Sequence[OptQuote], t_years: float, rate: float, future: float | None,
                spot: float | None) -> tuple[float, str] | None:  # fmt: skip
    if future and future > 0:
        return future, "future"
    if (f := parity_forward(quotes, t_years, rate)) is not None and f > 0:
        return f, "parity"
    if spot and spot > 0:
        return spot * math.exp(rate * t_years), "spot"
    return None


def expiry_iv(day: date, expiry: date, quotes: Sequence[OptQuote], *, future: float | None = None,
              spot: float | None = None, rate: float = RATE) -> ExpiryIv | None:  # fmt: skip
    """ATM IV and skew of one expiry from its traded options' settlement prices; None when it cannot be fitted."""
    dte = (expiry - day).days
    if dte <= 0 or not quotes:
        return None
    t = dte / 365
    fw = forward_for(quotes, t, rate, future, spot)
    if fw is None:
        return None
    fwd, source = fw
    by_strike: dict[float, dict[str, float]] = {}
    for q in quotes:
        iv = black76_iv(q.right, q.price, fwd, q.strike, t, rate)
        if iv is not None and 0.005 < iv < 4.99:  # the solver's bounds mean "no solution", not a volatility
            by_strike.setdefault(q.strike, {})[q.right] = iv * 100
    if not by_strike:
        return None
    atm = min(by_strike, key=lambda k: (abs(k - fwd), k))
    ivs = list(by_strike[atm].values())
    rows = [StrikeIv(k, v.get("call"), v.get("put")) for k, v in sorted(by_strike.items())]
    return ExpiryIv(
        expiry, dte, fwd, source, atm, sum(ivs) / len(ivs), skew_25d(rows, fwd, t, rate, div_yield=rate)
    )


def constant_maturity(
    points: Sequence[tuple[int, float]], target: int = TARGET_DTE
) -> tuple[float, str] | None:
    """IV (%) at `target` days from (dte, IV %) points: total variance σ²T linear in T between the bracketing expiries;
    the first expiry's IV when all are longer ("flat"); None when all are shorter."""
    pts = sorted((d, v) for d, v in points if d > 0 and v and v > 0)
    if not pts:
        return None
    if pts[0][0] >= target:
        return (pts[0][1], "interpolated" if pts[0][0] == target else "flat")
    for (d0, v0), (d1, v1) in zip(pts, pts[1:], strict=False):
        if d0 <= target <= d1:
            w0, w1 = v0 * v0 * d0, v1 * v1 * d1
            var = (w0 + (w1 - w0) * (target - d0) / (d1 - d0)) / target
            return (math.sqrt(var), "interpolated") if var > 0 else None
    return None


def front_future_returns(futs: Sequence[tuple[date, date, float]]) -> list[tuple[date, float]]:
    """Daily log returns of the front future from (day, expiry, settle) rows, each within one contract: for day d1 the
    contract is the earliest expiry after the previous day d0 that has a settlement on both days."""
    by_day: dict[date, dict[date, float]] = {}
    for d, e, p in futs:
        if p and p > 0:
            by_day.setdefault(d, {})[e] = p
    days = sorted(by_day)
    out = []
    for d0, d1 in zip(days, days[1:], strict=False):
        common = sorted(e for e in by_day[d1] if e in by_day[d0] and e > d0)
        if common:
            e = common[0]
            out.append((d1, math.log(by_day[d1][e] / by_day[d0][e])))
    return out


def realised_vol_pct(returns: Sequence[float], window: int = RV_WINDOW) -> float | None:
    """Annualised % volatility of the last `window` daily log returns (sample sd × √252); None with fewer."""
    r = list(returns)[-window:]
    if len(r) < window:
        return None
    m = sum(r) / len(r)
    return math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1) * 252) * 100


@dataclass(frozen=True)
class DaySummary:
    near: ExpiryIv | None
    next: ExpiryIv | None
    iv_30d: float | None
    iv_30d_method: str | None
    expiries: list[ExpiryIv]


def day_summary(day: date, options: dict[date, list[OptQuote]], futures: dict[date, float], spot: float | None,
                rate: float = RATE) -> DaySummary:  # fmt: skip
    """One underlying's day: per-expiry ATM IV, near/next term structure and the 30-day constant-maturity IV."""
    fitted = [x for e in sorted(options) if (x := expiry_iv(day, e, options[e], future=futures.get(e), spot=spot,
                                                             rate=rate)) is not None]  # fmt: skip
    later = [x for x in fitted if x.dte >= MIN_DTE]
    near = later[0] if later else None
    nxt = later[1] if len(later) > 1 else None
    cm = constant_maturity([(x.dte, x.atm_iv) for x in fitted])
    return DaySummary(near, nxt, cm[0] if cm else None, cm[1] if cm else None, fitted)
