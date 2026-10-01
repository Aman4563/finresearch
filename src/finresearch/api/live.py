"""Live data for the pages that refresh while the market is open: the session status (NSE holiday aware), a fast stock
quote, and a watched IPO's live subscription book.

The pages poll these only in session: equities 09:15–15:30 IST on NSE trading days (BSE equities keep the same
hours and trading holidays, so one session serves both exchanges), IPO bidding 10:00–17:00 IST on
the issue's bidding days (the exchanges keep publishing the book until the close). Outside those hours they return
the last figures with `live: false`, so the page can say the market is closed and when it last updated.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, date, datetime, time
from typing import Any

from fastapi import FastAPI, HTTPException

from finresearch.fincalc.dates import is_business_day, ist_datetime, next_business_day, to_ist

EQUITY_HOURS = (time(9, 15), time(15, 30))
BIDDING_HOURS = (time(10, 0), time(17, 0))
QUOTE_TTL_LIVE_S, QUOTE_TTL_CLOSED_S = 15, 300
BOOK_TTL_S = 60


def _holidays() -> dict[date, str]:
    from finresearch.adapters.nse_holidays import load_holidays

    return load_holidays("trading")


def market_status(now: datetime, holidays: dict[date, str]) -> dict[str, Any]:
    """Whether NSE equities trade now and whether IPO bidding hours are on, with the next equity open."""
    t = to_ist(now)
    d, clock = t.date(), t.time()
    trading_day = is_business_day(d, holidays)
    equity_open = trading_day and EQUITY_HOURS[0] <= clock < EQUITY_HOURS[1]
    bidding_open = trading_day and BIDDING_HOURS[0] <= clock < BIDDING_HOURS[1]
    nxt = d if trading_day and clock < EQUITY_HOURS[0] else next_business_day(d, holidays)
    # NSE lists a dozen or more trading holidays every year: none for this year means the list is not loaded, and
    # weekdays that are holidays would read as trading days (the monitor fetches it; `finresearch holidays` does too)
    known = any(h.year == d.year for h in holidays)
    return {
        "now": t.isoformat(),
        "trading_day": trading_day,
        "holiday": holidays.get(d),
        "holidays_known": known,
        "warning": None if known else f"NSE's {d.year} holiday list is not loaded: exchange holidays are not "
                                      "excluded, so a holiday can show as a trading day",
        "equity": {"open": equity_open, "hours": "09:15–15:30 IST",
                   "next_open": None if equity_open else ist_datetime(nxt, 9, 15).isoformat(),
                   "closes_at": ist_datetime(d, 15, 30).isoformat() if equity_open else None},
        "ipo_bidding": {"open": bidding_open, "hours": "10:00–17:00 IST"},
    }  # fmt: skip


def bidding_now(
    now: datetime, open_date: date | None, close_date: date | None, holidays: dict[date, str]
) -> bool:
    """IPO bidding hours on one of the issue's bidding days."""
    d = to_ist(now).date()
    return bool(open_date and close_date and open_date <= d <= close_date
                and market_status(now, holidays)["ipo_bidding"]["open"])  # fmt: skip


def add_live_routes(app: FastAPI, *, monitor_deps=None, clock=None) -> None:
    """`monitor_deps` (monitor.jobs.Deps) and `clock` (() -> aware datetime) are test seams."""
    from finresearch.adapters.bse_equity import scrip_code_of
    from finresearch.api.markets import MarketSources, TtlCache, quote_json

    cache = TtlCache()
    now = clock or (lambda: datetime.now(UTC))

    def sources() -> MarketSources:
        s = getattr(app.state, "markets", None)
        if s is None:
            s = app.state.markets = MarketSources(listings=getattr(app.state, "listings", None))
        return s

    @app.get("/api/market/status")
    async def status() -> dict[str, Any]:
        return market_status(now(), await asyncio.to_thread(_holidays))

    @app.get("/api/stocks/{symbol}/quote")
    async def stock_quote(symbol: str, exchange: str | None = None) -> dict[str, Any]:
        """Just the quote, for the stock page to refresh every few seconds in session (cached 15 s while the
        market is open, 5 minutes when it is closed). NSE by default; "BSE:<code>" or `exchange=BSE` reads BSE,
        whose equity session has the same hours (09:15-15:30 IST) and trading holidays."""
        from finresearch.api.markets import resolve_stock, stock_source_url

        if scrip_code_of(symbol) is None and exchange is None:
            sym = symbol.strip().upper()
            if not sym or len(sym) > 20:
                raise HTTPException(422, f"{symbol!r} is not an NSE symbol")
            inst = None
        else:
            inst = await resolve_stock(symbol, exchange, sources().listings)
            sym = inst.id
        ex = inst.exchange if inst else "NSE"
        st = market_status(now(), await asyncio.to_thread(_holidays))
        live = st["equity"]["open"]

        async def make() -> dict[str, Any]:
            q = await (sources().get_quote(sym, "BSE") if ex == "BSE" else sources().get_quote(sym))
            source = (
                stock_source_url(inst, q)
                if inst
                else f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"
            )
            from finresearch.api.markets import stock_page_url

            page = (
                stock_page_url(inst, q)
                if inst
                else f"https://www.nseindia.com/get-quotes/equity?symbol={sym}"
            )
            div_today = action_today = None
            if (
                ex == "BSE"
            ):  # BSE publishes no adjusted previous close: today's ex-date actions adjust the change
                from finresearch.api.markets import ex_today

                async def actions() -> list:
                    async with sources().open_equity("BSE") as eq:
                        return await eq.corporate_actions(sym)

                # on failure the change falls back to the unadjusted previous close, as BSE itself shows it
                with contextlib.suppress(Exception):
                    div_today, action_today = ex_today(await cache.get(("actions", sym), 3600, actions), q)
            return {"quote": quote_json(q, sym, now=now(), dividend_today=div_today, action_today=action_today),
                    "fetched_at": datetime.now(UTC).isoformat(), "source": source,
                    "quote_page": page, "exchange": ex}  # fmt: skip

        key = ("quote", sym, live) if ex == "NSE" else ("quote", "BSE", sym, live)
        try:
            out = await cache.get(key, QUOTE_TTL_LIVE_S if live else QUOTE_TTL_CLOSED_S, make)
        except Exception as e:  # the exchange refused or is down: say so, the page keeps its last figures
            raise HTTPException(502, f"{ex} quote for {sym} failed: {e}") from e
        return {**out, "live": live, "market": st["equity"]}

    @app.get("/api/watches/{watch_id}/live")
    async def watch_live(watch_id: int) -> dict[str, Any]:
        """A watched IPO's subscription book now, while bidding is on. Each new exchange timestamp is recorded as a
        snapshot, so the chart fills in between the scheduled checks. Outside bidding hours: `live: false`."""
        from finresearch.db import session_scope
        from finresearch.db.models import Watch
        from finresearch.monitor.jobs import Deps, NotYet, fetch_book

        holidays = await asyncio.to_thread(_holidays)
        t = now()
        with session_scope() as s:
            w = s.get(Watch, watch_id)
            if w is None:
                raise HTTPException(404, f"unknown watch {watch_id}")
            kind, open_d, close_d = w.kind, w.open_date, w.close_date
        if kind != "ipo" or not bidding_now(t, open_d, close_d, holidays):
            why = (
                "not an IPO watch"
                if kind != "ipo"
                else "outside bidding hours (10:00–17:00 IST on bidding days)"
            )
            return {"live": False, "reason": why}

        async def make() -> dict[str, Any]:
            deps = monitor_deps or Deps.live()
            with session_scope() as s:
                w = s.get(Watch, watch_id)
                _, snap, total, source = await fetch_book(s, w, deps, t)
                return {"as_of": snap.as_of.isoformat() if snap.as_of else None, "source": source,
                        "total_times": str(total),
                        "categories": [c.model_dump(mode="json") for c in snap.categories],
                        "fetched_at": datetime.now(UTC).isoformat()}  # fmt: skip

        try:
            book = await cache.get(("book", watch_id), BOOK_TTL_S, make)
        except NotYet as e:
            return {"live": True, "book": None, "reason": str(e)}
        except Exception as e:
            raise HTTPException(502, f"live subscription fetch failed: {e}") from e
        return {"live": True, "book": book}


def radar_ttl(now: datetime, default_s: int) -> int:
    """The IPO list refreshes every minute during bidding hours (subscription moves), else every `default_s`."""
    try:
        holidays = _holidays()
    except Exception:
        holidays = {}
    return 60 if market_status(now, holidays)["ipo_bidding"]["open"] else default_s
