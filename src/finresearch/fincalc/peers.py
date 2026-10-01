"""Listed-stock peer metrics and the peer summary (research item #6, roadmap A9: "peer comparison with median and IQR,
not three hand-picked peers"). Pure functions: signals.stock_peers fetches the inputs and stores the result.

Every metric is a `Metric`: a value with its period and source, or None with the reason it is missing. A missing or
not-meaningful figure is never 0.

- Trailing twelve months (TTM) = the latest four consecutive quarters on ONE basis (the basis of the latest quarter:
  consolidated when the company files it, else standalone; results_from_nse reads consolidated first). A gap or a
  basis change inside the window gives no TTM rather than a mixed sum.
- Trailing P/E = price ÷ TTM basic EPS (sum of the four quarters' EPS as filed: the usual approximation, share counts
  shift a little between quarters). TTM EPS ≤ 0 -> "n/m" (fincalc.valuation.pe: a loss-maker's P/E is meaningless).
- P/B = market cap ÷ book value (equity attributable to owners on the latest filed balance sheet; total equity when
  the filing gives no owners' line, said so in the basis). Book ≤ 0 -> n/m.
- ROE = TTM profit attributable to owners ÷ average of the owners' equity at the start and end of those twelve months
  (fincalc.ratios.roe_on_average_equity). Balance sheets are filed only at half-year and year ends (SEBI LODR Reg.
  33(3)(f)), so the twelve months end on the latest balance-sheet date, which can be older than the P/E's TTM.
- Revenue and PAT growth = TTM vs the twelve months before (quarters 5-8), same basis. A non-positive base -> n/m
  (growth off a loss reads as nonsense either way).
- PAT margin = TTM profit to owners ÷ TTM revenue (fincalc.ratios.pat_margin).
- 1-year price return = price ÷ the close on the last trading day on or before the same date a year earlier - 1, the
  old close divided by every split/bonus factor that went ex in between (fincalc.signals.action_factor); dividends
  are not added back (a price return).

Ratios and returns are fractions (0.12 = 12 %); multiples are plain; money is in rupees.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import add_years
from finresearch.fincalc.numbers import Num, opt_decimal

QUARTER_GAP_DAYS = (80, 100)  # consecutive quarter ends are 89-92 days apart; allow a few days of slack
NM = "n/m"


@dataclass(frozen=True)
class Metric:
    value: Decimal | None
    period: str | None = None
    source: str | None = None
    basis: str | None = None
    reason: str | None = None  # why value is None ("n/m: ...", "no ...")

    def json(self) -> dict[str, Any]:
        d = asdict(self)
        d["value"] = None if self.value is None else float(self.value)
        return d


def missing(reason: str, period: str | None = None, source: str | None = None) -> Metric:
    return Metric(None, period, source, None, reason)


@dataclass(frozen=True)
class Quarter:
    """One filed quarter (a row of the /results route): rupees, EPS in rupees per share."""

    end: date
    revenue: Decimal | None
    profit: Decimal | None  # attributable to owners
    eps: Decimal | None
    consolidated: bool | None
    source: str | None = None  # the filing's XBRL

    @classmethod
    def from_row(cls, r: dict[str, Any]) -> Quarter:
        return cls(date.fromisoformat(r["period_end"]), opt_decimal(r.get("revenue")), opt_decimal(r.get("profit")),
                   opt_decimal(r.get("eps")), r.get("consolidated"), r.get("xbrl") or r.get("source_url"))  # fmt: skip


@dataclass(frozen=True)
class BalanceSheet:
    end: date
    equity_owners: Decimal | None
    total_equity: Decimal | None
    consolidated: bool | None
    source: str | None = None

    @property
    def book(self) -> tuple[Decimal | None, str]:
        if self.equity_owners is not None:
            return self.equity_owners, "equity attributable to owners"
        return self.total_equity, "total equity (the filing gives no owners' line; includes minority interests)"


def basis_word(consolidated: bool | None) -> str:
    return "consolidated" if consolidated else "standalone" if consolidated is False else "basis not stated"


def _consecutive(qs: list[Quarter]) -> bool:
    lo, hi = QUARTER_GAP_DAYS
    return all(lo <= (b.end - a.end).days <= hi for a, b in zip(qs, qs[1:], strict=False))


def ttm_window(quarters: list[Quarter], *, end: date | None = None) -> tuple[list[Quarter] | None, str]:
    """The latest four consecutive quarters on one basis (or the four ending on `end`), oldest first; or
    (None, reason). The basis is the latest quarter's."""
    qs = sorted(quarters, key=lambda q: q.end)
    if not qs:
        return None, "no quarterly results on file"
    basis = qs[-1].consolidated
    same = [q for q in qs if q.consolidated == basis]
    if end is not None:
        same = [q for q in same if q.end <= end]
        if not same or abs((same[-1].end - end).days) > 3:
            return None, f"no {basis_word(basis)} quarter ending {end:%d %b %Y} on file"
    win = same[-4:]
    if len(win) < 4:
        return None, f"fewer than four {basis_word(basis)} quarters on file"
    if not _consecutive(win):
        return None, f"the four latest {basis_word(basis)} quarters are not consecutive (a quarter is missing)"
    return win, ""


def ttm_label(win: list[Quarter]) -> str:
    return f"TTM to {win[-1].end:%d %b %Y} ({basis_word(win[-1].consolidated)}, four quarters)"


def ttm_sum(win: list[Quarter], field: str) -> Decimal | None:
    vals = [getattr(q, field) for q in win]
    return None if any(v is None for v in vals) else sum(vals, Decimal(0))


# --------------------------------------------------------------------------- per-company metrics
def pe_metric(price: Num | None, quarters: list[Quarter], price_asof: str | None = None) -> tuple[Metric, Metric]:
    """(TTM EPS, trailing P/E)."""
    from finresearch.fincalc.valuation import pe

    win, why = ttm_window(quarters)
    if win is None:
        return missing(why), missing(why)
    period, src = ttm_label(win), win[-1].source
    eps = ttm_sum(win, "eps")
    if eps is None:
        why = "a quarter in the TTM window has no EPS"
        return missing(why, period, src), missing(why, period, src)
    eps_m = Metric(eps, period, src, "sum of four quarterly basic EPS")
    if price is None:
        return eps_m, missing("no price", period, src)
    if eps <= 0:
        return eps_m, missing(f"{NM}: trailing EPS is {'negative' if eps < 0 else 'zero'}", period, src)
    return eps_m, Metric(pe(price, eps), f"price {price_asof or ''} ÷ EPS {period}".replace("  ", " "), src,
                         "price ÷ TTM basic EPS")  # fmt: skip


def latest_sheet(sheets: list[BalanceSheet], consolidated: bool | None) -> BalanceSheet | None:
    """The newest balance sheet on the given basis (the TTM's), else the newest on any basis."""
    same = [s for s in sheets if s.consolidated == consolidated and s.book[0] is not None]
    return max(same, key=lambda s: s.end) if same else None


def pb_metric(mcap: Num | None, sheets: list[BalanceSheet], consolidated: bool | None) -> Metric:
    from finresearch.fincalc.ratios import _div

    bs = latest_sheet(sheets, consolidated)
    if bs is None:
        return missing(f"no {basis_word(consolidated)} balance sheet on file")
    book, what = bs.book
    period = f"book at {bs.end:%d %b %Y} ({basis_word(bs.consolidated)})"
    if mcap is None:
        return missing("no market cap", period, bs.source)
    if book is None or book <= 0:
        return missing(f"{NM}: book value is not positive", period, bs.source)
    return Metric(_div(mcap, book), period, bs.source, f"market cap ÷ {what}")


def roe_metric(quarters: list[Quarter], sheets: list[BalanceSheet], consolidated: bool | None) -> Metric:
    """TTM owners' profit ending on the latest balance-sheet date ÷ average owners' equity over those 12 months."""
    from finresearch.fincalc.ratios import roe_on_average_equity

    bs = latest_sheet(sheets, consolidated)
    if bs is None:
        return missing(f"no {basis_word(consolidated)} balance sheet on file")
    start = add_years(bs.end, -1)
    opening = next((s for s in sheets if s.consolidated == bs.consolidated and abs((s.end - start).days) <= 3), None)
    if opening is None or opening.book[0] is None:
        return missing(f"no balance sheet at {start:%d %b %Y} for the opening equity", source=bs.source)
    win, why = ttm_window([q for q in quarters if q.consolidated == bs.consolidated], end=bs.end)
    if win is None:
        return missing(why, source=bs.source)
    pat = ttm_sum(win, "profit")
    period = f"TTM to {bs.end:%d %b %Y} ({basis_word(bs.consolidated)})"
    if pat is None:
        return missing("a quarter in the window has no profit figure", period, bs.source)
    avg = (opening.book[0] + bs.book[0]) / 2
    if avg <= 0:
        return missing(f"{NM}: average equity is not positive", period, bs.source)
    return Metric(roe_on_average_equity(pat, opening.book[0], bs.book[0]), period, bs.source,
                  f"TTM profit to owners ÷ average of {opening.end:%d %b %Y} and {bs.end:%d %b %Y} {bs.book[1]}")  # fmt: skip


def growth_metric(quarters: list[Quarter], field: str) -> Metric:
    """TTM `field` (revenue or profit) vs the twelve months before, same basis."""
    win, why = ttm_window(quarters)
    if win is None:
        return missing(why)
    # the prior window: the four quarters before the current window, ending exactly one quarter before it starts
    prior, why2 = ttm_window([q for q in quarters if q.end < win[0].end and q.consolidated == win[-1].consolidated])
    period, src = f"{ttm_label(win)} vs the twelve months before", win[-1].source
    if prior is None or prior[-1].consolidated != win[-1].consolidated or not _consecutive([prior[-1], win[0]]):
        return missing(f"no comparable prior twelve months ({why2 or 'basis or gap'})", period, src)
    now, before = ttm_sum(win, field), ttm_sum(prior, field)
    if now is None or before is None:
        return missing(f"a quarter has no {field} figure", period, src)
    if before <= 0:
        return missing(f"{NM}: the prior twelve months' {field} is not positive", period, src)
    return Metric((now - before) / before, period, src, f"TTM {field} ÷ prior TTM {field} - 1")


def margin_metric(quarters: list[Quarter]) -> Metric:
    from finresearch.fincalc.ratios import pat_margin

    win, why = ttm_window(quarters)
    if win is None:
        return missing(why)
    period, src = ttm_label(win), win[-1].source
    pat, rev = ttm_sum(win, "profit"), ttm_sum(win, "revenue")
    if pat is None or rev is None:
        return missing("a quarter has no revenue or profit figure", period, src)
    if rev <= 0:
        return missing(f"{NM}: revenue is not positive", period, src)
    return Metric(pat_margin(pat, rev), period, src, "TTM profit to owners ÷ TTM revenue")


def return_1y(price: Num | None, today: date, bars: list[tuple[date, Num]], actions: list[tuple[date | None, str]],
              source: str | None = None) -> Metric:  # fmt: skip
    """Price return over a year: `bars` are (day, unadjusted close) around the date a year before `today`;
    `actions` (ex-date, subject) are the corporate actions since then."""
    from finresearch.fincalc.signals import action_factor

    start = add_years(today, -1)
    old = [(d, opt_decimal(c)) for d, c in bars if d <= start and c is not None]
    if not old:
        return missing(f"no close on or before {start:%d %b %Y} on file", source=source)
    day, close = max(old)
    if (start - day).days > 10:
        return missing(f"the last close before {start:%d %b %Y} is from {day:%d %b %Y} (suspended?)", source=source)
    if price is None or close is None or close <= 0:
        return missing("no price", source=source)
    factor, applied = Decimal(1), []
    for ex, subject in {(ex, " ".join(s.lower().split())) for ex, s in actions}:  # NSE repeats some rows
        f = action_factor(subject)
        if f and ex is not None and day < ex <= today:
            factor *= Decimal(str(f))
            applied.append(f"{ex:%d %b %Y} x{f:g}")
    basis = "price ÷ close a year ago - 1 (price return, no dividends)"
    if applied:
        basis += "; old close divided by split/bonus " + ", ".join(sorted(applied))
    return Metric(Decimal(str(price)) / (close / factor) - 1, f"{day:%d %b %Y} to {today:%d %b %Y}", source, basis)


# --------------------------------------------------------------------------- peer set and summary
LEVELS = ("basic_industry", "industry", "sector", "macro")  # NSE's classification, finest first
LEVEL_LABELS = {"basic_industry": "basic industry", "industry": "industry", "sector": "sector",
                "macro": "macro-economic sector"}  # fmt: skip
MIN_PEERS = 5  # the finest level with at least this many listed peers (the company excluded) is used
MAX_PEERS = 15  # nearest by market cap: comparable scale without a hand-picked list (documented; unsourced)


def choose_level(own: dict[str, str | None], universe: list[dict[str, Any]], symbol: str,
                 min_peers: int = MIN_PEERS) -> tuple[str | None, list[dict[str, Any]]]:  # fmt: skip
    """The finest NSE classification level at which at least `min_peers` other stocks share the company's label, and
    those stocks. Falls back to the coarsest level that has any peers; (None, []) when none."""
    best: tuple[str | None, list[dict[str, Any]]] = (None, [])
    for lvl in LEVELS:
        label = own.get(lvl)
        if not label:
            continue
        peers = [u for u in universe if u.get(lvl) == label and u["symbol"] != symbol]
        if len(peers) >= min_peers:
            return lvl, peers
        if len(peers) > len(best[1]):
            best = (lvl, peers)
    return best


def nearest_by_mcap(peers: list[dict[str, Any]], own_mcap: Num | None, cap: int = MAX_PEERS) -> list[dict[str, Any]]:
    """The `cap` peers closest in market cap on a log scale (|ln(peer / own)|: twice and half as big are equally
    near); peers without a market cap go last. Without the company's own market cap, the largest `cap`."""
    import math

    def mc(p: dict[str, Any]) -> float | None:
        v = p.get("market_cap")
        return float(v) if v else None

    o = float(own_mcap) if own_mcap else None
    if o is None or o <= 0:
        return sorted(peers, key=lambda p: (mc(p) is None, -(mc(p) or 0), p["symbol"]))[:cap]
    return sorted(peers, key=lambda p: (mc(p) is None, abs(math.log(mc(p) / o)) if mc(p) else 0, p["symbol"]))[:cap]
