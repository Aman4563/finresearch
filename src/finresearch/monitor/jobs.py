"""What each scheduled check does. Every handler records its result and raises alerts; network failures raise so
the scheduler can retry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, Decision, MonitorJob, ResearchRun, SubscriptionSnapshotRow, Watch


class NotYet(RuntimeError):
    """The information is not published yet (for example the stock has not listed); retry later."""


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

        async def bse_ipo_detail(ipo_no: int):
            from finresearch.adapters.bse import BseClient

            async with BseClient() as bse:
                return await bse.ipo_detail(ipo_no)

        async def bse_quote(symbol: str):
            from finresearch.adapters.bse import BseClient

            async with BseClient() as bse:
                return await bse.quote(symbol)

        from finresearch.adapters.nse_fno import NseFno

        return cls(ipo_detail=ipo_detail, quote=quote, current_issues=current_issues, stock_snapshot=stock_snapshot,
                   bse_ipo_detail=bse_ipo_detail, bse_quote=bse_quote, fno=NseFno)  # fmt: skip


def alert(session: Session, watch: Watch, kind: str, message: str, level: str = "info", **data: Any) -> None:
    session.add(Alert(watch_id=watch.id, kind=kind, level=level, message=message, data=data))


def _fmt(x: Decimal | None) -> str:
    return "n/a" if x is None else f"{x:.2f}x"


async def fetch_book(session: Session, watch: Watch, deps: Deps, now: datetime):
    """The watch's current subscription book from the exchange, recorded as a snapshot (once per exchange timestamp).
    Returns (detail, snapshot, total, source). Used by the scheduled checks and the live view."""
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
    try:
        q = await (deps.bse_quote(watch.nse_symbol) if bse else deps.quote(watch.nse_symbol))
    except Exception as e:
        raise NotYet(f"no {exchange} quote for {watch.nse_symbol} yet: {e}") from e
    if q is None or q.listing_date is None or q.listing_date > to_ist(now).date() or q.open is None:
        raise NotYet(f"{watch.nse_symbol} has not listed on {exchange} yet")
    meta = dict(watch.meta or {})
    if q.listing_date != watch.listing_date:
        meta["expected_listing_date"] = watch.listing_date.isoformat()
        watch.listing_date = q.listing_date
    meta["listing_confirmed"] = True
    price = q.open if which == "open" else (q.close_price or q.last_price)
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
    alert(session, watch, f"listing_{which}", f"{watch.nse_symbol} listed on {q.listing_date}: {which} ₹{price}"
          + (f" ({gain:+.2f}% vs the ₹{upper} upper band)" if gain is not None else ""),
          "action" if which == "open" else "info", price=str(price), as_of=q.as_of.isoformat() if q.as_of else None)  # fmt: skip
    return {"price": str(price), "which": which, "listing_date": q.listing_date.isoformat(),
            "gain_pct": f"{gain:.2f}" if gain is not None else None, "decisions_updated": updated}  # fmt: skip


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
    ex-date coming up within EX_DATE_SOON_DAYS (still actionable)."""
    from datetime import timedelta

    from finresearch.fincalc.dates import to_ist
    from finresearch.fincalc.market import price_return

    snap = await deps.stock_snapshot(watch.nse_symbol)
    meta = dict(watch.meta or {})
    first = not meta.get("stock_initialised")
    sym, today, out = watch.nse_symbol, to_ist(now).date(), {"alerts": []}

    def say(kind: str, message: str, level: str = "info", always: bool = False, **data):
        if always or not first:
            alert(session, watch, kind, message, level, **data)
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
            say("big_move", f"{sym} closed at ₹{bars[-1].close} on {bars[-1].day}, {move * 100:+.2f}% on the day",
                "warn")  # fmt: skip
    meta.update(stock_initialised=True, seen_results=sorted(seen_results), seen_actions=sorted(seen_actions),
                ex_soon_alerted=sorted(soon))  # fmt: skip
    watch.meta = meta
    out["first_check"] = first
    return out


HANDLERS = {"subscription": subscription, "allotment": allotment, "listing": listing, "lockin": lockin,
            "stock_daily": stock_daily}  # fmt: skip
