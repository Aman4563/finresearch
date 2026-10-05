"""Listed-stock signal (docs/dev/RESEARCH_ROADMAP.md §D.2, items 11 and 13).

Event (`EVENT`, scored by the ledger's resolver "excess_return_12m"): "the stock's 12-month total return (closes plus
cash dividends with an ex-date in the window, not reinvested) beats the price return of NIFTYBEES (the Nifty 50 ETF)
over the same dates", horizon 12 months. The probability is a proxy for it: the hit rate of the stock's momentum +
trend bucket in the walk-forward backtest (evals/stock_backtest/results.json, finresearch.evals.stock_backtest), which
measured price return against the NIFTY 50 price index with dividends left out on both sides (NSE serves no total-return
history), with a Wilson interval on an effective n. The signal's base-rate description and a caveat say so.

While CALLS_ENABLED is False (decision #193, see the PROMOTION RULE below) the signal's action is INFORMATIONAL:
the composite's action is kept in `Signal.call` and the forecast ledger, never shown as an instruction.

The composite action comes from a transparent composite score whose weights were fixed before the backtest was run
(pre-registered, "composite v1"). They follow the Indian factor evidence (IIMA four-factor library [39][40]: momentum
and value premia are supported in India, size is not) and treat forensic scores as screening flags only:

| factor                              | contribution (score points)                       | evidence              |
|-------------------------------------|---------------------------------------------------|-----------------------|
| risk-adjusted 12-1 momentum         | 40 x clip(score / 1.5, -1, 1)                     | IIMA WML [39][40]     |
| trend: price vs its 200-day average | 20 x clip(distance / 10 %, -1, 1)                 | [41][42]; India [W]   |
| P/E percentile vs own history       | 10 x (1 - 2 x percentile)  (cheap +, dear -)      | context only [W]      |
| FII + DII holding change, 4 qtrs    | 5 x clip(change / 2 pp, -1, 1)                    | weak [W]              |
| promoter holding change, 4 qtrs     | 5 x clip(change / 2 pp, -1, 1)                    | weak [W]              |
| each forensic red flag              | -10 (at most -30)                                 | screening flag [W]    |
| 52-week position, volatility, ATR   | 0: shown as context and used for sizing           |                       |

BSE-only stocks (instrument "BSE:<scrip code>") get the same signal and forensic card from BSE data: BSE's daily
closes (one CSV for the whole range), corporate actions, Integrated Filing XBRL (quarters from Mar-2025, so at most
two fiscal years for the forensic scores) and shareholding-pattern XBRL. The bucket probability still comes from the
NIFTY 50 backtest (NSE large caps), which a small BSE-only company may not resemble: every such signal says so. Its
forecast is resolved on BSE closes against NIFTYBEES on NSE (see signals/ledger.py).

The P/E uses a trailing-twelve-month EPS built from what the company files (fincalc.signals.ttm_from_periods): four
quarters, or for a half-yearly filer (whose March filing reports six months) two half-years or a half-year and two
quarters, or the fiscal year's own EPS; the factor says which. A scored factor that cannot be computed (no P/E, fewer
than two shareholding patterns, no forensic score) is still listed with value None, contribution 0 and the reason.

Only the momentum + trend part is backtested; the composite and its action are "rule_based". Sizing is
volatility-scaled, capped by the profile's single-stock limit and by a quarter-Kelly ceiling from the bucket's
backtested mean excess return (roadmap §D.6); an ATR(14) stop distance is shown as risk control, not alpha [46].
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from finresearch.fincalc import forensic as fz
from finresearch.fincalc import signals as sg
from finresearch.portfolio.limits import position_limit
from finresearch.signals.base import Factor, Signal, Validation, action_for_score, clip_score
from finresearch.signals.registry import register

# the forecast ledger's stock event (finresearch.signals.ledger, resolver "excess_return_12m"): the signal is logged
# and later scored against exactly this. The backtest measured the nearest thing its data allows: price return vs the
# NIFTY 50 price index (dividends left out on both sides); see the caveat on every signal.
EVENT = ("12-month total return (NSE closes plus cash dividends) above the price return of NIFTYBEES "
         "(Nifty 50 ETF) over the same dates")  # fmt: skip
EVENT_BSE = ("12-month total return (BSE closes plus cash dividends) above the price return of NIFTYBEES "
             "(Nifty 50 ETF, NSE) over the same dates")  # fmt: skip
EVENT_KIND = "excess_return_12m"
BSE_CAVEAT = ("BSE-only stock: the bucket probability was backtested on NSE-listed NIFTY 50 stocks (large, liquid "
              "survivors). A small BSE-only company can trade thinly (wide spreads, days without trades, circuit "
              "limits) and has no survivorship-free history in this backtest, so treat the probability as a weak "
              "prior, not a measured rate for stocks like this one.")  # fmt: skip
LOG_FORECASTS = os.environ.get("FINRESEARCH_LOG_SIGNALS", "1") != "0"  # "0" for screenshots against a live DB
METHOD = ("Composite v1 (pre-registered weights: momentum 40, trend 20, valuation vs own history 10, shareholding "
          "10, forensic flags -10 each). Probability: backtested hit rate of the stock's momentum + trend bucket, "
          "NIFTY 50 universe, monthly walk-forward.")  # fmt: skip
# ---- calls are informational until a model shows an edge (decision #193)
# The momentum + trend rule showed no edge on the point-in-time NIFTY 50 (evals/experiments/stock_pit_universe:
# -3.1 pp a year vs the equal-weight members, Newey–West t -0.97; no pre-registered variant passed), and its ~56 %
# "chance of beating the Nifty" sits on a 55.7 % base rate (all NIFTY 50 stock-months, survivorship-flattered). So the
# composite's BUY / ACCUMULATE / HOLD / REDUCE / SELL is not shown as an instruction: the signal's action is
# INFORMATIONAL and `Signal.call` carries the label, the factor tilt and the probability next to its base rate.
# The forecast ledger still logs the composite action, probability and validation exactly as before (plus
# inputs.call_status), so the calls keep being scored out of sample.
# PROMOTION RULE: set CALLS_ENABLED = True only when a model pre-registered in evals/experiments (PREREG.md committed
# before its run) passes its bar on the point-in-time universe: net excess return vs the equal-weight PIT universe
# > 0 AND Newey–West t > 2.39 (Bonferroni for the variants tested, stock_pit_universe/PREREG.md "Pass bar"), and
# signals/stock.py then implements that model ("composite v2") with its validation text pointing at the results.
CALLS_ENABLED = False
INFORMATIONAL_LABEL = "Informational — no proven edge"
PROMOTION_RULE = ("Calls return only when a pre-registered model beats the equal-weight point-in-time NIFTY 50 after "
                  "costs with a Newey–West t above 2.39 (evals/experiments/stock_pit_universe/PREREG.md).")  # fmt: skip


def tilt(score: float) -> str:
    """The factor tilt in words, on the composite's fixed cut-offs (+50 / +20 / -20 / -50, base.action_for_score)."""
    if score >= 50:
        return "factors lean strongly positive"
    if score >= 20:
        return "factors lean positive"
    if score > -20:
        return "factors are mixed"
    if score > -50:
        return "factors lean negative"
    return "factors lean strongly negative"


def call_view(score: float, composite_action: str, prob: float | None, interval: tuple[float, float] | None,
              bt: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
    """`Signal.call` for an informational stock signal: label, tilt and the probability next to its base rate."""
    allb = ((bt or {}).get("buckets") or {}).get("all") or {}
    base = {"p": allb["p"], "n": allb.get("n"),
            "description": "of all NIFTY 50 stock-months in the backtest beat the index over the next 12 months "
                           "(today's members, so survivorship-flattered)"} if allb.get("p") is not None else None  # fmt: skip
    vs = None
    if prob is not None and base:
        noise = interval is not None and interval[0] <= base["p"] <= interval[1]
        vs = (f"{prob:.1%} chance it beats the Nifty, against a {base['p']:.1%} base rate "
              f"({base['p']:.1%} of NIFTY 50 stock-months beat the index): {(prob - base['p']) * 100:+.1f} pp"
              + (", within noise of the base rate" if noise else ""))  # fmt: skip
    return {"status": "informational", "label": INFORMATIONAL_LABEL, "tilt": tilt(score),
            "composite_action": composite_action, "universe_base_rate": base, "probability_vs_base": vs,
            "promotion_rule": PROMOTION_RULE}  # fmt: skip


HISTORY_DAYS = 1100  # three years: momentum needs 13 months; the rest feeds the P/E history
RESULT_QUARTERS = 12
RISK_BUDGET = {"low": 0.015, "medium": 0.02, "high": 0.025}  # weight x volatility (annual) per position
ATR_K = 2.5  # stop = price - k x ATR(14); roadmap §D.2 suggests k = 2-3
CACHE_S = 1800
NEGATIVE_CACHE_S = 30  # inputs with a part the exchange could not serve just now


# --------------------------------------------------------------------------- data sources (test seam)
@dataclass
class StockSources:
    """Where the provider's data comes from. Fields left None use NSE live (through the polite client)."""

    load: Callable[[str], Awaitable[dict[str, Any]]] | None = (
        None  # symbol -> the raw inputs (see _load_live)
    )
    backtest: Callable[[], dict[str, Any] | None] | None = None
    profile: Callable[[], Any] | None = None
    today: Callable[[], date] | None = None
    record: Callable[..., Any] | None = (
        None  # (signal, **kw) -> forecast id; default finresearch.signals.ledger.record
    )


SOURCES = StockSources()
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _today() -> date:
    from finresearch.fincalc.dates import today_ist

    return SOURCES.today() if SOURCES.today else today_ist()


def load_backtest() -> dict[str, Any] | None:
    if SOURCES.backtest is not None:
        return SOURCES.backtest()
    from finresearch.evals.stock_harvest import ARTEFACT_DIR

    p = ARTEFACT_DIR / "results.json"
    return json.loads(p.read_text()) if p.exists() else None


def _profile() -> Any:
    if SOURCES.profile is not None:
        return SOURCES.profile()
    from finresearch.suggest.profile import Profile

    try:
        from finresearch.db import session_scope
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return load_profile(s)
    except Exception:
        return Profile()


class _Errors(list):
    """The inputs' error lines, plus `unreachable`: the parts lost to the network or the exchange's gate."""

    def __init__(self) -> None:
        super().__init__()
        self.unreachable: list[str] = []

    def finish(self, results: Any) -> list[str]:
        """Every unreachable part, including the results' own XBRL fetches that failed on the network."""
        extra = (
            ["results"]
            if isinstance(results, dict) and results.get("unreachable") and "results" not in self.unreachable
            else []
        )
        return [*self.unreachable, *extra]


async def _part(errors: list[str], name: str, make: Callable[[], Awaitable[Any]], default: Any) -> Any:
    from finresearch.adapters.http import is_transient

    try:
        return await make()
    except Exception as e:  # one refused section must not sink the signal; it is listed in the caveats
        errors.append(f"{name}: {type(e).__name__}")
        if is_transient(e) and isinstance(errors, _Errors):
            errors.unreachable.append(name)
        return default


def instrument_key(instrument: str) -> str:
    """An NSE symbol ("INFY") or a BSE-only stock ("BSE:526433"); anything else is a ValueError."""
    from finresearch.adapters.bse_equity import bse_key, scrip_code_of
    from finresearch.api.markets import SYMBOL_RE

    sym = instrument.strip().upper()
    code = scrip_code_of(sym)
    if code:
        return bse_key(code)
    if not SYMBOL_RE.match(sym):
        raise ValueError(f"{instrument!r} is not an NSE symbol or BSE:<scrip code>")
    return sym


async def _load_live(sym: str) -> dict[str, Any]:
    """Quote, three years of daily bars, corporate actions, 12 quarters of results (with the annual statements the
    forensic scores need) and five quarters of shareholding, from NSE (from BSE for "BSE:<code>")."""
    from finresearch.adapters.bse_equity import scrip_code_of

    if (code := scrip_code_of(sym)) is not None:
        return await _load_live_bse(code)
    from finresearch.adapters.nse import NseClient
    from finresearch.adapters.nse_equity import NseEquity, walk_history
    from finresearch.api.markets import _retry, results_from_nse, shareholding_from_nse

    errors = _Errors()
    async with NseClient() as nse:
        try:
            quote = await nse.quote(sym)
        except Exception as e:
            raise LookupError(f"NSE has no quote for {sym}: {e}") from e
    today = _today()
    annual: dict[date, dict[str, Any]] = {}
    async with NseEquity() as eq:
        bars, partial = await walk_history(lambda lo, hi: _retry(lambda: eq.history(sym, lo, hi)),
                                           today - timedelta(days=HISTORY_DAYS), today)  # fmt: skip
        if partial:
            errors.append("price history: NSE refused part of the older history")
            errors.unreachable.append("price history")
        actions = await _part(errors, "corporate actions", lambda: eq.corporate_actions(sym), [])
        results = await _part(errors, "results", lambda: results_from_nse(eq, sym, RESULT_QUARTERS, annual_facts=annual),
                              {"quarters": []})  # fmt: skip
        holding = await _part(
            errors, "shareholding", lambda: shareholding_from_nse(eq, sym, 5), {"quarters": []}
        )
    return {"quote": quote, "bars": bars, "actions": actions, "results": results, "annual": annual,
            "shareholding": holding, "errors": list(errors), "unreachable": errors.finish(results)}  # fmt: skip


async def _load_live_bse(code: str, equity: Any = None) -> dict[str, Any]:
    """The same inputs for a BSE-only stock from BSE: its daily-price CSV answers three years in one request; results
    and the forensic annual statements come from BSE's Integrated Filing XBRL (Mar-2025 onwards)."""
    from finresearch.adapters.bse_equity import BseEquity
    from finresearch.api.markets import results_from_nse, shareholding_from_nse

    errors = _Errors()
    today = _today()
    annual: dict[date, dict[str, Any]] = {}
    async with equity or BseEquity() as eq:
        try:
            quote = await eq.quote(code)
        except Exception as e:
            raise LookupError(f"BSE has no quote for scrip {code}: {e}") from e
        if quote is None:
            raise LookupError(f"BSE has no quote for scrip {code}")
        bars = await _part(
            errors, "price history", lambda: eq.history(code, today - timedelta(days=HISTORY_DAYS), today), []
        )
        actions = await _part(errors, "corporate actions", lambda: eq.corporate_actions(code), [])
        results = await _part(errors, "results", lambda: results_from_nse(eq, code, RESULT_QUARTERS, annual_facts=annual),
                              {"quarters": []})  # fmt: skip
        holding = await _part(
            errors, "shareholding", lambda: shareholding_from_nse(eq, code, 5), {"quarters": []}
        )
    return {"exchange": "BSE", "quote": quote, "bars": bars, "actions": actions, "results": results,
            "annual": annual, "shareholding": holding, "errors": list(errors),
            "unreachable": errors.finish(results)}  # fmt: skip


async def inputs(sym: str) -> dict[str, Any]:
    """The raw inputs for one symbol, cached in memory for half an hour (the signal and the forensic card share it)."""
    import time

    hit = _cache.get(sym)
    if hit and hit[0] > time.time():
        return hit[1]
    raw = await (SOURCES.load or _load_live)(sym)
    # a part lost to the network (DNS, timeout, a 403/5xx or block page) must not stick for half an hour
    _cache[sym] = (time.time() + (NEGATIVE_CACHE_S if raw.get("unreachable") else CACHE_S), raw)
    if len(_cache) > 64:
        for k in [k for k, (at, _) in _cache.items() if at < time.time()]:
            _cache.pop(k, None)
    return raw


# --------------------------------------------------------------------------- derived features
@dataclass
class Features:
    price: float | None = None
    last_day: date | None = None
    bars: int = 0
    first_day: date | None = None
    sma200: float | None = None
    trend_distance: float | None = None
    mom_12_1: float | None = None
    vol: float | None = None
    mom_score: float | None = None
    atr14: float | None = None
    week52_position: float | None = None
    pe: float | None = None
    pe_percentile: float | None = None
    pe_points: int = 0
    pe_from: date | None = None
    pe_basis: str | None = None  # how the TTM EPS was built (fincalc.signals.ttm_basis)
    pe_ttm_end: date | None = None
    pe_ttm_eps: float | None = None
    pe_pieces: list[str] = field(default_factory=list)
    pe_note: str | None = None  # why a newer filed period could not be used
    pe_reason: str | None = None  # why there is no P/E (then pe is None)
    holding_reason: str | None = None
    inst_reason: str | None = None
    promoter_reason: str | None = None
    inst_change_pp: float | None = None
    promoter_change_pp: float | None = None
    holding_span: str | None = None
    adjustments: list[tuple[date, float]] = field(default_factory=list)
    anomalies: list[date] = field(default_factory=list)


def features(raw: dict[str, Any]) -> Features:
    f = Features()
    bars = [b for b in raw["bars"] if b.close]
    if not bars:
        return f
    days = [b.day for b in bars]
    close = [float(b.close) for b in bars]
    acts = [(a.ex_date, a.subject) for a in raw.get("actions") or [] if a.ex_date]
    adj = sg.adjust_for_actions(days, close, acts, high=[float(b.high) if b.high else None for b in bars],
                                low=[float(b.low) if b.low else None for b in bars])  # fmt: skip
    f.adjustments, f.anomalies = adj.applied, adj.anomalies
    c = adj.close
    f.price, f.last_day, f.bars, f.first_day = c[-1], days[-1], len(c), days[0]
    q = raw.get("quote")
    if (
        q is not None
    ):  # the display price: last traded in session, the official close after it (fincalc.price)
        from finresearch.fincalc.price import display_price

        px = display_price(q)
        if px is not None:
            f.price = float(px)
    f.sma200 = sg.sma(c, 200)
    if f.sma200:
        f.trend_distance = f.price / f.sma200 - 1
    f.mom_12_1 = sg.momentum_12_1(c)
    f.vol = sg.realised_vol(c)
    if f.mom_12_1 is not None and f.vol:
        f.mom_score = f.mom_12_1 / f.vol
    hl = [
        (h, lo_, cl)
        for h, lo_, cl in zip(adj.high, adj.low, c, strict=True)
        if h is not None and lo_ is not None
    ]
    if len(hl) >= 15:
        f.atr14 = sg.atr([x[0] for x in hl], [x[1] for x in hl], [x[2] for x in hl], 14)
    year = [x for d, x in zip(days, c, strict=True) if d > days[-1] - timedelta(days=365)]
    if year:
        f.week52_position = sg.range_position(f.price, min(year), max(year))
    _valuation(f, raw, days, c, adj.applied)
    _holding(f, raw.get("shareholding") or {})
    return f


def _filed_periods(raw: dict[str, Any]) -> list[sg.FiledPeriod]:
    """Every filed period with an EPS: the results' `periods` (quarters, half-years, year-to-dates, years), or for an
    older payload its quarters and fiscal years (a quarter without a start date is the three months to its end)."""
    res = raw.get("results") or {}
    rows = res.get("periods")
    if rows is None:
        rows = [*(res.get("quarters") or []), *(res.get("annual") or [])]
    out: list[sg.FiledPeriod] = []
    for r in rows:
        if r.get("eps") is None or not r.get("filed_at") or not r.get("period_end"):
            continue
        end = date.fromisoformat(r["period_end"])
        start = date.fromisoformat(r["period_start"]) if r.get("period_start") else _quarter_start(end)
        out.append(sg.FiledPeriod(start, end, float(r["eps"]), datetime.fromisoformat(r["filed_at"]).date(),
                                  r.get("consolidated")))  # fmt: skip
    return out


def _quarter_start(end: date) -> date:
    m = end.month - 2
    return date(end.year - (m < 1), m + 12 if m < 1 else m, 1)


def _span(a: date, b: date) -> str:
    return f"{a:%b %Y}" if (a.year, a.month) == (b.year, b.month) else f"{a:%b %Y}-{b:%b %Y}"


def _valuation(f: Features, raw: dict[str, Any], days: list[date], close: list[float],
               applied: list[tuple[date, float]]) -> None:  # fmt: skip
    """P/E = adjusted close / the TTM EPS known that day (fincalc.signals.ttm_from_periods: four quarters, two
    half-years, a half-year and two quarters, or the fiscal year's own EPS). EPS filed before a split or bonus is put on
    today's share basis with the same factor as the price. When no P/E can be computed, `pe_reason` says exactly why."""
    periods = [sg.FiledPeriod(p.start, p.end, p.eps / math.prod(fct for ex, fct in applied if ex > p.known), p.known,
                              p.consolidated) for p in _filed_periods(raw)]  # fmt: skip
    if not periods:
        failed = [e for e in raw.get("errors") or [] if e.startswith("results")]
        f.pe_reason = (f"The results could not be loaded ({'; '.join(failed)}), so there is no EPS to divide by."
                       if failed else "No filed results with an EPS were found, so there is no EPS to divide by.")  # fmt: skip
        return
    rows = sg.ttm_from_periods(periods)
    latest = max(periods, key=lambda p: p.end)
    on_file = ", ".join(f"{_span(p.start, p.end)}" for p in sorted(periods, key=lambda p: p.end)[-6:])
    if not rows:
        gaps = sg.ttm_gaps(periods, latest.end)
        f.pe_reason = ("The filed periods do not add up to any twelve months (on file: " + on_file + ")" +
                       (f"; missing for the year to {latest.end:%b %Y}: " + ", ".join(_span(a, b) for a, b in gaps)
                        if gaps else "") + ". No trailing EPS, so no P/E.")  # fmt: skip
        return
    cur = rows[-1]
    f.pe_basis, f.pe_ttm_end, f.pe_ttm_eps = cur.basis, cur.end, cur.eps
    f.pe_pieces = [_span(p.start, p.end) for p in cur.pieces]
    if latest.end > cur.end:
        gaps = sg.ttm_gaps(periods, latest.end)
        f.pe_note = (f"The latest filed period (to {latest.end:%d %b %Y}) cannot extend it: " +
                     (("the filings on file have no separate figures for " + ", ".join(_span(a, b) for a, b in gaps))
                      if gaps else "its pieces overlap other filed periods") + ".")  # fmt: skip
    if cur.eps <= 0:
        f.pe_reason = (f"Trailing EPS ({cur.basis}, to {cur.end:%d %b %Y}) is {cur.eps:.2f}: a loss, so P/E has no "
                       "meaning.")  # fmt: skip
        return
    pe = sg.pe_series(days, close, [(r.known, r.eps) for r in rows])
    if not f.price:
        f.pe_reason = "No current price."
        return
    f.pe = f.price / cur.eps
    f.pe_points = len(pe)
    f.pe_from = next((d for d in days if d >= rows[0].known), None)
    if len(pe) >= 120:  # about six months of daily P/E before a percentile means anything
        f.pe_percentile = sg.percentile_rank(pe, f.pe)


def _holding(f: Features, shp: dict[str, Any]) -> None:
    qs = [q for q in shp.get("quarters") or [] if q.get("groups")]
    if len(qs) < 2:
        n = len(shp.get("quarters") or [])
        why = (f"{len(qs)} of the {n} shareholding patterns read carry a category breakdown" if n
               else "no shareholding pattern could be read")  # fmt: skip
        f.holding_reason = f"Needs two filed shareholding patterns to compare; {why}."
        return
    a, b = qs[0], qs[-1]  # oldest, latest (up to four quarters apart)

    def g(q: dict[str, Any], k: str) -> float | None:
        return q["groups"].get(k)

    f.holding_span = f"{a['as_of']} to {b['as_of']}"
    fa, da, fb, db = g(a, "fii"), g(a, "dii"), g(b, "fii"), g(b, "dii")
    if None not in (fa, da, fb, db):
        f.inst_change_pp = (fb + db) - (fa + da)  # type: ignore[operator]
    else:
        f.inst_reason = f"The patterns ({f.holding_span}) do not both report FII and DII holdings."
    if g(a, "promoter") is not None and g(b, "promoter") is not None:
        f.promoter_change_pp = g(b, "promoter") - g(a, "promoter")  # type: ignore[operator]
    else:
        f.promoter_reason = (
            f"The patterns ({f.holding_span}) do not both report the promoter group's holding."
        )


# --------------------------------------------------------------------------- forensic
def forensic(raw: dict[str, Any]) -> dict[str, Any]:
    """The five forensic scores for the latest fiscal year against the one before, from the annual Integrated
    Filings (Mar-2025 onwards carry the balance sheet and cash flow)."""
    annual: dict[date, dict[str, Any]] = raw.get("annual") or {}
    q = raw.get("quote")
    industry = getattr(q, "industry", None)
    ends = sorted(annual, reverse=True)
    with_bs = [e for e in ends if "total_assets" in annual[e]["facts"]]
    cur_end = with_bs[0] if with_bs else (ends[0] if ends else None)
    cur = annual.get(cur_end) if cur_end else None
    prev_end = next((e for e in ends if cur_end and 330 <= (cur_end - e).days <= 400), None)
    prev = annual.get(prev_end) if prev_end else None
    notes: list[str] = []
    if prev is not None and cur is not None and prev["consolidated"] != cur["consolidated"]:
        notes.append("The two years were filed on different bases (consolidated vs standalone); year-on-year "
                     "scores are not computed.")  # fmt: skip
        prev = None
    if cur is None:
        notes.append("No annual results XBRL was found for the last fiscal years.")
    elif "total_assets" not in cur["facts"]:
        notes.append(
            "The annual filing carries no balance sheet (filings before the Mar-2025 quarter don't)."
        )
    restructured = restructured_reason(getattr(q, "listing_date", None), cur_end, prev_end, bool(ends))
    if restructured and cur is not None:
        notes.append(f"Recently listed or restructured ({restructured}): scores built on the balance sheet are "
                     "unreliable, and a single filed year allows no year-on-year comparison.")  # fmt: skip
    scores = fz.scorecard(cur["facts"] if cur else None, prev["facts"] if prev else None, industry=industry,
                          revenue_basis=cur.get("revenue_basis") if cur else None,
                          restructured=restructured)  # fmt: skip
    bse = raw.get("exchange") == "BSE"
    if bse:
        notes.append("Read from BSE's Integrated Filing XBRL, which starts with the Mar-2025 quarter: at most two "
                     "fiscal years, so the year-on-year scores rest on FY25 and FY26 only.")  # fmt: skip
    return {"symbol": getattr(q, "symbol", None), "industry": industry, "exchange": "BSE" if bse else "NSE",
            "scrip_code": getattr(q, "scrip_code", None) if bse else None,
            "fiscal_year_end": cur_end.isoformat() if cur_end else None,
            "prior_year_end": prev_end.isoformat() if prev and prev_end else None,
            "basis": None if cur is None else ("consolidated" if cur["consolidated"] else "standalone"),
            "sources": [x["xbrl"] for x in (cur, prev) if x], "notes": notes,
            "red_flags": sum(1 for s in scores if s.red_flag),
            "scores": [score_json(s) for s in scores],
            "disclaimer": "Screening flags, not buy/sell triggers. None of these scores has been validated on "
                          "Indian data [W]."}  # fmt: skip


def restructured_reason(
    listed: date | None, cur_end: date | None, prev_end: date | None, any_filing: bool
) -> str | None:
    """Why a company's latest balance sheet is not the history of a going concern: listed within the two fiscal years
    before the year's end (a demerged or newly listed company: TMCV, the Tata Motors CV demerger, listed 12-Nov-2025),
    or no annual filing for the year before (the entity did not report as a listed company then)."""
    if cur_end is None:
        return None
    if listed is not None and listed > cur_end - timedelta(days=730):
        return f"listed {listed:%d %b %Y}, within two fiscal years of the {cur_end:%b %Y} year end (a demerger or new listing)"
    if prev_end is None and any_filing:
        return "no annual results for the year before were found, as for a newly demerged or listed company"
    return None


def _num(v: Any) -> Any:
    if isinstance(v, Decimal):
        return round(float(v), 6)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, list):
        return [_num(x) for x in v]
    return v


def score_json(s: fz.Score) -> dict[str, Any]:
    return {"key": s.key, "name": s.name, "value": _num(s.value), "flag": s.flag, "red_flag": s.red_flag,
            "components": {k: _num(v) for k, v in s.components.items()}, "missing": s.missing,
            "proxies": s.proxies, "reason": s.reason, "thresholds": s.thresholds, "source": s.source,
            "caveat": s.caveat}  # fmt: skip


# --------------------------------------------------------------------------- the signal
def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))


SHP_PAGE = "https://www.nseindia.com/companies-listing/corporate-filings-shareholding-pattern"


def composite(f: Features, fz_scores: list[dict[str, Any]], shp_source: str = SHP_PAGE,
              fz_notes: list[str] | None = None) -> list[Factor]:  # fmt: skip
    """The factors and their contributions (pre-registered v1, see the module docstring). `shp_source` is where the
    shareholding patterns were read (NSE's filings page, or a BSE-only stock's BSE page). A scored factor that cannot
    be computed is still listed, with value None, contribution 0 and the reason, never silently dropped."""
    out: list[Factor] = []
    if f.mom_score is not None:
        out.append(Factor("Momentum (12-1 month, risk-adjusted)", round(f.mom_score, 2), 40 * _clip(f.mom_score / 1.5),
                          f"Return from 12 months ago to 1 month ago ({f.mom_12_1:+.1%}) divided by yearly volatility "
                          f"({f.vol:.1%}). Momentum is the strongest factor premium in Indian data (IIMA library: "
                          "~22 %/yr for winners minus losers, 1994-2014). Backtested here.",
                          "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/", "× σ"))  # fmt: skip
    if f.trend_distance is not None:
        out.append(Factor("Trend (price vs 200-day average)", round(f.trend_distance * 100, 1),
                          20 * _clip(f.trend_distance / 0.10),
                          f"Price is {abs(f.trend_distance):.1%} {'above' if f.trend_distance >= 0 else 'below'} its "
                          "200-trading-day average. Trend rules cut drawdowns but whipsaw in sideways markets; "
                          "India-specific evidence for the 200-DMA is weak. Backtested here.",
                          "fincalc:signals.sma", "%"))  # fmt: skip
    if f.pe is not None:
        pct = f.pe_percentile
        basis = (f"price ÷ trailing EPS {f.pe_ttm_eps:.2f}, {f.pe_basis} to {f.pe_ttm_end:%d %b %Y} "
                 f"({' + '.join(f.pe_pieces)}), from the filed results")  # fmt: skip
        out.append(Factor("P/E vs its own history", round(f.pe, 1), 0.0 if pct is None else 10 * (1 - 2 * pct),
                          (f"P/E {f.pe:.1f} ({basis}) is at the {pct:.0%} percentile of its daily history since "
                           f"{f.pe_from}. " if pct is not None else
                           f"P/E {f.pe:.1f} ({basis}); only {f.pe_points} days of P/E history, under the 120 a "
                           "percentile needs, so it adds nothing. ") + (f"{f.pe_note} " if f.pe_note else "") +
                          "Cheaper than usual adds, dearer subtracts. Context only: no Indian forward-return study "
                          "backs it. Not backtested.", "fincalc:signals.ttm_from_periods", "×"))  # fmt: skip
    else:
        out.append(Factor("P/E vs its own history", None, 0.0,
                          f"Not computed: {f.pe_reason or 'no trailing EPS.'} It adds nothing to the score.",
                          "fincalc:signals.ttm_from_periods", "×"))  # fmt: skip
    if f.inst_change_pp is not None:
        out.append(Factor("FII + DII holding change", round(f.inst_change_pp, 2), 5 * _clip(f.inst_change_pp / 2),
                          f"Change in foreign plus domestic institutional holding, {f.holding_span}, from the filed "
                          "shareholding-pattern XBRL. Weak evidence; small weight. Not backtested.",
                          shp_source, "pp"))  # fmt: skip
    else:
        out.append(Factor("FII + DII holding change", None, 0.0,
                          f"Not computed: {f.inst_reason or f.holding_reason or 'no shareholding data.'} It adds "
                          "nothing to the score.", shp_source, "pp"))  # fmt: skip
    if f.promoter_change_pp is not None:
        out.append(Factor("Promoter holding change", round(f.promoter_change_pp, 2),
                          5 * _clip(f.promoter_change_pp / 2),
                          f"Change in promoter holding, {f.holding_span}. Selling or dilution counts against; "
                          "buying for. Weak evidence; small weight. Not backtested.",
                          shp_source, "pp"))  # fmt: skip
    else:
        out.append(Factor("Promoter holding change", None, 0.0,
                          f"Not computed: {f.promoter_reason or f.holding_reason or 'no shareholding data.'} It adds "
                          "nothing to the score.", shp_source, "pp"))  # fmt: skip
    red = [s for s in fz_scores if s["red_flag"]]
    for i, s in enumerate(red):
        out.append(Factor(f"Forensic flag: {s['name']}", s["value"], -10.0 if i < 3 else 0.0,
                          f"{s['name']} is in its warning zone ({s['flag']}; {s['thresholds']}). A screening flag, "
                          "not a verdict: check the filings. Not validated on Indian data.", s["source"]))  # fmt: skip
    if not red:
        done = [s for s in fz_scores if s.get("value") is not None]
        if done:
            out.append(Factor("Forensic flags", 0, 0.0,
                              f"None of the {len(done)} forensic scores computed ("
                              + ", ".join(s["name"] for s in done) + ") is in its warning zone. Screening only.",
                              "fincalc:forensic.scorecard"))  # fmt: skip
        else:
            why = " ".join(fz_notes or []) or "the annual filings needed were not found."
            out.append(Factor("Forensic flags", None, 0.0,
                              f"Not computed: no forensic score could be calculated ({why.rstrip('.')}). Absence of "
                              "flags here means no data, not a clean bill.", "fincalc:forensic.scorecard"))  # fmt: skip
    if f.week52_position is not None:
        out.append(Factor("52-week position", round(f.week52_position * 100), 0.0,
                          "Where today's price sits between the 52-week low (0) and high (100). Context only; it "
                          "overlaps with momentum.", "fincalc:signals.range_position", "%"))  # fmt: skip
    if f.vol is not None:
        out.append(Factor("Realised volatility (1 year)", round(f.vol * 100, 1), 0.0,
                          "Annualised standard deviation of daily returns. Not scored; it sets the position size.",
                          "fincalc:signals.realised_vol", "%"))  # fmt: skip
    for fc in out:
        fc.contribution = round(fc.contribution, 2)
    return out


def history_reason(f: Features, listed: date | None, ex: str) -> str:
    """Why there is no signal, in numbers: when the stock listed, how many sessions the exchange returned, and what
    momentum (more than 252 sessions: 12 months back, skipping the last month) and the 200-day average need."""
    have = f"{ex} returned {f.bars} trading day{'s' if f.bars != 1 else ''} of prices"
    if f.first_day and f.last_day:
        months = (f.last_day.year - f.first_day.year) * 12 + f.last_day.month - f.first_day.month
        have += f" ({f.first_day:%d %b %Y} to {f.last_day:%d %b %Y}, about {months} month{'s' if months != 1 else ''})"
    need = []
    if f.mom_score is None:
        need.append(f"12-1 momentum needs {sg.TRADING_DAYS + 1} sessions (13 months)")
    if f.trend_distance is None:
        need.append("the 200-day average needs 200 sessions")
    # a listing inside the momentum window explains the short history (the first bars can come a few sessions later,
    # e.g. TMCV listed 12 Nov 2025 and its EQ-series history starts 26 Nov 2025)
    recent = listed and f.last_day and listed > f.last_day - timedelta(days=400)
    lead = f"Listed {listed:%d %b %Y}: " if recent else ""
    return (
        f"{lead}{have}; {' and '.join(need)}. The score and the backtested probability rest on momentum and "
        "trend, so there is no signal until then. The other factors below are shown for information only."
    )


def no_signal_factors(f: Features, fz_scores: list[dict[str, Any]], shp_source: str, fz_notes: list[str] | None,
                      why: str) -> list[Factor]:  # fmt: skip
    """Every factor, as in `composite`, with the missing momentum / trend listed (value None and the reason) and every
    contribution set to 0: without the two backtested factors nothing is scored (rule from #122: never drop a factor
    silently)."""
    out = composite(f, fz_scores, shp_source, fz_notes)
    names = {x.name for x in out}
    missing = []
    if "Momentum (12-1 month, risk-adjusted)" not in names:
        missing.append(Factor("Momentum (12-1 month, risk-adjusted)", None, 0.0,
                              f"Not computed: needs more than {sg.TRADING_DAYS} trading days of prices "
                              f"(has {f.bars}). {why.split(';')[0]}.",
                              "https://faculty.iima.ac.in/iffm/Indian-Fama-French-Momentum/", "× σ"))  # fmt: skip
    if "Trend (price vs 200-day average)" not in names:
        missing.append(Factor("Trend (price vs 200-day average)", None, 0.0,
                              f"Not computed: needs 200 trading days of prices (has {f.bars}).",
                              "fincalc:signals.sma", "%"))  # fmt: skip
    if f.vol is None:
        missing.append(Factor("Realised volatility (1 year)", None, 0.0,
                              f"Not computed: needs {sg.TRADING_DAYS + 1} daily closes (has {f.bars}).",
                              "fincalc:signals.realised_vol", "%"))  # fmt: skip
    for x in out:
        if x.contribution:
            x.explanation = f"{x.explanation} Informational only here: no score without momentum and trend."
        x.contribution = 0.0
    return missing + out


def sizing(f: Features, bucket: dict[str, Any] | None, profile: Any) -> dict[str, Any]:
    lim = position_limit(profile)  # the same limit as /portfolio and the pre-trade checklist (#194)
    risk, cap = lim.risk, lim.pct / 100
    # Judgment (audit #137): Kelly's μ is the bucket's mean 12-month return in excess of the NIFTY 50 (not of cash) and
    # σ the stock's own volatility (not the tracking error). Both choices make the ceiling smaller than an
    # excess-over-cash / tracking-error Kelly; the backtest's μ is survivorship-flattered, so the ceiling is loose anyway.
    mean_excess = bucket.get("mean_excess_12m") if bucket else None
    s = sg.position_size(f.vol, risk_budget=RISK_BUDGET.get(risk, 0.02), cap=cap, mean_excess=mean_excess)
    s["cap_source"] = lim.rule
    s["cap_rule"] = lim.to_json()
    s["risk_budget"] = RISK_BUDGET.get(risk, 0.02)
    if f.atr14 and f.price:
        stop = f.price - ATR_K * f.atr14
        s.update({"atr14": round(f.atr14, 2), "atr_k": ATR_K, "stop_price": round(stop, 2),
                  "stop_distance": round(ATR_K * f.atr14 / f.price, 4)})  # fmt: skip
    s["note"] = ("Volatility-scaled (weight x volatility = risk budget), capped by the single-stock limit, then by a "
                 "quarter-Kelly ceiling from the bucket's backtested mean excess return. The ATR stop is risk "
                 "control, not a return forecast.")  # fmt: skip
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}


@register("stock")
async def stock_signal(instrument: str, ctx: dict[str, Any]) -> Signal:
    from finresearch.evals.stock_backtest import bucket_of

    sym = instrument_key(instrument)
    bse = sym.startswith("BSE:")
    ex = "BSE" if bse else "NSE"
    raw = await inputs(sym)
    q = raw.get("quote")
    name = getattr(q, "company", None)
    f = features(raw)
    fzr = forensic(raw)
    bt = load_backtest()
    caveats = [
        "Survivorship bias: the bucket probabilities come from today's NIFTY 50 applied to the past, so they "
        "flatter. Rerun on the point-in-time NIFTY 50 (every member since 2014 from NSE's index-change notices, "
        "evals/experiments/stock_pit_universe), the same momentum-trend rule trailed the equal-weight index members "
        "by 3.1 pp a year after costs (Newey–West t −0.97), and no pre-registered variant (market trend filter, low "
        "volatility, momentum + low-vol) beat them: no edge is claimed.",
        "Costs (STT, stamp duty, exchange fees, slippage) are in the backtest; taxes are not. The backtest compares "
        "price returns with the NIFTY 50 price index (no dividends on either side); the logged forecast is scored "
        "on the stock's total return against NIFTYBEES, which is close but not identical.",
        "Personal research, not advice: a probability with a range, not a prediction for this stock.",
    ]
    if bse:
        caveats.insert(0, BSE_CAVEAT)
        caveats.append("The logged forecast is scored on BSE closes (plus cash dividends) against NIFTYBEES on NSE, "
                       "the same benchmark as NSE stocks so that both land in one calibration.")  # fmt: skip
    if raw.get("unreachable"):
        caveats.insert(0, f"Couldn't reach {ex} just now for: {', '.join(raw['unreachable'])}. Those factors are "
                          "missing for now (not because nothing is filed); this signal is not logged and is recomputed "
                          "on the next view.")  # fmt: skip
    if raw.get("errors"):
        caveats.append(f"Some inputs could not be loaded from {ex}: " + "; ".join(raw["errors"]))
    if f.anomalies:
        caveats.append("Unexplained one-day moves over 35 % in the price history (" +
                       ", ".join(d.isoformat() for d in f.anomalies[:3]) + "): a demerger or bad print.")  # fmt: skip
    from finresearch.adapters.bse_equity import bse_quote_page, scrip_code_of

    source = (bse_quote_page(scrip_code_of(sym) or sym, q) if bse else
              f"https://www.nseindia.com/get-quotes/equity?symbol={sym}")  # fmt: skip
    base = dict(asset="stock", instrument=sym, name=name, event=EVENT_BSE if bse else EVENT, horizon="12 months",
                method=METHOD, as_of=datetime.now(UTC), sources=[source])  # fmt: skip
    if f.mom_score is None or f.trend_distance is None:
        why = history_reason(f, getattr(q, "listing_date", None), ex)
        factors = no_signal_factors(f, fzr["scores"], source if bse else SHP_PAGE, fzr["notes"], why)
        return Signal(**base, action="NO_SIGNAL", score=0.0, factors=factors,
                      validation=Validation(status="rule_based", description=f"No signal: {why}"),
                      caveats=[why, *caveats])  # fmt: skip
    factors = composite(f, fzr["scores"], source if bse else SHP_PAGE, fzr["notes"])
    score = clip_score(sum(x.contribution for x in factors))
    bucket_name = bucket_of(f.trend_distance >= 0, f.mom_score)
    bucket = (bt or {}).get("buckets", {}).get(bucket_name)
    prob = interval = None
    base_rate = None
    metrics: dict[str, float] = {}
    if bucket and bucket.get("n"):
        prob = bucket["p"]
        interval = tuple(bucket["wilson95"]) if bucket.get("wilson95") else None
        base_rate = {"n": bucket["n"], "p": bucket["p"], "ci": bucket.get("wilson95"),
                     "description": f"{bucket_name}: NIFTY 50 stock-months {bt['stats']['from'][:7]} to "
                                    f"{bt['stats']['to'][:7]} whose next 12 months' price return beat the NIFTY 50 price index (backtested "
                                    "on price returns, no dividends: a proxy for the dividend-inclusive event above; "
                                    f"CI on an effective n of {bucket['n_effective']} = stock-months ÷ 12 for the overlapping "
                                    "12-month windows; stocks in the same month move together, so the true n is "
                                    "smaller and the range too narrow)"}  # fmt: skip
        st = bt["stats"]
        metrics = {k: round(v, 4) for k, v in {
            "strategy_cagr": st["cagr"]["strategy"], "equal_weight_cagr": st["cagr"]["equal_weight_universe"],
            "nifty50_cagr": st["cagr"]["nifty50_price"], "excess_cagr": st["excess_cagr_vs_equal_weight"],
            "monthly_hit_rate": st["monthly_hit_rate_vs_equal_weight"]["rate"],
            "strategy_max_drawdown": st["max_drawdown"]["strategy"]}.items() if v is not None}  # fmt: skip
        caveats.insert(0, _edge_caveat(bt, bucket))
    validation = Validation(
        status="rule_based", n=int(bucket["n_effective"]) if bucket else 0, metrics=metrics,
        description=("Backtested part: the momentum + trend bucket behind the probability (walk-forward, "
                     f"{bt['stats']['months']} months, costs included). Not backtested: the composite weights, "
                     "valuation, shareholding and forensic factors, and the action cut-offs." if bt else
                     "The backtest artefact is missing; the probability is not available."))  # fmt: skip
    if bt is None:
        caveats.insert(0, "No backtest results on disk (evals/stock_backtest/results.json): no probability.")
    composite_action = action_for_score(score)
    call = None
    if not CALLS_ENABLED:  # decision #193: analysis and factors, no instruction
        call = call_view(score, composite_action, prob, interval, bt)
        caveats.insert(1, f"{INFORMATIONAL_LABEL}: the composite would read {composite_action}, but no model has "
                          f"shown an edge, so it is not a recommendation. {PROMOTION_RULE}")  # fmt: skip
    sig = Signal(**base, action=composite_action if CALLS_ENABLED else "INFORMATIONAL", score=round(score, 1),
                 validation=validation, probability=prob, probability_interval=interval, base_rate=base_rate,
                 factors=factors, caveats=caveats, sizing=sizing(f, bucket, _profile()), call=call)  # fmt: skip
    # ctx log=0: a scheduled alert check, not a viewed signal. A signal missing inputs the exchange could not serve
    # just now is not logged: the day's forecast is recorded from complete inputs on a later view
    if str(ctx.get("log", "1")) != "0" and not raw.get("unreachable"):
        _log(sig, bucket_name)
    return sig


def _edge_caveat(bt: dict[str, Any], bucket: dict[str, Any]) -> str:
    """What the backtest says about the rule's edge, in plain words, from the committed numbers."""
    st = bt["stats"]
    ex = st.get("excess_cagr_vs_equal_weight")
    hr = st["monthly_hit_rate_vs_equal_weight"]
    lo, hi = hr.get("wilson95") or (None, None)
    t = st.get("newey_west_t_excess_vs_equal_weight")
    base = (bt.get("buckets") or {}).get("all") or {}
    no_edge = ex is None or ex <= 0 or (lo is not None and lo <= 0.5 <= hi) or (t is not None and abs(t) < 2)
    text = (f"Backtest: the momentum + trend portfolio returned {st['cagr']['strategy']:.1%} a year against "
            f"{st['cagr']['equal_weight_universe']:.1%} for the same stocks equally weighted (excess {ex:+.1%}, "
            f"t = {t:.2f}); it beat them in {hr['rate']:.0%} of months (95% CI {lo:.0%}-{hi:.0%}).")  # fmt: skip
    if no_edge:
        text += (
            " That is no demonstrated edge after costs, so treat the momentum and trend factors as context."
        )
    if base.get("p") is not None and bucket.get("wilson95"):
        blo, bhi = bucket["wilson95"]
        if blo <= base["p"] <= bhi:
            text += (f" This stock's bucket ({bucket['p']:.0%}) is within noise of the {base['p']:.0%} base rate for "
                     "all NIFTY 50 stock-months.")  # fmt: skip
    return text


def _log(sig: Signal, bucket_name: str) -> None:
    """Log the signal in the forecast ledger (one open forecast per stock per day) so it is scored in 12 months.
    A ledger failure never blocks the signal."""
    if not LOG_FORECASTS:
        return
    from finresearch.fincalc.dates import add_years

    today = _today()
    try:
        if SOURCES.record is not None:
            rec = SOURCES.record
        else:
            from finresearch.signals.ledger import record as rec

        # the ledger keeps the composite's action (unchanged since v1) so it is still scored; call_status records
        # that it was shown as informational (#193)
        logged = replace(sig, action=sig.call["composite_action"]) if sig.call else sig
        rec(logged, source="signal:stock", resolve_on=add_years(today, 1), event_kind=EVENT_KIND,
            inputs={"symbol": sig.instrument, "benchmark": "NIFTYBEES", "start_date": today.isoformat(),
                    "bucket": bucket_name, "call_status": sig.call["status"] if sig.call else "call",
                    **({"exchange": "BSE", "benchmark_exchange": "NSE"} if sig.instrument.startswith("BSE:") else {})})  # fmt: skip
    except Exception:  # the database may be down; the signal still shows
        logging.getLogger(__name__).warning("could not log the %s signal", sig.instrument, exc_info=True)
