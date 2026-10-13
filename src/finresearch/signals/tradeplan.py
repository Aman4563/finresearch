"""The F&O trade-plan card (#317): for a strategy the user builds, or a pre-registered library recipe applied to today's
chain ("Suggest setups"), one card with a verdict and everything needed to act on it by hand: exact legs and strikes,
lots from the profile's risk budget, a limit-price range from bid/ask, max profit/loss, breakevens, both
probabilities of profit, the stop-loss in premium and in underlying terms, the profit target, the time exit,
adjustment and invalidation rules, and the evidence (the matching backtest result and the user's paper record).

Verdict (fincalc.tradeplan.verdict): AVOID when a hard check fails (undefined risk, over budget even at one set, a
same-day expiry) or the matching pre-registered backtest FAILED out of sample with an expectancy ≤ 0; WAIT when any
other check fails or is unknown (signals.fno.assess: EV after costs, IV context; plus the recipe's own entry filters);
ENTER only when everything passes, and without a strategy that passed out of sample it is always "ENTER (no proven
edge — size small)", sized at half the budget. The market-context indicators (signals.fno_context) never feed it.

Evidence matching: the legs' shape (fincalc.tradeplan.classify) is matched to a library spec of the same kind. The
match is exact only for the spec's own underlying inside its days-to-expiry window; any other match is shown as
related evidence and never changes the verdict either way (a decision for the user, listed in the PR).

Nothing here places an order: the card's one-click actions are a paper trade and exit-rule alerts (api/fno_book).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any

from finresearch.fincalc import options as o
from finresearch.fincalc import tradeplan as tp
from finresearch.signals import fno as fs

DELTA_ALERT = 0.30  # short-strike |delta| alert (rule of thumb: the short strike is being tested)
EVENT_ALERT_DAYS = 2


def chain_quotes(chain) -> list[tp.ChainQuote]:
    f = lambda x: None if x is None else float(x)  # noqa: E731
    out = []
    for r in chain.rows:
        for right, q in (("call", r.call), ("put", r.put)):
            if q is not None:
                out.append(tp.ChainQuote(float(r.strike), right, f(q.iv), f(q.last_price), f(q.bid), f(q.ask),
                                         f(q.oi), f(q.volume)))  # fmt: skip
    return out


def _quote(quotes: list[tp.ChainQuote], right: str, strike: float) -> tp.ChainQuote | None:
    return next((q for q in quotes if q.right == right and q.strike == strike), None)


def match_spec(kind: str, symbol: str, dte: int) -> tuple[Any | None, bool]:
    """(the library spec of this kind, exact?) — exact = the spec's own underlying and days-to-expiry window."""
    from finresearch.backtest import specs

    cands = [s for s in specs.SPECS if s.kind == kind and s.role != "overlay"]
    if not cands:
        return None, False
    exact = [s for s in cands if s.underlying == symbol.upper() and s.dte_min <= dte <= s.dte_max]
    if exact:
        return exact[0], True
    best = min(cands, key=lambda s: (s.underlying != symbol.upper(), 0 if s.dte_min <= dte <= s.dte_max else
                                     min(abs(dte - s.dte_min), abs(dte - s.dte_max))))  # fmt: skip
    return best, False


def backtest_rows() -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """The latest saved backtest's strategy rows and run header; (None, None) when none is saved."""
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.backtest import store as bstore
    from finresearch.db import session_scope

    try:
        with session_scope() as s:
            run = bstore.latest_run(s)
            rep = bstore.run_json(s, run) if run else None
    except SQLAlchemyError:
        return None, None
    if rep is None:
        return None, None
    head = {k: rep.get(k) for k in ("id", "created_at", "source", "oos_evaluated", "variants_tried")}
    return rep["strategies"], head


def paper_trades() -> list[dict[str, Any]]:
    """Every paper trade as {symbol, label, status, net_pnl, legs: [PlanLeg]} (no personal data: paper only)."""
    from sqlalchemy import select
    from sqlalchemy.exc import SQLAlchemyError

    from finresearch.db import session_scope
    from finresearch.db.models import PaperStrategy

    try:
        with session_scope() as s:
            return [{"symbol": st.symbol, "label": st.label or "", "status": st.status,
                     "net_pnl": None if st.net_pnl is None else float(st.net_pnl),
                     "legs": [tp.PlanLeg(x.right, float(x.strike), x.side, x.lots) for x in st.legs]}
                    for st in s.scalars(select(PaperStrategy))]  # fmt: skip
    except SQLAlchemyError:
        return []


def paper_record(
    trades: list[dict[str, Any]], symbol: str, kind: str, spec_key: str | None
) -> dict[str, Any]:
    """The user's paper results for this strategy: trades opened from a plan of this spec (label "plan:<key>"), or
    any paper trade on the same underlying with the same shape."""
    tag = f"plan:{spec_key}" if spec_key else None
    mine = [t for t in trades if (tag and t["label"].startswith(tag))
            or (t["symbol"] == symbol.upper() and tp.classify(t["legs"]) == kind)]  # fmt: skip
    closed = [t["net_pnl"] for t in mine if t["status"] == "closed" and t["net_pnl"] is not None]
    wins = sum(1 for x in closed if x > 0)
    return {"trades": len(mine), "open": sum(t["status"] == "open" for t in mine), "closed": len(closed),
            "wins": wins, "losses": len(closed) - wins, "total_net": round(sum(closed), 2) if closed else None,
            "expectancy": round(sum(closed) / len(closed), 2) if closed else None,
            "note": "Paper results are kinder than real fills and too few to be evidence until there are dozens of "
                    "closed trades." if closed else "No closed paper trade of this strategy yet."}  # fmt: skip


# --------------------------------------------------------------------------- the card
class Inputs:
    """Everything the card reads besides the chain: market history, the profile's F&O settings, the saved backtest,
    the paper book and the event dates (None = unknown)."""

    def __init__(self, *, today: date, closes: list[float], iv_series: list[float], capital: float, max_loss_pct: float,
                 brokerage: float, defined_risk_only: bool, rate: float, iv_src: dict[str, Any] | None = None,
                 iv_as_of: date | None = None, strategies: list[dict[str, Any]] | None = None,
                 backtest_head: dict[str, Any] | None = None, paper: list[dict[str, Any]] | None = None,
                 events: list[tuple[date, str]] | None = None, is_index: bool = True) -> None:  # fmt: skip
        self.today, self.closes, self.iv_series, self.iv_src, self.iv_as_of = (
            today,
            closes,
            iv_series,
            iv_src,
            iv_as_of,
        )
        self.capital, self.max_loss_pct, self.brokerage = capital, max_loss_pct, brokerage
        self.defined_risk_only, self.rate = defined_risk_only, rate
        self.strategies, self.backtest_head, self.paper = strategies, backtest_head, paper or []
        self.events, self.is_index = events, is_index

    @property
    def budget(self) -> float:
        return self.capital * self.max_loss_pct / 100


def _analyse(chain, lot: int, legs: list[fs.LegIn], x: Inputs) -> dict[str, Any]:
    return fs.analyse(chain, lot, legs, today=x.today, closes=x.closes, iv_series=x.iv_series, capital=x.capital,
                      max_loss_pct=x.max_loss_pct, brokerage=x.brokerage, rate=x.rate, iv_source=x.iv_src,
                      iv_as_of=x.iv_as_of)  # fmt: skip


def _evidence(kind: str, symbol: str, dte: int, x: Inputs, spec: Any | None) -> dict[str, Any]:
    exact = spec is not None and spec.underlying == symbol.upper()
    if spec is None:
        spec, exact = match_spec(kind, symbol, dte)
    row = next((r for r in x.strategies or [] if spec and r.get("key") == spec.key), None)
    level = tp.evidence_level(row)
    out = {"spec_key": spec.key if spec else None, "spec_name": spec.name if spec else None,
           "match": None if spec is None else ("exact" if exact else "related"), "status": (row or {}).get("status"),
           "level": level if exact else "none", "raw_level": level,
           "text": tp.evidence_text(row, level) if spec else tp.evidence_text(None, "none"),
           "run": x.backtest_head, "url": "/fno/backtests"}  # fmt: skip
    if spec is not None and not exact:
        out["text"] = (f"Related only ({spec.name}, not this underlying or expiry window): " + out["text"]
                       + " It does not change the verdict.")  # fmt: skip
    if x.strategies is None:
        out["text"] = "No pre-registered backtest is saved yet: no strategy has historical evidence."
    out["spec"] = spec
    return out


def _time_exit(plan: tp.ExitPlan, expiry: date) -> dict[str, Any]:
    if plan.hold_to_expiry:
        return {"dte": 0, "by": expiry.isoformat(), "text": f"Hold to expiry ({expiry:%d-%b-%Y}), as the recipe does: "
                "long options that finish in the money are exercised and pay STT on their intrinsic value."}  # fmt: skip
    if plan.exit_dte is not None:
        by = expiry - timedelta(days=plan.exit_dte)
        return {"dte": plan.exit_dte, "by": by.isoformat(),
                "text": f"Close by {by:%d-%b-%Y} ({plan.exit_dte} days before expiry), win or lose: gamma risk "
                        "grows fast in the last week."}  # fmt: skip
    return {"dte": 1, "by": (expiry - timedelta(days=1)).isoformat(),
            "text": f"Close on the last session before expiry day ({expiry:%d-%b-%Y}) at the latest: SEBI found most "
                    "losses in same-day-expiry trading."}  # fmt: skip


def _exit_rules(plan: tp.ExitPlan, kind: str, legs: list[fs.LegIn], is_index: bool) -> list[dict[str, Any]]:
    """The alerts the one-click action arms (api/fno_book exit-rule kinds; each raises one alert, never trades)."""
    out = []
    if plan.profit_pct is not None:
        out.append({"kind": "profit", "threshold": plan.profit_pct, "why": "profit target"})
    if plan.stop_pct is not None:
        out.append({"kind": "stop", "threshold": plan.stop_pct, "why": "stop-loss"})
    dte = plan.exit_dte if plan.exit_dte is not None else (None if plan.hold_to_expiry else 1)
    if dte is not None:
        out.append({"kind": "dte", "threshold": dte, "why": "time exit"})
    if any(x.side == "sell" and x.right != "future" for x in legs):
        out.append(
            {
                "kind": "delta",
                "threshold": DELTA_ALERT,
                "why": "a short strike is being tested (rule of thumb)",
            }
        )
    if not is_index:
        out.append({"kind": "event", "threshold": EVENT_ALERT_DAYS, "why": "results before expiry"})
    return out


ADJUST = {
    "credit": ["Do not add to a losing short-premium position: close it at the stop instead of averaging.",
               "Rolling out (close, reopen the same structure at a later expiry for a credit) only if the new trade "
               "passes this card's checks on its own; never roll to avoid booking a loss.",
               "If one side of an iron condor is tested, closing that side and keeping the other is the simple "
               "adjustment; it does not reduce the loss already taken."],
    "debit": ["Do not average down: the debit paid is the planned risk.",
              "At +50% you may close half and let the rest run with the stop moved to breakeven (rule of thumb)."],
    "future": ["Do not add to a losing futures position; the stop is the underlying level shown."],
}  # fmt: skip
ADJUST_NOTE = "Rules of thumb, not backtested: no adjustment rule has evidence in this app."


def _invalidation(kind: str, legs: list[fs.LegIn], spot: float, events: list[tuple[date, str]] | None,
                  today: date, expiry: date, is_index: bool) -> list[str]:  # fmt: skip
    out = []
    shorts = [x for x in legs if x.side == "sell" and x.right != "future"]
    for x in shorts:
        side = "below" if x.right == "put" else "above"
        out.append(f"The underlying closes {side} the short {x.right} strike {x.strike:,.0f} "
                   f"(spot {spot:,.0f} now): the trade's premise has failed, exit rather than hope.")  # fmt: skip
    longs = [x for x in legs if x.side == "buy" and x.right != "future"]
    if longs and not shorts:
        out.append(
            "The move you bought does not come before the time exit: time decay is working against you."
        )
    out.append("Implied volatility jumps (a market shock) while you are short premium, or collapses while you are "
               "long it." if shorts else "Implied volatility collapses (e.g. after an event) while you hold options.")  # fmt: skip
    if events is None:
        out.append("Results / RBI dates before expiry are unknown here: check them yourself before entering."
                   if not is_index else "RBI policy dates have no source in the app: check them before entering.")  # fmt: skip
    else:
        for d, what in events:
            if today <= d <= expiry:
                out.append(f"{what} on {d:%d-%b-%Y}, before expiry: a gap can jump past any stop.")
    return out


def _leg_iv(q: tp.ChainQuote | None, right: str, price: float, spot: float, strike: float, t: float,
            rate: float) -> float | None:  # fmt: skip
    if right == "future":
        return None
    if q and q.iv:
        return q.iv / 100
    return o.implied_vol(right, price, spot, strike, t, rate)  # type: ignore[arg-type]


def build(chain, lot: int, legs_in: list[fs.LegIn], x: Inputs, *, spec: Any | None = None,
          extra_failed: list[tuple[str, str]] | None = None, title: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The trade-plan card for `legs_in` (the base set: its lots are the ratio, the budget sets the multiple). `spec`
    = the library recipe in "Suggest setups"; `extra_failed` = the recipe's own entry checks that failed (name, why).
    Raises ValueError when a leg has no price (signals.fno.analyse)."""
    sym, expiry = chain.symbol.upper(), chain.expiry
    spot, dte = float(chain.underlying), (chain.expiry - x.today).days
    t = max(dte, 0.5) / 365
    plan_legs = [tp.PlanLeg(y.right, y.strike, y.side, y.lots) for y in legs_in]
    kind = tp.classify(plan_legs)
    ev = _evidence(kind, sym, dte, x, spec)
    a0 = _analyse(chain, lot, legs_in, x)
    sz = tp.size(a0["max_loss"], a0["costs"]["entry"]["total"], x.budget, proven=ev["level"] == "proven")
    m = max(sz.suggested, 1)
    legs = [fs.LegIn(y.right, y.strike, y.side, y.lots * m, y.premium) for y in legs_in]
    a = _analyse(chain, lot, legs, x) if m > 1 else a0
    _, factors, failed = fs.assess(a, defined_risk_only=x.defined_risk_only)
    reasons = [f"{f.name}: {f.explanation}" for f in factors if f.name in failed]
    if sz.suggested == 0 and a0["max_loss"] is not None and "Max loss within your F&O budget" not in failed:
        failed.append("Max loss within your F&O budget")  # max loss fits but max loss + entry costs does not
        reasons.append(f"Sizing: {sz.reason}.")
    for name, why in extra_failed or []:
        failed.append(name)
        reasons.append(why)
    action, qualifier = tp.verdict(failed, ev["level"])
    if ev["level"] == "negative":
        reasons.insert(0, f"The matching pre-registered backtest ({ev['spec_name']}): {ev['text']}")
    quotes = chain_quotes(chain)
    lq = [None if y.right == "future" else _quote(quotes, y.right, y.strike) for y in legs]
    limits = tp.limit_guidance([tp.PlanLeg(y.right, y.strike, y.side, y.lots) for y in legs], lq, lot)
    exits = tp.spec_exits(ev["spec"]) if ev["spec"] is not None and (spec is not None or ev["match"] == "exact") \
        else tp.default_exits(kind, dte)  # fmt: skip
    # levels at the planned entry prices: the bid/ask mid when known, else the analysis' last prices (labelled)
    entry_src = "bid/ask mid" if limits["status"] == "ok" else "last traded price (no live bid/ask)"
    ol, ivs = [], []
    for y, al, lr, q in zip(legs, a["legs"], limits["legs"], lq, strict=True):
        px = (
            al["premium"]
            if y.right == "future"
            else (lr["mid"] if limits["status"] == "ok" else al["premium"])
        )
        ol.append(o.Leg(y.right, y.strike, 0.0 if y.right == "future" else px, al["qty"]))  # type: ignore[arg-type]
        ivs.append(_leg_iv(q, y.right, px, spot, y.strike, t, x.rate))
    lv = tp.levels(ol, ivs, spot, t, x.rate, exits, a["max_loss"])
    rn, rw = a["risk_neutral"], a["real_world"]
    family = "future" if kind.endswith("_future") else ("credit" if tp.is_credit(kind) else "debit")
    label = action if qualifier is None else f"{action} ({qualifier})"
    if action == "ENTER" and not reasons:
        reasons.append(
            "Every check passes: defined risk, within budget, EV after costs no worse than fair, IV fits."
        )
    return {
        "title": title or f"{sym} {expiry:%d-%b-%Y} · {kind.replace('_', ' ')}",
        "symbol": sym, "expiry": expiry.isoformat(), "days_to_expiry": dte, "spot": spot, "lot_size": lot,
        "as_of": chain.as_of.isoformat() if chain.as_of else None, "source": chain.source,
        "kind": kind, "action": action, "qualifier": qualifier, "label": label, "failed": failed, "reasons": reasons,
        "legs": [{"right": y.right, "strike": y.strike, "side": y.side, "lots": y.lots, "qty": al["qty"],
                  "last": al["premium"]} for y, al in zip(legs, a["legs"], strict=True)],
        "size": {"multiple": sz.multiple, "suggested": sz.suggested, "budget": round(sz.budget, 2),
                 "risk_per_set": None if sz.risk_per_multiple is None else round(sz.risk_per_multiple, 2),
                 "reason": sz.reason, "capital": x.capital, "max_loss_pct": x.max_loss_pct},
        "entry": {**limits, "levels_priced_at": entry_src},
        "max_profit": a["max_profit"], "max_loss": a["max_loss"], "breakevens": a["breakevens"],
        "net_premium": a["net_premium"], "costs": a["costs"]["total_to_expiry"],
        "probability": {"real_world": None if rw is None else rw["pop"], "real_world_window": (rw or {}).get("window"),
                        "risk_neutral": None if rn is None else rn["pop"],
                        "note": "Model probabilities of a positive P&L after costs at expiry (lognormal; real prices "
                                "have fat tails), not forecasts."},
        "exits": {"source": exits.source, "rule_of_thumb": tp.RULE_OF_THUMB if exits.source == "rule of thumb" else None,
                  "stop": lv["stop"], "target": lv["target"], "entry_net": lv["entry_net"], "credit": lv["credit"],
                  "per_share_unit": lv["per_share_unit"], "notes": lv["notes"], "time": _time_exit(exits, expiry),
                  "profit_pct": exits.profit_pct, "stop_pct": exits.stop_pct},
        "exit_rules": _exit_rules(exits, kind, legs, x.is_index),
        "adjustments": {"rules": ADJUST[family], "note": ADJUST_NOTE},
        "invalidation": _invalidation(kind, legs, spot, x.events, x.today, expiry, x.is_index),
        "evidence": {k: v for k, v in ev.items() if k != "spec"},
        "paper": paper_record(x.paper, sym, kind, ev["spec_key"]),
        "iv_context": a["iv_context"], "checks": [asdict(f) for f in factors],
        "notes": a["notes"],
        "disclaimer": "A plan to act on by hand, at your own risk: nothing is ordered. Model levels assume today's "
                      "IVs; gaps and IV spikes can blow through any stop. Not advice.",
    }  # fmt: skip


# --------------------------------------------------------------------------- "Suggest setups": the library on today's chain
def monthly_expiries(expiries: list[date]) -> set[date]:
    """The last listed expiry of each calendar month (NSE's monthly contract; a week's expiry is any other)."""
    last: dict[tuple[int, int], date] = {}
    for e in expiries:
        k = (e.year, e.month)
        last[k] = max(last.get(k, e), e)
    return set(last.values())


def recipe_expiry(spec: Any, expiries: list[date], today: date) -> date | None:
    months = monthly_expiries(expiries)
    ok = [e for e in sorted(expiries) if spec.dte_min <= (e - today).days <= spec.dte_max
          and (spec.expiry_kind == "any" or e in months)]  # fmt: skip
    return ok[0] if ok else None


def recipe_legs(spec: Any, chain, today: date, rate: float) -> list[fs.LegIn] | str:
    """The spec's legs on the live chain (its rule from backtest.engine.choose_legs: the short leg by delta, the wing
    width_pct of spot away), or the reason none can be picked."""
    spot, t = float(chain.underlying), max((chain.expiry - today).days, 0.5) / 365
    quotes, a = chain_quotes(chain), spec.anchor_delta
    w = (spec.width_pct or 0) / 100 * spot

    def credit(right: str) -> list[fs.LegIn] | str:
        short = tp.pick_by_delta(quotes, right, a, spot, t, rate)
        if short is None:
            return f"no traded {right} near {a:g} delta"
        far = tp.wing(quotes, right, short.strike, short.strike - w if right == "put" else short.strike + w)
        return (
            [fs.LegIn(right, short.strike, "sell"), fs.LegIn(right, far.strike, "buy")]
            if far
            else f"no {right} wing"
        )

    def debit(right: str) -> list[fs.LegIn] | str:
        near = tp.pick_by_delta(quotes, right, a, spot, t, rate, otm_only=False)
        if near is None:
            return f"no traded {right} near {a:g} delta"
        far = tp.wing(quotes, right, near.strike, near.strike + w if right == "call" else near.strike - w)
        return (
            [fs.LegIn(right, near.strike, "buy"), fs.LegIn(right, far.strike, "sell")]
            if far
            else f"no {right} wing"
        )

    if spec.kind in ("put_credit", "call_credit"):
        return credit(spec.kind.split("_")[0])
    if spec.kind == "iron_condor":
        p, c = credit("put"), credit("call")
        return p if isinstance(p, str) else c if isinstance(c, str) else p + c
    if spec.kind in ("debit_call", "debit_put"):
        return debit(spec.kind.split("_")[1])
    return f"{spec.kind} is an overlay on a held stock: not suggested"


def _recipe_checks(spec: Any, x: Inputs) -> list[tuple[str, str]]:
    from finresearch.fincalc import volatility as vol

    if spec.iv_pct_min is None:
        return []
    pct = vol.iv_stats(x.iv_series).percentile
    if pct is None:
        return [("Recipe IV filter", f"IV percentile unknown: the recipe enters only when the 30-day IV is at or above "
                 f"its {spec.iv_pct_min:.0%} percentile of the past year.")]  # fmt: skip
    if pct < spec.iv_pct_min:
        return [
            (
                "Recipe IV filter",
                f"IV percentile {pct:.0%} is below the recipe's {spec.iv_pct_min:.0%} filter.",
            )
        ]
    return []


def waiting(spec: Any, symbol: str, x: Inputs, why: str) -> dict[str, Any]:
    """A recipe with no trade today: WAIT (or AVOID when its own backtest is negative), with no legs."""
    ev = _evidence(spec.kind, symbol, spec.dte_min, x, spec)
    action, _ = tp.verdict(["Entry window"], ev["level"])
    reasons = [why] + ([f"The pre-registered backtest: {ev['text']}"] if ev["level"] == "negative" else [])
    return {"title": f"{symbol.upper()} · {spec.name}", "symbol": symbol.upper(), "kind": spec.kind, "action": action,
            "qualifier": None, "label": action, "reasons": reasons, "legs": [], "failed": ["Entry window"],
            "evidence": {k: v for k, v in ev.items() if k != "spec"}, "spec_key": spec.key}  # fmt: skip


async def suggest(symbol: str, expiries: list[date], get_chain, lot_for, x: Inputs) -> list[dict[str, Any]]:
    """One card per non-overlay library recipe on `symbol` today. `get_chain(expiry)` and `lot_for(expiry)` are async;
    each expiry's chain is read once."""
    from finresearch.backtest import specs

    chains: dict[date, Any] = {}
    out = []
    for spec in specs.SPECS:
        if spec.role == "overlay":
            continue
        e = recipe_expiry(spec, expiries, x.today)
        if e is None:
            kind = "a monthly" if spec.expiry_kind == "monthly" else "an"
            out.append(waiting(spec, symbol, x, f"No {kind} expiry {spec.dte_min}-{spec.dte_max} days away today: "
                                                f"the recipe's entry window is closed."))  # fmt: skip
            continue
        if e not in chains:
            chains[e] = (await get_chain(e), await lot_for(e))
        chain, lot = chains[e]
        if chain is None or chain.underlying is None or not lot:
            out.append(
                waiting(spec, symbol, x, f"No option chain or lot size for {symbol.upper()} {e:%d-%b-%Y}.")
            )
            continue
        legs = recipe_legs(spec, chain, x.today, x.rate)
        if isinstance(legs, str):
            out.append(waiting(spec, symbol, x, f"Cannot build the recipe's legs today: {legs}."))
            continue
        try:
            card = build(chain, lot, legs, x, spec=spec, extra_failed=_recipe_checks(spec, x),
                         title=f"{symbol.upper()} {e:%d-%b-%Y} · {spec.name}")  # fmt: skip
        except ValueError as err:
            out.append(waiting(spec, symbol, x, str(err)))
            continue
        out.append({**card, "spec_key": spec.key})
    return out
