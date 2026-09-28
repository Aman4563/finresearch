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

    @classmethod
    def live(cls) -> Deps:
        from finresearch.adapters.nse import NseClient

        async def ipo_detail(symbol: str):
            async with NseClient() as nse:
                return await nse.ipo_detail(symbol)

        async def quote(symbol: str):
            async with NseClient() as nse:
                return await nse.quote(symbol)

        return cls(ipo_detail=ipo_detail, quote=quote)


def alert(session: Session, watch: Watch, kind: str, message: str, level: str = "info", **data: Any) -> None:
    session.add(Alert(watch_id=watch.id, kind=kind, level=level, message=message, data=data))


def _fmt(x: Decimal | None) -> str:
    return "n/a" if x is None else f"{x:.2f}x"


async def subscription(session: Session, job: MonitorJob, watch: Watch, deps: Deps, now: datetime) -> dict:
    from finresearch.suggest.rules import subscription_metrics

    detail = await deps.ipo_detail(watch.nse_symbol)
    snap = detail.combined
    if snap is None or snap.total_times is None:
        raise NotYet("NSE has no combined subscription table yet")
    session.execute(insert(SubscriptionSnapshotRow).values(
        nse_symbol=watch.nse_symbol, as_of=snap.as_of or now, source=snap.source, total_times=snap.total_times,
        categories=[c.model_dump(mode="json") for c in snap.categories], raw={},
    ).on_conflict_do_nothing(index_elements=["nse_symbol", "as_of", "source"]))  # fmt: skip
    m = subscription_metrics(detail)
    result = {k: str(v.value) if v.value is not None else None for k, v in m.items()}
    result["as_of"] = snap.as_of.isoformat() if snap.as_of else None
    changes = _reevaluate_rules(session, watch, detail, now)
    if changes:
        alert(session, watch, "rule_change", f"{watch.nse_symbol}: " + "; ".join(changes), "action",
              metrics=result)  # fmt: skip
    if job.params.get("final"):
        parts = [f"{label} {_fmt(m[k].value)}" for k, label in (("total_times", "total"), ("qib_times", "QIB"),
                                                                ("nii_times", "NII"), ("rii_times", "retail")) if k in m]  # fmt: skip
        alert(session, watch, "subscription_final", f"{watch.nse_symbol} closed: " + ", ".join(parts)
              + f" (NSE combined, {result['as_of']})", metrics=result)  # fmt: skip
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
    from finresearch.fincalc.ipo import listing_gain
    from finresearch.suggest.advisor import record_outcome

    try:
        q = await deps.quote(watch.nse_symbol)
    except Exception as e:
        raise NotYet(f"no NSE quote for {watch.nse_symbol} yet: {e}") from e
    if q.listing_date is None or q.listing_date > now.date() or q.open is None:
        raise NotYet(f"{watch.nse_symbol} has not listed yet")
    meta = dict(watch.meta or {})
    if q.listing_date != watch.listing_date:
        meta["expected_listing_date"] = watch.listing_date.isoformat()
        watch.listing_date = q.listing_date
    meta["listing_confirmed"] = True
    which = job.params.get("which", "open")
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
          f"{shares} may become tradable ({p['basis']}). Expect supply pressure around this date.", "warn")  # fmt: skip
    return {"alerted": True}


HANDLERS = {"subscription": subscription, "allotment": allotment, "listing": listing, "lockin": lockin}
