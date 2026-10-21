"""Factor exposures of the portfolio and of each holding against IIMA's Indian four factors (#348).

Time-series regression of daily excess returns on the market premium, size (SMB), value (HML) and momentum (WML):

    r_t - Rf_t = a + b_mkt·(Rm - Rf)_t + b_smb·SMB_t + b_hml·HML_t + b_wml·WML_t + e_t

with Newey-West standard errors (`factor_math.ols_hac`). The portfolio's series is its realised daily time-weighted
return (`history.History`); a holding's is its unit-adjusted close-to-close return (`history.returns_of`). A return
counts only when it spans exactly one factor-file trading day (its previous day is the factor file's previous trading
day): a fund NAV dated on an exchange holiday or a gap in prices is dropped and counted, never stretched.

The window is the last `WINDOW_RETURNS` matched returns (about three years) and ends where the factor file ends;
IIMA publishes a few releases a year, so the newest months of the portfolio are not covered and the payload says so.
Fewer than `MIN_RETURNS` matched returns give a reason, never a number. Exposures describe the past window, are
estimates with errors, and are not advice. Personal data stays on this machine and never reaches an LLM.
"""

from __future__ import annotations

import bisect
from collections.abc import Mapping
from datetime import date
from itertools import pairwise
from typing import Any

from finresearch.adapters import iima_factors as iima
from finresearch.adapters.iima_factors import FactorSeries
from finresearch.portfolio import factor_math as fm
from finresearch.portfolio.analytics import PRIVACY, metric
from finresearch.portfolio.history import History, returns_of
from finresearch.signals.base import DISCLAIMER

MIN_RETURNS = 120  # as analytics.MIN_BETA: about six months of trading days
WINDOW_RETURNS = 756  # three years of trading days, the usual length for a time-series factor regression
HISTORY_YEARS = 3  # how far back before the factor file's end a holding's own closes are read
FACTORS = (
    ("mkt", "Market (Rm − Rf)", "mf"),
    ("smb", "Size: small minus big (SMB)", "smb"),
    ("hml", "Value: high minus low book-to-market (HML)", "hml"),
    ("wml", "Momentum: winners minus losers (WML)", "wml"),
)
METHOD = ("OLS of daily excess returns (return − Rf) on IIMA's market premium, SMB, HML and WML with an intercept; "
          "Newey-West standard errors (Bartlett kernel, lags floor(4·(n/100)^(2/9)), n/(n−k) correction); 95 % "
          "intervals are ±1.96 standard errors (asymptotic). Alpha is the daily intercept × 252.")  # fmt: skip


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(x, nd)


def _matched(series: Mapping[date, float], prev: Mapping[date, date], fs: FactorSeries,
             fdays: list[date]) -> tuple[list[date], int]:  # fmt: skip
    """Days whose return spans exactly one factor-file trading day, and how many returns could not be matched."""
    keep, dropped = [], 0
    for d in sorted(series):
        i = bisect.bisect_left(fdays, d)
        ok = i < len(fdays) and fdays[i] == d and i > 0 and fdays[i - 1] == prev.get(d) and d in fs.rows
        if ok:
            keep.append(d)
        elif d <= fdays[-1]:
            dropped += 1
    return keep, dropped


def regress(series: Mapping[date, float], prev: Mapping[date, date], fs: FactorSeries) -> dict[str, Any]:
    """One series' exposures (or the reason there are none)."""
    fdays = sorted([*fs.rows, *fs.skipped])
    keep, dropped = _matched(series, prev, fs, fdays)
    keep = keep[-WINDOW_RETURNS:]
    out: dict[str, Any] = {"available": False, "reason": None, "exposures": [], "alpha_annual": None,
                           "r2": None, "adj_r2": None, "lags": None,
                           "window": {"start": keep[0].isoformat() if keep else None,
                                      "end": keep[-1].isoformat() if keep else None,
                                      "returns": len(keep), "dropped": dropped}}  # fmt: skip
    if len(keep) < MIN_RETURNS:
        out["reason"] = f"needs {MIN_RETURNS} daily returns that overlap the factor data; have {len(keep)}"
        return out
    y = [series[d] - fs.rows[d].rf for d in keep]
    X = [[getattr(fs.rows[d], attr) for _, _, attr in FACTORS] for d in keep]
    try:
        res = fm.ols_hac(y, X)
    except ValueError as e:
        out["reason"] = str(e)
        return out
    for j, (key, label, _) in enumerate(FACTORS, start=1):
        b, se = res.coef[j], res.se[j]
        out["exposures"].append({"factor": key, "label": label, "beta": _r(b), "se": _r(se),
                                 "t": _r(res.t[j], 2), "ci95": [_r(b - fm.Z95 * se), _r(b + fm.Z95 * se)]})  # fmt: skip
    out["alpha_annual"] = {**metric(res.coef[0] * 252, "fraction", n=res.n, nd=6, how="daily intercept × 252"),
                           "se": _r(res.se[0] * 252, 6)}  # fmt: skip
    out.update(available=True, r2=_r(res.r2), adj_r2=_r(res.adj_r2), lags=res.lags)
    return out


def _portfolio_series(h: History) -> tuple[dict[date, float], dict[date, date]]:
    ser, prev = {}, {}
    for t in range(1, len(h.days)):
        r = h.returns[t] if t < len(h.returns) else None
        if r is not None:
            ser[h.days[t]], prev[h.days[t]] = r, h.days[t - 1]
    return ser, prev


def _holding_series(closes: Mapping[date, float], factors: Mapping[date, float] | None
                    ) -> tuple[dict[date, float], dict[date, date]]:  # fmt: skip
    ks = sorted(closes)
    before = {b: a for a, b in pairwise(ks)}
    ser = returns_of(dict(closes), dict(factors or {}))
    return ser, {d: before[d] for d in ser}


def source(fs: FactorSeries) -> dict[str, Any]:
    return {"name": "IIMA Indian Fama-French-Momentum data library (survivorship-bias adjusted, daily)",
            "citation": iima.CITATION, "terms": iima.TERMS, "url": fs.url, "page": iima.BASE,
            "release": fs.release, "first": fs.first.isoformat(), "last": fs.last.isoformat(),
            "fetched_at": fs.fetched_at.isoformat() if fs.fetched_at else None,
            "skipped_days": len(fs.skipped)}  # fmt: skip


def exposures(h: History, fs: FactorSeries | None, holding_closes: Mapping[str, Mapping[date, float]], *,
              source_error: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The payload: portfolio exposures, one row per current holding, the factor source and the coverage note.
    `holding_closes`: instrument key -> closes over the factor window (absent or empty: unknown)."""
    out: dict[str, Any] = {"disclaimer": DISCLAIMER, "privacy": PRIVACY, "method": METHOD,
                           "min_returns": MIN_RETURNS, "window_returns": WINDOW_RETURNS,
                           "as_of": h.days[-1].isoformat() if h.days else None, "portfolio": None, "holdings": [],
                           "source": None, "coverage_note": None, "available": False, "reason": None}  # fmt: skip
    if fs is None:
        out["reason"] = f"IIMA factor data could not be read: {source_error or 'unknown error'}"
        return out
    out["source"] = source(fs)
    if h.days and h.days[-1] > fs.last:
        out["coverage_note"] = (f"IIMA's release {fs.release} ends on {fs.last.isoformat()}; your history runs to "
                                f"{h.days[-1].isoformat()}, so the days after {fs.last.isoformat()} are not in "
                                "these regressions until the next release.")  # fmt: skip
    if not h.ok:
        out["reason"] = h.reason or "no value history yet"
        return out
    out["available"] = True
    out["portfolio"] = regress(*_portfolio_series(h), fs)
    pos = [p for p in h.positions if p.value > 0]
    total = sum(p.value for p in pos)
    for p in pos:
        row = {
            "key": p.key,
            "name": p.name,
            "asset_type": p.asset_type,
            "weight": _r(p.value / total) if total else None,
        }
        closes = holding_closes.get(p.key) or {}
        if len(closes) < 2:
            row.update(available=False, reason="no price history could be read", exposures=[])
        else:
            row.update(regress(*_holding_series(closes, h.factors.get(p.key)), fs))
        out["holdings"].append(row)
    return out


async def load(h: History, fs: FactorSeries, fetch: Any, store: Any, today: date) -> dict[str, Any]:
    """`exposures` with each current holding's closes read for the `HISTORY_YEARS` before the factor file's end
    (through the on-disk PriceStore, so a release's window is fetched once). A failed read is that holding's reason,
    never a 0 exposure."""
    from datetime import timedelta

    from finresearch.fincalc.dates import add_years
    from finresearch.portfolio.history import Instrument

    end = min(fs.last, today)
    start = add_years(end, -HISTORY_YEARS) - timedelta(days=10)
    closes: dict[str, dict[date, float]] = {}
    for p in h.positions:
        if p.value <= 0:
            continue
        kind, ident = p.key.split(":", 1)
        inst = Instrument(p.key, "mf" if kind == "MF" else kind.lower(), ident, p.scheme)
        try:
            closes[p.key] = await store.get(p.key, start, end, lambda a, b, i=inst: fetch(i, a, b), today)
        except Exception:
            closes[p.key] = {}
    return exposures(h, fs, closes)
