"""The morning brief (08:30 IST, trading days) and the weekly performance digest (Sunday), built from stored data.

Split by content, not frequency (research plan §1.18): the *daily* brief carries what may need a decision today —
dated events for your holdings and watches, rules that fired, signal changes, the tax calendar and data health.
*Performance* (value change split into market move and new money, contributors) is weekly by default: checking a
portfolio's P&L often raises perceived risk and loss aversion (Benartzi & Thaler 1995, "Myopic loss aversion", QJE
110(1) [U]). A daily performance line is opt-in (brief settings).

Everything is deterministic Python over the local database (no network, no model): templated text from stored
numbers. The wording follows the app's rules: "your rule fired", "your target says", "no action needed"; never
"you should". Each brief is kept as an in-app alert (kind "morning_brief" / "weekly_digest") and, when channels are
chosen, pushed through the existing ntfy / Telegram / macOS delivery (quiet hours hold it until they end).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, AlertEvalSlot, NotificationSetting, PortfolioSnapshot, Watch
from finresearch.signals.base import DISCLAIMER

BRIEF_KIND, DIGEST_KIND = "morning_brief", "weekly_digest"
BRIEF_AT, DIGEST_AT = time(8, 30), time(9, 0)
LATE_BY = timedelta(
    hours=2
)  # a brief not sent within this of its time is skipped (the next one is due tomorrow)
EVENT_DAYS = 7
PUSH_MAX = 900  # characters in a push message (Telegram allows 4096; phones show far less)
BEHAVIOUR_NOTE = ("Performance is weekly by default: checking P&L daily tends to raise loss aversion without "
                  "improving decisions (Benartzi & Thaler 1995).")  # fmt: skip


class BriefSettings(BaseModel):
    """Stored in notification_setting under "brief" (no secrets here; channels are configured on Profile)."""

    enabled: bool = True  # build the brief and the digest (in-app)
    channels: list[Literal["ntfy", "telegram", "macos"]] = Field(default_factory=list)  # also push them here
    daily_performance: bool = False  # opt-in: a one-line flows-adjusted change in the daily brief
    weekly_digest: bool = True


def load_settings(s: Session) -> BriefSettings:
    row = s.get(NotificationSetting, "brief")
    return BriefSettings.model_validate(row.value if row else {})


def save_settings(s: Session, body: dict[str, Any]) -> BriefSettings:
    from datetime import UTC

    cur = load_settings(s).model_dump(mode="json")
    new = BriefSettings.model_validate({**cur, **{k: v for k, v in body.items() if k in cur}})
    row = s.get(NotificationSetting, "brief")
    if row is None:
        s.add(NotificationSetting(key="brief", value=new.model_dump(mode="json")))
    else:
        row.value, row.updated_at = new.model_dump(mode="json"), datetime.now(UTC)
    s.flush()
    return new


def _inr(x: float | Decimal | None) -> str:
    from finresearch.fincalc.numbers import group_indian

    if x is None:
        return "n/a"
    d = Decimal(str(x)).quantize(Decimal("1"))
    return ("-" if d < 0 else "") + "₹" + group_indian(str(abs(d)))


# --------------------------------------------------------------------------- the pieces
def value_change(snaps: list[tuple[date, float, float]]) -> dict[str, Any] | None:
    """ΔV split over a window of (day, value, cumulative net invested) rows in date order:
    ΔV = V_end - V_start; new money = invested_end - invested_start; market move = ΔV - new money;
    time-weighted return = Π (V_t - (I_t - I_{t-1})) / V_{t-1} - 1 (flows at the end of the day, as metrics.drawdown).
    """
    rows = [r for r in snaps if r[1] > 0]
    if len(rows) < 2:
        return None
    (d0, v0, i0), (d1, v1, i1) = rows[0], rows[-1]
    idx = 1.0
    for (_, va, ia), (_, vb, ib) in pairwise(rows):
        idx *= (vb - (ib - ia)) / va
    dv, flows = v1 - v0, i1 - i0
    return {"from": d0.isoformat(), "to": d1.isoformat(), "start": round(v0, 2), "end": round(v1, 2),
            "change": round(dv, 2), "new_money": round(flows, 2), "market": round(dv - flows, 2),
            "twr_pct": round((idx - 1) * 100, 2), "days": len(rows)}  # fmt: skip


def contributors(hist: dict[str, dict[str, list]], names: dict[str, str], start: str, end: str,
                 n: int = 3) -> dict[str, list[dict[str, Any]]]:  # fmt: skip
    """Market move per holding between two valuation days: units at the start × price change (a holding whose units
    changed in between is valued on the start units, so buying more is not counted as a gain)."""
    a, b = hist.get(start) or {}, hist.get(end) or {}
    rows = []
    for hid, (p1, _u1) in b.items():
        if hid not in a or a[hid][0] in (None, 0) or p1 is None:
            continue
        p0, u0 = a[hid]
        rows.append({"name": names.get(hid, hid), "inr": round(float(u0 or 0) * (float(p1) - float(p0)), 2),
                     "return_pct": round((float(p1) / float(p0) - 1) * 100, 2)})  # fmt: skip
    rows.sort(key=lambda r: r["inr"])
    return {
        "top": [r for r in reversed(rows) if r["inr"] > 0][:n],
        "bottom": [r for r in rows if r["inr"] < 0][:n],
    }


def _snaps(s: Session, since: date) -> list[tuple[date, float, float]]:
    rows = s.scalars(select(PortfolioSnapshot).where(PortfolioSnapshot.day >= since, PortfolioSnapshot.complete.is_(True))
                     .order_by(PortfolioSnapshot.day)).all()  # fmt: skip
    return [(r.day, float(r.value), float(r.invested)) for r in rows]


def ipo_events(s: Session, today: date, days: int = EVENT_DAYS) -> list[dict[str, Any]]:
    end = today + timedelta(days=days)
    out = []
    for w in s.scalars(select(Watch).where(Watch.kind == "ipo", Watch.active.is_(True))):
        name = w.nse_symbol or w.label or f"watch {w.id}"
        for d, what in ((w.open_date, "opens for bidding"), (w.close_date, "closes (UPI mandate by 5 PM)"),
                        (w.allotment_date, "basis of allotment"), (w.listing_date, "lists")):  # fmt: skip
            if d and today <= d <= end:
                out.append(
                    {
                        "day": d.isoformat(),
                        "kind": "ipo",
                        "title": f"IPO {name} {what}",
                        "path": f"/monitor/{w.id}",
                    }
                )
    return out


def holding_events(ev: dict[str, Any], today: date, days: int = EVENT_DAYS) -> list[dict[str, Any]]:
    end = today + timedelta(days=days)
    out = []
    for sym, st in (ev.get("stocks") or {}).items():
        for a in st.get("actions") or []:
            d = date.fromisoformat(a["ex_date"])
            if today <= d <= end:
                out.append({"day": a["ex_date"], "kind": "corporate_action", "title": f"{st['name']}: {a['subject']} "
                            f"(ex-date; record {a.get('record_date') or 'n/a'})", "path": f"/stocks/{sym}"})  # fmt: skip
        for m in st.get("board_meetings") or []:
            d = date.fromisoformat(m["day"])
            if today <= d <= end:
                what = "board meeting on results" if m["results"] else f"board meeting ({m['purpose']})"
                out.append({"day": m["day"], "kind": "results" if m["results"] else "board_meeting",
                            "title": f"{st['name']}: {what}", "path": f"/stocks/{sym}"})  # fmt: skip
    return out


# --------------------------------------------------------------------------- the brief
def build_brief(s: Session, now: datetime) -> dict[str, Any]:
    """The morning brief as structured data (the /brief page renders it; `brief_text` makes the push message)."""
    from finresearch.fincalc.dates import fiscal_year, to_ist
    from finresearch.fincalc.tax_calendar import calendar
    from finresearch.portfolio import cache
    from finresearch.portfolio.metrics import advance_tax, lt_watch
    from finresearch.portfolio.report import load
    from finresearch.portfolio.sip import sip_health

    today = to_ist(now).date()
    settings = load_settings(s)
    data = load(s)
    has_pf = bool(data.holdings)
    v = cache.read(s, cache.VALUATION)
    sg = cache.read(s, cache.SIGNALS)
    ev = cache.read(s, cache.EVENTS)

    events = ipo_events(s, today) + holding_events(ev, today)
    sips = [x.json() for x in sip_health(data.holdings, data.txns, today)] if has_pf else []
    for x in sips:
        d = date.fromisoformat(x["next_expected"])
        if today <= d <= today + timedelta(days=EVENT_DAYS) and x["status"] == "on track":
            events.append({"day": x["next_expected"], "kind": "sip", "title": f"SIP {x['name']}: next instalment "
                           f"expected (about {_inr(x['amount'])})", "path": "/portfolio"})  # fmt: skip
    lots = lt_watch(s, data, v, today) if has_pf and v.get("day") else []
    for x in lots:
        if (date.fromisoformat(x["lt_date"]) - today).days <= EVENT_DAYS:
            events.append({"day": x["lt_date"], "kind": "long_term", "title": f"{x['name']}: a lot turns long-term "
                           f"(tax on a sale: {_inr(x['tax_now'])} now vs {_inr(x['tax_later'])} after)",
                           "path": "/portfolio"})  # fmt: skip
    tax_cal = calendar(today, fiscal_year(today), horizon_days=120)
    for t in tax_cal:
        if (date.fromisoformat(t["day"]) - today).days <= 14:
            events.append({"day": t["day"], "kind": "tax", "title": t["title"] + ("" if t["verified"] else " [unverified]"),
                           "path": "/brief#tax"})  # fmt: skip
    events.sort(key=lambda e: (e["day"], e["kind"], e["title"]))

    prev = s.scalars(select(Alert).where(Alert.kind == BRIEF_KIND).order_by(Alert.id.desc())).first()
    since = (
        prev.created_at if prev and prev.created_at and prev.created_at < now else now - timedelta(hours=24)
    )
    since = max(since, now - timedelta(days=4))
    fired = s.scalars(select(Alert).where(Alert.created_at >= since, Alert.kind.not_in((BRIEF_KIND, DIGEST_KIND)))
                      .order_by(Alert.id.desc()).limit(50)).all()  # fmt: skip
    rules = [{"id": a.id, "kind": a.kind, "level": a.level, "message": a.message, "at": a.created_at.isoformat(),
              "path": (a.data or {}).get("path") or (f"/monitor/{a.watch_id}" if a.watch_id else "/monitor")}
             for a in fired]  # fmt: skip

    # signal changes found by a daily pass since the previous brief (so a holiday does not repeat them)
    changes = (sg.get("changes") or []) if sg.get("at") and datetime.fromisoformat(sg["at"]) >= since else []

    health: list[dict[str, Any]] = []
    if has_pf:
        if not v.get("day"):
            health.append({"level": "warn", "text": "No daily valuation yet: the monitor values the portfolio after "
                           "each close while `finresearch serve` runs."})  # fmt: skip
        else:
            age = (today - date.fromisoformat(v["day"])).days
            if age > 4:
                health.append(
                    {"level": "warn", "text": f"The last daily valuation is {age} days old ({v['day']})."}
                )
            for x in v.get("unpriced") or []:
                health.append({"level": "warn", "text": f"{x['name']}: no price ({x['why']})."})
            for x in v.get("stale") or []:
                health.append({"level": "info", "text": f"{x['name']}: price as of {x.get('as_of') or 'unknown'} "
                               f"({x.get('source') or 'no source'})."})  # fmt: skip
        for key, label in ((cache.SIGNALS, "signals"), (cache.EVENTS, "exchange events")):
            c = cache.read(s, key)
            if c.get("errors"):
                health.append({"level": "info", "text": f"{len(c['errors'])} holding(s) had no {label} in the last "
                               "daily pass (the source refused or had no data)."})  # fmt: skip
        if ev.get("bse_only"):
            health.append({"level": "info", "text": f"{len(ev['bse_only'])} BSE-only stock(s): corporate actions and "
                           "results dates are read from NSE only, so they are not in this calendar."})  # fmt: skip
        unknown_cost = sum(1 for h in data.holdings for lot in data.lots.get(h.id, [])
                           if lot.open_quantity > 0 and lot.cost_per_unit is None)  # fmt: skip
        if unknown_cost:
            health.append({"level": "info", "text": f"{unknown_cost} open lot(s) have an unknown cost: tax and XIRR "
                           "skip them until the cost is entered."})  # fmt: skip
    missed = [x for x in sips if x["status"] != "on track"]

    perf = None
    if settings.daily_performance and has_pf:
        perf = value_change(_snaps(s, today - timedelta(days=6))[-2:])
    disc = _disclosures(s, now)
    for x in disc.get("unavailable") or []:
        health.append({"level": "info", "text": f"Disclosures: {x} unavailable (no recent good read); red flags from it "
                       "are not shown as 'none'."})  # fmt: skip
    at = advance_tax(s, data, today) if has_pf else None
    quiet = (
        not rules
        and not changes
        and not missed
        and not [e for e in events if e["day"] == today.isoformat()]
        and not _disclosure_items(disc)
    )
    return {
        "day": today.isoformat(), "generated_at": now.isoformat(), "has_portfolio": has_pf,
        "headline": "Nothing needs a decision today: no rule fired and nothing is due. No action needed."
                    if quiet else _headline(rules, changes, events, missed, today, disc),
        "events": events, "rules_fired": rules, "since": since.isoformat(), "signal_changes": changes,
        "sip": sips, "sip_missed": missed, "long_term": lots, "tax_calendar": tax_cal, "advance_tax": at,
        "health": health, "performance": perf, "settings": settings.model_dump(mode="json"), "disclosures": disc,
        "method": "Deterministic templates over the local database: holdings' events and signals from the monitor's "
                  "daily pass (NSE, AMFI), rule alerts, the dated tax table. Nothing here is sent to an LLM.",
        "behaviour_note": BEHAVIOUR_NOTE, "disclaimer": DISCLAIMER,
    }  # fmt: skip


def _disclosures(s: Session, now: datetime) -> dict[str, Any]:
    """Red flags, insider buying, deals, rating actions and SEBI orders for holdings and the watchlist
    (finresearch.disclosures; database only). A failure here never breaks the brief."""
    try:
        from finresearch.disclosures.views import brief_section

        with s.begin_nested():
            return brief_section(s, now)
    except Exception as e:  # tables missing (not migrated) or a bad row: say so in data health
        import logging

        logging.getLogger(__name__).warning("disclosures section failed", exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"[:200], "unavailable": ["exchange disclosures"]}


def _disclosure_items(disc: dict[str, Any] | None) -> int:
    d = disc or {}
    return len(d.get("flags") or []) + len(d.get("ratings") or []) + len(d.get("sebi_orders") or [])


def _headline(rules: list, changes: list, events: list, missed: list, today: date,
              disc: dict[str, Any] | None = None) -> str:  # fmt: skip
    parts = []
    if n := _disclosure_items(disc):
        parts.append(f"{n} red flag{'s' if n != 1 else ''} or rating/SEBI item{'s' if n != 1 else ''} on holdings "
                     "and the watchlist")  # fmt: skip
    if rules:
        parts.append(
            f"{len(rules)} alert{'s' if len(rules) != 1 else ''} since the last brief (your rules and the monitor)"
        )
    if changes:
        parts.append(f"{len(changes)} signal change{'s' if len(changes) != 1 else ''} on holdings")
    due = [e for e in events if e["day"] == today.isoformat()]
    if due:
        parts.append(f"{len(due)} item{'s' if len(due) != 1 else ''} dated today")
    if missed:
        parts.append(f"{len(missed)} SIP{'s' if len(missed) != 1 else ''} missed or stopped")
    return "; ".join(parts) + ". Review them below; the figures carry their sources."


def brief_text(b: dict[str, Any]) -> str:
    """The push message: short, plain text, most important first, with the disclaimer's short form."""
    lines = [f"Morning brief {date.fromisoformat(b['day']):%a %d %b}", b["headline"]]
    for r in b["rules_fired"][:4]:
        lines.append(f"• {r['message']}")
    for c in b["signal_changes"][:3]:
        lines.append(f"• Signal {c['name']}: {c['from']} → {c['to']}")
    for e in b["events"][:5]:
        lines.append(f"• {date.fromisoformat(e['day']):%d %b}: {e['title']}")
    for x in b["sip_missed"][:2]:
        lines.append(f"• SIP {x['name']}: {x['status']} (last {x['last']})")
    disc = b.get("disclosures") or {}
    for f in (disc.get("flags") or [])[:3]:
        lines.append(f"• Red flag {f['key']}: {f['label']}")
    for r in (disc.get("ratings") or [])[:2]:
        lines.append(
            f"• Rating {r.get('name') or r['key']}: {str(r.get('action', '')).replace('_', ' ')} ({r.get('agency')})"
        )
    for o in (disc.get("sebi_orders") or [])[:1]:
        lines.append(f"• SEBI order may name {o['key']}: {o.get('title', '')[:80]}")
    if b.get("performance"):
        p = b["performance"]
        lines.append(f"• Value {p['twr_pct']:+.2f}% (new money removed) since {p['from']}")
    if b["health"]:
        lines.append(f"• Data health: {len(b['health'])} note(s)")
    lines.append("Personal research, not advice.")
    text = "\n".join(lines)
    return text if len(text) <= PUSH_MAX else text[: PUSH_MAX - 1] + "…"


# --------------------------------------------------------------------------- the weekly digest
def build_digest(s: Session, now: datetime, days: int = 7) -> dict[str, Any]:
    from finresearch.fincalc.dates import to_ist
    from finresearch.portfolio import cache

    today = to_ist(now).date()
    snaps = _snaps(s, today - timedelta(days=days + 4))
    start = [r for r in snaps if r[0] <= today - timedelta(days=days)]
    window = ([start[-1]] if start else []) + [r for r in snaps if r[0] > today - timedelta(days=days)]
    vc = value_change(window)
    v = cache.read(s, cache.VALUATION)
    hist = v.get("history") or {}
    contrib = None
    if vc and hist:
        days_in = sorted(hist)
        a = next((d for d in days_in if d >= vc["from"]), None)
        b = days_in[-1]
        if a and a < b:
            names = {hid: h["name"] for hid, h in (v.get("holdings") or {}).items()}
            contrib = {"from": a, "to": b, **contributors(hist, names, a, b)}
    sg = cache.read(s, cache.SIGNALS)
    return {
        "day": today.isoformat(), "generated_at": now.isoformat(), "window_days": days, "value": vc,
        "contributors": contrib, "signals": sorted((sg.get("items") or {}).values(), key=lambda x: -(x.get("weight_pct") or 0))[:10],
        "benchmark": "Comparison with NIFTYBEES over the same window needs the portfolio value history "
                     "(portfolio analytics); not shown yet.",
        "method": "ΔV = value now − value a week ago; new money = change in net invested (buys − sales); market "
                  "move = ΔV − new money; time-weighted return chains daily (V_t − flow_t) / V_{t−1}. Contributors: "
                  "units at the start × price change, from the monitor's daily valuations.",
        "behaviour_note": BEHAVIOUR_NOTE, "disclaimer": DISCLAIMER,
    }  # fmt: skip


def digest_text(d: dict[str, Any]) -> str:
    lines = [f"Weekly digest to {date.fromisoformat(d['day']):%d %b}"]
    vc = d.get("value")
    if not vc:
        lines.append(
            "Not enough daily valuations this week to compare (the monitor values the portfolio after each close)."
        )
    else:
        lines.append(f"Time-weighted return {vc['twr_pct']:+.2f}% ({vc['from']} to {vc['to']}): market move "
                     f"{_inr(vc['market'])}, new money {_inr(vc['new_money'])}.")  # fmt: skip
        c = d.get("contributors") or {}
        if c.get("top"):
            lines.append("Up most: " + ", ".join(f"{x['name']} {_inr(x['inr'])}" for x in c["top"]))
        if c.get("bottom"):
            lines.append("Down most: " + ", ".join(f"{x['name']} {_inr(x['inr'])}" for x in c["bottom"]))
    lines.append("Personal research, not advice.")
    text = "\n".join(lines)
    return text if len(text) <= PUSH_MAX else text[: PUSH_MAX - 1] + "…"


# --------------------------------------------------------------------------- scheduling and delivery
def due(now: datetime, holidays: set | None = None) -> list[tuple[str, str]]:
    """[("brief", "brief:<day>"), ("digest", "digest:<day>")] due at `now` (IST): the brief at 08:30 on trading
    days, the digest at 09:00 on Sundays; each only within LATE_BY of its time."""
    from finresearch.fincalc.dates import is_business_day, ist_datetime, to_ist

    ist = to_ist(now)
    day = ist.date()
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    out = []
    at = ist_datetime(day, BRIEF_AT.hour, BRIEF_AT.minute)
    if is_business_day(day, holidays) and at <= ist < at + LATE_BY:
        out.append(("brief", f"brief:{day.isoformat()}"))
    at = ist_datetime(day, DIGEST_AT.hour, DIGEST_AT.minute)
    if day.weekday() == 6 and at <= ist < at + LATE_BY:
        out.append(("digest", f"digest:{day.isoformat()}"))
    return out


def _claim(s: Session, slot: str, now: datetime) -> bool:
    got = s.execute(insert(AlertEvalSlot).values(slot=slot, started_at=now, result={})
                    .on_conflict_do_nothing(index_elements=["slot"]).returning(AlertEvalSlot.slot)).first()  # fmt: skip
    return got is not None


def publish(s: Session, kind: str, now: datetime) -> Alert:
    """Build and store one brief or digest as an in-app alert, and queue it on the chosen channels."""
    from finresearch.monitor import notify

    settings = load_settings(s)
    if kind == "brief":
        body = build_brief(s, now)
        text, akind, title = brief_text(body), BRIEF_KIND, "FinResearch · Morning brief"
    else:
        body = build_digest(s, now)
        text, akind, title = digest_text(body), DIGEST_KIND, "FinResearch · Weekly digest"
    a = Alert(watch_id=None, kind=akind, level="info", message=text, data={"path": "/brief", kind: body})
    s.add(a)
    s.flush()
    notify.queue(s, a, list(settings.channels), "default", title=title, message=text, path="/brief", now=now)
    return a


def brief_step(now: datetime, *, holidays: set | None = None) -> dict[str, int]:
    """The monitor's brief work for this tick (DB only, fast)."""
    from finresearch.db import session_scope

    out: dict[str, int] = {}
    passes = due(now, holidays)
    if not passes:
        return out
    with session_scope() as s:
        settings = load_settings(s)
    if not settings.enabled:
        return out
    for kind, slot in passes:
        if kind == "digest" and not settings.weekly_digest:
            continue
        with session_scope() as s:
            if not _claim(s, slot, now):
                continue
            publish(s, kind, now)
            out[kind] = 1
    return out
