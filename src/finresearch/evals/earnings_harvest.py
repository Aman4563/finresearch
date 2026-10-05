"""Polite, resumable harvest for the earnings-surprise experiment (evals/experiments/earnings_surprise/PREREG.md).

Per stock (the point-in-time Nifty 50 members since Apr-2021, evals.stock_universe), through the app's rate-limited
NSE client plus PAUSE_S between requests:
- NSE's Financial Results index and Integrated Filing (Financials) index, one request each: every quarterly filing
  with its broadcast timestamp (IST) and XBRL link;
- the XBRL of the first-reported filing per quarter end on the stock's basis (cached on disk for good through
  api.markets._xbrl: a filing's URL carries its id and never changes, and the stock peer job shares that cache);
- corporate actions (split/bonus ex-dates) and the daily EQ-series closes from PRICE_START.
Plus NIFTYBEES (the benchmark) and the NIFTY 50 price index (kept, unused: ADDENDUM 2). Raw data goes to data/cache/earnings_surprise/ (gitignored); a stock already on disk
is skipped, so an interrupted harvest resumes where it stopped.

    uv run python -m finresearch.evals.earnings_harvest [--symbols A,B] [--max-minutes 60]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch.config import REPO_ROOT

DATA_DIR = REPO_ROOT / "data" / "cache" / "earnings_surprise"
FIRST_QUARTER = date(
    2018, 6, 30
)  # NSE results XBRL starts around the Sep-2018 quarter; older filings are HTML
PRICE_START = date(2021, 6, 1)  # the first SUE is the Sep-2021 quarter (announced from Oct-2021)
MEMBERS_SINCE = date(2021, 4, 1)
PAUSE_S = 0.5  # on top of the client's own 2 requests/s per host: about one request a second
MARKET = "NIFTYBEES"  # the benchmark (ADDENDUM 2: the NIFTY 50 index series has months of holes)
SEED = 181  # stocks are fetched in a fixed shuffled order, so a harvest cut short is a random subset


def _ts(value: str | None) -> tuple[datetime | None, bool]:
    """An NSE timestamp in IST and whether it carried a time of day."""
    from finresearch.adapters.nse import parse_nse_timestamp

    v = (value or "").strip()
    if not v or v == "-":
        return None, False
    return parse_nse_timestamp(v), ":" in v


def filing_rows(results: list[dict[str, Any]], integrated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Both NSE indexes as one list of {end, consolidated, at, has_time, xbrl, revised, source}. Integrated filings
    keep their ORIGINAL broadcast time (`broadcast_Date`); a revision is marked, never dated as the original."""
    from finresearch.adapters.nse import parse_nse_date
    from finresearch.adapters.nse_equity import INTEGRATED_FINANCIALS, _archive_link, _upper_date

    out: list[dict[str, Any]] = []
    for r in results:
        end = parse_nse_date(r.get("toDate"))
        at, has_time = _ts(r.get("broadCastDate") or r.get("filingDate"))
        x = r.get("xbrl") or ""
        out.append({"end": end, "consolidated": (r.get("consolidated") or "").lower() == "consolidated", "at": at,
                    "has_time": has_time, "xbrl": x if x.endswith(".xml") else None, "revised": False,
                    "source": "nse_financial_results"})  # fmt: skip
    for r in integrated:
        if (r.get("type") or "") != INTEGRATED_FINANCIALS:
            continue
        at, has_time = _ts(r.get("broadcast_Date"))
        nature = (r.get("consolidated") or "").lower()
        out.append({"end": _upper_date(r.get("qe_Date")), "consolidated": nature.startswith("consolidated"),
                    "at": at, "has_time": has_time, "xbrl": _archive_link(r.get("xbrl")),
                    "revised": (r.get("type_Sub") or "").lower().startswith("revis"),
                    "source": "nse_integrated_filing"})  # fmt: skip
    return [f for f in out if f["end"] and f["at"]]


def choose_basis(rows: list[dict[str, Any]]) -> bool:
    """Consolidated when consolidated XBRL filings exist for the majority of quarter ends, else standalone."""
    ends = {f["end"] for f in rows if f["xbrl"]}
    cons = {f["end"] for f in rows if f["xbrl"] and f["consolidated"]}
    return bool(ends) and len(cons) * 2 > len(ends)


def first_reported(rows: list[dict[str, Any]], consolidated: bool) -> dict[date, dict[str, Any]]:
    """Per quarter end: the announcement (the earliest broadcast of ANY filing for that quarter, either basis) and
    the earliest non-revised XBRL filing on `consolidated` (a revision only when nothing else exists, flagged)."""
    out: dict[date, dict[str, Any]] = {}
    for end in sorted({f["end"] for f in rows}):
        same = [f for f in rows if f["end"] == end]
        first = min(same, key=lambda f: f["at"])
        cands = sorted((f for f in same if f["xbrl"] and f["consolidated"] == consolidated),
                       key=lambda f: (f["revised"], f["at"]))  # fmt: skip
        if not cands:
            continue
        pick = cands[0]
        out[end] = {"announced": first["at"], "has_time": first["has_time"], "xbrl": pick["xbrl"],
                    "filed": pick["at"], "revised_only": pick["revised"], "source": pick["source"]}  # fmt: skip
    return out


def universe() -> list[str]:
    """Data symbols of every point-in-time Nifty 50 member since MEMBERS_SINCE, in the fixed shuffled order."""
    from finresearch.evals import stock_universe as U

    periods, _ = U.reconstruct(U._current_list(), U.load_changes())
    start = max(d for d, _ in periods if d <= MEMBERS_SINCE)
    syms = sorted({U.data_symbol(s) for d, m in periods if d >= start for s in m})
    random.Random(SEED).shuffle(syms)
    return [*syms, MARKET]


async def _polite(eq: Any, make: Any) -> Any:
    """One request after a pause; one retry with a fresh NSE session after a longer pause (as evals.stock_harvest)."""
    await asyncio.sleep(PAUSE_S)
    try:
        return await make()
    except Exception:
        await asyncio.sleep(8)
        eq.nse._warmed = False
        return await make()


def _quarter_facts(data: bytes) -> dict[str, Any] | None:
    from finresearch.adapters.xbrl import parse_results_xbrl
    from finresearch.api.markets import REVENUE_BASES

    q = parse_results_xbrl(data).quarter
    if q is None or not q.facts:
        return None
    rev_key = next((k for k in REVENUE_BASES if k in q.facts), None)
    eps = q.facts.get("eps_basic")
    return {"start": q.start.isoformat() if q.start else None, "end": q.end.isoformat() if q.end else None,
            "eps": None if eps is None else float(eps), "revenue_basis": rev_key,
            "revenue": float(q.facts[rev_key]) if rev_key else None}  # fmt: skip


async def harvest_symbol(eq: Any, sym: str, end: date) -> dict[str, Any]:
    from finresearch.adapters.nse_equity import nse_endpoint, walk_history
    from finresearch.api.markets import _xbrl

    errors: list[str] = []
    res = await _polite(eq, lambda: eq._get(sym, *nse_endpoint("results", sym, period="Quarterly")))
    integ = await _polite(eq, lambda: eq._get(sym, *nse_endpoint("integrated_filings", sym)))
    integ = integ.get("data", []) if isinstance(integ, dict) else integ or []
    rows = [f for f in filing_rows(res or [], integ) if f["end"] >= FIRST_QUARTER]
    basis = choose_basis(rows)
    quarters = []
    for qe, f in sorted(first_reported(rows, basis).items()):
        try:
            facts = _quarter_facts(await _polite(eq, lambda f=f: _xbrl(eq, f["xbrl"])))
        except Exception as e:  # recorded; the quarter is then a gap
            errors.append(f"{qe} {f['xbrl']}: {type(e).__name__}: {e}"[:200])
            continue
        if facts is None:
            errors.append(f"{qe}: no current-quarter facts")
            continue
        quarters.append({"quarter_end": qe.isoformat(), "announced": f["announced"].isoformat(),
                         "has_time": f["has_time"], "filed": f["filed"].isoformat(), "xbrl": f["xbrl"],
                         "revised_only": f["revised_only"], "source": f["source"], **facts})  # fmt: skip
    acts = await _polite(eq, lambda: eq.corporate_actions(sym))
    bars, partial = await walk_history(lambda lo, hi: _polite(eq, lambda: eq.history(sym, lo, hi)),
                                       PRICE_START, end, max_requests=40)  # fmt: skip
    return {"symbol": sym, "consolidated": basis, "quarters": quarters, "errors": errors,
            "actions": [[a.ex_date.isoformat(), a.subject] for a in acts if a.ex_date],
            "prices": [[b.day.isoformat(), float(b.close)] for b in bars if b.close], "prices_partial": partial,
            "harvested": date.today().isoformat()}  # fmt: skip


def _write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.rename(path)


async def harvest(symbols: list[str], end: date, max_minutes: float, out: Path = DATA_DIR) -> None:
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.evals.stock_harvest import _walk_forward

    (out / "stocks").mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max_minutes * 60
    async with NseEquity() as eq:
        idx = out / "index_NIFTY50.json"
        if not idx.exists():
            bars, partial = await _walk_forward(eq, PRICE_START, end)
            _write(
                idx,
                {"partial": partial, "bars": [[b.day.isoformat(), float(b.close)] for b in bars if b.close]},
            )
            print(f"NIFTY 50: {len(bars)} days{' (partial)' if partial else ''}", file=sys.stderr, flush=True)
        for i, sym in enumerate(symbols, 1):
            p = out / "stocks" / f"{sym}.json"
            if p.exists():
                continue
            if time.monotonic() > deadline:
                print(
                    f"time budget reached; {len(symbols) - i + 1} stocks left (run again to resume)",
                    file=sys.stderr,
                )
                break
            try:
                data = await harvest_symbol(eq, sym, end)
            except Exception as e:  # keep going; coverage is reported
                print(
                    f"[{i}/{len(symbols)}] {sym}: FAILED {type(e).__name__}: {e}"[:300],
                    file=sys.stderr,
                    flush=True,
                )
                continue
            _write(p, data)
            print(f"[{i}/{len(symbols)}] {sym}: {len(data['quarters'])} quarters, {len(data['prices'])} days, "
                  f"{len(data['errors'])} errors", file=sys.stderr, flush=True)  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--symbols", default="")
    ap.add_argument("--end", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    ap.add_argument("--max-minutes", type=float, default=60)
    a = ap.parse_args(argv)
    syms = [s for s in a.symbols.split(",") if s] or universe()
    asyncio.run(harvest(syms, a.end, a.max_minutes))


if __name__ == "__main__":
    main()
