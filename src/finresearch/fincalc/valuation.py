"""Per-share and valuation arithmetic.

Units: ``pat``, ``net_worth``, ``amount``, ``market_cap``, ``debt``, ``cash`` must share one
rupee unit; per-share outputs are in that unit per share, so convert to **rupees** first
(``numbers.convert(x, "crore", "inr")``) when you want ₹/share. Multiples are plain numbers;
yields are fractions. ``None`` propagates; P/E and P/B return ``None`` for non-positive
EPS/BVPS (not meaningful). ValueError for negative share counts / prices and COE <= g.

The valuation toolkit (#504: discount rate, explicit DCF and its checks, projected cash flows, lender methods)
returns small dataclasses that carry their inputs and constants, so a claim can cite one figure of a call by its
``result_key``; a figure that cannot be computed is ``None`` with the reason in the result's ``unknown``.
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
    """Median, inter-quartile range and full range of peer multiples, and where the company sits (0–100
    percentile). `min` and `max` are of the values counted in `n` (#504: non-positive values are already dropped
    when the caller asked for positive ones only). The five-number summary (minimum, quartiles, maximum) is after
    the comps-analysis skill of anthropics/financial-services (Apache-2.0), rewritten."""

    n: int
    q1: Decimal
    median: Decimal
    q3: Decimal
    own: Decimal | None
    percentile: Decimal | None
    min: Decimal
    max: Decimal


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
                     _quantile(xs, Decimal("0.75")), o, pct, xs[0], xs[-1])  # fmt: skip


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


# --------------------------------------------------------------------------- discount rate (#504)
# The cost of capital built from its parts, so the rate in a DCF is a calculation on cited claims and never a
# free-hand number. Every rate is an annual fraction (0.065 = 6.5 %); a figure of 1 or more is refused as a percent
# typed where a fraction belongs. Idea after the dcf-model skill of anthropics/financial-services (Apache-2.0),
# rewritten from the published formulas.


def _rate(x: Num, name: str, *, low: Decimal = Decimal(-1), low_ok: bool = False) -> Decimal:
    """A rate as a fraction in (low, 1), or [low, 1) with `low_ok`; ValueError otherwise."""
    d = to_decimal(x)
    if d >= 1:
        raise ValueError(f"{name} must be a fraction (0.065 for 6.5 %), got {d}")
    if d < low or (d == low and not low_ok):
        raise ValueError(f"{name} must be {'>=' if low_ok else '>'} {low}, got {d}")
    return d


@dataclass(frozen=True)
class CostOfEquity:
    """CAPM cost of equity with the three inputs it was built from."""

    risk_free: Decimal
    beta: Decimal
    equity_risk_premium: Decimal
    cost_of_equity: Decimal


def cost_of_equity_capm(risk_free: Num, beta: Num, equity_risk_premium: Num) -> CostOfEquity:
    """CAPM cost of equity ``risk_free + beta x equity_risk_premium`` (Sharpe 1964); fractions; cite `cost_of_equity`.

    Sharpe, "Capital Asset Prices: A Theory of Market Equilibrium under Conditions of Risk", Journal of Finance
    19(3), 1964. The equity risk premium is the caller's ASSUMPTION claim (India has no official figure): nothing is
    defaulted here. A negative beta is allowed (the formula holds); a premium <= 0 is refused, it is not a premium.
    """
    rf, b = _rate(risk_free, "risk_free"), to_decimal(beta)
    erp = _rate(equity_risk_premium, "equity_risk_premium", low=Decimal(0))
    return CostOfEquity(rf, b, erp, rf + b * erp)


@dataclass(frozen=True)
class CostOfDebt:
    """After-tax cost of debt with its inputs."""

    pre_tax: Decimal
    tax_rate: Decimal
    after_tax: Decimal


def after_tax_cost_of_debt(pre_tax: Num, tax_rate: Num) -> CostOfDebt:
    """After-tax cost of debt ``pre_tax x (1 - tax_rate)``: interest is tax-deductible; fractions; cite `after_tax`.

    `pre_tax` is the company's borrowing rate (the yield on its bonds, or interest / average debt); `tax_rate` the
    marginal rate the interest is deducted at. A loss-making company gets no shield yet: pass 0 and say so.
    """
    kd = _rate(pre_tax, "pre_tax", low=Decimal(0), low_ok=True)
    t = _rate(tax_rate, "tax_rate", low=Decimal(0), low_ok=True)
    return CostOfDebt(kd, t, kd * (1 - t))


# The net-cash rule, our stated choice [U]: a company with more cash than debt is discounted at its cost of equity
# (debt weight 0). Strictly its shares are a blend of the business and the cash, so the business alone is riskier
# than the shares and its rate is ABOVE the cost of equity: (Ke x E - r_cash x C) / (E - C) for net cash C. That
# needs the return on the cash as one more assumption, so the simple floor is used and the direction of the error is
# said: the rule understates the rate (overstates value) for a cash-rich company, by about 0.4 percentage points
# when net cash is 5 % of market cap (Ke 13.7 %, cash earning 6.5 %: (0.137 - 0.065 x 0.05) / 0.95 = 14.08 %).
NET_CASH_RULE = ("net debt <= 0 (net cash): the debt weight is 0 and WACC = cost of equity; the cash is not "
                 "unlevered out of the equity, which understates the rate for a cash-rich company")  # fmt: skip


@dataclass(frozen=True)
class Wacc:
    """Weighted average cost of capital on market-value weights, with its inputs, the weights and the rule used."""

    cost_of_equity: Decimal
    cost_of_debt_after_tax: Decimal
    market_cap: Decimal
    net_debt: Decimal
    debt_used: Decimal  # max(net debt, 0)
    weight_equity: Decimal
    weight_debt: Decimal
    wacc: Decimal
    net_cash: bool
    rule: str


def wacc(cost_of_equity: Num, cost_of_debt_after_tax: Num, market_cap: Num, net_debt: Num) -> Wacc:
    """WACC ``Ke x E/(E+D) + Kd(after tax) x D/(E+D)`` on market-value weights; net cash gives Ke; cite `wacc`.

    E is the market capitalisation and D the net debt, in one unit (book debt stands in for its market value, the
    usual practice when the debt is not traded; never book equity). Weighting by market values is Modigliani and
    Miller's cost of capital (American Economic Review 48(3), 1958; with the interest tax shield, 53(3), 1963).
    Net cash (net_debt <= 0): the debt weight is 0 and the answer is the cost of equity (NET_CASH_RULE, in `rule`).
    """
    ke = _rate(cost_of_equity, "cost_of_equity", low=Decimal(0))
    kd = _rate(cost_of_debt_after_tax, "cost_of_debt_after_tax", low=Decimal(0), low_ok=True)
    e, nd = to_decimal(market_cap), to_decimal(net_debt)
    if e <= 0:
        raise ValueError(f"market_cap must be > 0, got {e}")
    d = max(nd, Decimal(0))
    we, wd = e / (e + d), d / (e + d)
    rule = NET_CASH_RULE if nd <= 0 else "market-value weights: E = market cap, D = net debt"
    return Wacc(ke, kd, e, nd, d, we, wd, ke * we + kd * wd, nd <= 0, rule)


# --------------------------------------------------------------------------- explicit DCF (#504)
# Year-by-year cash flows with one terminal-value method per call, so each call has one value to cite; call it once
# per method to compare them. Conventions:
# * year-end (default): CF_t is discounted t years. Mid-year: the year's cash arrives evenly, so it is treated as
#   arriving at t - 0.5 (the mid-year adjustment of Koller, Goedhart & Wessels, "Valuation", McKinsey & Co.
#   [U: chapter and page not checked]; the terminal-value treatment below is derived here, not quoted).
# * growth method (Gordon 1962, "The Investment, Financing and Valuation of the Corporation"): TV_n = CF_n (1+g) /
#   (r - g). Under the mid-year convention its flows are mid-year too, CF_n (1+g)^k at n + k - 0.5, whose sum at
#   time 0 is TV_n / (1+r)^(n - 0.5): the terminal value is discounted n - 0.5 years, not n.
# * exit-multiple method: TV_n = multiple x the final year's metric (EBITDA, say), a price at the END of year n, so
#   it is discounted the full n years under both conventions.
# * implied figures put the two methods on one footing: with s = sqrt(1 + r) under mid-year (else 1), the growth
#   method implies the exit multiple s x TV_n / metric, and an exit value TV implies the growth
#   (TV x r - s x CF_n) / (TV + s x CF_n), the g that solves TV = s x CF_n (1+g) / (r - g).


@dataclass(frozen=True)
class DcfExplicit:
    """A DCF of year-by-year cash flows with one terminal-value method; `unknown` says why each None is None."""

    cash_flows: list[Decimal]
    discount_rate: Decimal
    mid_year: bool
    periods: list[Decimal]  # years each cash flow is discounted: t, or t - 0.5 under the mid-year convention
    pv_cash_flows: list[Decimal]
    pv_explicit: Decimal
    terminal_method: str  # "growth" or "exit_multiple"
    terminal_growth: Decimal | None
    exit_multiple: Decimal | None
    terminal_metric: Decimal | None
    terminal_value: Decimal  # at the end of the last year, before discounting
    terminal_period: Decimal  # years the terminal value is discounted
    pv_terminal: Decimal
    value: Decimal
    terminal_share: Decimal | None  # pv_terminal / value; None when the value is not positive
    implied_exit_multiple: Decimal | None  # growth method, when a positive terminal_metric is given
    implied_terminal_growth: Decimal | None  # exit-multiple method, when the final cash flow is positive
    unknown: dict[str, str]


def _series(xs: object, name: str) -> list[Decimal]:
    """A non-empty list of numbers (year 1 first); ValueError for a bare number, a string or an empty list."""
    if not isinstance(xs, list | tuple) or not xs:
        raise ValueError(f"{name} must be a non-empty list, year 1 first")
    return [to_decimal(x) for x in xs]


def dcf_explicit(cash_flows: list[Num], discount_rate: Num, terminal_growth: Num | None = None,
                 exit_multiple: Num | None = None, terminal_metric: Num | None = None,
                 mid_year: bool = False) -> DcfExplicit:  # fmt: skip
    """DCF of year-by-year `cash_flows` (year 1 first) plus a terminal value by growth OR exit multiple; cite `value`.

    Give `terminal_growth` (Gordon: CF_n (1+g) / (r - g)) or `exit_multiple` with `terminal_metric` (the final
    year's EBITDA or other metric), one per call. Returns the PV of each year, the PV of the terminal value, its
    share of the value, and the other method's implied figure: the exit multiple the growth method implies (needs
    `terminal_metric`), or the growth an exit multiple implies. `mid_year`: cash arrives at t - 0.5 (see the
    conventions above this function). A negative year is allowed. ValueError when r <= g, when the final cash flow
    is not positive under the growth method (a perpetuity of a loss is not a value), or for a non-positive multiple
    or metric under the exit method. A figure that cannot be computed is None with its reason in `unknown`.
    """
    cfs = _series(cash_flows, "cash_flows")
    r = _rate(discount_rate, "discount_rate", low=Decimal(0))
    if not isinstance(mid_year, bool):
        raise ValueError("mid_year must be true or false")
    if (terminal_growth is None) == (exit_multiple is None):
        raise ValueError(
            "give terminal_growth (growth method) or exit_multiple with terminal_metric, one per call"
        )
    n, half = len(cfs), Decimal("0.5") if mid_year else Decimal(0)
    s = (1 + r).sqrt() if mid_year else Decimal(1)  # (1 + r)^0.5: what half a year of discounting is worth
    disc, pvs = Decimal(1), []
    for cf in cfs:
        disc *= 1 + r
        pvs.append(cf * s / disc)
    metric, unknown = opt_decimal(terminal_metric), {}
    g = m = implied_m = implied_g = None
    if terminal_growth is not None:
        g = _rate(terminal_growth, "terminal_growth")
        if r <= g:
            raise ValueError("discount_rate must exceed terminal_growth")
        if cfs[-1] <= 0:
            raise ValueError("the final year's cash flow must be > 0 for the growth method: extend the forecast "
                             "to a year with a positive cash flow, or use an exit multiple")  # fmt: skip
        tv, tv_period = cfs[-1] * (1 + g) / (r - g), n - half
        pv_tv = tv * s / disc
        if metric is None:
            unknown["implied_exit_multiple"] = "terminal_metric was not given"
        elif metric <= 0:
            unknown["implied_exit_multiple"] = (
                "terminal_metric is not positive: a multiple of it means nothing"
            )
        else:
            implied_m = tv * s / metric
        unknown["implied_terminal_growth"] = (
            "applies to the exit-multiple method: here the growth is the input"
        )
    else:
        m = to_decimal(exit_multiple)
        if m <= 0:
            raise ValueError(f"exit_multiple must be > 0, got {m}")
        if metric is None:
            raise ValueError("the exit-multiple method needs terminal_metric (the final year's EBITDA, say)")
        if metric <= 0:
            raise ValueError(f"terminal_metric must be > 0 for the exit-multiple method, got {metric}")
        tv, tv_period = m * metric, Decimal(n)
        pv_tv = tv / disc
        if cfs[-1] <= 0:
            unknown["implied_terminal_growth"] = ("the final year's cash flow is not positive: no growth rate "
                                                  "turns it into this terminal value")  # fmt: skip
        else:
            implied_g = (tv * r - s * cfs[-1]) / (tv + s * cfs[-1])
        unknown["implied_exit_multiple"] = "applies to the growth method: here the multiple is the input"
    pv, value = sum(pvs, Decimal(0)), sum(pvs, Decimal(0)) + pv_tv
    if value <= 0:
        unknown["terminal_share"] = "the value is not positive: a share of it means nothing"
    return DcfExplicit(cfs, r, mid_year, [Decimal(t) - half for t in range(1, n + 1)], pvs, pv,
                       "growth" if g is not None else "exit_multiple", g, m, metric, tv, tv_period, pv_tv, value,
                       pv_tv / value if value > 0 else None, implied_m, implied_g, unknown)  # fmt: skip


# --------------------------------------------------------------------------- projected cash flows (#504)
# Unlevered free cash flow, year by year, from a revenue growth path and an EBIT margin path:
#     revenue_t = revenue_(t-1) x (1 + growth_t)         EBIT_t = revenue_t x margin_t
#     NOPAT_t   = EBIT_t - tax_t                          tax_t  = tax_rate x max(EBIT_t, 0)
#     FCF_t     = NOPAT_t + D&A_t - capex_t - change in working capital_t
# with D&A and capex as shares of revenue and the working-capital change as a share of the CHANGE in revenue (free
# cash flow to the firm as in Damodaran, "Investment Valuation", and Koller, Goedhart & Wessels, "Valuation"
# [U: chapters not checked]; the layout follows the dcf-model skill of anthropics/financial-services, rewritten).
# Every input is the caller's assumption and belongs in the ledger as an ASSUMPTION claim.
LOSS_YEAR_TAX_RULE = ("a year with negative EBIT pays no tax and gets no refund; losses carried forward are not "
                      "modelled (they would lower a later year's tax: state that as its own assumption)")  # fmt: skip


@dataclass(frozen=True)
class FcfPath:
    """Projected revenue, EBIT, tax, NOPAT, D&A, EBITDA, capex, working-capital change and FCF: one list entry per
    year (year 1 first), the inputs they came from, and the final year's figures as single values a claim can cite."""

    base_revenue: Decimal
    growth_path: list[Decimal]
    ebit_margin_path: list[Decimal]
    tax_rate: Decimal
    da_pct: Decimal
    capex_pct: Decimal
    nwc_pct_of_change: Decimal
    revenue: list[Decimal]
    ebit: list[Decimal]
    tax: list[Decimal]
    nopat: list[Decimal]
    da: list[Decimal]
    ebitda: list[Decimal]
    capex: list[Decimal]
    nwc_change: list[Decimal]
    fcf: list[Decimal]
    final_revenue: Decimal
    final_ebit: Decimal
    final_ebitda: Decimal
    final_fcf: Decimal
    tax_rule: str


def fcf_path(revenue: Num, growth_path: list[Num], ebit_margin_path: list[Num], tax_rate: Num, da_pct: Num,
             capex_pct: Num, nwc_pct_of_change: Num) -> FcfPath:  # fmt: skip
    """Year-by-year revenue, EBIT, NOPAT and free cash flow from growth and EBIT margin paths; cite `final_fcf`.

    `revenue` is the latest actual year (not yet grown); `growth_path` and `ebit_margin_path` have one entry per
    forecast year. FCF = EBIT - tax + D&A - capex - change in working capital, with `da_pct` and `capex_pct` shares
    of revenue and `nwc_pct_of_change` the share of the year's CHANGE in revenue tied up in working capital. A loss
    year pays no tax and gets no refund (`tax_rule`). Pass `fcf` to `dcf_explicit` as its cash flows; to cite one
    year's figure, pass the paths up to that year and cite `final_fcf` (or `final_revenue`, `final_ebit`,
    `final_ebitda`). ValueError for paths of different lengths or a rate that is not a fraction.
    """
    rev0 = to_decimal(revenue)
    if rev0 <= 0:
        raise ValueError(f"revenue must be > 0, got {rev0}")
    gs = [_rate(g, f"growth_path[{i}]") for i, g in enumerate(_series(growth_path, "growth_path"))]
    ms = [_rate(m, f"ebit_margin_path[{i}]")
          for i, m in enumerate(_series(ebit_margin_path, "ebit_margin_path"))]  # fmt: skip
    if len(gs) != len(ms):
        raise ValueError(
            f"need one EBIT margin for each year of growth: {len(gs)} growth rates, {len(ms)} margins"
        )
    t = _rate(tax_rate, "tax_rate", low=Decimal(0), low_ok=True)
    da_p = _rate(da_pct, "da_pct", low=Decimal(0), low_ok=True)
    cx_p = _rate(capex_pct, "capex_pct", low=Decimal(0), low_ok=True)
    wc_p = _rate(nwc_pct_of_change, "nwc_pct_of_change")
    cols: dict[str, list[Decimal]] = {k: [] for k in ("revenue", "ebit", "tax", "nopat", "da", "ebitda", "capex",
                                                      "nwc_change", "fcf")}  # fmt: skip
    prev = rev0
    for g, m in zip(gs, ms, strict=True):
        rev = prev * (1 + g)
        ebit = rev * m
        tax = t * max(ebit, Decimal(0))
        da, capex, wc = da_p * rev, cx_p * rev, wc_p * (rev - prev)
        row = {"revenue": rev, "ebit": ebit, "tax": tax, "nopat": ebit - tax, "da": da, "ebitda": ebit + da,
               "capex": capex, "nwc_change": wc, "fcf": ebit - tax + da - capex - wc}  # fmt: skip
        for k, v in row.items():
            cols[k].append(v)
        prev = rev
    return FcfPath(rev0, gs, ms, t, da_p, cx_p, wc_p, **cols, final_revenue=cols["revenue"][-1],
                   final_ebit=cols["ebit"][-1], final_ebitda=cols["ebitda"][-1], final_fcf=cols["fcf"][-1],
                   tax_rule=LOSS_YEAR_TAX_RULE)  # fmt: skip


# --------------------------------------------------------------------------- DCF sanity checks (#504)
# Four named checks on a DCF's own inputs and output (the idea of validate_dcf.py in anthropics/financial-services,
# rewritten; its bands are US-dollar numbers and are not used). A check whose input is missing is listed under
# `not_checked` with the reason and the status is then "incomplete", never "ok". The bands are OUR stated choices:
#
# 1. terminal growth >= discount rate: an ERROR, not a judgement. Gordon's CF (1+g) / (r - g) has no finite value.
# 2. terminal growth > the risk-free rate: a warning. A company growing faster than the economy for ever becomes the
#    economy, and the nominal risk-free rate is the market's stand-in for the economy's long-run nominal growth
#    (the stable-growth cap of Damodaran, "Investment Valuation" [U: chapter not checked]). The rate compared is
#    whatever the caller supplies; with this app's short risk-free rate (fincalc.riskfree: at most 364 days, no
#    term premium) the check is stricter than with a 10-year yield.
# 3. terminal share outside TERMINAL_SHARE_BAND = 0.35 to 0.75: a warning [U]. A company already in its steady state
#    (cash flow growing at g from year 1, discounted at r) has exactly ((1+g) / (1+r))^n of its value beyond year
#    n: the flows from year n + 1 on are the whole stream scaled by (1+g)^n and arriving n years later. At rupee
#    discount rates of 12-14 % and terminal growth of 3-5 % (the ranges api/accuracy.py states as its assumptions)
#    and the usual 5 to 10 forecast years that is 0.3625 ((1.03/1.14)^10) to 0.7242 ((1.05/1.12)^5): the band is
#    those corners rounded outwards. Above it the forecast years carry less of the value than they would for a
#    company that is already in its steady state, so the answer is mostly the terminal assumption; below it the
#    terminal assumptions are harsher than a steady state and need a reason. The identity is exact; choosing those
#    corners is the judgement. With `years` the benchmark for the caller's own r, g and horizon is returned too.
# 4. discount rate not above the risk-free rate, or more than DISCOUNT_RATE_MAX_OVER_RISK_FREE = 15 percentage
#    points above it: a warning. The floor is the supplied rupee risk-free rate itself (a risky rupee is never
#    worth more than a certain one), so no equity risk premium is assumed by the floor. The ceiling is a judgement
#    [U]: 15 points is a beta of 2 on a 7.5 % premium, or 1.5 on 10 %; beyond it the rate is a venture hurdle rate
#    and the DCF returns whatever the rate was set to. A band tied to the risk-free rate moves with rupee rates,
#    which a fixed range cannot.
TERMINAL_SHARE_BAND = (Decimal("0.35"), Decimal("0.75"))
DISCOUNT_RATE_MAX_OVER_RISK_FREE = Decimal("0.15")
DCF_CHECKS = ("terminal_growth_at_or_above_discount_rate", "terminal_growth_above_risk_free",
              "terminal_share_outside_band", "discount_rate_outside_band")  # fmt: skip


@dataclass(frozen=True)
class DcfFlag:
    name: str  # one of DCF_CHECKS
    level: str  # "error" or "warn"
    message: str


@dataclass(frozen=True)
class DcfChecks:
    """The DCF sanity checks: `status` is "error", "warn", "incomplete" (a check could not run) or "ok"."""

    status: str
    flags: list[DcfFlag]
    errors: int
    warnings: int
    checked: list[str]
    not_checked: dict[str, str]  # check -> why it could not run
    discount_rate: Decimal
    terminal_growth: Decimal | None
    risk_free: Decimal | None
    terminal_share: Decimal | None
    years: int | None
    spread_over_terminal_growth: Decimal | None  # r - g
    spread_over_risk_free: Decimal | None  # r - risk-free
    # ((1+g) / (1+r))^years: the terminal share of a company that grows at g from year 1 (needs `years`)
    steady_state_share: Decimal | None
    bands: dict[str, Decimal]


def _pct(x: Decimal) -> str:
    return f"{x * 100:.2f} %"


def dcf_checks(discount_rate: Num, terminal_growth: Num | None = None, risk_free: Num | None = None,
               terminal_share: Num | None = None, years: int | None = None) -> DcfChecks:  # fmt: skip
    """Sanity flags for a DCF: terminal growth vs the rate (error) and risk-free, share and rate bands; cite `errors`.

    Flags (see DCF_CHECKS and the reasoning above this function): terminal growth at or above the discount rate
    (error); terminal growth above the risk-free rate (warn); terminal share outside 0.35-0.75 (warn); discount
    rate not above the risk-free rate or more than 15 percentage points over it (warn). All inputs are fractions;
    for the exit-multiple method pass `dcf_explicit`'s implied_terminal_growth. A check whose input is not given is
    listed in `not_checked` and the status is "incomplete": it has not passed. `years` (the forecast length) adds
    `steady_state_share`. Citable numbers: `errors`, `warnings`, `spread_over_terminal_growth`.
    """
    r = _rate(discount_rate, "discount_rate", low=Decimal(0))
    g = None if terminal_growth is None else _rate(terminal_growth, "terminal_growth")
    rf = None if risk_free is None else _rate(risk_free, "risk_free", low=Decimal(0), low_ok=True)
    share = opt_decimal(terminal_share)
    if years is not None and (isinstance(years, bool) or not isinstance(years, int) or years < 1):
        raise ValueError(f"years must be a whole number >= 1, got {years!r}")
    lo, hi = TERMINAL_SHARE_BAND
    steady = ((1 + g) / (1 + r)) ** years if years is not None and g is not None and g < r else None
    missing = {"terminal_growth": g, "risk_free": rf, "terminal_share": share}
    needs = dict(zip(DCF_CHECKS, (("terminal_growth",), ("terminal_growth", "risk_free"), ("terminal_share",),
                                  ("risk_free",)), strict=True))  # fmt: skip
    flags: list[DcfFlag] = []
    not_checked = {name: " and ".join(k for k in keys if missing[k] is None) + " not given"
                   for name, keys in needs.items() if any(missing[k] is None for k in keys)}  # fmt: skip

    def flag(i: int, level: str, message: str) -> None:
        flags.append(DcfFlag(DCF_CHECKS[i], level, message))

    if g is not None and g >= r:
        flag(0, "error", f"terminal growth {_pct(g)} is not below the discount rate {_pct(r)}: the terminal value "
                         "has no finite value")  # fmt: skip
    if g is not None and rf is not None and g > rf:
        flag(1, "warn", f"terminal growth {_pct(g)} is above the risk-free rate {_pct(rf)}: growth for ever above "
                        "the economy's long-run nominal growth needs a reason")  # fmt: skip
    if share is not None and not lo <= share <= hi:
        beside = (
            "" if steady is None else f"; a company growing at {_pct(g)} throughout would have {steady:.4f}"
        )
        flag(2, "warn", f"the terminal value is {share:.4f} of the value, outside {lo}-{hi}{beside}")
    if rf is not None and not rf < r <= rf + DISCOUNT_RATE_MAX_OVER_RISK_FREE:
        flag(3, "warn", f"the discount rate {_pct(r)} is not within (0, {_pct(DISCOUNT_RATE_MAX_OVER_RISK_FREE)}] "
                        f"over the risk-free rate {_pct(rf)}")  # fmt: skip
    errors = sum(f.level == "error" for f in flags)
    status = "error" if errors else "warn" if flags else "incomplete" if not_checked else "ok"
    return DcfChecks(status, flags, errors, len(flags) - errors, [c for c in DCF_CHECKS if c not in not_checked],
                     not_checked, r, g, rf, share, years, None if g is None else r - g,
                     None if rf is None else r - rf, steady,
                     {"terminal_share_low": lo, "terminal_share_high": hi,
                      "discount_rate_max_over_risk_free": DISCOUNT_RATE_MAX_OVER_RISK_FREE})  # fmt: skip


# --------------------------------------------------------------------------- lender methods (#504)
# Banks, NBFCs and insurers: debt is their raw material, so free cash flow, EV and EBITDA do not apply (see `ev`).
# They are valued on equity: book value plus the present value of what they earn above their cost of equity, or the
# dividends they pay. `justified_pb` above is the one-line steady-state case of both. The need for these was noted
# from FinceptTerminal's feature list (AGPL-3.0: its code was not opened); built from the papers named below.
#
# Residual income (Edwards & Bell, "The Theory and Measurement of Business Income", 1961; Ohlson, "Earnings, Book
# Values, and Dividends in Equity Valuation", Contemporary Accounting Research 11(2), 1995):
#     V_0 = B_0 + sum over t of (ROE_t - Ke) x B_(t-1) / (1 + Ke)^t
# with book value rolled forward by clean surplus, B_t = B_(t-1) + earnings_t - dividends_t (Ohlson's assumption),
# dividends = payout_ratio x earnings; a loss year pays no dividend. After the forecast years, one of:
#   * terminal growth g: residual income grows at g for ever, RI_n (1+g) / (Ke - g), discounted n years;
#   * a fade: ROE moves in a straight line from the last forecast ROE to Ke over `fade_years` years (year n + k has
#     ROE_n + (Ke - ROE_n) x k / fade_years), after which nothing is earned above Ke. The straight-line fade is
#     Lee, Myers & Swaminathan's ("What is the Intrinsic Value of the Dow?", Journal of Finance 54(5), 1999), who
#     fade to the industry's median ROE; fading to Ke itself is our stricter choice [U]: competition removes the
#     whole excess return. fade_years = 0 ends it with the forecast.


@dataclass(frozen=True)
class ResidualIncome:
    """Residual-income value: one list entry per year (forecast years, then any fade years), the inputs, and why
    `continuing_value` is None when it is (`unknown`)."""

    book_value: Decimal
    cost_of_equity: Decimal
    payout_ratio: Decimal
    roe_path: list[Decimal]
    terminal_growth: Decimal | None
    fade_years: int | None
    roe: list[Decimal]  # the forecast ROEs, then the fade years' ROEs
    opening_book: list[Decimal]  # B_(t-1) of each year
    residual_income: list[Decimal]
    pv_residual_income: list[Decimal]
    closing_book: Decimal  # book value at the end of the last listed year
    pv_explicit: Decimal  # PV of the forecast years' residual income
    continuing_value: Decimal | None  # growth ending: the value at year n of all later residual income
    pv_continuing: (
        Decimal  # PV of everything after the forecast years (the growth perpetuity, or the fade years)
    )
    value: Decimal
    value_to_book: Decimal  # value / book_value: the P/B the inputs justify
    unknown: dict[str, str]


def _whole(x: object, name: str, minimum: int) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or x < minimum:
        raise ValueError(f"{name} must be a whole number >= {minimum}, got {x!r}")
    return x


def residual_income(book_value: Num, roe_path: list[Num], cost_of_equity: Num, payout_ratio: Num,
                    terminal_growth: Num | None = None, fade_years: int | None = None) -> ResidualIncome:  # fmt: skip
    """Residual-income value for a lender: book + PV of (ROE - Ke) x opening book, then growth OR a fade; cite `value`.

    `book_value` is the latest book value (total or per share); `roe_path` the forecast ROE of each year; book rolls
    forward by retained earnings (`payout_ratio` of earnings is paid out, nothing in a loss year). End the forecast
    with `terminal_growth` (residual income grows at g for ever) or `fade_years` (ROE moves in a straight line to
    the cost of equity over that many years; 0 = no excess return after the forecast), one per call. With ROE equal
    to the cost of equity throughout the value is the book value. Edwards-Bell (1961), Ohlson (1995); fractions.
    ValueError for a non-positive book value, Ke <= g, or a payout ratio outside 0-1.
    """
    b0 = to_decimal(book_value)
    if b0 <= 0:
        raise ValueError(f"book_value must be > 0 to anchor a residual-income value, got {b0}")
    roes = [_rate(x, f"roe_path[{i}]") for i, x in enumerate(_series(roe_path, "roe_path"))]
    ke, p = _rate(cost_of_equity, "cost_of_equity", low=Decimal(0)), to_decimal(payout_ratio)
    if not 0 <= p <= 1:
        raise ValueError(f"payout_ratio must be between 0 and 1, got {p}")
    if (terminal_growth is None) == (fade_years is None):
        raise ValueError(
            "give terminal_growth or fade_years (0 = no excess return after the forecast), one per call"
        )
    g = fade = None
    if terminal_growth is not None:
        g = _rate(terminal_growth, "terminal_growth")
        if ke <= g:
            raise ValueError("cost_of_equity must exceed terminal_growth")
    else:
        fade = _whole(fade_years, "fade_years", 0)
        roes = roes + [roes[-1] + (ke - roes[-1]) * k / fade for k in range(1, fade + 1)]
    n = len(roes) - (fade or 0)  # the forecast years; any fade years follow them
    book, disc, opening, ris, pvs = b0, Decimal(1), [], [], []
    for roe in roes:
        disc *= 1 + ke
        earnings, ri = roe * book, (roe - ke) * book
        opening.append(book)
        ris.append(ri)
        pvs.append(ri / disc)
        book += earnings - p * max(earnings, Decimal(0))
    pv_explicit, unknown = sum(pvs[:n], Decimal(0)), {}
    if g is not None:
        cv = ris[-1] * (1 + g) / (ke - g)
        pv_cont = cv / disc
    else:
        cv, pv_cont = None, sum(pvs[n:], Decimal(0))
        unknown["continuing_value"] = (
            "the fade years are listed one by one: there is no single continuing value"
        )
    value = b0 + pv_explicit + pv_cont
    return ResidualIncome(b0, ke, p, roes[:n], g, fade, roes, opening, ris, pvs, book, pv_explicit, cv, pv_cont,
                          value, value / b0, unknown)  # fmt: skip


@dataclass(frozen=True)
class Ddm:
    """Dividend-discount value in one or two stages, with its inputs. The stage figures are None for one stage:
    there the whole value is Gordon's formula and has no parts."""

    dividend: Decimal
    growth: Decimal
    cost_of_equity: Decimal
    high_growth: Decimal | None
    high_growth_years: int | None
    stages: int  # 1 or 2
    next_dividend: Decimal  # the dividend of year 1
    pv_high_growth: Decimal | None  # PV of the high-growth years' dividends
    pv_terminal: Decimal | None  # PV of the Gordon value at the end of the high-growth years
    value: Decimal
    terminal_share: Decimal | None


def ddm(dividend: Num, growth: Num, cost_of_equity: Num, high_growth: Num | None = None,
        high_growth_years: int | None = None) -> Ddm:  # fmt: skip
    """Dividend-discount value: Gordon ``D0 (1+g) / (Ke - g)``, or two stages with `high_growth` first; cite `value`.

    `dividend` is the latest annual dividend (D0, total or per share, not yet grown). One stage (Gordon, "The
    Investment, Financing and Valuation of the Corporation", 1962): dividends grow at `growth` for ever. Two stages:
    they grow at `high_growth` for `high_growth_years`, then at `growth`; give both or neither. For a lender next
    to `residual_income` and `justified_pb`. ValueError unless the dividend is positive (a company that pays no
    dividend has no dividend-discount value: that is unknown, not 0) and Ke > g.
    """
    d0 = to_decimal(dividend)
    if d0 <= 0:
        raise ValueError(f"dividend must be > 0, got {d0}: a dividend-discount model says nothing about a company "
                         "that pays no dividend")  # fmt: skip
    g, ke = _rate(growth, "growth"), _rate(cost_of_equity, "cost_of_equity", low=Decimal(0))
    if ke <= g:
        raise ValueError("cost_of_equity must exceed growth")
    if (high_growth is None) != (high_growth_years is None):
        raise ValueError("high_growth and high_growth_years go together: give both (two stages) or neither")
    if high_growth is None:
        return Ddm(d0, g, ke, None, None, 1, d0 * (1 + g), None, None, d0 * (1 + g) / (ke - g), None)
    gh, years = _rate(high_growth, "high_growth"), _whole(high_growth_years, "high_growth_years", 1)
    v = dcf(d0, gh, ke, g, years)  # the same two-stage arithmetic, on dividends
    return Ddm(d0, g, ke, gh, years, 2, d0 * (1 + gh), v.pv_explicit, v.pv_terminal, v.value,
               v.pv_terminal / v.value)  # fmt: skip
