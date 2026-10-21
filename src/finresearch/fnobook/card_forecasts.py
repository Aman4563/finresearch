"""The card ledger (P2.1, #376): every logged F&O card (card_log, #360) becomes a market-data-only forecast in the
forecast ledger, resolved through the backtest engine's exit simulation on the stored bhavcopy.

Pre-registered in evals/experiments/card_ledger/PREREG.md (22-Oct-2026). Event: the P&L of ONE SET (the card's base
lot ratio) after costs is > 0 under the card's exit plan. Entry at the card's planned prices (the bid/ask mid when the
card had one, else the last traded price) on the IST day it was logged, entry costs by fincalc.charges; from the next
stored session `backtest.engine.manage` marks, exits (profit-take, stop, days to expiry, the last session before
expiry day) or settles it on expiry day, with the engine's slippage and costs. Outcome 1 / 0; the net P&L per set is
the resolution value.

Unknown never becomes a result: a leg with no stored row on a session it was marked, a future leg (the engine marks
options only), an unknown lot size or an exit plan that cannot be re-derived void the forecast with the reason; a
path that is not finished in the stored data, or that exits on the latest stored session (whether that session was
the last before expiry day is not yet known), is NotYet. Waiting cards (no legs) are not forecasts.

Personal data stays out: the forecast's inputs are per set (no size, capital, budget or open book), so the forecast
ledger holds market data only. card_log itself stays personal and is read only here and in fnobook."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.backtest import engine
from finresearch.backtest.history import History
from finresearch.backtest.specs import BOOK, SPECS, Spec
from finresearch.db.models import CardLog, Forecast
from finresearch.fincalc import charges as ch
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc.dates import to_ist
from finresearch.signals.ledger import NotYet, Resolution, resolver

EVENT_KIND = "card_pnl"


def _same(a: float | None, b: float | None) -> bool:
    return (a is None and b is None) or (a is not None and b is not None and math.isclose(a, b, abs_tol=1e-9))


def exit_plan(card: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The card's exit plan re-derived from `exits.source` (the recipe's own exits, or the rule of thumb for its
    structure and days to expiry) and checked against the card's profit/stop %; (None, why) when it cannot be."""
    ex, kind, dte = card.get("exits") or {}, card.get("kind"), card.get("days_to_expiry")
    src = ex.get("source") or ""
    if src.startswith("spec:"):
        spec = next((s for s in SPECS if s.key == src.removeprefix("spec:")), None)
        if spec is None:
            return None, f"exit plan unknown: no registered recipe {src}"
        plan = tp.spec_exits(spec)
    elif src == "rule of thumb" and kind and dte is not None:
        plan = tp.default_exits(kind, int(dte))
    else:
        return None, f"exit plan unknown: source {src or 'missing'}"
    if not (_same(plan.profit_pct, ex.get("profit_pct")) and _same(plan.stop_pct, ex.get("stop_pct"))):
        return None, (f"exit plan unknown: the card's profit/stop {ex.get('profit_pct')}/{ex.get('stop_pct')} % differ "
                      f"from {src}'s {plan.profit_pct}/{plan.stop_pct} %")  # fmt: skip
    return {"profit_pct": plan.profit_pct, "stop_pct": plan.stop_pct, "exit_dte": plan.exit_dte,
            "hold_to_expiry": plan.hold_to_expiry}, None  # fmt: skip


def card_inputs(
    card: dict[str, Any], logged_at: datetime, *, card_log_id: int, index: int
) -> dict[str, Any] | None:
    """The forecast inputs for one card, per set; None for a card with no legs (no trade, no event)."""
    legs = card.get("legs") or []
    if not legs:
        return None
    unknown: dict[str, str] = {}
    m = max(int((card.get("size") or {}).get("suggested") or 0), 1)  # the card's legs are the base set x m
    entry = card.get("entry") or {}
    mids = [x.get("mid") for x in entry.get("legs") or []]
    use_mid = entry.get("status") == "ok" and len(mids) == len(legs) and all(v is not None for v in mids)
    out_legs = []
    for i, lg in enumerate(legs):
        lots = lg.get("lots")
        if not lots or lots % m:
            unknown["lots"] = f"leg {i + 1}'s {lots} lots are not a multiple of the card's {m} sets"
        out_legs.append({"right": lg["right"], "strike": float(lg["strike"]), "side": lg["side"],
                         "lots": (lots or 0) // m,
                         "entry": mids[i] if use_mid and lg["right"] != "future" else lg.get("last")})  # fmt: skip
    plan, why = exit_plan(card)
    if why:
        unknown["exit plan"] = why
    per_set = lambda v: None if v is None else round(v / m, 2)  # noqa: E731
    ev, dots = card.get("ev") or {}, card.get("pnl_dots") or {}
    prob = card.get("probability") or {}
    entry_day = to_ist(logged_at).date()
    return {
        "card_log_id": card_log_id, "card_index": index, "symbol": card.get("symbol"), "expiry": card.get("expiry"),
        "structure": card.get("kind"), "lot": card.get("lot_size"), "legs": out_legs,
        "entry_priced_at": entry.get("levels_priced_at") if use_mid else "last traded price",
        "entry_day": entry_day.isoformat(), "start_date": entry_day.isoformat(), "exit_plan": plan,
        "exit_source": (card.get("exits") or {}).get("source"),
        "verdict": card.get("action"), "qualifier": card.get("qualifier"), "label": card.get("label"),
        "failed": card.get("failed"), "evidence": card.get("evidence"),
        "probability": {"real_world": prob.get("real_world"), "risk_neutral": prob.get("risk_neutral")},
        "ev": {"real_world": per_set(ev.get("real_world")), "risk_neutral": per_set(ev.get("risk_neutral"))},
        "pnl_dots": {"model": dots.get("model"), "values": [per_set(v) for v in dots.get("values") or []],
                     "payoff_ratio": dots.get("payoff_ratio")} if dots else None,
        "unknown": unknown,
    }  # fmt: skip


def _void(why: str) -> Resolution:
    return Resolution(None, None, f"void: {why}")


def simulate(inp: dict[str, Any], history: History) -> Resolution:
    """Replay one card's set on `history` (module docstring). Raises NotYet while the path is unfinished."""
    if inp.get("unknown"):
        return _void("; ".join(f"{v}" for v in inp["unknown"].values()))
    if any(lg["right"] == "future" for lg in inp["legs"]):
        return _void("a future leg: the engine marks option legs only (not simulated yet)")
    plan, sym, lot = inp["exit_plan"], inp["symbol"], inp.get("lot")
    if not lot:
        return _void("lot size unknown")
    if any(lg["entry"] is None for lg in inp["legs"]):
        return _void("a leg's entry price is unknown")
    expiry, d0 = date.fromisoformat(inp["expiry"]), date.fromisoformat(inp["entry_day"])
    spec = Spec("card", "logged card", inp.get("structure") or "card", sym, "candidate", "any", 0, 999, 0.0, None,
                profit_take_pct=plan["profit_pct"],
                stop_mult=None if plan["stop_pct"] is None else plan["stop_pct"] / 100,
                exit_dte=plan["exit_dte"], hold_to_expiry=plan["hold_to_expiry"])  # fmt: skip
    legs = [engine.Leg(lg["right"], lg["strike"], lg["side"], lg["lots"] * lot, lg["entry"], None, lg["entry"])
            for lg in inp["legs"]]  # fmt: skip
    costs = ch.order_costs([ch.TradeLeg("option", x.side, x.entry, x.qty) for x in legs], d0,  # type: ignore[arg-type]
                           BOOK.brokerage_per_order).total  # fmt: skip
    premium = sum(-x.sign * x.entry * x.qty for x in legs)
    p = engine.Position("card", sym, expiry, d0, (expiry - d0).days, 1, lot, legs, premium, None, None, costs,
                        None, None)  # fmt: skip
    latest = history.latest(sym)
    days = history.sessions(sym, d0, latest) if latest else []
    if not days or days[-1] <= d0:
        raise NotYet(f"no stored {sym} session after {d0}")

    def eday(e: date) -> date:  # backtest.engine.run's: the last stored session on or before e
        if e > days[-1]:
            return e
        return max(d for d in days if d <= e)

    st = engine.SpecState(spec, open=[p])
    missing: list[str] = []
    for i, d in enumerate(days):
        x = history.day(sym, d)
        if x is None or d <= d0:
            continue
        if d < eday(expiry):
            key = engine.row_expiry(p, x)
            missing += [f"{sym} {lg.strike:g} {lg.right} on {d}" for lg in p.legs
                        if (key, lg.right, lg.strike) not in x.options]  # fmt: skip
        nxt = days[i + 1] if i + 1 < len(days) else None
        engine.manage(st, x, nxt, BOOK, eday)
        if not st.trades:
            continue
        if missing:
            return _void("no stored bhavcopy row for " + ", ".join(missing[:3]))
        tr = st.trades[0]
        if d >= days[-1] and d < expiry:
            raise NotYet(f"the exit on {d} is on the latest stored session: waiting for the next one")
        flags = f" ({'; '.join(tr['flags'])})" if tr["flags"] else ""
        return Resolution(
            int(tr["net"] > 0),
            tr["net"],
            f"{tr['reason']} on {tr['exit_day']}: net {tr['net']:,.2f} per set after costs{flags}",
        )
    raise NotYet(f"the set is still open at {days[-1]} (expiry {expiry})")


# --------------------------------------------------------------------------- the forecast ledger
SOURCE = "card_log:"  # + the card_log row id
RN_METHOD = "F&O card PoP, risk-neutral (ATM IV lognormal): the option market's own probability, benchmark"
EVENT = ("P&L of one set (the card's base lot ratio) after costs > 0 under the card's exit plan, replayed on the "
         "stored NSE F&O bhavcopy (evals/experiments/card_ledger/PREREG.md)")  # fmt: skip


def history() -> History:
    """The stored F&O history of the app's database (a test seam)."""
    from finresearch.backtest.dbhistory import DbHistory

    return DbHistory()


def _cards(kind: str, response: dict[str, Any]) -> list[dict[str, Any]]:
    """The cards of a logged response that carry an exit plan: a plan card, or each suggest card. A strategy check
    has none (not a forecast here)."""
    if kind == "plan":
        return [response]
    if kind == "suggest":
        return list(response.get("cards") or [])
    return []


def record_pending(s: Session) -> int:
    """Record every card_log card that has no forecast yet: one forecast per model (the real-world PoP under the plan
    card's method, the risk-neutral PoP as a benchmark), keyed on the card so nothing is ever replaced. Returns the
    number of forecasts written. Runs from the monitor's forecast step and `finresearch forecasts cards`, never in a
    GET (the suggest endpoint is one)."""
    from finresearch.signals.tradeplan import PLAN_METHOD

    done = set(s.scalars(select(Forecast.source).where(Forecast.source.like(f"{SOURCE}%")).distinct()))
    q = select(CardLog).where(CardLog.kind.in_(("plan", "suggest"))).order_by(CardLog.id)
    n = 0
    for row in s.scalars(q):
        if f"{SOURCE}{row.id}" in done:
            continue
        for i, c in enumerate(_cards(row.kind, row.response)):
            inp = card_inputs(c, row.created_at, card_log_id=row.id, index=i)
            if inp is None:
                continue
            expiry = date.fromisoformat(inp["expiry"])
            for tag, method, p in (("rw", PLAN_METHOD, inp["probability"]["real_world"]),
                                   ("rn", RN_METHOD, inp["probability"]["risk_neutral"])):  # fmt: skip
                values = dict(created_at=row.created_at, asset="fno", instrument=row.symbol, name=c.get("title"),
                              source=f"{SOURCE}{row.id}", event_kind=EVENT_KIND, event=EVENT,
                              horizon=f"to the card's exit, at the latest expiry {expiry:%d-%b-%Y}",
                              resolve_on=expiry, probability=p, action=(c.get("label") or c.get("action") or "")[:40],
                              method=method[:200], validation_status="rule_based", inputs=inp, status="open",
                              dedupe_key=f"card:{row.id}:{i}:{tag}")  # fmt: skip
                stmt = insert(Forecast).values(**values).on_conflict_do_nothing(index_elements=["dedupe_key"])
                n += len(s.scalars(stmt.returning(Forecast.id)).all())
    s.flush()
    return n


@resolver("fno", EVENT_KIND)
async def _resolve(f: Forecast, deps: Any, s: Session) -> Resolution:
    return simulate(f.inputs or {}, history())
