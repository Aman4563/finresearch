"""Alert readings for the disclosure metrics (alerts.registry, "disclosures" block), from the database only.

A reading is unknown (never fires) when the feed behind it has no recent good read; event metrics compare with the
rule's baseline (the last stage seen, the newest adverse rating filing or SEBI order seen)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from finresearch.disclosures import store, views
from finresearch.fincalc.dates import to_ist

NOW: Callable[[], datetime] | None = None  # test seam

STOCK_METRICS = ("pledge_pct", "pledge_change_pp", "surveillance_stage", "in_fno_ban", "insider_net_buy_90d",
                 "bulk_block_deals_5d", "rating_action", "sebi_order")  # fmt: skip


def _now() -> datetime:
    return NOW() if NOW else datetime.now(UTC)


def _new_item(field: str, latest: dict[str, Any] | None, baseline: dict[str, Any], today: date, source: str,
              as_of: str | None):  # fmt: skip
    """A 0/1 "something new appeared" flag: 1 when the newest item differs from the one in the baseline. On a rule's
    first check (no baseline yet) it fires only for an item at most NEW_ITEM_DAYS old."""
    from finresearch.alerts.compute import Reading

    key = latest.get("_key") if latest else None
    if field not in baseline:
        day = (latest or {}).get("_day")
        fresh = bool(key and day and day >= (today - timedelta(days=views.NEW_ITEM_DAYS)).isoformat())
        return Reading(Decimal(int(fresh)), source, as_of, baseline={field: key},
                       detail=(latest or {}).get("_text") if fresh else None)  # fmt: skip
    changed = key is not None and key != baseline.get(field)
    return Reading(Decimal(int(changed)), source, as_of, baseline={field: key},
                   detail=(latest or {}).get("_text") if changed else None)  # fmt: skip


def _rating_latest(
    s: Session, now: datetime, isin: str | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    r = views.ratings(s, now, isin=isin)
    a = r["latest_adverse"]
    if a is None:
        return r, None
    text = f"{a.get('agency') or 'an agency'}: {a.get('action', '').replace('_', ' ')} to {a.get('rating') or '?'}" + (
        f" (outlook {a['outlook']})" if a.get("outlook") else "") + f", rated {a.get('rating_day')}"  # fmt: skip
    key = store.dedupe_key(
        a.get("isin"), a.get("agency"), a.get("rating_day"), a.get("rating"), a.get("action_raw")
    )
    return r, {"_key": key, "_day": a.get("rating_day"), "_text": text}


def stock_reading(s: Session, metric: str, symbol: str, baseline: dict[str, Any]):
    from finresearch.alerts.compute import Reading, unknown

    now = _now()
    today = to_ist(now).date()
    if symbol.startswith("BSE:"):
        return unknown("a BSE-only stock: NSE's disclosure feeds do not list it")
    if metric in ("surveillance_stage", "in_fno_ban"):
        surv = views.surveillance(s, symbol, now, views.resolve_isin(s, symbol))
        if metric == "in_fno_ban":
            b = surv["fno_ban"]
            if b["in_ban"] is None:
                return unknown(f"F&O ban list unavailable: {b.get('reason')}", b["source"])
            return Reading(
                Decimal(int(b["in_ban"])), f"NSE F&O ban list for {b['trade_date']}", b["trade_date"]
            )
        stage = views.stage_text(surv)
        if stage is None:
            bad = [k.upper() for k in ("asm", "gsm") if surv[k]["state"] != "ok"]
            return unknown(f"NSE {' and '.join(bad)} list unavailable", "NSE ASM/GSM reports")
        before = baseline.get("stage")
        changed = before is not None and before != stage
        return Reading(Decimal(int(changed)), "NSE ASM and GSM reports", surv["asm"]["as_of"], baseline={"stage": stage},
                       detail=f"{before} -> {stage}" if changed else None)  # fmt: skip
    if metric in ("pledge_pct", "pledge_change_pp"):
        p = views.pledge(s, symbol, now)
        if p["state"] != "ok":
            return unknown(f"NSE pledge data unavailable: {p.get('reason')}", p["source"])
        latest = p["latest"] or {}
        if p["no_record"] or not latest:
            return unknown(f"NSE has no pledge record for {symbol}", p["source"])
        if metric == "pledge_pct":
            v = latest.get("pct_of_promoter")
            if v is None:
                return unknown(f"no promoter holding figure for {symbol} in NSE's pledge data", p["source"])
            return Reading(Decimal(v), f"NSE pledged data, quarter ended {latest.get('quarter_end')}",
                           latest.get("quarter_end"))  # fmt: skip
        if p["change_pp"] is None:
            return unknown(f"only one quarter of pledge data recorded for {symbol} so far", p["source"])
        return Reading(Decimal(p["change_pp"]), f"NSE pledged data: {p['prev_quarter']} -> {latest.get('quarter_end')}",
                       latest.get("quarter_end"))  # fmt: skip
    if metric == "insider_net_buy_90d":
        ins = views.insider(s, symbol, now)
        if ins["state"] != "ok":
            return unknown(f"NSE insider filings unavailable: {ins.get('reason')}", ins["source"])
        if not ins["complete"]:
            return unknown("insider filings incomplete: " + "; ".join(ins["problems"]), ins["source"])
        n = ins["net"]
        return Reading(Decimal(n["net_value"]), f"NSE PIT filings {n['start']} to {n['end']}: {n['n_counted']} open-"
                       f"market trades counted", n["end"])  # fmt: skip
    if metric == "bulk_block_deals_5d":
        d = views.deals(s, symbol, now)
        if d["state"] != "ok":
            return unknown(f"NSE deals unavailable: {d.get('reason')}", d["source"])
        return Reading(
            Decimal(d["recent_n"]), f"NSE bulk and block deals, last {d['recent_days']} days", d["as_of"]
        )
    if metric == "rating_action":
        isin = views.resolve_isin(s, symbol)
        if isin is None:
            return unknown(f"no ISIN known for {symbol} (needed to match rating filings)")
        r, latest = _rating_latest(s, now, isin)
        if r["state"] != "ok":
            return unknown(f"credit-rating filings unavailable: {r.get('reason')}", r["source"])
        return _new_item("rating", latest, baseline, today, "NSE credit-rating filings", r["as_of"])
    if metric == "sebi_order":
        o = views.sebi_orders(s, symbol, now)
        if o["state"] != "ok":
            return unknown(f"SEBI RSS unavailable: {o.get('reason')}", o["source"])
        top = o["orders"][0] if o["orders"] else None
        latest = {"_key": top["link"], "_day": top.get("day"), "_text": top["title"][:200]} if top else None
        return _new_item("order", latest, baseline, today, "SEBI RSS (possible match by name)", o["as_of"])
    return unknown(f"unknown disclosure metric {metric}")


def bond_reading(s: Session, isin: str, baseline: dict[str, Any]):
    from finresearch.alerts.compute import unknown

    now = _now()
    r, latest = _rating_latest(s, now, isin)
    if r["state"] != "ok":
        return unknown(f"credit-rating filings unavailable: {r.get('reason')}", r["source"])
    if r["issuer_code"] is None:
        return unknown(f"{isin} is not an Indian company ISIN (no issuer code to match rating filings)")
    return _new_item("rating", latest, baseline, to_ist(now).date(), "NSE credit-rating filings", r["as_of"])


def fno_ban_reading(s: Session, symbol: str):
    from finresearch.alerts.compute import Reading, unknown

    b = views.surveillance(s, symbol.upper(), _now())["fno_ban"]
    if b["in_ban"] is None:
        return unknown(f"F&O ban list unavailable: {b.get('reason')}", b["source"])
    return Reading(Decimal(int(b["in_ban"])), f"NSE F&O ban list for {b['trade_date']}", b["trade_date"])


def holdings_red_flags(s: Session):
    """(value, source, detail) for the portfolio metric: held NSE stocks with a surveillance, ban or rising-pledge
    flag. None when a list could not be read."""
    now = _now()
    t = store.tracked(s)
    held = {k: v for k, v in t.stocks.items() if v["held"]}
    if not held:
        return None, "no held NSE stocks", None
    names = []
    for sym, v in sorted(held.items()):
        surv = views.surveillance(s, sym, now, v.get("isin"))
        if any(surv[k]["state"] != "ok" for k in ("asm", "gsm", "fno_ban")):
            bad = [views.LABEL[k] for k in ("asm", "gsm", "fno_ban") if surv[k]["state"] != "ok"]
            return None, f"unavailable: {', '.join(bad)}", None
        flags = [
            f
            for f in views.flags_of(sym, surv, views.pledge(s, sym, now))
            if f["kind"] != "pledge" or f["tone"] == "warn"
        ]
        if flags:
            names.append(f"{sym} ({flags[0]['label']})")
    return Decimal(len(names)), "NSE ASM/GSM lists, F&O ban list and pledged data", ", ".join(names) or None
