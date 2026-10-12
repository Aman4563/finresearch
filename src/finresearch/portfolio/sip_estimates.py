"""SIP instalments between statements (#297): ESTIMATES, shown apart and never treated as transactions.

A fund's units are what its last statement said (meta.statement_units, #295). A running SIP buys more units every
month after that, which the app cannot see until the next CAS. Each expected instalment after the statement's end is a
`portfolio_expected_txn` row:

* basis "sip-inference": the SIP portfolio.sip infers from the buys (instalments one calendar month apart after the
  last one, the median amount), only while its status is "on track" or "not in a statement yet" (a statement that
  already shows it missed, stopped or rejected projects nothing);
* basis "kite": an ACTIVE monthly Zerodha Coin SIP (GET /mf/sips) on the fund's ISIN, when Zerodha is connected;
  a COMPLETE Coin order (GET /mf/orders, last 7 days) gives that instalment's units, a REJECTED one marks it rejected.

Estimated units = (amount − stamp duty) ÷ that day's AMFI NAV. Stamp duty on the issue of mutual-fund units is
0.005 % of the amount since 1-Jul-2020 (Indian Stamp Act, 1899 as amended by the Finance Act, 2019; in force from
1-Jul-2020 by the Department of Revenue's notification of 30-Mar-2020) [U: the statute and notification pages could
not be fetched on 12-Oct-2026; the rate matches the CAS stamp-duty rows, e.g. ₹2.00 on ₹40,000 in tests/cas_synth].
The NAV day is the instalment's day, or the next business day when it is a weekend or an NSE trading holiday: an
approximation, because the applicable NAV legally depends on when the AMC realises the funds (SEBI's cut-off rules),
so an estimate can be a day's NAV off. No NAV for that day: the units are unknown (None), never 0, and retried.

The rows never enter portfolio_txn, lots, tax, XIRR, the snapshot's totals or `complete`. When a CAS is imported
(service.apply) each expected row up to its end is matched to a real buy of the same holding (day ±MATCH_DAYS, amount
±1 %): confirmed; otherwise, once the statement covers day + MATCH_DAYS: rejected when the statement lists a rejected
purchase in that window (meta.sip_rejections), else missed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from finresearch.fincalc.dates import is_business_day, next_business_day
from finresearch.fincalc.tax import add_months

STAMP_DUTY = Decimal("0.00005")  # 0.005 % of the amount (module docstring)
MATCH_DAYS = 3
MATCH_AMOUNT = Decimal("0.01")  # ±1 %
NAV_RETRY_DAYS = 60  # an unknown NAV is asked for again while its day is this recent
PAISE, UNITS_Q = Decimal("0.01"), Decimal("0.000001")
INFERENCE, KITE = "sip-inference", "kite"
EXPECTED, CONFIRMED, MISSED, REJECTED = "expected", "confirmed", "missed", "rejected"


def stamp_duty(amount: Decimal) -> Decimal:
    return (amount * STAMP_DUTY).quantize(PAISE, ROUND_HALF_UP)


def estimate_units(amount: Decimal, nav: Decimal | None) -> Decimal | None:
    """(amount − stamp duty) ÷ NAV, to 6 decimals; None when the NAV is unknown (never 0)."""
    if nav is None or nav <= 0:
        return None
    return ((amount - stamp_duty(amount)) / nav).quantize(UNITS_Q, ROUND_HALF_UP)


def nav_day(day: date, holidays: Iterable[date] = ()) -> date:
    hol = set(holidays)
    return day if is_business_day(day, hol) else next_business_day(day, hol)


def inferred_days(last: date, as_of: date, today: date) -> list[date]:
    """Monthly instalment days after the last one seen, after the statement's end (`as_of`), up to today."""
    out, k = [], 1
    while (d := add_months(last, k)) <= today:
        if d > as_of:
            out.append(d)
        k += 1
    return out


def kite_days(next_instalment: date, as_of: date, today: date) -> list[date]:
    """A Kite monthly SIP's past instalment days: one month apart before its next instalment, after `as_of`."""
    out, k = [], 1
    while (d := add_months(next_instalment, -k)) > as_of:
        if d <= today:
            out.append(d)
        k += 1
    return sorted(out)


@dataclass(frozen=True)
class Plan:
    """The expected instalments of one holding."""

    holding_id: int
    amount: Decimal
    days: tuple[date, ...]
    basis: str


def plan_inferred(h: Any, txns: list[Any], today: date) -> Plan | None:
    """The inferred SIP's instalments after the holding's statement units (portfolio.sip), or None."""
    from finresearch.portfolio.sip import NOT_YET, fund_buys, infer_sip, sip_status, statement_end

    end = statement_end(h)
    got = infer_sip(fund_buys(txns))
    if end is None or got is None:
        return None
    _, amount, _, last = got
    if sip_status(last, today, end, (h.meta or {}).get("sip_rejections") or []) not in ("on track", NOT_YET):
        return None
    days = inferred_days(last, end, today)
    return Plan(h.id, amount, tuple(days), INFERENCE) if days else None


def plan_kite(h: Any, sip: Any, today: date) -> Plan | None:
    """A Kite SIP's instalments after the holding's statement units: ACTIVE monthly SIPs with a next instalment only."""
    from finresearch.portfolio.sip import statement_end

    end = statement_end(h)
    if end is None or sip.status != "ACTIVE" or sip.frequency != "monthly" or sip.next_instalment is None:
        return None
    if not sip.amount or sip.amount <= 0:
        return None
    days = kite_days(sip.next_instalment, end, today)
    return Plan(h.id, sip.amount, tuple(days), KITE) if days else None


def store_plans(s: Any, plans: Iterable[Plan], holidays: Iterable[date] = ()) -> int:
    """Insert the planned instalments not stored yet (holding, day unique); returns how many were added. A stored
    row keeps its status: a later plan never reopens a confirmed, missed or rejected instalment."""
    from sqlalchemy.dialects.postgresql import insert

    from finresearch.db.models import PortfolioExpectedTxn

    hol, added = set(holidays), 0
    for p in plans:
        for d in p.days:
            got = s.execute(insert(PortfolioExpectedTxn).values(
                holding_id=p.holding_id, expected_day=d, amount=p.amount, nav_day=nav_day(d, hol), status=EXPECTED,
                basis=p.basis).on_conflict_do_nothing(index_elements=["holding_id", "expected_day"])
                .returning(PortfolioExpectedTxn.id)).first()  # fmt: skip
            added += got is not None
    return added


def _nav_for(rows: list[Any], code: str | None, isin: str | None, day: date) -> Decimal | None:
    """The NAV a scheme printed for exactly `day` (by AMFI code, else by either ISIN), or None."""
    for r in rows:
        if r.day != day or r.nav is None:
            continue
        if (code and r.code == code) or (isin and isin in (r.isin_growth, r.isin_reinvest)):
            return r.nav
    return None


async def fill_navs(s: Any, today: date, navs: Callable[[date], Awaitable[list[Any]]]) -> dict[str, int]:
    """Estimate the units of expected rows still without them, from `navs(day)` (AMFI rows of that day, all schemes):
    one call per NAV day, only for days up to today and at most NAV_RETRY_DAYS old. A failed call leaves them unknown."""
    from sqlalchemy import func, select

    from finresearch.db.models import PortfolioExpectedTxn, PortfolioHolding

    rows = s.scalars(select(PortfolioExpectedTxn).where(
        PortfolioExpectedTxn.status == EXPECTED, PortfolioExpectedTxn.est_units.is_(None),
        PortfolioExpectedTxn.nav_day <= today,
        PortfolioExpectedTxn.nav_day >= today - timedelta(days=NAV_RETRY_DAYS))).all()  # fmt: skip
    out = {"estimated": 0, "nav_unknown": 0, "nav_failed": 0}
    by_day: dict[date, list[Any]] = {}
    for r in rows:
        by_day.setdefault(r.nav_day, []).append(r)
    for day, items in sorted(by_day.items()):
        try:
            got = await navs(day)
        except Exception:
            out["nav_failed"] += len(items)
            continue
        for r in items:
            h = s.get(PortfolioHolding, r.holding_id)
            nav = _nav_for(got, h.scheme_code if h else None, h.isin if h else None, day)
            units = estimate_units(r.amount, nav)
            if units is None:
                out["nav_unknown"] += 1
                continue
            r.est_nav, r.est_units, r.updated_at = nav, units, func.now()
            out["estimated"] += 1
    s.flush()
    return out


def apply_kite_orders(s: Any, holding_isins: dict[int, str], orders: Iterable[Any]) -> dict[str, int]:
    """Kite's own record of an instalment (GET /mf/orders: the last 7 days): a COMPLETE buy of the fund's ISIN within
    MATCH_DAYS and ±1 % of an expected row gives its units and NAV (Kite's average price, on its exchange day); a
    REJECTED one marks the row rejected. Still estimates: only the CAS confirms."""
    from sqlalchemy import func, select

    from finresearch.db.models import PortfolioExpectedTxn

    out = {"kite_units": 0, "kite_rejected": 0}
    by_isin: dict[str, list[Any]] = {}
    for o in orders:
        if o.side == "BUY" and o.isin:
            by_isin.setdefault(o.isin.upper(), []).append(o)
    for hid, isin in holding_isins.items():
        cands = by_isin.get(isin.upper()) or []
        if not cands:
            continue
        for r in s.scalars(select(PortfolioExpectedTxn).where(PortfolioExpectedTxn.holding_id == hid,
                                                              PortfolioExpectedTxn.status == EXPECTED)):  # fmt: skip
            hit = _closest(r, [(o.day, o.amount, o) for o in cands])
            if hit is None:
                continue
            if hit.status == "COMPLETE" and hit.quantity and hit.quantity > 0:
                r.est_units, r.est_nav, r.nav_day = hit.quantity, hit.average_price, hit.day
                out["kite_units"] += 1
            elif hit.status == "REJECTED":
                r.status = REJECTED
                out["kite_rejected"] += 1
            r.updated_at = func.now()
    s.flush()
    return out


def _closest(row: Any, cands: list[tuple[date, Decimal | None, Any]], used: set[Any] | None = None) -> Any:
    """The candidate (day, amount, item) within ±MATCH_DAYS and ±1 % of the row's amount, closest in days."""
    best = None
    for day, amount, item in cands:
        if used is not None and item in used:
            continue
        gap = abs((day - row.expected_day).days)
        if gap > MATCH_DAYS or amount is None or abs(amount - row.amount) > row.amount * MATCH_AMOUNT:
            continue
        if best is None or gap < best[0]:
            best = (gap, item)
    return best[1] if best else None


def reconcile(s: Any, holding_id: int, end: date, rejections: Iterable[str]) -> dict[str, int]:
    """A CAS covering the holding up to `end` was imported: confirm each expected row matched by one of its buys (one
    buy per row, the closest day), and close the rows the statement covers (day + MATCH_DAYS ≤ end) as rejected
    (a rejected purchase in the window) or missed."""
    from sqlalchemy import func, select

    from finresearch.db.models import PortfolioExpectedTxn, PortfolioTxn

    rows = s.scalars(select(PortfolioExpectedTxn).where(
        PortfolioExpectedTxn.holding_id == holding_id, PortfolioExpectedTxn.status == EXPECTED,
        PortfolioExpectedTxn.expected_day <= end + timedelta(days=MATCH_DAYS)).order_by(PortfolioExpectedTxn.expected_day)).all()  # fmt: skip
    out = {CONFIRMED: 0, MISSED: 0, REJECTED: 0}
    if not rows:
        return out
    used = set(s.scalars(select(PortfolioExpectedTxn.matched_txn_id).where(
        PortfolioExpectedTxn.holding_id == holding_id, PortfolioExpectedTxn.matched_txn_id.is_not(None))))  # fmt: skip
    buys = [(t.day, abs(t.amount) if t.amount is not None else None, t.id)
            for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == holding_id,
                                                          PortfolioTxn.kind == "buy", PortfolioTxn.day <= end))
            if not (t.meta or {}).get("reinvest")]  # fmt: skip
    rej = [date.fromisoformat(d) for d in rejections]
    for r in rows:
        tid = _closest(r, buys, used)
        if tid is not None:
            used.add(tid)
            r.status, r.matched_txn_id = CONFIRMED, tid
        elif r.expected_day + timedelta(days=MATCH_DAYS) <= end:
            near = any(abs((d - r.expected_day).days) <= MATCH_DAYS for d in rej)
            r.status = REJECTED if near else MISSED
        else:
            continue  # the buy can still be in the next statement
        r.updated_at = func.now()
        out[r.status] += 1
    s.flush()
    return out


def reopen_unmatched(s: Any, txn_ids: Iterable[int]) -> int:
    """Rows confirmed by transactions about to be deleted (an import deleted) are expected again."""
    from sqlalchemy import update

    from finresearch.db.models import PortfolioExpectedTxn

    ids = list(txn_ids)
    if not ids:
        return 0
    res = s.execute(update(PortfolioExpectedTxn).where(PortfolioExpectedTxn.matched_txn_id.in_(ids))
                    .values(status=EXPECTED, matched_txn_id=None))  # fmt: skip
    return res.rowcount or 0


def _f(x: Decimal | None, nd: int) -> float | None:
    return None if x is None else round(float(x), nd)


def holding_estimate(rows: list[Any], since: str | None, price: Decimal | None) -> dict[str, Any] | None:
    """A fund row's estimate from its still-expected rows: never summed with unknowns (any instalment whose NAV is
    unknown makes the total units unknown), and its value labelled an estimate."""
    if not rows:
        return None
    known = [r.est_units for r in rows if r.est_units is not None]
    unknown = len(rows) - len(known)
    units = sum(known, Decimal(0)) if unknown == 0 else None
    n = len(rows)
    what = f"{n} instalment{'s' if n > 1 else ''} since {since or 'the last statement'}"
    if units is not None:
        label = f"+{units.quantize(Decimal('0.001'))} units estimated ({what}), waiting for statement"
    else:
        label = f"units estimated for {len(known)} of {n} instalments ({what}; {unknown} NAV unknown), " \
                "waiting for statement"  # fmt: skip
    value = units * price if units is not None and price is not None else None
    return {"instalments": n, "since": since, "units": _f(units, 4), "units_unknown": unknown,
            "amount": _f(sum((r.amount for r in rows), Decimal(0)), 2), "estimated_value": _f(value, 2),
            "basis": sorted({r.basis for r in rows}), "days": [r.expected_day.isoformat() for r in rows],
            "label": label}  # fmt: skip


def expected_rows(s: Any, holding_ids: Iterable[int]) -> dict[int, list[Any]]:
    from sqlalchemy import select

    from finresearch.db.models import PortfolioExpectedTxn

    ids = list(holding_ids)
    out: dict[int, list[Any]] = {}
    if not ids:
        return out
    for r in s.scalars(select(PortfolioExpectedTxn).where(PortfolioExpectedTxn.holding_id.in_(ids),
                                                          PortfolioExpectedTxn.status == EXPECTED)
                       .order_by(PortfolioExpectedTxn.expected_day)):  # fmt: skip
        out.setdefault(r.holding_id, []).append(r)
    return out


def portfolio_estimate(per_fund: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The portfolio's separate "estimated" line: never part of the summary's value, cost, XIRR or completeness."""
    if not per_fund:
        return None
    vals = [e["estimated_value"] for e in per_fund]
    value = round(sum(vals), 2) if all(v is not None for v in vals) else None
    n = sum(e["instalments"] for e in per_fund)
    return {"funds": len(per_fund), "instalments": n, "amount": round(sum(e["amount"] for e in per_fund), 2),
            "estimated_value": value, "value_unknown_funds": sum(1 for v in vals if v is None),
            "note": f"{n} SIP instalment{'s' if n > 1 else ''} in {len(per_fund)} fund{'s' if len(per_fund) > 1 else ''} "
                    "after the last statement: estimated, waiting for statement; not in the value, P&L, XIRR or tax"}  # fmt: skip
