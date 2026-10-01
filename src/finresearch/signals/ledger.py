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
  already known. The price-history resolver reads the first bar on or after the known listing date.
* Stock report (`verdict`), event "12-month total return (price + cash dividends, NSE closes) above the price
  return of NIFTYBEES, the Nifty 50 ETF": BUY, ACCUMULATE → map; REDUCE, AVOID → 1 − map; HOLD → no call.
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
STOCK_UP = ("BUY", "ACCUMULATE")
STOCK_DOWN = ("REDUCE", "AVOID")
CHECK_EVERY = timedelta(hours=6)
VOID_AFTER = timedelta(days=60)
READY_AFTER_IST = (16, 30)  # resolve after the close, when NSE has published the day's prices
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


def _key_for(session: Session, asset: str, instrument: str, event_kind: str, day: date, method: str) -> str:
    """The key for a new forecast: the legacy method-less key when a pre-#151 row of the SAME method holds it (so it
    keeps being updated, not duplicated), else the per-method key."""
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
                                        signal.method[:200])  # fmt: skip
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
                  dedupe_key=_key_for(session, asset, instrument, event_kind, day, RUN_METHOD))  # fmt: skip
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


def due(session: Session, now: datetime) -> list[Forecast]:
    ist = to_ist(now)
    last_day = ist.date() if (ist.hour, ist.minute) >= READY_AFTER_IST else ist.date() - timedelta(days=1)
    q = (select(Forecast).where(Forecast.status == "open", Forecast.resolve_on <= last_day)
         .where((Forecast.last_checked_at.is_(None)) | (Forecast.last_checked_at < now - CHECK_EVERY))
         .order_by(Forecast.resolve_on, Forecast.id))  # fmt: skip
    return list(session.scalars(q))


async def resolve_due(deps: Any, now: datetime | None = None) -> dict[str, int]:
    """Try every due forecast once; each in its own transaction so one failure does not undo the others."""
    from finresearch.db import session_scope

    now = now or datetime.now(UTC)
    with session_scope() as s:
        ids = [f.id for f in due(s, now)]
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


def _history_listing(session: Session, symbol: str | None) -> date | None:
    """The listing date the IPO harvest recorded for a mainboard symbol, if any (newest issue first)."""
    if not symbol or symbol.startswith("BSE:"):
        return None
    from finresearch.db.models import IpoHistory

    return session.scalar(select(IpoHistory.listing_date).where(IpoHistory.symbol == symbol,
                                                                IpoHistory.listing_date.is_not(None))
                          .order_by(IpoHistory.listing_date.desc()))  # fmt: skip


@resolver("ipo", "listing_gain")
async def resolve_listing_gain(f: Forecast, deps: Any, session: Session) -> Resolution:
    """Listing-day open vs the issue price: first from the monitor's own listing check (NSE or BSE quote on the
    listing day, stored on the watch), else from NSE's daily history (the first traded day after the forecast)."""
    from finresearch.fincalc.ipo import listing_gain

    inp = f.inputs or {}
    symbol = inp.get("symbol") or f.instrument
    watch = session.scalar(select(Watch).where(Watch.nse_symbol == symbol)) if symbol else None
    issue = _dec(inp.get("issue_price"))
    if issue is None and watch is not None:
        from finresearch.monitor.jobs import _upper_band

        issue = _upper_band(session, watch)
    listing = (watch.listing_date if watch is not None else None) or _history_listing(session, symbol)
    if listing is not None and to_ist(f.created_at) >= datetime.combine(
        listing, LISTING_OPEN_IST, tzinfo=IST
    ):
        # the listing-day open (pre-open call auction 09:00-09:08, trading from 09:15) was already known: scoring it
        # would put a hindsight "forecast" in the calibration
        return Resolution(
            None, None, f"void: forecast made after the listing-day open ({listing}, 09:00 IST)"
        )
    open_price, listed_on, basis = None, None, None
    if watch is not None and (watch.meta or {}).get("listing_open") is not None:
        open_price, listed_on = _dec(watch.meta["listing_open"]), watch.listing_date
        basis = "exchange quote on the listing day (monitor)"
    elif symbol and not symbol.startswith("BSE:") and getattr(deps, "price_history", None) is not None:
        # the first traded day on or after the known listing date (a stray earlier row is not the listing), else the
        # first on or after the forecast
        start = max(listing, ist_day(f.created_at)) if listing else ist_day(f.created_at)
        bars = [b for b in await deps.price_history(symbol, start, max(start, f.resolve_on) + timedelta(days=10))
                if b.open is not None and b.day >= start]  # fmt: skip
        if bars:
            open_price, listed_on, basis = bars[0].open, bars[0].day, "NSE daily history (first traded day)"
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
            "inputs": f.inputs or {}}  # fmt: skip


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


def independent_events(rows: list[Forecast]) -> list[Forecast]:
    """One scored forecast per INDEPENDENT outcome, so n, the Brier score, the bins and every Wilson interval count
    events, not ledger rows. The ledger keeps one row per instrument per IST day, so an IPO viewed on five bidding
    days is five rows about one listing, and a stock signal viewed daily logs ~250 overlapping 12-month windows a year.

    * point events (`POINT_EVENTS`: the IPO listing-day open): the LATEST resolved forecast per instrument, i.e. the
      last call before the information cutoff (forecasts made after the listing-day open are already voided).
    * window events (the stock's 12-month excess return): non-overlapping sampling per instrument. Forecasts are
      taken in start-date order, keeping one only when it starts on or after the previous kept one's resolution date.
      Overlapping windows share most of their return path, so their outcomes are serially correlated and counting
      each as a separate trial overstates n and narrows every interval (Hansen & Hodrick 1980, J. Political Economy
      88(5) on overlapping observations; Harri & Brorsen 2009, "The Overlapping Data Problem", Quant. Qual. Anal.
      Soc. Sci. 3(3), which finds non-overlapping samples a valid if less efficient choice). Non-overlapping sampling
      is used instead of an n / overlap correction because the overlap is not fixed: views are irregular (daily,
      monthly, never), so no single divisor is right.
    Stocks resolving over the same months still move together (one market), so even the event count overstates
    the independent evidence across stocks; that limit is stated, not corrected."""
    by_key: dict[tuple[str, str, str], list[Forecast]] = {}
    for f in rows:
        by_key.setdefault((f.asset, f.instrument, f.event_kind), []).append(f)
    out: list[Forecast] = []
    for (_a, _i, kind), fs in by_key.items():
        if kind in POINT_EVENTS:
            out.append(max(fs, key=lambda f: (f.created_at, f.id)))
            continue
        last_end: date | None = None
        for f in sorted(fs, key=lambda f: (_window(f), f.created_at, f.id)):
            start, end = _window(f)
            if last_end is None or start >= last_end:
                out.append(f)
                last_end = end
    return sorted(out, key=lambda f: f.id)


def calibration_groups(session: Session, asset: str | None = None, n_bins: int = 5) -> list[dict[str, Any]]:
    """Calibration per (asset, method) over resolved forecasts with a probability, plus coverage counts (open, no-call,
    void) so the track record shows what was not scored as well as what was. Scores, n and intervals count
    independent events (`independent_events`); `scored_forecasts` is the number of ledger rows behind them. The last
    group, asset "all", pools every method (a headline only: methods differ, so read the per-method rows before
    trusting it); there too each event counts once, as the latest non-shadow call on it."""
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
    # the headline pools the methods that made the calls; shadow methods (#151) are scored in their own rows only
    groups[("all", "all methods")] = [f for rows in list(groups.values()) for f in rows
                                      if f.validation_status != "shadow"]  # fmt: skip
    for (a, method), rows in sorted(groups.items(), key=lambda kv: (kv[0][0] == "all", kv[0])):
        rows_scored = [
            f for f in rows if f.status == "resolved" and f.probability is not None and f.outcome is not None
        ]
        scored = independent_events(rows_scored)
        summary = summarize([f.probability for f in scored], [int(f.outcome) for f in scored], n_bins)
        out.append({"asset": a, "method": method, "total": len(rows), "scored_forecasts": len(rows_scored),
                    "open": sum(f.status == "open" for f in rows),
                    "resolved": sum(f.status == "resolved" for f in rows),
                    "void": sum(f.status == "void" for f in rows),
                    "no_call": sum(f.probability is None for f in rows),
                    "validation_status": rows[-1].validation_status, **summary.to_json()})  # fmt: skip
    return out
