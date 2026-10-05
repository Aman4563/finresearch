"""Portfolio analytics payloads: performance vs a benchmark, risk, concentration and costs (research note §1.4, §1.7,
§1.8, §1.17). Deterministic Python over `history.History`; the arithmetic is in `analytics_math`.

Presentation rules (roadmap §D.6/§D.8 and the research note §1.21): every number carries its method and "as of";
insufficient data returns a reason instead of a number; thresholds are labelled as rules of thumb; wording is "your
rule fired / within your limits: no action needed", never "you should"; costs (tax, exit load, stamp duty) come
before any saving; and every payload carries the personal, non-advisory disclaimer. Personal data never leaves this
machine and is never sent to an LLM.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from finresearch.portfolio import analytics_math as am
from finresearch.portfolio.history import (
    BENCHMARK,
    History,
    Instrument,
    PriceStore,
    parse_benchmark_actions,
    returns_of,
)
from finresearch.portfolio.limits import PositionLimit, position_limit
from finresearch.signals.base import DISCLAIMER

MIN_VOL, WARN_VOL = 60, 250  # trading days of returns
MIN_BETA = 120
MIN_VAR = 250
MIN_SHARPE = 250
MIN_RC = 120
MIN_XIRR_DAYS = 60  # as valuation.MIN_XIRR_DAYS: annualising a few weeks misleads
MIN_COMPARE_DAYS = 365  # beat/lag wording only after a year, and even then it is noise under ~3-5 years [K]
MAX_POINTS = 1200  # chart rows sent to the page (every k-th day beyond that; the last day is always kept)

# rules of thumb [W], labelled as such in the UI. The single-stock limit is portfolio.limits.position_limit (the
# profile's max position, else 5 / 8 / 10 % by risk appetite), the same one the stock signal and pre-trade use (#194)
SECTOR_LIMIT_PCT = 25.0
GROUP_LIMIT_PCT = 20.0
TRIVIAL_VALUE = 5000.0  # a regular→direct row below this value, or ...
TRIVIAL_SAVING = 250.0  # ... saving less than this a year, is suppressed (and counted)
GROSS_RETURN_ASSUMPTION = 0.10  # for the 10-year cost illustration only; not a forecast [K]

PRIVACY = "Computed on this machine from your local database; never sent to an LLM."


def _r(x: float | None, nd: int = 2) -> float | None:
    return None if x is None or not math.isfinite(x) else round(x, nd)


def metric(value: float | None, unit: str, *, reason: str | None = None, n: int | None = None,
           note: str | None = None, how: str | None = None, nd: int = 4) -> dict[str, Any]:  # fmt: skip
    return {"value": _r(value, nd) if reason is None else None, "unit": unit, "reason": reason, "n": n,
            "note": note, "how": how}  # fmt: skip


def _thin(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(rows) <= MAX_POINTS:
        return rows
    k = math.ceil(len(rows) / MAX_POINTS)
    out = rows[::k]
    if out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def base(h: History) -> dict[str, Any]:
    return {"disclaimer": DISCLAIMER, "privacy": PRIVACY, "excluded": h.excluded, "warnings": h.warnings,
            "notes": h.notes, "start_reason": h.start_reason, "reason": h.reason,
            "as_of": h.days[-1].isoformat() if h.days else None,
            "start": h.days[0].isoformat() if h.days else None}  # fmt: skip


# --------------------------------------------------------------------------- performance
def performance(h: History) -> dict[str, Any]:
    out = base(h)
    if not h.ok:
        out.update(available=False, series=[], summary=None)
        return out
    days, idx, end = h.days, h.index, h.days[-1]
    span = (end - days[0]).days
    v_end = h.value[-1]
    twr = idx[-1] / idx[0] - 1
    ann = (1 + twr) ** (365 / span) - 1 if span >= 365 else None
    xirr, xirr_reason = None, None
    if span < MIN_XIRR_DAYS:
        xirr_reason = (
            f"the series covers {span} days: under {MIN_XIRR_DAYS} days an annualised return misleads"
        )
    else:
        try:
            xirr = am.investor_xirr(h.flows, end, v_end)
        except ValueError as e:
            xirr_reason = str(e)
    summary: dict[str, Any] = {
        "days": len(days), "calendar_days": span, "value": _r(v_end), "invested": _r(h.invested[-1]),
        "gain": _r(v_end - h.invested[-1]), "initial": _r(h.initial),
        "twr": metric(twr, "fraction", how="chain of daily (V_t - F_t) / V_(t-1) - 1: flows removed"),
        "twr_annualised": metric(ann, "fraction", reason=None if ann is not None else "under a year: not annualised"),
        "xirr": metric(xirr, "fraction", reason=xirr_reason,
                       how="money-weighted: every flow in the window, the first day's holdings as money put in, "
                           "today's value as money back"),
    }  # fmt: skip
    b = h.benchmark
    rows = []
    die_series: list[float | None] = [None] * len(days)
    if b is not None:
        level = dict(zip(b.days, b.level, strict=True))
        eq = am.direct_index_equivalent(h.flows, level, end, v_end)
        units, fi = 0.0, 0
        flows = sorted(h.flows)
        for t, d in enumerate(days):
            while fi < len(flows) and flows[fi][0] <= d:
                units = max(0.0, units + flows[fi][1] / level[flows[fi][0]])
                fi += 1
            die_series[t] = units * level[d]
        b_twr = b.level[-1] / b.level[0] - 1
        die_xirr, die_reason = None, xirr_reason
        if xirr_reason is None:
            try:
                die_xirr = am.investor_xirr(h.flows, end, eq.value)
            except ValueError as e:
                die_reason = str(e)
        verdict = None
        if span >= MIN_COMPARE_DAYS:
            gap = v_end - eq.value
            verdict = (f"Over {span / 365:.1f} years your portfolio is ₹{abs(gap):,.0f} "
                       f"{'ahead of' if gap >= 0 else 'behind'} the same cash flows put into {BENCHMARK}. Under about "
                       "3-5 years such a gap is mostly noise, not evidence of skill or its absence.")  # fmt: skip
        summary["benchmark"] = {
            "label": b.label, "note": b.note, "symbol": BENCHMARK,
            "twr": metric(b_twr, "fraction", how="NIFTYBEES closes with distributions reinvested on ex-dates"),
            "twr_gap_pp": _r((twr - b_twr) * 100, 2),
            "die_value": _r(eq.value), "die_xirr": metric(die_xirr, "fraction", reason=die_reason),
            "alpha_inr": _r(v_end - eq.value), "pme": metric(eq.pme, "ratio", reason=None if eq.pme else "no inflows"),
            "clamped": eq.clamped, "dividends": sum(1 for d in b.dividends if days[0] < d <= end), "splits": sum(1 for d in b.factors if days[0] < d <= end),
            "verdict": verdict,
            "early": span < MIN_COMPARE_DAYS,
        }  # fmt: skip
    else:
        summary["benchmark"] = None
        out["warnings"] = [
            *out["warnings"],
            f"{BENCHMARK} history could not be read: no benchmark comparison",
        ]
    b0 = b.level[0] if b is not None else None
    for t, d in enumerate(days):
        rows.append({"date": d.isoformat(), "portfolio": _r(idx[t] * 100, 3),
                     "benchmark": _r(b.level[t] / b0 * 100, 3) if b is not None and b0 else None,
                     "value": _r(h.value[t]), "invested": _r(h.invested[t]), "die_value": _r(die_series[t])})  # fmt: skip
    out.update(available=True, series=_thin(rows), summary=summary)
    return out


# --------------------------------------------------------------------------- risk
@dataclass
class RiskFree:
    rate: float  # annual, effective
    label: str
    as_of: str | None
    source: str | None
    fallback: bool = False
    user_set: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"rate": round(self.rate, 6), "label": self.label, "as_of": self.as_of, "source": self.source,
                "fallback": self.fallback, "user_set": self.user_set}  # fmt: skip


def risk_free_from_curve(curve: Any) -> RiskFree:
    """FBIL's 3-month point of the G-sec par curve (half-yearly compounded) as an effective annual rate. A proxy for
    the 91-day T-bill: FBIL's T-bill benchmark is not read by the app."""
    y = float(curve.par_yield(0.25))
    eff = (1 + y / 2) ** 2 - 1
    label = (f"FBIL G-sec par yield at 3 months, {y * 100:.2f} % half-yearly = {eff * 100:.2f} % a year "
             "(a stand-in for the 91-day T-bill)")  # fmt: skip
    return RiskFree(
        eff, label, curve.as_of.isoformat(), curve.source, bool(getattr(curve, "fallback", False))
    )


def _realised(h: History) -> tuple[list[date], list[float], list[float], list[float] | None]:
    w = am.defined_tail(h.returns)
    days = h.days[w:]
    idx = h.index[w:]
    r = [x for x in h.returns[w + 1 :] if x is not None]
    rb = None
    if h.benchmark is not None:
        lv = h.benchmark.level[w:]
        rb = [lv[t] / lv[t - 1] - 1 for t in range(1, len(lv))]
    return days, idx, r, rb


def _position_returns(
    h: History, lookback_days: int = 365
) -> tuple[list[Any], list[date], list[list[float]]]:
    """Positions with a price series and their aligned daily returns over the last `lookback_days` (calendar)."""
    if not h.positions:
        return [], [], []
    end = h.days[-1] if h.days else max(max(c) for c in h.closes.values())
    since = end - timedelta(days=lookback_days)
    series = {}
    for p in h.positions:
        c = {d: v for d, v in h.closes.get(p.key, {}).items() if d >= since - timedelta(days=10)}
        rets = {d: r for d, r in returns_of(c, h.factors.get(p.key)).items() if d > since}
        if rets:
            series[p.key] = rets
    keep = [p for p in h.positions if p.key in series and p.value > 0]
    if not keep:
        return [], [], []
    common = sorted(set.intersection(*(set(series[p.key]) for p in keep)))
    mat = [[series[p.key][d] for p in keep] for d in common]
    return keep, common, mat


def risk(
    h: History, rf: RiskFree, sectors: dict[str, str], stress: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    out = base(h)
    out["risk_free"] = rf.to_json()
    if not h.ok:
        out.update(available=False, realised=None, hypothetical=None, stress=stress or [])
        return out
    days, idx, r, rb = _realised(h)
    n = len(r)
    v_end = h.value[-1]
    real: dict[str, Any] = {
        "window": {"start": days[0].isoformat(), "end": days[-1].isoformat(), "returns": n}
    }
    # volatility
    if n < MIN_VOL:
        real["volatility"] = metric(None, "fraction", reason=f"needs {MIN_VOL} daily returns; have {n}", n=n)
    else:
        real["volatility"] = metric(am.annualised_vol(r), "fraction", n=n, how="stdev(daily TWR)·√252",
                                    note=f"under {WARN_VOL} days: a rough estimate" if n < WARN_VOL else None)  # fmt: skip
    # beta and tracking error
    if rb is None:
        real["beta"] = metric(None, "ratio", reason=f"{BENCHMARK} history unavailable", n=n)
        real["tracking_error"] = metric(None, "fraction", reason=f"{BENCHMARK} history unavailable", n=n)
    elif n < MIN_BETA:
        why = f"needs {MIN_BETA} daily returns; have {n}"
        real["beta"] = metric(None, "ratio", reason=why, n=n)
        real["tracking_error"] = metric(None, "fraction", reason=why, n=n)
    else:
        real["beta"] = metric(
            am.beta(r, rb), "ratio", n=n, how=f"cov(portfolio, {BENCHMARK}) / var({BENCHMARK})"
        )
        real["tracking_error"] = metric(am.tracking_error(r, rb), "fraction", n=n,
                                        how=f"stdev(portfolio - {BENCHMARK} daily returns)·√252")  # fmt: skip
    # drawdown and recovery
    dd = am.drawdown_recovery(idx)
    real["max_drawdown"] = {
        **metric(dd.depth, "fraction", n=n, how="largest fall of the TWR index from a running peak"),
        "peak": days[dd.peak].isoformat(), "trough": days[dd.trough].isoformat(),
        "recovered": days[dd.recovered].isoformat() if dd.recovered is not None else None,
        "days_to_recover": (days[dd.recovered] - days[dd.trough]).days if dd.recovered is not None else None,
        "underwater_days": ((days[dd.recovered] if dd.recovered is not None else days[-1]) - days[dd.peak]).days
        if dd.depth < 0 else 0,
        "current": _r(dd.current, 4),
    }  # fmt: skip
    # historical VaR / CVaR
    if n < MIN_VAR:
        why = f"needs {MIN_VAR} daily returns (about a year); have {n}"
        for k in ("var_1d", "cvar_1d", "var_21d", "cvar_21d"):
            real[k] = metric(None, "fraction", reason=why, n=n)
    else:
        v1, c1 = am.historical_var(r)
        hr = am.horizon_returns(idx)
        v21, c21 = am.historical_var(hr)
        tail = sum(1 for x in r if x <= -v1)
        real["var_1d"] = {**metric(v1, "fraction", n=n, how="-5th percentile of daily returns (historical)"),
                          "inr": _r(v1 * v_end, 0),
                          "note": f"about 1 day in 20 lost more; {tail} of {n} days in this window did"}  # fmt: skip
        real["cvar_1d"] = {**metric(c1, "fraction", n=n, how="average loss on the worst 5 % of days"),
                           "inr": _r(c1 * v_end, 0)}  # fmt: skip
        real["var_21d"] = {**metric(v21, "fraction", n=len(hr), how="-5th percentile of overlapping 21-day returns"),
                           "inr": _r(v21 * v_end, 0),
                           "note": f"{len(hr)} overlapping windows ≈ {len(hr) / 21:.0f} independent months: thin"}  # fmt: skip
        real["cvar_21d"] = {**metric(c21, "fraction", n=len(hr), how="average of the worst 5 % of 21-day returns"),
                            "inr": _r(c21 * v_end, 0)}  # fmt: skip
    # Sharpe / Sortino
    if n < MIN_SHARPE:
        why = f"needs {MIN_SHARPE} daily returns (about a year); have {n}"
        real["sharpe"] = metric(None, "ratio", reason=why, n=n)
        real["sortino"] = metric(None, "ratio", reason=why, n=n)
    else:
        s, so = am.sharpe_sortino(idx, days, rf.rate)
        real["sharpe"] = metric(s, "ratio", n=n, how="(mean daily return·252 - risk-free) / volatility")
        real["sortino"] = metric(so, "ratio", n=n, reason=None if so is not None else "no day fell below the risk-free rate",
                                 how="(mean daily return - risk-free/252)·252 / (downside deviation·√252)")  # fmt: skip
    # hypothetical: today's weights over the last 12 months of prices
    keep, common, mat = _position_returns(h)
    hypo: dict[str, Any] = {"note": "Today's holdings and weights applied to their last 12 months of prices: what "
                                    "the current mix would have done, not your realised history."}  # fmt: skip
    if len(common) < MIN_RC:
        hypo["reason"] = (
            f"needs {MIN_RC} common trading days of prices for today's holdings; have {len(common)}"
        )
        hypo["positions"], hypo["sectors"] = [], []
    else:
        tot = sum(p.value for p in keep)
        w = [p.value / tot for p in keep]
        rc, vol = am.risk_contributions(w, mat)
        port = [sum(wi * x for wi, x in zip(w, row, strict=True)) for row in mat]
        v1, c1 = am.historical_var(port)
        hypo.update(days=len(common), from_=common[0].isoformat(), to=common[-1].isoformat(),
                    volatility=metric(vol, "fraction", n=len(common), how="√(wᵀΣw·252), Σ = sample covariance"),
                    var_1d={**metric(v1, "fraction", n=len(common)), "inr": _r(v1 * tot, 0)},
                    cvar_1d={**metric(c1, "fraction", n=len(common)), "inr": _r(c1 * tot, 0)})  # fmt: skip
        hypo["positions"] = sorted(
            [{"key": p.key, "name": p.name, "weight": _r(wi, 4), "risk_share": _r(c, 4),
              "sector": sectors.get(p.key) or _default_sector(p)}
             for p, wi, c in zip(keep, w, rc, strict=True)], key=lambda x: -(x["risk_share"] or 0))  # fmt: skip
        by: dict[str, list[float]] = {}
        for x in hypo["positions"]:
            a = by.setdefault(x["sector"], [0.0, 0.0])
            a[0] += x["weight"] or 0
            a[1] += x["risk_share"] or 0
        hypo["sectors"] = sorted([{"sector": k, "weight": _r(v[0], 4), "risk_share": _r(v[1], 4)} for k, v in by.items()],
                                 key=lambda x: -(x["risk_share"] or 0))  # fmt: skip
        hypo["how"] = (
            "RC_i = w_i·(Σw)_i / wᵀΣw: each holding's share of the portfolio's variance (adds up to 100 %)"
        )
        left = [p.name for p in h.positions if p not in keep]
        if left:
            hypo["missing"] = left
    out.update(available=True, realised=real, hypothetical=hypo, stress=stress or [], value=_r(v_end))
    return out


def _default_sector(p: Any) -> str:
    if p.asset_type == "mf":
        return "Funds (not looked through)"
    return p.sector or "Unclassified"


# --------------------------------------------------------------------------- stress scenarios
@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    window: tuple[date, date]
    per_holding: bool
    note: str


SCENARIOS = (
    Scenario("covid2020", "COVID-19 fall, 2020", (date(2020, 1, 1), date(2020, 4, 30)), True,
             "Peak and trough found in NIFTYBEES closes inside Jan-Apr 2020; each holding's own move between those "
             "two days where it has prices."),
    Scenario("gfc2008", "Global financial crisis, 2008-09", (date(2008, 1, 1), date(2009, 3, 31)), False,
             "Per-holding prices for 2008 are not fetched (most of today's holdings have no price series that far "
             "back, and many did not exist), so equity holdings are shown at NIFTYBEES's own fall and other holdings "
             "at 0 %. Debt and gold did not behave like that in 2008: read it as an equity-only shock."),
    Scenario("corr2024", "2024-25 correction", (date(2024, 9, 1), date(2025, 3, 31)), True,
             "Peak and trough found in NIFTYBEES closes inside Sep-2024 to Mar-2025; small and mid caps fell more "
             "than the Nifty 50 in this episode, which each holding's own move captures."),
)  # fmt: skip
HYPOTHETICAL_FALL = -0.20


def _on_or_before(closes: dict[date, float], d: date, within: int = 7) -> float | None:
    ks = [k for k in closes if d - timedelta(days=within) <= k <= d]
    return closes[max(ks)] if ks else None


def scenario_result(sc: Scenario, bench_closes: dict[date, float], divs: dict[date, float],
                    factors: dict[date, float], positions: Sequence[Any],
                    own: dict[str, dict[date, float]]) -> dict[str, Any]:  # fmt: skip
    """One stored episode applied to today's positions (instructive, not a probability)."""
    lo, hi = sc.window
    bd = sorted(d for d in bench_closes if lo <= d <= hi)
    base_out = {"id": sc.id, "label": sc.label, "window": [lo.isoformat(), hi.isoformat()], "note": sc.note}
    if len(bd) < 10:
        return {**base_out, "available": False, "reason": f"{BENCHMARK} has no prices for this window"}
    tr = am.total_return_index(bd, [bench_closes[d] for d in bd], divs, factors)
    from finresearch.fincalc.market import max_drawdown

    dd = max_drawdown(tr)
    peak, trough = bd[dd.peak_index], bd[dd.trough_index]
    bmove = float(dd.max_drawdown)
    rows, total, loss = [], 0.0, 0.0
    for p in positions:
        total += p.value
        r, how = None, None
        c = own.get(p.key) or {}
        if sc.per_holding and c:
            a, b = _on_or_before(c, peak), _on_or_before(c, trough)
            if a and b:
                r, how = b / a - 1, "own prices"
        if r is None:
            if p.tax_class == "equity":
                r, how = bmove, f"proxy: {BENCHMARK} move"
            else:
                r, how = 0.0, "not modelled (0 %)"
        loss += p.value * r
        rows.append({"key": p.key, "name": p.name, "move": _r(r, 4), "inr": _r(p.value * r, 0), "how": how})
    return {**base_out, "available": True, "peak": peak.isoformat(), "trough": trough.isoformat(),
            "benchmark_move": _r(bmove, 4), "portfolio_move": _r(loss / total, 4) if total else None,
            "inr": _r(loss, 0), "positions": sorted(rows, key=lambda x: x["inr"] or 0)}  # fmt: skip


def hypothetical_fall(h: History, fall: float = HYPOTHETICAL_FALL) -> dict[str, Any]:
    """Nifty 50 falls `fall`: each holding moves by its own 1-year beta to NIFTYBEES × the fall."""
    out = {"id": "beta20", "label": f"Nifty 50 falls {abs(fall):.0%} (hypothetical)",
           "note": "Each holding moves by its own 1-year beta to NIFTYBEES times the fall. Betas tend to rise in "
                   "crashes, so this can understate a real fall [K]."}  # fmt: skip
    b = h.benchmark
    if b is None or not h.positions:
        return {**out, "available": False, "reason": f"{BENCHMARK} history unavailable"}
    since = h.days[-1] - timedelta(days=365)
    bret = {d: r for d, r in returns_of(b.closes, b.factors).items() if d > since}
    rows, total, loss = [], 0.0, 0.0
    for p in h.positions:
        pr = {d: r for d, r in returns_of(h.closes.get(p.key, {}), h.factors.get(p.key)).items() if d > since}
        common = sorted(set(pr) & set(bret))
        total += p.value
        if len(common) >= MIN_BETA:
            beta = am.beta([pr[d] for d in common], [bret[d] for d in common])
            how = f"beta {beta:.2f} ({len(common)} days)"
        else:
            beta, how = (
                (1.0, "beta 1 assumed (short history)")
                if p.tax_class == "equity"
                else (0.0, "not modelled (0 %)")
            )
        r = beta * fall
        loss += p.value * r
        rows.append({"key": p.key, "name": p.name, "move": _r(r, 4), "inr": _r(p.value * r, 0), "how": how})
    return {**out, "available": True, "benchmark_move": fall, "portfolio_move": _r(loss / total, 4) if total else None,
            "inr": _r(loss, 0), "positions": sorted(rows, key=lambda x: x["inr"] or 0)}  # fmt: skip


async def stress_scenarios(h: History, fetch: Any, store: PriceStore, today: date) -> list[dict[str, Any]]:
    """The stored episodes plus the beta-scaled hypothetical. Per-holding windows are fetched once and cached for
    good (past prices do not change)."""
    if not h.ok or not h.positions:
        return []
    from finresearch.portfolio.history import _cached_actions

    try:
        divs, factors = parse_benchmark_actions(await _cached_actions(fetch, store, today))
    except Exception:
        divs, factors = {}, {}
    out = []
    for sc in SCENARIOS:
        lo, hi = sc.window
        binst = Instrument(f"NSE:{BENCHMARK}", "nse", BENCHMARK)
        try:
            bc = await store.get(binst.key, lo, hi, lambda a, b, i=binst: fetch(i, a, b), today)
        except Exception as e:
            out.append({"id": sc.id, "label": sc.label, "available": False, "reason": f"{BENCHMARK} prices unavailable ({type(e).__name__})",
                        "window": [lo.isoformat(), hi.isoformat()], "note": sc.note})  # fmt: skip
            continue
        own: dict[str, dict[date, float]] = {}
        if sc.per_holding:
            for p in h.positions:
                kind, ident = p.key.split(":", 1)
                inst = Instrument(p.key, "mf" if kind == "MF" else kind.lower(), ident, p.scheme)
                try:
                    own[p.key] = await store.get(p.key, lo, hi, lambda a, b, i=inst: fetch(i, a, b), today)
                except Exception:
                    own[p.key] = {}
        out.append(scenario_result(sc, bc, divs, factors, h.positions, own))
    out.append(hypothetical_fall(h))
    return out


# --------------------------------------------------------------------------- concentration
# Business groups: there is no free machine-readable source [K]. A small starting map from company names and NSE
# symbols, marked [unverified]; the user can correct or extend it (Concentration tab), and the correction wins.
GROUP_SYMBOLS: dict[str, tuple[str, ...]] = {
    "Tata": ("TCS", "TITAN", "TRENT", "VOLTAS", "INDHOTEL", "TATAMOTORS", "TATASTEEL", "TATAPOWER", "TATACONSUM",
             "TATACOMM", "TATAELXSI", "TATACHEM", "TATAINVEST", "TATATECH", "RALLIS", "NELCO", "TMPV", "TMCV"),
    "Adani": ("ADANIENT", "ADANIPORTS", "ADANIPOWER", "ADANIGREEN", "ADANIENSOL", "ATGL", "AWL", "AMBUJACEM", "ACC",
              "NDTV"),
    "Reliance (Mukesh Ambani)": ("RELIANCE", "JIOFIN", "NETWORK18", "TV18BRDCST"),
    "Bajaj": ("BAJFINANCE", "BAJAJFINSV", "BAJAJ-AUTO", "BAJAJHLDNG", "BAJAJELEC", "BAJAJHFL"),
    "Aditya Birla": ("GRASIM", "HINDALCO", "ULTRACEMCO", "ABCAPITAL", "ABFRL", "IDEA", "ABSLAMC"),
    "Mahindra": ("M&M", "TECHM", "M&MFIN", "MAHLIFE", "MHRIL"),
    "HDFC": ("HDFCBANK", "HDFCLIFE", "HDFCAMC"),
    "ICICI": ("ICICIBANK", "ICICIPRULI", "ICICIGI"),
    "Murugappa": ("CHOLAFIN", "CHOLAHLDNG", "TIINDIA", "CARBORUNIV", "COROMANDEL", "EIDPARRY"),
    "JSW": ("JSWSTEEL", "JSWENERGY", "JSWINFRA"),
    "Godrej": ("GODREJCP", "GODREJPROP", "GODREJIND", "GODREJAGRO"),
    "Vedanta": ("VEDL", "HINDZINC"),
}  # fmt: skip
GROUP_NAME_RE = [(g, re.compile(rf"^{re.escape(g.split(' (')[0])}\b", re.I)) for g in
                 ("Tata", "Adani", "Bajaj", "Aditya Birla", "Mahindra", "Godrej", "JSW", "Jindal")]  # fmt: skip
GROUP_SOURCE = ("Starting map from company names and NSE symbols [unverified: no official machine-readable list of "
                "business groups exists]; your corrections override it.")  # fmt: skip


def group_of(p: Any, overrides: dict[str, str | None]) -> tuple[str | None, str]:
    """(group, where it came from)."""
    if p.key in overrides:
        return overrides[p.key] or None, "your map"
    if p.asset_type != "stock":
        return None, ""
    sym = (p.nse_symbol or p.key.split(":", 1)[-1]).upper()
    for g, syms in GROUP_SYMBOLS.items():
        if sym in syms:
            return g, "starting map [unverified]"
    for g, rx in GROUP_NAME_RE:
        if rx.search(p.name or ""):
            return g, "name match [unverified]"
    return None, ""


def concentration(h: History, sectors: dict[str, str], overrides: dict[str, str | None],
                  limit: PositionLimit | None = None) -> dict[str, Any]:  # fmt: skip
    out = base(h)
    pos = [p for p in h.positions if p.value > 0]
    if not pos:
        out.update(available=False, reason=out["reason"] or "no priced holdings today")
        return out
    total = sum(p.value for p in pos)
    lim = limit or position_limit(None)
    stock_limit, stock_src = lim.pct, lim.rule
    rows = []
    for p in pos:
        g, gsrc = group_of(p, overrides)
        rows.append({"key": p.key, "name": p.name, "asset_type": p.asset_type, "value": _r(p.value),
                     "weight_pct": _r(p.value / total * 100, 2), "sector": sectors.get(p.key) or _default_sector(p),
                     "group": g, "group_source": gsrc, "accounts": len(p.holding_ids)})  # fmt: skip
    w = [p.value for p in pos]

    def agg(field: str, only_stocks: bool) -> list[dict[str, Any]]:
        by: dict[str, float] = {}
        for x in rows:
            if only_stocks and x["asset_type"] != "stock":
                continue
            k = x[field] or ("No group mapped" if field == "group" else "Unclassified")
            by[k] = by.get(k, 0.0) + (x["value"] or 0)
        return [{"label": k, "value": _r(v), "weight_pct": _r(v / total * 100, 2)}
                for k, v in sorted(by.items(), key=lambda kv: -kv[1])]  # fmt: skip

    sectors_rows = agg("sector", False)
    groups_rows = agg("group", True)
    flags = []
    for x in rows:
        if x["asset_type"] == "stock" and (x["weight_pct"] or 0) > stock_limit:
            flags.append({"kind": "stock", "label": x["name"], "weight_pct": x["weight_pct"], "limit_pct": stock_limit,
                          "text": f"Your limit ({stock_src}) is exceeded: {x['name']} is "
                                  f"{x['weight_pct']:.1f} % of the portfolio. Consider reviewing it."})  # fmt: skip
    for s in sectors_rows:
        if s["label"].startswith("Funds") or s["label"] == "Unclassified":
            continue
        if (s["weight_pct"] or 0) > SECTOR_LIMIT_PCT:
            flags.append({"kind": "sector", "label": s["label"], "weight_pct": s["weight_pct"], "limit_pct": SECTOR_LIMIT_PCT,
                          "text": f"{s['label']} is {s['weight_pct']:.1f} % of the portfolio, above the "
                                  f"{SECTOR_LIMIT_PCT:g} % rule of thumb [W]. Consider reviewing it."})  # fmt: skip
    for g in groups_rows:
        if g["label"] == "No group mapped":
            continue
        if (g["weight_pct"] or 0) > GROUP_LIMIT_PCT:
            flags.append({"kind": "group", "label": g["label"], "weight_pct": g["weight_pct"], "limit_pct": GROUP_LIMIT_PCT,
                          "text": f"The {g['label']} group is {g['weight_pct']:.1f} % of the portfolio, above the "
                                  f"{GROUP_LIMIT_PCT:g} % rule of thumb [W]. Consider reviewing it."})  # fmt: skip
    funds = sum(1 for p in pos if p.asset_type == "mf")
    out.update(
        available=True, total=_r(total), positions=sorted(rows, key=lambda x: -(x["value"] or 0)),
        sectors=sectors_rows, groups=groups_rows, flags=flags,
        status="Within your limits: no action needed." if not flags else f"{len(flags)} limit(s) exceeded.",
        hhi=_r(am.hhi(w), 4), n_effective=_r(am.n_effective(w), 2), top5_pct=_r(am.top_share(w, 5) * 100, 2),
        limits={"stock_pct": stock_limit, "stock_source": stock_src, "stock_rule": lim.to_json(), "sector_pct": SECTOR_LIMIT_PCT,
                "group_pct": GROUP_LIMIT_PCT},
        group_source=GROUP_SOURCE,
        funds_note=(f"{funds} fund(s) count as single positions: their own holdings are not looked through here, so "
                    "stock and sector exposure through funds is not included and N_eff understates diversification."
                    if funds else None),
        valued_at="last daily close or NAV in the price history",
    )  # fmt: skip
    return out


# --------------------------------------------------------------------------- costs
def is_direct(scheme: Any) -> bool:
    return "direct" in f"{scheme.plan or ''} {scheme.name}".lower()


def ter_row(table: dict[str, Any], scheme: Any) -> Any:
    from finresearch.adapters.amfi import ter_key

    for name in (scheme.name, scheme.name.split(" - ")[0]):
        hit = table.get(ter_key(name))
        if hit is not None:
            return hit
    return None


@dataclass
class FundLots:
    """What the switch analysis needs about one fund holding: its tax identity and open lots."""

    holding_id: int
    holding_tax: Any  # portfolio.tax.HoldingTax
    lots: list[tuple[date | None, Decimal, Decimal | None]]  # (acquired, open units, cost per unit)


def switch_cost(fl: Sequence[FundLots], nav: float, today: date, base_gains: Sequence[Any], slab: Decimal,
                exit_load: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    """Tax (change in this year's tax, via fincalc.tax.tax_delta), exit load and stamp duty of redeeming every open lot
    of a regular plan at `nav` and buying the direct plan with the proceeds."""
    from finresearch.fincalc.dates import fiscal_year
    from finresearch.fincalc.tax import tax_delta
    from finresearch.portfolio.tax import STAMP_MF_BUY, DisposalRow, evaluate, gains_of

    fy = fiscal_year(today)
    rows, unknown, units, load = [], 0, Decimal(0), Decimal(0)
    px = Decimal(str(nav))
    pct = Decimal(str(exit_load["pct"])) / 100 if exit_load and exit_load.get("pct") is not None else None
    within = int(exit_load.get("days") or 0) if exit_load else 0
    for f in fl:
        for acq, q, cpu in f.lots:
            units += q
            cost = cpu * q if cpu is not None else None
            if cost is None or acq is None:
                unknown += 1
            rows.append(evaluate(DisposalRow(f.holding_tax, acq, today, q, cost, q * px, True, "buy")))
            if pct is not None and acq is not None and (today - acq).days < within:
                load += q * px * pct
    proceeds = units * px
    tax = tax_delta(list(base_gains), gains_of(rows), fy, slab)
    stamp = (proceeds - load) * STAMP_MF_BUY
    gain = sum((r.gain for r in rows if r.gain is not None), Decimal(0))
    return {"value": float(proceeds), "gain": float(gain), "tax": float(tax), "exit_load": float(load) if pct is not None else None,
            "exit_load_known": pct is not None, "stamp": float(stamp), "unknown_cost_lots": unknown,
            "total": float(tax + stamp + (load if pct is not None else 0))}  # fmt: skip


def costs(h: History, table: dict[str, Any] | None, table_note: str | None, lots: dict[int, FundLots],
          base_gains: Sequence[Any], slab: Decimal, exit_loads: dict[str, Any], today: date) -> dict[str, Any]:  # fmt: skip
    out = base(h)
    funds = [p for p in h.positions if p.asset_type == "mf" and p.value > 0]
    out["ter_source"] = table_note
    if not funds:
        out.update(available=False, reason=out["reason"] or "no mutual-fund holdings priced today")
        return out
    if not table:
        out.update(
            available=False,
            reason=f"AMFI's expense-ratio (TER) data is not available: {table_note or 'unknown error'}",
        )
        return out
    rows, unmatched, weighted, matched_value = [], [], 0.0, 0.0
    switches, suppressed = [], 0
    for p in funds:
        sch = p.scheme
        t = ter_row(table, sch) if sch is not None else None
        direct = is_direct(sch) if sch is not None else False
        if t is None:
            unmatched.append(p.name)
            continue
        own = t.direct if direct else t.regular
        if own is None:
            unmatched.append(p.name)
            continue
        weighted += p.value * float(own)
        matched_value += p.value
        row = {"key": p.key, "name": p.name, "plan": "Direct" if direct else "Regular", "value": _r(p.value),
               "ter_pct": float(own), "direct_ter_pct": float(t.direct) if t.direct is not None else None,
               "regular_ter_pct": float(t.regular) if t.regular is not None else None,
               "ter_day": t.day.isoformat(), "yearly_cost": _r(p.value * float(own) / 100, 0)}  # fmt: skip
        rows.append(row)
        if direct or t.direct is None or t.regular is None:
            continue
        saving = p.value * (float(t.regular) - float(t.direct)) / 100
        if p.value < TRIVIAL_VALUE or saving < TRIVIAL_SAVING:
            suppressed += 1
            continue
        fl = [lots[i] for i in p.holding_ids if i in lots]
        load = next((exit_loads.get(str(i)) for i in p.holding_ids if exit_loads.get(str(i))), None)
        sw = switch_cost(fl, p.price or 0.0, today, base_gains, slab, load)
        from finresearch.fincalc.funds import expense_drag

        drag10 = float(expense_drag(Decimal(str(p.value)), 10, Decimal(str(GROSS_RETURN_ASSUMPTION)),
                                    Decimal(str(t.regular)) / 100, Decimal(str(t.direct)) / 100))  # fmt: skip
        be = am.break_even_years(sw["total"], saving)
        switches.append({"key": p.key, "name": p.name, "holding_ids": p.holding_ids, "value": _r(p.value),
                         "costs": {k: (_r(v, 0) if isinstance(v, float) else v) for k, v in sw.items()},
                         "yearly_saving": _r(saving, 0), "ten_year_difference": _r(drag10, 0),
                         "break_even_years": _r(be, 1),
                         "exit_load_input": load,
                         "text": _switch_text(p.name, sw, saving, be)})  # fmt: skip
    wter = weighted / matched_value if matched_value else None
    out.update(available=True, weighted_ter_pct=_r(wter, 3), yearly_cost=_r(weighted / 100, 0),
               matched_value=_r(matched_value), funds=rows, unmatched=unmatched, switches=switches,
               suppressed=suppressed,
               status=("No regular plans with a material saving: no action needed." if not switches else
                       f"{len(switches)} regular plan(s) where a direct plan is cheaper; costs of switching first."),
               assumptions={"gross_return": GROSS_RETURN_ASSUMPTION,
                            "trivial": f"rows under ₹{TRIVIAL_VALUE:,.0f} value or ₹{TRIVIAL_SAVING:,.0f} a year saving are not shown",
                            "tax": "change in this financial year's tax if every open lot were redeemed today (FIFO "
                                   "lots, dated rules, your profile's slab), including the ₹1.25 lakh equity LTCG "
                                   "exemption still unused; surcharge not modelled",
                            "exit_load": "the app has no exit-load data: enter each scheme's load and period from its "
                                         "Key Information Memorandum; until then the load is shown as unknown",
                            "service": "A regular plan pays your distributor; if they give advice or service you value, "
                                       "that is your judgement."})  # fmt: skip
    return out


def _switch_text(name: str, sw: dict[str, Any], saving: float, be: float | None) -> str:
    load = f"exit load ₹{sw['exit_load']:,.0f}" if sw["exit_load_known"] else "exit load unknown (enter it)"
    parts = [f"Switching {name} to its direct plan costs about ₹{sw['total']:,.0f} now: tax ₹{sw['tax']:,.0f}, {load}, "
             f"stamp duty ₹{sw['stamp']:,.0f}."]  # fmt: skip
    if sw["unknown_cost_lots"]:
        parts.append(f"{sw['unknown_cost_lots']} lot(s) have an unknown cost, so the tax is incomplete.")
    tail = "."
    if be is not None:
        tail = (
            f"; break-even in about {be:.1f} years"
            + ("" if sw["exit_load_known"] else " (before the unknown exit load)")
            + "."
        )
    parts.append(f"The lower expense ratio then saves about ₹{saving:,.0f} a year at today's value{tail}")
    return " ".join(parts)
