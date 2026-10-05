"""The stock peer dataset (#178): every NIFTY 500 stock's industry levels and peer metrics (fincalc.peers), built by
the monitor's nightly job and stored on disk; the stock page reads the store and fetches only the company itself.

Universe: the NIFTY 500 constituents (NSE's nsearchives `ind_nifty500list.csv`, the same source family as the
backtest harvest's Nifty 50 list, evals.stock_harvest); about 95 % of NSE's free-float market cap per NSE Indices'
factsheet, so most industries have enough listed peers. Extended (#199) by every held or watched NSE stock outside
it (read at run time through disclosures.store.tracked) and, for each of those, up to BROAD_PER_STOCK peers in its
own NSE industry level from the NIFTY Total Market list (NIFTY 500 + Microcap 250), nearest by market cap, so a held
small cap gets a peer table of companies its size. Any other company outside it is compared with the stored rows.

Per stock, per night: one quote (price, issued shares, NSE's four classification levels) and one price-history
request around the date a year ago (cached on disk: past bars never change). Results (quarterly XBRL, balance sheets)
and corporate actions are re-read only when the stored copy is older than RESULTS_MAX_AGE_DAYS; each filing's XBRL is
cached on disk for good (api.markets._xbrl), so a re-read costs the filing index plus any new filing. NSE's own rate
limit applies (adapters.http: 2 requests/s) plus PAUSE_S between stocks.

Extra requests for the extension, per night, with E held/watched stocks outside the NIFTY 500 (none when E = 0):
the Total Market list (1, cached a day); E x the per-stock cost above (quote + history, results weekly); quote-only
reads of same-sector candidates, at most BROAD_POOL_MAX per stock and each cached on disk for CLASS_TTL_DAYS (so
mostly zero after the first night); and at most BROAD_PER_STOCK chosen peers per stock at the per-stock cost (shared
peers are fetched once). E.g. 10 such stocks in 5 sectors: about 20 + ~150 candidate quotes on the first night
(about 0-10 later) + up to 150 peers x 2 = roughly 500 requests the first night, about 320 afterwards (~3 minutes at
NSE's 2 requests/s), on top of the ~1,000 for the NIFTY 500.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch.fincalc import peers as P

log = logging.getLogger(__name__)

UNIVERSE_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv"
UNIVERSE_NAME = "NIFTY 500"
RESULTS_MAX_AGE_DAYS = (
    7  # results change once a quarter; a week-old copy misses at most a few days of new filings
)
QUARTERS = 8  # current TTM + the twelve months before (growth)
HISTORY_TTL_S = 30 * 86400  # bars a year old never change
PAUSE_S = 0.5  # between stocks, on top of the client's per-host limit
STORE_VERSION = 1
# held/watched stocks outside the NIFTY 500 (#199): their industry peers come from NSE's NIFTY Total Market list (the
# NIFTY 500 plus the NIFTY Microcap 250, about 750 stocks, with NSE's sector in its Industry column); NSE's full
# equity list (EQUITY_L.csv) carries no classification, so classifying it would cost a quote per listed stock
BROAD_URL = "https://nsearchives.nseindia.com/content/indices/ind_niftytotalmarket_list.csv"
BROAD_NAME = "NIFTY Total Market"
# industry peers added per held/watched stock outside the base, nearest by market cap
BROAD_PER_STOCK = P.MAX_PEERS
# quote-only candidate reads per held/watched stock at most (a sector holds about 10-40 microcaps)
BROAD_POOL_MAX = 60
# a candidate's classification and market cap (used only to choose peers) are re-read monthly
CLASS_TTL_DAYS = 30
NOT_COMPUTED = "The peer dataset has not been built yet: the monitor builds it nightly (or run `finresearch stock peers`)."


@dataclass
class PeerSources:
    # () -> held and watched NSE symbols (tests: synthetic ones)
    tracked: Callable[[], list[str]] | None = None


SOURCES = PeerSources()


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def _levels(q: Any) -> dict[str, str | None]:
    return {"basic_industry": q.industry, "industry": getattr(q, "industry_info", None),
            "sector": getattr(q, "sector", None), "macro": getattr(q, "macro", None)}  # fmt: skip


async def fetch_inputs(
    eq: Any, symbol: str, today: date, old: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    m["price"] = (
        P.Metric(px, asof, page, view.label if view else None) if px else P.missing("no price", asof, page)
    )
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
        m["return_1y"] = P.missing(
            "corporate actions could not be read, so splits/bonuses are unchecked", source=hist_url
        )
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


# --------------------------------------------------------------------------- held/watched stocks outside the base
def tracked_symbols() -> list[str]:
    """NSE symbols of open holdings and active stock watches, read through disclosures.store.tracked (the code path
    the disclosure jobs use) at run time; BSE-only stocks are left out (no NSE quote or classification)."""
    if SOURCES.tracked is not None:
        return SOURCES.tracked()
    from finresearch.db import session_scope
    from finresearch.disclosures import store

    with session_scope() as s:
        return sorted(store.tracked(s).stocks)


async def broad_list(eq: Any) -> list[dict[str, str]]:
    """The NIFTY Total Market constituents with NSE's sector in the `industry` column (one request, cached a day)."""
    from finresearch.evals.stock_harvest import parse_constituents

    resp = await eq.nse.http.get(BROAD_URL, headers={"Referer": "https://www.nseindia.com/"}, cache_ttl=86400,
                                 cache_if=lambda f: f.content.lstrip(b"\xef\xbb\xbf")[:12] == b"Company Name")  # fmt: skip
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status} for {BROAD_URL}")
    rows = parse_constituents(resp.text)
    if len(rows) < 600:  # a block page or a truncated file
        raise RuntimeError(f"{BROAD_URL} listed only {len(rows)} stocks")
    return rows


def _class_path() -> Path:
    return store_path().with_name("classification.json")


async def industry_peers(
    eq: Any, today: date, owns: list[dict[str, Any]], exclude: set[str]
) -> dict[str, Any]:
    """For each held/watched row outside the base: candidates from the broader list whose sector (the list's
    `Industry` column, NSE's sector level) matches the row's sector or macro, at most BROAD_POOL_MAX of them; each
    candidate's NSE classification and market cap from a quote cached on disk for CLASS_TTL_DAYS; then the finest
    level shared (fincalc.peers.choose_level) and the BROAD_PER_STOCK nearest by market cap. Nothing is requested
    when `owns` is empty."""
    out: dict[str, Any] = {"symbols": [], "quotes": 0, "errors": []}
    if not owns:
        return out
    try:
        listed = await broad_list(eq)
    except Exception as e:  # recorded; the held stocks still get NIFTY 500 peers
        out["errors"].append(f"{BROAD_URL}: {type(e).__name__}: {e}"[:200])
        return out
    try:
        cache = json.loads(_class_path().read_text())
    except (FileNotFoundError, ValueError):
        cache = {}
    fresh_after = (today - timedelta(days=CLASS_TTL_DAYS)).isoformat()
    chosen: dict[str, None] = {}
    for own in owns:
        labels = {x.lower() for x in (own.get("sector"), own.get("macro")) if x}
        pool = [r["symbol"] for r in listed if r["industry"].lower() in labels and r["symbol"] not in exclude
                and r["symbol"] != own["symbol"]][:BROAD_POOL_MAX]  # fmt: skip
        cands = []
        for sym in pool:
            c = cache.get(sym)
            if not c or c.get("read", "") < fresh_after:
                try:
                    c = _classify(sym, await eq.nse.quote(sym), today)
                except Exception as e:
                    out["errors"].append(f"{sym}: {type(e).__name__}: {e}"[:200])
                    continue
                finally:
                    out["quotes"] += 1
                    await asyncio.sleep(PAUSE_S)
                cache[sym] = c
            cands.append(c)
        _, same = P.choose_level(own, cands, own["symbol"])
        for c in P.nearest_by_mcap(same, own["metrics"]["market_cap"]["value"], BROAD_PER_STOCK):
            chosen[c["symbol"]] = None
    path = _class_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cache, separators=(",", ":")))
    os.replace(tmp, path)
    out["symbols"] = list(chosen)
    return out


def _classify(symbol: str, q: Any, today: date) -> dict[str, Any]:
    from finresearch.fincalc.price import price_view
    from finresearch.fincalc.valuation import market_cap

    view = price_view(q, exchange="NSE")
    px = view.price if view else None
    mcap = market_cap(q.issued_shares, px) if px and q.issued_shares else None
    return {"symbol": symbol, **_levels(q), "market_cap": _f(mcap), "read": today.isoformat()}


async def _fetch_rows(eq: Any, syms: list[str], today: date, old: dict[str, Any], rows: dict[str, Any],
                     failed: dict[str, str], origin: str) -> None:  # fmt: skip
    """Each symbol's row into `rows` (tagged with `origin`); a failure keeps the previous row marked stale."""
    for sym in syms:
        try:
            inputs = await fetch_inputs(eq, sym, today, old.get(sym))
            rows[sym] = {**compute_row(sym, inputs, today), "origin": origin}
        except Exception as e:
            failed[sym] = f"{type(e).__name__}: {e}"[:200]
            if sym in old:
                rows[sym] = {**old[sym], "stale": True, "origin": origin}
        await asyncio.sleep(PAUSE_S)


async def build(eq: Any, today: date, symbols: list[str] | None = None,
                tracked: list[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """Every universe stock's row; a stock that fails keeps its previous row (marked stale) or is listed in
    `failed`. Raises when most of the base universe failed (the old store is then kept as it is).

    `tracked`: held and watched NSE symbols. Those outside the base get their own rows, and up to BROAD_PER_STOCK
    industry peers each from the broader list (`industry_peers`). Default: read through disclosures.store.tracked
    when the base is the NIFTY 500 (no `symbols`), none for an explicit symbol list."""
    old = (load() or {}).get("rows", {})
    syms = symbols or await universe_symbols(eq)
    rows: dict[str, Any] = {}
    failed: dict[str, str] = {}
    await _fetch_rows(eq, syms, today, old, rows, failed, "base")
    if len(failed) > len(syms) / 2:
        raise RuntimeError(f"{len(failed)} of {len(syms)} stocks failed, e.g. {next(iter(failed.items()))}")
    tracked_error = None
    if tracked is None and symbols is None:
        try:
            tracked = tracked_symbols()
        except Exception as e:  # recorded, not hidden: the base still builds, the extras wait for the next night
            tracked, tracked_error = [], f"{type(e).__name__}: {e}"[:200]
            log.warning("stock peers: could not read held/watched symbols: %s", tracked_error)
    base = set(syms)
    extras = [x for x in dict.fromkeys(t.strip().upper() for t in tracked or []) if x and x not in base]
    await _fetch_rows(eq, extras, today, old, rows, failed, "tracked")
    broad = await industry_peers(eq, today, [rows[x] for x in extras if x in rows and not rows[x].get("stale")],
                                 exclude=set(rows))  # fmt: skip
    await _fetch_rows(eq, broad["symbols"], today, old, rows, failed, "industry_peer")
    return {"version": STORE_VERSION, "as_of": today.isoformat(), "generated_at": datetime.now(UTC).isoformat(),
            "universe": UNIVERSE_NAME, "universe_source": UNIVERSE_URL,
            "extended": {"outside_base": len(extras), "industry_peers": len(broad["symbols"]),
                         "source": BROAD_URL if extras else None, "quotes": broad["quotes"],
                         "errors": broad["errors"][:10], "tracked_error": tracked_error},
            "rows": rows, "failed": failed}  # fmt: skip


async def refresh(today: date | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
    """Build and store the dataset; a short summary back."""
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.fincalc.dates import today_ist

    day = today or today_ist()
    async with NseEquity() as eq:
        data = await build(eq, day, symbols)
    save(data)
    return {"as_of": data["as_of"], "stocks": len(data["rows"]), "failed": len(data["failed"])}


# --------------------------------------------------------------------------- one stock's peer table
CAVEATS = [
    "Peers share the company's NSE classification at the finest level with at least "
    f"{P.MIN_PEERS} listed peers in the dataset; the {P.MAX_PEERS} nearest by market cap (log scale) are shown.",
    f"The dataset: the {UNIVERSE_NAME}, plus held and watched stocks outside it and up to {BROAD_PER_STOCK} "
    f"same-industry peers each from the {BROAD_NAME} list.",
    "Percentiles say where the company sits among these peers (0 = lowest, 100 = highest). For P/E and P/B a high "
    "percentile means priced higher than peers, not better; no metric here is labelled better or worse.",
    "TTM = the latest four consecutive quarters on one basis (consolidated when filed). EPS is summed as filed.",
    "Peers' prices are from the nightly build; the company's own row is fetched live.",
]


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in ("inputs", "origin")}  # origin would say what is held


def peer_table(data: dict[str, Any], own: dict[str, Any], symbol: str) -> dict[str, Any]:
    """The company's row, its peers' rows, the level used and the per-metric summary."""
    rows = [r for r in data["rows"].values() if r.get("symbol")]
    level, candidates = P.choose_level(own, rows, symbol)
    own_mcap = own["metrics"]["market_cap"]["value"]
    peers = P.nearest_by_mcap([{**r, "market_cap": r["metrics"]["market_cap"]["value"]} for r in candidates],
                              own_mcap)  # fmt: skip
    peers = [_public({k: v for k, v in p.items() if k != "market_cap"}) for p in peers]
    summary = P.summarise(own["metrics"], [p["metrics"] for p in peers])
    return {"status": "ok" if level else "no_peers", "symbol": symbol, "as_of": data["as_of"],
            "generated_at": data["generated_at"], "universe": data["universe"],
            "universe_source": data["universe_source"],
            "level": level, "level_label": P.LEVEL_LABELS.get(level or ""), "industry": own.get(level) if level else None,
            "candidates": len(candidates), "company": _public(own), "peers": peers, "summary": summary,
            "metrics": list(P.METRICS), "valuation_metrics": list(P.VALUATION), "caveats": CAVEATS,
            "universe_note": f"plus held and watched stocks outside it and their industry peers from the {BROAD_NAME}"
                             if data.get("extended", {}).get("outside_base") else None,
            "message": None if level else "No other stock in the peer dataset shares any of this company's NSE "
                                          "classification levels."}  # fmt: skip


async def live_row(eq: Any, symbol: str, today: date) -> dict[str, Any]:
    """The company's own row, fetched now (its stored row, if any, supplies results younger than a week)."""
    old = ((load() or {}).get("rows") or {}).get(symbol)
    return compute_row(symbol, await fetch_inputs(eq, symbol, today, old), today)
