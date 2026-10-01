"""Daily archive of data that future validation needs and nobody serves historically (portfolio-insights research
§2.2 S6, §2.4, §2.5; issue #147). Append-only JSONL under `data/archive/`, one line per record, no migration.

After the close (from 16:15 IST, weekdays), at most once per IST day per stream:

- `sector_pe.jsonl` — NSE quote `secInfo.pdSectorInd` / `pdSectorPe` / `pdSymbolPe` (verified in the recorded quotes
  `tests/fixtures/prices/nse_quote_*_20260930_after_close.json`) for one representative stock per sectoral index plus
  the active NSE stock watches (capped): a stock-P/E ÷ sector-P/E history for S6, validated in ~2 years.
- `iv_term.jsonl` — for NIFTY, BANKNIFTY and FINNIFTY, the ATM IV and 25-delta skew of the NEXT expiry after the one
  the IV job records (`monitor.iv`, `iv_history` keeps the near one): the term structure (next − near) and skew for
  the variance-risk-premium measurement of §2.5. One option-chain request per index.
- `gsec_spread.jsonl` — the FBIL par-yield curve of the latest published day, and NSE's traded bond list (price,
  coupon, maturity, rating, traded value) with a pre-tax spread to the matching-tenor par yield. The coupon frequency
  is not in NSE's list, so the spread is computed at an ASSUMED yearly frequency and labelled so; the raw inputs are
  stored so it can be recomputed once the frequency is known (§2.4: context, not a signal).

Network: about 20 quotes, 3 option chains, 1 bond list and 2 FBIL requests a day, through the app's polite clients.
A stream that fails is retried at most 3 times a day, 15 minutes apart; a failure never breaks the monitor tick.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from finresearch.config import REPO_ROOT
from finresearch.fincalc.dates import to_ist

log = logging.getLogger(__name__)
ARCHIVE_DIR = REPO_ROOT / "data" / "archive"
START = (16, 15)  # IST: after the IV job's 15:50 start, so the near-expiry row is usually there
MAX_TRIES, RETRY_S = 3, 900
MAX_WATCHES = 30
# one liquid stock per NSE sectoral index: its quote carries that index's P/E (pdSectorPe)
SECTOR_REPRESENTATIVES = ("INFY", "HDFCBANK", "SBIN", "BAJFINANCE", "MARUTI", "HINDUNILVR", "TATASTEEL", "SUNPHARMA",
                          "APOLLOHOSP", "NTPC", "ONGC", "DLF", "TITAN", "LT", "BHARTIARTL", "ULTRACEMCO")  # fmt: skip
IV_TERM_SYMBOLS = ("NIFTY", "BANKNIFTY", "FINNIFTY")
_tries: dict[tuple[str, date], tuple[int, float]] = {}


@dataclass
class ArchiveSources:
    """Everything the archive reads; tests replace these. None = the live source."""

    quote: Callable[[str], Awaitable[Any]] | None = None  # symbol -> adapters.nse.Quote
    fno: Any = None  # the monitor's option-chain client factory (async context manager)
    bonds: Callable[[], Awaitable[list[Any]]] | None = None  # -> list[adapters.nse_bonds.ListedBond]
    par_curve: Callable[[], Awaitable[Any]] | None = None  # -> adapters.fbil.ParCurve
    watched: Callable[[], list[str]] | None = None  # active NSE stock watches
    near_iv: Callable[[str, date], dict[str, Any] | None] | None = None  # iv_history row (symbol, day)
    out_dir: Path = field(default_factory=lambda: ARCHIVE_DIR)


# ------------------------------------------------------------------------------------------------ storage
def _path(src: ArchiveSources, stream: str) -> Path:
    return src.out_dir / f"{stream}.jsonl"


def _jsonable(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, date | datetime):
        return v.isoformat()
    return v


def append(src: ArchiveSources, stream: str, rows: list[dict[str, Any]]) -> None:
    src.out_dir.mkdir(parents=True, exist_ok=True)
    with _path(src, stream).open("a") as f:
        for r in rows:
            f.write(json.dumps({k: _jsonable(v) for k, v in r.items()}, sort_keys=True) + "\n")


def recorded_days(src: ArchiveSources, stream: str) -> set[str]:
    """The `archived_on` days already in a stream (read once per call; the files stay small: ~40 lines a day)."""
    p = _path(src, stream)
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text().splitlines():
        try:
            out.add(json.loads(line).get("archived_on"))
        except ValueError:
            continue
    return out


# ------------------------------------------------------------------------------------------------ records
def sector_pe_row(q: Any, archived_on: date) -> dict[str, Any] | None:
    """One sector-P/E record from a quote, or None when NSE gave no sector P/E."""
    if getattr(q, "sector_pe", None) is None:
        return None
    return {"archived_on": archived_on, "symbol": q.symbol, "sector_index": q.sector_index, "sector_pe": q.sector_pe,
            "symbol_pe": q.symbol_pe, "close": q.close_price or q.last_price, "as_of": q.as_of,
            "source": "NSE quote secInfo.pdSectorPe/pdSymbolPe"}  # fmt: skip


def next_expiry(expiries: list[date], near: date) -> date | None:
    """The first listed expiry after the near one the IV job uses."""
    return next((e for e in sorted(expiries) if e > near), None)


def iv_term_row(symbol: str, near: dict[str, Any], nxt: dict[str, Any], archived_on: date) -> dict[str, Any]:
    slope = (float(nxt["atm_iv"]) - float(near["atm_iv"])) if nxt.get("atm_iv") is not None and \
        near.get("atm_iv") is not None else None  # fmt: skip
    return {"archived_on": archived_on, "symbol": symbol, "day": nxt["day"],
            "near_expiry": near["expiry"], "near_atm_iv": near["atm_iv"], "near_skew_25d": near.get("skew_25d"),
            "next_expiry": nxt["expiry"], "next_atm_iv": nxt["atm_iv"], "next_skew_25d": nxt.get("skew_25d"),
            "term_slope": slope, "underlying": nxt.get("underlying"),
            "source": "NSE option chains; fincalc.volatility.atm_iv / skew_25d"}  # fmt: skip


def bond_spread_rows(bonds: list[Any], curve: Any, today: date, archived_on: date) -> list[dict[str, Any]]:
    """Traded bonds with a complete row: the inputs plus a pre-tax spread (pp) of the effective annual YTM at an
    ASSUMED yearly coupon over the effective annual FBIL par yield at the same tenor."""
    from finresearch.fincalc import bonds as B

    out = []
    for b in bonds:
        price = b.last_price or b.close
        if not (price and b.coupon_pct is not None and b.maturity and b.face_value and b.maturity > today):
            continue
        if not b.traded_value:
            continue
        years = (b.maturity - today).days / 365.25
        spread = None
        try:
            ytm = B.ytm(price, today, b.maturity, b.coupon_pct / 100, 1, b.face_value)  # price taken as clean
            gsec = curve.par_yield(years)
            spread = (B.effective_annual(ytm, 1) - B.effective_annual(gsec, 2)) * 100
        except Exception:  # a price the solver cannot bracket: keep the inputs, no spread
            spread = None
        out.append({"archived_on": archived_on, "isin": b.isin, "symbol": b.symbol, "series": b.series,
                    "rating": b.rating, "coupon_pct": b.coupon_pct, "face_value": b.face_value, "price": price,
                    "maturity": b.maturity, "tenor_years": round(years, 3), "traded_value": b.traded_value,
                    "as_of": b.as_of, "curve_as_of": curve.as_of, "spread_pp_assumed_yearly": spread,
                    "assumption": "coupon yearly, price clean (NSE's list gives neither)"})  # fmt: skip
    return out


# ------------------------------------------------------------------------------------------------ live sources
async def _live_bonds():
    from finresearch.adapters.nse_bonds import live_bonds

    return await live_bonds()


async def _live_curve():
    from finresearch.adapters.fbil import FbilClient

    async with FbilClient() as fbil:
        return await fbil.latest_par_curve()


def _live_watched() -> list[str]:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Watch

    with session_scope() as s:
        rows = s.scalars(select(Watch.nse_symbol).where(Watch.active.is_(True), Watch.kind == "stock",
                                                        Watch.exchange != "BSE")).all()  # fmt: skip
    return sorted({r.upper() for r in rows if r})


def _live_near_iv(symbol: str, day: date) -> dict[str, Any] | None:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import IvHistory

    with session_scope() as s:
        r = s.scalars(select(IvHistory).where(IvHistory.symbol == symbol, IvHistory.day == day)).first()
        if r is None:
            return None
        return {"expiry": r.expiry, "atm_iv": r.atm_iv, "skew_25d": r.skew_25d, "day": r.day}


# ------------------------------------------------------------------------------------------------ the step
def _due(stream: str, today: date) -> bool:
    n, at = _tries.get((stream, today), (0, 0.0))
    return n < MAX_TRIES and time.time() - at >= RETRY_S


def _tried(stream: str, today: date) -> None:
    n, _ = _tries.get((stream, today), (0, 0.0))
    _tries[(stream, today)] = (n + 1, time.time())


def _done(stream: str, today: date) -> None:
    _tries[(stream, today)] = (MAX_TRIES, time.time())


async def archive_step(now: datetime, src: ArchiveSources | None = None, *,
                       holidays: set[date] | None = None) -> dict[str, Any]:  # fmt: skip
    """Archive each stream once per NSE trading day after the close. Returns {"archived": {stream: n}, "failed":
    {...}}. On an exchange holiday NSE serves the last session's figures: archiving them under the holiday's date
    would add a duplicate day to the series, so holidays are skipped like weekends."""
    src = src or ArchiveSources()
    local = to_ist(now)
    today = local.date()
    if local.weekday() >= 5 or (local.hour, local.minute) < START:
        return {"archived": {}, "failed": {}, "skipped": "outside the after-close window"}
    if holidays is None:
        from finresearch.adapters.nse_holidays import trading_holidays

        holidays = trading_holidays()
    if today in holidays:
        return {"archived": {}, "failed": {}, "skipped": "exchange holiday"}
    out: dict[str, Any] = {"archived": {}, "failed": {}}
    key = today.isoformat()
    for stream, run in (("sector_pe", _sector_pe), ("iv_term", _iv_term), ("gsec_spread", _gsec_spread)):
        if not _due(stream, today):
            continue
        if key in recorded_days(src, stream):
            _done(stream, today)
            continue
        _tried(stream, today)
        try:
            rows = await run(src, today)
        except Exception as e:
            out["failed"][stream] = f"{type(e).__name__}: {e}"[:200]
            continue
        if rows:
            append(src, stream, rows)
            _done(stream, today)
            out["archived"][stream] = len(rows)
        else:
            out["failed"][stream] = "no rows"
    if out["failed"]:
        log.info("data archive: %s", out["failed"])
    return out


async def _sector_pe(src: ArchiveSources, today: date) -> list[dict[str, Any]]:
    watched = (src.watched or _live_watched)()[:MAX_WATCHES]
    symbols = list(dict.fromkeys([*SECTOR_REPRESENTATIVES, *watched]))
    if src.quote is not None:
        return await _quotes(src.quote, symbols, today)
    import asyncio

    from finresearch.adapters.nse import NseClient

    async with NseClient() as nse:  # one session, warmed once (as portfolio.valuation.QuoteBatch does)
        first = [True]

        async def quote(sym: str):
            await asyncio.sleep(0 if first[0] else 1.0)  # polite on top of the client's own limit
            q = await nse.quote(sym, warm=first[0])
            first[0] = False
            return q

        return await _quotes(quote, symbols, today)


async def _quotes(quote, symbols: list[str], today: date) -> list[dict[str, Any]]:
    rows = []
    for sym in symbols:
        try:
            row = sector_pe_row(await quote(sym), today)
        except Exception:
            row = None
        if row:
            rows.append(row)
    return rows


async def _iv_term(src: ArchiveSources, today: date) -> list[dict[str, Any]]:
    from finresearch.monitor.iv import snapshot

    if src.fno is None:
        return []
    near_of = src.near_iv or _live_near_iv
    rows = []
    async with src.fno() as f:
        for sym in IV_TERM_SYMBOLS:
            near = near_of(sym, today)
            if not near or near.get("expiry") is None:
                continue  # the IV job has not recorded today's near expiry yet: retried later
            expiries, _ = await f.contract_info(sym)
            nxt = next_expiry(expiries, near["expiry"])
            if nxt is None:
                continue
            snap = snapshot(await f.option_chain(sym, nxt), today)
            if (
                snap is not None and snap["expiry"] == nxt
            ):  # never file another expiry's chain as the next one
                rows.append(iv_term_row(sym, near, snap, today))
    return rows


async def _gsec_spread(src: ArchiveSources, today: date) -> list[dict[str, Any]]:
    curve = await (src.par_curve or _live_curve)()
    if getattr(curve, "fallback", False):
        raise RuntimeError("FBIL unreachable (fallback curve): not archived")
    bonds = await (src.bonds or _live_bonds)()
    rows = [{"archived_on": today, "kind": "par_curve", "curve_as_of": curve.as_of, "source": curve.source,
             "points": [[float(p.tenor_years), float(p.semi_annual)] for p in curve.points]}]  # fmt: skip
    return rows + [{"kind": "bond", **r} for r in bond_spread_rows(bonds, curve, today, today)]


def reset_tries() -> None:
    _tries.clear()
