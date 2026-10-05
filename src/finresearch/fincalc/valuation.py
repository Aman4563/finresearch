"""Per-share and valuation arithmetic.

Units: ``pat``, ``net_worth``, ``amount``, ``market_cap``, ``debt``, ``cash`` must share one
rupee unit; per-share outputs are in that unit per share, so convert to **rupees** first
(``numbers.convert(x, "crore", "inr")``) when you want ₹/share. Multiples are plain numbers;
yields are fractions. ``None`` propagates; P/E and P/B return ``None`` for non-positive
EPS/BVPS (not meaningful). ValueError for negative share counts / prices and COE <= g.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Literal

from finresearch.fincalc.numbers import Num, opt_decimal, require_price, require_shares, to_decimal


def eps(pat: Num | None, shares: Num) -> Decimal | None:
    """``PAT / shares``. With PAT in rupees this is ₹/share.

    Note: RHP-printed EPS uses *weighted-average* shares (Ind AS 33); PAT / post-issue shares
    gives a "fully diluted post-issue" EPS, which is what an IPO P/E should use. Label which.
    """
    p = opt_decimal(pat)
    s = require_shares(shares)
    return None if p is None else p / s


def pe(price: Num, eps_: Num | None) -> Decimal | None:
    """``price / EPS``. None when EPS is None or <= 0 (loss-making: P/E not meaningful)."""
    pr = require_price(price)
    e = opt_decimal(eps_)
    if e is None or e <= 0:
        return None
    return pr / e


def bvps(net_worth: Num | None, shares: Num) -> Decimal | None:
    """Book value per share ``net worth / shares`` (net worth in rupees -> ₹/share)."""
    nw = opt_decimal(net_worth)
    return None if nw is None else nw / require_shares(shares)


def pb(price: Num, bvps_: Num | None) -> Decimal | None:
    """``price / BVPS``. None when BVPS is None or <= 0."""
    pr = require_price(price)
    b = opt_decimal(bvps_)
    if b is None or b <= 0:
        return None
    return pr / b


def market_cap(shares: Num, price: Num) -> Decimal:
    """``shares * price`` in rupees (use post-issue shares for IPO market cap)."""
    return require_shares(shares) * require_price(price)


def post_issue_shares(pre_shares: Num, fresh_shares: Num) -> Decimal:
    """``pre-issue shares + fresh shares``. OFS does not change the share count."""
    return require_shares(pre_shares) + require_shares(fresh_shares, "fresh_shares", allow_zero=True)


def shares_from_amount(amount: Num, price: Num, rounding: Literal["floor", "half_up"] = "floor") -> Decimal:
    """Shares issued for a rupee ``amount`` at ``price`` per share.

    Offer documents compute the fresh-issue share count as ``floor(amount / price)`` — you
    cannot issue a fraction of a share, and rounding up would exceed the authorised fresh
    issue size. E.g. ₹320 cr @ ₹272 = 11,764,705.88 -> **1,17,64,705** (Orient Cables RHP);
    ₹750 cr @ ₹34 = 220,588,235.29 -> 220,588,235 (Moneyview). ``amount`` must be in rupees.
    """
    a = to_decimal(amount)
    if a < 0:
        raise ValueError("amount must be >= 0")
    mode = {"floor": ROUND_DOWN, "half_up": ROUND_HALF_UP}[rounding]
    return (a / require_price(price)).quantize(Decimal(1), rounding=mode)


def ev(market_cap_: Num, debt: Num | None, cash: Num | None) -> Decimal | None:
    """Enterprise value ``market cap + debt - cash & equivalents``.

    For lenders/NBFCs EV is not meaningful (debt is raw material) — use P/B instead.
    """
    m, d, c = to_decimal(market_cap_), opt_decimal(debt), opt_decimal(cash)
    if d is None or c is None:
        return None
    return m + d - c


def ev_ebitda(ev_: Num | None, ebitda: Num | None) -> Decimal | None:
    """``EV / EBITDA``. None when EBITDA <= 0."""
    e, b = opt_decimal(ev_), opt_decimal(ebitda)
    if e is None or b is None or b <= 0:
        return None
    return e / b


def price_to_sales(market_cap_: Num, revenue: Num | None) -> Decimal | None:
    """``market cap / revenue`` (same unit). None when revenue <= 0."""
    m, r = to_decimal(market_cap_), opt_decimal(revenue)
    if r is None or r <= 0:
        return None
    return m / r


def earnings_yield(eps_: Num | None, price: Num) -> Decimal | None:
    """``EPS / price`` as a fraction (inverse of P/E; defined for losses too)."""
    e = opt_decimal(eps_)
    return None if e is None else e / require_price(price)


def justified_pb(roe: Num, cost_of_equity: Num, growth: Num) -> Decimal:
    """Gordon-growth justified P/B ``(ROE - g) / (COE - g)``; all inputs fractions.

    Raises ValueError unless ``COE > g`` (the model diverges otherwise).
    """
    r, k, g = to_decimal(roe), to_decimal(cost_of_equity), to_decimal(growth)
    if k <= g:
        raise ValueError("cost_of_equity must exceed growth")
    return (r - g) / (k - g)


@dataclass(frozen=True)
class FairValueRange:
    """Implied per-share value range from applying a multiple range to a per-share metric."""

    low: Decimal
    mid: Decimal
    high: Decimal
    metric: Decimal
    multiple_low: Decimal
    multiple_high: Decimal

    def upside_vs(self, price: Num) -> tuple[Decimal, Decimal, Decimal]:
        """Fractional up/downside of (low, mid, high) versus ``price``."""
        p = require_price(price)
        return (self.low / p - 1, self.mid / p - 1, self.high / p - 1)


def implied_fair_value(metric_per_share: Num, multiple_low: Num, multiple_high: Num) -> FairValueRange:
    """``metric x multiple`` range, e.g. BVPS x justified P/B band or EPS x peer P/E band.

    ``mid`` is the arithmetic midpoint. Raises ValueError if ``multiple_low > multiple_high`` or
    a multiple is negative.
    """
    m = to_decimal(metric_per_share)
    lo, hi = to_decimal(multiple_low), to_decimal(multiple_high)
    if lo < 0 or hi < 0 or lo > hi:
        raise ValueError("need 0 <= multiple_low <= multiple_high")
    return FairValueRange(m * lo, m * (lo + hi) / 2, m * hi, m, lo, hi)


# --------------------------------------------------------------------------- triangulation (roadmap §C.7)
# DCF with sensitivity, reverse DCF (Mauboussin & Rappaport, "Expectations Investing"), relative multiples and the
# EV bridge (Damodaran-style). Rates are fractions. All cash-flow inputs share one unit; per-share outputs are in
# that unit per share (convert to rupees first for ₹/share).


@dataclass(frozen=True)
class DcfValue:
    """Two-stage DCF: ``years`` of growth at ``growth``, then a Gordon terminal value growing at ``terminal_growth``."""

    pv_explicit: Decimal
    pv_terminal: Decimal
    value: Decimal

    @property
    def terminal_share(self) -> Decimal:
        """Fraction of the value that sits in the terminal value (high = the answer is mostly the TV assumption)."""
        return self.pv_terminal / self.value if self.value else Decimal(0)


def dcf(cash_flow: Num, growth: Num, discount_rate: Num, terminal_growth: Num, years: int = 10) -> DcfValue:
    """Present value of ``cash_flow`` (the latest year's FCF, not yet grown) growing at ``growth`` for ``years``,
    then a terminal value ``CF_n × (1 + g_T) / (r − g_T)`` discounted from year ``n``.

    ``CF_t = CF_0 (1+g)^t``; value = Σ_{t=1..n} CF_t/(1+r)^t + TV/(1+r)^n. Raises ValueError unless r > g_T
    (the terminal value diverges otherwise) and ``years >= 1``. With g = g_T it equals Gordon: CF_0(1+g)/(r−g).
    """
    cf, g, r, gt = (
        to_decimal(cash_flow),
        to_decimal(growth),
        to_decimal(discount_rate),
        to_decimal(terminal_growth),
    )
    if r <= gt:
        raise ValueError("discount_rate must exceed terminal_growth")
    if years < 1:
        raise ValueError("years must be >= 1")
    if r <= -1 or g <= -1:
        raise ValueError("rates must be > -100%")
    pv = Decimal(0)
    cft = cf
    disc = Decimal(1)
    for _ in range(years):
        cft *= 1 + g
        disc *= 1 + r
        pv += cft / disc
    tv = cft * (1 + gt) / (r - gt)
    pvt = tv / disc
    return DcfValue(pv, pvt, pv + pvt)


def ev_bridge(enterprise_value: Num, *, cash: Num = 0, debt: Num = 0, leases: Num = 0, nci: Num = 0,
              non_operating_assets: Num = 0, esop_value: Num = 0, fresh_issue_proceeds: Num = 0) -> Decimal:  # fmt: skip
    """Equity value = EV + cash + non-operating assets + IPO fresh-issue proceeds − debt − leases − NCI − ESOPs.

    For an IPO the fresh issue's money comes into the company (OFS money goes to the sellers, so it is excluded);
    pair the result with post-issue shares (``post_issue_shares``). Proceeds used to repay debt are neutral here:
    cash in, debt out."""
    return (to_decimal(enterprise_value) + to_decimal(cash) + to_decimal(non_operating_assets)
            + to_decimal(fresh_issue_proceeds) - to_decimal(debt) - to_decimal(leases) - to_decimal(nci)
            - to_decimal(esop_value))  # fmt: skip


def dcf_per_share(cash_flow: Num, growth: Num, discount_rate: Num, terminal_growth: Num, shares: Num, *,
                  years: int = 10, net_debt: Num = 0, fresh_issue_proceeds: Num = 0) -> Decimal:  # fmt: skip
    """``(DCF value − net debt + fresh-issue proceeds) / shares`` (net debt negative = net cash)."""
    v = dcf(cash_flow, growth, discount_rate, terminal_growth, years).value
    eq = ev_bridge(v, debt=net_debt, fresh_issue_proceeds=fresh_issue_proceeds)
    return eq / require_shares(shares)


def reverse_dcf(price: Num, shares: Num, cash_flow: Num, discount_rate: Num, terminal_growth: Num, *,
                years: int = 10, net_debt: Num = 0, fresh_issue_proceeds: Num = 0, low: Num = "-0.5",
                high: Num = "1.0", tolerance: Num = "0.0000001") -> Decimal | None:  # fmt: skip
    """The growth g* of the next ``years`` that makes the DCF value per share equal ``price`` (Mauboussin &
    Rappaport's "price-implied expectations"). Bisection: the value rises monotonically with g when the cash flow
    is positive. Returns None when the cash flow is <= 0 (no growth rate can justify a price from a negative
    base) or when g* lies outside [low, high]."""
    p, cf = require_price(price), to_decimal(cash_flow)
    if cf <= 0:
        return None
    lo, hi, tol = to_decimal(low), to_decimal(high), to_decimal(tolerance)

    def f(g: Decimal) -> Decimal:
        return dcf_per_share(cf, g, discount_rate, terminal_growth, shares, years=years, net_debt=net_debt,
                             fresh_issue_proceeds=fresh_issue_proceeds) - p  # fmt: skip

    flo, fhi = f(lo), f(hi)
    if flo > 0 or fhi < 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
        if hi - lo <= tol:
            break
    return (lo + hi) / 2


def dcf_grid(cash_flow: Num, growth: Num, shares: Num, discount_rates: list[Num], terminal_growths: list[Num], *,
             years: int = 10, net_debt: Num = 0, fresh_issue_proceeds: Num = 0) -> list[list[Decimal | None]]:  # fmt: skip
    """Per-share values for each (discount rate row × terminal growth column); None where r <= g_T."""
    out: list[list[Decimal | None]] = []
    for r in discount_rates:
        row: list[Decimal | None] = []
        for gt in terminal_growths:
            try:
                row.append(dcf_per_share(cash_flow, growth, r, gt, shares, years=years, net_debt=net_debt,
                                         fresh_issue_proceeds=fresh_issue_proceeds))  # fmt: skip
            except ValueError:
                row.append(None)
        out.append(row)
    return out


@dataclass(frozen=True)
class PeerStats:
    """Median and inter-quartile range of peer multiples, and where the company sits (0–100 percentile)."""

    n: int
    q1: Decimal
    median: Decimal
    q3: Decimal
    own: Decimal | None
    percentile: Decimal | None


def _quantile(xs: list[Decimal], q: Decimal) -> Decimal:
    """Linear interpolation between order statistics (Excel QUARTILE.INC / numpy 'linear')."""
    pos = (len(xs) - 1) * q
    i = int(pos)
    frac = pos - i
    return xs[i] if i + 1 >= len(xs) else xs[i] + (xs[i + 1] - xs[i]) * frac


def peer_distribution(
    values: list[Num], own: Num | None = None, *, positive_only: bool = False, min_n: int = 3
) -> PeerStats:
    """Quartiles of peer values and the company's percentile rank = share of peers strictly below it + half of the
    peers equal to it (0-100). `positive_only` drops non-positive values first (a loss-maker's P/E or a negative
    book's P/B is not meaningful); without it negative values count (a negative ROE or growth is real data and
    dropping it would bias the median upwards). ValueError with fewer than `min_n` values.

    The percentile only says where the company sits; whether high is good depends on the metric (a high P/E
    percentile means dearer than peers, not better)."""
    xs = sorted(x for x in (to_decimal(p) for p in values) if not positive_only or x > 0)
    if len(xs) < min_n:
        raise ValueError(f"need at least {min_n} {'positive ' if positive_only else ''}peer values")
    o = opt_decimal(own)
    pct = None
    if o is not None:
        below = sum(1 for x in xs if x < o)
        ties = sum(1 for x in xs if x == o)
        pct = Decimal(100) * (below + Decimal(ties) / 2) / len(xs)
    return PeerStats(len(xs), _quantile(xs, Decimal("0.25")), _quantile(xs, Decimal("0.5")),
                     _quantile(xs, Decimal("0.75")), o, pct)  # fmt: skip


def peer_stats(peers: list[Num], own: Num | None = None) -> PeerStats:
    """Peer multiple distribution (non-positive multiples dropped: a loss-maker's P/E is not meaningful) and the
    company's percentile rank = share of peers below it + half of ties. Roadmap §C.7 wants ≥ 3 peers; raises
    ValueError with fewer."""
    try:
        return peer_distribution(peers, own, positive_only=True)
    except ValueError:
        raise ValueError("need at least 3 positive peer multiples") from None


@dataclass(frozen=True)
class Band:
    low: Decimal
    high: Decimal
    intersection: tuple[Decimal, Decimal] | None
    disagree: bool  # the methods' midpoints differ by more than 30 %
    spread: Decimal  # (max mid − min mid) / min mid


def triangulate(ranges: dict[str, tuple[Num, Num]], disagree_above: Num = "0.30") -> Band:
    """Fair-value band from several methods' (low, high) ranges: the union, the intersection (None when they do
    not overlap) and a disagreement flag when the midpoints differ by more than ``disagree_above`` (roadmap §C.7)."""
    if not ranges:
        raise ValueError("need at least one range")
    rs = [(min(to_decimal(a), to_decimal(b)), max(to_decimal(a), to_decimal(b))) for a, b in ranges.values()]
    lo, hi = min(r[0] for r in rs), max(r[1] for r in rs)
    ilo, ihi = max(r[0] for r in rs), min(r[1] for r in rs)
    mids = [(a + b) / 2 for a, b in rs]
    spread = (max(mids) - min(mids)) / min(mids) if min(mids) > 0 else Decimal(0)
    return Band(lo, hi, (ilo, ihi) if ilo <= ihi else None, spread > to_decimal(disagree_above), spread)
