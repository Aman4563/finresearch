"""The F&O trade-plan card (#317): for a strategy the user builds, or a pre-registered library recipe applied to today's
chain ("Suggest setups"), one card with a verdict and everything needed to act on it by hand: exact legs and strikes,
lots from the profile's risk budget, a limit-price range from bid/ask, max profit/loss, breakevens, both
probabilities of profit, the stop-loss in premium and in underlying terms, the profit target, the time exit,
adjustment and invalidation rules, and the evidence (the matching backtest result and the user's paper record).

Verdict (one engine, `decide`, also used by the signal card's Strategy check, #355): AVOID when a hard check fails
(undefined risk, over budget even at one set, a same-day expiry) or the matching pre-registered backtest is harmful
(t ≤ −2 and CI below zero, delta-neutral recipes only); WAIT when any other check fails or is unknown (signals.fno.assess:
bid/ask spreads within 10% of the premium; the recipe's own registered entry filters; the IV percentile of ad hoc legs
is context, not a gate; #356: the model EV is shown, labelled "model EV - not validated", and never a gate);
ENTER only when everything passes and the matched spec passed out of sample (a candidate); everything passing
without one is "PAPER: NO PROVEN EDGE — paper only (0 real lots)" (#350), sized for paper by the same formula and
never with real money. The market-context indicators (signals.fno_context) never feed it.

Evidence matching: the legs' shape (fincalc.tradeplan.classify) is matched to a library spec of the same kind. The
match is exact only for the spec's own underlying inside its days-to-expiry window; any other match is shown as
related evidence and never changes the verdict either way (a decision for the user, listed in the PR).

Nothing here places an order: the card's one-click actions are a paper trade and exit-rule alerts (api/fno_book).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from finresearch.fincalc import options as o
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc.dates import session_clock, years_to_expiry
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
    head = {k: rep.get(k) for k in ("id", "created_at", "source", "oos_evaluated", "variants_tried", "code_hash",
                                    "engine_hash", "data_hash", "git_commit", "git_dirty")}  # fmt: skip
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


# --------------------------------------------------------------------------- the open book (#358)
@dataclass(frozen=True)
class OpenPosition:
    """One open F&O position for the open-book cap: legs at their entry prices (signed units), its shape and label."""

    symbol: str
    kind: str
    label: str
    legs: list[o.Leg]
    spot: (
        float | None
    )  # a reference spot for an undefined-risk shock (paper: at entry); the card's own when the same
    source: str  # paper | groww


@dataclass(frozen=True)
class RealPosition:
    """A Groww F&O position group (underlying + expiry) with its legs at Groww's average prices."""

    symbol: str
    expiry: date | None
    legs: list[o.Leg]


@dataclass(frozen=True)
class BookData:
    """What the card reads of the open book: every open position (None = unknown), month-to-date F&O P&L after costs
    of the paper and the real book (None = unknown) and notes on what is counted."""

    positions: list[OpenPosition] | None
    mtd_paper: float | None
    mtd_real: float | None
    notes: tuple[str, ...] = ()


UNKNOWN_BOOK = BookData(None, None, None, ("The open book was not read: its risk is unknown.",))
EMPTY_BOOK = BookData([], 0.0, 0.0)  # tests and callers that know nothing is open


def _signed(right: str, strike: float, side: str, qty: int, price: float) -> o.Leg:
    q = qty if side == "buy" else -qty
    return o.Leg(right, price, 0.0, q) if right == "future" else o.Leg(right, strike, price, q)  # type: ignore[arg-type]


def paper_book(today: date) -> tuple[list[OpenPosition], float | None]:
    """Open paper positions and the paper book's month-to-date P&L after costs: net P&L of trades closed this month
    plus marked P&L after entry costs of open ones (an open trade's whole marked P&L counts: the month-start mark is
    not stored). None when any of it is unknown (an unmarked open trade, a closed one without a net P&L)."""
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PaperStrategy
    from finresearch.fincalc.dates import to_ist

    first = today.replace(day=1)
    out: list[OpenPosition] = []
    mtd: float | None = 0.0
    with session_scope() as s:
        for st in s.scalars(select(PaperStrategy)):
            if st.status == "closed":
                if st.closed_at is not None and to_ist(st.closed_at).date() >= first:
                    mtd = None if mtd is None or st.net_pnl is None else mtd + float(st.net_pnl)
                continue
            legs = [_signed(x.right, float(x.strike), x.side, x.qty, float(x.entry_price)) for x in st.legs]
            kind = tp.classify([tp.PlanLeg(x.right, float(x.strike), x.side, x.lots) for x in st.legs])
            spot = None if st.spot_at_entry is None else float(st.spot_at_entry)
            out.append(OpenPosition(st.symbol, kind, st.label or "", legs, spot, "paper"))
            mtd = (
                None
                if mtd is None or st.mtm_gross is None
                else mtd + float(st.mtm_gross) - float(st.entry_costs)
            )
    return out, mtd


async def real_positions(today: date) -> tuple[list[RealPosition] | None, str | None]:
    """Groww F&O positions (the last sync's rows, resolved through Groww's instruments file), or None when they are
    unknown: connected but not read, a contract or a long/short direction unresolved, an average price missing.
    Not connected = no real positions known to the app (said in the note). Local only: never sent to a model."""
    from collections import defaultdict

    from finresearch.fnobook import positions as pos
    from finresearch.fnobook import service

    try:
        st = service.stored_positions()
    except Exception:
        return None, "Groww's F&O positions could not be read: the open risk is unknown."
    if st["status"] == "not_connected":
        return [], "Groww is not connected: only paper positions are counted in the open book."
    if st["status"] != "ok":
        return None, ("Groww is connected but its F&O positions have not been read yet: the open risk is unknown "
                      "(run a Groww sync on the Connections page).")  # fmt: skip
    if not st["rows"]:
        return [], None
    inst = await service._instruments(service._market())
    if inst is None:
        return None, ("Groww's instruments file could not be read, so the Groww F&O positions cannot be priced: the "
                      "open risk is unknown (try again, or sync Groww).")  # fmt: skip
    groups: dict[tuple[str, date], list[o.Leg]] = defaultdict(list)
    for r in st["rows"]:
        c = pos.contract(inst.contract(r.get("symbol") or ""))
        qty, avg = service._f(r.get("quantity")), service._f(r.get("avg_price"))
        if c is None or not r.get("direction_known") or qty is None or avg is None:
            return None, (f"The Groww F&O position {r.get('symbol') or '?'} could not be resolved (contract, long/short "
                          "or average price unknown): the open risk is unknown (sync Groww again).")  # fmt: skip
        right, q = c["right"], int(qty)
        leg = o.Leg("future", avg, 0.0, q) if right == "future" else o.Leg(right, c["strike"], avg, q)
        groups[(c["underlying"], c["expiry"])].append(leg)
    return [RealPosition(k[0], k[1], v) for k, v in groups.items()], None


async def load_book(today: date) -> BookData:
    """The open book for a card (#358): open paper trades and Groww F&O positions, and both month-to-date P&Ls. The
    real book's month-to-date P&L is not stored yet (P0.10's card log and P2.1), so it is unknown: real money stays 0."""
    from sqlalchemy.exc import SQLAlchemyError

    notes = ["The real book's month-to-date F&O P&L is not recorded yet: real-money permission stays off."]
    try:
        paper, mtd = paper_book(today)
    except SQLAlchemyError:
        return UNKNOWN_BOOK
    real, why = await real_positions(today)
    if why:
        notes.append(why)
    if real is None:
        return BookData(None, mtd, None, tuple(notes))
    rp = [OpenPosition(r.symbol, tp.classify([tp.PlanLeg(x.right, x.strike, "buy" if x.qty > 0 else "sell", abs(x.qty))
                                              for x in r.legs]), "", r.legs, None, "groww") for r in real]  # fmt: skip
    return BookData(paper + rp, mtd, None, tuple(notes))


def book_for(
    b: BookData, capital: float, symbol: str, kind: str, spot: float | None, spec: Any | None
) -> tp.OpenBook:
    """The OpenBook one card sees: OR over every open position (each shocked at the card's spot when it is the same
    underlying, else at its own reference spot), and the open count of this recipe (its paper tag) or shape."""
    risk: float | None = None
    same = 0
    if b.positions is not None:
        risk = 0.0
        for p in b.positions:
            r = tp.position_risk(p.legs, spot if p.symbol == symbol.upper() else p.spot)
            risk = None if risk is None or r is None else risk + r
            tag = spec is not None and p.label.startswith(f"plan:{spec.key}")
            same += bool(tag or (p.symbol == symbol.upper() and p.kind == kind))
    return tp.OpenBook(capital=capital, open_risk=risk, open_same=same,
                       max_concurrent=None if spec is None else spec.max_concurrent,
                       mtd_paper=b.mtd_paper, mtd_real=b.mtd_real, notes=b.notes)  # fmt: skip


def book_json(ob: tp.OpenBook) -> dict[str, Any]:
    return {"budget": round(ob.budget, 2), "budget_pct": tp.BOOK_BUDGET_PCT,
            "open_risk": None if ob.open_risk is None else round(ob.open_risk, 2), "open_same": ob.open_same,
            "max_concurrent": ob.max_concurrent, "mtd_paper": None if ob.mtd_paper is None else round(ob.mtd_paper, 2),
            "month_stop_pct": tp.MONTH_LOSS_STOP_PCT, "notes": list(ob.notes)}  # fmt: skip


# --------------------------------------------------------------------------- the card
PLAN_METHOD = "rule-based F&O trade-plan card"


def card_reliability(probability: float | None, scored: int | None) -> dict[str, Any]:
    """signals.reliability for a trade-plan card (#357): rule-based, no base rate, and `scored` = this method's resolved
    forecasts in the ledger (None = unreadable: calibration unknown, never established)."""
    from finresearch.signals.reliability import reliability

    return reliability({"asset": "fno", "probability": probability, "validation": {"status": "rule_based", "n": 0}},
                       scored)  # fmt: skip


class Inputs:
    """Everything the card reads besides the chain: market history, the profile's F&O settings, the saved backtest,
    the paper book and the event dates (None = unknown)."""

    def __init__(self, *, today: date, closes: list[float], iv_series: list[float], capital: float, max_loss_pct: float,
                 brokerage: float, defined_risk_only: bool, rate: float, iv_src: dict[str, Any] | None = None,
                 iv_as_of: date | None = None, strategies: list[dict[str, Any]] | None = None,
                 backtest_head: dict[str, Any] | None = None, paper: list[dict[str, Any]] | None = None,
                 events: list[tuple[date, str]] | None = None, is_index: bool = True,
                 book: BookData = UNKNOWN_BOOK, iv_points: list[tuple[Any, float | None]] | None = None,
                 scored: int | None = None, now: datetime | None = None,
                 calendar: tuple[list[Any], list[Any]] | None = None) -> None:  # fmt: skip
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
        self.calendar = (
            calendar  # #367: (events, coverage) from eventcal.store; None = not read (check: unknown)
        )
        self.book = book  # the open book (#358); unknown unless read: then no card has room
        self.scored = scored  # #357: the plan card method's scored forecasts (None = the ledger was not read)
        self.now = (
            now  # the clock T is measured from (#359); None = 15:30 IST on `today` (end-of-day convention)
        )
        # per-session IV points for a recipe's IV filter (#354's definition); default: iv_series as consecutive sessions
        self.iv_points = iv_points if iv_points is not None else list(enumerate(iv_series))

    @property
    def budget(self) -> float:
        return self.capital * self.max_loss_pct / 100


def _analyse(chain, lot: int, legs: list[fs.LegIn], x: Inputs, bidask_chain: Any = None,
             early_exit_p: float = 1.0) -> dict[str, Any]:  # fmt: skip
    return fs.analyse(chain, lot, legs, today=x.today, closes=x.closes, iv_series=x.iv_series, capital=x.capital,
                      max_loss_pct=x.max_loss_pct, brokerage=x.brokerage, rate=x.rate, iv_source=x.iv_src,
                      iv_as_of=x.iv_as_of, bidask_chain=bidask_chain, early_exit_p=early_exit_p,
                      now=x.now)  # fmt: skip


def evidence_for(kind: str, symbol: str, dte: int, strategies: list[dict[str, Any]] | None,
                 head: dict[str, Any] | None, spec: Any | None = None, *, today: date) -> dict[str, Any]:  # fmt: skip
    """The evidence block for legs of `kind` (or for the recipe `spec`): the matched library spec, its saved backtest
    row and level. Shared by the trade-plan card and the signal card's "Strategy check" (#350: one rule for both).
    #379: exact evidence from a stale run (older than the max age, from before a regime change, or of unknown date)
    is not evidence: its level reads "none" (`raw_level` keeps what it said; harmful stays harmful, staleness never
    relaxes an AVOID) and `stale` lists why, which `decide` turns into a failed check (WAIT)."""
    from finresearch.inventory import regimes

    exact = spec is not None and spec.underlying == symbol.upper()
    if spec is None:
        spec, exact = match_spec(kind, symbol, dte)
    row = next((r for r in strategies or [] if spec and r.get("key") == spec.key), None)
    level = tp.evidence_level(row, kind)
    stale = (regimes.staleness(regimes.run_day((head or {}).get("created_at")), today)
             if exact and strategies is not None else [])  # fmt: skip
    raw_level, level = level, ("harmful" if level == "harmful" else "none") if stale else level
    out = {"spec_key": spec.key if spec else None, "spec_name": spec.name if spec else None,
           "match": None if spec is None else ("exact" if exact else "related"), "status": (row or {}).get("status"),
           "level": level if exact else "none", "raw_level": raw_level, "stale": stale,
           "text": tp.evidence_text(row, raw_level) if spec else tp.evidence_text(None, "none"),
           "run": head, "url": "/fno/backtests"}  # fmt: skip
    if spec is not None and not exact:
        out["text"] = (f"Related only ({spec.name}, not this underlying or expiry window): " + out["text"]
                       + " It does not change the verdict.")  # fmt: skip
    if strategies is None:
        out["text"] = "No pre-registered backtest is saved yet: no strategy has historical evidence."
    if head is not None:  # #379: shown, never a gate (a decision listed in the PR)
        from finresearch.inventory import provenance

        out["code_changed"] = provenance.code_changed(head)
    else:
        out["code_changed"] = None
    if stale:
        out["text"] = "Stale evidence, read as unvalidated (" + "; ".join(stale) + "). " + out["text"]
    out["spec"] = spec
    return out


IV_CHECK = "IV context fits the strategy"
BUDGET_CHECK = "Max loss within your F&O budget"


@dataclass(frozen=True)
class Decision:
    action: str
    qualifier: str | None
    failed: list[str]
    size: tp.Size
    reasons: list[str]  # why beyond the failed checks' own explanations (sizing, the recipe's filters)


MONTH_CHECK = "Month-to-date F&O loss stop"
MONTH_CHECK_REAL = "Month-to-date F&O loss stop (real book)"
BOOK_CHECK = "Open book within its budget"
STALE_CHECK = "Backtest evidence is current"  # #379: inventory.regimes.staleness


def decide(gates: list[str], max_loss_one: float | None, costs_one: float, budget: float, level: str,
           extra_failed: list[tuple[str, str]] | None = None, book: tp.OpenBook | None = None,
           stale: list[str] | None = None) -> Decision:  # fmt: skip
    """The one verdict and sizing rule (#355) for the trade-plan card, "Suggest setups" and the signal card's Strategy
    check: `gates` = signals.fno.assess's failed checks for the legs, sized from the base set's max loss and entry
    costs, `level` = the matched spec's evidence. The IV-percentile check is context for ad hoc legs, never a gate (it
    was never tested; a recipe applies its own registered iv_pct_min through `extra_failed`).
    `book` (#358): the open positions and month-to-date P&L. A full (or unknown) open book sizes 0 and reads WAIT; a
    paper month-to-date loss at the stop (or unknown) turns the card to WAIT; real money also needs the real book's
    month-to-date P&L known and above the stop.
    `stale` (#379): why the matched backtest evidence is stale; any reason caps the verdict at WAIT."""
    failed = [g for g in gates if g != IV_CHECK]
    reasons: list[str] = []
    if stale:
        failed.append(STALE_CHECK)
        reasons.append("Backtest evidence is stale: " + "; ".join(stale) + ".")
    sz = tp.size(max_loss_one, costs_one, budget, proven=level == "proven", book=book)
    if sz.multiple == 0 and max_loss_one is not None and BUDGET_CHECK not in failed:
        failed.append(BUDGET_CHECK)  # max loss fits but max loss + entry costs does not
        reasons.append(f"Sizing: {sz.reason}.")
    elif sz.multiple > 0 and sz.suggested == 0:
        failed.append(BOOK_CHECK)  # the trade fits its budget, the open book has no room for it
        why = " ".join(book.notes) if book is not None and book.open_risk is None else ""
        reasons.append(f"{BOOK_CHECK}: {sz.reason}." + (f" {why}" if why else ""))
    if book is not None and book.month_stopped():
        failed.append(MONTH_CHECK)
        reasons.append(f"{MONTH_CHECK}: " + ("the paper book's month-to-date P&L is unknown (an open paper trade has "
                       "no mark yet: it is marked in the next in-session pass), so no card can pass it." if book.mtd_paper is None else
                       f"the paper book is down ₹{-book.mtd_paper:,.0f} this month, at or beyond the "
                       f"{tp.MONTH_LOSS_STOP_PCT:g}% of F&O capital stop: every card waits until next month."))  # fmt: skip
    for name, why in extra_failed or []:
        failed.append(name)
        reasons.append(why)
    action, qualifier = tp.verdict(failed, level)
    if action == tp.ENTER and book is not None and book.month_stopped(real=True):
        # a real-money call needs the real book's month: unknown or at the stop → WAIT (paper size kept)
        failed.append(MONTH_CHECK_REAL)
        reasons.append(f"{MONTH_CHECK_REAL}: " + ("the real book's month-to-date F&O P&L is not known here, so no "
                       "real-money size is given." if book.mtd_real is None else f"the real book is down "
                       f"₹{-book.mtd_real:,.0f} this month, at or beyond the {tp.MONTH_LOSS_STOP_PCT:g}% stop."))  # fmt: skip
        action, qualifier = tp.verdict(failed, level)
    if action != tp.ENTER:
        sz = replace(sz, real_lots=0)  # real money only on an ENTER
    return Decision(action, qualifier, failed, sz, reasons)


def _evidence(kind: str, symbol: str, dte: int, x: Inputs, spec: Any | None) -> dict[str, Any]:
    return evidence_for(kind, symbol, dte, x.strategies, x.backtest_head, spec, today=x.today)


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
                   if not is_index else "RBI policy, Budget and election dates before expiry are not covered by the "
                   "event calendar: check them before entering.")  # fmt: skip
    else:
        for d, what in events:
            if today <= d <= expiry:
                out.append(f"{what} on {d:%d-%b-%Y}, before expiry: a gap can jump past any stop.")
    return out


def event_check(x: Inputs, expiry: date) -> dict[str, Any]:
    """#367: the informational "tier-1 event inside holding window" check, from the plan time to expiry, using only
    calendar rows public at the plan time (point in time). Not a gate: it changes no action or size."""
    from finresearch.eventcal import calendar as cal

    events, coverage = x.calendar if x.calendar is not None else (None, None)
    out = cal.window_check(events, coverage, start=x.today, end=expiry, as_of=session_clock(x.today, x.now))
    return {**out, "window": {"from": x.today.isoformat(), "to": expiry.isoformat()}}


def _leg_iv(q: tp.ChainQuote | None, right: str, price: float, spot: float, strike: float, t: float,
            rate: float) -> float | None:  # fmt: skip
    if right == "future":
        return None
    if q and q.iv:
        return q.iv / 100
    return o.implied_vol(right, price, spot, strike, t, rate)  # type: ignore[arg-type]


def needs_bidask(chain, legs: list[fs.LegIn]) -> bool:
    """True when an option leg has no bid/ask in `chain` and the chain is not NSE's own (#324): re-reading NSE's chain
    for an untraded NSE strike would only spend the shared request budget."""
    if chain.source == "NSE":
        return False
    quotes = chain_quotes(chain)
    lq = [None if y.right == "future" else _quote(quotes, y.right, y.strike) for y in legs]
    return tp.needs_bidask(lq, [y.right for y in legs])


def _with_bidask(lq: list[tp.ChainQuote | None], legs: list[fs.LegIn], other) -> list[tp.ChainQuote | None]:
    """Every option leg's bid/ask from `other` (one chain, one timestamp; never a bid from one source and an ask from
    another); IV, last price and OI stay the plan chain's."""
    oq = chain_quotes(other)
    out: list[tp.ChainQuote | None] = []
    for q, y in zip(lq, legs, strict=True):
        n = None if y.right == "future" else _quote(oq, y.right, y.strike)
        out.append(None if q is None else replace(q, bid=n.bid if n else None, ask=n.ask if n else None))
    return out


def build(chain, lot: int, legs_in: list[fs.LegIn], x: Inputs, *, spec: Any | None = None,
          extra_failed: list[tuple[str, str]] | None = None, title: str | None = None,
          bidask: tuple[Any, str] | None = None, bidask_note: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The trade-plan card for `legs_in` (the base set: its lots are the ratio, the budget sets the multiple). `spec`
    = the library recipe in "Suggest setups"; `extra_failed` = the recipe's own entry checks that failed (name, why).
    `bidask` = (NSE's chain, its label) for the limit guidance when the plan's chain has no bid/ask for a leg (#324);
    `bidask_note` = why it is missing when it was needed and could not be read.
    Raises ValueError when a leg has no price (signals.fno.analyse)."""
    sym, expiry = chain.symbol.upper(), chain.expiry
    spot, dte = float(chain.underlying), (chain.expiry - x.today).days
    t = years_to_expiry(
        chain.expiry, session_clock(x.today, x.now)
    )  # #359: to the minute; ≤ 0 → analyse refuses it
    plan_legs = [tp.PlanLeg(y.right, y.strike, y.side, y.lots) for y in legs_in]
    kind = tp.classify(plan_legs)
    ev = _evidence(kind, sym, dte, x, spec)
    # #356: the cost-complete EV and the liquidity check read NSE's bid/ask when the plan's chain has none (#324); a
    # hold-to-expiry recipe pays no exit half-spread, every other plan is assumed to exit early (p = 1, conservative)
    held = (
        ev["spec"] is not None and (spec is not None or ev["match"] == "exact") and ev["spec"].hold_to_expiry
    )
    ba_chain, p_exit = (bidask[0] if bidask else None), (0.0 if held else 1.0)
    a0 = _analyse(chain, lot, legs_in, x, ba_chain, p_exit)
    if spec is None and ev["match"] == "exact":
        # legs that are exactly a library recipe borrow its evidence, so they also get its registered entry filter
        # (#355: never looser than the recipe once the untested IV gate is context only)
        extra_failed = [*(extra_failed or []), *_recipe_checks(ev["spec"], x)]
    ob = book_for(x.book, x.capital, sym, kind, spot, ev["spec"] if ev["match"] == "exact" else None)
    sz = tp.size(
        a0["max_loss"], a0["costs"]["entry"]["total"], x.budget, proven=ev["level"] == "proven", book=ob
    )
    # the paper book takes at most 100 lots a leg (api/fno_book.PaperLegIn): never size a plan beyond it
    m = max(1, min(sz.suggested, 100 // max(y.lots for y in legs_in)))
    legs = [fs.LegIn(y.right, y.strike, y.side, y.lots * m, y.premium) for y in legs_in]
    a = _analyse(chain, lot, legs, x, ba_chain, p_exit) if m > 1 else a0
    _, factors, gates = fs.assess(a, defined_risk_only=x.defined_risk_only)
    d = decide(
        gates,
        a0["max_loss"],
        a0["costs"]["entry"]["total"],
        x.budget,
        ev["level"],
        extra_failed,
        ob,
        stale=ev["stale"],
    )
    action, qualifier, failed = d.action, d.qualifier, d.failed
    reasons = [f"{f.name}: {f.explanation}" for f in factors if f.name in failed] + d.reasons
    if x.capital <= 0:
        reasons.insert(0, "F&O capital is not set in your profile, so no size fits a budget: set F&O capital and the "
                          "max-loss % on the profile page.")  # fmt: skip
    if ev["level"] == "harmful":
        reasons.insert(0, f"The matching pre-registered backtest ({ev['spec_name']}): {ev['text']}")
    quotes = chain_quotes(chain)
    lq = [None if y.right == "future" else _quote(quotes, y.right, y.strike) for y in legs]
    from finresearch.signals import bidask as ba

    bq, bsrc = lq, ba.label(chain, x.today)
    if tp.needs_bidask(lq, [y.right for y in legs]) and chain.source != "NSE":
        bq, bsrc = (_with_bidask(lq, legs, bidask[0]), bidask[1]) if bidask else (lq, None)
    limits = tp.limit_guidance([tp.PlanLeg(y.right, y.strike, y.side, y.lots) for y in legs], bq, lot)
    if bidask is None and bsrc is None and bidask_note:
        limits["notes"].insert(0, bidask_note)
    src = bsrc if limits["status"] == "ok" else "unknown"
    exits = tp.spec_exits(ev["spec"]) if ev["spec"] is not None and (spec is not None or ev["match"] == "exact") \
        else tp.default_exits(kind, dte)  # fmt: skip
    # levels at the planned entry prices: the bid/ask mid when known, else the analysis' last prices (labelled)
    entry_src = f"bid/ask mid ({src})" if limits["status"] == "ok" else "last traded price (no live bid/ask)"
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
    label = action if qualifier is None else qualifier if action == tp.PAPER else f"{action} ({qualifier})"
    if action == tp.ENTER and not reasons:
        reasons.append(
            "Every check passes: defined risk, within budget, bid/ask spreads within 10% of the premium."
        )
    elif action == tp.PAPER:
        reasons.append("Every check passes, but no strategy that passed its pre-registered out-of-sample test "
                       "matches these legs: paper-trade it to test it; 0 real-money lots (#350).")  # fmt: skip
    return {
        "title": title or f"{sym} {expiry:%d-%b-%Y} · {kind.replace('_', ' ')}",
        "symbol": sym, "expiry": expiry.isoformat(), "days_to_expiry": dte, "spot": spot, "lot_size": lot,
        "as_of": chain.as_of.isoformat() if chain.as_of else None, "source": chain.source,
        "kind": kind, "action": action, "qualifier": qualifier, "label": label, "failed": failed, "reasons": reasons,
        "legs": [{"right": y.right, "strike": y.strike, "side": y.side, "lots": y.lots, "qty": al["qty"],
                  "last": al["premium"]} for y, al in zip(legs, a["legs"], strict=True)],
        "size": {"multiple": sz.multiple, "suggested": m if sz.suggested else 0,
                 "real_lots": min(d.size.real_lots, m), "open_book": book_json(ob), "budget": round(sz.budget, 2),
                 "risk_per_set": None if sz.risk_per_multiple is None else round(sz.risk_per_multiple, 2),
                 "reason": sz.reason, "capital": x.capital, "max_loss_pct": x.max_loss_pct},
        "entry": {**limits, "levels_priced_at": entry_src, "source": src},
        "max_profit": a["max_profit"], "max_loss": a["max_loss"], "risk_shape": a["risk_shape"], "breakevens": a["breakevens"],
        "net_premium": a["net_premium"], "costs": a["costs"]["total_to_expiry"],
        # #356: model EVs after statutory costs and half-spreads, informational ("model EV - not validated")
        "ev": {"label": a["model_ev"]["label"], "range": a["model_ev"]["range"], "note": a["model_ev"]["note"],
               "risk_neutral": None if rn is None else rn["ev"], "real_world": None if rw is None else rw["ev"],
               "real_world_window": (rw or {}).get("window"), "costs": a["costs"]["total_with_spreads"],
               "half_spreads": a["costs"]["half_spreads"],
               "payoff_ratio": (a["pnl_dots"] or {}).get("payoff_ratio")},  # #357: E[win] / |E[loss]|
        "pnl_dots": a["pnl_dots"],
        # #357: every F&O card's probability carries its reliability (#376: logged cards are recorded and scored)
        "reliability": card_reliability(None if rw is None else rw["pop"], x.scored),
        "probability": {"real_world": None if rw is None else rw["pop"], "real_world_window": (rw or {}).get("window"),
                        "risk_neutral": None if rn is None else rn["pop"],
                        # #433: since #376 each logged card is a forecast in the ledger (fnobook.card_forecasts)
                        "note": "Model probabilities of a positive P&L after costs at expiry (lognormal; real prices "
                                "have fat tails). The card is recorded in the forecast ledger and scored on its P&L "
                                "under the card's exit plan; its reliability line says how many are scored yet."},
        "exits": {"source": exits.source, "rule_of_thumb": tp.RULE_OF_THUMB if exits.source == "rule of thumb" else None,
                  "stop": lv["stop"], "target": lv["target"], "entry_net": lv["entry_net"], "credit": lv["credit"],
                  "per_share_unit": lv["per_share_unit"], "notes": lv["notes"], "time": _time_exit(exits, expiry),
                  "profit_pct": exits.profit_pct, "stop_pct": exits.stop_pct},
        "exit_rules": _exit_rules(exits, kind, legs, x.is_index),
        "adjustments": {"rules": ADJUST[family], "note": ADJUST_NOTE},
        "invalidation": _invalidation(kind, legs, spot, x.events, x.today, expiry, x.is_index),
        "event_check": event_check(x, expiry),
        "evidence": {k: v for k, v in ev.items() if k != "spec"},
        # the plan:<spec> tag only counts for an exact match: a related spec's trades are another underlying's
        "paper": paper_record(x.paper, sym, kind, ev["spec_key"] if ev["match"] == "exact" else None),
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


def recipe_legs(
    spec: Any, chain, today: date, rate: float, now: datetime | None = None
) -> list[fs.LegIn] | str:
    """The spec's legs on the live chain (its rule from backtest.engine.choose_legs: the short leg by delta, the wing
    width_pct of spot away), or the reason none can be picked. Deltas at T to the minute from `now` (#359)."""
    spot, t = float(chain.underlying), years_to_expiry(chain.expiry, session_clock(today, now))
    if t <= 0:
        return f"the {chain.expiry:%d-%b-%Y} contract has expired (15:30 IST)"
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


def recipe_checks(spec: Any, strategies: list[dict[str, Any]] | None,
                  iv_points: list[tuple[Any, float | None]]) -> list[tuple[str, str]]:  # fmt: skip
    """The recipe's registered entry filter (iv_pct_min, 0.30 for the sellers in backtest.specs): part of the recipe's
    definition, labelled as such (#355), unlike the IV context of ad hoc legs, which is never a gate. Live, it reads
    the IV percentile as the context panel does (#354: the last 252 sessions, ≥ 80% coverage, flat 30-day IVs
    missing; signals.fno_context.iv_rank_input); the backtest's own filter (backtest.engine) is registered and unchanged."""
    from finresearch.fincalc import volatility as vol

    if spec.iv_pct_min is None:
        return []
    row = next((r for r in strategies or [] if r.get("key") == spec.key), None)
    status = (row or {}).get("status")
    what = {"candidate": "the recipe's registered filter",
            "fails": "the registered filter of a recipe that failed out of sample"}.get(
        status, "the registered filter of a recipe not judged out of sample")  # fmt: skip
    st = vol.iv_stats_sessions(iv_points)
    if st.percentile is None:
        return [("Recipe IV filter", f"IV percentile unknown ({st.status}): the recipe enters only when the 30-day IV "
                 f"is at or above its {spec.iv_pct_min:.0%} percentile of the past year ({what}).")]  # fmt: skip
    if st.percentile < spec.iv_pct_min:
        return [("Recipe IV filter", f"IV percentile {st.percentile:.0%} is below the recipe's {spec.iv_pct_min:.0%} "
                 f"filter ({what}).")]  # fmt: skip
    return []


def _recipe_checks(spec: Any, x: Inputs) -> list[tuple[str, str]]:
    return recipe_checks(spec, x.strategies, x.iv_points)


def waiting(spec: Any, symbol: str, x: Inputs, why: str) -> dict[str, Any]:
    """A recipe with no trade today: WAIT (or AVOID when its own backtest is harmful), with no legs."""
    ev = _evidence(spec.kind, symbol, spec.dte_min, x, spec)
    action, _ = tp.verdict(["Entry window"], ev["level"])
    reasons = [why] + ([f"The pre-registered backtest: {ev['text']}"] if ev["level"] == "harmful" else [])
    return {"title": f"{symbol.upper()} · {spec.name}", "symbol": symbol.upper(), "kind": spec.kind, "action": action,
            "qualifier": None, "label": action, "reasons": reasons, "legs": [], "failed": ["Entry window"],
            "evidence": {k: v for k, v in ev.items() if k != "spec"}, "spec_key": spec.key}  # fmt: skip


async def suggest(symbol: str, expiries: list[date], get_chain, lot_for, x: Inputs,
                  get_bidask=None) -> list[dict[str, Any]]:  # fmt: skip
    """One card per non-overlay library recipe on `symbol` today. `get_chain(expiry)`, `lot_for(expiry)` and
    `get_bidask(expiry)` (-> (chain, label) or None: the bid/ask fallback, #324) are async; each expiry's chain is read
    once, and its bid/ask fallback at most once, on the first card of that expiry that needs it (a failure is kept for
    the other cards of this call: no retry per card)."""
    from finresearch.backtest import specs
    from finresearch.signals import bidask as ba

    chains: dict[date, Any] = {}
    fallback: dict[date, tuple[Any, str] | None] = {}
    out = []
    for spec in specs.SPECS:
        if spec.role != "candidate" or spec.underlying != symbol.upper():
            continue  # #355: controls, overlays and another underlying's recipes are never served as setups
        e = recipe_expiry(spec, expiries, x.today)
        if e is None:
            kind = "monthly " if spec.expiry_kind == "monthly" else ""
            out.append(waiting(spec, symbol, x, f"No {kind}expiry {spec.dte_min}-{spec.dte_max} days away today: "
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
        legs = recipe_legs(spec, chain, x.today, x.rate, x.now)
        if isinstance(legs, str):
            out.append(waiting(spec, symbol, x, f"Cannot build the recipe's legs today: {legs}."))
            continue
        if get_bidask is not None and e not in fallback and needs_bidask(chain, legs):
            fallback[e] = await get_bidask(e)
        try:
            card = build(chain, lot, legs, x, spec=spec, extra_failed=_recipe_checks(spec, x),
                         title=f"{symbol.upper()} {e:%d-%b-%Y} · {spec.name}", bidask=fallback.get(e),
                         bidask_note=ba.FAILED_NOTE if e in fallback else None)  # fmt: skip
        except ValueError as err:
            out.append(waiting(spec, symbol, x, str(err)))
            continue
        out.append({**card, "spec_key": spec.key})
    return out
