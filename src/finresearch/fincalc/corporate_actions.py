"""Corporate-action price factors, cumulative back-adjustment, total return and delisting returns (#369, roadmap P1.7).

A factor f is how many post-action units one pre-action share is worth in price terms: the pre-ex price divided by f
is comparable with the post-ex price. Formulas from the issue (standard exchange/index-provider practice):

    bonus a:b (a new for every b held)      f = (a + b) / b
    split or consolidation                   f = FV_old / FV_new          (consolidation: f < 1)
    rights, n new per N held at P_sub        TERP = (N x P_cum + n x P_sub) / (N + n),  f = P_cum / TERP
    demerger, V = value of the demerged unit f = P_cum / (P_cum - V)
    cumulative factor                        CF_t = product over ex-dates after t of f_i;  adjusted P~_t = P_t / CF_t
    total return                             TR_t = TR_t-1 x (P_t x f_t + D_t) / P_t-1
    delisting                                r = (P_exit + D) / P_last - 1, None if P_exit is unknown

The delisting rule deliberately has no US-style -30 % default for an unknown exit price: unknown stays unknown.
Money stays Decimal, as in the rest of fincalc. Bad terms raise ValueError rather than producing a silent factor 1.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal

Event = tuple[date, Decimal]  # (ex-date, factor or dividend per share)


def _pos(**kw: Decimal) -> None:
    for name, v in kw.items():
        if v is None or v <= 0:
            raise ValueError(f"{name} must be positive, got {v}")


def bonus_factor(a: Decimal | int, b: Decimal | int) -> Decimal:
    """Bonus a:b: a new shares for every b held."""
    a, b = Decimal(a), Decimal(b)
    _pos(a=a, b=b)
    return (a + b) / b


def face_value_factor(fv_old: Decimal, fv_new: Decimal) -> Decimal:
    """Split (fv_new < fv_old, f > 1) or consolidation (fv_new > fv_old, f < 1)."""
    _pos(fv_old=fv_old, fv_new=fv_new)
    return Decimal(fv_old) / Decimal(fv_new)


def rights_factor(n_new: Decimal | int, n_held: Decimal | int, p_cum: Decimal, p_sub: Decimal) -> Decimal:
    """Rights n_new for every n_held at p_sub, cum-rights price p_cum. A rights price at or above p_cum has no value
    to the holder (nobody subscribes), so the factor is 1."""
    n_new, n_held, p_cum, p_sub = Decimal(n_new), Decimal(n_held), Decimal(p_cum), Decimal(p_sub)
    _pos(n_new=n_new, n_held=n_held, p_cum=p_cum, p_sub=p_sub)
    if p_sub >= p_cum:
        return Decimal(1)
    terp = (n_held * p_cum + n_new * p_sub) / (n_held + n_new)
    return p_cum / terp


def demerger_factor(p_cum: Decimal, value: Decimal) -> Decimal:
    """Demerger: `value` per share of the demerged business (0 < value < p_cum)."""
    _pos(p_cum=p_cum, value=value)
    if value >= p_cum:
        raise ValueError(f"demerged value {value} must be below the cum price {p_cum}")
    return Decimal(p_cum) / (Decimal(p_cum) - Decimal(value))


def cumulative_factors(days: Sequence[date], events: Sequence[Event]) -> list[Decimal]:
    """CF_t for each day: the product of the factors whose ex-date is after t (same-day factors compound)."""
    out = []
    for t in days:
        cf = Decimal(1)
        for ex, f in events:
            if ex > t:
                cf *= f
        out.append(cf)
    return out


def adjust_prices(days: Sequence[date], prices: Sequence[Decimal], events: Sequence[Event]) -> list[Decimal]:
    """Back-adjusted prices P_t / CF_t: comparable with the latest price."""
    return [p / cf for p, cf in zip(prices, cumulative_factors(days, events), strict=True)]


def total_return_index(days: Sequence[date], prices: Sequence[Decimal], *, factors: Sequence[Event] = (),
                       dividends: Sequence[Event] = ()) -> list[Decimal]:  # fmt: skip
    """TR index starting at 1: TR_t = TR_t-1 x (P_t x f_t + D_t) / P_t-1, with f_t the product of factors and D_t the
    sum of dividends per (pre-action) share whose ex-date is t."""
    f_on: dict[date, Decimal] = {}
    d_on: dict[date, Decimal] = {}
    for ex, f in factors:
        f_on[ex] = f_on.get(ex, Decimal(1)) * f
    for ex, d in dividends:
        d_on[ex] = d_on.get(ex, Decimal(0)) + d
    if not prices:
        return []
    out = [Decimal(1)]
    for i in range(1, len(prices)):
        _pos(price=prices[i - 1])
        t = days[i]
        out.append(out[-1] * (prices[i] * f_on.get(t, Decimal(1)) + d_on.get(t, Decimal(0))) / prices[i - 1])
    return out


def delisting_return(
    p_last: Decimal, p_exit: Decimal | None, *, dividend: Decimal = Decimal(0)
) -> Decimal | None:
    """Return from the last traded close to the exit (offer or liquidation) value; None when the exit is unknown."""
    if p_exit is None:
        return None
    _pos(p_last=p_last)
    return (Decimal(p_exit) + dividend) / Decimal(p_last) - 1
