"""The forecast ledger: log every probability before the outcome is known, resolve it on its date, score it later.

docs/dev/RESEARCH_ROADMAP.md §C.10 (Steps 1–2) and §E Phase 1 item 1. Metrics live in `finresearch.evals.calibration`.

Recording
* `record(signal, source=..., resolve_on=..., event_kind=...)` logs a signal provider's `Signal`.
* `record_run(session, run_id)` logs a finished research run's verdict (called when a run completes, and by the daily
  monitor step / `finresearch forecasts backfill` for runs finished before the ledger existed).
* Dedupe: at most one open forecast per asset + instrument + event kind + IST day + METHOD (`dedupe_key`; the method
  since #151, so a shadow method logged beside the live call never displaces it, and each method's calibration group
  keeps its own forecast). A newer forecast on the same day replaces the open one of the same method (several
  same-day runs count once, as the last run's call); an older one never replaces a newer one. Rows logged before
  #151 carry the method-less key; a same-day forecast of the SAME method still updates that row (`_key_for`).

Research-run verdicts → probabilities (fixed map v1, until outcomes say otherwise; roadmap §C.10 Step 1)
* The agents only say low / medium / high. That is mapped to P = 0.55 / 0.65 / 0.75 that the verdict's direction is
  right. The model's own words are never shown as a probability (Xiong et al., roadmap source [27]).
* IPO report (`overall_verdict`), event "the NSE listing-day open is above the issue price":
  APPLY, APPLY (listing gains only), APPLY (long term) → P(event) = map; AVOID → P(event) = 1 − map;
  APPLY-CONDITIONAL and NEUTRAL → no call (logged with probability None, resolved for coverage, never scored:
  the condition is free text, and scoring it as 0.5 would only add noise). A forecast made after the listing-day
  open (09:00 IST on the listing date, when the pre-open auction starts) is voided at resolution: its outcome was
  already known. The listing open comes from the IPO harvest's listing bar (ipo_history), the monitor's quote, or NSE's
  daily history ON the listing date (EQ, else the T2T "BE" series); a later session's open is never scored (#292).
* Stock report (`verdict`), event "12-month total return (price + cash dividends, NSE closes) above the price
  return of NIFTYBEES, the Nifty 50 ETF": BUY, ACCUMULATE → map; REDUCE, AVOID → 1 − map; HOLD → no call. Since #241
  a stock report states a research view instead (FAVOURABLE → map, UNFAVOURABLE → 1 − map, MIXED → no call); it is
  still scored, so the track record shows whether the views have an edge, but shown as informational.
  NIFTYBEES stands in for the Nifty 50 TRI because the app has no index-history adapter; it tracks the index less
  its expense ratio. A split, bonus, rights issue, consolidation, demerger or scheme of arrangement in the window
  (of the stock, or a split/consolidation of NIFTYBEES itself) voids the forecast rather than score unadjusted prices.
* BSE-only stocks (instrument "BSE:<scrip code>", inputs `exchange` "BSE"): the same event on BSE closes plus cash
  dividends from BSE's corporate actions. The benchmark stays NIFTYBEES on NSE (the two exchanges share trading days
  and hours), so BSE and NSE stock forecasts are scored against one benchmark and pool in one calibration group. A
  SENSEX-tracking ETF on BSE was the alternative; it would split the stock group by benchmark for no gain in accuracy.
* Fund and bond reports are not logged: neither verdict maps to an event the app can check yet.
* Only reports that passed the publish gate (status "done") are logged.

Resolving
* Resolvers are registered per (asset, event kind) with `@resolver(...)`. Each gets the forecast and the monitor's
  `Deps` and returns a `Resolution`, or raises `NotYet` to be tried again later. `resolve(...)` records an outcome by
  hand (or from a provider's own resolver).
* `resolve_due(deps, now)` runs from the monitor once a day after the close: every open forecast whose `resolve_on`
  has passed is checked (at most every CHECK_EVERY), and voided when still unresolvable VOID_AFTER days later.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import AgentStep, Company, Forecast, IpoOffer, ResearchRun, Watch
from finresearch.fincalc.dates import IST, add_years, next_business_day, to_ist
from finresearch.signals.base import Signal

log = logging.getLogger(__name__)

CONFIDENCE_P = {"low": 0.55, "medium": 0.65, "high": 0.75}
RUN_METHOD = "report verdict, fixed confidence map v1 (low 0.55 / medium 0.65 / high 0.75)"
IPO_EVENT = "NSE listing-day open above the issue price"
STOCK_EVENT = ("12-month total return (NSE closes plus cash dividends) above the price return of NIFTYBEES "
               "(Nifty 50 ETF) over the same dates")  # fmt: skip
STOCK_EVENT_BSE = ("12-month total return (BSE closes plus cash dividends) above the price return of NIFTYBEES "
                   "(Nifty 50 ETF, NSE) over the same dates")  # fmt: skip
BENCHMARK = "NIFTYBEES"
IPO_UP = ("APPLY", "APPLY (listing gains only)", "APPLY (long term)")
IPO_DOWN = ("AVOID",)
STOCK_UP = ("BUY", "ACCUMULATE", "FAVOURABLE")
STOCK_DOWN = ("REDUCE", "AVOID", "UNFAVOURABLE")
CHECK_EVERY = timedelta(hours=6)
VOID_AFTER = timedelta(days=60)
READY_AFTER_IST = (16, 30)  # resolve after the close, when NSE has published the day's prices
# The last moment a retail bidder can act: bids and UPI mandates close at 5 pm IST on the issue's last bidding day
# (SEBI T+3 listing circular SEBI/HO/CFD/TPD1/CIR/P/2023/140, 9-Aug-2023; #353 "the 17:00 UPI cut-off"). An IPO is
# scored on its last forecast BEFORE this on that day: the final book is published after it, so a later forecast
# uses information no bidder had (pre-registered in evals/experiments/ipo_calibration/ADDENDUM.md, 21-Oct-2026).
IPO_CUTOFF = time(17, 0)
EXPECTED_LISTING_DAYS = (
    7  # no known listing date: first check a week after the report (retried until it lists)
)


class NotYet(RuntimeError):
    """The outcome is not observable yet (not listed, no price for the date); try again later."""


@dataclass
class Resolution:
    outcome: int | None  # 1 / 0; None = void (cannot be scored honestly)
    value: float | None = None
    note: str = ""


Resolver = Callable[[Forecast, Any, Session], Awaitable[Resolution]]
RESOLVERS: dict[tuple[str, str], Resolver] = {}


def resolver(asset: str, event_kind: str) -> Callable[[Resolver], Resolver]:
    def deco(fn: Resolver) -> Resolver:
        RESOLVERS[(asset, event_kind)] = fn
        return fn

    return deco


def ist_day(dt: datetime) -> date:
    return to_ist(dt).date()


def dedupe_key(asset: str, instrument: str, event_kind: str, day: date, method: str | None = None) -> str:
    """One open forecast per asset + instrument + event kind + IST day + method (a 12-hex hash of the method keeps the
    key short). Without a method: the pre-#151 key."""
    base = f"{asset}:{instrument}:{event_kind}:{day.isoformat()}"
    if method is None:
        return base
    import hashlib

    return f"{base}:{hashlib.sha1(method.encode()).hexdigest()[:12]}"


def _key_for(session: Session, asset: str, instrument: str, event_kind: str, day: date, method: str,
             created: datetime | None = None) -> str:  # fmt: skip
    """The key for a new forecast: the legacy method-less key when a pre-#151 row of the SAME method holds it (so it
    keeps being updated, not duplicated), else the per-method key. An IPO forecast made at or after 17:00 IST gets
    its own slot (#353), so a view after the bidding cut-off never overwrites the day's decision-time forecast."""
    if event_kind in POINT_EVENTS and created is not None and to_ist(created).time() >= IPO_CUTOFF:
        return f"{dedupe_key(asset, instrument, event_kind, day, method)}:after-cutoff"
    legacy = dedupe_key(asset, instrument, event_kind, day)
    held = session.scalar(select(Forecast.method).where(Forecast.dedupe_key == legacy))
    return (
        legacy
        if held is not None and held == method[:200]
        else dedupe_key(asset, instrument, event_kind, day, method)
    )


# ------------------------------------------------------------------------------------------------------ recording
def _upsert(session: Session, values: dict[str, Any]) -> int:
    ins = insert(Forecast).values(**values)
    update_cols = {k: ins.excluded[k] for k in values if k not in ("dedupe_key",)}
    stmt = ins.on_conflict_do_update(
        index_elements=["dedupe_key"], set_=update_cols,
        where=(Forecast.status == "open") & (Forecast.created_at <= ins.excluded.created_at),
    ).returning(Forecast.id)  # fmt: skip
    fid = session.scalars(stmt).first()
    if fid is None:  # a newer or already-resolved forecast holds the key
        fid = session.scalar(select(Forecast.id).where(Forecast.dedupe_key == values["dedupe_key"]))
    session.flush()
    return fid


def record(signal: Signal, *, source: str, resolve_on: date, event_kind: str, session: Session | None = None,
           run_id: int | None = None, inputs: dict[str, Any] | None = None,
           created_at: datetime | None = None) -> int | None:  # fmt: skip
    """Log a provider's signal as a forecast of `event_kind` (a key with a registered resolver). A NO_SIGNAL is not
    logged. Returns the forecast id (the existing one when a newer forecast already holds today's key)."""
    if signal.action == "NO_SIGNAL":
        return None
    created = created_at or signal.as_of or datetime.now(UTC)
    lo, hi = signal.probability_interval or (None, None)
    values = dict(created_at=created, asset=signal.asset, instrument=signal.instrument, name=signal.name,
                  source=source, run_id=run_id, event_kind=event_kind, event=signal.event, horizon=signal.horizon,
                  resolve_on=resolve_on, probability=signal.probability, interval_low=lo, interval_high=hi,
                  action=signal.action, score=signal.score, method=signal.method[:200],
                  validation_status=signal.validation.status,
                  inputs={"factors": [{"name": f.name, "value": _plain(f.value), "contribution": f.contribution}
                                      for f in signal.factors],
                          "base_rate": signal.base_rate, "sources": signal.sources, **(inputs or {})},
                  status="open")  # fmt: skip

    def put(s: Session) -> int:
        values["dedupe_key"] = _key_for(s, signal.asset, signal.instrument, event_kind, ist_day(created),
                                        signal.method[:200], created)  # fmt: skip
        return _upsert(s, values)

    if session is not None:
        return put(session)
    from finresearch.db import session_scope

    with session_scope() as s:
        return put(s)


def _plain(v: Any) -> Any:
    return str(v) if isinstance(v, Decimal) else v


def final_synthesis(session: Session, run_id: int) -> dict[str, Any] | None:
    st = session.scalars(select(AgentStep).where(AgentStep.run_id == run_id, AgentStep.stage == "synthesis",
                                                 AgentStep.status == "done")
                         .order_by(AgentStep.finished_at.desc().nulls_last(), AgentStep.id.desc())).first()  # fmt: skip
    return (st.output or None) if st else None


def verdict_probability(
    kind: str, verdict: str | None, confidence: str | None
) -> tuple[float | None, str] | None:
    """(P(event), direction) for a report verdict under the fixed map; (None, "no call") when the verdict takes no
    side; None when the kind has no checkable event."""
    base = CONFIDENCE_P.get((confidence or "").lower())
    if kind == "ipo_report":
        up, down = IPO_UP, IPO_DOWN
    elif kind == "stock_report":
        up, down = STOCK_UP, STOCK_DOWN
    else:
        return None
    if verdict in up and base is not None:
        return base, "up"
    if verdict in down and base is not None:
        return round(1 - base, 4), "down"
    return None, "no call"


def _ipo_issue_price(session: Session, run: ResearchRun) -> Decimal | None:
    from finresearch.suggest.rules import _ledger_fact, _upper_from

    return _ledger_fact(session, run.id, "price_band_upper") or _upper_from(
        ((run.manifest or {}).get("facts") or {}).get("issue_info") or {}
    )


def record_run(session: Session, run_id: int) -> int | None:
    """Log a finished research run's verdict as a forecast (see the module docstring for the mapping)."""
    run = session.get(ResearchRun, run_id)
    if run is None or run.status != "done" or run.kind not in ("ipo_report", "stock_report"):
        return None
    co = session.get(Company, run.company_id) if run.company_id else None
    syn = final_synthesis(session, run_id)
    if co is None or not syn:
        return None
    verdict = syn.get("overall_verdict") if run.kind == "ipo_report" else syn.get("verdict")
    mapped = verdict_probability(run.kind, verdict, syn.get("confidence"))
    if mapped is None:
        return None
    p, direction = mapped
    created = run.finished_at or datetime.now(UTC)
    day = ist_day(created)
    inputs: dict[str, Any] = {"run_id": run.id, "verdict": verdict, "confidence": syn.get("confidence"),
                              "direction": direction, "mapping": CONFIDENCE_P}  # fmt: skip
    if run.kind == "ipo_report":
        watch = session.scalar(select(Watch).where(Watch.company_id == co.id))
        bse_no = (co.meta or {}).get("bse_ipo_no")
        instrument = co.nse_symbol or (f"BSE:{bse_no}" if bse_no else None)
        if not instrument:
            return None
        offer = session.scalar(
            select(IpoOffer).where(IpoOffer.company_id == co.id).order_by(IpoOffer.id.desc())
        )
        listing = (watch.listing_date if watch else None) or (offer.listing_date if offer else None)
        resolve_on = max(listing, day) if listing else day + timedelta(days=EXPECTED_LISTING_DAYS)
        issue = _ipo_issue_price(session, run)
        inputs |= {"symbol": co.nse_symbol, "issue_price": str(issue) if issue is not None else None,
                   "issue_price_basis": "upper end of the price band (ledger fact or NSE issue info)",
                   "listing_date": listing.isoformat() if listing else None,
                   "listing_date_basis": "watch / NSE" if listing else "unknown; checked from a week after the report",
                   "bse_ipo_no": bse_no, "listing_verdict": syn.get("verdict_listing")}  # fmt: skip
        event_kind, event, horizon, asset = "listing_gain", IPO_EVENT, "listing day", "ipo"
    else:
        if not co.nse_symbol and not co.bse_code:
            return None
        end = add_years(day, 1)
        from finresearch.adapters.nse_holidays import trading_holidays
        from finresearch.fincalc.dates import is_business_day

        hol = trading_holidays()
        resolve_on = end if is_business_day(end, hol) else next_business_day(end, hol)
        instrument = co.nse_symbol or f"BSE:{co.bse_code}"  # a BSE-only stock is scored on BSE closes
        inputs |= {"symbol": instrument, "start_date": day.isoformat(), "benchmark": BENCHMARK,
                   "report_horizon": syn.get("horizon"), "entry_zone": syn.get("entry_zone"),
                   "note": "the report's own horizon may be longer; the ledger scores the 12-month call"}  # fmt: skip
        if not co.nse_symbol:
            inputs |= {"exchange": "BSE", "benchmark_exchange": "NSE"}
        event_kind, event, horizon, asset = "excess_return_12m", STOCK_EVENT if co.nse_symbol else STOCK_EVENT_BSE, \
            "12 months", "stock"  # fmt: skip
    values = dict(created_at=created, asset=asset, instrument=instrument, name=co.name, source=f"run:{run.id}",
                  run_id=run.id, event_kind=event_kind, event=event, horizon=horizon, resolve_on=resolve_on,
                  probability=p, interval_low=None, interval_high=None, action=str(verdict)[:40], score=None,
                  method=RUN_METHOD, validation_status="uncalibrated", inputs=inputs, status="open",
                  dedupe_key=_key_for(session, asset, instrument, event_kind, day, RUN_METHOD, created))  # fmt: skip
    return _upsert(session, values)


def record_run_safely(run_id: int) -> int | None:
    """The pipeline's hook: logging a forecast must never fail or block a finished run."""
    from finresearch.db import session_scope

    try:
        with session_scope() as s:
            return record_run(s, run_id)
    except Exception:
        log.warning("could not log a forecast for run %s", run_id, exc_info=True)
        return None


def backfill_runs(session: Session) -> list[int]:
    """Log forecasts for finished runs, oldest first (so each day's latest run wins the dedupe key)."""
    ids = session.scalars(select(ResearchRun.id).where(ResearchRun.status == "done",
                                                       ResearchRun.kind.in_(("ipo_report", "stock_report")))
                          .order_by(ResearchRun.finished_at.asc().nulls_last(), ResearchRun.id)).all()  # fmt: skip
    out = []
    for rid in ids:
        fid = record_run(session, rid)
        if fid is not None:
            out.append(fid)
    return sorted(set(out))


# ------------------------------------------------------------------------------------------------------ resolving
def resolve(session: Session, forecast_id: int, outcome: int | None, *, value: float | None = None, note: str = "",
            now: datetime | None = None) -> Forecast:  # fmt: skip
    """Record an outcome (1 / 0), or void the forecast with outcome None. A resolved forecast is never changed."""
    f = session.get(Forecast, forecast_id)
    if f is None:
        raise LookupError(f"unknown forecast {forecast_id}")
    if f.status != "open":
        raise ValueError(f"forecast {forecast_id} is already {f.status}")
    if outcome not in (0, 1, None):
        raise ValueError("outcome must be 0, 1 or None (void)")
    now = now or datetime.now(UTC)
    f.outcome, f.resolution_value, f.resolution_note = outcome, value, note[:2000] or None
    f.status = "void" if outcome is None else "resolved"
    f.resolved_at = f.last_checked_at = now
    return f


def due(session: Session, now: datetime, *, recheck: bool = False) -> list[Forecast]:
    """Open forecasts whose date has passed (after the close), not checked in the last CHECK_EVERY unless `recheck`
    (`finresearch forecasts resolve --recheck`: a manual pass right after new data, e.g. an IPO harvest)."""
    ist = to_ist(now)
    last_day = ist.date() if (ist.hour, ist.minute) >= READY_AFTER_IST else ist.date() - timedelta(days=1)
    q = select(Forecast).where(Forecast.status == "open", Forecast.resolve_on <= last_day)
    if not recheck:
        q = q.where((Forecast.last_checked_at.is_(None)) | (Forecast.last_checked_at < now - CHECK_EVERY))
    return list(session.scalars(q.order_by(Forecast.resolve_on, Forecast.id)))


async def resolve_due(deps: Any, now: datetime | None = None, *, recheck: bool = False) -> dict[str, int]:
    """Try every due forecast once; each in its own transaction so one failure does not undo the others."""
    from finresearch.db import session_scope
    from finresearch.direction import (
        live,  # noqa: F401  (importing it registers the F&O direction resolvers, #327)
    )
    from finresearch.fnobook import (
        card_forecasts,  # noqa: F401  (importing it registers the F&O card resolver, #376)
    )

    now = now or datetime.now(UTC)
    with session_scope() as s:
        ids = [f.id for f in due(s, now, recheck=recheck)]
    stats = {"resolved": 0, "void": 0, "waiting": 0, "errors": 0}
    for fid in ids:
        with session_scope() as s:
            f = s.get(Forecast, fid)
            fn = RESOLVERS.get((f.asset, f.event_kind))
            try:
                if fn is None:
                    raise NotYet(f"no resolver for {f.asset}/{f.event_kind} yet")
                r = await fn(f, deps, s)
            except NotYet as e:
                late = datetime.combine(f.resolve_on, datetime.min.time(), tzinfo=UTC) + VOID_AFTER < now
                if late:
                    resolve(s, fid, None, note=f"void: still unresolvable {VOID_AFTER.days} days after the date ({e})",
                            now=now)  # fmt: skip
                    stats["void"] += 1
                else:
                    f.last_checked_at, f.resolution_note = now, f"waiting: {e}"[:2000]
                    stats["waiting"] += 1
                continue
            except Exception as e:  # network trouble: try again at the next check
                log.warning("resolving forecast %s failed", fid, exc_info=True)
                f.last_checked_at, f.resolution_note = now, f"error: {type(e).__name__}: {e}"[:2000]
                stats["errors"] += 1
                continue
            resolve(s, fid, r.outcome, value=r.value, note=r.note, now=now)
            stats["void" if r.outcome is None else "resolved"] += 1
    return stats


def _dec(x: Any) -> Decimal | None:
    try:
        return Decimal(str(x)) if x is not None else None
    except Exception:
        return None


LISTING_OPEN_IST = time(9, 0)


def _history_row(session: Session, symbol: str | None):
    """The IPO harvest's row for an NSE symbol (newest listing first), if any. BSE-only symbols have none."""
    if not symbol or symbol.startswith("BSE:"):
        return None
    from finresearch.db.models import IpoHistory

    return session.scalars(select(IpoHistory).where(IpoHistory.symbol == symbol,
                                                     IpoHistory.listing_date.is_not(None))
                           .order_by(IpoHistory.listing_date.desc())).first()  # fmt: skip


def _history_open(row, listing: date) -> tuple[Decimal | None, str | None]:
    """The harvest's listing open when its stored bar is ON the listing date (the harvest itself accepts the first bar
    on or after it); else None. Returns (open, series)."""
    if row is None or row.listing_date != listing or row.list_open is None:
        return None, None
    from finresearch.evals.ipo_history import listing_bar

    bar = listing_bar((row.raw or {}).get("bars") or [], listing)
    if bar is None or bar.get("day") != listing.isoformat():
        return None, None
    return _dec(row.list_open), ((row.source or {}).get("history") or {}).get("series")


def _is_listing_bar(bar, issue: Decimal | None) -> bool:
    """NSE sets the listing day's previous close to the issue price; a later session's previous close is the last
    traded price. Used only when the listing date is unknown."""
    prev = _dec(getattr(bar, "prev_close", None))
    return prev is not None and issue is not None and issue > 0 and abs(prev / issue - 1) <= Decimal("0.01")


async def _nse_listing_open(deps: Any, symbol: str, listing: date | None, since: date, until: date,
                            issue: Decimal | None) -> tuple[Decimal, date, str] | None:  # fmt: skip
    """The listing-day open from NSE's daily history: EQ first, then the T2T (BE) series, where an issue that listed in
    trade-for-trade has its first sessions (its EQ rows start ~10 sessions later). With a known listing date the first
    bar must be ON that date; without one, the first bar after the forecast must carry the listing-day marker
    (previous close = issue price). A later session's open is never returned as the listing open."""
    start = listing or since
    end = max(start, until) + timedelta(days=10)
    sources = [("EQ", deps.price_history)]
    if getattr(deps, "t2t_price_history", None) is not None:
        sources.append(("BE", deps.t2t_price_history))
    for series, fetch in sources:
        bars = [b for b in await fetch(symbol, start, end) if b.open is not None and b.day >= start]
        if not bars:
            continue
        first = bars[0]
        if listing is not None and first.day == listing:
            return first.open, first.day, series
        if listing is None and _is_listing_bar(first, issue):
            return first.open, first.day, series
    return None


@resolver("ipo", "listing_gain")
async def resolve_listing_gain(f: Forecast, deps: Any, session: Session) -> Resolution:
    """Listing-day open vs the issue price. The listing date: the watch's, else the IPO harvest's (NSE's own), else
    the one NSE stated when the forecast was logged (inputs). The open, in order: the harvest's listing bar
    (ipo_history.list_open, when its bar is on the listing date), the monitor's quote on the listing day (watch),
    NSE's daily history (EQ, then the T2T BE series) on exactly the listing date. Otherwise it keeps waiting (#292)."""
    from finresearch.fincalc.ipo import listing_gain

    inp = f.inputs or {}
    symbol = inp.get("symbol") or f.instrument
    watch = session.scalar(select(Watch).where(Watch.nse_symbol == symbol)) if symbol else None
    issue = _dec(inp.get("issue_price"))
    if issue is None and watch is not None:
        from finresearch.monitor.jobs import _upper_band

        issue = _upper_band(session, watch)
    hist = _history_row(session, symbol)
    stated = None
    if inp.get("listing_date"):
        try:
            stated = date.fromisoformat(str(inp["listing_date"])[:10])
        except ValueError:
            stated = None
    # a confirmed date (the watch's, or NSE's via the harvest) can void a hindsight forecast; the date stated when the
    # forecast was logged may be indicative, so it only picks the bar (a wrong one waits for the harvest)
    confirmed = (watch.listing_date if watch is not None else None) or (hist.listing_date if hist else None)
    listing = confirmed or stated
    if confirmed is not None and to_ist(f.created_at) >= datetime.combine(
        confirmed, LISTING_OPEN_IST, tzinfo=IST
    ):
        # the listing-day open (pre-open call auction 09:00-09:08, trading from 09:15) was already known: scoring it
        # would put a hindsight "forecast" in the calibration
        return Resolution(
            None, None, f"void: forecast made after the listing-day open ({confirmed}, 09:00 IST)"
        )
    open_price, listed_on, basis = None, None, None
    h_open, h_series = _history_open(hist, listing) if listing is not None else (None, None)
    if h_open is not None:
        open_price, listed_on = h_open, listing
        basis = f"NSE listing-day bar ({h_series or 'EQ'} series) from the IPO history harvest"
    elif watch is not None and (watch.meta or {}).get("listing_open") is not None:
        open_price, listed_on = _dec(watch.meta["listing_open"]), watch.listing_date
        basis = "exchange quote on the listing day (monitor)"
    elif symbol and not symbol.startswith("BSE:") and getattr(deps, "price_history", None) is not None:
        got = await _nse_listing_open(deps, symbol, listing, ist_day(f.created_at), f.resolve_on, issue)
        if got is not None:
            open_price, listed_on, series = got
            basis = f"NSE daily history ({series} series, listing day)"
        elif listing is not None:
            raise NotYet(f"{f.instrument}: NSE has no {listing} (listing day) bar in the EQ or BE series yet")
    if open_price is None:
        raise NotYet(f"{f.instrument} has no listing-day open yet")
    if issue is None:
        return Resolution(
            None, None, f"void: listed at ₹{open_price} on {listed_on} but the issue price is unknown"
        )
    gain = listing_gain(issue, open_price) * 100
    return Resolution(1 if open_price > issue else 0, float(gain),
                      f"opened at ₹{open_price} on {listed_on} vs issue ₹{issue} ({gain:+.2f}%); {basis}")  # fmt: skip


# share-count or perimeter changes that NSE/BSE closes are not adjusted for: the forecast is voided rather than scored on
# unadjusted prices. A demerger or scheme of arrangement moves part of the value into another listed company (the
# Tata Motors CV demerger, Nov-2025), so the parent's close drops with no split or bonus in the subject.
VOIDING_ACTIONS = ("split", "bonus", "rights", "consolidation", "sub-division", "subdivision", "demerger",
                   "de-merger", "scheme of arrangement", "amalgamation", "reduction of capital", "capital reduction")  # fmt: skip


def _close_on_or_before(bars: list, day: date):
    ok = [b for b in bars if b.close is not None and b.day <= day]
    return ok[-1] if ok else None


@resolver("stock", "excess_return_12m")
async def resolve_excess_return(f: Forecast, deps: Any, session: Session) -> Resolution:
    """Total return of the stock (last close on or before each date, plus cash dividends with an ex-date in the
    window, not reinvested) against NIFTYBEES's price return over the same dates. A BSE-only stock ("BSE:<code>")
    reads its closes and corporate actions from BSE (`deps.bse_price_history` / `bse_corporate_actions`); the
    benchmark is NIFTYBEES on NSE either way."""
    from finresearch.adapters.bse_equity import scrip_code_of

    if getattr(deps, "price_history", None) is None:
        raise NotYet("no price-history source configured")
    inp = f.inputs or {}
    symbol, bench = inp.get("symbol") or f.instrument, inp.get("benchmark") or BENCHMARK
    start = date.fromisoformat(inp["start_date"]) if inp.get("start_date") else ist_day(f.created_at)
    end = f.resolve_on
    code = scrip_code_of(symbol)
    if code is not None:
        if getattr(deps, "bse_price_history", None) is None:
            raise NotYet("no BSE price-history source configured")
        stock_history, stock_id, ex = deps.bse_price_history, code, "BSE"
        stock_actions = getattr(deps, "bse_corporate_actions", None)
    else:
        stock_history, stock_id, ex = deps.price_history, symbol, "NSE"
        stock_actions = getattr(deps, "corporate_actions", None)

    async def closes(history, sym: str, label: str, exchange: str):
        a = _close_on_or_before(await history(sym, start - timedelta(days=10), start), start)
        b = _close_on_or_before(await history(sym, end - timedelta(days=10), end), end)
        if a is None or b is None:
            raise NotYet(f"no {exchange} close for {label} near {start if a is None else end}")
        return a, b

    s0, s1 = await closes(stock_history, stock_id, symbol, ex)
    b0, b1 = await closes(deps.price_history, bench, bench, "NSE")
    bench_actions = getattr(deps, "corporate_actions", None)
    if bench_actions is not None:
        # the benchmark's closes are unadjusted too: a unit split of NIFTYBEES would read as a 90 % fall
        for ca in await bench_actions(bench):
            if (
                ca.ex_date is not None
                and b0.day < ca.ex_date <= b1.day
                and any(w in ca.subject.lower() for w in VOIDING_ACTIONS)
            ):
                return Resolution(None, None, f"void: {bench} {ca.subject.strip()} (ex {ca.ex_date}) changes its unit "
                                  "count, and NSE closes are not adjusted")  # fmt: skip
    divs = Decimal(0)
    if stock_actions is not None:
        for ca in await stock_actions(stock_id):
            if ca.ex_date is None or not (s0.day < ca.ex_date <= s1.day):
                continue
            if any(w in ca.subject.lower() for w in VOIDING_ACTIONS):
                return Resolution(None, None, f"void: {ca.subject.strip()} (ex {ca.ex_date}) changes the share "
                                  f"count, and {ex} closes are not adjusted")  # fmt: skip
            divs += ca.dividend_per_share or 0
    else:
        return Resolution(
            None, None, "void: corporate actions unavailable, so splits and dividends cannot be checked"
        )
    stock = (s1.close + divs) / s0.close - 1
    index = b1.close / b0.close - 1
    excess = (stock - index) * 100
    return Resolution(1 if stock > index else 0, float(excess),
                      f"{symbol} {stock * 100:+.2f}% (₹{s0.close} on {s0.day} → ₹{s1.close} on {s1.day}, dividends "
                      f"₹{divs}) vs {bench} {index * 100:+.2f}% (₹{b0.close} → ₹{b1.close}); excess {excess:+.2f} pp")  # fmt: skip


# ------------------------------------------------------------------------------------------------------ reading
def forecast_json(f: Forecast) -> dict[str, Any]:
    return {"id": f.id, "created_at": f.created_at.isoformat() if f.created_at else None, "asset": f.asset,
            "instrument": f.instrument, "name": f.name, "source": f.source, "run_id": f.run_id,
            "event_kind": f.event_kind, "event": f.event, "horizon": f.horizon, "resolve_on": f.resolve_on.isoformat(),
            "probability": f.probability,
            "interval": [f.interval_low, f.interval_high] if f.interval_low is not None else None,
            "action": f.action, "score": f.score, "method": f.method, "validation_status": f.validation_status,
            "status": f.status, "outcome": f.outcome, "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
            "resolution_value": f.resolution_value, "resolution_note": f.resolution_note,
            "last_checked_at": f.last_checked_at.isoformat() if f.last_checked_at else None,
            "inputs": f.inputs or {}, "call": _call_of(f)}  # fmt: skip


def _call_of(f: Forecast) -> dict[str, Any] | None:
    """How a logged stock-signal forecast is shown while signals.stock has no proven edge (#193): informational with
    its factor tilt. The stored action is the composite's and stays as logged; rows from before the switch (no
    inputs.call_status) are shown the same way, since the same rule never had an edge either."""
    from finresearch.signals import stock

    if f.asset == "stock" and (f.source or "").startswith("run:"):
        # a stock research report's verdict (#241): its research view, informational under the same policy
        view = stock.report_view(f.action)
        if view is None:
            return None
        return {"status": "informational", "label": view["label"], "composite_action": f.action,
                "tilt": f"research view: {view['view']}"}  # fmt: skip
    if f.source != "signal:stock":
        return None

    status = (f.inputs or {}).get("call_status")
    if stock.CALLS_ENABLED and status != "informational":
        return None
    return {
        "status": "informational",
        "label": stock.INFORMATIONAL_LABEL,
        "composite_action": f.action,
        "tilt": stock.tilt(f.score) if f.score is not None else None,
    }


_LEGACY_IPO_BAND_METHOD = "empirical base rate by final QIB band × regime ("


def _method_group(method: str) -> str:
    """The calibration group of a method string. IPO base-rate forecasts logged before audit #137 carry their QIB band
    in the method ("... (50–100x, post-Apr-2022)"); they belong to the one base-rate method."""
    if method.startswith(_LEGACY_IPO_BAND_METHOD):
        from finresearch.signals.ipo import BASE_RATE_METHOD

        return BASE_RATE_METHOD
    return method


# events that happen once per instrument (an IPO lists once): every forecast of it is a forecast of the same outcome
POINT_EVENTS = frozenset({"listing_gain"})


def _window(f: Forecast) -> tuple[date, date]:
    start = (f.inputs or {}).get("start_date")
    return (date.fromisoformat(start) if start else ist_day(f.created_at)), f.resolve_on


# nested events of one direction path, in the order a cluster keeps them (#353): P(up) is the primary event
CLUSTER_EVENT_ORDER = {"up": 0, "up_k": 1, "down_k": 2}


def _cluster(f: Forecast) -> tuple[str, str, str]:
    """The event cluster of a forecast (#353). Direction forecasts (inputs carry `h`) of one underlying and horizon
    from one start date are nested events of ONE price path (P(up), P(up_k), P(down_k)): they share a cluster, so
    the window pass below keeps one of them. F&O card forecasts (#376) cluster by underlying, expiry and structure kind
(`independent_events` keeps the first card). Every other forecast clusters by its event kind as before."""
    h = (f.inputs or {}).get("h")
    if h is not None and f.asset == "fno":
        return f.asset, f.instrument, f"h{h}"
    inp = f.inputs or {}
    if f.asset == "fno" and "card_log_id" in inp:  # #376: one event per underlying, expiry and structure
        return f.asset, f"{f.instrument} {inp.get('expiry')}", f"card:{inp.get('structure')}"
    return f.asset, f.instrument, f.event_kind


def cutoff_of(close_day: date) -> datetime:
    """17:00 IST on the issue's last bidding day (`IPO_CUTOFF`)."""
    return datetime.combine(close_day, IPO_CUTOFF, IST)


def ipo_close_dates(session: Session, instruments: set[str]) -> dict[str, date]:
    """The last bidding day per IPO instrument (NSE symbol or "BSE:<ipo no>"), from the watch, the IPO offer or the
    issue registry; an instrument with none is absent (unknown, never guessed)."""
    from finresearch.db.models import IpoIssue

    out: dict[str, date] = {}
    if not instruments:
        return out
    nse = {i for i in instruments if not i.startswith("BSE:")}
    for sym, d in session.execute(select(IpoIssue.symbol, IpoIssue.close_date).where(
            IpoIssue.exchange == "NSE", IpoIssue.symbol.in_(nse), IpoIssue.close_date.isnot(None))):  # fmt: skip
        out[sym] = d
    bse = {i.removeprefix("BSE:"): i for i in instruments if i.startswith("BSE:")}
    for key, d in session.execute(select(IpoIssue.issue_key, IpoIssue.close_date).where(
            IpoIssue.exchange == "BSE", IpoIssue.issue_key.in_(set(bse)), IpoIssue.close_date.isnot(None))):  # fmt: skip
        out[bse[key]] = d
    for sym, d in session.execute(select(IpoOffer.nse_symbol, IpoOffer.close_date).where(
            IpoOffer.nse_symbol.in_(nse), IpoOffer.close_date.isnot(None))):  # fmt: skip
        out[sym] = d
    for sym, d in session.execute(select(Watch.nse_symbol, Watch.close_date).where(
            Watch.kind == "ipo", Watch.nse_symbol.in_(nse), Watch.close_date.isnot(None))):  # fmt: skip
        out[sym] = d  # the watch is the date the app tracked bidding by: it wins
    return out


def independent_events(rows: list[Forecast], close_dates: dict[str, date] | None = None) -> list[Forecast]:
    """One scored forecast per INDEPENDENT outcome, so n, the Brier score, the bins and every Wilson interval count
    events, not ledger rows. The ledger keeps one row per instrument per IST day, so an IPO viewed on five bidding
    days is five rows about one listing, and a stock signal viewed daily logs ~250 overlapping 12-month windows a year.

    * point events (`POINT_EVENTS`: the IPO listing-day open): with `close_dates` (#353, the scored rule), the LAST
      forecast made before 17:00 IST on the issue's last bidding day (`IPO_CUTOFF`): the one a bidder could act on.
      An issue whose last bidding day is unknown, or with no forecast before the cut-off, is not scored (never the
      latest by default). Without `close_dates`: the latest resolved forecast (the pre-#353 rule, kept for the
      comparison in `ipo_rule_comparison`).
    * window events (the stock's 12-month excess return, the F&O direction paths): non-overlapping sampling per
      cluster (`_cluster`: one per underlying, horizon and start-date window, so nested direction events count
      once, keeping P(up) before P(up_k) before P(down_k)). Forecasts are taken in start-date order, keeping one only
      when it starts on or after the previous kept one's resolution date.
      Overlapping windows share most of their return path, so their outcomes are serially correlated and counting
      each as a separate trial overstates n and narrows every interval (Hansen & Hodrick 1980, J. Political Economy
      88(5) on overlapping observations; Harri & Brorsen 2009, "The Overlapping Data Problem", Quant. Qual. Anal.
      Soc. Sci. 3(3), which finds non-overlapping samples a valid if less efficient choice). Non-overlapping sampling
      is used instead of an n / overlap correction because the overlap is not fixed: views are irregular (daily,
      monthly, never), so no single divisor is right.
    * F&O card forecasts (#376, evals/experiments/card_ledger/PREREG.md): the FIRST card logged per underlying, expiry
      and structure kind, so the owner's repeated looks at one trade count once.
    Stocks resolving over the same months still move together (one market), so even the event count overstates
    the independent evidence across stocks; that limit is stated, not corrected."""
    by_key: dict[tuple[str, str, str], list[Forecast]] = {}
    for f in rows:
        by_key.setdefault(_cluster(f), []).append(f)
    out: list[Forecast] = []
    for (_a, inst, kind), fs in by_key.items():
        if kind.startswith("card:"):  # #376, pre-registered: the first card logged in the cluster
            out.append(min(fs, key=lambda f: (f.created_at, (f.inputs or {}).get("card_index", 0), f.id)))
            continue
        if kind in POINT_EVENTS:
            if close_dates is None:
                out.append(max(fs, key=lambda f: (f.created_at, f.id)))
                continue
            close = close_dates.get(inst)
            before = [f for f in fs if close is not None and f.created_at < cutoff_of(close)]
            if before:
                out.append(max(before, key=lambda f: (f.created_at, f.id)))
            continue
        last_end: date | None = None
        order = CLUSTER_EVENT_ORDER
        for f in sorted(
            fs, key=lambda f: (_window(f), order.get((f.inputs or {}).get("event"), 9), f.created_at, f.id)
        ):
            start, end = _window(f)
            if last_end is None or start >= last_end:
                out.append(f)
                last_end = end
    return sorted(out, key=lambda f: f.id)


def ipo_rule_comparison(
    rows: list[Forecast], close_dates: dict[str, date], n_bins: int = 5
) -> dict[str, Any]:
    """#353: the pre-registered decision-time rule (last forecast before the cut-off) beside the old one (latest
    before listing) on the same issues, so the two can be compared as live forecasts resolve."""
    from finresearch.evals.calibration import summarize

    known = [f for f in rows if f.instrument in close_dates]
    cut = independent_events(known, close_dates)
    issues = {f.instrument for f in cut}
    latest = independent_events([f for f in known if f.instrument in issues])

    def sm(fs: list[Forecast]) -> dict[str, Any]:
        j = summarize([f.probability for f in fs], [int(f.outcome) for f in fs], n_bins).to_json()
        return {"n": j["n"], "brier": j["brier"], "mean_p": j["mean_p"]}

    return {"cutoff": sm(cut), "latest": sm(latest),
            "unknown_close_date": len({f.instrument for f in rows} - set(close_dates)),
            "no_forecast_before_cutoff": len({f.instrument for f in known} - issues),
            "rule": "scored: the last forecast before 17:00 IST on the last bidding day; compared: the latest before "
                    "listing (on the same issues)"}  # fmt: skip


HEADLINE = "headline: the calls (benchmarks and shadow tests excluded)"


def is_benchmark(f: Forecast) -> bool:
    """A benchmark, not a call (#353): the option market's own probability (direction, logged as rule_based) or an
    empirical base rate. They are scored in their own rows, never in a headline: otherwise ~2,000 market-implied
    direction rows a year would make the track record measure the option market. The F&O card's risk-neutral PoP (#376)
    is the option market's probability too."""
    from finresearch.direction.live import METHOD_IMPLIED
    from finresearch.signals.ipo import BASE_RATE_METHOD

    from finresearch.fnobook.card_forecasts import RN_METHOD

    return f.validation_status == "base_rate" or _method_group(f.method) in (
        METHOD_IMPLIED,
        BASE_RATE_METHOD,
        RN_METHOD,
    )


def calibration_groups(session: Session, asset: str | None = None, n_bins: int = 5) -> list[dict[str, Any]]:
    """Calibration per (asset, method) over resolved forecasts with a probability, plus coverage counts (open, no-call,
    void) so the track record shows what was not scored as well as what was. Scores, n and intervals count
    independent events (`independent_events`: clusters, and an IPO's decision-time forecast); `scored_forecasts` is
    the number of ledger rows behind them. Headlines (#353) pool only the calls (no benchmark, `is_benchmark`, and
    no shadow test): one per asset (method `HEADLINE`), and the last group, asset "all", across assets (methods
    differ, so read the per-method rows before trusting it); there each event counts once, as the latest call on it.
    IPO groups also carry `ipo_rules`: the decision-time rule beside the old latest-forecast rule."""
    from finresearch.evals.calibration import summarize

    q = select(Forecast)
    if asset:
        q = q.where(Forecast.asset == asset)
    groups: dict[tuple[str, str], list[Forecast]] = {}
    for f in session.scalars(q.order_by(Forecast.id)):
        groups.setdefault((f.asset, _method_group(f.method)), []).append(f)
    out = []
    if not groups:
        return out
    closes = ipo_close_dates(
        session, {f.instrument for (a, _), rows in groups.items() if a == "ipo" for f in rows}
    )
    calls = [
        f
        for rows in list(groups.values())
        for f in rows
        if f.validation_status != "shadow" and not is_benchmark(f)
    ]
    for a in sorted({f.asset for f in calls}):
        groups[(a, HEADLINE)] = [f for f in calls if f.asset == a]
    groups[("all", "all methods")] = calls
    for (a, method), rows in sorted(groups.items(), key=lambda kv: (kv[0][0] == "all", kv[0])):
        rows_scored = [
            f for f in rows if f.status == "resolved" and f.probability is not None and f.outcome is not None
        ]
        scored = independent_events(rows_scored, closes)
        summary = summarize([f.probability for f in scored], [int(f.outcome) for f in scored], n_bins)
        g = {"asset": a, "method": method, "total": len(rows), "scored_forecasts": len(rows_scored),
             "open": sum(f.status == "open" for f in rows), "resolved": sum(f.status == "resolved" for f in rows),
             "void": sum(f.status == "void" for f in rows), "no_call": sum(f.probability is None for f in rows),
             "validation_status": "headline" if method in (HEADLINE, "all methods") else (rows[-1].validation_status
                                                                                           if rows else None),
             **summary.to_json()}  # fmt: skip
        if a == "ipo":
            g["ipo_rules"] = ipo_rule_comparison(rows_scored, closes, n_bins)
        out.append(g)
    return out
