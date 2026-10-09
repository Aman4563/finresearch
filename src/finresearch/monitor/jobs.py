"""What each scheduled check does. Every handler records its result and raises alerts; network failures raise so
the scheduler can retry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, Decision, MonitorJob, ResearchRun, SubscriptionSnapshotRow, Watch
from finresearch.fincalc.numbers import format_inr


class NotYet(RuntimeError):
    """The information is not published yet (for example the stock has not listed); retry later."""


class CloseNotPublished(RuntimeError):
    """The stock traded today but the exchange has not published its official close yet: retry within the day
    (the scheduler's usual network-error delay), not on the next trading day as for NotYet."""


@dataclass
class Deps:
    """Network access for the handlers; tests pass fakes."""

    ipo_detail: Any  # async (symbol) -> IpoDetail
    quote: Any  # async (symbol) -> Quote
    current_issues: Any = None  # async () -> list[IpoIssue]; overall times for SME issues
    stock_snapshot: Any = None  # async (symbol) -> dict: bars, announcements, actions, shareholding
    holidays: Any = None  # async (kind) -> NSE holiday-master payload (tests); None + live_holidays uses NSE
    live_holidays: bool = False
    bse_ipo_detail: Any = None  # async (ipo_no) -> IpoDetail from BSE, for a BSE SME watch (meta bse_ipo_no)
    bse_quote: Any = (
        None  # async (symbol) -> Quote | None from BSE (None before listing), for a BSE SME watch
    )
    fno: Any = (
        None  # () -> async context manager with NseFno's methods; set: record daily ATM IV (monitor.iv)
    )
    price_history: Any = (
        None  # async (symbol, start, end) -> list[PriceBar] (NSE daily history); resolves forecasts
    )
    corporate_actions: Any = None  # async (symbol) -> list[CorporateAction]; splits void, dividends count
    forecasts: bool = (
        False  # log finished runs' verdicts and resolve due forecasts once a day (signals.ledger)
    )
    archive_books: bool = False  # archive every open issue's book 4 times a bidding day (item 15); live only
    archive_spacing_s: float = (
        2.0  # pause between issues within one archive pass (on top of the rate limiter)
    )
    intraday: Any = None  # async (kind, symbol) -> IntradaySeries; set: archive each session after the close
    # BSE-only stocks (watch.exchange "BSE"; the argument is the BSE scrip code): the same shapes as the NSE ones
    bse_stock_snapshot: Any = (
        None  # async (code) -> dict: bars, announcements, actions, shareholding (from BSE)
    )
    bse_price_history: Any = (
        None  # async (code, start, end) -> list[PriceBar] (BSE daily); resolves BSE forecasts
    )
    bse_corporate_actions: Any = None  # async (code) -> list[CorporateAction] from BSE
    # the daily portfolio pass (monitor.portfolio_daily) and the morning brief / weekly digest (monitor.digest);
    # None = the live source (NSE via one QuoteBatch session, AMFI NAVAll and TER files, signal providers with log=0)
    portfolio_daily: bool = False
    pf_quote: Any = None  # async (symbol, exchange) -> Quote
    pf_scheme_rows: Any = None  # async () -> list[SchemeNav] (AMFI NAVAll)
    pf_listings: Any = (
        None  # async () -> Listings (NSE + BSE by ISIN); None = the stored ISIN map, else NSE + BSE
    )
    pf_signal: Any = None  # async (asset, instrument) -> Signal (never logged in the forecast ledger)
    pf_stock_events: Any = None  # async (symbol) -> {"actions", "board_meetings", "results"}
    pf_ter: Any = None  # async (month) -> {ter_key(name): SchemeTer}
    pf_spacing_s: float = 1.0  # pause between instruments in the signals and events steps
    # async (holdings: list[history.HoldingIn], today) -> history.History: the canonical value history (#239) the
    # daily pass stores for the alerts, brief, digest and net worth. None = not built by the pass (the Performance tab
    # still builds and stores it); the live app wires portfolio_daily.live_history
    pf_history: Any = None
    brief: bool = False  # build the 08:30 brief and the weekly digest (monitor.digest)
    archive: bool = (
        False  # the daily validation archive after the close (monitor.archive: sector P/E, IV term, G-sec)
    )
    disclosures: bool = (
        False  # the daily disclosure refresh (monitor.disclosures: surveillance, pledge, insiders, ratings)
    )
    fund_ranks: bool = False  # the daily fund category ranking (monitor.fund_ranks, signals.fund_rank)
    stock_peers: bool = False  # the nightly stock peer build (monitor.stock_peers, signals.stock_peers)
    lookthrough: bool = (
        False  # the monthly fund portfolio fetch for held funds (monitor.lookthrough_fetch, #214)
    )
    retention: bool = (
        False  # the weekly prune of the HTTP cache and old intraday series (monitor.retention, #247)
    )

    @classmethod
    def live(cls) -> Deps:
        from finresearch.adapters.nse import NseClient

        async def ipo_detail(symbol: str):
            async with NseClient() as nse:
                return await nse.ipo_detail(symbol)

        async def quote(symbol: str):
            async with NseClient() as nse:
                return await nse.quote(symbol)

        async def current_issues():
            async with NseClient() as nse:
                return await nse.current_issues()

        async def stock_snapshot(symbol: str):
            from datetime import timedelta

            from finresearch.adapters.nse_equity import NseEquity
            from finresearch.fincalc.dates import today_ist

            async with NseEquity() as eq:
                today = today_ist()
                return {"bars": await eq.history(symbol, today - timedelta(days=10), today),
                        "announcements": await eq.announcements(symbol),
                        "actions": await eq.corporate_actions(symbol), "shareholding": await eq.shareholding(symbol)}  # fmt: skip

        async def bse_stock_snapshot(code: str):
            from datetime import timedelta

            from finresearch.adapters.bse_equity import BseEquity
            from finresearch.fincalc.dates import today_ist

            async with BseEquity() as eq:  # BSE answers any date range in one request
                today = today_ist()
                return {"bars": await eq.history(code, today - timedelta(days=10), today),
                        "announcements": await eq.announcements(code),
                        "actions": await eq.corporate_actions(code), "shareholding": await eq.shareholding(code)}  # fmt: skip

        async def bse_price_history(code: str, start, end):
            from finresearch.adapters.bse_equity import BseEquity

            async with BseEquity() as eq:
                return await eq.history(code, start, end)

        async def bse_corporate_actions(code: str):
            from finresearch.adapters.bse_equity import BseEquity

            async with BseEquity() as eq:
                return await eq.corporate_actions(code)

        async def bse_ipo_detail(ipo_no: int):
            from finresearch.adapters.bse import BseClient

            async with BseClient() as bse:
                return await bse.ipo_detail(ipo_no)

        async def bse_quote(symbol: str):
            from finresearch.adapters.bse import BseClient

            async with BseClient() as bse:
                return await bse.quote(symbol)

        async def price_history(symbol: str, start, end):
            from finresearch.adapters.nse_equity import NseEquity

            async with NseEquity() as eq:
                return await eq.history(symbol, start, end)

        async def corporate_actions(symbol: str):
            from finresearch.adapters.nse_equity import NseEquity

            async with NseEquity() as eq:
                return await eq.corporate_actions(symbol)

        from finresearch.adapters.nse_fno import NseFno
        from finresearch.monitor.intraday import live_fetch
        from finresearch.monitor.portfolio_daily import live_history

        return cls(ipo_detail=ipo_detail, quote=quote, current_issues=current_issues, stock_snapshot=stock_snapshot,
                   bse_ipo_detail=bse_ipo_detail, bse_quote=bse_quote, fno=NseFno, price_history=price_history,
                   corporate_actions=corporate_actions, forecasts=True, archive_books=True,
                   intraday=live_fetch, bse_stock_snapshot=bse_stock_snapshot, bse_price_history=bse_price_history,
                   bse_corporate_actions=bse_corporate_actions, portfolio_daily=True, brief=True, archive=True,
                   live_holidays=True, disclosures=True, fund_ranks=True, stock_peers=True,
                   lookthrough=True, pf_history=live_history, retention=True)  # fmt: skip


def alert(session: Session, watch: Watch, kind: str, message: str, level: str = "info", **data: Any) -> None:
    session.add(Alert(watch_id=watch.id, kind=kind, level=level, message=message, data=data))


def _fmt(x: Decimal | None) -> str:
    return "n/a" if x is None else f"{x:.2f}x"


async def fetch_book(session: Session, watch: Watch, deps: Deps, now: datetime, *, record: bool = True):
    """The watch's current subscription book from the exchange, recorded as a snapshot (once per exchange timestamp)
    when `record`. Returns (detail, snapshot, total, source). Used by the scheduled checks (recorded) and the live
    view (GET /api/watches/{id}/live: never recorded, #247)."""
    bse_ipo_no = (watch.meta or {}).get("bse_ipo_no")  # a BSE SME issue: BSE publishes the whole book
    detail = await (deps.bse_ipo_detail(bse_ipo_no) if bse_ipo_no else deps.ipo_detail(watch.nse_symbol))
    snap = detail.combined
    if snap is None:
        raise NotYet("the exchange has no subscription table yet")
    total, source = snap.total_times, snap.source
    # NSE SME tables publish no offered shares, so no category times; BSE's SME table has both
    if total is None and deps.current_issues and not bse_ipo_no:
        row = next((i for i in await deps.current_issues() if i.symbol == watch.nse_symbol), None)
        total, source = (row.times_subscribed, "nse_current_issues") if row else (None, source)
    if total is None:
        raise NotYet("the exchange has not published a subscription total yet")
    if not record:
        return detail, snap, total, source
    session.execute(insert(SubscriptionSnapshotRow).values(
        nse_symbol=watch.nse_symbol, as_of=snap.as_of or now, source=source, total_times=total,
        categories=[c.model_dump(mode="json") for c in snap.categories], raw={},
    ).on_conflict_do_nothing(index_elements=["nse_symbol", "as_of", "source"]))  # fmt: skip
    return detail, snap, total, source


async def subscription(session: Session, job: MonitorJob, watch: Watch, deps: Deps, now: datetime) -> dict:
    from finresearch.suggest.rules import subscription_metrics

    detail, snap, total, source = await fetch_book(session, watch, deps, now)
    m = subscription_metrics(detail)
    if m.get("total_times") is None or m["total_times"].value is None:
        from finresearch.suggest.rules import Metric

        m["total_times"] = Metric(
            total, "NSE current issues (overall)", snap.as_of.isoformat() if snap.as_of else None
        )
    result = {k: str(v.value) if v.value is not None else None for k, v in m.items()}
    result["as_of"] = snap.as_of.isoformat() if snap.as_of else None
    changes = _reevaluate_rules(session, watch, detail, now)
    if changes:
        alert(session, watch, "rule_change", f"{watch.nse_symbol}: " + "; ".join(changes), "action",
              metrics=result)  # fmt: skip
    if job.params.get("final"):
        parts = [f"{label} {_fmt(m[k].value)}" for k, label in (("total_times", "total"), ("qib_times", "QIB"),
                 ("nii_times", "NII"), ("rii_times", "retail")) if k in m and m[k].value is not None]  # fmt: skip
        label = {"nse_combined": "NSE combined", "bse_sme": "BSE SME book"}.get(
            source, "NSE current issues; SME category times unpublished"
        )
        alert(session, watch, "subscription_final", f"{watch.nse_symbol} closed: " + ", ".join(parts)
              + f" ({label}, {result['as_of']})", metrics=result)  # fmt: skip
    return {**result, "rule_changes": changes}


def _reevaluate_rules(session: Session, watch: Watch, detail, now: datetime) -> list[str]:
    """Re-check the investor's rules on fresh data; report every rule whose status changed since the last check."""
    from finresearch.suggest.advisor import load_profile
    from finresearch.suggest.rules import gather

    d = session.scalars(select(Decision).join(ResearchRun, ResearchRun.id == Decision.run_id)
                        .where(ResearchRun.company_id == watch.company_id).order_by(Decision.id.desc())).first()  # fmt: skip
    if d is None:
        return []
    profile = load_profile(session)
    inputs = gather(session, d.run_id, profile, live_detail=detail, now=now, gate_ok=None)
    previous = dict((watch.meta or {}).get("rule_status") or
                    {r["rule"]["id"]: r["status"] for r in (d.inputs or {}).get("rules", [])})  # fmt: skip
    current, changes = {}, []
    for r in inputs.rules:
        if r.rule.metric == "gate_ok":
            continue  # the gate is part of the report, not live data
        current[r.rule.id] = r.status
        before = previous.get(r.rule.id)
        if before is not None and before != r.status:
            value = f"{r.value:.2f}" if r.value is not None else "unknown"
            changes.append(f"rule {r.rule.id} is now {r.status} ({r.rule.metric} = {value}; was {before})")
    watch.meta = {**(watch.meta or {}), "rule_status": {**previous, **current}}
    return changes


async def allotment(session: Session, job: MonitorJob, watch: Watch, deps: Deps, now: datetime) -> dict:
    alert(session, watch, "allotment", f"{watch.nse_symbol}: the basis of allotment is expected today "
          f"({watch.allotment_date}). Check your status with the registrar and record allotted lots in the journal.",
          "action")  # fmt: skip
    return {"reminded": True}


async def listing(session: Session, job: MonitorJob, watch: Watch, deps: Deps, now: datetime) -> dict:
    from finresearch.fincalc.dates import to_ist
    from finresearch.fincalc.ipo import listing_gain
    from finresearch.suggest.advisor import record_outcome

    which = job.params.get("which", "open")
    if (watch.meta or {}).get(f"listing_{which}") is not None:  # recorded by an earlier slot for this event
        return {"which": which, "skipped": "already recorded"}
    bse = bool((watch.meta or {}).get("bse_ipo_no"))  # a BSE SME issue lists on BSE only
    exchange = "BSE" if bse else "NSE"
    from finresearch.adapters.http import is_transient

    try:
        q = await (deps.bse_quote(watch.nse_symbol) if bse else deps.quote(watch.nse_symbol))
    except Exception as e:
        # a timeout or a 403 on listing day: retried in minutes (RETRY_DELAY), not on the next trading day
        if is_transient(e):
            raise
        raise NotYet(f"no {exchange} quote for {watch.nse_symbol} yet: {e}") from e
    # an open of 0 is NSE's "no trade yet" placeholder (e.g. a listing special session that has not matched)
    if q is None or q.listing_date is None or q.listing_date > to_ist(now).date() or not q.open:
        raise NotYet(f"{watch.nse_symbol} has not listed on {exchange} yet")
    # A quote describes its own session: a check that runs after the listing day (a retry, a late confirmation) would
    # read a later day's open and close. Then the listing day's bar from the exchange's price history is used.
    # The day is the later of NSE's listing date and the watch's: a demerged or relisted company's quote can carry its
    # original listing date (TMCV), while a listing is never earlier than the date the watch expected.
    day = max(q.listing_date, watch.listing_date or q.listing_date)
    later = q.as_of is not None and to_ist(q.as_of).date() > day
    bar = await _listing_bar(deps, watch.nse_symbol, day, bse) if later else None
    meta = dict(watch.meta or {})
    if q.listing_date != watch.listing_date:
        meta["expected_listing_date"] = watch.listing_date.isoformat()
        watch.listing_date = q.listing_date
    meta["listing_confirmed"] = True
    if bar is not None:
        price = bar.close if which == "close" else bar.open
    elif which == "close":
        from finresearch.fincalc.price import OFFICIAL_CLOSE, price_view

        v = price_view(q, exchange=exchange, now=now)
        if v.kind != OFFICIAL_CLOSE:
            # an in-session or not-yet-published close must never be recorded as the listing-day close
            raise CloseNotPublished(
                f"{exchange} has not published {watch.nse_symbol}'s official close yet ({v.label.lower()})"
            )
        price = v.price
    else:
        price = q.open
    meta[f"listing_{which}"] = str(price)
    watch.meta = meta
    upper = _upper_band(session, watch)
    gain = listing_gain(upper, price) * 100 if upper and price else None
    updated = []
    if which == "open":
        for d in session.scalars(select(Decision).join(ResearchRun, ResearchRun.id == Decision.run_id)
                                 .where(ResearchRun.company_id == watch.company_id)):  # fmt: skip
            if d.listing_price is None:
                d.listing_price = price
                d.issue_price = d.issue_price or upper
                d.outcome = record_outcome(d)
                updated.append(d.id)
    # rupees to the paisa with Indian grouping: NSE prints prices with six decimals ("₹272.000000", #264)
    alert(session, watch, f"listing_{which}", f"{watch.nse_symbol} listed on {q.listing_date}: {which} "
          f"{format_inr(price, 'inr')}" + (f" ({gain:+.2f}% vs the {format_inr(upper, 'inr')} upper band)"
                                           if gain is not None else ""),
          "action" if which == "open" else "info", price=str(price), as_of=q.as_of.isoformat() if q.as_of else None)  # fmt: skip
    return {"price": str(price), "which": which, "listing_date": q.listing_date.isoformat(),
            "gain_pct": f"{gain:.2f}" if gain is not None else None, "decisions_updated": updated}  # fmt: skip


class ListingDayPassed(RuntimeError):
    """The listing day is over and its open/close could not be read from the exchange's price history: never record a
    later session's price as the listing price (retried, then failed with an alert)."""


async def _listing_bar(deps: Deps, symbol: str, day: date, bse: bool) -> Any:
    """The listing day's daily bar (open and official close), or ListingDayPassed."""
    # BSE's history is keyed by scrip code; a BSE SME watch carries the issue's symbol, so it has no history to read
    fetch = getattr(deps, "bse_price_history", None) if bse else getattr(deps, "price_history", None)
    if bse and not symbol.isdigit():
        fetch = None
    if fetch is None:
        raise ListingDayPassed(
            f"{symbol}'s quote is from after its listing day {day} and no price history is set"
        )
    bars = await fetch(symbol, day, day)
    bar = next((b for b in bars or [] if b.day == day and b.open and b.close), None)
    if bar is None:
        raise ListingDayPassed(f"no {day} bar for {symbol} in the exchange's price history yet")
    return bar


def _upper_band(session: Session, watch: Watch) -> Decimal | None:
    from finresearch.suggest.rules import _ledger_fact, _upper_from

    run = session.scalars(select(ResearchRun).where(ResearchRun.company_id == watch.company_id,
                                                    ResearchRun.kind == "ipo_report").order_by(ResearchRun.id.desc())).first()  # fmt: skip
    if run is None:
        return None
    return _ledger_fact(session, run.id, "price_band_upper") or _upper_from(
        ((run.manifest or {}).get("facts") or {}).get("issue_info") or {}
    )


async def lockin(session: Session, job: MonitorJob, watch: Watch, deps: Deps, now: datetime) -> dict:
    p = job.params
    shares = f"{int(Decimal(p['shares'])):,} shares" if p.get("shares") else "shares"
    alert(session, watch, "lockin", f"{watch.nse_symbol}: the {p['holder']} lock-in ends on {p['unlock_date']}; "
          f"{shares} may become tradable ({p['basis']}). Expect supply pressure around this date."
          + (f" {p['note']}" if p.get("note") else ""), "warn")  # fmt: skip
    return {"alerted": True}


BIG_MOVE = Decimal("0.05")  # a daily move of 5% or more raises an alert
EX_DATE_SOON_DAYS = 7


async def stock_daily(session: Session, job: MonitorJob, watch: Watch, deps: Deps, now: datetime) -> dict:
    """After-close check of a watched stock: new results filings, corporate actions and ex-dates, promoter-holding
    changes and large price moves. The first check records what exists without alerting on history, except an
    ex-date coming up within EX_DATE_SOON_DAYS (still actionable).

    An NSE watch reads NSE; a BSE-only stock's watch (exchange "BSE") reads the same things from BSE (daily bars,
    announcements, corporate actions, shareholding via adapters/bse_equity.py) and its alerts say "BSE <code>"."""
    from datetime import timedelta

    from finresearch.fincalc.dates import to_ist
    from finresearch.fincalc.market import price_return

    bse = watch.exchange == "BSE"
    if bse:
        if deps.bse_stock_snapshot is None:
            raise RuntimeError("no BSE data source configured for a BSE watch")
        snap = await deps.bse_stock_snapshot(watch.bse_code)
    else:
        snap = await deps.stock_snapshot(watch.nse_symbol)
    meta = dict(watch.meta or {})
    first = not meta.get("stock_initialised")
    sym, today, out = watch.label, to_ist(now).date(), {"alerts": []}
    if bse:
        out["exchange"] = "BSE"

    def say(kind: str, message: str, level: str = "info", always: bool = False, **data):
        if always or not first:
            alert(session, watch, kind, message, level, **({"exchange": "BSE"} if bse else {}), **data)
            out["alerts"].append(kind)

    seen_results = set(meta.get("seen_results", []))
    for a in snap["announcements"]:
        if a.results_period_end and a.results_period_end.isoformat() not in seen_results:
            seen_results.add(a.results_period_end.isoformat())
            say("results", f"{sym} filed financial results for the period ended {a.results_period_end}", "action",
                url=a.attachment)  # fmt: skip
    seen_actions, soon = set(meta.get("seen_actions", [])), set(meta.get("ex_soon_alerted", []))
    for ca in snap["actions"]:
        key = f"{ca.ex_date}|{ca.subject}"
        if key not in seen_actions:
            seen_actions.add(key)
            say(
                "corporate_action",
                f"{sym}: {ca.subject} (ex-date {ca.ex_date}, record date {ca.record_date})",
            )
        if (
            ca.ex_date
            and today <= ca.ex_date <= today + timedelta(days=EX_DATE_SOON_DAYS)
            and key not in soon
        ):
            soon.add(key)
            say("ex_date_soon", f"{sym}: {ca.subject} goes ex on {ca.ex_date}; buy before then to be entitled",
                "action", always=True)  # fmt: skip
    latest = next((sh for sh in snap["shareholding"] if sh.promoter_pct is not None), None)
    if latest:
        prev = meta.get("promoter_pct")
        if prev is not None and Decimal(prev) != latest.promoter_pct:
            delta = latest.promoter_pct - Decimal(prev)
            say("holding_change", f"{sym}: promoter holding {prev}% -> {latest.promoter_pct}% ({delta:+.2f} pp, "
                f"as of {latest.as_of})", "warn" if abs(delta) >= 1 else "info")  # fmt: skip
        meta["promoter_pct"] = str(latest.promoter_pct)
    bars = [b for b in snap["bars"] if b.close]
    if len(bars) >= 2:
        move = price_return(bars[-2].close, bars[-1].close)
        out["last_close"], out["day_move"] = str(bars[-1].close), f"{move:.4f}"
        if abs(move) >= BIG_MOVE and meta.get("big_move_alerted") != bars[-1].day.isoformat():
            meta["big_move_alerted"] = bars[-1].day.isoformat()
            say("big_move", f"{sym} closed at {format_inr(bars[-1].close, 'inr')} on {bars[-1].day}, {move * 100:+.2f}% on the day",
                "warn")  # fmt: skip
    meta.update(stock_initialised=True, seen_results=sorted(seen_results), seen_actions=sorted(seen_actions),
                ex_soon_alerted=sorted(soon))  # fmt: skip
    watch.meta = meta
    out["first_check"] = first
    return out


# --------------------------------------------------------------------------- item 15: archive every open book

# IST times of the archive passes on each bidding day; the last follows the 17:00 close (the final book)
ARCHIVE_TIMES = ((11, 0), (13, 0), (15, 0), (17, 15))
ARCHIVE_WINDOW_MIN = 45  # a pass still runs up to this long after its time (monitor restarts, slow ticks)
ARCHIVE_RETRY_S = 300  # a pass NSE refused is retried within the window, at most every 5 minutes
_archive_retry_at: dict[str, float] = {}  # slot -> earliest time.time() of its next attempt


def archive_slot(now: datetime, holidays: set | None = None) -> str | None:
    """The archive slot due at `now` ("2026-09-30T13:00"), or None outside the windows and on non-trading days."""
    from datetime import timedelta

    from finresearch.fincalc.dates import is_business_day, ist_datetime, to_ist

    ist = to_ist(now)
    day = ist.date()
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    if not is_business_day(day, holidays):
        return None
    for h, m in ARCHIVE_TIMES:
        start = ist_datetime(day, h, m)
        if start <= ist < start + timedelta(minutes=ARCHIVE_WINDOW_MIN):
            return f"{day.isoformat()}T{h:02d}:{m:02d}"
    return None


async def archive_open_books(deps: Deps, now: datetime, *, holidays: set | None = None) -> dict | None:
    """Snapshot the category book of EVERY open NSE issue (not only watched ones), once per archive slot.

    This builds the intraday history the IPO model lacks (roadmap §D.1: what was knowable before the 5 pm UPI
    cut-off). The slot row is claimed first (unique key), so two monitor processes never archive the same slot;
    snapshots are de-duplicated on (symbol, NSE timestamp, source). Requests go through the polite NSE client with
    `archive_spacing_s` between issues. BSE-only SME issues are not covered."""
    import asyncio

    from sqlalchemy import delete

    from finresearch.db import session_scope
    from finresearch.db.models import SubscriptionArchiveSlot
    from finresearch.fincalc.dates import to_ist

    if not deps.archive_books or deps.current_issues is None:
        return None
    import time

    slot = archive_slot(now, holidays)
    if slot is None or _archive_retry_at.get(slot, 0.0) > time.time():
        return None

    def release() -> None:  # the next tick in the window (after ARCHIVE_RETRY_S) tries the slot again
        _archive_retry_at[slot] = time.time() + ARCHIVE_RETRY_S
        with session_scope() as s:
            s.execute(delete(SubscriptionArchiveSlot).where(SubscriptionArchiveSlot.slot == slot))

    with session_scope() as s:
        claimed = s.execute(insert(SubscriptionArchiveSlot).values(slot=slot, started_at=now, result={})
                            .on_conflict_do_nothing(index_elements=["slot"])
                            .returning(SubscriptionArchiveSlot.slot)).first()  # fmt: skip
    if claimed is None:
        return None
    try:
        issues = await deps.current_issues()
    except Exception:
        release()
        raise
    today = to_ist(now).date()
    live = [i for i in issues if i.symbol and (i.issue_start is None or i.issue_start <= today)
            and (i.issue_end is None or today <= i.issue_end)]  # fmt: skip
    result: dict[str, Any] = {"slot": slot, "open": len(live), "archived": [], "unchanged": [], "no_book": [],
                              "errors": []}  # fmt: skip
    for n, issue in enumerate(live):
        if n:
            await asyncio.sleep(deps.archive_spacing_s)
        try:
            detail = await deps.ipo_detail(issue.symbol)
        except Exception as e:
            result["errors"].append(f"{issue.symbol}: {type(e).__name__}: {e}"[:200])
            continue
        snap = detail.combined
        total = snap.total_times if snap else None
        source = snap.source if snap else None
        if snap is not None and total is None and issue.times_subscribed is not None:
            total, source = (
                issue.times_subscribed,
                "nse_current_issues",
            )  # NSE SME tables carry no category times
        if snap is None or total is None:
            result["no_book"].append(issue.symbol)
            continue
        with session_scope() as s:
            got = s.execute(insert(SubscriptionSnapshotRow).values(
                nse_symbol=issue.symbol, as_of=snap.as_of or now, source=source, total_times=total,
                categories=[c.model_dump(mode="json") for c in snap.categories],
                raw={"archive_slot": slot, "series": issue.series},
            ).on_conflict_do_nothing(index_elements=["nse_symbol", "as_of", "source"])
             .returning(SubscriptionSnapshotRow.id)).first()  # fmt: skip
        result["archived" if got else "unchanged"].append(issue.symbol)
    if live and len(result["errors"]) == len(live):
        # NSE refused every open issue (a 403 wave, an outage): nothing was archived, so the slot is not done
        release()
        return result
    with session_scope() as s:
        row = s.get(SubscriptionArchiveSlot, slot)
        if row is not None:
            row.result = result
    return result


HANDLERS = {"subscription": subscription, "allotment": allotment, "listing": listing, "lockin": lockin,
            "stock_daily": stock_daily}  # fmt: skip
