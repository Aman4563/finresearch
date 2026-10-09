"""The portfolio's daily value history, rebuilt from its transactions and historical prices (research note §1.1).

    V(t) = Σ_i u_i(t)·P_i(t)          F(t) = net money put in on day t

- Units u_i(t) come from the same FIFO replay as the lots (`lots.build_lots` on the events up to each event day), so
  splits, bonuses, statement opening balances and removals count exactly as they do for tax. They are multiplied by
  the **raw** close: a split multiplies the units on its ex-date while the close drops, so neither series needs
  adjusting.
- Prices: NSE daily closes (`NseEquity.history`, walked ~70 trading days per request), BSE daily closes for BSE-only
  stocks (one CSV per range), AMFI NAV history for funds (`AmfiClient.scheme_history`). Every instrument's closes are
  cached on disk (state_dir/portfolio_history/) with the date ranges already fetched, and only missing ranges are
  requested later; ranges that end more than a few days ago are final.
- Flows mirror `valuation.cash_flows` (the app's XIRR): a purchase adds amount + charges, a sale takes out the proceeds
  net of charges, a dividend paid out takes out its amount; reinvested dividends and the units they buy are neither.
  Opening balances, transfers out ("remove") and trades without an amount move in or out *in kind* at that day's close.
  A flow on a day without prices counts on the next trading day of the series.
- The series starts on the first day every holding's units are known: a holding that starts with a statement's
  opening balance has unknown units before it, so the series starts no earlier than that date, and it says so. It
  also starts no earlier than the first day every holding then held has a price, and at most MAX_YEARS back.
- Benchmark: NIFTYBEES (the Nippon India Nifty 50 ETF) as a total return: NSE closes with its cash distributions
  reinvested on their ex-dates and its unit split (10 -> 1 on 19-Dec-2019, NSE corporate actions) applied. It stands in
  for the Nifty 50 TRI (niftyindices.com has no machine-readable feed the app can use) and trails it by the ETF's
  expense ratio and tracking error. The forecast ledger uses the same ETF.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from finresearch.portfolio import analytics_math as am
from finresearch.portfolio.lots import Event, bonus_ratio, build_lots, split_factor, superseded_openings

log = logging.getLogger(__name__)

BENCHMARK = "NIFTYBEES"
BENCHMARK_LABEL = "NIFTYBEES total return (Nifty 50 ETF, distributions reinvested)"
MAX_YEARS = 10
FINAL_AFTER_DAYS = 5  # a fetched range that ended this many days ago has every bar it will ever have
RISK_LOOKBACK_DAYS = (
    380  # current holdings' prices are read for a year even if bought last week (risk contribution)
)
# Groww's daily candles serve ranges starting at most this many days back (the daily top-up); see Fetcher._groww
GROWW_HISTORY_DAYS = 31
STALE_DAYS = 10  # a forward-filled price older than this (calendar days) marks the day incomplete
JUMP = 0.40  # a one-day move beyond ±40 % with no split/bonus recorded is probably a missing corporate action

_DIV_RE = re.compile(
    r"dividend[^0-9]*?(?:rs\.?|re\.?|₹|inr)\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*per\s*(?:share|unit)", re.I
)


# --------------------------------------------------------------------------- inputs
@dataclass
class HoldingIn:
    """What the history needs about one holding (detached from the database session)."""

    id: int
    name: str
    account: str
    asset_type: str
    ikey: str
    isin: str | None
    nse_symbol: str | None
    bse_code: str | None
    scheme_code: str | None
    sector: str | None
    tax_class: str
    meta: dict[str, Any]
    events: list[Event]


def holdings_from(data: Any) -> list[HoldingIn]:
    """`report.Loaded` -> detached inputs (the effective tax class is resolved here, as the page does)."""
    from finresearch.portfolio.report import tax_class_of
    from finresearch.portfolio.service import events_of

    out = []
    for h in data.holdings:
        eff, _, _ = tax_class_of(h)
        out.append(HoldingIn(h.id, h.name, h.account, h.asset_type, h.ikey, h.isin, h.nse_symbol, h.bse_code,
                             h.scheme_code, h.sector, eff, dict(h.meta or {}), events_of(data.txns.get(h.id, []))))  # fmt: skip
    return out


def fingerprint(holdings: Sequence[HoldingIn]) -> str:
    """A hash of every transaction: the history is rebuilt when (and only when) the transactions change."""
    h = hashlib.sha256()
    for x in sorted(holdings, key=lambda x: x.id):
        h.update(f"{x.id}|{x.ikey}|{x.nse_symbol}|{x.bse_code}|{x.scheme_code}|{x.tax_class}".encode())
        for e in sorted(x.events, key=lambda e: (e.day, e.id or 0)):
            h.update(
                f"{e.id}|{e.day}|{e.kind}|{e.quantity}|{e.price}|{e.amount}|{e.charges}|{sorted(e.meta.items())}".encode()
            )
    return h.hexdigest()[:24]


# --------------------------------------------------------------------------- price cache
Fetch = Callable[[date, date], Awaitable[tuple[dict[date, float], bool]]]  # -> (closes, complete)


def _merge(ranges: list[list[date]]) -> list[list[date]]:
    out: list[list[date]] = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + timedelta(days=1):
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def _missing(start: date, end: date, ranges: list[list[date]]) -> list[tuple[date, date]]:
    gaps, cur = [], start
    for a, b in _merge(ranges):
        if b < cur:
            continue
        if a > end:
            break
        if a > cur:
            gaps.append((cur, min(end, a - timedelta(days=1))))
        cur = max(cur, b + timedelta(days=1))
    if cur <= end:
        gaps.append((cur, end))
    return gaps


class PriceStore:
    """Daily closes per instrument on disk: {"ranges": [[from, to], ...], "closes": {day: close}}."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', key)}.json"

    def load(self, key: str) -> tuple[dict[date, float], list[list[date]]]:
        p = self._path(key)
        if not p.exists():
            return {}, []
        try:
            d = json.loads(p.read_text())
            closes = {date.fromisoformat(k): float(v) for k, v in d.get("closes", {}).items()}
            ranges = [[date.fromisoformat(a), date.fromisoformat(b)] for a, b in d.get("ranges", [])]
            return closes, ranges
        except (ValueError, KeyError, TypeError):
            return {}, []

    def save(self, key: str, closes: dict[date, float], ranges: list[list[date]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        body = {"ranges": [[a.isoformat(), b.isoformat()] for a, b in _merge(ranges)],
                "closes": {d.isoformat(): repr(v) for d, v in sorted(closes.items())}}  # fmt: skip
        tmp = self._path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(body))
        tmp.replace(self._path(key))

    async def get(self, key: str, start: date, end: date, fetch: Fetch, today: date) -> dict[date, float]:
        closes, ranges = self.load(key)
        changed = False
        for a, b in _missing(start, end, ranges):
            got, complete = await fetch(a, b)
            closes.update(got)
            changed = True
            final = b <= today - timedelta(days=FINAL_AFTER_DAYS)
            if complete and final:
                ranges.append([a, b])
            elif got:
                lo = a if complete else min(got)
                hi = b if final else max(got)
                if lo <= hi:
                    ranges.append([lo, hi])
        if changed:
            self.save(key, closes, ranges)
        return {d: v for d, v in closes.items() if start <= d <= end}


# --------------------------------------------------------------------------- instruments
@dataclass
class Instrument:
    key: str  # "NSE:INFY", "BSE:500325", "MF:120503"
    kind: str  # nse | bse | mf
    ident: str
    scheme: Any = None  # SchemeNav for funds


def instrument_for(
    h: HoldingIn, schemes: dict[str, Any], isin_map: dict[str, Any] | None = None
) -> Instrument | None:
    if h.asset_type == "mf":
        code = h.scheme_code
        sch = schemes.get(code or "") or schemes.get(f"ISIN:{(h.isin or '').upper()}")
        if sch is None:
            return None
        return Instrument(f"MF:{sch.code}", "mf", sch.code, sch)
    if h.asset_type == "stock":
        from finresearch.portfolio.valuation import instrument_of

        sym, exch, _ = instrument_of(h, isin_map)
        if not sym:
            return None
        return Instrument(f"{exch}:{sym}", "bse" if exch == "BSE" else "nse", sym)
    return None


class Fetcher:
    """Price history through the app's `MarketSources` (live clients by default; fakes in tests). One NSE session is
    shared by every stock in a build, so NSE's cookie warm-up happens once and the polite client's rate limit
    (2 requests per second) applies to the whole build."""

    def __init__(self, sources: Any, groww: Any = "groww") -> None:
        self.sources = sources
        # Groww's daily candles first (#267): `groww` is an adapters.groww_market.GrowwMarket (default: the process's),
        # None turns it off. `split_days` (filled by `build`) are the split/bonus ex-dates per instrument key.
        self.groww = groww
        self.split_days: dict[str, set[date]] = {}
        self.provenance: dict[str, str] = {}  # instrument key -> where its last fetched closes came from
        self._eq: dict[str, Any] = {}
        self._cms: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc: object) -> None:
        for cm in self._cms.values():
            with contextlib.suppress(Exception):  # closing a client must not fail the build
                await cm.__aexit__(None, None, None)

    async def _equity(self, exchange: str) -> Any:
        async with self._lock:
            if exchange not in self._eq:
                cm = self.sources.open_equity(exchange)
                self._cms[exchange] = cm
                self._eq[exchange] = await cm.__aenter__()
            return self._eq[exchange]

    async def __call__(self, inst: Instrument, start: date, end: date) -> tuple[dict[date, float], bool]:
        if inst.kind == "mf":
            rows = await self.sources.get_nav_history(inst.scheme, start, end)
            return {
                r.day: float(r.nav) for r in rows if r.day and r.nav is not None and start <= r.day <= end
            }, True
        got = await self._groww(inst, start, end)
        if got is not None:
            return got
        self.provenance[inst.key] = "BSE" if inst.kind == "bse" else "NSE"
        return await self._exchange(inst, start, end)

    async def _groww(self, inst: Instrument, start: date, end: date) -> tuple[dict[date, float], bool] | None:
        """Closes from Groww, or None to use the exchange. Whether Groww's daily closes are adjusted for splits and
        bonuses is not documented [U], and this history multiplies units by RAW closes. Adjusted and raw closes agree
        on every day after the instrument's latest split/bonus, so Groww is asked only for a recent range
        (GROWW_HISTORY_DAYS: the daily top-up of the cached closes) with no recorded split/bonus on or after its start;
        older backfills stay on the exchange's raw closes."""
        from finresearch.adapters import groww_market

        market = groww_market.MARKET if self.groww == "groww" else self.groww
        today = self.sources.today() if hasattr(self.sources, "today") else date.today()
        if market is None or start < today - timedelta(days=GROWW_HISTORY_DAYS):
            return None
        if any(d >= start for d in self.split_days.get(inst.key, ())):
            return None
        try:
            got = await market.history_closes("BSE" if inst.kind == "bse" else "NSE", inst.ident, start, end)
        except Exception as e:  # Groww failed: the exchange answers this range
            log.info("Groww history for %s failed (%s): using the exchange", inst.key, type(e).__name__)
            return None
        if got is None:
            return None
        self.provenance[inst.key] = "Groww"
        return got, True

    async def _exchange(self, inst: Instrument, start: date, end: date) -> tuple[dict[date, float], bool]:
        from finresearch.adapters.nse_equity import walk_history

        eq = await self._equity("BSE" if inst.kind == "bse" else "NSE")
        if getattr(eq, "answers_full_range", False):
            bars, partial = await eq.history(inst.ident, start, end), False
        else:
            bars, partial = await walk_history(lambda lo, hi: eq.history(inst.ident, lo, hi), start, end)
        return {
            b.day: float(b.close) for b in bars if b.close is not None and start <= b.day <= end
        }, not partial

    async def actions(self, symbol: str) -> list[Any]:
        eq = await self._equity("NSE")
        return await eq.corporate_actions(symbol)


# --------------------------------------------------------------------------- result
@dataclass
class Position:
    """One instrument (possibly held in several accounts) at the end of the series."""

    key: str
    name: str
    asset_type: str
    tax_class: str
    holding_ids: list[int]
    units: float
    price: float | None
    price_day: date | None
    value: float
    sector: str | None
    nse_symbol: str | None
    scheme: Any = None


@dataclass
class Benchmark:
    days: list[date]
    level: list[float]  # total-return index aligned to History.days (forward-filled)
    closes: dict[date, float]  # raw closes
    dividends: dict[date, float]
    factors: dict[date, float]
    label: str = BENCHMARK_LABEL
    note: str | None = None


@dataclass
class History:
    days: list[date] = field(default_factory=list)
    value: list[float] = field(default_factory=list)
    flow: list[float] = field(default_factory=list)  # net money in, per day (day 0: see `initial`)
    returns: list[float | None] = field(default_factory=list)
    index: list[float] = field(default_factory=list)
    invested: list[float] = field(default_factory=list)  # cumulative net money in (the start value counts)
    complete: list[bool] = field(default_factory=list)
    initial: float = 0.0  # value on day 0 of the units held before the day's own trades + the day's flows
    flows: list[tuple[date, float]] = field(default_factory=list)  # (day, net in) incl. the initial amount
    positions: list[Position] = field(default_factory=list)
    closes: dict[str, dict[date, float]] = field(default_factory=dict)  # instrument key -> raw closes
    factors: dict[str, dict[date, float]] = field(
        default_factory=dict
    )  # instrument key -> split/bonus unit factors
    benchmark: Benchmark | None = None
    start_reason: str = ""
    notes: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reason: str | None = None  # why there is no series at all

    @property
    def ok(self) -> bool:
        return self.reason is None and len(self.days) >= 2


# --------------------------------------------------------------------------- units and flows
def units_by_day(events: Sequence[Event]) -> list[tuple[date, float]]:
    """(event day, units after every event of that day), replaying `build_lots` on each prefix: exactly the lots'
    splits, bonuses, opening-balance and removal rules."""
    evs = sorted(events, key=lambda e: e.day)
    days = sorted({e.day for e in evs if e.kind != "dividend"})
    out = []
    for d in days:
        out.append((d, float(build_lots([e for e in evs if e.day <= d]).units)))
    return out


def units_on(steps: Sequence[tuple[date, float]], day: date) -> float:
    u = 0.0
    for d, v in steps:
        if d > day:
            break
        u = v
    return u


def unit_factors(events: Sequence[Event]) -> dict[date, float]:
    """Split/bonus unit multipliers by ex-date (10 -> 2 split: 5; 1:1 bonus: 2), for per-instrument returns."""
    out: dict[date, float] = {}
    for e in events:
        if e.kind == "split" and (f := split_factor(e.meta)) is not None:
            out[e.day] = out.get(e.day, 1.0) * float(f)
        elif e.kind == "bonus" and (r := bonus_ratio(e.meta)) is not None:
            out[e.day] = out.get(e.day, 1.0) * float(1 + r[0] / r[1])
    return out


@dataclass
class _Flow:
    day: date
    cash: float | None  # None: in kind (units × that day's close)
    units: float = 0.0  # in-kind units (+ in, - out)


def flows_of(events: Sequence[Event]) -> list[_Flow]:
    """External flows of one holding (see the module docstring). A statement opening that the lots ignore (an older
    statement or tradebook already covers that history) is no flow either: its units are already in the series."""
    out = []
    skip = superseded_openings(events)
    for i, e in enumerate(events):
        if i in skip:
            continue
        reinvest = bool(e.meta.get("reinvest"))
        gross = e.gross()
        q = float(e.quantity) if e.quantity is not None else 0.0
        if e.kind == "buy" and not reinvest:
            out.append(_Flow(e.day, float(gross + e.charges)) if gross is not None else _Flow(e.day, None, q))
        elif e.kind == "sell":
            out.append(
                _Flow(e.day, -float(gross - e.charges)) if gross is not None else _Flow(e.day, None, -q)
            )
        elif e.kind == "dividend" and not reinvest and e.amount:
            out.append(_Flow(e.day, -float(abs(e.amount))))
        elif e.kind == "opening":
            out.append(_Flow(e.day, None, q))
        elif e.kind == "remove":
            out.append(_Flow(e.day, None, -q))
    return out


def _ffill(closes: dict[date, float], days: Sequence[date]) -> list[tuple[float | None, date | None]]:
    """(price, its day) on or before each day."""
    keys = sorted(closes)
    out, i, last = [], 0, (None, None)
    for d in days:
        while i < len(keys) and keys[i] <= d:
            last = (closes[keys[i]], keys[i])
            i += 1
        out.append(last)
    return out


def _next_on_or_after(days: Sequence[date], d: date) -> int | None:
    import bisect

    i = bisect.bisect_left(days, d)
    return i if i < len(days) else None


def parse_benchmark_actions(actions: Sequence[Any]) -> tuple[dict[date, float], dict[date, float]]:
    """(cash dividend per unit by ex-date, unit factor by ex-date) from NSE corporate actions."""
    from finresearch.portfolio.service import parse_action

    divs: dict[date, float] = {}
    factors: dict[date, float] = {}
    for a in actions:
        if a.ex_date is None:
            continue
        subject = a.subject or ""
        amt = a.dividend_per_share
        if amt is None:
            got = _DIV_RE.findall(subject)
            amt = sum((Decimal(x) for x in got), Decimal(0)) if got else None
        if amt:
            divs[a.ex_date] = divs.get(a.ex_date, 0.0) + float(amt)
        for kind, meta in parse_action(subject):
            f = split_factor(meta) if kind == "split" else None
            if kind == "bonus" and (r := bonus_ratio(meta)) is not None:
                f = 1 + r[0] / r[1]
            if f:
                factors[a.ex_date] = factors.get(a.ex_date, 1.0) * float(f)
    return divs, factors


# --------------------------------------------------------------------------- build
async def build(holdings: Sequence[HoldingIn], *, fetch: Any, store: PriceStore, schemes: dict[str, Any],
                today: date, isin_map: dict[str, Any] | None = None,
                benchmark: bool = True) -> History:  # fmt: skip
    """Rebuild the daily series. `fetch` is a `Fetcher` (or a fake with the same call and `actions`)."""
    hist = History()
    live = [h for h in holdings if any(e.kind in ("buy", "opening", "sell", "remove") for e in h.events)]
    if not live:
        hist.reason = "no transactions yet: import a statement or tradebook, or add transactions by hand"
        return hist
    insts: dict[int, Instrument] = {}
    for h in live:
        inst = instrument_for(h, schemes, isin_map)
        if inst is None:
            what = "set its AMFI scheme code" if h.asset_type == "mf" else (
                "set its NSE symbol or BSE code" if h.asset_type == "stock" else "the app has no price history for it")  # fmt: skip
            hist.excluded.append(f"{h.name} ({h.account}): no price history source ({what})")
            continue
        insts[h.id] = inst
    steps = {h.id: units_by_day(h.events) for h in live if h.id in insts}
    # units known from: the first event, or a statement's opening balance (units before it are unknown)
    starts, open_starts = [], []
    for h in live:
        if h.id not in insts:
            continue
        first = min(e.day for e in h.events)
        starts.append(first)
        firsts = [e for e in h.events if e.day == first]
        if any(e.kind == "opening" for e in firsts):
            open_starts.append((first, h.name))
    if not starts:
        hist.reason = "no holding has a price history source: " + "; ".join(hist.excluded)
        return hist
    start = min(starts)
    hist.start_reason = f"first transaction on {start.isoformat()}"
    if open_starts:
        d, name = max(open_starts)
        if d > start:
            start = d
            hist.start_reason = (f"{name} starts with a statement's opening balance on {d.isoformat()}: units "
                                 "before that date are unknown, so the series starts there")  # fmt: skip
    from finresearch.fincalc.dates import add_years

    floor = add_years(today, -MAX_YEARS)
    if start < floor:
        start = floor
        hist.start_reason = f"limited to the last {MAX_YEARS} years"
    # --- prices
    risk_from = today - timedelta(days=RISK_LOOKBACK_DAYS)
    need: dict[str, tuple[Instrument, date, date]] = {}
    for h in live:
        inst = insts.get(h.id)
        if inst is None:
            continue
        first = max(start, min(e.day for e in h.events)) - timedelta(days=10)
        held_now = units_on(steps[h.id], today) > 1e-6
        last_event = max(e.day for e in h.events)
        lo = min(first, risk_from) if held_now else first
        hi = today if held_now else min(today, last_event + timedelta(days=10))
        if inst.key in need:
            _, a, b = need[inst.key]
            lo, hi = min(lo, a), max(hi, b)
        need[inst.key] = (inst, lo, hi)
    splits = getattr(fetch, "split_days", None)
    if isinstance(
        splits, dict
    ):  # the Fetcher keeps Groww off ranges a split/bonus could make ambiguous (#267)
        for h in live:
            if h.id in insts:
                splits.setdefault(insts[h.id].key, set()).update(unit_factors(h.events))
    closes: dict[str, dict[date, float]] = {}
    for key, (inst, lo, hi) in need.items():
        try:
            closes[key] = await store.get(key, lo, hi, lambda a, b, inst=inst: fetch(inst, a, b), today)
        except Exception as e:  # one instrument's failure never fails the build: it is excluded and named
            log.warning("price history for %s failed: %s", key, e)
            closes[key] = {}
    for h in live:
        inst = insts.get(h.id)
        if inst is not None and not closes.get(inst.key):
            hist.excluded.append(f"{h.name} ({h.account}): no price history returned for {inst.key}")
            insts.pop(h.id)
    used = [h for h in live if h.id in insts]
    if not used:
        hist.reason = "no price history could be read: " + "; ".join(hist.excluded)
        return hist
    hist.closes = {k: v for k, v in closes.items() if v}
    # --- calendar: every day any used instrument has a price, from the start to today
    cal = sorted({d for h in used for d in closes[insts[h.id].key] if start <= d <= today})
    # the first day every holding then held has a price
    first_price = {k: min(v) for k, v in closes.items() if v}
    for i, d in enumerate(cal):
        if all(units_on(steps[h.id], d) <= 1e-6 or first_price[insts[h.id].key] <= d for h in used):
            if i > 0:
                hist.start_reason += f"; prices for every holding start on {d.isoformat()}"
            cal = cal[i:]
            break
    else:
        cal = []
    if len(cal) < 2:
        hist.reason = "fewer than two trading days of prices since the first transaction"
        return hist
    start = cal[0]
    days = cal
    n = len(days)
    value = [0.0] * n
    complete = [True] * n
    flow = [0.0] * n
    pre_value = 0.0
    for h in used:
        key = insts[h.id].key
        px = _ffill(closes[key], days)
        st = steps[h.id]
        for t, d in enumerate(days):
            u = units_on(st, d)
            if u <= 1e-6:
                continue
            p, pd = px[t]
            if p is None or (d - pd).days > STALE_DAYS:
                complete[t] = False
                if p is None:
                    continue
            value[t] += u * p
        # units held before the first day's own events, valued on day 0
        u0 = units_on(st, start - timedelta(days=1))
        if u0 > 1e-6 and px[0][0] is not None:
            pre_value += u0 * px[0][0]
        for fl in flows_of(h.events):
            if fl.day < start:
                continue
            t = _next_on_or_after(days, fl.day)
            if t is None:
                continue  # after the last priced day: counts from the next build
            if fl.cash is not None:
                flow[t] += fl.cash
            elif px[t][0] is not None:
                flow[t] += fl.units * px[t][0]
    hist.days, hist.value, hist.flow, hist.complete = days, value, flow, complete
    hist.initial = pre_value + flow[0]
    hist.flows = [(days[0], hist.initial)] + [
        (d, f) for d, f in zip(days[1:], flow[1:], strict=True) if abs(f) > 1e-9
    ]
    hist.returns = am.twr_returns(value, flow)
    hist.index = am.chain(hist.returns)
    inv, acc = [], 0.0
    for t in range(n):
        acc += hist.initial if t == 0 else flow[t]
        inv.append(acc)
    hist.invested = inv
    if not all(complete):
        hist.warnings.append(f"{complete.count(False)} day(s) used a price more than {STALE_DAYS} days old")
    # --- jumps that look like an unrecorded split or bonus
    for h in used:
        key = insts[h.id].key
        fac = unit_factors(h.events)
        hist.factors.setdefault(key, {}).update(fac)
        c = sorted((d, v) for d, v in closes[key].items() if d >= start)
        st = steps[h.id]
        for (d0, p0), (d1, p1) in pairwise(c):
            if (
                p0 > 0
                and abs(p1 / p0 - 1) > JUMP
                and not any(d0 < d <= d1 for d in fac)
                and units_on(st, d1) > 1e-6
            ):
                hist.warnings.append(f"{h.name}: price moved {p1 / p0 - 1:+.0%} on {d1.isoformat()} with no split or "
                                     "bonus recorded; check its corporate actions (Holdings → edit)")  # fmt: skip
    # --- positions at the end
    by_key: dict[str, Position] = {}
    for h in used:
        u = units_on(steps[h.id], today)
        if u <= 1e-6:
            continue
        inst = insts[h.id]
        p, pd = _ffill(closes[inst.key], [today])[0]
        pos = by_key.get(inst.key)
        if pos is None:
            pos = by_key[inst.key] = Position(inst.key, h.name, h.asset_type, h.tax_class, [], 0.0, p, pd, 0.0,
                                              h.sector, h.nse_symbol, inst.scheme)  # fmt: skip
        pos.holding_ids.append(h.id)
        pos.units += u
        pos.value += u * (p or 0.0)
    hist.positions = sorted(by_key.values(), key=lambda p: -p.value)
    hist.notes.append("Flows count at the day's close; opening balances and transfers count in kind at that day's "
                      "close; a flow on a non-trading day counts on the next trading day.")  # fmt: skip
    # --- benchmark
    if benchmark:
        hist.benchmark = await _benchmark(fetch, store, days, today)
    return hist


async def _benchmark(fetch: Any, store: PriceStore, days: Sequence[date], today: date) -> Benchmark | None:
    inst = Instrument(f"NSE:{BENCHMARK}", "nse", BENCHMARK)
    lo = min(days[0], today - timedelta(days=RISK_LOOKBACK_DAYS)) - timedelta(days=10)
    try:
        closes = await store.get(inst.key, lo, today, lambda a, b: fetch(inst, a, b), today)
    except Exception as e:
        log.warning("benchmark history failed: %s", e)
        return None
    if not closes:
        return None
    note = None
    try:
        divs, factors = parse_benchmark_actions(await _cached_actions(fetch, store, today))
    except Exception as e:
        log.warning("benchmark corporate actions failed: %s", e)
        divs, factors, note = {}, {}, "price return only: NIFTYBEES corporate actions could not be read"
    bdays = sorted(closes)
    tr = am.total_return_index(bdays, [closes[d] for d in bdays], divs, factors)
    level_by = dict(zip(bdays, tr, strict=True))
    aligned = [v for v, _ in _ffill(level_by, days)]
    if any(v is None for v in aligned):
        first = (
            next(i for i, v in enumerate(aligned) if v is not None)
            if any(v is not None for v in aligned)
            else None
        )
        if first is None:
            return None
        aligned = [aligned[first] if v is None else v for v in aligned]
        note = (note + "; " if note else "") + "the benchmark has no price before " + days[first].isoformat()
    label = BENCHMARK_LABEL if divs else "NIFTYBEES price return (no distributions found)"
    return Benchmark(list(days), [float(v) for v in aligned], closes, divs, factors, label, note)


async def _cached_actions(fetch: Any, store: PriceStore, today: date) -> list[Any]:
    """NIFTYBEES corporate actions, cached on disk for a day."""
    from types import SimpleNamespace

    path = store.root / "NIFTYBEES_actions.json"
    if path.exists():
        try:
            d = json.loads(path.read_text())
            if d.get("day") == today.isoformat():
                return [SimpleNamespace(ex_date=date.fromisoformat(a["ex"]) if a.get("ex") else None,
                                        subject=a["subject"], dividend_per_share=None) for a in d["actions"]]  # fmt: skip
        except (ValueError, KeyError):
            pass
    acts = await fetch.actions(BENCHMARK)
    store.root.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"day": today.isoformat(), "actions": [
        {"ex": a.ex_date.isoformat() if a.ex_date else None, "subject": a.subject} for a in acts]}))  # fmt: skip
    return acts


def returns_of(closes: dict[date, float], factors: dict[date, float] | None = None) -> dict[date, float]:
    """Daily unit-adjusted returns of one instrument: P_t·f_t / P_{t-1} - 1 (f: split/bonus on (t-1, t]). A one-day
    move beyond ±JUMP with no recorded action is dropped (a probable unrecorded split or bonus)."""
    factors = factors or {}
    ks = sorted(closes)
    out = {}
    for a, b in pairwise(ks):
        f = 1.0
        for d, x in factors.items():
            if a < d <= b:
                f *= x
        if closes[a] > 0:
            r = closes[b] * f / closes[a] - 1
            if abs(r) <= JUMP or f != 1.0:
                out[b] = r
    return out
