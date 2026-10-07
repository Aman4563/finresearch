"""/api/brief: the morning brief, the weekly digest, the calendars, the dashboard's portfolio strip and the full export
of your data (the daily layer).

Every route reads the local database only (no network): the numbers come from the monitor's daily portfolio pass
(monitor.portfolio_daily) and the stored transactions. Personal data never goes to an LLM. The export is a zip of
your own data as JSON and CSV, watermarked "PERSONAL – NOT FOR DISTRIBUTION"; notification secrets (ntfy topic and
token, Telegram bot token) and derived caches are left out.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError
from sqlalchemy import select

from finresearch.db import session_scope

WATERMARK = "PERSONAL – NOT FOR DISTRIBUTION"
CALENDAR_DAYS = 45


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return format(v.normalize(), "f") if v == v.to_integral_value() or abs(v) >= 1 else str(v)
    return v


def rows_of(s: Any, model: Any, where: Any = None, limit: int | None = None) -> list[dict[str, Any]]:
    cols = [c.key for c in model.__table__.columns]
    q = select(model)
    if where is not None:
        q = q.where(where)
    pk = list(model.__table__.primary_key.columns)
    if pk:
        q = q.order_by(*pk)
    if limit:
        q = q.limit(limit)
    return [{c: _jsonable(getattr(r, c)) for c in cols} for r in s.scalars(q)]


def _csv(rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    if not rows:
        return ""
    cols = list(rows[0])
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(
            {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v for k, v in r.items()}
        )
    return buf.getvalue()


def export_tables(s: Any) -> dict[str, list[dict[str, Any]]]:
    """Your data, table by table. Left out: notification settings and broker connections (secrets: tokens, API
    keys), delivery and sync logs, and derived caches."""
    from finresearch.db import models as M

    out = {
        "investor_profile": rows_of(s, M.InvestorProfile),
        "portfolio_holdings": rows_of(s, M.PortfolioHolding),
        "portfolio_transactions": rows_of(s, M.PortfolioTxn),
        "portfolio_lots": rows_of(s, M.PortfolioLot),
        "portfolio_disposals": rows_of(s, M.PortfolioDisposal),
        "portfolio_imports": rows_of(s, M.PortfolioImport),
        "portfolio_snapshots": rows_of(s, M.PortfolioSnapshot),
        "portfolio_settings": rows_of(s, M.PortfolioSetting, M.PortfolioSetting.key.not_like("cache:%")),
        "decision_journal": rows_of(s, M.Decision),
        "trade_journal": rows_of(s, M.TradeNote),
        "watches": rows_of(s, M.Watch),
        "alerts": rows_of(s, M.Alert, limit=5000),
        "alert_rule_state": rows_of(s, M.AlertRuleState),
    }
    # household finances (/wealth), when that module's tables exist; broker connections are left out (tokens)
    for name, model in (("wealth_assets", "WealthAsset"), ("wealth_valuations", "WealthValuation"),
                        ("wealth_loans", "WealthLoan"), ("wealth_goals", "WealthGoal"), ("wealth_policies", "WealthPolicy")):  # fmt: skip
        if hasattr(M, model):
            out[name] = rows_of(s, getattr(M, model))
    return out


def build_zip(tables: dict[str, list[dict[str, Any]]], now: datetime) -> bytes:
    from finresearch.signals.base import DISCLAIMER

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        readme = (f"{WATERMARK}\n\nFinResearch export of your own data, {now:%d %b %Y %H:%M} UTC.\n"
                  "json/<table>.json holds every row; csv/<table>.csv the same rows (nested fields as JSON text).\n"
                  "Left out on purpose: notification settings and broker connections (they hold secrets), the "
                  "delivery and sync logs and derived caches (rebuilt by the monitor).\n\n" + DISCLAIMER + "\n")  # fmt: skip
        z.writestr("README.txt", readme)
        z.writestr("manifest.json", json.dumps({"watermark": WATERMARK, "exported_at": now.isoformat(),
                                                "tables": {k: len(v) for k, v in tables.items()}}, indent=2))  # fmt: skip
        for name, rows in tables.items():
            z.writestr(f"json/{name}.json", json.dumps({"watermark": WATERMARK, "rows": rows}, ensure_ascii=False,
                                                       indent=1, default=str))  # fmt: skip
            z.writestr(f"csv/{name}.csv", f"# {WATERMARK}\n" + _csv(rows))
    return buf.getvalue()


def strip(s: Any, now: datetime) -> dict[str, Any]:
    """The dashboard's portfolio strip, from stored data only: value, the week's flows-adjusted change, LTCG headroom
    and the latest portfolio alert. Daily P&L is deliberately not shown (see monitor.digest)."""
    from finresearch.db.models import Alert, PortfolioSnapshot
    from finresearch.fincalc.dates import fiscal_year, to_ist
    from finresearch.monitor.digest import BEHAVIOUR_NOTE, value_change
    from finresearch.portfolio import cache
    from finresearch.portfolio.report import disposal_rows, load
    from finresearch.portfolio.tax import fy_summary

    today = to_ist(now).date()
    data = load(s)
    if not data.holdings:
        return {"has_portfolio": False}
    last = s.scalars(select(PortfolioSnapshot).order_by(PortfolioSnapshot.day.desc())).first()
    from finresearch.portfolio import series

    ser, _why = series.load(s)  # the week's change reads the canonical value history (#239)
    rows = ser.rows(today - timedelta(days=11)) if ser is not None else []
    start = [r for r in rows if r[0] <= today - timedelta(days=7)]
    week = value_change(
        ([start[-1]] if start else []) + [r for r in rows if r[0] > today - timedelta(days=7)]
    )
    ex = fy_summary(disposal_rows(data), fiscal_year(today), Decimal("0.30"))["exemption"]
    # an unclassified disposal may have used the exemption: the headroom is unknown (#213)
    if not ex["complete"]:
        ex = {**ex, "remaining": None}
    alert = s.scalars(select(Alert).where(Alert.kind == "rule_alert", Alert.created_at >= now - timedelta(days=7))
                      .order_by(Alert.id.desc()).limit(50)).all()  # fmt: skip
    pf = next((a for a in alert if (a.data or {}).get("rule_kind") == "portfolio"), None)
    v = cache.read(s, cache.VALUATION)
    nw = None
    try:  # net worth from the household module (/wealth), only when manual assets or loans are entered
        from finresearch.wealth.service import load as load_wealth
        from finresearch.wealth.service import net_worth_on

        book = load_wealth(s)
        if book.assets or book.loans:
            nw = net_worth_on(book, today)
    except Exception:  # the module or its tables are absent: the strip works without it
        nw = None
    return {
        "has_portfolio": True, "net_worth": nw,
        "value": float(last.value) if last else None, "as_of": last.day.isoformat() if last else None,
        "complete": bool(last.complete) if last else False,
        "holdings": sum(1 for h in data.holdings if any(lot.open_quantity > 0 for lot in data.lots.get(h.id, []))),
        "week": week, "ltcg_headroom": ex["remaining"], "ltcg_limit": ex["limit"],
        "top_alert": {"message": pf.message, "at": pf.created_at.isoformat(), "level": pf.level} if pf else None,
        "unpriced": len(v.get("unpriced") or []) + len(v.get("stale") or []), "daily_pass": v.get("day"),
        "note": BEHAVIOUR_NOTE,
    }  # fmt: skip


def calendar_view(s: Any, now: datetime, days: int = CALENDAR_DAYS) -> dict[str, Any]:
    """Dated items for the next `days` days: holdings' corporate actions and results meetings, watched IPOs, SIP
    instalments, lots turning long-term, ELSS lots finishing their lock-in and the tax calendar."""
    from finresearch.fincalc.dates import fiscal_year, to_ist
    from finresearch.fincalc.tax_calendar import calendar
    from finresearch.monitor.digest import elss_event, holding_events, ipo_events
    from finresearch.portfolio import cache
    from finresearch.portfolio.metrics import advance_tax, elss_unlocks, lt_watch
    from finresearch.portfolio.report import load
    from finresearch.portfolio.sip import sip_health

    today = to_ist(now).date()
    data = load(s)
    ev = cache.read(s, cache.EVENTS)
    v = cache.read(s, cache.VALUATION)
    items = ipo_events(s, today, days) + holding_events(ev, today, days)
    sips = sip_health(data.holdings, data.txns, today)
    for x in sips:
        if x.status == "on track" and x.next_expected <= today + timedelta(days=days):
            items.append({"day": x.next_expected.isoformat(), "kind": "sip", "title": f"SIP {x.name}: next instalment "
                          f"expected (about ₹{x.amount:,.0f})", "path": "/portfolio"})  # fmt: skip
    lots = lt_watch(s, data, v, today) if data.holdings and v.get("day") else []
    for x in lots:
        items.append({"day": x["lt_date"], "kind": "long_term", "title": f"{x['name']}: a lot turns long-term",
                      "path": "/portfolio"})  # fmt: skip
    unlocks = elss_unlocks(s, data, today, days) if data.holdings else []
    items += [elss_event(x) for x in unlocks]
    tax = calendar(today, fiscal_year(today), horizon_days=max(days, 120))
    items += [{"day": t["day"], "kind": "tax", "title": t["title"], "verified": t["verified"], "note": t["note"],
               "path": "/brief#tax"} for t in tax if t["day"] <= (today + timedelta(days=days)).isoformat()]  # fmt: skip
    return {"from": today.isoformat(), "days": days, "items": sorted(items, key=lambda e: (e["day"], e["kind"])),
            "tax": tax, "advance_tax": advance_tax(s, data, today) if data.holdings else None,
            "long_term": lots, "sip": [x.json() for x in sips], "elss_unlocks": unlocks, "events_read": ev.get("day"),
            "bse_only": ev.get("bse_only") or []}  # fmt: skip


def add_brief_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    @app.get("/api/brief")
    def brief() -> dict[str, Any]:
        """Today's brief, built now from stored data, plus the list of briefs and digests already sent."""
        from finresearch.db.models import Alert
        from finresearch.monitor.digest import BRIEF_KIND, DIGEST_KIND, build_brief

        with session_scope() as s:
            body = build_brief(s, now())
            past = s.scalars(select(Alert).where(Alert.kind.in_((BRIEF_KIND, DIGEST_KIND)))
                             .order_by(Alert.id.desc()).limit(30)).all()  # fmt: skip
            body["history"] = [{"id": a.id, "kind": a.kind, "at": a.created_at.isoformat(),
                                "message": a.message} for a in past]  # fmt: skip
            return body

    @app.get("/api/brief/digest")
    def digest() -> dict[str, Any]:
        from finresearch.monitor.digest import build_digest

        with session_scope() as s:
            return build_digest(s, now())

    @app.get("/api/brief/calendar")
    def cal() -> dict[str, Any]:
        with session_scope() as s:
            return calendar_view(s, now())

    @app.get("/api/brief/settings")
    def get_settings() -> dict[str, Any]:
        from finresearch.monitor import notify
        from finresearch.monitor.digest import load_settings

        with session_scope() as s:
            ready = {ch: notify.configured(ch, notify.load(s, ch)) or "ready" for ch in notify.CHANNELS}
            return {**load_settings(s).model_dump(mode="json"), "channel_status": ready}

    @app.put("/api/brief/settings")
    async def put_settings(request: Request) -> dict[str, Any]:
        from finresearch.monitor.digest import save_settings

        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(422, "the body must be JSON") from e
        if not isinstance(body, dict):
            raise HTTPException(422, "the body must be a JSON object")
        try:
            with session_scope() as s:
                return save_settings(s, body).model_dump(mode="json")
        except ValidationError as e:
            raise HTTPException(422, "; ".join(err["msg"] for err in e.errors())) from e

    @app.post("/api/brief/send")
    def send_now(kind: str = "brief") -> dict[str, Any]:
        """Build and send a brief (or digest) now, in-app and on the chosen channels (a manual test)."""
        from finresearch.monitor.digest import publish

        if kind not in ("brief", "digest"):
            raise HTTPException(422, "kind must be brief or digest")
        with session_scope() as s:
            a = publish(s, kind, now())
            return {"id": a.id, "kind": a.kind, "message": a.message}

    @app.get("/api/brief/{alert_id}")
    def stored(alert_id: int) -> dict[str, Any]:
        from finresearch.db.models import Alert
        from finresearch.monitor.digest import BRIEF_KIND, DIGEST_KIND

        with session_scope() as s:
            a = s.get(Alert, alert_id)
            if a is None or a.kind not in (BRIEF_KIND, DIGEST_KIND):
                raise HTTPException(404, f"no brief {alert_id}")
            return {"id": a.id, "kind": a.kind, "at": a.created_at.isoformat(), "message": a.message,
                    "body": (a.data or {}).get("brief") or (a.data or {}).get("digest")}  # fmt: skip

    @app.get("/api/dashboard/portfolio")
    def dashboard_portfolio() -> dict[str, Any]:
        with session_scope() as s:
            return strip(s, now())

    @app.get("/api/export/all.zip")
    def export_all() -> Response:
        """Everything of yours as a zip of JSON and CSV (watermarked; secrets and caches left out)."""
        t = now()
        with session_scope() as s:
            data = build_zip(export_tables(s), t)
        name = f"finresearch-export-{t:%Y%m%d-%H%M}.zip"
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})  # fmt: skip
