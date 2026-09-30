"""One-off, polite harvest of the price data the stock backtest needs (docs/dev/RESEARCH_ROADMAP.md §D.2, item 13).

What it fetches, once, from NSE's own public endpoints through the app's rate-limited client:
- the Nifty 50 constituent list (nsearchives `ind_nifty50list.csv`), saved with its fetch date under
  `evals/stock_backtest/` (committed: the universe is part of the result);
- each constituent's daily EQ-series history (NSE `getHistoricalTradeData`, ~70 trading days per request) and its
  corporate actions (for split/bonus adjustment: NSE's history is NOT adjusted - INFY closed 1434.25 on 3-Sep-2018
  and 737.15 on the 1:1 bonus ex-date with "previous close" 1434.25);
- the NIFTY 50 price index (NSE `historicalOR/indicesHistory`; NSE serves no total-return series there).

Raw data goes to `data/cache/stock_backtest/` (gitignored). A symbol already on disk is skipped, so an interrupted
harvest resumes. At about one request a second, 50 symbols x ~13 years is roughly 45 minutes.

    uv run python -m finresearch.evals.stock_harvest [--start 2014-01-01] [--end 2026-09-29] [--symbols A,B]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from finresearch.config import REPO_ROOT

CONSTITUENTS_URL = "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv"
DATA_DIR = REPO_ROOT / "data" / "cache" / "stock_backtest"
ARTEFACT_DIR = REPO_ROOT / "evals" / "stock_backtest"
INDEX = "NIFTY 50"
PAUSE_S = 1.0  # on top of the client's own per-host limit: be gentle, this runs once


def parse_constituents(text: str) -> list[dict[str, str]]:
    rows = csv.DictReader(io.StringIO(text))
    return [{"symbol": r["Symbol"].strip(), "name": r["Company Name"].strip(), "industry": r["Industry"].strip(),
             "isin": r["ISIN Code"].strip()} for r in rows if r.get("Symbol")]  # fmt: skip


async def _polite(eq: Any, make: Any) -> Any:
    """One request after a pause; one retry with a fresh NSE session after a longer pause."""
    await asyncio.sleep(PAUSE_S)
    try:
        return await make()
    except Exception:
        await asyncio.sleep(8)
        eq.nse._warmed = False
        return await make()


async def _walk_forward(eq: Any, start: date, end: date) -> tuple[list[Any], bool]:
    """The index endpoint answers a range with its EARLIEST ~70 days (the equity one with its latest), so walk
    forwards."""
    bars: dict[date, Any] = {}
    lo = start
    for _ in range(200):
        if lo > end:
            break
        hi = min(end, lo + timedelta(days=360))
        try:
            got = await _polite(eq, lambda lo=lo, hi=hi: eq.index_history(INDEX, lo, hi))
        except Exception:
            return [bars[d] for d in sorted(bars)], True
        got = [b for b in got if lo <= b.day <= hi]
        for b in got:
            bars[b.day] = b
        latest = max((b.day for b in got), default=None)
        lo = (
            hi + timedelta(days=1)
            if latest is None or latest >= hi - timedelta(days=4)
            else latest + timedelta(days=1)
        )
    return [bars[d] for d in sorted(bars)], False


async def harvest(start: date, end: date, symbols: list[str] | None = None, out: Path = DATA_DIR) -> None:
    from finresearch.adapters.nse_equity import NseEquity, walk_history

    (out / "prices").mkdir(parents=True, exist_ok=True)
    (out / "actions").mkdir(parents=True, exist_ok=True)
    ARTEFACT_DIR.mkdir(parents=True, exist_ok=True)
    async with NseEquity() as eq:
        existing = sorted(ARTEFACT_DIR.glob("nifty50_constituents_*.csv"))
        if existing:
            universe = parse_constituents(existing[-1].read_text())
        else:
            resp = await eq.nse.http.get(CONSTITUENTS_URL, headers={"Referer": "https://www.nseindia.com/"})
            text = resp.text
            universe = parse_constituents(text)
            fetched = resp.record.fetched_at.date().isoformat()
            (ARTEFACT_DIR / f"nifty50_constituents_{fetched}.csv").write_text(text)
        # explicit symbols may be former members (the point-in-time universe, evals.stock_universe)
        todo = list(symbols) if symbols else [u["symbol"] for u in universe]
        idx_path = out / "index_NIFTY50.csv"
        if not idx_path.exists():
            bars, partial = await _walk_forward(eq, start, end)
            with idx_path.open("w") as f:
                f.write("date,close\n")
                f.writelines(f"{b.day.isoformat()},{b.close}\n" for b in bars if b.close)
            print(f"index {INDEX}: {len(bars)} days{' (partial)' if partial else ''}", file=sys.stderr)
        for i, sym in enumerate(todo, 1):
            p = out / "prices" / f"{sym}.csv"
            if p.exists():
                continue
            try:
                acts = await _polite(eq, lambda sym=sym: eq.corporate_actions(sym))
                (out / "actions" / f"{sym}.json").write_text(json.dumps(
                    [{"subject": a.subject, "ex_date": a.ex_date.isoformat() if a.ex_date else None} for a in acts],
                    indent=0))  # fmt: skip
                bars, partial = await walk_history(
                    lambda lo, hi, sym=sym: _polite(eq, lambda: eq.history(sym, lo, hi)),
                    start,
                    end,
                    max_requests=90,
                )
            except Exception as e:  # keep going; the coverage table reports the gap
                print(f"[{i}/{len(todo)}] {sym}: FAILED {type(e).__name__}: {e}", file=sys.stderr)
                continue
            tmp = p.with_suffix(".tmp")
            with tmp.open("w") as f:
                f.write("date,open,high,low,close,prev_close,volume\n")
                for b in bars:
                    if b.close:
                        f.write(
                            f"{b.day.isoformat()},{b.open or ''},{b.high or ''},{b.low or ''},{b.close},"
                            f"{b.prev_close or ''},{b.volume or ''}\n"
                        )
            tmp.rename(p)
            first = bars[0].day if bars else None
            print(
                f"[{i}/{len(todo)}] {sym}: {len(bars)} days from {first}{' (partial)' if partial else ''}",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, default=date(2014, 1, 1))
    ap.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    ap.add_argument("--symbols", default="")
    a = ap.parse_args(argv)
    asyncio.run(harvest(a.start, a.end, [s for s in a.symbols.split(",") if s] or None))


if __name__ == "__main__":
    main()
