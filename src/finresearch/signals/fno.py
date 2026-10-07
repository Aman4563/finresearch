"""F&O strategy signal: a risk, cost and fit assessment, not an edge (docs/dev/RESEARCH_ROADMAP.md §D.5).

The app has no edge-generating F&O signal, and the evidence says most individual traders lose, mainly through costs and
option buying (SEBI, 20-Aug-2026 [P30]). So the "signal" asks four questions about a proposed strategy and says ENTER
only when every answer is acceptable:

1. Defined risk: the maximum loss is finite (no naked short options).
2. Costs: the expected P&L after costs under the real-world model (σ = realised volatility, the less favourable of the
   20- and 60-day windows; drift μ stated, default the risk-free rate, i.e. no directional view) is no worse
   than the fair-price expectation, which is minus the costs (EV_rw ≥ −costs).
3. Budget: the maximum loss is within the profile's F&O risk budget (fno_max_loss_pct of fno_capital_inr).
4. IV context fits the strategy: premium sellers (net short vega) want IV high, buyers want it low. IV percentile
   over the recorded history once there are 60 days; until then the ratio of ATM IV to 20-day realised volatility is
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
EV_TOLERANCE = 1.0  # EV_rw must be ≥ −EV_TOLERANCE × costs
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
def _r(x: float | None, d: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(x, d)


def analyse(chain, lot: int, legs_in: list[LegIn], *, today: date, closes: list[float], iv_series: list[float],
            capital: float, max_loss_pct: float, brokerage: float, rate: float = RATE, drift: float | None = None,
            overrides: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Everything the page and the signal show for one strategy. Raises ValueError when a leg has no premium."""
    spot = float(chain.underlying)
    days = (chain.expiry - today).days
    t = max(days, 0.5) / 365
    mu = rate if drift is None else drift
    by_strike = {float(r.strike): r for r in chain.rows}
    legs, orders, notes = [], [], []
    vega = 0.0
    for x in legs_in:
        qty = x.lots * lot * (1 if x.side == "buy" else -1)
        if x.right == "future":
            legs.append(o.Leg("future", x.strike, 0.0, qty))
            orders.append(ch.TradeLeg("future", x.side, x.strike, abs(qty)))
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

    def run(sigma: float | None, m: float) -> o.Outcome | None:
        return o.outcome(legs, spot, t, sigma, m, rate, entry.total, exit_cost) if sigma else None

    rn = run(iv, rate)
    rw20 = run(rv20, mu)
    rw60 = run(rv60, mu)
    # the headline real-world case is the less favourable of the two realised-volatility windows (lower EV):
    # a low 20-day volatility flatters premium sellers, a high one flatters buyers
    rw = min((x for x in (rw20, rw60) if x), key=lambda x: x.ev, default=None)
    rw_window = None if rw is None else ("20-day" if rw is rw20 else "60-day")
    costs_total = entry.total + (rn.exit_cost if rn else 0.0)
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
        "legs": [{"right": x.right, "strike": x.strike, "premium": x.premium, "qty": x.qty} for x in legs],
        "breakevens": prof.breakevens,
        "max_profit": _r(prof.max_profit, 2),
        "max_loss": _r(prof.max_loss, 2),
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
            "expected_exercise_stt": _r(rn.exit_cost if rn else None, 2),
            "total_to_expiry": _r(costs_total, 2),
            "brokerage_per_order": brokerage,
            "note": (
                "Entry charges at the chain's last prices; holding to expiry adds STT on long options "
                "that finish in the money (its model expectation is shown). Squaring off earlier costs "
                "another round of charges instead (shown at the same prices). Margin (SPAN) is not "
                "computed offline; futures settlement charges are not modelled."
            ),
        },
        "risk_neutral": oc(rn),
        "real_world": None if rw is None else {**oc(rw), "window": rw_window},
        "real_world_20d": oc(rw20),
        "real_world_60d": oc(rw60),
        "fair_price_note": (
            "At fair prices the expected P&L of any strategy is minus its costs: under the "
            "risk-neutral model EV ≈ −costs by construction. A real-world EV above that needs the "
            "market's IV to be wrong in your favour."
            + (
                f" Here the risk-neutral EV before costs is ₹{_inr(rn.ev_gross)}, not 0, because the chain's last "
                "prices differ from one-σ (ATM IV) model prices: skew across strikes and stale last trades."
                if rn and abs(rn.ev_gross) > max(1.0, 0.01 * abs(prof.net_premium))
                else ""
            )
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

    unlimited = a["max_loss"] is None
    gate("Defined risk", not unlimited or not defined_risk_only, "unlimited loss" if unlimited else f"max loss ₹{_inr(-a['max_loss'])}",
         "Only strategies whose worst case is known in advance pass (no naked short options): your profile allows "
         "defined-risk strategies only." if defined_risk_only else "Your profile allows undefined-risk strategies.",
         "fincalc:options.profile")  # fmt: skip
    rw, costs = a["real_world"], a["costs"]["total_to_expiry"] or 0.0
    gate("EV after costs (real-world)", None if rw is None else rw["ev"] >= -EV_TOLERANCE * costs,
         None if rw is None else rw["ev"],
         f"Expected P&L after ₹{_inr(costs)} of costs if the price moves with its realised volatility "
        f"({(rw or {}).get('window', 'n/a')}, the less favourable of 20 and 60 days) "
         f"(drift {a['drift'] * 100:.1f}% a year). It passes when it is no worse than the fair-price expectation, "
         "minus the costs." + ("" if rw else " No realised volatility: unknown, so it fails."),
         "fincalc:options.outcome", "₹")  # fmt: skip
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
            f"IV percentile over {ivc['n']} recorded days",
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
    if a["days_to_expiry"] <= 0:
        gate("Not a same-day expiry", False, "expires today",
             "SEBI found 59% of index-option turnover in contracts expiring the same day, where most traders lose.",
             SEBI_STUDY_URL)  # fmt: skip
    hard = {"Defined risk", "Max loss within your F&O budget", "Not a same-day expiry"}
    if held:
        action = "EXIT" if hard & set(failed) or "EV after costs (real-world)" in failed else "HOLD"
    else:
        action = "WAIT" if failed else "ENTER"
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
                      f"Where today's ATM IV sits between its recorded low and high (IVR). {ivc['status'].capitalize()}.",
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


def iv_series(symbol: str) -> list[tuple[date, float, float | None]]:
    """The recorded daily ATM IV (and skew) for `symbol`, oldest first; [] when the table does not exist yet."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope
    from finresearch.db.models import IvHistory

    try:
        with session_scope() as s:
            rows = s.execute(select(IvHistory.day, IvHistory.atm_iv, IvHistory.skew_25d)
                             .where(IvHistory.symbol == symbol.upper()).order_by(IvHistory.day)).all()  # fmt: skip
    except SQLAlchemyError:
        return []
    return [(d, float(v), None if k is None else float(k)) for d, v, k in rows]


def load_profile():
    from finresearch.db import session_scope
    from finresearch.suggest.advisor import load_profile as _load

    with session_scope() as s:
        return _load(s)


# --------------------------------------------------------------------------- the provider
@register("fno")
async def fno_signal(instrument: str, ctx: dict[str, Any]) -> Signal:
    from finresearch.adapters.nse_fno import lot_size_for
    from finresearch.fincalc.dates import now_ist

    sym = instrument.upper()
    today = now_ist().date()
    notice = risk_notice()
    async with _client() as f:
        if ctx.get("expiry"):
            expiry = date.fromisoformat(ctx["expiry"])
        else:
            expiries, _ = await f.contract_info(sym)
            if not expiries:
                raise LookupError(f"no F&O expiries for {sym}")
            expiry = expiries[0]
        chain = await f.option_chain(sym, expiry)
        try:
            lot = lot_size_for(await f.lot_sizes(), sym, expiry)
        except Exception:
            lot = None
        closes = await underlying_closes(f, sym, today)
    method = ("rule-based risk/cost/fit gate v1: defined risk, EV after costs under realised volatility, max loss "
              "within the profile's F&O budget, IV context; lognormal model (roadmap §D.5). Return range = real-world P&L "
              "quantiles after costs as a fraction of the capital at risk (the maximum loss)")  # fmt: skip
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
    series = iv_series(sym)
    drift = float(ctx["drift"]) if ctx.get("drift") not in (None, "") else None
    from finresearch.signals.rates import risk_free

    rinfo = await risk_free(max((expiry - today).days, 0.5) / 365)  # FBIL par yield at the expiry (#244)
    a = analyse(chain, lot, legs, today=today, closes=closes, iv_series=[v for _, v, _ in series], capital=capital,
                max_loss_pct=float(profile.fno_max_loss_pct), brokerage=float(profile.fno_brokerage_per_order_inr),
                rate=rinfo["rate"], drift=drift)  # fmt: skip
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
    budget = capital * float(profile.fno_max_loss_pct) / 100
    loss = -a["max_loss"] if a["max_loss"] is not None else None
    multiple = math.floor(budget / loss) if loss else 0
    sizing = {"max_multiple_of_these_legs": multiple if action in ("ENTER", "HOLD") else 0, "budget_inr": round(budget, 2),
              "max_loss_inr": None if loss is None else round(loss, 2),
              "reason": (f"Your budget per strategy is {float(profile.fno_max_loss_pct):g}% of ₹{_inr(capital)} = ₹{_inr(budget)}; "
                         + (f"these legs risk ₹{_inr(loss)}, so at most {multiple}× them." if loss else
                            "the loss of these legs is unlimited, so no size fits."))}  # fmt: skip
    caveat = caveats(a, notice)
    caveat.append(f"Risk-free rate {rinfo['rate']:.2%} (continuous): {rinfo['source']}; {rinfo['note']}.")
    if failed:
        caveat.insert(0, "Not passing: " + "; ".join(failed) + ".")
    return Signal(
        asset="fno", instrument=sym, name=f"{sym} {expiry:%d-%b-%Y} · {len(legs)}-leg strategy", action=action,
        score=clip_score(sum(x.contribution for x in gates)),
        event="the strategy's P&L after costs is positive at expiry (real-world model)",
        horizon=f"until expiry ({expiry:%d-%b-%Y}, {a['days_to_expiry']} days)", method=method, validation=validation,
        probability=rw["pop"] if rw else None,
        probability_interval=(min(pops), max(pops)) if len(pops) > 1 else None,
        expected_return=exp_ret,
        factors=factors, caveats=caveat, sizing=sizing,
        sources=[notice["url"], "https://www.nseindia.com/option-chain", ch.BUDGET_2026, ch.ZERODHA_CHARGES],
        as_of=chain.as_of,
    )  # fmt: skip
