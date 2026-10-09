"""Intraday charts: today's NSE 1-minute price series as 1m/5m/15m/30m/1h candles, for a stock or an index, plus
archived earlier sessions for multi-day (5D) views.

- Source: the NSE quote page's own 1D chart (adapters.nse_intraday): one price sample a minute, no volume. For the
  BSE view of a stock ("BSE:<scrip code>") BSE's 1D chart (adapters.bse_intraday), which adds BSE's own volume. Candles
  are built from those 1-minute samples (fincalc.candles), so highs and lows can be slightly understated.
- Freshness: every response carries the newest sample's time (`as_of`, IST), when the app fetched it and the measured
  lag. The series gains one point a minute, so it is cached 30 s while NSE is open and 10 minutes otherwise.
- Multi-day: NSE publishes only the current session; earlier sessions come from the app's own archive
  (monitor.intraday), which starts filling the first day a symbol is viewed or watched. Fewer archived days than
  asked is not an error: the response says how many exist.
"""

from __future__ import annotations

import asyncio
import time as _time
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from finresearch.fincalc.candles import INTERVALS, aggregate

QUOTE_TTL_LIVE_S, QUOTE_TTL_CLOSED_S = 30, 600
STORE_EVERY_S = 300  # archive an in-session series at most every 5 minutes per symbol
MAX_DAYS = 5
SOURCE_LABEL = "NSE quote-page chart, 1-minute price samples"
BSE_SOURCE_LABEL = "BSE quote-page chart, 1-minute price samples with BSE volume"
NOTE_BSE = ("Candles are built from BSE's 1-minute price samples, so highs and lows can be slightly understated; volume "
            "is BSE's own (NSE volume is not included).")  # fmt: skip
NOTE_CANDLES = ("Candles are built from 1-minute price samples (NSE publishes no intraday volume or tick high/low "
                "on its public pages), so highs and lows can be slightly understated.")  # fmt: skip


def _num(v) -> float | None:
    return None if v is None else round(float(v), 4)


def post_close(ticks) -> Any:
    """The official close the exchange's chart publishes after the session: the last sample stamped after 15:30:59 IST
    (the candles ignore it), or None while the session is on or when the chart has none."""
    from datetime import time

    edge = time(15, 30, 59)
    after = [t for t in ticks or [] if t.at.time() > edge]
    return after[-1].price if after else None


def candles_json(candles) -> list[dict[str, Any]]:
    return [{"t": c.start.isoformat(), "o": _num(c.open), "h": _num(c.high), "l": _num(c.low), "c": _num(c.close),
             "n": c.samples, **({"v": _num(c.volume)} if c.volume is not None else {}),
             **({"partial": True} if c.partial else {})} for c in candles]  # fmt: skip


def add_intraday_routes(app: FastAPI, *, fetch=None, clock=None) -> None:
    """`fetch` ((kind, symbol) -> IntradaySeries; or `app.state.intraday_fetch`) and `clock` (() -> aware datetime)
    are test seams."""
    from finresearch.adapters.nse_intraday import INDICES
    from finresearch.api.live import _holidays, market_status
    from finresearch.api.markets import SYMBOL_RE, TtlCache

    cache = TtlCache()
    now = clock or (lambda: datetime.now(UTC))
    stored_at: dict[tuple[str, str], float] = {}

    async def get_series(kind: str, sym: str):
        f = fetch or getattr(app.state, "intraday_fetch", None)
        if f is not None:
            return await f(kind, sym)
        from finresearch.monitor.intraday import live_fetch

        return await live_fetch(kind, sym)

    def archive(series, t: datetime) -> None:
        key = (series.kind, series.symbol)
        if _time.time() - stored_at.get(key, 0.0) < STORE_EVERY_S:
            return
        try:
            from finresearch.db import session_scope
            from finresearch.monitor.intraday import store

            with session_scope() as s:
                store(s, series, t)
            stored_at[key] = _time.time()
        except Exception:  # no database / table not migrated yet: the chart still works for today
            stored_at[key] = _time.time()

    def archived(kind: str, sym: str, n: int, before) -> list[Any]:
        if n <= 0:
            return []
        try:
            from finresearch.db import session_scope
            from finresearch.monitor.intraday import from_json, load_days

            with session_scope() as s:
                return [(r.day, r.prev_close, from_json(r.ticks), r.complete)
                        for r in load_days(s, kind, sym, n, before=before)]  # fmt: skip
        except Exception:
            return []

    async def _links(kind: str, sym: str) -> tuple[str | None, str]:
        """(the exchange's human page for the stock or index, the exact data request behind the series). A BSE
        scrip's page comes from the scrip master; None when it has none (the request is still given)."""
        from finresearch.adapters.bse_equity import scrip_code_of
        from finresearch.adapters.nse_intraday import equity_chart_url, equity_page, index_page

        code = scrip_code_of(sym) if kind == "equity" else None
        if code is not None:
            from finresearch.adapters.bse_intraday import graph_url
            from finresearch.api.markets import bse_stock_page

            m = getattr(app.state, "markets", None)
            listings = getattr(m, "listings", None) or getattr(app.state, "listings", None)
            return await bse_stock_page(code, listings), graph_url(code)
        if kind == "index":
            from urllib.parse import urlencode

            from finresearch.adapters.nse import NSE_BASE
            from finresearch.adapters.nse_intraday import INDEX_PATH

            return index_page(sym), f"{NSE_BASE}{INDEX_PATH}?" + urlencode(
                {"functionName": "getIndexChart", "index": sym, "flag": "1D"})  # fmt: skip
        return equity_page(sym), equity_chart_url(sym)

    async def serve(kind: str, sym: str, interval: str, days: int) -> dict[str, Any]:
        if interval not in INTERVALS:
            raise HTTPException(422, f"interval must be one of {', '.join(INTERVALS)}")
        ex = "BSE" if sym.startswith("BSE:") else "NSE"
        t = now()
        st = market_status(t, await asyncio.to_thread(_holidays))
        live = st["equity"]["open"]

        async def make() -> dict[str, Any]:
            series = await get_series(kind, sym)
            return {"series": series, "fetched_at": datetime.now(UTC)}

        error = None
        try:
            got = await cache.get(
                ("intraday", kind, sym, live), QUOTE_TTL_LIVE_S if live else QUOTE_TTL_CLOSED_S, make
            )
        except Exception as e:  # NSE refused: fall back to the archive when there is one
            got, error = None, f"{type(e).__name__}: {e}"[:300]
        series = got["series"] if got else None
        if series is not None and series.ticks:
            await asyncio.to_thread(archive, series, t)
        today_day = series.day if series is not None and series.ticks else None
        past = await asyncio.to_thread(archived, kind, sym, days - (1 if today_day else 0), today_day)
        from finresearch.monitor.intraday import is_complete

        today_row = (
            [(today_day, series.prev_close, series.ticks, is_complete(today_day, t))] if today_day else []
        )
        sessions = [*past, *today_row]
        if not sessions:
            if error:
                raise HTTPException(502, f"{ex} intraday series for {sym} failed: {error}")
            raise HTTPException(
                404, f"{ex} has no intraday series for {sym} (not traded today, or not listed there)"
            )
        ticks = [tk for _, _, ts, _ in sessions for tk in ts]
        candles = aggregate(ticks, interval, live=live and today_day is not None)
        _, prev_close, last_ticks, _ = sessions[-1]
        last_traded = candles[-1].close if candles else None
        # after the session NSE's chart carries a point after 15:30 holding the official close (a stock's closePrice,
        # an index's closing value). Candles stop at 15:30, so without it the chart's "last" would be the last trade
        # (TMCV 30-Sep-2026: 420.00 vs the official 421.65). BSE's chart does not: after 15:30 it repeats the last
        # trade (TENNIND 30-Sep-2026: 506.50 vs BSE's close 508.15), so a BSE series ends on its last trade.
        close_mark = post_close(last_ticks) if not live and ex == "NSE" else None
        last = close_mark if close_mark is not None else last_traded
        last_kind = "official_close" if close_mark is not None else "last_traded"
        as_of = last_ticks[-1].at if last_ticks else None
        fetched = got["fetched_at"] if got else None
        delay = round((fetched - as_of).total_seconds()) if fetched and as_of and live else None
        live_source = None
        if live and today_day is not None:
            # the latest value from the user's Groww API when connected (#267); the candles stay the exchange's samples
            from finresearch.adapters import groww_market

            gv = await groww_market.MARKET.live_value(kind, sym)
            if gv is not None:
                from decimal import Decimal

                ref = prev_close if prev_close is not None else last_traded
                last = last_traded = Decimal(str(gv[0])) if isinstance(ref, Decimal) else gv[0]
                last_kind, live_source = "last_traded", groww_market.SOURCE
                fetched = gv[1]
                as_of = gv[1]
                delay = 0
        change = (last - prev_close) if last is not None and prev_close else None
        page, request = await _links(kind, sym)
        has_volume = any(c.volume is not None for c in candles)
        notes = [NOTE_BSE if ex == "BSE" else NOTE_CANDLES]
        if days > len(sessions):
            notes.append(f"{len(sessions)} of {days} sessions available: {ex} publishes only the current session at "
                         "1-minute resolution, and the app archives each day from the first day a symbol is viewed or "
                         "watched.")  # fmt: skip
        if error:
            notes.append(f"{ex} did not answer just now ({error}); showing archived sessions.")
        if live_source:
            notes.append(
                f"Latest value from your {live_source} API (LTP); the candles are {ex}'s 1-minute samples."
            )
        return {
            "symbol": sym, "kind": kind, "interval": interval, "days_requested": days,
            "sessions": [{"day": d.isoformat(), "prev_close": _num(pc), "samples": len(ts), "complete": c}
                         for d, pc, ts, c in sessions],
            "candles": candles_json(candles),
            "prev_close": _num(prev_close), "last": _num(last), "last_kind": last_kind,
            "last_label": "Close (official)" if last_kind == "official_close" else "Last traded",
            "last_traded": _num(last_traded), "official_close": _num(close_mark),
            "change": _num(change), "change_pct": _num(change / prev_close * 100) if change is not None else None,
            "as_of": as_of.isoformat() if as_of else None,
            "fetched_at": fetched.isoformat() if fetched else None,
            "delay_s": delay, "live": live, "market": st["equity"],
            "refresh_s": QUOTE_TTL_LIVE_S, "source": page or request, "exchange": ex,
            "quote_page": page, "data_request": request,
            "source_label": BSE_SOURCE_LABEL if ex == "BSE" else SOURCE_LABEL, "last_source": live_source or ex,
            "has_volume": has_volume, "notes": notes,
        }  # fmt: skip

    @app.get("/api/stocks/{symbol}/intraday")
    async def stock_intraday(symbol: str, interval: str = Query("5m"),
                             days: int = Query(1, ge=1, le=MAX_DAYS)) -> dict[str, Any]:  # fmt: skip
        """Intraday candles for a stock: an NSE symbol, or "BSE:<scrip code>" for BSE's series (the stock page's BSE
        view). Today's session (or the last one when closed) and, for days > 1, the archived sessions before it."""
        from finresearch.adapters.bse_equity import scrip_code_of

        sym = symbol.strip().upper()
        if scrip_code_of(sym) is None and not SYMBOL_RE.match(sym):
            raise HTTPException(422, f"{symbol!r} is not an NSE symbol or a BSE:<scrip code> key")
        return await serve("equity", sym, interval, days)

    @app.get("/api/indices")
    def indices() -> dict[str, Any]:
        return {"indices": list(INDICES)}

    @app.get("/api/indices/{name}/intraday")
    async def index_intraday(name: str, interval: str = Query("5m"),
                             days: int = Query(1, ge=1, le=MAX_DAYS)) -> dict[str, Any]:  # fmt: skip
        """Intraday candles for an NSE index (NIFTY 50, NIFTY BANK, ...)."""
        nm = " ".join(name.strip().upper().split())
        if nm not in INDICES:
            raise HTTPException(404, f"unknown index {name!r}; one of {', '.join(INDICES)}")
        return await serve("index", nm, interval, days)
