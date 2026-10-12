"""An IPO allotment the decision journal knows, for a broker or CAS baseline that does not (#294).

Shares allotted in an IPO arrive in the demat account; a broker holdings sync or a holdings statement then records
them as an `opening` baseline (connectors.merge rule 1) with the broker's average price and no purchase date, so the
tax term stays "unknown" and the XIRR leaves the holding out. When the IPO's journal Decision records the allotment
(allotted lots and the issue price) and the baseline holds exactly those shares, the baseline stands for that
allotment: its acquisition date is the allotment date and its cost the issue price. The holding period of shares
allotted in a public issue runs from the allotment (the asset comes into existence then) [S: the usual reading; no
CBDT circular was re-read for this], and an IPO acquisition needs no STT for s.112A (CBDT Notification 60/2018,
1-Oct-2018, carve-out for public issues) [S].

The fill is a derived marker on the baseline (`meta.ipo_allotment`, like `superseded_by`): set and cleared on every
rebuild, never a second transaction and never an edit of the stored units, price or date (merge rule 5), so deleting
the Decision or the baseline undoes it. lots.opening_basis reads it.

Filled only when every check passes; otherwise the baseline is left as it is, with a warning saying why:
- the holding's ISIN (else NSE symbol) is the Decision's company's; allotted lots x lot size (the Decision's
  `lot_size` metric, else the IPO offer's) equals the baseline's units exactly; the baseline is dated on or after the
  allotment date (the IPO watch's, which is the expected date from the close under SEBI's T+3 timeline unless the
  watch was updated) and has no date of its own;
- the broker's average price, when it has one, agrees with the issue price within ISSUE_PRICE_TOLERANCE: the
  Decision's issue price may be the upper band filled in at listing (monitor.jobs) while the investor paid less (an
  employee or retail discount), so a disagreement is never resolved in favour of either figure;
- exactly one Decision matches (two allotments of one company would be ambiguous).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

ISSUE_PRICE_TOLERANCE = Decimal("0.005")  # 0.5 %: a broker's average of an allotment is the issue price
UNITS_TOL = Decimal("0.001")
MARK = "ipo_allotment"


@dataclass(frozen=True)
class Allotment:
    decision_id: int
    shares: Decimal
    issue_price: Decimal
    allotted_on: date | None
    source: str


def allotments(s: Session, h: Any) -> list[Allotment]:
    """The journal's recorded allotments of the holding's company (by ISIN, else NSE symbol)."""
    from finresearch.db.models import Company, Decision, IpoOffer, ResearchRun, Watch

    keys = []
    if h.isin:
        keys.append(Company.isin == h.isin.upper())
    if h.nse_symbol:
        keys.append(Company.nse_symbol == h.nse_symbol.upper())
    if not keys:
        return []
    rows = s.execute(select(Decision, Company.id).join(ResearchRun, ResearchRun.id == Decision.run_id)
                     .join(Company, Company.id == ResearchRun.company_id)
                     .where(or_(*keys), Decision.allotted_lots > 0, Decision.issue_price.is_not(None))).all()  # fmt: skip
    out = []
    for d, cid in rows:
        lot = ((d.inputs or {}).get("metrics") or {}).get("lot_size", {}).get("value")
        if not lot:
            lot = s.scalar(select(IpoOffer.lot_size).where(IpoOffer.company_id == cid, IpoOffer.lot_size.is_not(None))
                           .order_by(IpoOffer.id.desc()).limit(1))  # fmt: skip
        if not lot:
            continue
        w = s.scalars(select(Watch).where(Watch.company_id == cid, Watch.kind == "ipo")
                      .order_by(Watch.id.desc())).first()  # fmt: skip
        when = w.allotment_date if w is not None else None
        src = "IPO watch's allotment date" + (
            "" if (w and (w.meta or {}).get("listing_confirmed")) else " (expected)"
        )
        a = Allotment(d.id, Decimal(d.allotted_lots) * Decimal(str(lot)), Decimal(d.issue_price), when, src)
        # two research runs of one IPO can each carry a Decision: the same allotment recorded twice is one allotment
        if not any(
            (x.shares, x.issue_price, x.allotted_on) == (a.shares, a.issue_price, a.allotted_on) for x in out
        ):
            out.append(a)
    return out


def fills(s: Session, h: Any, txns: list[Any]) -> tuple[dict[int, dict[str, Any]], list[str]]:
    """({txn id: marker} for the baselines an allotment fills, warnings)."""
    from finresearch.portfolio.lots import superseded_openings

    if h.asset_type != "stock":
        return {}, []
    cands = [t for i, t in enumerate(txns) if i not in superseded_openings(txns) and t.kind == "opening"
             and not (t.meta or {}).get("acquired") and t.quantity]  # fmt: skip
    if not cands:
        return {}, []
    out: dict[int, dict[str, Any]] = {}
    warn: list[str] = []
    allot = allotments(s, h)
    for t in cands:
        hit = [a for a in allot if abs(a.shares - t.quantity) <= UNITS_TOL
               and (a.allotted_on is None or t.day >= a.allotted_on)]  # fmt: skip
        if not hit:
            continue
        if len(hit) > 1:
            warn.append("IPO allotment not applied to the baseline: more than one journal decision records this "
                        "allotment; enter the purchase date and cost by hand")  # fmt: skip
            continue
        a = hit[0]
        if a.allotted_on is None:
            warn.append("IPO allotment found in the journal but its allotment date is unknown: enter the purchase "
                        "date by hand")  # fmt: skip
            continue
        if t.price is not None and abs(t.price - a.issue_price) > a.issue_price * ISSUE_PRICE_TOLERANCE:
            warn.append(f"IPO allotment not applied: the broker's average ₹{t.price:f} differs from the journal's "
                        f"issue price ₹{a.issue_price:f} (a discount, or an upper band filled in at listing?): enter "
                        "the cost and date by hand")  # fmt: skip
            continue
        out[t.id] = {"decision_id": a.decision_id, "acquired": a.allotted_on.isoformat(),
                     "price": format(a.issue_price, "f"), "acquired_source": a.source,
                     "broker_price": None if t.price is None else format(t.price, "f")}  # fmt: skip
    return out, warn


def apply(s: Session, h: Any, txns: list[Any]) -> list[str]:
    """Set or clear the marker on the holding's baselines; returns the warnings for the lot notes."""
    found, warn = fills(s, h, txns)
    for t in txns:
        m = dict(t.meta or {})
        if found.get(t.id) != m.get(MARK):
            m.pop(MARK, None)
            t.meta = {**m, **({MARK: found[t.id]} if t.id in found else {})}
    return warn


def rebuild_company(s: Session, company_id: int | None) -> int:
    """Rebuild the stock holdings of a company whose journal allotment changed, so a baseline is filled (or the fill
    is cleared) at once rather than at the next import. Returns the number rebuilt."""
    from finresearch.db.models import Company, PortfolioHolding
    from finresearch.portfolio.service import rebuild

    co = s.get(Company, company_id) if company_id else None
    if co is None:
        return 0
    keys = [PortfolioHolding.isin == co.isin] if co.isin else []
    keys += [PortfolioHolding.nse_symbol == co.nse_symbol] if co.nse_symbol else []
    if not keys:
        return 0
    hs = s.scalars(select(PortfolioHolding).where(PortfolioHolding.asset_type == "stock", or_(*keys))).all()
    for h in hs:
        rebuild(s, h.id)
    return len(hs)
