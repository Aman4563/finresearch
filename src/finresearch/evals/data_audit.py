"""Price audit: every price the app shows, checked against the other published figures for the same thing.

`finresearch audit prices` (read-only; a few requests per symbol through the app's polite clients) compares, per
stock and for the quote's session day D:

- NSE quote official close (`closePrice`) vs NSE's history bar for D (the same figure, published twice: exact);
- the app's display price (fincalc.price) vs that official close once the session is over;
- NSE's last trade vs its official close (an EXPECTED difference after the close, reported, never a mismatch);
- NSE previous close vs the history bar's previous close (exact) and NSE's base price vs the previous close (they
  differ only on an ex-date, reported as expected with the adjustment);
- the app's day change vs (history close - base price) (exact);
- NSE's day VWAP (`averagePrice`) vs the history bar's VWAP (0.02 %);
- market cap: issued shares x display price vs NSE's own `totalMarketCap` (0.1 %);
- NSE intraday chart: the post-close point vs the official close (exact), the last in-session sample vs the last trade
  (BSE's chart only repeats the last trade after 15:30: reported, not used as the close);
- BSE quote LTP and the app's BSE close vs BSE's history bar for D; BSE vs NSE close and previous close (cross
  exchange: 0.5 %, the two exchanges compute their closes separately, so small gaps are real, not errors);
- an index: NSE's index page value vs its history close vs the intraday post-close point (exact);
- a fund: AMFI NAVAll's latest NAV vs AMFI's NAV history for the same date (exact);
- a bond: NSE's traded-bonds list last price vs its `close` field (reported as expected: thin trading).

Statuses: ok, mismatch (beyond tolerance: a real discrepancy), expected (a documented difference, such as last trade
vs official close), pending (the figure is not published yet), no_data, error. Sources are injectable (`AuditSources`)
so the logic is tested offline with fakes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import now_ist, to_ist
from finresearch.fincalc.price import OFFICIAL_CLOSE, price_view

DEFAULT_SYMBOLS = ("INFY", "TMCV", "SBIN", "RELIANCE", "HDFCBANK", "ASTRAL", "TENNIND", "BSE:526433")
DEFAULT_INDEX = "NIFTY 50"
DEFAULT_FUND = "122639"  # Parag Parikh Flexi Cap Fund - Direct Plan - Growth (AMFI scheme code)
EXACT = Decimal("0.005")  # rupees: one published figure read twice
VWAP_TOL = Decimal("0.0002")  # 0.02 %
MCAP_TOL = Decimal("0.001")  # 0.1 %
CROSS_TOL = Decimal("0.005")  # 0.5 % between NSE and BSE


@dataclass
class Check:
    subject: str
    check: str
    a_label: str
    a: str | None
    b_label: str
    b: str | None
    status: str
    tolerance: str = ""
    note: str = ""


@dataclass
class AuditReport:
    at: str
    checks: list[Check] = field(default_factory=list)

    @property
    def mismatches(self) -> list[Check]:
        return [c for c in self.checks if c.status == "mismatch"]

    def as_json(self) -> dict[str, Any]:
        return {"at": self.at, "checks": [asdict(c) for c in self.checks],
                "summary": {s: sum(1 for c in self.checks if c.status == s)
                            for s in ("ok", "mismatch", "expected", "pending", "no_data", "error")}}  # fmt: skip

    def markdown(self) -> str:
        head = "| Subject | Check | A | B | Status | Note |\n|---|---|---|---|---|---|\n"
        rows = [f"| {c.subject} | {c.check} | {c.a_label}: {c.a or '—'} | {c.b_label}: {c.b or '—'} | "
                f"**{c.status}**{f' (tol {c.tolerance})' if c.tolerance else ''} | {c.note} |" for c in self.checks]  # fmt: skip
        s = self.as_json()["summary"]
        return (f"Price audit at {self.at}: " + ", ".join(f"{v} {k}" for k, v in s.items() if v) + "\n\n" + head
                + "\n".join(rows) + "\n")  # fmt: skip


@dataclass
class AuditSources:
    """Where the audit reads from; the defaults are the app's live clients (see `live_sources`)."""

    nse_quote: Callable[[str], Awaitable[Any]]
    nse_history: Callable[[str, date, date], Awaitable[list]]
    bse_quote: Callable[[str], Awaitable[Any]]
    bse_history: Callable[[str, date, date], Awaitable[list]]
    nse_intraday: Callable[[str], Awaitable[Any]]
    bse_intraday: Callable[[str], Awaitable[Any]]
    bse_code_for_isin: Callable[[str], Awaitable[str | None]]
    index_snapshot: Callable[[str], Awaitable[dict]] | None = None
    index_history: Callable[[str, date, date], Awaitable[list]] | None = None
    index_intraday: Callable[[str], Awaitable[Any]] | None = None
    nav_all: Callable[[], Awaitable[list]] | None = None
    nav_history: Callable[[date, date], Awaitable[list]] | None = None
    bonds: Callable[[], Awaitable[list]] | None = None


def _s(v: Any) -> str | None:
    return None if v is None else str(v)


def compare(subject: str, check: str, a_label: str, a: Any, b_label: str, b: Any, *, abs_tol: Decimal | None = None,
            rel_tol: Decimal | None = None, expected_note: str | None = None, note: str = "") -> Check:  # fmt: skip
    """ok / mismatch within the tolerance; `expected_note` turns a difference into "expected" (documented)."""
    tol = f"±{abs_tol}" if abs_tol is not None else f"{rel_tol * 100}%" if rel_tol is not None else ""
    if a is None or b is None:
        return Check(subject, check, a_label, _s(a), b_label, _s(b), "no_data", tol, note)
    a, b = Decimal(str(a)), Decimal(str(b))
    diff = abs(a - b)
    within = (abs_tol is not None and diff <= abs_tol) or (
        rel_tol is not None and b != 0 and diff / abs(b) <= rel_tol
    )
    if abs_tol is None and rel_tol is None:
        within = diff == 0
    if within:
        return Check(subject, check, a_label, _s(a), b_label, _s(b), "ok", tol, note)
    gap = f"gap {a - b:+}" + (f" ({(a - b) / b * 100:+.3f}%)" if b else "")
    if expected_note:
        return Check(
            subject, check, a_label, _s(a), b_label, _s(b), "expected", tol, f"{gap}; {expected_note}"
        )
    return Check(
        subject,
        check,
        a_label,
        _s(a),
        b_label,
        _s(b),
        "mismatch",
        tol,
        f"{gap}. {note}".strip(". ") + ("." if note else ""),
    )


def _bar(bars: list, day: date | None) -> Any:
    return next((b for b in bars or [] if b.day == day), None)


def _post_close(series: Any) -> Any:
    from finresearch.api.intraday import post_close

    return post_close(getattr(series, "ticks", None)) if series is not None else None


def _in_session_last(series: Any) -> Any:
    from datetime import time

    ticks = [
        t for t in getattr(series, "ticks", None) or [] if t.at.time() <= time(15, 30, 59) and t.phase != "PO"
    ]
    return ticks[-1].price if ticks else None


async def _try(checks: list[Check], subject: str, what: str, make: Callable[[], Awaitable[Any]]) -> Any:
    try:
        return await make()
    except Exception as e:  # one source down must not end the audit
        checks.append(
            Check(subject, what, "source", None, "", None, "error", note=f"{type(e).__name__}: {e}"[:200])
        )
        return None


async def audit_nse_stock(src: AuditSources, sym: str, checks: list[Check]) -> None:
    q = await _try(checks, sym, "NSE quote", lambda: src.nse_quote(sym))
    if q is None:
        return
    v = price_view(q, exchange="NSE")
    day = v.day
    over = v.session != "open"
    bars = (
        await _try(checks, sym, "NSE history", lambda: src.nse_history(sym, day - timedelta(days=10), day))
        if day
        else None
    )
    bar = _bar(bars, day)
    if v.official_close is None:
        checks.append(Check(sym, "NSE official close published", "closePrice", None, "session", v.session,
                            "pending" if over else "ok", note="in session: the display price is the last trade"))  # fmt: skip
    else:
        checks.append(compare(sym, "NSE quote close = NSE history close", "quote closePrice", v.official_close,
                              f"history {day}", bar.close if bar else None, abs_tol=EXACT))  # fmt: skip
    if over and v.official_close is not None:
        checks.append(compare(sym, "app display price = official close", f"app ({v.label})", v.price,
                              "official close", v.official_close, abs_tol=EXACT))  # fmt: skip
        checks.append(compare(sym, "last trade vs official close", "lastPrice", v.last_traded, "closePrice",
                              v.official_close, abs_tol=EXACT,
                              expected_note="the official close is computed from the closing trades; the app shows the "
                                            "close and notes the last trade"))  # fmt: skip
    if bar is not None:
        checks.append(compare(sym, "NSE previous close = history previous close", "quote previousClose",
                              q.previous_close, "history prevClose", bar.prev_close, abs_tol=EXACT))  # fmt: skip
        if bar.vwap is not None and getattr(q, "average_price", None) is not None and over:
            checks.append(compare(sym, "NSE day VWAP = history VWAP", "quote averagePrice", q.average_price,
                                  "history vwap", bar.vwap, rel_tol=VWAP_TOL))  # fmt: skip
        if over and v.change is not None and v.reference is not None and bar.close is not None:
            checks.append(compare(sym, "app day change = history close − reference", "app change", v.change,
                                  f"{bar.close} − {v.reference}", bar.close - v.reference, abs_tol=EXACT,
                                  note=f"reference = {v.reference_kind}"))  # fmt: skip
    if q.previous_close is not None and getattr(q, "base_price", None) is not None:
        checks.append(compare(sym, "base price vs previous close", "basePrice", q.base_price, "previousClose",
                              q.previous_close, abs_tol=EXACT,
                              expected_note="a corporate action goes ex today; the app measures the change from "
                                            "the adjusted base price"))  # fmt: skip
    if q.issued_shares and v.price and getattr(q, "exchange_market_cap", None) and v.kind == OFFICIAL_CLOSE:
        mine = (q.issued_shares * v.price).quantize(Decimal(1))
        checks.append(compare(sym, "market cap = NSE totalMarketCap", "shares × display price", mine,
                              "NSE totalMarketCap", q.exchange_market_cap.quantize(Decimal(1)), rel_tol=MCAP_TOL))  # fmt: skip
    series = await _try(checks, sym, "NSE intraday", lambda: src.nse_intraday(sym))
    if series is not None and over:
        checks.append(compare(sym, "NSE intraday post-close point = official close", "chart after 15:30",
                              _post_close(series), "closePrice", v.official_close, abs_tol=EXACT))  # fmt: skip
        checks.append(compare(sym, "NSE intraday last session sample = last trade", "chart ≤15:30:59",
                              _in_session_last(series), "lastPrice", v.last_traded, abs_tol=EXACT,
                              note="one sample a minute: a trade in the last seconds can be missed"))  # fmt: skip
    isin = getattr(q, "isin", None)
    code = await _try(checks, sym, "BSE code by ISIN", lambda: src.bse_code_for_isin(isin)) if isin else None
    if code:
        await audit_bse(
            src, code, checks, subject=f"{sym} (BSE {code})", nse_view=v, nse_prev=q.previous_close
        )


async def audit_bse(src: AuditSources, code: str, checks: list[Check], *, subject: str | None = None,
                    nse_view: Any = None, nse_prev: Any = None) -> None:  # fmt: skip
    subject = subject or f"BSE:{code}"
    q = await _try(checks, subject, "BSE quote", lambda: src.bse_quote(code))
    if q is None:
        return
    v = price_view(q, exchange="BSE")
    day = v.day
    bars = (
        await _try(
            checks, subject, "BSE history", lambda: src.bse_history(code, day - timedelta(days=10), day)
        )
        if day
        else None
    )
    bar = _bar(bars, day)
    over = v.session != "open"
    if over:
        checks.append(compare(subject, "BSE LTP vs BSE history close", "header LTP", v.last_traded,
                              f"history {day}", bar.close if bar else None, abs_tol=EXACT,
                              expected_note="no trade in BSE's closing session: the LTP is the last trade, the app "
                                            "shows the history close"))  # fmt: skip
        checks.append(compare(subject, "app display price = BSE close", f"app ({v.label})", v.price,
                              f"history {day}", bar.close if bar else None, abs_tol=EXACT))  # fmt: skip
        series = await _try(checks, subject, "BSE intraday", lambda: src.bse_intraday(code))
        if series is not None:
            checks.append(compare(subject, "BSE intraday after 15:30 vs BSE close", "chart after 15:30",
                                  _post_close(series), f"history {day}", bar.close if bar else None, abs_tol=EXACT,
                                  expected_note="BSE's chart repeats the last trade after the close; the app ends a "
                                                "BSE series on its last trade and takes the close from the history"))  # fmt: skip
    prev_bar = next((b for b in reversed(bars or []) if day and b.day < day), None)
    if prev_bar is not None:
        checks.append(compare(subject, "BSE previous close = BSE history previous close", "header PrevClose",
                              q.previous_close, f"history {prev_bar.day}", prev_bar.close, abs_tol=EXACT))  # fmt: skip
    if nse_view is not None and over and nse_view.official_close is not None:
        checks.append(compare(subject, "BSE close vs NSE close (cross-exchange)", "BSE close", v.price,
                              "NSE close", nse_view.official_close, rel_tol=CROSS_TOL,
                              note="the exchanges compute their closes separately"))  # fmt: skip
    if nse_prev is not None:
        checks.append(compare(subject, "BSE previous close vs NSE previous close", "BSE PrevClose", q.previous_close,
                              "NSE previousClose", nse_prev, rel_tol=CROSS_TOL))  # fmt: skip


async def audit_index(src: AuditSources, name: str, checks: list[Check]) -> None:
    if src.index_snapshot is None:
        return
    snap = await _try(checks, name, "NSE index page", lambda: src.index_snapshot(name))
    if not snap:
        return
    stamp = str(snap.get("time") or "")
    try:
        day = datetime.strptime(stamp, "%d-%b-%Y %H:%M").date()
    except ValueError:
        day = now_ist().date()
    bars = (
        await _try(
            checks, name, "NSE index history", lambda: src.index_history(name, day - timedelta(days=10), day)
        )
        if src.index_history
        else None
    )
    bar = _bar(bars, day)
    prev = next((b for b in reversed(bars or []) if b.day < day), None)
    checks.append(compare(name, "index page value = index history close", f"getIndexData last ({stamp})",
                          snap.get("last"), f"history {day}", bar.close if bar else None, abs_tol=Decimal("0.005")))  # fmt: skip
    checks.append(compare(name, "index previous close = history previous close", "previousClose",
                          snap.get("previous_close"), f"history {prev.day if prev else '?'}", prev.close if prev else None,
                          abs_tol=Decimal("0.005")))  # fmt: skip
    if src.index_intraday is not None:
        series = await _try(checks, name, "NSE index intraday", lambda: src.index_intraday(name))
        if series is not None:
            checks.append(compare(name, "index intraday post-close point = index close", "chart after 15:30",
                                  _post_close(series), f"history {day}", bar.close if bar else None, abs_tol=Decimal("0.005")))  # fmt: skip


async def audit_fund(src: AuditSources, code: str, checks: list[Check]) -> None:
    if src.nav_all is None or src.nav_history is None:
        return
    subject = f"fund {code}"
    rows = await _try(checks, subject, "AMFI NAVAll", src.nav_all)
    row = next((r for r in rows or [] if r.code == code), None)
    if row is None:
        checks.append(Check(subject, "AMFI NAVAll row", "NAVAll", None, "", None, "no_data"))
        return
    subject = f"fund {code} ({row.name[:40]})"
    hist = await _try(checks, subject, "AMFI NAV history",
                      lambda: src.nav_history(row.day - timedelta(days=6), row.day)) if row.day else None  # fmt: skip
    mine = sorted((r for r in hist or [] if r.code == code and r.nav is not None), key=lambda r: r.day)
    same = next((r for r in mine if r.day == row.day), None)
    checks.append(compare(subject, f"NAVAll NAV = NAV history NAV ({row.day})", "NAVAll", row.nav,
                          "history", same.nav if same else None, abs_tol=Decimal("0.00005")))  # fmt: skip
    latest = mine[-1] if mine else None
    checks.append(Check(subject, "NAV date labelled", "NAVAll date", _s(row.day), "history latest",
                        _s(latest.day if latest else None),
                        "ok" if latest and latest.day == row.day else "mismatch" if latest else "no_data",
                        note="the fund page and portfolio show the NAV with its date"))  # fmt: skip


async def audit_bond(src: AuditSources, checks: list[Check]) -> None:
    if src.bonds is None:
        return
    rows = await _try(checks, "bonds", "NSE traded bonds", src.bonds)
    traded = sorted((b for b in rows or [] if b.last_price), key=lambda b: -(b.traded_value or 0))
    if not traded:
        checks.append(Check("bonds", "a traded bond", "list", None, "", None, "no_data"))
        return
    b = traded[0]
    checks.append(compare(f"bond {b.symbol} {b.series} ({b.isin})", "list last price vs list close", "ltP", b.last_price,
                          "close", b.close, abs_tol=EXACT,
                          expected_note="bonds trade thinly; the app prices a bond on its last trade and says so"))  # fmt: skip


async def run_audit(src: AuditSources, symbols: tuple[str, ...] | list[str] = DEFAULT_SYMBOLS, *,
                    index: str | None = DEFAULT_INDEX, fund: str | None = DEFAULT_FUND, bond: bool = True,
                    at: datetime | None = None) -> AuditReport:  # fmt: skip
    from finresearch.adapters.bse_equity import scrip_code_of

    report = AuditReport(at=to_ist(at or now_ist()).strftime("%Y-%m-%d %H:%M IST"))
    for sym in symbols:
        code = scrip_code_of(sym)
        if code:
            await audit_bse(src, code, report.checks)
        else:
            await audit_nse_stock(src, sym.upper(), report.checks)
    if index:
        await audit_index(src, index, report.checks)
    if fund:
        await audit_fund(src, fund, report.checks)
    if bond:
        await audit_bond(src, report.checks)
    return report


def live_sources(nse: Any, bse_client: Any, amfi: Any) -> AuditSources:
    """The app's live clients (one NseClient, one BseClient, one AmfiClient), read-only."""
    from finresearch.adapters.bse_equity import BseEquity
    from finresearch.adapters.bse_intraday import bse_intraday
    from finresearch.adapters.nse_bonds import live_bonds
    from finresearch.adapters.nse_equity import NseEquity
    from finresearch.adapters.nse_intraday import equity_intraday, index_intraday, index_snapshot

    eq, beq = NseEquity(nse), BseEquity(bse_client)
    scrips: dict[str, str] = {}

    async def code_for(isin: str) -> str | None:
        if not scrips:
            scrips.update({s.isin: s.code for s in await beq.scrips()})
        return scrips.get((isin or "").upper())

    async def fresh(fn, *a):  # every NSE page family wants its own referer warm-up
        nse._warmed = False
        return await fn(*a)

    return AuditSources(
        nse_quote=lambda s: nse.quote(s), nse_history=lambda s, a, b: fresh(eq.history, s, a, b),
        bse_quote=beq.quote, bse_history=beq.history,
        nse_intraday=lambda s: fresh(equity_intraday, nse, s), bse_intraday=lambda c: bse_intraday(bse_client, c),
        bse_code_for_isin=code_for,
        index_snapshot=lambda n: fresh(index_snapshot, nse, n), index_history=lambda n, a, b: fresh(eq.index_history, n, a, b),
        index_intraday=lambda n: fresh(index_intraday, nse, n),
        nav_all=amfi.nav_all, nav_history=lambda a, b: amfi.history(a, b),
        bonds=lambda: fresh(live_bonds, nse),
    )  # fmt: skip


async def audit_live(symbols: list[str] | None = None, **kw: Any) -> AuditReport:
    from finresearch.adapters.amfi import AmfiClient
    from finresearch.adapters.bse import BseClient
    from finresearch.adapters.nse import NseClient

    async with NseClient() as nse, BseClient() as bse, AmfiClient() as amfi:
        return await run_audit(live_sources(nse, bse, amfi), tuple(symbols or DEFAULT_SYMBOLS), **kw)
