"""Market facts for the valuation agents (#504 part A): read-only, deterministic, non-personal market data only.

- risk_free_rate: the answer of the rate chain (signals.rates, #479: the owner-entered yield, else the RBI repo-rate
  proxy, else unknown) for a tenor of 1 to 364 days, with its tier, date and source, and the effective annual rate a
  CAPM takes. Nothing is defaulted: unknown is {"known": false, "reason": ...}.
- index_history: an NSE index's stored daily closes (index_eod, NSE's daily index close file) with P/E, P/B and
  dividend yield as printed, in pages.
- stock_beta: the OLS beta of a stock to an index (fincalc.market.beta) from the stored closes of both, on the
  index's sessions, back-adjusted for the stored split and bonus factors; refused with the reason when the series is
  short, stale, gappy, crosses an action whose factor is unknown, or shows a one-session move no stored action
  explains.

Every answer is one bounded record: figures as fixed-precision strings (so a claim can repeat them exactly), the
arguments that reproduce it (`reproduce`) and its provenance (datasets, NSE source files, window, method version).
An unknown answer is {"known": false, "reason": ...} and carries no number. No `citation` is returned: save_claim
has no re-runnable citation for these tools yet (the option-fact kind is not reused; a decision for part B).

Scope (privacy): equity_eod is NSE's full bhavcopy, every listed security whether or not the owner holds it, and
index_eod is every NSE index, so which symbols answer says nothing about the portfolio (unlike the stock options of
fno_bhav, see mcp_server.option_facts). corp_action holds the F&O stock universe's actions. The tools take the one
symbol the agent names and never list what is stored. The owner-entered rate is a market yield the owner typed with
its source, not a holding. The module imports no personal package.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import CorpActionFetch, EquityEodDay, IndexEod, IndexEodDay
from finresearch.fincalc import market
from finresearch.fincalc import riskfree as R
from finresearch.fincalc.dates import today_ist

METHOD_VERSION = "market_facts/1"
TOOLS = ("risk_free_rate", "index_history", "stock_beta")
INDEX_EOD = "index_eod: NSE's daily index close file (stored, local)"
EQUITY_EOD = "equity_eod: NSE's full equity bhavcopy, official closes, unadjusted (stored, local)"
INDEX_NAMES = ("F&O indices are stored under their tickers (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50), "
               "every other index under NSE's name in capitals (NIFTY 500, NIFTY IT, ...)")  # fmt: skip
SHORT_RATE = ("a short rate: the chain holds no yield beyond 364 days and adds no term premium (no G-sec curve is "
              "stored: FBIL is paused, #476). Pair it with an equity risk premium measured over a short rate; with "
              "a premium measured over the 10-year government yield the cost of equity is understated by the "
              "term premium")  # fmt: skip
MAX_ROWS = (
    400  # index_history rows per call (as the document tools' 400 lines); more rows come by `next_start`
)
DEFAULT_HISTORY_DAYS = 366  # index_history's default range: the year to `end`


def _unknown(reason: str, **extra: Any) -> dict[str, Any]:
    return {"known": False, "reason": reason, **extra}


def _q(x: Decimal | float, places: int) -> str:
    return str(Decimal(str(x)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def _day(text: str | None, name: str) -> date | None:
    if text is None or text == "":
        return None
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        raise ValueError(f"{name} must be a date YYYY-MM-DD, got {text!r}") from None


def risk_free_rate(s: Session, tenor: str = "364D", day: str | None = None,
                   today: date | None = None) -> dict[str, Any]:  # fmt: skip
    """signals.rates' answer for `tenor` ("1D" to "364D") on `day` (default today, IST), as an agent reads it."""
    from finresearch.refseries.owner_rates import tenor_days
    from finresearch.signals import rates

    try:
        n = tenor_days(tenor)
    except ValueError as e:
        return _unknown(f"{e}. The rate chain holds no yield beyond {R.MAX_TENOR_DAYS} days (no G-sec curve is "
                        "stored: FBIL is paused, #476): a longer risk-free rate is unknown here")  # fmt: skip
    today = today or (rates.TODAY or today_ist)()
    d = _day(day, "day") or today
    if d > today:
        return _unknown(f"{d} is in the future: no rate is known for it")
    out = rates.rate_on(s, d, n / 365)
    head = {"day": d.isoformat(), "tenor": f"{n}D", "tenor_days": n}
    if not out["known"]:
        return _unknown(out["note"], **head)
    eff = rates.effective_annual(out)
    return {"known": True, **head, "rate_effective_annual": _q(eff, 6), "rate_effective_annual_pct": _q(eff * 100, 4),
            "yield_simple": _q(out["yield"], 6), "rate_continuous": _q(out["rate"], 6), "tier": out["tier"],
            "as_of": out["as_of"], "source": out["source"], "url": out["url"], "note": out["note"],
            "stale": out["stale"], "basis": out["basis"], "caveat": SHORT_RATE,
            "use": "rate_effective_annual is the annual fraction for valuation.cost_of_equity_capm(risk_free=...); "
                   "yield_simple is the yield as RBI quotes it; rate_continuous is for option maths",
            "reproduce": {"tenor": f"{n}D", "day": d.isoformat()},
            "provenance": {"chain": "signals.rates: the owner-entered yield, else the RBI repo-rate proxy, else "
                                    "unknown (fincalc.riskfree)",
                           "computation": "effective annual = (1 + y x D/365)^(365/D) - 1 from the simple "
                                          "Actual/365 yield y over its own term of D days",
                           "method_version": METHOD_VERSION}}  # fmt: skip


def _text(x: Decimal | None) -> str | None:
    """A stored figure as printed, without the column's padding zeros (22000.0000 -> "22000"); None stays None."""
    return None if x is None else format(x.normalize(), "f")


def _file_url(s: Session, manifest: Any, day: date) -> str | None:
    """The NSE archive URL the stored day was loaded from (None when the manifest has no loaded row for it)."""
    return s.scalar(select(manifest.url).where(manifest.day == day, manifest.status == "loaded"))


def index_history(s: Session, symbol: str, start: str | None = None, end: str | None = None,
                  today: date | None = None) -> dict[str, Any]:  # fmt: skip
    """`symbol`'s stored daily closes in [start, end], oldest first, at most MAX_ROWS a call (`next_start` continues).
    Default: the year to `end` (default today, IST). A close NSE did not print stays null."""
    sym = " ".join(str(symbol or "").split()).upper()
    if not sym:
        return _unknown("symbol is required: " + INDEX_NAMES)
    hi = _day(end, "end") or today or today_ist()
    lo = _day(start, "start") or hi - timedelta(days=DEFAULT_HISTORY_DAYS)
    if lo > hi:
        return _unknown(f"start {lo} is after end {hi}")
    rows = s.execute(select(IndexEod.day, IndexEod.close, IndexEod.pe, IndexEod.pb, IndexEod.div_yield,
                            IndexEod.index_name)
                     .where(IndexEod.symbol == sym, IndexEod.day.between(lo, hi))
                     .order_by(IndexEod.day).limit(MAX_ROWS + 1)).all()  # fmt: skip
    if not rows:
        return _unknown(f"no stored index closes for {sym} from {lo} to {hi}. {INDEX_NAMES}")
    more, rows = len(rows) > MAX_ROWS, rows[:MAX_ROWS]
    last = rows[-1][0]
    return {"known": True, "symbol": sym, "index_name": rows[-1][5], "start": lo.isoformat(), "end": hi.isoformat(),
            "first": rows[0][0].isoformat(), "last": last.isoformat(), "n": len(rows),
            "columns": ["day", "close", "pe", "pb", "div_yield_pct"],
            "rows": [[d.isoformat(), _text(c), _text(pe), _text(pb), _text(dy)] for d, c, pe, pb, dy, _ in rows],
            "next_start": (last + timedelta(days=1)).isoformat() if more else None,
            "note": ("more rows: call again with start=next_start" if more else "end of the range")
                    + "; null = not printed by NSE that day (unknown, not 0); closes are index levels, not "
                      "total-return levels (dividends are left out)",
            "reproduce": {"symbol": sym, "start": lo.isoformat(), "end": hi.isoformat()},
            "provenance": {"dataset": INDEX_EOD, "source_url": _file_url(s, IndexEodDay, last),
                           "source_note": "one file per session; the URL is the last returned day's",
                           "method_version": METHOD_VERSION}}  # fmt: skip


# stock_beta's rules, our stated choices (the sample floor and the staleness limit are fincalc.market's):
DEFAULT_SESSIONS = 250  # about one year of NSE sessions of daily returns
MAX_SESSIONS = 1250  # about five years: older daily returns say little about the business as it is now [U]
# [U] the share of the window's index sessions the stock may lack a close on (not listed yet, suspended, a day not
# loaded); the registered F&O beta allows the same tenth (25 of 250 sessions, fincalc.beta_specs)
MAX_MISSING_SHARE = Decimal("0.10")
# [U] a one-session move beyond 35 % that no stored action explains is taken as a split, bonus or demerger missing
# from the stored actions, and the beta is refused: the anomaly threshold of fincalc.signals.adjust_for_actions.
# A smaller unrecorded action (a 1:4 bonus is -20 %) passes it, which is why `largest_move` is always returned.
MAX_SESSION_MOVE = Decimal("0.35")
DAILY_NOTE = ("raw OLS beta on daily simple returns, not shrunk towards 1; a thinly traded stock's daily beta is "
              "biased towards 0 (Scholes & Williams 1977; Dimson 1979); quote the standard error with it")  # fmt: skip


def _window(s: Session, idx: str, as_of: date, sessions: int) -> list[tuple[date, Decimal]]:
    """The last `sessions` + 1 stored closes of the index up to `as_of`, oldest first (a null close is no session)."""
    got = s.execute(select(IndexEod.day, IndexEod.close)
                    .where(IndexEod.symbol == idx, IndexEod.close.is_not(None), IndexEod.day <= as_of)
                    .order_by(IndexEod.day.desc()).limit(sessions + 1)).all()  # fmt: skip
    return [(d, c) for d, c in reversed(got)]


def stock_beta(s: Session, symbol: str, index: str = "NIFTY", end: str | None = None,
               sessions: int = DEFAULT_SESSIONS, today: date | None = None) -> dict[str, Any]:  # fmt: skip
    """The OLS beta of `symbol` to `index` over the last `sessions` stored index sessions up to `end` (default the
    latest stored session, which must then be fresh), on back-adjusted closes; unknown with the reason otherwise."""
    from itertools import pairwise

    from finresearch.fincalc.corporate_actions import adjust_prices
    from finresearch.refseries import corp_actions, equity_eod

    sym, idx = str(symbol or "").strip().upper(), " ".join(str(index or "").split()).upper()
    if not sym or not idx:
        return _unknown("symbol and index are required")
    lo_n, hi_n = market.BETA_MIN_RETURNS, MAX_SESSIONS
    if isinstance(sessions, bool) or not isinstance(sessions, int) or not lo_n <= sessions <= hi_n:
        return _unknown(f"sessions must be a whole number from {lo_n} to {hi_n}, got {sessions!r}")
    today = today or today_ist()
    as_of = _day(end, "end") or today
    if as_of > today:
        return _unknown(f"{as_of} is in the future: no closes are stored for it")
    got = _window(s, idx, as_of, sessions)
    if not got:
        return _unknown(f"no stored index closes for {idx} up to {as_of}. {INDEX_NAMES}")
    if len(got) < sessions + 1:
        return _unknown(f"short: only {len(got) - 1} sessions of {idx} returns are stored up to {as_of}, "
                        f"{sessions} were asked for")  # fmt: skip
    first, last = got[0][0], got[-1][0]
    if (as_of - last).days > market.BETA_MAX_AGE_DAYS:
        return _unknown(f"stale: the stored {idx} closes end on {last}, {(as_of - last).days} days before {as_of} "
                        f"(at most {market.BETA_MAX_AGE_DAYS})")  # fmt: skip
    closes = dict(equity_eod.closes_between(s, sym, first, last))
    if not closes:
        return _unknown(f"no stored closes for {sym} from {first} to {last}: {EQUITY_EOD}; check the NSE symbol "
                        "(a renamed stock's earlier closes are under its old symbol)")  # fmt: skip
    days = [d for d, _ in got if d in closes]
    missing = len(got) - len(days)
    if missing > MAX_MISSING_SHARE * len(got):
        return _unknown(f"misaligned: {sym} has no stored close on {missing} of the {len(got)} {idx} sessions from "
                        f"{first} to {last} (at most {MAX_MISSING_SHARE:.0%}): listed later, suspended, renamed or "
                        "days not loaded")  # fmt: skip
    hist = corp_actions.factor_history(s, sym)
    gaps = [(d, kind, why) for d, kind, why in hist.unknown if days[0] < d <= days[-1]]
    if gaps:
        d, kind, why = gaps[0]
        return _unknown(f"a corporate action of {sym} went ex on {d} with no known price factor ({kind}: {why}): "
                        "the closes on either side of it are not comparable, so the beta is unknown")  # fmt: skip
    events = [(d, f) for d, f in hist.events if days[0] < d <= days[-1]]
    prices = adjust_prices(days, [closes[d] for d in days], events)
    for (_, p0), (d1, p1) in pairwise(zip(days, prices, strict=True)):
        if abs(p1 / p0 - 1) > MAX_SESSION_MOVE:
            return _unknown(f"{sym} moved {float(p1 / p0 - 1):+.1%} on {d1} and no stored corporate action explains "
                            f"it (beyond {MAX_SESSION_MOVE:.0%}): a split, bonus or demerger may be missing from "
                            "the stored actions, so the beta is unknown")  # fmt: skip
    try:
        b = market.beta(prices, [c for d, c in got if d in closes], days, days, as_of=as_of)
    except ValueError as e:
        return _unknown(f"{sym} against {idx}: {e}")
    fetched = s.scalar(select(func.count()).select_from(CorpActionFetch).where(
        CorpActionFetch.source == "nse", CorpActionFetch.symbol == sym, CorpActionFetch.status == "loaded"))  # fmt: skip
    return _beta_answer(s, sym, idx, sessions, b, missing, last, events, bool(fetched))


def _beta_answer(s: Session, sym: str, idx: str, sessions: int, b: market.Beta, missing: int, last: date,
                 events: list[tuple[date, Decimal]], fetched: bool) -> dict[str, Any]:  # fmt: skip
    if fetched:
        adjustment = (f"back-adjusted for {len(events)} stored split, bonus or rights factor(s) in the window "
                      "(corp_action, fincalc.corporate_actions.adjust_prices)")  # fmt: skip
    else:
        adjustment = (f"no corporate-action history is stored for {sym} (it is kept for F&O stocks): the closes "
                      f"are as printed; an action smaller than {MAX_SESSION_MOVE:.0%} in the window would go "
                      "unnoticed, see largest_move")  # fmt: skip
    return {"known": True, "symbol": sym, "index": idx, "beta": _q(b.beta, 4), "se": _q(b.se, 4),
            "r_squared": _q(b.r_squared, 4), "alpha_per_session": _q(b.alpha, 6), "n": b.n,
            "window_start": b.window_start.isoformat(), "window_end": b.window_end.isoformat(),
            "sessions": sessions, "missing_sessions": missing, "frequency": "daily",
            "largest_move": _q(b.largest_move, 4), "largest_move_date": b.largest_move_date.isoformat(),
            "adjustment": adjustment, "adjusted_for": [[d.isoformat(), _text(f)] for d, f in events],
            "note": DAILY_NOTE,
            "reproduce": {"symbol": sym, "index": idx, "end": last.isoformat(), "sessions": sessions},
            "provenance": {"datasets": [EQUITY_EOD, INDEX_EOD],
                           "equity_source_url": _file_url(s, EquityEodDay, b.window_end),
                           "index_source_url": _file_url(s, IndexEodDay, last),
                           "source_note": "one file per session; the URLs are the window's last day's",
                           "computation": f"fincalc.market.beta: {b.method}; closes on the {idx} sessions both "
                                          f"have, at least {b.min_returns} returns, the last close at most "
                                          f"{b.max_age_days} days old",
                           "method_version": METHOD_VERSION}}  # fmt: skip
