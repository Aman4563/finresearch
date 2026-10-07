"""The monitor's daily portfolio pass (roadmap P14): value the portfolio after the close, compute each holding's
signal once, and read the dated events of the holdings, so the alert metrics, the morning brief and the dashboard
strip work from stored data without opening /portfolio.

When (IST, trading days only):
- "close" pass at CLOSE_AT, or 30 minutes before the profile's after-close check time when that is later, so the
  daily alert pass (at the check time) reads today's numbers: valuation + holdings' signals + events + TER;
- "nav" pass at NAV_AT when mutual funds are held: AMFI publishes the day's NAVs in the evening, so the day's
  snapshot is re-valued with them (the latest valuation of a day wins).

Valuation is done here in Python, exactly as the portfolio page does it (portfolio.valuation.fetch_prices with one
QuoteBatch session, portfolio.report.snapshot, portfolio.metrics.record_snapshot), never by calling
GET /api/portfolio. Each pass is claimed in `alert_eval_slot` ("pf_daily:<day>", "pf_nav:<day>"), so two monitor
processes never run it twice; a failed pass is retried up to MAX_ATTEMPTS times, RETRY_AFTER apart.

Politeness: quotes share one NSE session at the client's 2 requests a second; signals and events are capped at
MAX_INSTRUMENTS per pass with `pf_spacing_s` between instruments; AMFI's NAV and TER files are one download each.
Signals are computed with ctx log=0: a scheduled check never writes the forecast ledger (only a viewed signal does,
per signals.ledger's logging rules). Personal data stays in the local database; nothing here calls a model.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from finresearch.db import session_scope
from finresearch.db.models import AlertEvalSlot, PortfolioHolding, PortfolioTxn
from finresearch.portfolio import cache

log = logging.getLogger(__name__)

CLOSE_AT = time(15, 50)  # NSE publishes the official close shortly after 15:30 (fincalc.price)
NAV_AT = time(23, 0)
MAX_INSTRUMENTS = 40
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=20)
STALE_PRICE_DAYS = 4  # a price older than this (calendar days) is flagged in data health
UPCOMING_DAYS = 45


def close_time(stock_time: tuple[int, int]) -> time:
    at = datetime(2000, 1, 1, *stock_time) - timedelta(minutes=30)
    return max(CLOSE_AT, at.time())


def due_passes(
    now: datetime, stock_time: tuple[int, int], holidays: set | None = None
) -> list[tuple[str, str]]:
    """[("close", "pf_daily:<day>"), ("nav", "pf_nav:<day>")] as due at `now` (trading days only)."""
    from finresearch.fincalc.dates import is_business_day, to_ist

    ist = to_ist(now)
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    if not is_business_day(ist.date(), holidays):
        return []
    out = []
    if ist.time() >= close_time(stock_time):
        out.append(("close", f"pf_daily:{ist.date().isoformat()}"))
    if ist.time() >= NAV_AT:
        out.append(("nav", f"pf_nav:{ist.date().isoformat()}"))
    return out


def claim(slot: str, now: datetime) -> bool:
    """Claim a pass: a new slot, or a failed one whose retry time has come (at most MAX_ATTEMPTS)."""
    with session_scope() as s:
        got = s.execute(insert(AlertEvalSlot).values(slot=slot, started_at=now, result={"status": "running", "attempts": 1})
                        .on_conflict_do_nothing(index_elements=["slot"]).returning(AlertEvalSlot.slot)).first()  # fmt: skip
        if got is not None:
            return True
        row = s.scalars(
            select(AlertEvalSlot).where(AlertEvalSlot.slot == slot).with_for_update(skip_locked=True)
        ).first()
        if row is None:
            return False
        r = dict(row.result or {})
        if r.get("status") != "failed" or int(r.get("attempts", 1)) >= MAX_ATTEMPTS:
            return False
        if r.get("retry_at") and datetime.fromisoformat(r["retry_at"]) > now:
            return False
        row.result = {**r, "status": "running", "attempts": int(r.get("attempts", 1)) + 1}
        return True


def _finish(slot: str, result: dict[str, Any], now: datetime, error: str | None = None) -> None:
    with session_scope() as s:
        row = s.get(AlertEvalSlot, slot)
        if row is None:
            return
        attempts = int((row.result or {}).get("attempts", 1))
        if error:
            row.result = {"status": "failed", "attempts": attempts, "error": error[:500],
                          "retry_at": (now + RETRY_AFTER).isoformat()}  # fmt: skip
        else:
            row.result = {"status": "done", "attempts": attempts, **result}


def has_holdings() -> tuple[bool, bool]:
    """(any open holding, any mutual fund)."""
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(PortfolioHolding)) or 0
        mf = (
            s.scalar(
                select(func.count()).select_from(PortfolioHolding).where(PortfolioHolding.asset_type == "mf")
            )
            or 0
        )
    return n > 0, mf > 0


async def portfolio_step(deps: Any, now: datetime, *, holidays: set | None = None,
                         stock_time: tuple[int, int] | None = None) -> dict[str, Any]:  # fmt: skip
    """The monitor's portfolio work for this tick: run whichever pass is due and not yet done."""
    if not getattr(deps, "portfolio_daily", False):
        return {}
    any_h, any_mf = has_holdings()
    if not any_h:
        return {}
    if stock_time is None:
        from finresearch.monitor.scheduler import watch_windows

        stock_time = watch_windows().stock_time()
    out: dict[str, Any] = {}
    for kind, slot in due_passes(now, stock_time, holidays):
        if kind == "nav" and not any_mf:
            continue
        if not claim(slot, now):
            continue
        try:
            res = await daily_pass(deps, now, full=kind == "close")
        except Exception as e:
            log.warning("portfolio %s pass failed; retried later", kind, exc_info=True)
            _finish(slot, {}, now, f"{type(e).__name__}: {e}")
            out[kind] = "failed"
            continue
        _finish(slot, res, now)
        out[kind] = res
    return out


# --------------------------------------------------------------------------- the pass
async def daily_pass(deps: Any, now: datetime, *, full: bool = True) -> dict[str, Any]:
    """Value the portfolio (and, for the close pass, signals, events and TER). Returns a small summary."""
    from finresearch.fincalc.dates import to_ist

    today = to_ist(now).date()
    res: dict[str, Any] = {}
    val = await valuation_pass(deps, now, today)
    res["valuation"] = {k: val.get(k) for k in ("value", "complete", "holdings", "unpriced")}
    try:
        res["history"] = await history_pass(deps, today)
    except Exception as e:  # the value history never fails the pass; the next one rebuilds it
        log.warning("portfolio value history failed", exc_info=True)
        res["history"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    if not full:
        return res
    for name, fn in (("signals", signals_pass), ("events", events_pass), ("ter", ter_pass)):
        try:
            got = await fn(deps, now, today, val)
            res[name] = {
                k: v for k, v in got.items() if k in ("computed", "errors_n", "changes_n", "funds", "stocks")
            }
        except Exception as e:  # one failed part never loses the others; the next pass retries it
            log.warning("portfolio %s step failed", name, exc_info=True)
            res[name] = {"error": f"{type(e).__name__}: {e}"[:300]}
    return res


HISTORY_TIMEOUT_S = (
    600  # the first build reads years of closes (later ones only the missing days: PriceStore)
)


async def live_history(holdings: list[Any], today: date) -> Any:
    """Build the reconstructed value history with the live sources (NSE/BSE closes, AMFI NAV history), as the
    Performance tab does, without the benchmark (the stored series does not need it)."""
    from finresearch.api.markets import MarketSources
    from finresearch.config import get_settings
    from finresearch.portfolio.history import Fetcher, PriceStore, build

    schemes: dict[str, Any] = {}
    if any(h.asset_type == "mf" for h in holdings):
        for r in await _live_nav_rows():
            schemes[r.code] = r
            for i in (r.isin_growth, r.isin_reinvest):
                if i:
                    schemes[f"ISIN:{i.upper()}"] = r
    async with Fetcher(MarketSources()) as f:
        return await build(holdings, fetch=f, store=PriceStore(get_settings().state_dir / "portfolio_history"),
                           schemes=schemes, today=today, benchmark=False)  # fmt: skip


async def history_pass(deps: Any, today: date) -> dict[str, Any]:
    """Rebuild the canonical value history (portfolio.series, #239), store it, and reconcile the day's snapshots
    against it. Skipped (with the reason) when the deps have no builder."""
    from finresearch.portfolio import series
    from finresearch.portfolio.history import fingerprint, holdings_from
    from finresearch.portfolio.report import load

    if deps.pf_history is None:
        return {"skipped": "no value-history builder configured"}
    with session_scope() as s:
        hs = holdings_from(load(s))
    fp = fingerprint(hs)
    hist = await asyncio.wait_for(deps.pf_history(hs, today), HISTORY_TIMEOUT_S)
    with session_scope() as s:
        if not series.save(s, hist, fp, today):
            return {"stored": False, "reason": hist.reason or "fewer than two days of prices"}
        rec = series.reconcile_db(s, series.from_history(hist, fp, today), today)
    return {"stored": True, "days": len(hist.days), "checked": rec["checked"], "differ": len(rec["differ"])}


def _detached_holdings() -> list[Any]:
    with session_scope() as s:
        hs = list(s.scalars(select(PortfolioHolding)))
        s.expunge_all()
        return hs


async def _live_nav_rows() -> list:
    from finresearch.adapters.amfi import AmfiClient

    async with AmfiClient() as amfi:
        return await amfi.nav_all()


async def _live_listings() -> Any:
    """NSE + BSE listings merged by ISIN for the valuation's ISIN lookups (a holding with only an ISIN, a BSE-only
    company under a broker's placeholder symbol: portfolio.valuation.instrument_of). The stored ISIN map is used while
    fresh (the API writes it whenever it loads the listings); else both lists are downloaded and the map stored."""
    from datetime import UTC

    from finresearch.adapters.bse_equity import BseEquity, Listing, Listings, merge_listings
    from finresearch.adapters.http import PoliteClient
    from finresearch.adapters.nse_equity import EQUITY_LIST_URL, parse_equity_list
    from finresearch.disclosures import store

    now = datetime.now(UTC)
    with session_scope() as s:
        row = store.feed(s, store.ISIN_MAP)
        fresh = row is not None and row.ok_at is not None and now - row.ok_at < store.ISIN_MAP_FRESH
        m = store.isin_map(s) if fresh else {}
    if m:
        return Listings(rows=[Listing(key=nse or f"BSE:{bse}", symbol=nse or bse or isin, name=isin, isin=isin,
                                      exchange="both" if nse and bse else ("NSE" if nse else "BSE"),
                                      exchanges=[x for x, c in (("NSE", nse), ("BSE", bse)) if c], nse_symbol=nse,
                                      bse_code=bse) for isin, (nse, bse) in m.items()])  # fmt: skip
    async with PoliteClient() as c:
        resp = await c.get(EQUITY_LIST_URL, headers={"Referer": "https://www.nseindia.com/"})
    if not resp.ok:
        raise RuntimeError(f"NSE equity list unavailable (HTTP {resp.status})")
    nse_rows = parse_equity_list(resp.content.decode("utf-8", "replace"))
    async with BseEquity() as bse_client:
        bse_rows = await bse_client.scrips()
    listings = merge_listings(nse_rows, bse_rows)
    with session_scope() as s:
        store.record_isin_map(s, listings, now, force=True)
    return listings


def plan_of(*names: str | None) -> str | None:
    """ "direct" / "regular" from a scheme name (AMFI names say "Direct Plan" or "Regular Plan"), else None."""
    text = " ".join(n for n in names if n).lower()
    if "direct" in text:
        return "direct"
    if "regular" in text:
        return "regular"
    return None


def day_move(prev: list | None, cur: list | None) -> float | None:
    """% move of a holding's price between two valuation days, from [price, units] pairs. None when a price is
    missing or the units changed (a split, bonus, buy or sale would read as a false move)."""
    if not prev or not cur or prev[0] in (None, 0) or cur[0] is None:
        return None
    if abs(float(prev[1] or 0) - float(cur[1] or 0)) > 1e-6:
        return None
    return round((float(cur[0]) / float(prev[0]) - 1) * 100, 4)


async def valuation_pass(deps: Any, now: datetime, today: date) -> dict[str, Any]:
    from finresearch.portfolio.metrics import record_snapshot
    from finresearch.portfolio.report import snapshot
    from finresearch.portfolio.valuation import QuoteBatch, fetch_prices

    holdings = _detached_holdings()
    nav_rows: list = []

    async def rows() -> list:
        nonlocal nav_rows
        nav_rows = await (deps.pf_scheme_rows or _live_nav_rows)()
        return nav_rows

    # the ISIN lookups the portfolio page does too (#200): without them a BSE-only stock under a placeholder symbol
    # was quoted on NSE and a holding with only an ISIN had no live price in the daily pass
    if deps.pf_quote is not None:  # a test seam: its listings too (None = no ISIN lookups)
        prices = await fetch_prices(
            holdings, quote=deps.pf_quote, scheme_rows=rows, listings=deps.pf_listings
        )
    else:
        async with QuoteBatch() as batch:
            prices = await fetch_prices(holdings, quote=batch.quote, scheme_rows=rows,
                                        listings=deps.pf_listings or _live_listings)  # fmt: skip
    names = {r.code: r.name for r in nav_rows}
    with session_scope() as s:
        snap = snapshot(s, prices, today)
        by_asset = {r["label"]: r["value"] for r in snap["allocation"]["asset"]}
        value = snap["summary"]["value"] or 0.0
        if value:
            record_snapshot(s, today, value, snap["invested"], by_asset, snap["complete"])
        prev = cache.read(s, cache.VALUATION)
        hist: dict[str, dict[str, list]] = dict(prev.get("history") or {})
        cur: dict[str, Any] = {}
        unpriced, stale = [], []
        for r in snap["holdings"]:
            if r["closed"]:
                continue
            hid = str(r["id"])
            scheme_name = names.get(r["scheme_code"] or "")
            as_of = (r["price_as_of"] or "")[:10] or None
            if r["value"] is None:
                unpriced.append(
                    {"holding_id": r["id"], "name": r["name"], "why": r["price_error"] or "no price"}
                )
            elif (r["price_source"] and "statement" in r["price_source"]) or (
                    as_of and (today - date.fromisoformat(as_of)).days > STALE_PRICE_DAYS):  # fmt: skip
                stale.append(
                    {"holding_id": r["id"], "name": r["name"], "as_of": as_of, "source": r["price_source"]}
                )
            cur[hid] = {"name": r["name"], "account": r["account"], "asset_type": r["asset_type"], "units": r["units"],
                        "price": r["price"], "price_as_of": as_of, "price_source": r["price_source"],
                        "value": r["value"], "cost": r["cost"], "sector": r["sector"], "tax_class": r["tax_class"],
                        "category": r["category"], "scheme_code": r["scheme_code"], "nse_symbol": r["nse_symbol"],
                        "bse_code": r["bse_code"], "signal": r["signal"],
                        "plan": plan_of(r["name"], scheme_name) if r["asset_type"] == "mf" else None,
                        "weight_pct": round(r["value"] / value * 100, 4) if value and r["value"] else None}  # fmt: skip
        hist[today.isoformat()] = {hid: [c["price"], c["units"]] for hid, c in cur.items()}
        hist = dict(sorted(hist.items())[-cache.HISTORY_DAYS :])
        before = [d for d in hist if d < today.isoformat()]
        moves: dict[str, float] = {}
        if before:
            p = hist[before[-1]]
            for hid in cur:
                mv = day_move(p.get(hid), hist[today.isoformat()].get(hid))
                if mv is not None:
                    moves[hid] = mv
        # dividends recorded since the last pass (the first pass only sets the cursor)
        cursor = prev.get("dividend_cursor")
        top = s.scalar(select(func.max(PortfolioTxn.id)).where(PortfolioTxn.kind == "dividend")) or 0
        new_divs = []
        if cursor is not None and top > int(cursor):
            hname = {h.id: h.name for h in holdings}
            for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.kind == "dividend", PortfolioTxn.id > int(cursor))
                               .order_by(PortfolioTxn.id)):  # fmt: skip
                new_divs.append({"holding_id": t.holding_id, "name": hname.get(t.holding_id), "day": t.day.isoformat(),
                                 "amount": float(abs(t.amount or 0))})  # fmt: skip
        # the same day re-valued (the NAV pass) keeps the day's dividend list
        if prev.get("day") == today.isoformat() and not new_divs:
            new_divs = prev.get("new_dividends") or []
        val = {"day": today.isoformat(), "at": now.isoformat(), "value": value, "invested": snap["invested"],
               "complete": snap["complete"], "holdings": cur, "history": hist, "moves": moves,
               "moves_vs": before[-1] if before else None, "unpriced": unpriced, "stale": stale,
               "new_dividends": new_divs, "dividend_cursor": max(top, int(cursor or 0)),
               "by_asset": by_asset, "sectors": {x["label"]: x["value"] for x in snap["allocation"]["sector"]}}  # fmt: skip
        cache.write(s, cache.VALUATION, val)
    return {**val, "holdings": len(cur), "unpriced": len(unpriced)}


def _instruments(val: dict[str, Any]) -> dict[tuple[str, str], list[str]]:
    out: dict[tuple[str, str], list[str]] = {}
    with session_scope() as s:
        v = cache.read(s, cache.VALUATION)
    for hid, h in (v.get("holdings") or {}).items():
        sig = h.get("signal")
        if sig and sig.get("asset") and sig.get("instrument"):
            out.setdefault((sig["asset"], sig["instrument"]), []).append(hid)
    return out


async def _live_signal(asset: str, instrument: str):
    from finresearch.signals import get_provider

    provider = get_provider(asset)
    if provider is None:
        raise LookupError(f"no {asset} signal provider")
    return await provider(instrument, {"log": "0"})  # scheduled: never logged in the forecast ledger


async def signals_pass(deps: Any, now: datetime, today: date, val: dict[str, Any]) -> dict[str, Any]:
    """Each held instrument's signal (largest positions first, at most MAX_INSTRUMENTS)."""
    inst = _instruments(val)
    with session_scope() as s:
        v = cache.read(s, cache.VALUATION)
        prev = cache.read(s, cache.SIGNALS)
    weight = {k: sum((v["holdings"][h].get("weight_pct") or 0) for h in hids) for k, hids in inst.items()}
    order = sorted(inst, key=lambda k: -weight[k])[:MAX_INSTRUMENTS]
    before = prev.get("items") or {}
    items: dict[str, Any] = {}
    errors, changes = [], []
    fetch = deps.pf_signal or _live_signal
    for n, (asset, code) in enumerate(order):
        if n and deps.pf_spacing_s:
            await asyncio.sleep(deps.pf_spacing_s)
        key = f"{asset}:{code}"
        try:
            sig = await fetch(asset, code)
        except Exception as e:
            errors.append({"instrument": key, "error": f"{type(e).__name__}: {e}"[:200]})
            if key in before:  # keep yesterday's reading, marked as such
                items[key] = {**before[key], "stale": True}
            continue
        names = sorted({v["holdings"][h]["name"] for h in inst[(asset, code)]})
        items[key] = {"asset": asset, "instrument": code, "name": sig.name or names[0], "action": sig.action,
                      "score": round(float(sig.score), 1), "probability": sig.probability,
                      "event": sig.event, "horizon": sig.horizon, "method": sig.method,
                      "validation": sig.validation.status, "n": sig.validation.n, "sizing": sig.sizing,
                      "holding_ids": inst[(asset, code)], "weight_pct": round(weight[(asset, code)], 4),
                      "day": today.isoformat(), "call": getattr(sig, "call", None)}  # fmt: skip
        # an informational signal (the stock signal while no model has shown an edge, #193) changes by its factor
        # tilt, and the brief says so; a reading cached before it (no tilt) is not compared
        call = items[key]["call"] or {}
        informational = call.get("status") == "informational"
        new = call.get("tilt") if informational else sig.action
        old_item = before.get(key) or {}
        old = ((old_item.get("call") or {}).get("tilt") if informational else
               (None if (old_item.get("call") or {}).get("status") == "informational" else old_item.get("action")))  # fmt: skip
        if old and new and old != new and prev.get("day") != today.isoformat():
            changes.append({"instrument": key, "name": items[key]["name"], "from": old, "to": new,
                            **({"informational": True, "label": call.get("label")} if informational else {})})  # fmt: skip
    if prev.get("day") == today.isoformat() and not changes:
        changes = prev.get("changes") or []
    out = {"day": today.isoformat(), "at": now.isoformat(), "items": items, "changes": changes, "errors": errors,
           "skipped": max(0, len(inst) - len(order))}  # fmt: skip
    with session_scope() as s:
        cache.write(s, cache.SIGNALS, out)
    return {**out, "computed": len(items), "errors_n": len(errors), "changes_n": len(changes)}


async def _live_events(symbols: list[str], spacing: float):
    """{symbol: {"actions", "board_meetings", "results"}} over ONE NSE session (3 requests per stock)."""
    from finresearch.adapters.nse_equity import NseEquity

    out: dict[str, Any] = {}
    async with NseEquity() as eq:
        for n, sym in enumerate(symbols):
            if n and spacing:
                await asyncio.sleep(spacing)
            try:
                out[sym] = {"actions": await eq.corporate_actions(sym), "board_meetings": await eq.board_meetings(sym),
                            "results": await eq.results(sym)}  # fmt: skip
            except Exception as e:
                out[sym] = e
    return out


async def events_pass(deps: Any, now: datetime, today: date, val: dict[str, Any]) -> dict[str, Any]:
    """Corporate actions, board meetings and the latest results filing of each NSE stock holding."""
    with session_scope() as s:
        v = cache.read(s, cache.VALUATION)
    by_sym: dict[str, list[str]] = {}

    def nse_of(h: dict[str, Any]) -> str | None:
        # the snapshot's signal key resolves ISIN-only holdings through the ISIN map (#200); "BSE:..." is not on NSE
        key = (h.get("signal") or {}).get("instrument") or h.get("nse_symbol")
        return None if not key or key.startswith("BSE:") else key

    for hid, h in (v.get("holdings") or {}).items():
        if h["asset_type"] == "stock" and nse_of(h):
            by_sym.setdefault(nse_of(h), []).append(hid)
    bse_only = sorted({h["name"] for h in (v.get("holdings") or {}).values()
                       if h["asset_type"] == "stock" and not nse_of(h)})  # fmt: skip
    symbols = sorted(by_sym)[:MAX_INSTRUMENTS]
    if deps.pf_stock_events is not None:
        raw = {}
        for sym in symbols:
            try:
                raw[sym] = await deps.pf_stock_events(sym)
            except Exception as e:
                raw[sym] = e
    else:
        raw = await _live_events(symbols, deps.pf_spacing_s)
    stocks, errors = {}, []
    horizon = today + timedelta(days=UPCOMING_DAYS)
    for sym in symbols:
        got = raw.get(sym)
        if isinstance(got, Exception) or got is None:
            errors.append({"symbol": sym, "error": f"{type(got).__name__}: {got}"[:200]})
            continue
        name = v["holdings"][by_sym[sym][0]]["name"]
        actions = [{"ex_date": a.ex_date.isoformat(), "record_date": a.record_date.isoformat() if a.record_date else None,
                    "subject": " ".join(a.subject.split())[:200]}
                   for a in got["actions"] if a.ex_date and today - timedelta(days=7) <= a.ex_date <= horizon]  # fmt: skip
        meetings, seen = [], set()
        for b in got["board_meetings"]:
            if b.day and today <= b.day <= horizon and (b.day, b.results) not in seen:
                seen.add((b.day, b.results))
                meetings.append({"day": b.day.isoformat(), "purpose": b.purpose, "results": b.results,
                                 "description": b.description[:240], "url": b.attachment})  # fmt: skip
        filed = [r.filed_at for r in got["results"] if r.filed_at]
        last = max(filed) if filed else None
        stocks[sym] = {"name": name, "holding_ids": by_sym[sym], "actions": sorted(actions, key=lambda a: a["ex_date"]),
                       "board_meetings": sorted(meetings, key=lambda m: m["day"]),
                       "last_results": last.date().isoformat() if last else None,
                       "last_results_period": next((r.period_to.isoformat() for r in got["results"]
                                                    if r.filed_at == last and r.period_to), None)}  # fmt: skip
    out = {"day": today.isoformat(), "at": now.isoformat(), "stocks": stocks, "errors": errors,
           "bse_only": bse_only, "source": "NSE corporate actions, board meetings and financial-results filings"}  # fmt: skip
    with session_scope() as s:
        cache.write(s, cache.EVENTS, out)
    return {**out, "stocks": len(stocks), "errors_n": len(errors)}


async def _live_ter(month: date) -> dict:
    from finresearch.adapters.amfi import AmfiClient

    async with AmfiClient() as amfi:
        return await amfi.ter(month)


async def ter_pass(deps: Any, now: datetime, today: date, val: dict[str, Any]) -> dict[str, Any]:
    """Each held fund's TER (AMFI's monthly TER file; the previous month's when this month's is not out yet)."""
    from finresearch.adapters.amfi import ter_key

    with session_scope() as s:
        v = cache.read(s, cache.VALUATION)
        prev = cache.read(s, cache.TER)
    funds = {hid: h for hid, h in (v.get("holdings") or {}).items() if h["asset_type"] == "mf"}
    if not funds:
        return {"funds": 0}
    fetch = deps.pf_ter or _live_ter
    table = await fetch(today.replace(day=1))
    if not table:
        table = await fetch((today.replace(day=1) - timedelta(days=1)).replace(day=1))
    before = prev.get("funds") or {}
    out_f: dict[str, Any] = {}
    for hid, h in funds.items():
        name = h["name"]
        row = table.get(ter_key(name))
        if row is None:  # the TER file names schemes without the plan/option suffix
            base = ter_key(name.split(" - ")[0])
            row = table.get(base)
        if row is None:
            out_f[hid] = {"name": name, "ter": None, "why": "not found in AMFI's TER file by name"}
            continue
        plan = h.get("plan") or "regular"
        ter = row.direct if plan == "direct" else row.regular
        old = before.get(hid) or {}
        rec = {"name": name, "plan": plan, "ter": None if ter is None else float(ter), "day": row.day.isoformat(),
               "prev_ter": old.get("prev_ter"), "changed_on": old.get("changed_on")}  # fmt: skip
        if old.get("ter") is not None and rec["ter"] is not None and abs(old["ter"] - rec["ter"]) > 1e-9:
            rec["prev_ter"], rec["changed_on"] = old["ter"], today.isoformat()
        out_f[hid] = rec
    out = {"day": today.isoformat(), "at": now.isoformat(), "funds": out_f,
           "source": "AMFI TER file (https://www.amfiindia.com/ter-of-mf-schemes)"}  # fmt: skip
    with session_scope() as s:
        cache.write(s, cache.TER, out)
    return {"funds": len(out_f)}


def _d(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))
