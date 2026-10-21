"""F&O strategy signal: a risk, cost and fit assessment, not an edge (docs/dev/RESEARCH_ROADMAP.md §D.5).

The app has no edge-generating F&O signal, and the evidence says most individual traders lose, mainly through costs and
option buying (SEBI, 20-Aug-2026 [P30]). So the "signal" asks four questions about a proposed strategy and says ENTER
only when every answer is acceptable:

1. Defined risk: both payoff slopes are bounded (#351): no naked short options, and a loss bounded only by a price
   of zero (a short put, a put ratio, a long future + short call) is not defined risk.
2. Liquidity (#356): Σ half the bid/ask spread of every leg is at most 10% of the premium at mids (live bid/ask only;
   an estimated or unknown spread fails). The old "EV_rw ≥ −costs" gate is gone: the entry costs cancelled in it, so it
   only tested "IV at the strikes above realised volatility". Model EVs are now cost-complete (fills at mid ± half the
   spread, plus the exit half-spread) and informational: the risk-neutral EV with each leg at its own-strike IV is the
   fair-price identity −(statutory costs + half-spreads), and the σ = realised-volatility EVs (20- and 60-day windows,
   drift μ stated) are labelled scenarios, "model EV - not validated", that never change a verdict.
3. Budget: the maximum loss is within the profile's F&O risk budget (fno_max_loss_pct of fno_capital_inr).
4. IV context fits the strategy: premium sellers (net short vega) want IV high, buyers want it low. IV percentile
   of the 30-day IV derived from NSE's F&O bhavcopy history (#306, a year back as soon as it is backfilled), else of
   the monitor's recorded ATM IV once there are 60 days; until then the ratio of ATM IV to 20-day realised volatility is
   used as a stated proxy (implied variance above realised is the variance risk premium, Carr & Wu 2009 [52]; no
   India-specific VRP study was verified [W]).

Otherwise WAIT (or, for a position the user says is open (`held=1`), EXIT when a hard rule fails and HOLD otherwise).
The probability is the real-world chance of a positive P&L after costs at expiry, shown with the risk-neutral one;
the range is the span across the volatility assumptions (IV, 20- and 60-day realised), not a confidence interval.
Validation: rule-based, never backtested (n = 0).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from finresearch.fincalc import charges as ch
from finresearch.fincalc import options as o
from finresearch.fincalc import volatility as vol
from finresearch.signals.base import Factor, Signal, Validation, clip_score
from finresearch.signals.registry import register

SEBI_STUDY_URL = ("https://www.sebi.gov.in/media-and-notifications/press-releases/aug-2026/sebi-studies-indicate-key-"
                  "trends-in-retail-participation-trading-behaviour-and-profitability-in-the-equity-derivatives_103838.html")  # fmt: skip
# The risk notice shown on /fno and in every F&O signal (roadmap §D.5 and A7, source [P30]). Override the text with the
# FINRESEARCH_FNO_RISK_NOTICE setting when SEBI publishes a newer study.
RISK_NOTICE = {
    "headline": "87.7% of individual F&O traders lost money in FY26",
    "text": ("SEBI's study of FY26 found that 87.7% of individual equity-derivative traders made a net loss "
             "(₹91,685 crore in total). About 92% of the losses came from options, 97% of traders were mainly option "
             "buyers, and 59% of index-option turnover was in contracts expiring the same day. Only mainly-option "
             "sellers (about 2% of traders) had a positive median return."),
    "source": "SEBI press release 50/2026, equity derivatives study",
    "url": SEBI_STUDY_URL,
    "date": "2026-08-20",
}  # fmt: skip
RATE = 0.065  # analyse()'s default only; the API and the signal pass FBIL's par yield at the expiry (signals.rates)
# #356 (P0.6, roadmap MF1): model EV is shown, never a gate, until a physical model passes P2.2/P3.4. The only
# EV-related gate is liquidity: Σ|q_i|·(ask_i − bid_i)/2 ≤ LIQUIDITY_MAX_SPREAD_SHARE × the premium at mids (the
# credit received, or the debit paid). 10% is the issue's registered example [U: an assumption, not a measured cut-off]
LIQUIDITY_MAX_SPREAD_SHARE = 0.10
LIQUIDITY_CHECK = "Bid/ask spreads within 10% of the premium"
EV_INFO = "EV after costs and spreads (model, not validated)"
MODEL_EV_LABEL = "model EV - not validated"
SCENARIO = "scenario, not a forecast"
# why σ = realised volatility is a scenario: over 2022-26 the 30-day IV forecast next-month realised volatility better
# than the 20-day realised volatility (MAE 3.40 vs 4.09 vol points; roadmap #356, institutional gap analysis §2)
RV_SCENARIO_NOTE = ("σ = realised volatility is a scenario, not a forecast: in 2022-26 the 30-day IV forecast "
                    "next-month realised volatility better than the 20-day realised volatility (MAE 3.40 vs 4.09 vol "
                    "points). The lognormal's thin left tail understates crash losses (until roadmap P3.4).")  # fmt: skip
DRAWDOWN_PCT, DRAWDOWN_REPEATS = 0.20, 20  # risk-of-ruin style check: P(20 % drawdown within 20 repeats)
MIN_DTE_FOR_IV = 7  # the IV series reads the nearest expiry at least a week away


def _inr(x: float) -> str:
    """Whole rupees with Indian digit grouping (12,34,567)."""
    from finresearch.fincalc.numbers import group_indian

    return ("-" if x < 0 else "") + group_indian(str(round(abs(x))))


def risk_notice() -> dict[str, str]:
    from finresearch.config import get_settings

    text = getattr(get_settings(), "fno_risk_notice", None)
    return {**RISK_NOTICE, "text": text} if text else dict(RISK_NOTICE)


# --------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class LegIn:
    right: str  # call | put | future
    strike: float
    side: str  # buy | sell
    lots: int = 1
    premium: float | None = None


def parse_legs(text: str) -> list[LegIn]:
    """Legs from a query string: JSON ([{"right","strike","side","lots"}]) or "buy:call:22800:1,sell:call:23000:1"."""
    text = (text or "").strip()
    if not text:
        return []
    out = []
    if text.startswith("["):
        out = [LegIn(str(x["right"]), float(x["strike"]), str(x["side"]), int(x.get("lots", 1)),
                     float(x["premium"]) if x.get("premium") is not None else None) for x in json.loads(text)]  # fmt: skip
    else:
        for part in text.split(","):
            bits = part.strip().split(":")
            if len(bits) < 3:
                raise ValueError(f"cannot read leg {part!r}; use side:right:strike[:lots]")
            out.append(LegIn(bits[1], float(bits[2]), bits[0], int(bits[3]) if len(bits) > 3 else 1))
    if len(out) > 8:
        raise ValueError("at most 8 legs")
    for leg in out:
        if (
            leg.right not in ("call", "put", "future")
            or leg.side not in ("buy", "sell")
            or not 1 <= leg.lots <= 100
        ):
            raise ValueError(f"bad leg {leg}")
    return out


def preset_legs(chain, key: str) -> list[LegIn]:
    """The same presets as the /fno page (web/src/components/fno/model.ts), around the ATM strike, traded strikes."""
    rows = [r for r in chain.rows]
    if not rows or chain.underlying is None:
        return []
    spot = float(chain.underlying)
    strikes = [float(r.strike) for r in rows]
    i_atm = min(range(len(rows)), key=lambda i: abs(strikes[i] - spot))
    step = (strikes[min(i_atm + 1, len(rows) - 1)] - strikes[max(i_atm - 1, 0)]) / 2 or 1
    w = max(1, round(spot * 0.01 / step))

    def pick(offset: int, right: str) -> float | None:
        for d in range(len(rows)):
            for sgn in (1, -1) if offset >= 0 else (-1, 1):
                j = i_atm + offset + sgn * d
                if 0 <= j < len(rows):
                    q = rows[j].call if right == "call" else rows[j].put
                    if q and q.last_price and q.last_price > 0:
                        return strikes[j]
        return None

    spec = {"long_call": [("call", 0, "buy")], "long_put": [("put", 0, "buy")],
            "bull_call": [("call", 0, "buy"), ("call", w, "sell")], "bear_put": [("put", 0, "buy"), ("put", -w, "sell")],
            "straddle": [("call", 0, "buy"), ("put", 0, "buy")], "strangle": [("call", w, "sell"), ("put", -w, "sell")],
            "iron_condor": [("call", w, "sell"), ("call", 2 * w, "buy"), ("put", -w, "sell"), ("put", -2 * w, "buy")]}  # fmt: skip
    if key not in spec:
        raise ValueError(f"unknown preset {key!r}; one of {sorted(spec)}")
    legs = []
    for right, off, side in spec[key]:
        k = pick(off, right)
        if k is None:
            return []
        legs.append(LegIn(right, k, side))
    return legs


# --------------------------------------------------------------------------- the analysis (also used by the API)
def _model_ev(ev_q: float | None, *rws: Any) -> dict[str, Any]:
    """The EV range across the shown models (#356/#357): the own-strike-IV identity and the σ = RV scenarios."""
    evs = [x for x in (ev_q, *(r.ev for r in rws if r is not None)) if x is not None]
    return {"label": MODEL_EV_LABEL, "range": [_r(min(evs), 2), _r(max(evs), 2)] if evs else None,
            "note": RV_SCENARIO_NOTE}  # fmt: skip


def _r(x: float | None, d: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, d)


def _quote_of(chain, right: str, strike: float):
    row = next((r for r in chain.rows if float(r.strike) == strike), None) if chain is not None else None
    return (row.call if right == "call" else row.put) if row else None


def _half_spread(q, fb, ref: float) -> tuple[float, float, str]:
    """(mid, half-spread, source) for one option leg (#356): the live bid/ask of the chain, else of `fb` (NSE's chain
    when the plan's has none, #324), else the registered slippage tiers (backtest.specs.Book: a fraction of the price
    by contracts traded that day, the worst tier when the volume is unknown, at least ₹0.10) around `ref`."""
    from finresearch.backtest import costs

    for x in (q, fb):
        bid = float(x.bid) if x is not None and x.bid and x.bid > 0 else None
        ask = float(x.ask) if x is not None and x.ask and x.ask > 0 else None
        if bid is not None and ask is not None and ask >= bid:
            return (bid + ask) / 2, (ask - bid) / 2, "bid/ask"
    volume = float(q.volume) if q is not None and q.volume is not None else None
    return ref, costs.slippage(ref, volume), "slippage tiers (no live bid/ask)"


def analyse(chain, lot: int, legs_in: list[LegIn], *, today: date, closes: list[float], iv_series: list[float],
            capital: float, max_loss_pct: float, brokerage: float, rate: float = RATE, drift: float | None = None,
            overrides: dict[str, Any] | None = None, iv_source: dict[str, Any] | None = None,
            iv_as_of: date | None = None, bidask_chain: Any = None, early_exit_p: float = 1.0,
            now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Everything the page and the signal show for one strategy. `now` = the clock T is measured from (#359: live
    callers pass now_ist(); without it, 15:30 IST on `today`). Raises ValueError when a leg has no premium or the
    contract has expired.
    #356: model EVs are cost-complete (each leg at its fill, mid ± half the bid/ask, plus `early_exit_p` × the exit
    half-spreads; `bidask_chain` = NSE's chain for legs the plan's chain quotes without bid/ask) and informational."""
    from finresearch.fincalc import dates

    spot = float(chain.underlying)
    days = (chain.expiry - today).days
    t = dates.years_to_expiry(
        chain.expiry, dates.session_clock(today, now)
    )  # minutes to 15:30 IST, never floored
    if t <= 0:
        raise ValueError(f"the {chain.expiry:%d-%b-%Y} contract has expired (15:30 IST): nothing to price")
    mu = rate if drift is None else drift
    by_strike = {float(r.strike): r for r in chain.rows}
    legs, orders, notes = [], [], []
    fills: list[o.Leg] = []  # the legs at their fills (#356): what the model EVs are computed on
    own_ivs: list[float | None] = []  # each option leg's IV at its mid (the fair-price identity)
    spread_rows: list[dict[str, Any]] = []
    vega = 0.0
    for x in legs_in:
        qty = x.lots * lot * (1 if x.side == "buy" else -1)
        if x.right == "future":
            legs.append(o.Leg("future", x.strike, 0.0, qty))
            fills.append(o.Leg("future", x.strike, 0.0, qty))
            own_ivs.append(None)
            orders.append(ch.TradeLeg("future", x.side, x.strike, abs(qty)))
            # the chain quotes no future: its spread is unknown (never 0), so the liquidity check cannot pass
            spread_rows.append({"right": "future", "strike": x.strike, "side": x.side, "mid": None, "half": None,
                                "fill": None, "source": "unknown (no quote for a future)"})  # fmt: skip
            continue
        row = by_strike.get(x.strike)
        q = (row.call if x.right == "call" else row.put) if row else None
        prem = x.premium if x.premium is not None else (float(q.last_price) if q and q.last_price else None)
        if prem is None:
            raise ValueError(f"no premium for {x.right} {x.strike:g}: pass one or pick a traded strike")
        legs.append(o.Leg(x.right, x.strike, prem, qty))
        orders.append(ch.TradeLeg("option", x.side, prem, abs(qty)))
        iv = float(q.iv) / 100 if q and q.iv else o.implied_vol(x.right, prem, spot, x.strike, t, rate)
        if iv:
            vega += o.greeks(x.right, spot, x.strike, t, rate, iv).vega * qty
        mid, half, src = _half_spread(q, _quote_of(bidask_chain, x.right, x.strike), prem)
        fill = o.fill_price(mid, half, qty)
        fills.append(o.Leg(x.right, x.strike, fill, qty))
        own_ivs.append(o.implied_vol(x.right, mid, spot, x.strike, t, rate))  # type: ignore[arg-type]
        spread_rows.append({"right": x.right, "strike": x.strike, "side": x.side, "mid": round(mid, 4),
                            "half": round(half, 4), "fill": round(fill, 4), "source": src})  # fmt: skip
    prof = o.profile(legs, spot)

    # volatilities: ATM IV (market), 20- and 60-day realised (history)
    rows = [vol.StrikeIv(float(r.strike), float(r.call.iv) if r.call and r.call.iv else None,
                         float(r.put.iv) if r.put and r.put.iv else None) for r in chain.rows]  # fmt: skip
    atm = vol.atm_iv(rows, spot)
    iv = atm[1] / 100 if atm else None
    rv20 = o.realised_vol(closes, 20) if len(closes) >= 22 else None
    rv60 = o.realised_vol(closes, 60) if len(closes) >= 62 else None
    if rv20 is not None and rv60 is None:
        notes.append(f"only {len(closes)} daily closes: the real-world case uses the 20-day window alone")
    if rv20 is None:
        notes.append(f"only {len(closes)} daily closes: no realised volatility, so no real-world probability")

    # costs: entry orders now, and STT on long options exercised in the money at expiry
    entry = ch.order_costs(orders, today, brokerage, overrides)
    square_off = ch.order_costs(ch.flip(orders), today, brokerage, overrides)
    stt_ex = float(ch.rates_as_of(chain.expiry, overrides)["stt_option_exercise"].rate)
    longs = [x for x in legs if x.right != "future" and x.qty > 0]

    def exit_cost(p: float) -> float:
        return stt_ex * sum(x.qty * (max(0.0, p - x.strike) if x.right == "call" else max(0.0, x.strike - p))
                            for x in longs)  # fmt: skip

    # #356: half-spreads paid on entry (at the fills) and, with probability early_exit_p, again on an early exit
    opt_rows = [r for r in spread_rows if r["right"] != "future"]
    halves_known = all(r["half"] is not None for r in spread_rows)
    entry_spread = sum(
        abs(x.qty) * r["half"] for x, r in zip(legs, spread_rows, strict=True) if r["half"] is not None
    )
    exit_spread = early_exit_p * entry_spread
    spread_src = ("unknown (no quote for a future)" if not halves_known else
                  "bid/ask" if all(r["source"] == "bid/ask" for r in opt_rows) else
                  "slippage tiers (no live bid/ask)")  # fmt: skip
    premium_mid = abs(
        sum(-x.qty * r["mid"] for x, r in zip(legs, spread_rows, strict=True) if r["mid"] is not None)
    )

    def run(sigma: float | None, m: float) -> o.Outcome | None:
        return (
            o.outcome(fills, spot, t, sigma, m, rate, entry.total + exit_spread, exit_cost) if sigma else None
        )

    rn = run(iv, rate)  # its PoP only (one ATM-IV lognormal); its EV is the own-strike-IV identity below
    q_ev = o.own_iv_ev(fills, own_ivs, spot, t, rate, entry.total, stt_ex, exit_spread)
    rw20 = run(rv20, mu)
    rw60 = run(rv60, mu)
    # the headline real-world case is the less favourable of the two realised-volatility windows (lower EV):
    # a low 20-day volatility flatters premium sellers, a high one flatters buyers
    rw = min((x for x in (rw20, rw60) if x), key=lambda x: x.ev, default=None)
    rw_window = None if rw is None else ("20-day" if rw is rw20 else "60-day")
    # #357: a 20-dot quantile plot of the P&L at expiry (after costs and spreads) and the payoff ratio, under the
    # headline real-world scenario (else the risk-neutral ATM-IV model), labelled with the model it came from
    basis = rw or rn
    dots = None
    if basis is not None:
        dist = o.pnl_distribution(
            fills, spot, t, basis.vol, basis.drift, entry.total + exit_spread, exit_cost, 2000
        )
        pr = o.payoff_ratio(dist)
        dots = {"model": f"real-world {rw_window} scenario" if rw else "risk-neutral (ATM IV)",
                "values": [round(v, 2) for v in o.weighted_quantiles(dist, [(i + 0.5) / 20 for i in range(20)])],
                "payoff_ratio": None if pr is None else round(pr, 3),
                "note": "Each dot is 5% of outcomes: the P&L at expiry after costs and spreads in a lognormal "
                        f"{SCENARIO} (thin tails: real crashes are larger)."}  # fmt: skip
    stt_q = q_ev[1] if q_ev else (rn.exit_cost if rn else 0.0)  # e^(−rT)·E_Q[STT on exercise], own-strike IVs
    costs_total = entry.total + stt_q
    move = o.expected_move(spot, iv, t) if iv else None
    check = o.max_loss_check(prof.max_loss, capital, max_loss_pct)
    stats = vol.iv_stats(iv_series)
    skew = vol.skew_25d(rows, spot, t, rate)
    denom = abs(prof.max_loss) if prof.max_loss else (capital or None)
    dd = None
    if rw and capital > 0 and prof.max_loss is not None:
        dist = o.pnl_distribution(legs, spot, t, rw.vol, mu, entry.total, exit_cost)
        dd = o.drawdown_probability(dist, capital, DRAWDOWN_PCT, DRAWDOWN_REPEATS)

    def oc(x: o.Outcome | None) -> dict[str, Any] | None:
        return None if x is None else {"vol": _r(x.vol), "drift": _r(x.drift), "pop": _r(x.pop),
                                       "ev": _r(x.ev, 2), "ev_gross": _r(x.ev_gross, 2),
                                       "quantiles": {k: _r(v, 2) for k, v in x.quantiles.items()}}  # fmt: skip

    return {
        "spot": spot,
        "lot_size": lot,
        "days_to_expiry": days,
        "t_years": _r(t, 6),
        "rate": rate,
        "drift": mu,
        "legs": [
            {
                "right": x.right,
                "strike": x.strike,
                "premium": x.premium,
                "qty": x.qty,
                "fill": r["fill"],
                "spread_source": r["source"],
            }
            for x, r in zip(legs, spread_rows, strict=True)
        ],
        "breakevens": prof.breakevens,
        "max_profit": _r(prof.max_profit, 2),
        "max_loss": _r(prof.max_loss, 2),
        # #351: defined risk needs both payoff slopes bounded; a loss bounded only by a price of zero is not
        "defined_risk": prof.defined_risk,
        "risk_shape": (
            "defined" if prof.defined_risk else "zero_bounded" if prof.bounded_only_by_zero else "unlimited"
        ),
        "net_premium": _r(prof.net_premium, 2),
        "net_vega": _r(vega, 2),
        "vols": {
            "atm_iv": _r(iv),
            "atm_strike": atm[0] if atm else None,
            "realised_20d": _r(rv20),
            "realised_60d": _r(rv60),
            "closes": len(closes),
        },
        "costs": {
            "entry": entry.to_json(),
            "square_off_at_same_prices": square_off.to_json(),
            "expected_exercise_stt": _r(stt_q if (q_ev or rn) else None, 2),
            "total_to_expiry": _r(costs_total, 2),
            # #356: the bid/ask half-spreads, paid on entry at the fills and, with p_early_exit, on the way out
            "half_spreads": {
                "entry": _r(entry_spread, 2) if halves_known else None,
                "exit_expected": _r(exit_spread, 2) if halves_known else None,
                "p_early_exit": early_exit_p,
                "theta": 1.0,
                "source": spread_src,
                "premium_at_mid": _r(premium_mid, 2),
                "share_of_premium": _r(entry_spread / premium_mid, 4)
                if spread_src == "bid/ask" and premium_mid > 0
                else None,
                "limit_share": LIQUIDITY_MAX_SPREAD_SHARE,
            },
            "total_with_spreads": _r(costs_total + entry_spread + exit_spread, 2) if halves_known else None,
            "brokerage_per_order": brokerage,
            "note": (
                "Entry charges at the chain's last prices; holding to expiry adds STT on long options "
                "that finish in the money (its model expectation is shown). Squaring off earlier costs "
                "another round of charges instead (shown at the same prices). Margin (SPAN) is not "
                "computed offline; futures settlement charges are not modelled."
            ),
        },
        # #356: the risk-neutral EV prices each leg at its own-strike IV (EV_Q = −(statutory costs + half-spreads));
        # its PoP is the one ATM-IV lognormal's. Real-world cases are σ = RV scenarios, not forecasts: no EV gates
        "risk_neutral": None
        if rn is None
        else {
            **oc(rn),
            "ev": _r(q_ev[0], 2) if q_ev else None,
            "ev_gross": _r(q_ev[0] + entry.total + q_ev[1] + exit_spread, 2) if q_ev else None,
            "ev_method": "each leg at its own-strike IV of the mid: the fair-price identity, −(statutory costs + "
            "half-spreads)",
            "pop_method": "one lognormal at the ATM IV",
        },
        "real_world": None if rw is None else {**oc(rw), "window": rw_window, "kind": SCENARIO},
        "real_world_20d": None if rw20 is None else {**oc(rw20), "kind": SCENARIO},
        "real_world_60d": None if rw60 is None else {**oc(rw60), "kind": SCENARIO},
        "model_ev": _model_ev(q_ev[0] if q_ev else None, rw20, rw60),
        "pnl_dots": dots,
        "fair_price_note": (
            "At fair prices the expected P&L of any strategy is minus its costs: with each leg at its own-strike "
            "IV, the risk-neutral EV is exactly −(statutory costs + half-spreads). A real-world EV above that "
            "needs the market's IV to be wrong in your favour; the σ = realised-volatility cases are scenarios, "
            "not forecasts, and no EV changes the verdict (model EV - not validated)."
        ),
        "expected_move": None
        if move is None
        else {"move": _r(move[0], 2), "low": _r(move[1], 2), "high": _r(move[2], 2), "vol": _r(iv)},
        "capital_check": {
            "capital": capital,
            "max_loss": _r(check.max_loss, 2),
            "pct": _r(check.pct_of_capital, 3),
            "limit_pct": max_loss_pct,
            "within": check.within,
            "message": check.message,
        },
        "drawdown": None
        if dd is None
        else {"probability": _r(dd), "threshold_pct": DRAWDOWN_PCT * 100, "repeats": DRAWDOWN_REPEATS},
        "return_denominator": _r(denom, 2),
        "iv_context": {
            "n": stats.n,
            "current": _r(stats.current, 3),
            "rank": _r(stats.rank),
            "percentile": _r(stats.percentile),
            "low": _r(stats.low, 3),
            "high": _r(stats.high, 3),
            "status": stats.status,
            "skew_25d": _r(skew, 3),
            "iv_rv_ratio": _r(iv / rv20, 3) if iv and rv20 else None,
            # which series the rank and percentile come from (#306) and its last day
            "source": (iv_source or {}).get("source", "recorded"),
            "as_of": iv_as_of.isoformat() if iv_as_of else None,
        },
        "notes": notes,
    }


def assess(
    a: dict[str, Any], *, held: bool = False, defined_risk_only: bool = True
) -> tuple[str, list[Factor], list[str]]:
    """The four checks of the module docstring → (action, factors, failed check names)."""
    f: list[Factor] = []
    failed: list[str] = []

    def gate(name: str, ok: bool | None, value: Any, why: str, source: str, unit: str | None = None) -> None:
        # each hard check moves the score by ±25; an unknown answer counts as a failure (no ENTER without it)
        f.append(Factor(name, value, 25.0 if ok else -25.0, why, source, unit))
        if not ok:
            failed.append(name)

    # #351: both payoff slopes must be bounded; a missing classification counts as undefined (unknown is never safe)
    defined = a.get("defined_risk") is True and a["max_loss"] is not None
    value = ("unlimited loss" if a["max_loss"] is None else
             f"max loss ₹{_inr(-a['max_loss'])}" if defined else
             f"bounded only by a price of zero: ₹{_inr(-a['max_loss'])}")  # fmt: skip
    gate("Defined risk", defined or not defined_risk_only, value,
         "Only strategies whose worst case is capped by a bought option pass (no naked short options, no put ratio, "
         "no short call against a long future): a loss that stops only at a price of zero is not defined risk. Your "
         "profile allows defined-risk strategies only." if defined_risk_only else
         "Your profile allows undefined-risk strategies.", "fincalc:options.profile")  # fmt: skip
    # #356: spreads bind through liquidity (live bid/ask only: an estimate or an unknown spread never passes)
    hs = a["costs"]["half_spreads"]
    share = hs["share_of_premium"]
    gate(LIQUIDITY_CHECK, share is not None and share <= LIQUIDITY_MAX_SPREAD_SHARE,
         None if share is None else round(share * 100, 1),
         f"Σ half the bid/ask spread of every leg (₹{_inr(hs['entry'] or 0)}) against the premium at mids "
         f"(₹{_inr(hs['premium_at_mid'] or 0)}): crossing wide spreads costs more than the trade can earn. At most "
         f"{LIQUIDITY_MAX_SPREAD_SHARE:.0%} (an assumption)." + ("" if share is not None else
         f" Spreads are {hs['source']}: unknown, so it fails."), "NSE option chain bid/ask", "% of premium")  # fmt: skip
    cc = a["capital_check"]
    gate(
        "Max loss within your F&O budget",
        cc["within"],
        None if cc["pct"] is None else round(cc["pct"], 2),
        cc["message"] + " Budget = fno_max_loss_pct × fno_capital_inr in the profile.",
        "profile",
        "% of capital",
    )
    ivc = a["iv_context"]
    seller = (a["net_vega"] or 0) < 0
    if ivc["percentile"] is not None:
        ok, value, how = (
            (ivc["percentile"] >= 0.5) if seller else (ivc["percentile"] <= 0.5),
            round(ivc["percentile"] * 100),
            (
                f"IV percentile of the 30-day IV over {ivc['n']} days of NSE's F&O bhavcopy (as of {ivc['as_of']})"
                if ivc.get("source") == "bhavcopy"
                else f"IV percentile over {ivc['n']} recorded days"
            ),
        )
        unit = "percentile"
    elif ivc["iv_rv_ratio"] is not None:
        ok, value, how = (
            (ivc["iv_rv_ratio"] > 1) if seller else (ivc["iv_rv_ratio"] < 1),
            ivc["iv_rv_ratio"],
            f"ATM IV ÷ 20-day realised volatility (proxy: IV history is {ivc['status']})",
        )
        unit = "×"
    else:
        ok, value, how, unit = None, None, f"no IV context ({ivc['status']}, no realised volatility)", None
    gate("IV context fits the strategy", ok, value,
         f"{'Net short volatility (selling premium) wants IV high' if seller else 'Net long volatility (buying options) wants IV low'}; "
         f"measured by {how}. A high-IV premium sale is an uncertain variance-risk-premium harvest with tail risk, "
         "not an edge.", "fincalc:volatility", unit)  # fmt: skip
    # #356 (MF1): the model EV is shown, labelled, and scores nothing: it cannot change a verdict
    rw, rn = a["real_world"], a["risk_neutral"]
    f.append(Factor(EV_INFO, None if rw is None else rw["ev"], 0.0,
                    f"Real-world {SCENARIO}: the expected P&L after statutory costs and half-spreads if the price moves "
                    f"with its realised volatility ({(rw or {}).get('window', 'n/a')}, the less favourable of 20 and 60 "
                    f"days; drift {a['drift'] * 100:.1f}% a year). At fair prices it would be "
                    f"₹{_inr(rn['ev']) if rn and rn['ev'] is not None else 'unknown'} (own-strike IVs). "
                    f"{MODEL_EV_LABEL}: it is not a gate. {RV_SCENARIO_NOTE}", "fincalc:options.outcome", "₹"))  # fmt: skip
    if a["days_to_expiry"] <= 0:
        gate("Not a same-day expiry", False, "expires today",
             "SEBI found 59% of index-option turnover in contracts expiring the same day, where most traders lose.",
             SEBI_STUDY_URL)  # fmt: skip
    hard = {"Defined risk", "Max loss within your F&O budget", "Not a same-day expiry"}
    # #356: a model EV no longer exits a held position; only a hard check does
    action = ("EXIT" if hard & set(failed) else "HOLD") if held else ("WAIT" if failed else "ENTER")
    return action, f, failed


def _factor_info(a: dict[str, Any]) -> list[Factor]:
    """Informational factors (no score contribution): both probabilities, costs, expected move, IV rank."""
    rn, rw = a["risk_neutral"], a["real_world"]
    em, ivc = a["expected_move"], a["iv_context"]
    pct = lambda x: None if x is None else round(x * 100, 1)  # noqa: E731
    out = [
        Factor("Chance of profit, risk-neutral", pct(rn and rn["pop"]), 0.0,
               "Model probability that the P&L after costs is positive at expiry, with σ = ATM implied volatility and "
               "drift = the risk-free rate (N(d₂)-based). What option prices imply, not a forecast.", "fincalc:options.prob_positive", "%"),
        Factor("Chance of profit, real-world", pct(rw and rw["pop"]), 0.0,
               f"The same with σ = realised volatility ({(rw or {}).get('window', 'n/a')} window, the less favourable of 20 "
               f"and 60 days) and a stated drift of {a['drift'] * 100:.1f}% a year "
               "(no directional view). A model probability, not a forecast.", "fincalc:options.prob_positive", "%"),
        Factor("Costs to expiry", a["costs"]["total_to_expiry"], 0.0,
               "STT, exchange charges, SEBI fee, stamp duty, GST and brokerage on entry, plus expected STT on exercise.",
               "fincalc:charges", "₹"),
    ]  # fmt: skip
    if em:
        out.append(Factor("Expected move to expiry (1σ)", em["move"], 0.0,
                          f"±S·σ·√T with the ATM IV: about two in three outcomes land between {_inr(em['low'])} and "
                          f"{_inr(em['high'])} under the model.", "fincalc:options.expected_move", "points"))  # fmt: skip
    out.append(Factor("IV rank", None if ivc["rank"] is None else round(ivc["rank"] * 100), 0.0,
                      # #354 b: it ranks a stored previous close (bhavcopy 30-day IV or recorded ATM IV), not the live IV
                      f"Where the {'NSE F&O bhavcopy 30-day IV' if ivc.get('source') == 'bhavcopy' else 'recorded ATM IV'}"
                      f" of the {ivc.get('as_of') or 'latest stored'} close sits between its stored low and high (IVR), "
                      f"not today's live IV. {ivc['status'].capitalize()}.",
                      "fincalc:volatility.iv_stats", "%"))  # fmt: skip
    if ivc["skew_25d"] is not None:
        out.append(Factor("25-delta skew", ivc["skew_25d"], 0.0, "25-delta put IV minus 25-delta call IV: how much "
                          "more the market charges for downside protection.", "fincalc:volatility.skew_25d", "vol pts"))  # fmt: skip
    return out


def caveats(a: dict[str, Any], notice: dict[str, str]) -> list[str]:
    c = [f"{notice['text']} Source: {notice['source']} ({notice['date']}), {notice['url']}",
         "No edge is claimed: this checks risk, costs and fit. Evidence says most retail F&O traders lose, mainly "
         "through costs and option buying; high-IV premium selling is an uncertain variance-risk-premium harvest "
         "with tail risk, and no India-specific study of it was verified.",
         "Probabilities come from a lognormal model; real prices have fat tails and jumps (gaps on results, budget "
         "and global news), so extreme losses are more likely than the model says."]  # fmt: skip
    unconfirmed = [r for r in a["costs"]["entry"]["rates"] if r["status"] == "unconfirmed"]
    if unconfirmed:
        c.append("STT uses the Finance Bill 2026 rates (0.15% on option sales and exercise), whose enactment is not "
                 "confirmed; the rates are configurable.")  # fmt: skip
    dd = a["drawdown"]
    cc = a["capital_check"]
    if cc["pct"] is not None and not cc["within"]:
        c.append(f"Risk of ruin: one maximum loss is {cc['pct']:.1f}% of your F&O capital, above your "
                 f"{cc['limit_pct']:g}% limit." + (f" Repeating this {dd['repeats']} times, the model puts the chance "
                 f"of a {dd['threshold_pct']:.0f}% drawdown at {dd['probability'] * 100:.0f}%." if dd else ""))  # fmt: skip
    elif dd and dd["probability"] >= 0.05:
        c.append(f"Repeating this {dd['repeats']} times, the model puts the chance of a {dd['threshold_pct']:.0f}% "
                 f"drawdown of your F&O capital at {dd['probability'] * 100:.0f}%.")  # fmt: skip
    if a["max_loss"] is None:
        c.append("Unlimited loss: a naked short option can lose far more than the premium received.")
    elif a.get("risk_shape") == "zero_bounded":
        c.append(f"Not defined risk: the loss is bounded only by a price of zero (₹{_inr(-a['max_loss'])}); nothing "
                 "bought caps it on the way down.")  # fmt: skip
    c.append("Margin (SPAN + exposure) is not computed here; check your broker's margin calculator.")
    return c


# --------------------------------------------------------------------------- data access (injectable for tests)
CLIENT = None  # () -> async context manager with NseFno's methods; None = the live NSE client
_CLOSES: dict[tuple[str, date], tuple[float, list[float]]] = {}


def _client():
    if CLIENT is not None:
        return CLIENT()
    from finresearch.adapters.nse_fno import NseFno

    return NseFno()


async def underlying_closes(f, symbol: str, today: date) -> list[float]:
    """About 90 trading days of closes (cached 30 minutes); [] when NSE has none or refuses."""
    key = (symbol, today)
    hit = _CLOSES.get(key)
    if hit and hit[0] > time.time() - 1800:
        return hit[1]
    try:
        rows = await f.closes(symbol, today - timedelta(days=140), today)
    except Exception:
        return []
    closes = [float(c) for _, c in rows]
    _CLOSES[key] = (time.time(), closes)
    return closes


BHAV_METHOD = ("30-day constant-maturity ATM IV from NSE's end-of-day F&O bhavcopy (fincalc.fno_history): Black-76 on "
               "each expiry's forward (its future's settlement price, else put-call parity, else spot × e^(rT)) with "
               "settlement prices of traded contracts, total variance interpolated to 30 days; as of the last "
               "published bhavcopy (the previous close until about 19:30 IST).")  # fmt: skip
LIVE_METHOD = ("ATM IV = mean of call and put IV at the strike nearest the underlying, nearest expiry at least 7 days "
               "away, recorded after the close by the monitor from NSE's option chain.")  # fmt: skip


def iv_series_info(symbol: str) -> tuple[list[tuple[date, float, float | None]], dict[str, Any]]:
    """The daily IV series that IV rank and percentile use, oldest first, with its source (#306): the bhavcopy-derived
    30-day IV once it has MIN_DAYS days (available as soon as `finresearch fno backfill` has run), else the monitor's
    recorded ATM IV (iv_history, from 30-Sep-2026). The two are never spliced: different contracts and pricing."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod, IvHistory

    sym = symbol.upper()
    try:
        with session_scope() as s:
            bhav = s.execute(select(FnoIvEod.day, FnoIvEod.iv_30d, FnoIvEod.skew_25d)
                             .where(FnoIvEod.symbol == sym, FnoIvEod.iv_30d.is_not(None))
                             .order_by(FnoIvEod.day)).all()  # fmt: skip
    except SQLAlchemyError:  # the #306 migration has not run on this database yet
        bhav = []
    if len(bhav) >= vol.MIN_DAYS:
        rows = [(d, float(v), None if k is None else float(k)) for d, v, k in bhav]
        return rows, {"source": "bhavcopy", "label": "NSE F&O bhavcopy, 30-day IV", "method": BHAV_METHOD,
                      "url": "https://nsearchives.nseindia.com/content/fo/"}  # fmt: skip
    try:
        with session_scope() as s:
            live = s.execute(select(IvHistory.day, IvHistory.atm_iv, IvHistory.skew_25d)
                             .where(IvHistory.symbol == sym).order_by(IvHistory.day)).all()  # fmt: skip
    except SQLAlchemyError:
        live = []
    rows = [(d, float(v), None if k is None else float(k)) for d, v, k in live]
    return rows, {"source": "recorded", "label": "recorded daily ATM IV", "method": LIVE_METHOD,
                  "url": "https://www.nseindia.com/option-chain"}  # fmt: skip


def iv_series(symbol: str) -> list[tuple[date, float, float | None]]:
    """The IV series for rank and percentile (see iv_series_info), oldest first; [] when no table exists yet."""
    return iv_series_info(symbol)[0]


def load_profile():
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import load_profile as _load

    with session_scope() as s:
        return _load(s)


class ExpiredContract(ValueError):
    """A contract whose expiry has passed: nothing is left to trade or analyse (#293)."""


def live_expiries(expiries: list[date], today: date) -> list[date]:
    """The expiries still tradable on `today` (an expiry today still trades until the close), soonest first. NSE's and
    Groww's contract lists can keep a just-expired date for a while."""
    return sorted(e for e in expiries if e >= today)


def check_expiry(expiry: date, today: date) -> date:
    if expiry < today:
        raise ExpiredContract(f"the {expiry:%d-%b-%Y} contract expired before today ({today:%d-%b-%Y})")
    return expiry


# --------------------------------------------------------------------------- the provider
@register("fno")
async def fno_signal(instrument: str, ctx: dict[str, Any]) -> Signal:
    from finresearch.adapters.nse_fno import lot_size_for
    from finresearch.fincalc.dates import now_ist

    sym = instrument.upper()
    now = now_ist()
    today = now.date()
    notice = risk_notice()
    async with _client() as f:
        if ctx.get("expiry"):
            expiry = check_expiry(date.fromisoformat(ctx["expiry"]), today)
        else:
            expiries, _ = await f.contract_info(sym)
            expiries = live_expiries(expiries, today)
            if not expiries:
                raise LookupError(f"no F&O expiries for {sym} on or after {today}")
            expiry = expiries[0]
        chain = await f.option_chain(sym, expiry)
        try:
            lot = lot_size_for(await f.lot_sizes(), sym, expiry)
        except Exception:
            lot = None
        closes = await underlying_closes(f, sym, today)
    method = ("rule-based risk/cost/fit gate v2: defined risk, bid/ask spreads within 10% of the premium, max loss "
              "within the profile's F&O budget, IV context; model EV shown, not a gate (#356); lognormal model (roadmap "
              "§D.5). Return range = real-world P&L quantiles after costs and spreads as a fraction of the capital at "
              "risk (the maximum loss)")  # fmt: skip
    validation = Validation("rule_based", 0, {}, "Not validated: no edge is claimed, so there is nothing to backtest "
                            "yet. The checks are fixed rules drawn from the roadmap's evidence.")  # fmt: skip

    def no_signal(reason: str) -> Signal:
        return Signal(asset="fno", instrument=sym, name=None, action="NO_SIGNAL", score=0.0, event=reason,
                      horizon=f"until expiry ({expiry:%d-%b-%Y})", method=method, validation=validation,
                      caveats=[reason, f"{notice['text']} Source: {notice['source']} ({notice['date']})."],
                      sources=[notice["url"], "https://www.nseindia.com/option-chain"],
                      as_of=chain.as_of or datetime.now(tz=now_ist().tzinfo))  # fmt: skip

    if chain.underlying is None or not chain.rows:
        return no_signal(f"NSE returned no option chain for {sym} {expiry:%d-%b-%Y}")
    if not lot:
        return no_signal(f"no NSE lot size for {sym} {expiry:%b-%y}")
    legs = parse_legs(ctx.get("legs", "")) or (preset_legs(chain, ctx["preset"]) if ctx.get("preset") else [])
    if not legs:
        return no_signal("no strategy: pass legs (or a preset) to assess")
    profile = load_profile()
    capital = float(profile.fno_capital_inr)
    if capital <= 0:
        return no_signal("F&O capital is not set in your profile, so the maximum loss cannot be checked against a "
                         "budget. Set it (and the % limit) on the profile page.")  # fmt: skip
    series, src = iv_series_info(sym)
    drift = float(ctx["drift"]) if ctx.get("drift") not in (None, "") else None
    from finresearch.fincalc.dates import years_to_expiry
    from finresearch.signals.rates import risk_free

    if years_to_expiry(expiry, now) <= 0:
        return no_signal(f"the {expiry:%d-%b-%Y} contract expired at 15:30 IST today: nothing to assess")
    rinfo = await risk_free(
        years_to_expiry(expiry, now)
    )  # FBIL par yield at the expiry (#244); T to the minute (#359)
    a = analyse(chain, lot, legs, today=today, now=now, closes=closes, iv_series=[v for _, v, _ in series], capital=capital,
                max_loss_pct=float(profile.fno_max_loss_pct), brokerage=float(profile.fno_brokerage_per_order_inr),
                rate=rinfo["rate"], drift=drift, iv_source=src,
                iv_as_of=series[-1][0] if series else None)  # fmt: skip
    a["rate_source"] = rinfo
    if a["risk_neutral"] is None:
        return no_signal(f"the chain has no ATM implied volatility for {sym} {expiry:%d-%b-%Y}")
    held = str(ctx.get("held", "")).lower() in ("1", "true", "yes")
    action, gates, failed = assess(a, held=held, defined_risk_only=profile.fno_defined_risk_only)
    factors = gates + _factor_info(a)
    rw, rn = a["real_world"], a["risk_neutral"]
    pops = [x["pop"] for x in (rn, a["real_world_20d"], a["real_world_60d"]) if x]
    denom = a["return_denominator"]
    exp_ret = {k: round(v / denom, 4) for k, v in rw["quantiles"].items()} if rw and denom else None
    # #350/#355: the trade-plan card's sizing and verdict (signals.tradeplan.decide): lots from max loss + entry costs,
    # real money only for a matched candidate spec and an ENTER, never ENTER without one; IV percentile is context
    from finresearch.fincalc import tradeplan as tp
    from finresearch.signals import tradeplan as tpl

    budget = capital * float(profile.fno_max_loss_pct) / 100
    loss = -a["max_loss"] if a["max_loss"] is not None else None
    kind = tp.classify([tp.PlanLeg(x.right, x.strike, x.side, x.lots) for x in legs])
    strategies, head = tpl.backtest_rows()
    ev = tpl.evidence_for(kind, sym, a["days_to_expiry"], strategies, head, today=today)
    # the open book (#358): open paper trades and Groww positions, month-to-date P&L; the same cap as the trade card
    from finresearch.fincalc import options as o
    from finresearch.fnobook.events import is_index

    one = [o.Leg(x.right, x.strike, al["premium"], al["qty"]) if x.right != "future"  # type: ignore[arg-type]
           else o.Leg("future", al["premium"], 0.0, al["qty"]) for x, al in zip(legs, a["legs"], strict=True)]  # fmt: skip
    terms = tpl.trade_terms(sym, expiry, float(chain.underlying), one, now, rinfo["rate"], is_index(sym))
    ob = tpl.book_for(await tpl.load_book(today), capital, sym, kind, float(chain.underlying),
                      ev["spec"] if ev["match"] == "exact" else None, terms)  # fmt: skip
    # legs that are exactly a library recipe get its registered IV filter, as on the trade card (#355)
    from finresearch.signals import fno_context

    extra = (tpl.recipe_checks(ev["spec"], strategies, fno_context.iv_rank_input(sym)[0])
             if ev["match"] == "exact" else [])  # fmt: skip
    d = tpl.decide(
        failed,
        a["max_loss"],
        a["costs"]["entry"]["total"],
        budget,
        ev["level"],
        extra,
        book=ob,
        stale=ev["stale"],
        mu_lo=ev["mu_lo"],
    )
    sz, qualifier = d.size, None
    if not held:  # the trade-plan card's verdict for the same legs (#355: one engine)
        action, qualifier, failed = d.action, d.qualifier, d.failed
    sizing = {"max_multiple_of_these_legs": sz.multiple if action in ("ENTER", "PAPER", "HOLD") else 0,
              "paper_lots": sz.suggested, "real_lots": sz.real_lots if action == "ENTER" else 0,
              "budget_inr": round(budget, 2), "max_loss_inr": None if loss is None else round(loss, 2),
              "evidence": {k: v for k, v in ev.items() if k != "spec"}, "verdict_label": qualifier,
              "open_book": tpl.book_json(ob),
              "reason": (f"Your budget per strategy is {float(profile.fno_max_loss_pct):g}% of ₹{_inr(capital)} = ₹{_inr(budget)}; "
                         + (f"{sz.reason}." if loss else "the loss of these legs is unlimited, so no size fits."))}  # fmt: skip
    caveat = caveats(a, notice)
    caveat.append(f"Risk-free rate {rinfo['rate']:.2%} (continuous): {rinfo['source']}; {rinfo['note']}.")
    caveat.append(backtest_caveat())
    if ev["stale"]:  # #379: the matched backtest evidence is stale, so it is read as unvalidated
        caveat.insert(0, "Backtest evidence is stale: " + "; ".join(ev["stale"]) + ".")
    if failed:
        caveat.insert(0, "Not passing: " + "; ".join(failed) + ".")
    return Signal(
        asset="fno", instrument=sym, name=f"{sym} {expiry:%d-%b-%Y} · {len(legs)}-leg strategy", action=action,
        score=clip_score(sum(x.contribution for x in gates)),
        event="the strategy's P&L after costs is positive at expiry (real-world model)",
        horizon=f"until expiry ({expiry:%d-%b-%Y}, {a['days_to_expiry']} days)", method=method, validation=validation,
        probability=rw["pop"] if rw else None,
        probability_interval=(min(pops), max(pops)) if len(pops) > 1 else None,
        probability_interval_kind="model_span",  # #357: IV vs 20/60-day realised volatility, not a confidence interval
        expected_return=exp_ret,
        factors=factors, caveats=caveat, sizing=sizing,
        sources=[notice["url"], "https://www.nseindia.com/option-chain", ch.BUDGET_2026, ch.ZERODHA_CHARGES],
        as_of=chain.as_of,
    )  # fmt: skip


# --------------------------------------------------------------------------- backtest evidence (#307)
BACKTEST_NOTE = ("A 'candidate' passed every criterion pre-registered before its out-of-sample run (specs.py, "
                 "evals/experiments/fno_backtest/PREREG.md). It is evidence from the past with stated limits, not a "
                 "guarantee: SEBI found most retail F&O traders lose.")  # fmt: skip


def backtest_evidence(with_setups: bool = True) -> dict[str, Any] | None:
    """The latest saved backtest (finresearch.backtest.store) with each strategy's status; for each candidate, its
    current setup (the rule's legs at the last stored close of the app's F&O history). None when no run is saved."""
    from dataclasses import replace

    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.backtest import setup, specs
    from finresearch.backtest import store as bstore
    from finresearch.backtest.dbhistory import DbHistory
    from finresearch.db import session_scope

    try:
        with session_scope() as s:
            run = bstore.latest_run(s)
            rep = bstore.run_json(s, run) if run else None
    except SQLAlchemyError:  # the #307 migration has not run on this database yet
        return None
    if rep is None:
        return None
    candidates = []
    for r in rep["strategies"]:
        if r["status"] != "candidate":
            continue
        c = {"key": r["key"], "name": r["name"], "oos": (r.get("windows") or {}).get("oos"),
             "criteria": r.get("criteria"), "setup": None}  # fmt: skip
        if with_setups:
            try:
                spec = replace(specs.by_key(r["key"]), underlying=r["underlying"])
                c["setup"] = setup.current_setup(spec, DbHistory())
            except (KeyError, SQLAlchemyError, ValueError) as e:
                c["setup"] = {
                    "status": "unknown",
                    "reason": f"setup could not be computed: {type(e).__name__}",
                }
        candidates.append(c)
    rep["candidates"] = candidates
    rep["note"] = BACKTEST_NOTE
    from finresearch.fincalc.dates import today_ist
    from finresearch.inventory import provenance, regimes

    stale = regimes.staleness(regimes.run_day(rep.get("created_at")), today_ist())
    # #379: stale evidence reads as unvalidated (a card matched to it waits); a code change is shown, not a gate
    rep["evidence_status"] = {"current": not stale, "stale": stale, "code_changed": provenance.code_changed(rep),
                              "max_age_days": regimes.MAX_EVIDENCE_AGE_DAYS}  # fmt: skip
    return rep


def backtest_caveat() -> str:
    """One line for the strategy signal: what the pre-registered backtest found (or that none is saved)."""
    ev = backtest_evidence(with_setups=False)
    if not ev:
        return "No pre-registered backtest is saved yet (finresearch fno backtest run): no strategy has historical evidence."
    n = len(ev["candidates"])
    return (f"Pre-registered backtest ({ev['source']}, {ev['variants_tried']} variants): {n} candidate"
            f"{'' if n == 1 else 's'} out of sample" + (f" ({', '.join(c['key'] for c in ev['candidates'])})" if n else
            "; nothing passed") + ". See /fno/backtests.")  # fmt: skip
