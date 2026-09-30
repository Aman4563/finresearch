"""Fund look-through arithmetic: overlap, active share, style drift and the whole portfolio's true exposure.

All weights are PERCENT (7.5 means 7.5 %), keyed by ISIN; money is Decimal. Pure functions, no I/O.

Formulas
- Portfolio overlap between schemes A and B (SEBI Master Circular for Mutual Funds, Annexure 1A, "Methodology for
  Portfolio Overlapping", circular of 26-Feb-2026): weight of each scrip = investment in that ISIN as % of the
  scheme's AUM; only scrips common to both count; overlap = Σ_i min(w_iA, w_iB). SEBI's worked example gives 45 %
  (golden test). The app applies it to the equity holdings (Indian + foreign, hedged arbitrage excluded) because that
  is what the equity-vs-equity overlap limits are about; the common-scrip count is shown alongside.
- Active share (Cremers & Petajisto 2009, RFS 22(9)) = ½ Σ_i |w_i,fund − w_i,index| over the union of holdings, both
  sides re-scaled to 100 % equity so cash does not count as "active". Descriptive only: its power to predict returns
  is disputed (Frazzini, Friedman & Pomorski 2016, FAJ 72(2)) [U: papers not re-read].
- Holdings drift between two months = ½ Σ_i |w_i,t − w_i,t−1| on equity weights re-scaled to 100 %: 0 = the same
  portfolio, 100 = nothing in common. A month-end proxy for turnover plus price moves, not the fund's reported
  turnover ratio.
- Look-through exposure to stock s = direct_s + Σ_f V_f · w_{f,s} / 100 (V_f = the value of the user's units in
  fund f). Every rupee is assigned: stocks, the fund's non-equity lines by kind, and the remainder
  V_f · (100 − Σ ISIN weights) / 100 as "cash, TREPS & net receivables", so the buckets add up exactly to the
  portfolio's value (golden test).
- Redundancy = share of look-through equity in stocks reached through two or more routes (two funds, or a fund and
  a direct holding).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal(0)
HUNDRED = Decimal(100)
HALF = Decimal("0.5")

EQUITY_KINDS = frozenset({"equity", "foreign_equity"})
BUCKET_LABEL = {
    "equity": "Indian equity",
    "foreign_equity": "Foreign equity",
    "arbitrage": "Arbitrage (hedged equity)",
    "reit_invit": "REITs & InvITs",
    "debt": "Bonds & money market",
    "govt": "Government securities",
    "mf_units": "Mutual fund units (not looked through)",
    "other": "Other instruments",
    "remainder": "Cash, TREPS & net receivables",
    "no_file": "Funds without a holdings file",
    "not_equity_direct": "Other direct holdings",
}


# ----------------------------------------------------------------------------------------------- weights
def normalise(weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Re-scale to add up to 100 (an all-cash or empty portfolio stays empty)."""
    total = sum((w for w in weights.values() if w > 0), ZERO)
    if total <= 0:
        return {}
    return {k: w * HUNDRED / total for k, w in weights.items() if w > 0}


def combine(pairs: Iterable[tuple[str, Decimal]]) -> dict[str, Decimal]:
    """ISIN -> total weight (one ISIN can appear on two lines, e.g. listed and locked-in shares)."""
    out: dict[str, Decimal] = defaultdict(Decimal)
    for k, w in pairs:
        out[k] += w
    return dict(out)


@dataclass(frozen=True)
class Overlap:
    overlap: Decimal  # percent, Σ min(wA, wB)
    common: int  # number of common ISINs
    items: list[tuple[str, Decimal, Decimal, Decimal]]  # (isin, wA, wB, min), largest min first
    weight_a: Decimal  # Σ wA over everything compared (context: an 80 %-equity fund can overlap at most 80 %)
    weight_b: Decimal


def overlap(a: Mapping[str, Decimal], b: Mapping[str, Decimal]) -> Overlap:
    """SEBI Annexure 1A overlap: Σ over common ISINs of min(w_A, w_B), weights in % of each scheme's AUM."""
    items = [(k, a[k], b[k], min(a[k], b[k])) for k in a.keys() & b.keys() if a[k] > 0 and b[k] > 0]
    items.sort(key=lambda x: (-x[3], x[0]))
    return Overlap(
        sum((x[3] for x in items), ZERO), len(items), items, sum(a.values(), ZERO), sum(b.values(), ZERO)
    )


def overlap_matrix(funds: Mapping[str, Mapping[str, Decimal]]) -> dict[tuple[str, str], Overlap]:
    """Every unordered pair (i < j in the given order) of the funds."""
    keys = list(funds)
    return {(x, y): overlap(funds[x], funds[y]) for i, x in enumerate(keys) for y in keys[i + 1 :]}


def active_share(fund: Mapping[str, Decimal], index: Mapping[str, Decimal]) -> Decimal | None:
    """½ Σ |w_fund − w_index| on both sides re-scaled to 100 % equity; None when either side is empty."""
    f, b = normalise(fund), normalise(index)
    if not f or not b:
        return None
    return HALF * sum((abs(f.get(k, ZERO) - b.get(k, ZERO)) for k in f.keys() | b.keys()), ZERO)


def drift(prev: Mapping[str, Decimal], curr: Mapping[str, Decimal]) -> Decimal | None:
    """Holdings drift between two months: ½ Σ |w_t − w_t−1| on equity re-scaled to 100 % (0 same, 100 disjoint)."""
    return active_share(curr, prev)


def hhi(weights: Mapping[str, Decimal]) -> tuple[Decimal, Decimal] | None:
    """(HHI on fractions, effective number of holdings 1/HHI) of weights re-scaled to 100 %."""
    w = normalise(weights)
    if not w:
        return None
    h = sum(((x / HUNDRED) ** 2 for x in w.values()), ZERO)
    return h, (Decimal(1) / h if h > 0 else ZERO)


# ----------------------------------------------------------------------------------------------- look-through
@dataclass(frozen=True)
class FundLine:
    isin: str
    name: str
    weight: Decimal  # % of the fund's net assets
    kind: str  # adapters.amc_portfolio kinds
    industry: str | None = None


@dataclass(frozen=True)
class FundInput:
    source: str  # label shown as the route ("Parag Parikh Flexi Cap Fund")
    value: Decimal  # value of the user's units
    lines: list[FundLine] | None  # None: no holdings file for this fund


@dataclass(frozen=True)
class DirectInput:
    key: str  # ISIN, else the exchange symbol
    name: str
    value: Decimal
    sector: str | None = None
    equity: bool = True  # False: SGB, other listed non-equity: kept in its own bucket
    cap: str | None = None  # fallback cap bucket when the ISIN is not on the AMFI list


@dataclass
class StockExposure:
    key: str
    name: str
    value: Decimal = ZERO
    sector: str | None = None
    cap: str = "Unclassified"
    kind: str = "equity"
    routes: dict[str, Decimal] = field(default_factory=dict)


@dataclass
class Exposure:
    total: Decimal
    stocks: list[StockExposure]  # equity only, largest first
    buckets: dict[
        str, Decimal
    ]  # every rupee: equity kinds + non-equity kinds + remainder + no_file + other direct
    sectors: dict[str, Decimal]  # equity only
    caps: dict[str, Decimal]  # equity only
    equity: Decimal
    redundancy: Decimal | None  # percent of look-through equity reached by ≥ 2 routes

    def check(self) -> Decimal:
        """Σ buckets − total: exactly zero when every rupee is assigned (the reconciliation test)."""
        return sum(self.buckets.values(), ZERO) - self.total


def lookthrough(direct: Iterable[DirectInput], funds: Iterable[FundInput],
                cap_of: Callable[[str], str | None] | None = None, direct_label: str = "Direct") -> Exposure:  # fmt: skip
    """Aggregate direct holdings and funds' published holdings into stock, sector, cap and asset-bucket exposure.

    `cap_of(isin)` returns 'Large Cap' / 'Mid Cap' / 'Small Cap' from AMFI's list, or None; foreign equity is 'Foreign'.
    Sector labels come from the funds' files (SEBI/AMFI industry names) when any fund holds the stock, else from the
    direct holding's own sector, so one company is not split across two naming schemes."""
    direct, funds = list(direct), list(funds)
    stocks: dict[str, StockExposure] = {}
    buckets: dict[str, Decimal] = defaultdict(Decimal)
    fund_industry: dict[str, str] = {}
    for f in funds:
        for ln in f.lines or []:
            if ln.kind in EQUITY_KINDS and ln.industry and ln.isin not in fund_industry:
                fund_industry[ln.isin] = ln.industry

    def cap(key: str, kind: str, fallback: str | None) -> str:
        if kind == "foreign_equity":
            return "Foreign"
        got = cap_of(key) if cap_of else None
        return got or fallback or "Unclassified"

    def add(
        key: str, name: str, value: Decimal, route: str, kind: str, sector: str | None, cap_fb: str | None
    ) -> None:
        s = stocks.get(key)
        if s is None:
            s = stocks[key] = StockExposure(key, name, sector=fund_industry.get(key) or sector,
                                            cap=cap(key, kind, cap_fb), kind=kind)  # fmt: skip
        s.value += value
        s.routes[route] = s.routes.get(route, ZERO) + value
        buckets[kind] += value

    for d in direct:
        if d.equity:
            kind = "foreign_equity" if len(d.key) == 12 and not d.key.startswith("IN") else "equity"
            add(d.key, d.name, d.value, direct_label, kind, d.sector, d.cap)
        else:
            buckets["not_equity_direct"] += d.value
    for f in funds:
        if f.lines is None:
            buckets["no_file"] += f.value
            continue
        assigned = ZERO
        for ln in f.lines:
            v = f.value * ln.weight / HUNDRED
            assigned += v
            if ln.kind in EQUITY_KINDS:
                add(ln.isin, ln.name, v, f.source, ln.kind, ln.industry, None)
            else:
                buckets[ln.kind] += v
        buckets["remainder"] += f.value - assigned  # exact: whatever the ISIN lines did not cover

    total = sum((d.value for d in direct), ZERO) + sum((f.value for f in funds), ZERO)
    ordered = sorted(stocks.values(), key=lambda s: (-s.value, s.key))
    equity = sum((s.value for s in ordered), ZERO)
    sectors: dict[str, Decimal] = defaultdict(Decimal)
    caps: dict[str, Decimal] = defaultdict(Decimal)
    for s in ordered:
        sectors[s.sector or "Unclassified"] += s.value
        caps[s.cap] += s.value
    multi = sum((s.value for s in ordered if sum(1 for v in s.routes.values() if v > 0) >= 2), ZERO)
    return Exposure(total=total, stocks=ordered, buckets=dict(buckets),
                    sectors=dict(sorted(sectors.items(), key=lambda kv: -kv[1])),
                    caps=dict(sorted(caps.items(), key=lambda kv: -kv[1])), equity=equity,
                    redundancy=(multi * HUNDRED / equity) if equity > 0 else None)  # fmt: skip


# ----------------------------------------------------------------------------------------------- style over months
@dataclass(frozen=True)
class MonthStyle:
    month: str
    equity_pct: Decimal  # Indian + foreign equity, % of net assets
    caps: dict[str, Decimal]  # % of equity
    sectors: dict[str, Decimal]  # % of equity, largest first
    drift: Decimal | None  # vs the previous month in the series (None for the first)
    holdings: int


def style_series(months: list[tuple[str, list[FundLine]]], cap_of: Callable[[str], str | None] | None = None
                 ) -> list[MonthStyle]:  # fmt: skip
    """One fund's month-by-month style from its published holdings (months in any order; returned oldest first)."""
    out: list[MonthStyle] = []
    prev: dict[str, Decimal] | None = None
    for month, lines in sorted(months, key=lambda m: m[0]):
        eq = combine((ln.isin, ln.weight) for ln in lines if ln.kind in EQUITY_KINDS)
        total = sum(eq.values(), ZERO)
        caps: dict[str, Decimal] = defaultdict(Decimal)
        sectors: dict[str, Decimal] = defaultdict(Decimal)
        for ln in lines:
            if ln.kind not in EQUITY_KINDS or total <= 0:
                continue
            share = ln.weight * HUNDRED / total
            bucket = (
                "Foreign"
                if ln.kind == "foreign_equity"
                else ((cap_of(ln.isin) if cap_of else None) or "Unclassified")
            )
            caps[bucket] += share
            sectors[ln.industry or "Unclassified"] += share
        out.append(MonthStyle(month, total, dict(caps), dict(sorted(sectors.items(), key=lambda kv: -kv[1])),
                              drift(prev, eq) if prev is not None else None, len(eq)))  # fmt: skip
        prev = eq
    return out
