"""The stock peer dataset (#178): every NIFTY 500 stock's industry levels and peer metrics (fincalc.peers), built by
the monitor's nightly job and stored on disk; the stock page reads the store and fetches only the company itself.

Universe: the NIFTY 500 constituents (NSE's nsearchives `ind_nifty500list.csv`, the same source family as the
backtest harvest's Nifty 50 list, evals.stock_harvest); about 95 % of NSE's free-float market cap per NSE Indices'
factsheet, so most industries have enough listed peers. A company outside it is compared with NIFTY 500 peers.

Per stock, per night: one quote (price, issued shares, NSE's four classification levels) and one price-history
request around the date a year ago (cached on disk: past bars never change). Results (quarterly XBRL, balance sheets)
and corporate actions are re-read only when the stored copy is older than RESULTS_MAX_AGE_DAYS; each filing's XBRL is
cached on disk for good (api.markets._xbrl), so a re-read costs the filing index plus any new filing. NSE's own rate
limit applies (adapters.http: 2 requests/s) plus PAUSE_S between stocks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch.fincalc import peers as P

log = logging.getLogger(__name__)

UNIVERSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
UNIVERSE_NAME = "NIFTY 500"
RESULTS_MAX_AGE_DAYS = 7  # results change once a quarter; a week-old copy misses at most a few days of new filings
QUARTERS = 8  # current TTM + the twelve months before (growth)
HISTORY_TTL_S = 30 * 86400  # bars a year old never change
PAUSE_S = 0.5  # between stocks, on top of the client's per-host limit
STORE_VERSION = 1
NOT_COMPUTED = "The peer dataset has not been built yet: the monitor builds it nightly (or run `finresearch stock peers`)."


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _levels(q: Any) -> dict[str, str | None]:
    return {"basic_industry": q.industry, "industry": getattr(q, "industry_info", None),
            "sector": getattr(q, "sector", None), "macro": getattr(q, "macro", None)}  # fmt: skip


async def fetch_inputs(eq: Any, symbol: str, today: date, old: dict[str, Any] | None = None) -> dict[str, Any]:
    """The raw inputs of one stock's metrics. `old` is its previous stored row: its results and corporate actions
    are reused while younger than RESULTS_MAX_AGE_DAYS."""
    from finresearch.api.markets import results_from_nse

    q = await eq.nse.quote(symbol)
    inputs: dict[str, Any] = {"quote": q}
    fresh = old and old.get("inputs") and old.get("results_read") and \
        date.fromisoformat(old["results_read"]) > today - timedelta(days=RESULTS_MAX_AGE_DAYS)  # fmt: skip
    if fresh:
        inputs.update({k: old["inputs"][k] for k in ("quarters", "sheets", "actions", "results_errors")})
        inputs["results_read"] = old["results_read"]
    else:
        sheets: dict[date, dict[str, Any]] = {}
        res = await results_from_nse(eq, symbol, QUARTERS, balance_sheets=sheets)
        inputs["quarters"] = res.get("quarters") or []
        inputs["sheets"] = [{**v, "end": k.isoformat(), "equity_owners": _f(v["equity_owners"]),
                             "total_equity": _f(v["total_equity"])} for k, v in sorted(sheets.items())]  # fmt: skip
        inputs["results_errors"] = (res.get("errors") or [])[:5]
        try:
            acts = await eq.corporate_actions(symbol)
            inputs["actions"] = [[a.ex_date.isoformat(), a.subject] for a in acts if a.ex_date]
        except Exception as e:  # recorded: the 1-year return then says it could not check splits
            inputs["actions"] = None
            inputs["results_errors"].append(f"corporate actions: {type(e).__name__}: {e}"[:200])
        inputs["results_read"] = today.isoformat()
    start = P.add_years(today, -1)
    bars = await eq.history(symbol, start - timedelta(days=10), start, cache_ttl=HISTORY_TTL_S)
    inputs["bars"] = [[b.day.isoformat(), _f(b.close)] for b in bars if b.close]
    return inputs


def compute_row(symbol: str, inputs: dict[str, Any], today: date) -> dict[str, Any]:
    """One stock's peer-table row: name, NSE classification, and every metric (fincalc.peers.Metric as JSON)."""
    from decimal import Decimal

    from finresearch.adapters.nse_equity import nse_source_url
    from finresearch.fincalc.price import price_view
    from finresearch.fincalc.valuation import market_cap
    from finresearch.verify.stock_baseline import quote_page

    q = inputs["quote"]
    page = quote_page(symbol)
    view = price_view(q, exchange="NSE")
    px = view.price if view else None
    asof = q.as_of.strftime("%d %b %Y %H:%M IST") if q.as_of else today.isoformat()
    m: dict[str, P.Metric] = {}
    m["price"] = P.Metric(px, asof, page, view.label if view else None) if px else P.missing("no price", asof, page)
    mcap = market_cap(q.issued_shares, px) if px and q.issued_shares else None
    m["market_cap"] = P.Metric(mcap, asof, page, "issued shares x price (NSE issuedSize)") if mcap else \
        P.missing("no issued-share count or price on the quote", asof, page)  # fmt: skip
    qs = [P.Quarter.from_row(r) for r in inputs.get("quarters") or [] if r.get("period_end")]
    sheets = [P.BalanceSheet(date.fromisoformat(s["end"]), None if s["equity_owners"] is None else Decimal(str(s["equity_owners"])),
                             None if s["total_equity"] is None else Decimal(str(s["total_equity"])), s["consolidated"],
                             s.get("xbrl")) for s in inputs.get("sheets") or []]  # fmt: skip
    eps, m["pe"] = P.pe_metric(px, qs, asof)
    win, _ = P.ttm_window(qs)
    basis = win[-1].consolidated if win else (qs[-1].consolidated if qs else None)
    m["pb"] = P.pb_metric(mcap, sheets, basis)
    m["roe"] = P.roe_metric(qs, sheets, basis)
    m["revenue_growth"] = P.growth_metric(qs, "revenue")
    m["pat_growth"] = P.growth_metric(qs, "profit")
    m["pat_margin"] = P.margin_metric(qs)
    hist_url = nse_source_url("history", symbol, start=P.add_years(today, -1) - timedelta(days=10),
                              end=P.add_years(today, -1))  # fmt: skip
    acts = inputs.get("actions")
    if acts is None:
        m["return_1y"] = P.missing("corporate actions could not be read, so splits/bonuses are unchecked", source=hist_url)
    else:
        bars = [(date.fromisoformat(d), c) for d, c in inputs.get("bars") or []]
        m["return_1y"] = P.return_1y(px, today, bars, [(date.fromisoformat(d), s) for d, s in acts], hist_url)
    return {"symbol": symbol, "name": q.company, **_levels(q), "price_as_of": asof,
            "results_read": inputs.get("results_read"), "eps_ttm": eps.json(),
            "metrics": {k: v.json() for k, v in m.items()},
            "inputs": {k: inputs.get(k) for k in ("quarters", "sheets", "actions", "results_errors", "bars")}}  # fmt: skip


# --------------------------------------------------------------------------- the nightly build and the store
def store_path() -> Path:
    from finresearch.config import get_settings

    return get_settings().state_dir / "stock_peers" / "latest.json"


def save(data: dict[str, Any]) -> Path:
    """Written atomically (temp file renamed over the old one): the API never reads half a file."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    os.replace(tmp, path)
    return path


_LOADED: dict[str, Any] = {}


def load() -> dict[str, Any] | None:
    """The stored dataset, re-read only when the file changed; None before the first build."""
    path = store_path()
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    if _LOADED.get("path") != str(path) or _LOADED.get("mtime") != mtime:
        _LOADED.update(path=str(path), mtime=mtime, data=json.loads(path.read_text()))
    return _LOADED["data"]


async def universe_symbols(eq: Any) -> list[str]:
    from finresearch.evals.stock_harvest import parse_constituents

    resp = await eq.nse.http.get(UNIVERSE_URL, headers={"Referer": "https://www.nseindia.com/"}, cache_ttl=86400,
                                 cache_if=lambda f: f.content.lstrip(b"\xef\xbb\xbf")[:12] == b"Company Name")  # fmt: skip
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status} for {UNIVERSE_URL}")
    rows = parse_constituents(resp.text)
    if len(rows) < 400:  # a block page or a truncated file must not shrink the universe
        raise RuntimeError(f"{UNIVERSE_URL} listed only {len(rows)} stocks")
    return [r["symbol"] for r in rows]


async def build(eq: Any, today: date, symbols: list[str] | None = None) -> dict[str, Any]:
    """Every universe stock's row; a stock that fails keeps its previous row (marked stale) or is listed in
    `failed`. Raises when most of the universe failed (the old store is then kept as it is)."""
    old = (load() or {}).get("rows", {})
    syms = symbols or await universe_symbols(eq)
    rows: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for sym in syms:
        try:
            inputs = await fetch_inputs(eq, sym, today, old.get(sym))
            rows[sym] = compute_row(sym, inputs, today)
        except Exception as e:
            failed[sym] = f"{type(e).__name__}: {e}"[:200]
            if sym in old:
                rows[sym] = {**old[sym], "stale": True}
        await asyncio.sleep(PAUSE_S)
    if len(failed) > len(syms) / 2:
        raise RuntimeError(f"{len(failed)} of {len(syms)} stocks failed, e.g. {next(iter(failed.items()))}")
    return {"version": STORE_VERSION, "as_of": today.isoformat(), "generated_at": datetime.now(UTC).isoformat(),
            "universe": UNIVERSE_NAME, "universe_source": UNIVERSE_URL, "rows": rows, "failed": failed}  # fmt: skip


async def refresh(today: date | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
    """Build and store the dataset; a short summary back."""
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.fincalc.dates import today_ist

    day = today or today_ist()
    async with NseEquity() as eq:
        data = await build(eq, day, symbols)
    save(data)
    return {"as_of": data["as_of"], "stocks": len(data["rows"]), "failed": len(data["failed"])}
