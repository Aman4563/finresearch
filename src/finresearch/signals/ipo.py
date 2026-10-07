"""IPO signal: apply or skip before allotment, sell or hold at listing (docs/dev/RESEARCH_ROADMAP.md §D.1).

Event: the listing-day OPEN is above the issue price. Horizon: listing day.

Method, in order of preference:
1. The fitted listing model (`evals.ipo_model`: L2 logistic for P(open > issue), quantile regression for the return
   range) — ONLY if its committed walk-forward report passes the bar (Brier skill > 0 against the base-rate table in
   ≥ 5 of the 7 complete test years 2019–2025). Validation status "backtested".
2. Otherwise the empirical base-rate table: mainboard issues in the same final-QIB band × regime (pre/post the
   Apr-2022 NII allotment reform). P = Laplace-smoothed share that opened above the issue price, range = 95% Wilson
   interval, return quantiles = the cell's empirical 10/50/90%. Validation status "base_rate".

Shadow test (#151): the E-IPO-1 shrinkage blend p = λ·p_model + (1−λ)·p_table (evals/experiments/ipo_calibration)
passed its walk-forward bar at the minimum, so it is NOT the call. It is computed alongside (`Signal.shadow`) and
logged in the forecast ledger as its own method with validation "shadow"; the switch criterion is pre-registered in
SHADOW.md. The live OFS share (NSE issue-size text) and Nifty 50 20-session return feed it as in the harvest.

Decision-time caveat: the history uses FINAL subscription (after the 5 pm close); a retail applicant decides on the
latest live snapshot, and QIB books fill late on the last day. So the table is optimistic about what is knowable.

Expected value of applying for one lot (roadmap §D.1): E[value] ≈ P(allot) × lot cost × E[r], with
P(allot) ≈ min(1, 1/category×) (`fincalc.ipo.allotment_probability_floor`, a floor for a minimum-lot applicant) and
E[r] = the cell's mean listing-open return. Blocked UPI funds are treated as free for the few days they are blocked.
The lottery unit is the category's minimum application (1 lot retail; the first whole lot above ₹2 lakh for sNII and
above ₹10 lakh for bNII, ICDR Reg 32(3A)), so the size suggested is that minimum and `ev_per_application` = EV per lot
× the minimum lots. A category book below 1x is not a lottery (every valid bid is allotted in full). An sNII/bNII
investor whose capital cannot cover the minimum application gets SKIP (a smaller bid would be a retail bid).

After listing the default is SELL_AT_LISTING: the median 780-day buy-and-hold abnormal return of 2016–22 Indian
mainboard IPOs was −31.2% and only 41% beat the market [36]. It becomes HOLD_AFTER_LISTING only if the stock signal
(when one exists) independently rates the stock BUY or ACCUMULATE at the listing price.

GMP (grey-market premium) is never an input. The investor's rules (suggest/profile.py) are applied on top, and a
rule `p_listing_gain < x → skip` sets the personal probability threshold (default 0.5 when there is no such rule).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from finresearch.fincalc import ipo as fipo
from finresearch.signals.base import Factor, Signal, Validation, clip_score
from finresearch.signals.registry import register

log = logging.getLogger(__name__)
EVENT = "NSE listing-day open above the issue price"  # = signals.ledger.IPO_EVENT
HORIZON = "listing day"
DEFAULT_THRESHOLD = 0.5
BASE_RATE_METHOD = "empirical base rate by final QIB band × regime (post-Apr-2022)"
BLEND_METHOD = ("shrinkage blend v1: λ × L2 logistic listing model + (1 − λ) × QIB-band base rate "
                "(walk-forward calibrated, E-IPO-1)")  # fmt: skip
BLEND_CAVEAT = ("The blended model reads the live book as if it were final. On the closing day the last Nifty close "
                "is the previous session's, and a book that is still filling moves the model more than the band "
                "table: re-check after the close.")  # fmt: skip
SOURCES_CITED = [
    "NSE ipo-detail activeCat (combined NSE+BSE book) and public-past-issues; NSE price history (listing day)",
    "Neupane, Paleari & Vismara, institutional demand and IPO underpricing in India [33]",
    "SEBI NII allotment reform, issues opening on/after 4-Apr-2022 [35]",
    "Long-run BHAR of Indian mainboard IPOs 2016-22: median -31.2% [36]",
]
DECISION_CAVEAT = ("The history uses FINAL subscription after the 5 pm close; you decide on the latest live book, "
                   "and QIB demand usually arrives on the last day. The table is optimistic about what is knowable.")  # fmt: skip
LONG_RUN = ("Median 780-day buy-and-hold abnormal return of 197 Indian mainboard IPOs (2016-22) was -31.2%; only 41% "
            "beat the market [36]. The listing is the trade unless a separate long-term case holds.")  # fmt: skip


@dataclass
class Book:
    qib: float | None
    nii: float | None
    bnii: float | None
    snii: float | None
    retail: float | None
    total: float | None
    public_shares: float | None
    as_of: datetime | None
    source: str


@dataclass
class Sources:
    """Everything the provider reads; tests replace these."""

    history_rows: Callable[[], list[dict[str, Any]]]
    latest_snapshot: Callable[[str], dict[str, Any] | None]
    ipo_detail: Callable[[str, str | None], Awaitable[Any]]
    issue_terms: Callable[[str, str | None], Awaitable[Any]]
    profile: Callable[[], Any]
    artefact: Callable[[], dict[str, Any] | None]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    stock_signal: Callable[[str], Awaitable[Any]] | None = None
    record: Callable[[Signal, date, dict[str, Any]], None] | None = None  # the forecast ledger
    calibrated: Callable[[], dict[str, Any] | None] | None = None  # the E-IPO-1 blend artefact
    nifty_closes: Callable[[date], Awaitable[list[tuple[date, Decimal]]]] | None = (
        None  # NIFTY 50 closes to a day
    )


# --------------------------------------------------------------------------- live sources (cached, polite)

_CACHE: dict[Any, tuple[float, Any]] = {}
DETAIL_TTL_S = 300
ROWS_TTL_S = 600


def _cached(key: Any, ttl: float, make: Callable[[], Any]) -> Any:
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = make()
    _CACHE[key] = (time.time() + ttl, value)
    return value


def history_rows_db() -> list[dict[str, Any]]:
    """Mainboard ipo_history rows as dicts (cached 10 minutes); [] if the table is missing or empty."""

    def make() -> list[dict[str, Any]]:
        from finresearch.evals.ipo_model import load_rows

        return load_rows()

    try:
        return _cached("history_rows", ROWS_TTL_S, make)
    except Exception:  # database unreachable: answer [] now, never cache it (the next call asks again)
        return []


def base_rate_table(by: str = "qib") -> dict[str, Any]:
    """The base-rate table from the database, or the committed snapshot (with its harvest date) when the database
    has no harvested rows yet."""
    rows = [r for r in history_rows_db() if r.get("series") == "EQ"]
    if rows:
        out = fipo.base_rates(rows, by=by)
        listed = [
            str(r["listing_date"]) for r in rows if r.get("return_open") is not None and r.get("listing_date")
        ]
        out["source"] = "database"
        out["as_of"] = max(listed) if listed else None
        return out
    from finresearch.evals.ipo_model import load_artefact

    snap = (load_artefact() or {}).get("base_rates", {}).get(by)
    if snap:
        return {**snap, "source": "snapshot"}
    return {**fipo.base_rates([], by=by), "source": "empty", "as_of": None}


def listing_gain_metric(qib):
    """`p_listing_gain` for the rules engine (suggest.rules.gather): the base-rate probability for the live QIB
    book. The signal itself may use the fitted model when that passed its bar; rules on reports use the table."""
    from finresearch.suggest.rules import Metric

    if qib is None or qib.value is None:
        return Metric(None, "needs the live QIB book")
    table = base_rate_table()
    cell = cell_for(table, float(qib.value))
    if not cell or not cell.get("n"):
        return Metric(None, "no base-rate history for this QIB band yet (run `finresearch ipo harvest`)")
    p = fipo.smoothed_rate(round(cell["p_gain"] * cell["n"]), cell["n"])
    return Metric(Decimal(str(round(p, 4))), f"base rate: {cell['n']} post-2022 mainboard issues with final QIB "
                  f"{cell['band']} (FINAL books; optimistic at decision time)", qib.as_of)  # fmt: skip


def latest_snapshot_db(symbol: str) -> dict[str, Any] | None:
    try:
        from sqlalchemy import select

        from finresearch.db import session_scope
        from finresearch.db.models import SubscriptionSnapshotRow

        with session_scope() as s:
            row = s.scalars(select(SubscriptionSnapshotRow).where(
                SubscriptionSnapshotRow.nse_symbol == symbol, SubscriptionSnapshotRow.source == "nse_combined")
                .order_by(SubscriptionSnapshotRow.as_of.desc())).first()  # fmt: skip
            return (
                None
                if row is None
                else {"categories": row.categories, "as_of": row.as_of, "source": row.source}
            )
    except Exception:
        return None


_NSE_LOCK: asyncio.Lock | None = None


def _nse_lock() -> asyncio.Lock:
    """One provider NSE call at a time: every /ipos card asks for its signal at once, and each call opens its own
    client (so the per-client rate limiter alone would let them burst)."""
    global _NSE_LOCK
    if _NSE_LOCK is None:
        _NSE_LOCK = asyncio.Lock()
    return _NSE_LOCK


_NSE_SHARED: dict[str, Any] = {}
NSE_SESSION_S = 900  # re-open the shared session (and its cookies) every 15 minutes


def _shared_nse():
    """One warmed NSE session for the provider's calls on this event loop (call under `_nse_lock`). Each /ipos card
    asks for its signal; opening a new client per call cost a cookie warm-up (the NSE home page) every time, about
    half of a cold /ipos fill. NseClient re-warms by itself on a 401/403 or a block page."""
    from finresearch.adapters.nse import NseClient

    loop = asyncio.get_running_loop()
    cur = _NSE_SHARED.get("client")
    if (
        cur is None
        or _NSE_SHARED.get("loop") is not loop
        or _NSE_SHARED.get("at", 0) < time.time() - NSE_SESSION_S
    ):
        _NSE_SHARED.update(client=NseClient(), loop=loop, at=time.time())
        if cur is not None:
            _NSE_SHARED.setdefault("stale", []).append(cur)  # closed by the caller
    return _NSE_SHARED["client"]


async def _close_stale() -> None:
    import contextlib

    for c in _NSE_SHARED.pop("stale", []):
        with contextlib.suppress(Exception):  # its loop may be gone; then the process exits with it
            await c.aclose()


async def ipo_detail_live(symbol: str, series: str | None):
    key = ("detail", symbol, series)
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]

    async with _nse_lock():
        hit = _CACHE.get(key)  # another request may have fetched it while this one waited
        if hit and hit[0] > time.time():
            return hit[1]
        nse = _shared_nse()
        await _close_stale()
        detail = await nse.ipo_detail(symbol, series)
        _CACHE[key] = (time.time() + DETAIL_TTL_S, detail)
    return detail


async def issue_terms_live(symbol: str, series: str | None):
    async with _nse_lock():
        nse = _shared_nse()
        await _close_stale()
        return await nse.issue_terms(symbol, series)  # disk-cached for hours by the adapter


def profile_db():
    try:
        from finresearch.db import session_scope
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return load_profile(s)
    except Exception:
        from finresearch.suggest.profile import default_profile

        return default_profile()


async def stock_signal_live(symbol: str):
    from finresearch.signals.registry import get_provider

    provider = get_provider("stock")
    return None if provider is None else await provider(symbol, {})


def _artefact():
    from finresearch.evals.ipo_model import load_artefact

    return load_artefact()


def _calibrated():
    from finresearch.evals.ipo_calibration import load_signal_artefact

    return load_signal_artefact()


NIFTY_TTL_S = 3 * 3600


async def nifty_closes_live(today: date) -> list[tuple[date, Decimal]]:
    """NIFTY 50 closes for the ~45 days to `today` (one NSE request through the shared session, cached 3 hours)."""
    from datetime import timedelta

    from finresearch.adapters.nse_equity import NseEquity

    key = ("nifty_closes", today)
    hit = _CACHE.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    async with _nse_lock():
        eq = NseEquity(_shared_nse())  # not a context manager: the shared session stays open
        await _close_stale()
        bars = await eq.index_history("NIFTY 50", today - timedelta(days=45), today)
    out = [(b.day, b.close) for b in bars if b.close is not None]
    _CACHE[key] = (time.time() + NIFTY_TTL_S, out)
    return out


# --------------------------------------------------------------------------- pieces


def _fl(v: Any) -> float | None:
    return None if v is None else float(v)


def book_of(snapshot: dict[str, Any] | None, detail) -> Book | None:
    from finresearch.evals.ipo_history import book_from_snapshot

    if snapshot and snapshot.get("categories"):
        b = book_from_snapshot(snapshot["categories"])
        src, as_of = "archived snapshot (NSE combined book)", snapshot.get("as_of")
    elif detail is not None and detail.combined is not None:
        b = book_from_snapshot([c.model_dump(mode="json") for c in detail.combined.categories])
        src, as_of = "NSE ipo-detail (live combined book)", detail.combined.as_of
    else:
        return None
    return Book(qib=_fl(b["qib_times"]), nii=_fl(b["nii_times"]), bnii=_fl(b["bnii_times"]),
                snii=_fl(b["snii_times"]), retail=_fl(b["retail_times"]), total=_fl(b["total_times"]),
                public_shares=_fl(b["public_shares"]), as_of=as_of, source=src)  # fmt: skip


def category_times(book: Book, category: str) -> tuple[float | None, str]:
    """The subscription that decides this investor's lottery: retail, small NII (₹2–10 lakh) or big NII."""
    if category == "shni":
        return (book.snii if book.snii is not None else book.nii), "sNII (₹2–10 lakh)"
    if category == "bhni":
        return (book.bnii if book.bnii is not None else book.nii), "bNII (above ₹10 lakh)"
    return book.retail, "retail"


def cell_for(table: dict[str, Any], qib: float, post_2022: bool = True) -> dict[str, Any] | None:
    band = fipo.band_of(qib)
    regime = "post_2022" if post_2022 else "pre_2022"
    return next((c for c in table.get("cells", []) if c["band"] == band and c["regime"] == regime), None)


def reliability_interval(artefact: dict[str, Any], p: float) -> tuple[float, float] | None:
    """The model's measured range: the 95% Wilson interval of the observed gain rate in the walk-forward reliability
    bin whose mean forecast is closest to p (out-of-sample, pooled over the test years)."""
    bins = ((artefact.get("pooled") or {}).get("reliability_model")) or []
    if not bins:
        return None
    b = min(bins, key=lambda x: abs(x["mean_p"] - p))
    return fipo.wilson_interval(round(b["observed"] * b["n"]), b["n"])


def model_ready(artefact: dict[str, Any] | None) -> bool:
    return bool(artefact and (artefact.get("gate") or {}).get("passes") and artefact.get("final_model"))


def calibrated_ready(blend: dict[str, Any] | None) -> bool:
    """The E-IPO-1 blend is used only when its artefact shipped the blend and its walk-forward gate passed."""
    return bool(blend and blend.get("ship") == "S" and (blend.get("gate") or {}).get("passes")
                and blend.get("final_model") and "lambda" in ((blend.get("calibrator") or {}).get("params") or {}))  # fmt: skip


def calibrated_interval(blend: dict[str, Any], p: float) -> tuple[float, float] | None:
    """The blend's measured range: the Wilson 95% interval of the observed gain rate in the out-of-sample
    reliability bin (calibrated forecasts, pooled 2019–2025) whose mean forecast is closest to p, stretched to include
    p itself: beyond the outermost bins' means the nearest bin's interval can miss the forecast, and a range that
    excludes its own point estimate would mislead."""
    ci = reliability_interval(
        {"pooled": {"reliability_model": (blend.get("pooled") or {}).get("reliability")}}, p
    )
    return None if ci is None else (min(ci[0], p), max(ci[1], p))


def _no_signal(symbol: str, name: str | None, reason: str, now: datetime, *, base: dict[str, Any] | None = None,
               caveats: list[str] | None = None, validation: Validation | None = None) -> Signal:  # fmt: skip
    return Signal(asset="ipo", instrument=symbol, name=name, action="NO_SIGNAL", score=0.0, event=EVENT,
                  horizon=HORIZON, method="none: " + reason,
                  validation=validation or Validation(status="base_rate", description=reason), base_rate=base,
                  caveats=[reason, *(caveats or [])], sources=list(SOURCES_CITED), as_of=now)  # fmt: skip


def _regime_base(table: dict[str, Any], table_n: int) -> dict[str, Any] | None:
    tot = (table.get("regime_totals") or {}).get("post_2022") or {}
    if not tot.get("n"):
        return None
    return {"n": tot["n"], "p": tot["p_gain"], "ci": tot["p_gain_ci"],
            "description": f"all post-Apr-2022 mainboard issues (any QIB demand), of {table_n} harvested"}  # fmt: skip


def _listed(detail, rows: list[dict[str, Any]], symbol: str, today: date) -> dict[str, Any] | None:
    for r in rows:
        if r.get("symbol") == symbol and r.get("return_open") is not None:
            return {"listing_date": str(r.get("listing_date")), "open": r.get("list_open"),
                    "issue": r.get("issue_price"), "return_open": _fl(r.get("return_open"))}  # fmt: skip
    meta = ((getattr(detail, "raw", None) or {}).get("metaInfo") or {}) if detail is not None else {}
    listing = str(meta.get("listingDate") or "")[:10]
    try:
        if listing and date.fromisoformat(listing) <= today:
            return {"listing_date": listing}
    except ValueError:
        return None
    return None


# --------------------------------------------------------------------------- the provider


SME_REASON = (
    "SME issue: the base rates and model cover mainboard (EQ) issues only; SME listings (thin trading, 90% "
    "price bands) behave differently"
)
SIGNAL_TTL_BIDDING_S = 60  # the book moves minute to minute while bidding is open
SIGNAL_TTL_S = 600
_SIGNALS: dict[Any, tuple[float, Signal]] = {}


def signal_ttl(now: datetime) -> int:
    """60 s during IPO bidding hours (10:00-17:00 IST on trading days), 10 minutes otherwise."""
    from finresearch.api.live import _holidays, market_status

    try:
        holidays = _holidays()
    except Exception:
        holidays = {}
    return SIGNAL_TTL_BIDDING_S if market_status(now, holidays)["ipo_bidding"]["open"] else SIGNAL_TTL_S


def _inputs_key(symbol: str, ctx: dict[str, Any], src: Sources) -> tuple:
    """What the signal depends on besides the exchange's pages (which the adapters cache): the context, the latest
    recorded book and the profile (rules, capital, category). A new snapshot or a profile edit is a new key."""
    import hashlib

    snap = src.latest_snapshot(symbol)
    try:
        prof = src.profile()
        prof_id = hashlib.sha256(prof.model_dump_json().encode()).hexdigest()[:16] if hasattr(prof, "model_dump_json") \
            else repr(prof)  # fmt: skip
    except Exception:
        prof_id = None
    return (symbol, tuple(sorted((str(k), str(v)) for k, v in ctx.items())),
            str(snap.get("as_of")) if snap else None, prof_id)  # fmt: skip


@register("ipo")
async def ipo_signal(instrument: str, ctx: dict[str, Any]) -> Signal:
    """The provider behind /api/signals/ipo/{symbol}, cached per symbol + inputs (60 s in bidding hours, 10 minutes
    otherwise). A cache hit returns the stored signal without recomputing, so the forecast ledger is written only
    when the signal is computed (and it de-duplicates per symbol per day). A signal degraded by a network/exchange
    failure is never cached: the next request computes it again."""
    import time as _t

    symbol = instrument.strip().upper()
    if not symbol or len(symbol) > 30:
        raise ValueError("an NSE IPO symbol is required")
    key = await asyncio.to_thread(_inputs_key, symbol, ctx, SOURCES)
    hit = _SIGNALS.get(key)
    if hit and hit[0] > _t.time():
        return hit[1]
    state: dict[str, Any] = {}
    sig = await compute(instrument, ctx, SOURCES, state=state)
    if not state.get("unreachable"):
        _SIGNALS[key] = (_t.time() + signal_ttl(SOURCES.now()), sig)
        if len(_SIGNALS) > 256:
            now = _t.time()
            for k in [k for k, (at, _) in _SIGNALS.items() if at < now]:
                _SIGNALS.pop(k, None)
    return sig


async def compute(
    instrument: str, ctx: dict[str, Any], src: Sources, *, state: dict[str, Any] | None = None
) -> Signal:
    """`state` (optional) receives "unreachable": True when an exchange page failed on the network or at its gate, so
    the caller does not cache the result."""
    from finresearch.fincalc.dates import to_ist
    from finresearch.suggest.rules import Metric, evaluate, lot_limits

    symbol = instrument.strip().upper()
    if not symbol or len(symbol) > 30:
        raise ValueError("an NSE IPO symbol is required")
    series = (ctx.get("series") or "").upper() or None
    now = src.now()
    today = to_ist(now).date()
    rows = await asyncio.to_thread(src.history_rows)
    eq_rows = [r for r in rows if r.get("series") == "EQ"]
    artefact = src.artefact()
    if eq_rows:
        table = fipo.base_rates(eq_rows)
        listed = [
            str(r["listing_date"])
            for r in eq_rows
            if r.get("return_open") is not None and r.get("listing_date")
        ]
        table.update(source="database", as_of=max(listed) if listed else None)
    else:  # the committed snapshot until the database has harvested rows
        table = {**((artefact or {}).get("base_rates") or {}).get("qib", {}), "source": "snapshot"}
    table_n = table.get("n", 0)

    if series == "SME":  # known from the caller (the /ipos card): no NSE request is needed to say so
        return _no_signal(symbol, None, SME_REASON, now)
    snap = await asyncio.to_thread(src.latest_snapshot, symbol)
    detail = None
    try:
        detail = await src.ipo_detail(symbol, series)
    except Exception as e:
        from finresearch.adapters.http import is_transient

        if state is not None and is_transient(e):
            state["unreachable"] = True
        if snap is None:
            return _no_signal(symbol, None, f"NSE issue page unavailable ({type(e).__name__})", now,
                              base=_regime_base(table, table_n))  # fmt: skip
    name = getattr(detail, "company_name", None)
    if (getattr(detail, "series", series) or "EQ").upper() == "SME" or series == "SME":
        return _no_signal(symbol, name, SME_REASON, now)

    listed = _listed(detail, rows, symbol, today)
    if listed:
        return await _after_listing(symbol, name, listed, now, src)

    if table_n == 0:
        return _no_signal(symbol, name, "no harvested IPO history yet (run `finresearch ipo harvest`)", now)
    book = book_of(snap, detail)
    if book is None or book.qib is None:
        return _no_signal(symbol, name, "the exchange has not published a QIB book for this issue yet", now,
                          base=_regime_base(table, table_n),
                          caveats=["The unconditional post-2022 base rate is shown for context only."])  # fmt: skip

    # Judgment (audit #137): any cell with n >= 1 is used, Laplace-smoothed, with its (wide) Wilson range and a "only n
    # past issues" caveat below 20. The walk-forward reference table (evals.ipo_model.table_forecaster) falls back to the
    # regime rate below MIN_CELL = 5; today every post-2022 cell holds 34+ issues except <1x (0), so the two agree.
    cell = cell_for(table, book.qib)
    if not cell or not cell.get("n"):
        reason = f"no post-2022 issue closed with a final QIB book of {fipo.band_of(book.qib)}"
        profile = await asyncio.to_thread(src.profile)
        live = {
            "qib_times": book.qib,
            "nii_times": book.nii,
            "rii_times": book.retail,
            "total_times": book.total,
        }
        metrics = {k: Metric(None if v is None else Decimal(str(v)), book.source) for k, v in live.items()}
        fired = [r for r in (evaluate(r, metrics) for r in profile.rules if r.metric in metrics)
                 if r.status == "fired" and r.rule.action == "skip"]  # fmt: skip
        if not fired:
            return _no_signal(symbol, name, reason, now, base=_regime_base(table, table_n))
        return Signal(asset="ipo", instrument=symbol, name=name, action="SKIP", score=0.0, event=EVENT,
                      horizon=HORIZON, method="your rules (no comparable history for a probability)",
                      validation=Validation(status="rule_based", description=reason),
                      base_rate=_regime_base(table, table_n),
                      caveats=["Your rules say skip: " + "; ".join(f"{r.rule.id} ({r.rule.metric} = {float(r.value):.2f})"
                                                                  for r in fired), reason, DECISION_CAVEAT],
                      sizing={"rules": [r.to_json() for r in fired]}, sources=list(SOURCES_CITED), as_of=now)  # fmt: skip

    # ---- terms: lot and upper band
    terms = None
    try:
        terms = await src.issue_terms(symbol, series)
    except Exception as e:
        from finresearch.adapters.http import is_transient

        if state is not None and is_transient(e):
            state["unreachable"] = True
        terms = getattr(detail, "terms", None) if detail is not None else None
    lot = getattr(terms, "lot_size", None)
    upper = getattr(terms, "price_high", None)
    lot_cost = Decimal(lot) * Decimal(upper) if lot and upper else None

    # ---- probability
    caveats = [DECISION_CAVEAT]
    factors: list[Factor] = []
    k_gain = round(cell["p_gain"] * cell["n"])
    p_cell = fipo.smoothed_rate(k_gain, cell["n"])
    regime = table["regime_totals"]["post_2022"]
    p_regime = (
        fipo.smoothed_rate(round(regime["p_gain"] * regime["n"]), regime["n"]) if regime.get("n") else 0.5
    )
    base = {"n": cell["n"], "p": cell["p_gain"], "ci": cell["p_gain_ci"],
            "description": f"post-Apr-2022 mainboard issues with final QIB {cell['band']} (source: "
                           f"{table.get('source', 'database')}, listings to {table.get('as_of') or 'n/a'})"}  # fmt: skip
    quantiles = {"p10": cell["p10"], "p50": cell["median"], "p90": cell["p90"]}
    use_model = model_ready(artefact)
    blend = None if use_model or src.calibrated is None else src.calibrated()
    shadow = await _shadow_blend(blend, cell, p_cell, p_regime, book, upper, eq_rows, today, detail, src) \
        if calibrated_ready(blend) else None  # fmt: skip
    if use_model:
        from finresearch.evals import ipo_model as im

        fm = artefact["final_model"]
        feat = _live_features(book, upper, eq_rows, today)
        pred = im.predict(fm, [feat])
        p = float(pred["p"][0])
        quantiles = {"p10": float(pred["q10"][0]), "p50": float(pred["q50"][0]), "p90": float(pred["q90"][0])}
        interval = reliability_interval(artefact, p)
        g = artefact["gate"]
        method = "L2 logistic + quantile regression on the final-book features (walk-forward validated)"
        validation = Validation(status="backtested", n=artefact.get("n_rows", 0),
                                metrics={k: v for k, v in (artefact.get("pooled") or {}).items()
                                         if isinstance(v, int | float)},
                                description=f"walk-forward {g['years_evaluated'][0]}-{g['years_evaluated'][-1]}: "
                                            f"Brier skill vs the base-rate table > 0 in {len(g['years_passed'])} of "
                                            f"{len(g['years_evaluated'])} years")  # fmt: skip
        contrib = im.contributions(fm, feat)
        p0 = 1 / (1 + math.exp(-fm["logistic"][0]))
        score = clip_score(200 * (p - 0.5))
        total = sum(contrib.values())
        factors.append(Factor("Average past issue", round(p0, 3), 200 * (p0 - 0.5),
                              "The model's probability for an issue with average features.", "fincalc:ipo_model"))  # fmt: skip
        for key, c in contrib.items():
            share = (c / total) if abs(total) > 1e-12 else 0.0
            factors.append(Factor(im.FEATURE_LABELS[key], round(feat.get(key) or 0.0, 4)
                                  if isinstance(feat.get(key), float) else feat.get(key), 200 * (p - p0) * share,
                                  f"Logit term {c:+.3f} for this issue.", "fincalc:ipo_model"))  # fmt: skip
    else:
        p = p_cell
        interval = tuple(cell["p_gain_ci"]) if cell.get("p_gain_ci") else None
        g = (artefact or {}).get("gate") or {}
        folds = [
            f for f in (artefact or {}).get("folds", []) if f.get("bss_table_vs_climatology") is not None
        ]
        table_skill = (f"; out of sample this table beat the overall base rate (Brier skill > 0) in "
                       f"{sum(1 for f in folds if f['bss_table_vs_climatology'] > 0)} of {len(folds)} years"
                       if folds else "")  # fmt: skip
        why = (f"the fitted model did not pass the bar (Brier skill > 0 vs this table in {len(g.get('years_passed', []))}"
               f" of {len(g.get('years_evaluated', []))} test years; {GATE_TEXT})" if g
               else "no walk-forward report for the fitted model yet")  # fmt: skip
        method = (
            BASE_RATE_METHOD  # the band is an input (base_rate, factors), not part of the method: calibration
        )
        # groups forecasts by method, and one group per band would never pool the base-rate forecasts
        validation = Validation(status="base_rate", n=cell["n"],
                                metrics={"p_gain": cell["p_gain"], "ci_low": (cell["p_gain_ci"] or [0, 0])[0],
                                         "ci_high": (cell["p_gain_ci"] or [0, 0])[1]},
                                description=f"share of {cell['n']} comparable past issues that opened above the issue "
                                            f"price{table_skill}; {why}")  # fmt: skip
        score = clip_score(200 * (p - 0.5))
        factors.append(Factor("Post-2022 base rate", round(p_regime, 3), 200 * (p_regime - 0.5),
                              f"Share of all {regime.get('n', 0)} post-Apr-2022 mainboard issues that opened above the "
                              "issue price (Laplace-smoothed).", "fincalc:ipo.base_rates"))  # fmt: skip
        factors.append(Factor("QIB demand band", f"{book.qib:.2f}x ({cell['band']})", 200 * (p - p_regime),
                              "Institutional (QIB) demand is the strongest known predictor of Indian listing gains "
                              "[33]; this moves the rate to that of issues in the same QIB band.",
                              book.source, "times"))  # fmt: skip
    if cell["n"] < 20:
        caveats.append(f"Only {cell['n']} past issues in this band: the range is wide.")

    # ---- expected value of one lot
    profile = await asyncio.to_thread(src.profile)
    cat_times, cat_label = category_times(book, profile.category)
    e_r = cell.get("mean")
    p_allot = float(fipo.allotment_probability_floor(Decimal(str(cat_times)))) if cat_times and cat_times > 0 else \
        (1.0 if cat_times is not None else None)  # fmt: skip
    ev = p_allot * float(lot_cost) * e_r if p_allot is not None and lot_cost and e_r is not None else None
    limits = lot_limits(profile, lot_cost)
    # The lottery unit is the category's minimum application (ICDR Reg 32(3A)): 1 lot for retail, the first whole lot
    # above ₹2 lakh for sNII, above ₹10 lakh for bNII. A successful sNII/bNII applicant is allotted that minimum, so
    # the expected value that matters is per application; `ev_per_lot` is the same quantity per lot applied.
    min_lots = limits.get("min_lots") or 1
    ev_app = ev * min_lots if ev is not None else None
    sizing = {"lot_cost": float(lot_cost) if lot_cost else None, "lot_size": lot, "upper_band": _fl(upper),
              "p_allot": p_allot, "p_allot_basis": f"min(1, 1/{cat_label} times) — a floor for a minimum "
              f"{cat_label} application (fincalc.ipo.allotment_probability_floor)", "expected_return_mean": e_r,
              "ev_per_lot": ev, "min_lots": min_lots, "ev_per_application": ev_app,
              "category": profile.category, "limits": limits}  # fmt: skip
    by_capital = limits.get("by_capital")
    cannot_afford = profile.category != "retail" and by_capital is not None and by_capital < min_lots
    if ev is None:
        caveats.append(
            "Expected value per lot unavailable: the lot, the upper band or the category book is missing."
        )

    # ---- the investor's rules
    metrics = {"qib_times": _m(book.qib, book.source), "nii_times": _m(book.nii, book.source),
               "rii_times": _m(book.retail, book.source), "total_times": _m(book.total, book.source),
               "price_band_upper": _m(upper, "NSE issue information"), "lot_size": _m(lot, "NSE issue information"),
               "lot_cost": _m(lot_cost, "fincalc: lot_size x price_band_upper"),
               "max_lots_by_capital": _m(limits["by_capital"], "fincalc: floor(capital / lot_cost)"),
               "p_listing_gain": _m(p, f"signals.ipo ({validation.status})")}  # fmt: skip
    metrics = {k: Metric(v[0], v[1]) for k, v in metrics.items()}
    results = [evaluate(r, metrics) for r in profile.rules if r.metric in metrics]
    fired = [r for r in results if r.status == "fired" and r.rule.action == "skip"]
    unknown = [r for r in results if r.status == "unknown" and r.rule.action == "skip"]
    warns = [r for r in results if r.status == "fired" and r.rule.action == "warn"]
    has_p_rule = any(r.rule.metric == "p_listing_gain" for r in results)
    threshold_ok = has_p_rule or p >= DEFAULT_THRESHOLD
    if cannot_afford:
        action = "SKIP"
        caveats.insert(0, f"Your capital (₹{_inr(float(profile.capital_per_ipo_inr))} per issue) is below the minimum "
                          f"{cat_label} application of {min_lots} lots (₹{_inr(min_lots * float(lot_cost))}); "
                          "a smaller bid would be a retail bid.")  # fmt: skip
    elif fired:
        action = "SKIP"
        caveats.insert(0, "Your rules say skip: " + "; ".join(f"{r.rule.id} ({r.rule.metric} = "
                                                              f"{float(r.value):.2f})" for r in fired))  # fmt: skip
    elif not threshold_ok:
        action = "SKIP"
        caveats.insert(0, f"P(listing gain) {p:.0%} is below the default {DEFAULT_THRESHOLD:.0%} threshold "
                          "(add a p_listing_gain rule to set your own).")  # fmt: skip
    elif ev is not None and ev <= 0:
        action = "SKIP"
        caveats.insert(0, "The expected value of one lot is not positive.")
    else:
        action = "APPLY"
    caveats += [f"Rule {r.rule.id} cannot be checked yet ({r.rule.metric} unknown)." for r in unknown]
    caveats += [f"Warning rule {r.rule.id}: {r.rule.description or r.rule.metric}" for r in warns]
    if book.as_of:
        caveats.append(f"Book as of {to_ist(book.as_of).strftime('%d-%b-%Y %H:%M')} IST ({book.source}).")
    if cannot_afford:
        sizing.update(max_lots=0, reason=f"The minimum {cat_label} application is {min_lots} lots; your capital "
                                         f"covers {by_capital}.")  # fmt: skip
    elif action == "APPLY":
        by = [x for x in (limits.get("by_capital"), limits.get("by_category")) if x is not None]
        cap_lots = min(by) if by else None
        if cat_times is not None and cat_times < 1:
            # an undersubscribed category is not a lottery: every valid bid is allotted in full
            max_lots, reason = cap_lots, (f"The {cat_label} book is below 1x, so every valid bid is allotted in full; "
                                          "the size is capped only by your capital and the category limit.")  # fmt: skip
        else:
            max_lots = None if cap_lots is None else min(min_lots, cap_lots)
            reason = (f"An oversubscribed {cat_label} book is a lottery over applications and a winner gets the "
                      f"minimum application ({min_lots} lot{'s' if min_lots != 1 else ''}), so the minimum "
                      "application gives the same odds as a bigger bid; never above your capital and category "
                      "limits.")  # fmt: skip
        sizing.update(max_lots=max_lots, reason=reason)
    sizing["rules"] = [r.to_json() for r in results]
    sig = Signal(asset="ipo", instrument=symbol, name=name, action=action, score=round(score, 2), event=EVENT,
                 horizon=HORIZON, method=method, validation=validation, probability=round(p, 4),
                 probability_interval=(round(interval[0], 4), round(interval[1], 4)) if interval else None,
                 expected_return={k: round(v, 4) for k, v in quantiles.items() if v is not None}, base_rate=base,
                 factors=factors, caveats=caveats, sizing=sizing, sources=list(SOURCES_CITED), as_of=now)  # fmt: skip
    sig.shadow = shadow
    # ctx log=0: a GET or a scheduled check, not a logged view (#247: only POST /api/signals/ipo/{symbol} logs)
    if src.record is not None and str(ctx.get("log", "1")) != "0":
        resolve_on = expected_listing(detail, today)
        inputs = {
            "symbol": symbol,
            "issue_price": _s(upper),
            "qib_times": book.qib,
            "book_as_of": _s(book.as_of),
        }
        src.record(sig, resolve_on, inputs)
        if shadow is not None:  # logged as its own method: scored out of sample, never used for the call
            src.record(shadow_signal(sig, shadow), resolve_on, {**inputs, "shadow_of": sig.method})
    return sig


def _inr(x: float) -> str:
    from finresearch.fincalc.numbers import group_indian

    return group_indian(str(round(x)))


def _s(v: Any) -> str | None:
    return None if v is None else (v.isoformat() if isinstance(v, datetime) else str(v))


GATE_TEXT = "the signal therefore uses the base-rate table"


def _m(v: Any, source: str) -> tuple[Decimal | None, str]:
    return (None if v is None else Decimal(str(v))), source


def _live_features(book: Book, upper, rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Model features for an open issue from the live book (Nifty unavailable here → 0, the training imputation).

    The OFS share and the Nifty return are left None here; `_live_features_full` fills both (the blend path, #147).
    The raw-model path still uses this function alone: it is unreached while `ipo_model_walkforward.json` fails
    its gate, and must switch to `_live_features_full` before it is ever used."""
    from finresearch.evals.ipo_history import ipo_count

    book_cr = (book.public_shares or 0) * float(upper or 0) / 1e7
    listings = [date.fromisoformat(str(r["listing_date"])[:10]) for r in rows if r.get("listing_date")]
    return {"ln_qib": math.log1p(max(book.qib or 0, 0)), "ln_nii": math.log1p(max(book.nii or 0, 0)),
            "ln_retail": math.log1p(max(book.retail or 0, 0)), "ln_book_cr": math.log(max(book_cr, 1e-3)),
            "ofs_share": None, "nifty20": None, "ipo_count_90d": float(ipo_count(listings, today)),
            "post_2022": 1.0}  # fmt: skip


async def _live_features_full(book: Book, upper, rows: list[dict[str, Any]], today: date, detail,
                             src: Sources) -> dict[str, Any]:  # fmt: skip
    """`_live_features` plus the two inputs the harvest records and the blend needs (audit #137 gap closed): the OFS
    share parsed from NSE's issue-size text with the harvest's own parser, and the NIFTY 50 20-session return to the
    last close on or before today (`evals.ipo_history.nifty_return`). Either stays None (imputed as in training) when
    it cannot be computed."""
    from finresearch.evals.ipo_history import _info, nifty_return, parse_issue_size

    feat = _live_features(book, upper, rows, today)
    info = getattr(detail, "issue_info", None) or {}
    size = parse_issue_size(_info(info, "Issue Size"), Decimal(str(upper)) if upper else None)
    if size["ofs_share"] is not None:
        feat["ofs_share"] = float(size["ofs_share"])
    if src.nifty_closes is not None:
        try:
            closes = await src.nifty_closes(today)
            r = nifty_return(sorted(closes), today, inclusive=True) if closes else None
            feat["nifty20"] = None if r is None else float(r)
        except Exception:
            log.warning("NIFTY 50 history unavailable for the IPO model", exc_info=True)
    return feat


SHADOW_TEXT = ("Shadow test: logged for out-of-sample scoring, not used for the call. It replaces the base-rate "
               "table only if, after at least 30 resolved live IPO forecasts where both methods forecast, its Brier "
               "score is lower with a paired-bootstrap 90% interval that excludes 0 "
               "(evals/experiments/ipo_calibration/SHADOW.md).")  # fmt: skip


async def _shadow_blend(blend: dict[str, Any], cell: dict[str, Any], p_cell: float, p_regime: float, book: Book,
                        upper, rows: list[dict[str, Any]], today: date, detail, src: Sources) -> dict[str, Any]:  # fmt: skip
    """The E-IPO-1 blend p = λ·p_model + (1 − λ)·p_ref, computed alongside the live call (shadow mode, #151). The
    reference is the band's rate, or the regime's below MIN_CELL issues, as in the walk-forward."""
    from finresearch.evals import ipo_model as im

    fm, lam = blend["final_model"], float(blend["calibrator"]["params"]["lambda"])
    p_ref, ref = ((p_cell, f"QIB band {cell['band']}") if cell["n"] >= blend.get("min_cell", 5)
                  else (p_regime, "post-2022 regime (band has too few issues)"))  # fmt: skip
    feat = await _live_features_full(book, upper, rows, today, detail, src)
    p_model = float(im.predict(fm, [feat])["p"][0])
    p = lam * p_model + (1 - lam) * p_ref
    interval = calibrated_interval(blend, p)
    g, pooled = blend["gate"], blend["pooled"]
    narrow = min(
        (f for f in blend["folds"] if f["year"] in g["years_passed"]), key=lambda f: f["bss_vs_table"]
    )
    notes = []
    if feat.get("ofs_share") is None:
        notes.append("OFS share not stated in NSE's issue-size text: imputed with the training median.")
    if feat.get("nifty20") is None:
        notes.append("Nifty 50 20-session return unavailable: set to 0 (the training imputation).")
    return {"method": BLEND_METHOD, "probability": round(p, 4),
            "probability_interval": [round(interval[0], 4), round(interval[1], 4)] if interval else None,
            "p_model": round(p_model, 4), "p_reference": round(p_ref, 4), "reference": ref, "lambda": lam,
            "status": "shadow", "description": SHADOW_TEXT,
            "backtest": f"walk-forward {g['years_evaluated'][0]}-{g['years_evaluated'][-1]} (one of "
                        f"{len(blend.get('variants_tested', []))} pre-registered calibrations): beat the table in "
                        f"{len(g['years_passed'])} of {len(g['years_evaluated'])} years, narrowest {narrow['year']} "
                        f"({narrow['bss_vs_table']:+.3f}); pooled Brier {pooled['brier']:.4f} vs "
                        f"{pooled['brier_table']:.4f} (n = {pooled['n']}); λ = {lam:.2f}",
            "caveats": [BLEND_CAVEAT, *notes]}  # fmt: skip


def shadow_signal(sig: Signal, shadow: dict[str, Any]) -> Signal:
    """The shadow forecast as a ledger entry: same event and instrument, its own method, validation "shadow". The
    action is only what the default 50% threshold would say; it is never shown or acted on."""
    p = shadow["probability"]
    iv = shadow.get("probability_interval")
    return Signal(asset=sig.asset, instrument=sig.instrument, name=sig.name,
                  action="APPLY" if p >= DEFAULT_THRESHOLD else "SKIP", score=round(clip_score(200 * (p - 0.5)), 2),
                  event=sig.event, horizon=sig.horizon, method=shadow["method"],
                  validation=Validation(status="shadow", description=shadow["description"]), probability=p,
                  probability_interval=tuple(iv) if iv else None, base_rate=sig.base_rate,
                  factors=[Factor("Model probability", shadow["p_model"], 0.0, "L2 logistic listing model", "fincalc:ipo_model"),
                           Factor(f"Base rate, {shadow['reference']}", shadow["p_reference"], 0.0, "blend reference",
                                  "fincalc:ipo.base_rates")],
                  caveats=list(shadow["caveats"]), sources=list(sig.sources), as_of=sig.as_of)  # fmt: skip


async def _after_listing(
    symbol: str, name: str | None, listed: dict[str, Any], now: datetime, src: Sources
) -> Signal:
    action, why = "SELL_AT_LISTING", "default after listing (no independent stock signal says buy)"
    if src.stock_signal is not None:
        try:
            st = await src.stock_signal(symbol)
        except Exception:
            st = None
        # the stock signal's composite action, also while it is shown as informational (signals.stock #193): this
        # IPO rule is unchanged by that presentation decision
        composite = ((getattr(st, "call", None) or {}).get("composite_action") or getattr(st, "action", None)
                     if st is not None else None)  # fmt: skip
        if composite in ("BUY", "ACCUMULATE"):
            action, why = "HOLD_AFTER_LISTING", f"the stock signal rates it {composite} at the current price"
    ret = listed.get("return_open")
    factors = [Factor("Long-run IPO returns", -0.312, -30.0, LONG_RUN, "RESEARCH_ROADMAP §D.1 [36]", "BHAR")]
    if ret is not None:
        factors.append(Factor("Listing-day open vs issue", round(ret, 4), 0.0,
                              "What the listing paid on the open; recorded, not a forecast.", "NSE price history"))  # fmt: skip
    sig = Signal(asset="ipo", instrument=symbol, name=name, action=action,
                 score=-30.0 if action == "SELL_AT_LISTING" else 20.0,
                 event="holding after listing beats the market over about two years", horizon="listing day onward",
                 method=f"rule: {why}", validation=Validation(status="rule_based", description="published evidence on "
                 "Indian IPO long-run returns; not validated on this app's outcomes"),
                 factors=factors, caveats=[LONG_RUN, "For issues under ₹250 crore, watch the 30-day anchor unlock."],
                 sizing={"listing": listed}, sources=list(SOURCES_CITED), as_of=now)  # fmt: skip
    return sig


def record_live(sig: Signal, resolve_on: date, inputs: dict[str, Any]) -> None:
    """Log an APPLY/SKIP forecast in the forecast ledger (finresearch.signals.ledger), resolved on the expected
    listing date by the ledger's own IPO listing resolver (open vs issue price). One forecast per symbol per IST day
    (the ledger de-duplicates); a failure to log never blocks the signal."""
    try:
        from finresearch.signals.ledger import record

        record(sig, source="signal:ipo", resolve_on=resolve_on, event_kind="listing_gain", inputs=inputs)
    except Exception:
        log.warning(
            "could not log the IPO signal for %s in the forecast ledger", sig.instrument, exc_info=True
        )


def expected_listing(detail, today: date) -> date:
    """T+3 exchange days after the issue close (NSE's issue period), else a week from today (the resolver retries)."""
    from datetime import timedelta

    from finresearch.monitor.plan import expected_dates
    from finresearch.monitor.watch import parse_period

    period = parse_period((getattr(detail, "issue_info", None) or {}).get("Issue Period"))
    if period:
        try:
            return expected_dates(period[1])[1]
        except Exception:
            pass
    return today + timedelta(days=7)


SOURCES = Sources(history_rows=history_rows_db, latest_snapshot=latest_snapshot_db, ipo_detail=ipo_detail_live,
                  issue_terms=issue_terms_live, profile=profile_db, artefact=_artefact,
                  stock_signal=stock_signal_live, record=record_live, calibrated=_calibrated,
                  nifty_closes=nifty_closes_live)  # fmt: skip
