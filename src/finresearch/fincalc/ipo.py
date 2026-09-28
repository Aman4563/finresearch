"""IPO issue arithmetic: issue size, reservation split, subscription, allotment odds, lock-ins.

Amounts are in **rupees** unless stated; share counts are whole numbers. Fractions are
returned for rates (``0.0709`` = 7.09%). Regulatory references are to the SEBI (Issue of
Capital and Disclosure Requirements) Regulations, 2018 ("ICDR"), as amended — verify the
current text / the specific RHP before relying on a default.
"""

from __future__ import annotations

import calendar
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal

from finresearch.fincalc.numbers import Num, opt_decimal, require_price, require_shares, to_decimal

# --- issue size -----------------------------------------------------------------------------


@dataclass(frozen=True)
class IssueMath:
    """Fresh / OFS / total shares and rupee amounts at one price."""

    price: Decimal
    fresh_shares: Decimal
    ofs_shares: Decimal
    total_shares: Decimal
    fresh_amount: Decimal
    ofs_amount: Decimal
    total_amount: Decimal


def issue_math(price: Num, fresh_shares: Num, ofs_shares: Num) -> IssueMath:
    """Amounts = shares x price. Use :func:`valuation.shares_from_amount` (floor) first when the
    RHP states the fresh issue as a rupee amount. Amounts are recomputed from the floored share
    count, so e.g. Moneyview fresh = 220,588,235 x ₹34 = ₹749.99999 cr, not exactly ₹750 cr."""
    p = require_price(price)
    f = require_shares(fresh_shares, "fresh_shares", allow_zero=True)
    o = require_shares(ofs_shares, "ofs_shares", allow_zero=True)
    return IssueMath(p, f, o, f + o, f * p, o * p, (f + o) * p)


# --- reservation ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Reservation:
    """Shares per category. ``net_offer = total - employee - shareholder``."""

    total: Decimal
    employee: Decimal
    shareholder: Decimal
    net_offer: Decimal
    qib: Decimal
    anchor_max: Decimal
    qib_ex_anchor_min: Decimal
    nii: Decimal
    snii: Decimal
    bnii: Decimal
    retail: Decimal


def reservation_split(
    total_shares: Num,
    *,
    qib_pct: Num = 50,
    nii_pct: Num = 15,
    retail_pct: Num = 35,
    employee_shares: Num = 0,
    shareholder_shares: Num = 0,
    anchor_share_of_qib: Num = 60,
    snii_ratio: tuple[int, int] = (1, 3),
) -> Reservation:
    """Split an offer into categories (percentages in percent points, of the NET offer).

    Defaults are the ICDR Reg 32(1) book-built split (QIB <=50 / NII >=15 / retail >=35);
    Reg 32(2) issues (e.g. loss-making) use 75/15/10. Employee and shareholder reservations are
    taken off the top first. NII -> sNII (₹2–10 lakh) 1/3 and bNII (>₹10 lakh) 2/3 (``snii_ratio``). Anchors may
    take up to 60% of the QIB portion (``anchor_max``). Each category is **floored** to whole
    shares (rounding residue goes unallocated here; RHPs sometimes push it into one category —
    compare to the printed figure). Moneyview: 35% x 321,082,435 = 112,378,852.25 -> 112,378,852.
    """
    total = require_shares(total_shares, "total_shares")
    emp = require_shares(employee_shares, "employee_shares", allow_zero=True)
    sh = require_shares(shareholder_shares, "shareholder_shares", allow_zero=True)
    q, n, r = to_decimal(qib_pct), to_decimal(nii_pct), to_decimal(retail_pct)
    if min(q, n, r) < 0 or q + n + r != 100:
        raise ValueError(f"category percentages must be >= 0 and sum to 100, got {q + n + r}")
    anc = to_decimal(anchor_share_of_qib)
    if not 0 <= anc <= 60:
        raise ValueError("anchor share of QIB must be within 0..60%")
    s_num, s_den = snii_ratio
    if s_den <= 0 or not 0 <= s_num <= s_den:
        raise ValueError("snii_ratio must be (num, den) with 0 <= num <= den, den > 0")
    net = total - emp - sh
    if net < 0:
        raise ValueError("reservations exceed total offer")

    def fl(x: Decimal) -> Decimal:
        return x.quantize(Decimal(1), rounding=ROUND_DOWN)

    qib = fl(net * q / 100)
    nii = fl(net * n / 100)
    retail = fl(net * r / 100)
    anchor_max = fl(qib * anc / 100)
    snii = fl(nii * s_num / s_den)  # exact ratio: avoids 1/3 precision loss before flooring
    return Reservation(
        total=total,
        employee=emp,
        shareholder=sh,
        net_offer=net,
        qib=qib,
        anchor_max=anchor_max,
        qib_ex_anchor_min=qib - anchor_max,
        nii=nii,
        snii=snii,
        bnii=nii - snii,
        retail=retail,
    )


def retail_lots_available(retail_shares: Num, lot_size: Num) -> Decimal:
    """``floor(retail shares / lot size)`` = max number of retail applicants that can get the
    minimum lot (SEBI minimum-application allotment). Moneyview: 112,378,852 / 441 -> 254,827."""
    return (
        require_shares(retail_shares, "retail_shares", allow_zero=True) / require_shares(lot_size, "lot_size")
    ).quantize(Decimal(1), rounding=ROUND_DOWN)


# --- subscription & allotment ---------------------------------------------------------------


def rebase_subscription(shares_bid: Num, shares_offered: Num) -> Decimal:
    """Times subscribed ``shares bid / shares offered`` against a base of your choice.

    Exchange snapshots divide by shares offered **excluding** the anchor portion; use this to
    restate against another base (e.g. full QIB quota incl. anchors, or a revised category size).
    """
    return require_shares(shares_bid, "shares_bid", allow_zero=True) / require_shares(
        shares_offered, "shares_offered"
    )


def subscription_by_category(bids: Mapping[str, Num], offered: Mapping[str, Num]) -> dict[str, Decimal]:
    """Times subscribed per category plus ``"total"`` (sum of bids / sum of offered over the
    categories present in both mappings). Raises ValueError if keys differ."""
    if set(bids) != set(offered):
        raise ValueError(f"category mismatch: {sorted(set(bids) ^ set(offered))}")
    out = {k: rebase_subscription(bids[k], offered[k]) for k in bids}
    tb = sum((to_decimal(v) for v in bids.values()), Decimal(0))
    to = sum((to_decimal(v) for v in offered.values()), Decimal(0))
    out["total"] = rebase_subscription(tb, to)
    return out


def allotment_probability_floor(times_subscribed: Num) -> Decimal:
    """``min(1, 1 / times_subscribed)`` — a FLOOR on a minimum-lot applicant's odds.

    Subscription "times" is measured in **shares** bid, but retail/sNII allotment is a lottery
    over **applications** (each successful applicant gets one minimum lot). Many applicants bid
    more than one lot, so applications < shares bid / lot size and the true odds —
    ``lots available / valid applications`` — are *higher* than this floor. Compute the exact
    odds with ``retail_lots_available(...) / applications`` once the application count is out.
    14.11x -> 7.09%.
    """
    t = to_decimal(times_subscribed)
    if t <= 0:
        raise ValueError("times_subscribed must be > 0")
    return min(Decimal(1), 1 / t)


def allotment_probability(lots_available: Num, applications: Num) -> Decimal:
    """Exact lottery odds ``min(1, lots available / valid applications)``."""
    lots = require_shares(lots_available, "lots_available", allow_zero=True)
    apps = require_shares(applications, "applications")
    return min(Decimal(1), lots / apps)


# --- pricing ---------------------------------------------------------------------------------


def listing_gain(issue_price: Num, listing_price: Num) -> Decimal:
    """``(listing price - issue price) / issue price`` as a fraction."""
    ip = require_price(issue_price, "issue_price")
    return (require_price(listing_price, "listing_price") - ip) / ip


@dataclass(frozen=True)
class GmpEstimate:
    """Grey-market implied price. UNOFFICIAL: GMP is an unregulated, unaudited street quote."""

    issue_price: Decimal
    gmp: Decimal
    implied_price: Decimal
    implied_gain: Decimal
    label: str = "UNOFFICIAL grey-market estimate (not a SEBI/exchange figure)"


def gmp_implied_price(issue_price: Num, gmp: Num | None) -> GmpEstimate | None:
    """Unofficial GMP-implied listing price ``issue price + GMP`` and implied gain fraction.

    Grey-market premium is an informal, unregulated quote; always label it as such. None if
    GMP is not available.
    """
    g = opt_decimal(gmp)
    if g is None:
        return None
    ip = require_price(issue_price, "issue_price")
    implied = ip + g
    return GmpEstimate(ip, g, implied, g / ip)


# --- lock-in ---------------------------------------------------------------------------------


def add_months(d: date, months: int) -> date:
    """Calendar-month addition clamped to month end (31 Jan + 1 month -> 28/29 Feb)."""
    if months < 0:
        raise ValueError("months must be >= 0")
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


@dataclass(frozen=True)
class LockInEvent:
    """A tranche of shares whose lock-in ends on ``unlock_date``.

    ``unlock_date`` is the anniversary-style end date computed from the allotment date; the
    shares are typically tradable from the next trading day. Confirm against the RHP/exchange
    notice, which is authoritative.
    """

    holder: str
    unlock_date: date
    shares: Decimal | None
    basis: str


def lock_in_schedule(
    allotment_date: date,
    *,
    anchor_shares: Num | None = None,
    promoter_min_contribution_shares: Num | None = None,
    promoter_excess_shares: Num | None = None,
    pre_ipo_non_promoter_shares: Num | None = None,
    anchor_days: tuple[int, int] = (30, 90),
    promoter_min_months: int = 18,
    promoter_excess_months: int = 6,
    non_promoter_months: int = 6,
) -> list[LockInEvent]:
    """Lock-in expiries, all counted from the **date of allotment** in the IPO.

    Defaults (SEBI ICDR 2018 as amended in 2021):

    * Anchor investors (Schedule XIII): 50% of anchor shares locked for ``anchor_days[0]`` = 30
      days and the remaining 50% for ``anchor_days[1]`` = 90 days from allotment.
    * Promoters (Reg 16): minimum promoter contribution (20% of post-issue capital) for 18
      months; promoter holding in excess of it for 6 months (3 years / 1 year where the objects
      involve capital expenditure — pass the periods explicitly in that case).
    * Other pre-IPO shareholders (Reg 17): 6 months from allotment (with specified exemptions,
      e.g. certain VCF/AIF holders and OFS shares).

    Odd anchor share counts: the first tranche is ``floor(anchor / 2)``, the second takes the
    remainder. Share counts are optional (None -> tranche listed without a count). Returns
    events sorted by date.
    """
    if len(anchor_days) != 2 or min(anchor_days) < 0:
        raise ValueError("anchor_days must be two non-negative day counts")
    events: list[LockInEvent] = []
    a = opt_decimal(anchor_shares)
    if a is not None:
        a = require_shares(a, "anchor_shares", allow_zero=True)
    first = None if a is None else (a / 2).quantize(Decimal(1), rounding=ROUND_DOWN)
    second = None if a is None or first is None else a - first
    events.append(
        LockInEvent("anchor (50%)", allotment_date + timedelta(days=anchor_days[0]), first,
                    f"ICDR Sch XIII: 50% anchor, {anchor_days[0]} days from allotment")
    )  # fmt: skip
    events.append(
        LockInEvent("anchor (remaining 50%)", allotment_date + timedelta(days=anchor_days[1]), second,
                    f"ICDR Sch XIII: remaining 50% anchor, {anchor_days[1]} days from allotment")
    )  # fmt: skip

    def opt_sh(x: Num | None, name: str) -> Decimal | None:
        return None if x is None else require_shares(x, name, allow_zero=True)

    events.append(
        LockInEvent("promoter minimum contribution", add_months(allotment_date, promoter_min_months),
                    opt_sh(promoter_min_contribution_shares, "promoter_min_contribution_shares"),
                    f"ICDR Reg 16: minimum promoter contribution, {promoter_min_months} months")
    )  # fmt: skip
    events.append(
        LockInEvent("promoter excess over minimum", add_months(allotment_date, promoter_excess_months),
                    opt_sh(promoter_excess_shares, "promoter_excess_shares"),
                    f"ICDR Reg 16: promoter holding above minimum, {promoter_excess_months} months")
    )  # fmt: skip
    events.append(
        LockInEvent("pre-IPO non-promoter shareholders", add_months(allotment_date, non_promoter_months),
                    opt_sh(pre_ipo_non_promoter_shares, "pre_ipo_non_promoter_shares"),
                    f"ICDR Reg 17: pre-issue capital, {non_promoter_months} months")
    )  # fmt: skip
    return sorted(events, key=lambda e: e.unlock_date)
