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


def ttm_window(quarters: list[Quarter], *, end: date | None = None, skip: int = 0) -> tuple[list[Quarter] | None, str]:
    """The four consecutive quarters on one basis ending `skip` quarters before the latest (or ending on `end`),
    oldest first; or (None, reason). The basis is the latest quarter's."""
    qs = sorted(quarters, key=lambda q: q.end)
    if not qs:
        return None, "no quarterly results on file"
    basis = qs[-1].consolidated
    same = [q for q in qs if q.consolidated == basis]
    if end is not None:
        same = [q for q in same if q.end <= end]
        if not same or abs((same[-1].end - end).days) > 3:
            return None, f"no {basis_word(basis)} quarter ending {end:%d %b %Y} on file"
    else:
        same = same[: len(same) - skip] if skip else same
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
