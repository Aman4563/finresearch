"""Net worth and the household checks, assembled from the database (read-only; no network, no pricing calls).

The portfolio side is the latest saved portfolio snapshot (written when /portfolio is valued), so every figure says
its date. Manual assets are valued by finresearch.wealth.calc on each day; loans by their amortisation schedule.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import (
    PortfolioLot,
    PortfolioSetting,
    PortfolioSnapshot,
    WealthAsset,
    WealthGoal,
    WealthLoan,
    WealthPolicy,
    WealthValuation,
)
from finresearch.fincalc.numbers import format_inr
from finresearch.portfolio import limits
from finresearch.wealth import DISCLAIMER, PRIVACY, allocation, calc, household
from finresearch.wealth.goals import DEFAULT_ASSUMPTIONS, assumptions_from

OPEN = Decimal("0.0005")  # units below this are rounding noise (portfolio.lots.EPS)

ASSET_KINDS = ("fd", "rd", "epf", "ppf", "nps", "gold", "sgb", "real_estate", "cash", "other")
LOAN_KINDS = ("home", "car", "personal", "education", "other")
LOCKED_KINDS = ("epf", "ppf", "nps")  # retirement lock-ins: outside liquid net worth
BANK_DEPOSIT_KINDS = ("cash", "fd", "rd")  # counted for the DICGC per-bank flag
STALE_DAYS = 90
SETTINGS_KEY = "wealth_assumptions"
DEFAULT_INFLATION = 6.0
DEFAULT_SEED = 20260930
DEFAULT_N = 5000


def f(x: Decimal | float | int | None) -> float | None:
    return None if x is None else float(x)


# --------------------------------------------------------------------------- assumptions
def get_assumptions(s: Session) -> dict[str, Any]:
    row = s.get(PortfolioSetting, SETTINGS_KEY)
    raw = dict(row.value) if row else {}
    a = assumptions_from(raw)
    return {
        **{
            k: {
                "mu_pct": v.mu_pct,
                "sigma_pct": v.sigma_pct,
                "note": v.note,
                "default": {
                    "mu_pct": DEFAULT_ASSUMPTIONS[k].mu_pct,
                    "sigma_pct": DEFAULT_ASSUMPTIONS[k].sigma_pct,
                },
            }
            for k, v in a.items()
        },
        "inflation_pct": float(raw.get("inflation_pct", DEFAULT_INFLATION)),
        "seed": int(raw.get("seed", DEFAULT_SEED)),
        "n": int(raw.get("n", DEFAULT_N)),
    }


def set_assumptions(s: Session, body: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    assumptions_from({k: v for k, v in body.items() if k in DEFAULT_ASSUMPTIONS})  # validates
    for k in DEFAULT_ASSUMPTIONS:
        v = body.get(k)
        if isinstance(v, dict):
            clean[k] = {"mu_pct": float(v["mu_pct"]), "sigma_pct": float(v["sigma_pct"])}
    if "inflation_pct" in body:
        infl = float(body["inflation_pct"])
        if not 0 <= infl <= 20:
            raise ValueError("inflation must be within 0..20 %")
        clean["inflation_pct"] = infl
    if "seed" in body:
        clean["seed"] = int(body["seed"]) % (2**32)
    if "n" in body:
        n = int(body["n"])
        if not 500 <= n <= 20000:
            raise ValueError("paths must be within 500..20,000")
        clean["n"] = n
    row = s.get(PortfolioSetting, SETTINGS_KEY)
    if row is None:
        s.add(PortfolioSetting(key=SETTINGS_KEY, value=clean))
    else:
        row.value = clean
    s.flush()
    return get_assumptions(s)


# --------------------------------------------------------------------------- valuation
@dataclass
class Book:
    assets: list[WealthAsset]
    vals: dict[int, list[WealthValuation]]
    loans: list[WealthLoan]
    goals: list[WealthGoal]
    policies: list[WealthPolicy]
    snaps: list[PortfolioSnapshot]
    # open portfolio lots exist: a day without a portfolio valuation then has an unknown portfolio value, not ₹0
    has_portfolio: bool = False
    first_txn: date | None = None  # the portfolio's first transaction: before it the portfolio is ₹0, known
    series: Any = None  # portfolio.series.Series: the canonical reconstructed value history (#239), or None
    series_why: str | None = None  # why there is no usable series


def load(s: Session) -> Book:
    from sqlalchemy import func

    from finresearch.db.models import PortfolioTxn
    from finresearch.portfolio import series

    first = s.scalar(select(func.min(PortfolioTxn.day)).where(PortfolioTxn.kind.in_(("buy", "opening"))))
    ser, why = series.load(s) if first is not None else (None, None)
    return _book(s, first, ser, why)


def _book(s: Session, first: date | None, ser: Any, why: str | None) -> Book:
    vals: dict[int, list[WealthValuation]] = {}
    for v in s.scalars(select(WealthValuation).order_by(WealthValuation.day)):
        vals.setdefault(v.asset_id, []).append(v)
    return Book(
        assets=list(s.scalars(select(WealthAsset).order_by(WealthAsset.id))),
        vals=vals,
        loans=list(s.scalars(select(WealthLoan).order_by(WealthLoan.id))),
        goals=list(s.scalars(select(WealthGoal).order_by(WealthGoal.target_date, WealthGoal.id))),
        policies=list(s.scalars(select(WealthPolicy).order_by(WealthPolicy.id))),
        snaps=list(s.scalars(select(PortfolioSnapshot).order_by(PortfolioSnapshot.day))),
        has_portfolio=s.scalar(select(PortfolioLot.id).where(PortfolioLot.open_quantity > OPEN).limit(1))
        is not None,
        first_txn=first,
        series=ser,
        series_why=why,
    )


def _last(vals: list[WealthValuation], on: date) -> WealthValuation | None:
    got = None
    for v in vals:
        if v.day <= on:
            got = v
    return got


def asset_value(a: WealthAsset, vals: list[WealthValuation], on: date) -> dict[str, Any]:
    """{value, method, as_of, stale}. value None = unknown on that day (not entered yet)."""
    rate = a.rate_pct or Decimal(0)
    rs = f"{float(rate):g}"  # 7.100 -> 7.1
    if a.kind == "fd" and a.principal is not None and a.rate_pct is not None and a.start_date:
        v = calc.fd_value(
            a.principal, rate, a.start_date, on, compounding=a.compounding, maturity=a.maturity_date
        )
        how = "principal" if a.compounding <= 0 else f"P(1 + r/{a.compounding})^({a.compounding}t) at {rs} %"
        matured = bool(a.maturity_date and on >= a.maturity_date)
        return {
            "value": f(v) if on >= a.start_date else None,
            "method": how + (" (matured)" if matured else ""),
            "as_of": on.isoformat(),
            "stale": False,
            "matured": matured,
        }
    if a.kind == "rd" and a.monthly_contribution is not None and a.rate_pct is not None and a.start_date:
        v = calc.rd_value(a.monthly_contribution, rate, a.start_date, on, maturity=a.maturity_date)
        return {
            "value": f(v) if on >= a.start_date else None,
            "as_of": on.isoformat(),
            "stale": False,
            "method": f"instalments compounding quarterly at {rs} %",
        }
    last = _last(vals, on)
    if last is None:
        return {
            "value": None,
            "method": "no value entered on or before this day",
            "as_of": None,
            "stale": False,
        }
    if a.kind in ("epf", "ppf") and (a.rate_pct is not None or a.monthly_contribution):
        v = calc.provident_value(last.value, rate, last.day, on, monthly=a.monthly_contribution or 0)
        return {
            "value": f(v),
            "as_of": last.day.isoformat(),
            "stale": (on - last.day).days > STALE_DAYS,
            "method": f"balance on {last.day:%d-%b-%Y} + contributions + interest at {rs} % (estimate)",
        }
    return {
        "value": f(last.value),
        "method": f"your value on {last.day:%d-%b-%Y}",
        "as_of": last.day.isoformat(),
        "stale": (on - last.day).days > STALE_DAYS,
    }


def loan_emi(ln: WealthLoan) -> Decimal:
    return ln.emi if ln.emi is not None else calc.emi(ln.principal, ln.rate_pct, ln.tenure_months)


def loan_balance(ln: WealthLoan, on: date) -> float | None:
    if on < ln.start_date:
        return None
    e = loan_emi(ln)
    if ln.outstanding is not None and ln.outstanding_as_of and ln.outstanding_as_of <= on:
        return f(
            calc.outstanding(ln.outstanding, ln.rate_pct, e, calc.months_between(ln.outstanding_as_of, on))
        )
    return f(calc.outstanding(ln.principal, ln.rate_pct, e, calc.months_between(ln.start_date, on)))


def portfolio_on(snaps: list[PortfolioSnapshot], on: date) -> PortfolioSnapshot | None:
    got = None
    for sn in snaps:
        if sn.day <= on:
            got = sn
    return got


def _past_value(b: Book, on: date) -> tuple[float | None, str | None, str | None]:
    """A past day's portfolio value from the canonical reconstructed history (portfolio.series, #239)."""
    if b.first_txn is None or on < b.first_txn:
        return 0.0, None, None  # nothing bought yet: ₹0 is known
    if b.series is None:
        return None, f"portfolio: {b.series_why or 'no value history'}: left out of the total", None
    got = b.series.value_on(on)
    if got is None:
        return None, (f"portfolio: before the reconstructed value history starts ({b.series.start_reason}): left out "
                      "of the total"), None  # fmt: skip
    v, complete, d = got
    why = None
    if not complete:
        why = f"portfolio: the reconstructed value of {d.isoformat()} used a price more than 10 days old"
    elif b.series.excluded:
        why = "portfolio: the reconstructed value leaves out " + "; ".join(b.series.excluded)
    return v, why, d.isoformat()


def portfolio_value_on(b: Book, on: date, today: date | None = None) -> tuple[float | None, str | None]:
    """(the portfolio's value on a day, why it is unknown or incomplete). Value None = unknown, never ₹0 (#238).

    One rule for every reader (#239): a past day (before `today`) reads the canonical reconstructed history
    (transactions × official closes, portfolio.series); today reads the latest "as shown" valuation (the portfolio
    page or the daily pass), whose day is returned with it. An incomplete valuation keeps its value (what was priced)
    with the reason."""
    if today is not None and on < today:
        v, why, _ = _past_value(b, on)
        return v, why
    sn = portfolio_on(b.snaps, on)
    if sn is None:
        if b.has_portfolio:
            return (
                None,
                "portfolio: no valuation on or before this day (open /portfolio): left out of the total",
            )
        return 0.0, None
    if not sn.complete:
        return f(sn.value), (f"portfolio: the valuation of {sn.day.isoformat()} is incomplete (a holding without a "
                             "current price or cost, or a sale without a cost): it counts only what was priced")  # fmt: skip
    return f(sn.value), None


def net_worth_on(b: Book, on: date, today: date | None = None) -> dict[str, Any]:
    """Net worth on a day (`today` given and `on` earlier: a past day, valued from the reconstructed history).
    `complete` False (with `missing` saying why) when a part is unknown: the totals then add up only the known parts
    and must be read as such."""
    past = today is not None and on < today
    sn = None if past else portfolio_on(b.snaps, on)
    port_value, port_why = portfolio_value_on(b, on, today)
    port = port_value or 0.0
    manual = 0.0
    real_estate = locked = 0.0
    for a in b.assets:
        v = asset_value(a, b.vals.get(a.id, []), on)["value"]
        if v is None:
            continue
        manual += v
        if a.kind == "real_estate":
            real_estate += v
        elif a.kind in LOCKED_KINDS:
            locked += v
    loans = home = 0.0
    for ln in b.loans:
        x = loan_balance(ln, on)
        if x is not None:
            loans += x
            home += x if ln.kind == "home" else 0.0
    total = port + manual
    return {
        "date": on.isoformat(),
        "portfolio": None if port_value is None else round(port, 2),
        "manual": round(manual, 2),
        "assets": round(total, 2),
        "liabilities": round(loans, 2),
        "net_worth": round(total - loans, 2),
        # liquid: without real estate and retirement lock-ins, and without home loans (secured on the property that
        # is left out); other loans still count
        "liquid_net_worth": round(total - real_estate - locked - (loans - home), 2),
        "portfolio_day": _past_value(b, on)[2] if past else (sn.day.isoformat() if sn else None),
        "portfolio_source": "reconstructed" if past else "as shown",
        "complete": port_why is None,
        "missing": [port_why] if port_why else [],
    }


def _month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        d = date(y, m, calendar.monthrange(y, m)[1])
        if d < end:
            out.append(d)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def history(b: Book, today: date, max_points: int = 120) -> list[dict[str, Any]]:
    """Month-end net worth from the earliest dated entry to today, plus today. Each month uses only what was known
    on that day: the portfolio's reconstructed value (portfolio.series; today: the latest "as shown" valuation),
    manual values dated on or before it (FD/RD/loan schedules from their start dates)."""
    starts = (
        [sn.day for sn in b.snaps]
        + [ln.start_date for ln in b.loans]
        + ([b.first_txn] if b.first_txn else [])
    )
    starts += [v.day for vs in b.vals.values() for v in vs]
    starts += [a.start_date for a in b.assets if a.start_date and a.kind in ("fd", "rd")]
    starts = [d for d in starts if d <= today]
    if not starts:
        return []
    days = [*_month_ends(min(starts), today)[-max_points:], today]
    return [net_worth_on(b, d, today) for d in days]


# --------------------------------------------------------------------------- overview
def snapshot_gaps(s: Session, sn: PortfolioSnapshot | None) -> dict[str, Any] | None:
    """What the snapshot `sn` left out (portfolio.metrics.valuation_gaps), or None when that is not recorded for this
    very valuation: the stored gaps must name the same day and value, so a later rewrite of the day without them, or
    an older row, is never described by another valuation's gaps (#286)."""
    from finresearch.portfolio import cache

    got = cache.read(s, cache.GAPS)
    if sn is None or got.get("day") != sn.day.isoformat() or got.get("value") is None:
        return None
    return got.get("gaps") if abs(float(got["value"]) - float(sn.value)) < 0.01 else None


def _lower_bound(gaps: dict[str, Any] | None, portfolio: float) -> str | None:
    """None when an incomplete portfolio value is a lower bound of the whole, else why it is not (#286).

    It is a lower bound only when what it leaves out can add nothing negative and nothing it counts can be too high:
    the only gap is holdings without a price (counted as nothing), each a long position (units > 0, so its value is
    at least ₹0); no holding is valued at a stale price (an old price can be above today's); and something was
    priced (nothing priced at all stays unknown)."""
    if gaps is None:
        return "which holdings the valuation leaves out is not recorded (value the portfolio again on /portfolio)"
    if gaps.get("stale"):
        return f"{gaps['stale']} holding(s) valued at an old price, which can be above or below today's"
    unpriced = gaps.get("unpriced") or []
    if not unpriced:
        return "the valuation is incomplete for another reason than a missing price"
    if not gaps.get("priced") or portfolio <= 0:
        return "no holding is priced"
    if any(u.get("units") is None or u["units"] <= 0 or (u.get("last_price") or 0) < 0 for u in unpriced):
        return "a holding without a price could be worth less than ₹0"
    return None


def goal_funding(g: WealthGoal, values: dict[int, float], portfolio: float | None, portfolio_why: str | None = None,
                 unvalued: set[int] | frozenset[int] = frozenset(),
                 gaps: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    """Money set aside for a goal today: other savings + linked assets + the earmarked share of the portfolio.

    Unknown never becomes ₹0 (#262): when the goal earmarks a share of the portfolio and the portfolio's value is
    unknown (`portfolio` None: holdings without a valuation), `start` is None; an incomplete valuation (`portfolio_why`
    set: a holding without a price or cost) or a linked asset without a value (`unvalued`) keeps the known sum but
    `complete` is False with the reasons, so it is never read as the whole.

    A valuation whose only gap is cost basis (`gaps`: every open holding priced, none stale) counts as complete: the
    current value does not depend on cost.

    `bound` "lower" (#286): the only gap is earmarked holdings without a price (`gaps`, see _lower_bound), so `start`
    is a true lower bound of the money set aside; `unpriced` names them and `unpriced_share_pct` is their share of the
    earmarked portfolio at their last known prices (None = unknown: one of them has no known price). Otherwise
    `bound` is None and `bound_why` says why."""
    linked = [int(i) for i in (g.linked_asset_ids or []) if int(i) in values]
    missing = [int(i) for i in (g.linked_asset_ids or []) if int(i) in unvalued]
    pct = float(g.portfolio_pct or 0)
    why = []
    # a valuation incomplete only for cost basis (an unknown lot cost or a sale without a cost: every open holding
    # priced, none at a stale price) has a complete current value, which does not depend on cost
    value_complete = (
        gaps is not None and gaps.get("priced", 0) > 0 and not gaps.get("unpriced") and not gaps.get("stale")
    )
    if pct > 0 and (portfolio is None or (portfolio_why and not value_complete)):
        why.append(portfolio_why or "portfolio: value unknown")
    if missing:
        why.append(f"{len(missing)} linked asset(s) without a value yet: left out")
    start = None
    if not (pct > 0 and portfolio is None):
        start = round(
            float(g.current_inr or 0) + sum(values[i] for i in linked) + (portfolio or 0.0) * pct / 100, 2
        )
    out = {"start": start, "linked": linked, "complete": not why, "why": "; ".join(why) or None, "bound": None,
           "bound_why": None, "unpriced": [], "unpriced_share_pct": None}  # fmt: skip
    if why and start is not None:
        # a linked asset without a value is left unknown (its sign is the user's to enter); only the portfolio's
        # unpriced holdings can make a lower bound
        no = "a linked asset has no value" if missing else _lower_bound(gaps, portfolio or 0.0)
        if no is None and gaps is not None:
            last = [u.get("last_value") for u in gaps["unpriced"]]
            known = None if any(v is None for v in last) else sum(last)
            share = None if known is None else round(known / ((portfolio or 0.0) + known) * 100, 2)
            out.update(
                bound="lower", unpriced=[u["name"] for u in gaps["unpriced"]], unpriced_share_pct=share
            )
        else:
            out["bound_why"] = no
    return out


def _months_until(today: date, d: date) -> int:
    return calc.months_between(today, d)


def overview(s: Session, today: date) -> dict[str, Any]:
    from finresearch.suggest.advisor import load_profile

    prof = load_profile(s)
    hh = prof.household
    asm = get_assumptions(s)
    b = load(s)
    sn = portfolio_on(b.snaps, today)
    port_value, port_why = portfolio_value_on(b, today)

    # ---- assets, by class
    rows, values = [], {}
    by_class: dict[str, float] = {}
    real_estate: dict[int, float] = {}
    for a in b.assets:
        v = asset_value(a, b.vals.get(a.id, []), today)
        rows.append(
            {
                **asset_json(a),
                "valuation": v,
                "history": [{"day": x.day.isoformat(), "value": f(x.value)} for x in b.vals.get(a.id, [])][
                    -24:
                ],
            }
        )
        if v["value"] is not None:
            values[a.id] = v["value"]
            for k, x in allocation.split_asset(
                a.kind, v["value"], asset_class=a.asset_class, equity_pct=f(a.equity_pct)
            ).items():
                by_class[k] = by_class.get(k, 0.0) + x
                if k == "Real estate":
                    real_estate[a.id] = real_estate.get(a.id, 0.0) + x
    port_classes: dict[str, float] = {}
    if sn:
        for label, v in (sn.by_asset or {}).items():
            k = allocation.PORTFOLIO_CLASS.get(label, "Other")
            port_classes[k] = port_classes.get(k, 0.0) + float(v)
        for k, v in port_classes.items():
            by_class[k] = by_class.get(k, 0.0) + v

    nw = net_worth_on(b, today)
    loans_json = []
    total_emi = 0.0
    for ln in b.loans:
        bal = loan_balance(ln, today)
        e = f(loan_emi(ln)) or 0.0
        left = calc.months_to_repay(bal or 0, ln.rate_pct, e) if bal else 0.0
        active = bal is not None and bal > 0
        if active:
            total_emi += e
        pv = None
        if active:
            pv = household.prepay_vs_invest(
                kind=ln.kind,
                rate_pct=float(ln.rate_pct),
                balance=bal or 0.0,
                regime=hh.tax_regime,
                slab_pct=float(prof.tax_slab_pct),
                equity_mu_pct=asm["equity"]["mu_pct"],
                equity_sigma_pct=asm["equity"]["sigma_pct"],
                debt_mu_pct=asm["debt"]["mu_pct"],
                years_left=left / 12,
            )
        loans_json.append(
            {
                **loan_json(ln),
                "emi_used": e,
                "emi_computed": ln.emi is None,
                "outstanding_now": bal,
                "months_left": None if left == float("inf") else round(left, 1),
                "active": active,
                "prepay_vs_invest": pv,
            }
        )

    # ---- allocation vs the glide path (financial assets)
    total_fin = sum(v for k, v in by_class.items() if k in allocation.FINANCIAL)
    w_fin = allocation.weights(by_class, allocation.FINANCIAL)
    target = allocation.glide_target(hh.age, prof.risk_appetite, f(hh.target_equity_pct))
    abs_pp, rel_pct = limits.bands(prof)
    comparison = allocation.compare(w_fin, target, abs_pp, rel_pct) if target and total_fin > 0 else []
    outside = [r for r in comparison if r["outside_band"]]
    if not target:
        alloc_msg = "Add your age (or your own equity target) to compare with a glide path."
    elif not comparison:
        alloc_msg = "Nothing valued yet."
    elif outside:
        alloc_msg = (
            "Your target says "
            + "; ".join(
                f"{r['label']} is {abs(r['drift_pp']):.1f} pp {'above' if r['drift_pp'] > 0 else 'below'} it "
                f"(band ±{r['band_pp']:.1f} pp)"
                for r in outside
            )
            + ". Consider reviewing new money first (no tax, no exit load)."
        )
    else:
        alloc_msg = "Within your bands: no action needed."

    # ---- emergency fund and deposit insurance
    liquid = 0.0
    deposits: list[tuple[str | None, float]] = []
    for a in b.assets:
        v = values.get(a.id)
        if v is None:
            continue
        if a.kind == "cash":
            liquid += v
        elif a.kind in ("fd", "rd") and a.liquid:
            liquid += v * household.BREAKABLE_HAIRCUT
        elif a.liquid and a.kind not in ("fd", "rd"):
            liquid += v
        if a.kind in BANK_DEPOSIT_KINDS and not (
            a.maturity_date and a.maturity_date <= today and a.kind != "cash"
        ):
            deposits.append((a.institution, v))
    exp = f(hh.monthly_expenses_inr)
    months = household.emergency_months(liquid, exp)
    tgt, tgt_why = household.emergency_target(f(hh.emergency_months_target), hh.dependants, hh.earners)
    conc = household.deposit_concentration(deposits)
    if months is None:
        em_msg = "Add your monthly essential expenses to see how many months your liquid money covers."
    elif months >= tgt:
        em_msg = f"{months:.1f} months covered; your target is {tgt:g} ({tgt_why}): no action needed."
    else:
        em_msg = f"{months:.1f} months covered; your target is {tgt:g} ({tgt_why}): a gap of about {format_inr((tgt - months) * exp, 'inr', 0)}."  # type: ignore[operator]

    # ---- goals (funding only; the Monte Carlo runs per goal on request)
    goals_json = []
    linked_count: dict[int, int] = {}
    for g in b.goals:
        for i in g.linked_asset_ids or []:
            linked_count[int(i)] = linked_count.get(int(i), 0) + 1
    unvalued = {a.id for a in b.assets if a.id not in values}
    gaps = snapshot_gaps(s, sn) if port_why else None
    for g in b.goals:
        fund = goal_funding(g, values, port_value, port_why, unvalued, gaps)
        goals_json.append(
            {
                **goal_json(g),
                # None = unknown (the earmarked portfolio has no valuation), never ₹0; incomplete = a lower bound
                "funded_now": fund["start"],
                "funded_complete": fund["complete"],
                "funded_why": fund["why"],
                # "lower": funded_now is at least this (only unpriced holdings are left out, #286)
                "funded_bound": fund["bound"],
                "funded_unpriced": fund["unpriced"],
                "funded_unpriced_share_pct": fund["unpriced_share_pct"],
                "months_left": _months_until(today, g.target_date),
                "shared_links": [i for i in fund["linked"] if linked_count.get(i, 0) > 1],
            }
        )

    # ---- insurance: needs-based term cover
    live = [p for p in b.policies if p.end_date is None or p.end_date >= today]
    term_cover_existing = sum(float(p.cover_inr) for p in live if p.kind == "term")
    health_total = sum(float(p.cover_inr) for p in live if p.kind == "health")
    health_employer = sum(float(p.cover_inr) for p in live if p.kind == "health" and p.employer)
    years = (
        hh.support_years
        if hh.support_years is not None
        else (max(hh.retirement_age - hh.age, 0) if hh.age is not None else None)
    )
    financial_assets = nw["assets"] - by_class.get("Real estate", 0.0)
    # Each goal's gap counts only money the cover sum does not already deduct: `funded_now` includes linked assets
    # and a share of the portfolio, which are also inside `financial_assets` (subtracted once in CoverNeed.gap).
    # Netting them here as well would count them twice and understate the cover. Money outside the app
    # (current_inr) and linked real estate (left out of financial_assets) do reduce the gap.
    goal_gaps = sum(
        max(0.0, float(g.target_inr) - float(g.current_inr or 0)
            - sum(real_estate.get(int(i), 0.0) for i in (g.linked_asset_ids or [])))
        for g in b.goals
        if g.in_cover
    )  # fmt: skip
    ins: dict[str, Any] = {
        "term_existing": term_cover_existing,
        "health_total": health_total,
        "health_employer": health_employer,
        "health_rule_inr": household.HEALTH_RULE_INR,
    }
    if exp is None or years is None:
        ins["term"] = None
        ins["term_message"] = (
            "Add your age (or support years) and monthly expenses to estimate cover by the needs method."
        )
    else:
        need = household.term_cover(
            annual_expenses=exp * 12,
            years=years,
            nominal_pct=asm["debt"]["mu_pct"],
            inflation_pct=asm["inflation_pct"],
            loans=nw["liabilities"],
            goal_gaps=goal_gaps,
            assets=financial_assets,
            existing=term_cover_existing,
        )
        ins["term"] = {
            "expenses_pv": need.expenses_pv,
            "loans": need.loans,
            "goal_gaps": need.goal_gaps,
            "assets": need.assets,
            "existing": need.existing,
            "need": need.need,
            "gap": need.gap,
            "years": need.years,
            "real_rate_pct": need.rate * 100,
        }
        ins["term_message"] = (
            f"By the needs method your cover gap is about {format_inr(need.gap, 'inr', 0)}."
            if need.gap > 0
            else "By the needs method your existing cover and assets meet the need: no action needed."
        )
    inc = f(hh.monthly_income_inr)
    ins["income_multiple"] = None if inc is None else [inc * 12 * m for m in household.INCOME_MULTIPLE]

    # ---- debt
    foir = household.foir_pct(total_emi, inc)
    if foir is None:
        debt_msg = (
            "Add your monthly net income to see EMIs as a share of it." if total_emi else "No active loans."
        )
    elif foir >= household.FOIR_WARN_PCT:
        debt_msg = (
            f"EMIs take {foir:.1f} % of net income, at or above the ~40 % many lenders cap (rule of thumb)."
        )
    else:
        debt_msg = f"EMIs take {foir:.1f} % of net income, below the ~40 % many lenders cap (rule of thumb): no action needed."
    rated = [(float(ln["outstanding_now"]), float(ln["rate_pct"])) for ln in loans_json if ln["active"]]
    wavg = sum(b_ * r for b_, r in rated) / sum(b_ for b_, _ in rated) if rated else None

    return {
        "as_of": today.isoformat(),
        "net_worth": nw,
        "portfolio": {
            "value": port_value,
            "day": sn.day.isoformat() if sn else None,
            "complete": bool(sn.complete) if sn else (False if b.has_portfolio else None),
            "why": port_why,
            "by_class": port_classes,
        },
        "assets": rows,
        "loans": loans_json,
        "goals": goals_json,
        "policies": [policy_json(p) for p in b.policies],
        "allocation": {
            "by_class": {k: round(v, 2) for k, v in by_class.items()},
            "weights_financial": {k: round(v, 2) for k, v in w_fin.items()},
            "target": target,
            "comparison": comparison,
            "message": alloc_msg,
            "rule": "Equity % = clamp(100 − age, 20, 80) ± 10 for low/high risk appetite; gold 10 %; rest "
            f"debt and cash. Bands: {limits.band_rule(abs_pp, rel_pct)}, set on your profile (the same band as "
            "the Rebalance card). Rules of thumb, not advice.",
            "bands": {"abs_pp": abs_pp, "rel_pct": rel_pct},
            "risk_appetite": prof.risk_appetite,
            "own_target": hh.target_equity_pct is not None,
        },
        "emergency": {
            "liquid": round(liquid, 2),
            "monthly_expenses": exp,
            "months": months,
            "target_months": tgt,
            "target_why": tgt_why,
            "message": em_msg,
            "banks": conc,
            "dicgc_limit": household.DICGC_LIMIT_INR,
            "dicgc_source": household.DICGC_SOURCE,
        },
        "insurance": ins,
        "debt": {
            "total_emi": total_emi,
            "monthly_income": inc,
            "foir_pct": foir,
            "message": debt_msg,
            "weighted_rate_pct": wavg,
            "debt_to_assets_pct": nw["liabilities"] / nw["assets"] * 100 if nw["assets"] else None,
        },
        "household": hh.model_dump(mode="json"),
        "tax_slab_pct": float(prof.tax_slab_pct),
        "assumptions": asm,
        "history": history(b, today),
        "rates": calc.RATE_DEFAULTS,
        "privacy": PRIVACY,
        "disclaimer": DISCLAIMER,
    }


# --------------------------------------------------------------------------- goal plan
def goal_plan(
    s: Session, goal_id: int, today: date, *, seed: int | None = None, n: int | None = None
) -> dict[str, Any]:
    from finresearch.wealth.goals import plan

    g = s.get(WealthGoal, goal_id)
    if g is None:
        raise LookupError(goal_id)
    asm = get_assumptions(s)
    b = load(s)
    values = {
        a.id: v for a in b.assets if (v := asset_value(a, b.vals.get(a.id, []), today)["value"]) is not None
    }
    port_value, port_why = portfolio_value_on(b, today)  # the same rule as the overview (#238, #239)
    unvalued = {a.id for a in b.assets if a.id not in values}
    gaps = snapshot_gaps(s, portfolio_on(b.snaps, today)) if port_why else None
    fund = goal_funding(g, values, port_value, port_why, unvalued, gaps)
    lower = fund["bound"] == "lower"
    if not fund["complete"] and not lower:
        # the simulation starts from the money set aside today: from an unknown or partial start its P(success) and
        # "SIP for 75 %" would be a shortfall computed from ₹0 or from part of the money (#262). Say why instead.
        return {"goal_id": g.id, "name": g.name, "funded_complete": False, "funded_why": fund["why"],
                "start": fund["start"], "linked": fund["linked"], "bound": None, "bound_why": fund["bound_why"],
                "message": "Not simulated: the money set aside for this goal is "
                           + ("unknown" if fund["start"] is None else "incomplete")
                           + f" ({fund['why']})"
                           + (f"; not a lower bound either: {fund['bound_why']}" if fund["bound_why"] else "")
                           + ". Value the portfolio (open /portfolio) or the linked assets first.",
                "disclaimer": DISCLAIMER}  # fmt: skip
    months = calc.months_between(today, g.target_date)
    a = assumptions_from({k: asm[k] for k in DEFAULT_ASSUMPTIONS})
    p = plan(
        start=fund["start"],
        sip0=float(g.monthly_sip),
        step_up_pct=float(g.step_up_pct),
        months=months,
        target_today=float(g.target_inr),
        inflation_pct=float(g.inflation_pct),
        equity_pct=f(g.equity_pct),
        gold_pct=float(g.gold_pct),
        assumptions=a,
        n=n or asm["n"],
        seed=asm["seed"] if seed is None else seed,
    )
    out = p.to_json()
    ps = out["p_success"]
    if lower:
        # P(success) is non-decreasing and the SIP needed non-increasing in the starting money (wealth.goals: each
        # path's final value is start × a positive growth factor + the SIPs, on the same draws), so a run from the
        # priced part is a floor on the chance and a ceiling on the SIP (#286; tests/test_goal_lower_bound.py)
        need = out["sip_for_75"]
        msg = (f"At least {ps * 100:.1f} % on these assumptions, counting only the priced holdings; the true chance is "
               "higher if the unpriced ones are worth anything. "
               + ("No action needed." if ps >= 0.75 else
                  f"The numbers imply a starting SIP of at most {format_inr(need, 'inr', 0)} for 75 %." if need is not None
                  else "From the priced part alone no SIP reaches 75 % in the time left; with the unpriced holdings "
                       "the SIP needed is unknown."))  # fmt: skip
    elif ps >= 0.9:
        msg = "Above 90 % on these assumptions: your plan may be saving more than the goal needs (rule of thumb)."
    elif ps >= 0.75:
        msg = "Between 75 % and 90 % on these assumptions: no action needed."
    else:
        need = out["sip_for_75"]
        msg = (
            f"Below 75 % on these assumptions; the numbers imply a starting SIP of about {format_inr(need, 'inr', 0)} for 75 %."
            if need is not None
            else "Below 75 % on these assumptions, and no SIP reaches 75 % in the time left."
        )
    out.update(
        {
            "goal_id": g.id,
            "name": g.name,
            "funded_complete": not lower,
            "funded_why": fund["why"],
            # "lower": start, P(success) (and its range, the haircut, one-year-later and step-up rows) and the outcome
            # percentiles are lower bounds; sip_for_75/90 upper bounds, None = unknown, not "not reachable" (#286)
            "bound": "lower" if lower else None,
            "unpriced": fund["unpriced"],
            "unpriced_share_pct": fund["unpriced_share_pct"],
            "message": msg,
            "linked": fund["linked"],
            "method": "Seeded lognormal Monte Carlo, monthly steps, SIP added at month end, rebalanced monthly; "
            "classes drawn independently. Range: 95 % Wilson interval (simulation error) and the "
            f"result with equity returns 2 pp lower. Seed {out['seed']}, {out['n']:,} paths.",
            "disclaimer": DISCLAIMER,
        }
    )
    return out


# --------------------------------------------------------------------------- JSON
def asset_json(a: WealthAsset) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "name": a.name,
        "institution": a.institution,
        "asset_class": a.asset_class,
        "principal": f(a.principal),
        "rate_pct": f(a.rate_pct),
        "compounding": a.compounding,
        "monthly_contribution": f(a.monthly_contribution),
        "start_date": a.start_date.isoformat() if a.start_date else None,
        "maturity_date": a.maturity_date.isoformat() if a.maturity_date else None,
        "liquid": a.liquid,
        "equity_pct": f(a.equity_pct),
        "notes": a.notes,
    }


def loan_json(ln: WealthLoan) -> dict[str, Any]:
    return {
        "id": ln.id,
        "kind": ln.kind,
        "name": ln.name,
        "lender": ln.lender,
        "principal": f(ln.principal),
        "rate_pct": f(ln.rate_pct),
        "tenure_months": ln.tenure_months,
        "emi": f(ln.emi),
        "start_date": ln.start_date.isoformat(),
        "outstanding": f(ln.outstanding),
        "outstanding_as_of": ln.outstanding_as_of.isoformat() if ln.outstanding_as_of else None,
        "floating": ln.floating,
        "notes": ln.notes,
    }


def goal_json(g: WealthGoal) -> dict[str, Any]:
    return {
        "id": g.id,
        "name": g.name,
        "target_inr": f(g.target_inr),
        "target_date": g.target_date.isoformat(),
        "priority": g.priority,
        "inflation_pct": f(g.inflation_pct),
        "current_inr": f(g.current_inr),
        "monthly_sip": f(g.monthly_sip),
        "step_up_pct": f(g.step_up_pct),
        "linked_asset_ids": list(g.linked_asset_ids or []),
        "portfolio_pct": f(g.portfolio_pct),
        "equity_pct": f(g.equity_pct),
        "gold_pct": f(g.gold_pct),
        "in_cover": g.in_cover,
        "notes": g.notes,
    }


def policy_json(p: WealthPolicy) -> dict[str, Any]:
    return {
        "id": p.id,
        "kind": p.kind,
        "name": p.name,
        "cover_inr": f(p.cover_inr),
        "premium_inr": f(p.premium_inr),
        "end_date": p.end_date.isoformat() if p.end_date else None,
        "employer": p.employer,
        "notes": p.notes,
    }
