"""The portfolio as the API shows it: holdings with P&L and XIRR, allocation, P&L over time, dividends, and the tax
view. Reads the database, takes prices from valuation.fetch_prices; all arithmetic is fincalc's or lots'."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioDisposal, PortfolioHolding, PortfolioLot, PortfolioTxn
from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import RULES, VERIFIED_SOURCE, VERIFIED_THROUGH_FY, VERIFY_NOTE, fy_label
from finresearch.portfolio import elss
from finresearch.portfolio.limits import BONDS_SECTOR, sector_label
from finresearch.portfolio.tax import (
    DisposalRow,
    HoldingTax,
    OpenLot,
    auto_tax_class,
    evaluate,
    fy_summary,
    harvest,
    is_debt_security,
    is_listed,
)
from finresearch.portfolio.valuation import (
    CAP_LIST,
    STATEMENT_MAX_AGE_TRADING_DAYS,
    PriceInfo,
    asset_label,
    cap_bucket,
    cash_flows,
    fund_cap_bucket,
    statement_stale,
    xirr_or_reason,
)

ZERO = Decimal(0)
PRIVACY = ("Your portfolio stays on this machine: in the local database and data/portfolio/ (gitignored). It is never "
           "sent to an LLM, and CAS passwords are never stored.")  # fmt: skip


@dataclass
class Loaded:
    holdings: list[PortfolioHolding]
    txns: dict[int, list[PortfolioTxn]]
    lots: dict[int, list[PortfolioLot]]
    disposals: dict[int, list[PortfolioDisposal]]


def load(s: Session) -> Loaded:
    hs = list(s.scalars(select(PortfolioHolding).order_by(PortfolioHolding.name, PortfolioHolding.account)))
    txns: dict[int, list[PortfolioTxn]] = defaultdict(list)
    for t in s.scalars(select(PortfolioTxn).order_by(PortfolioTxn.day, PortfolioTxn.id)):
        txns[t.holding_id].append(t)
    lots: dict[int, list[PortfolioLot]] = defaultdict(list)
    for lot in s.scalars(select(PortfolioLot).order_by(PortfolioLot.id)):
        lots[lot.holding_id].append(lot)
    disp: dict[int, list[PortfolioDisposal]] = defaultdict(list)
    for d in s.scalars(select(PortfolioDisposal).order_by(PortfolioDisposal.sold, PortfolioDisposal.id)):
        disp[d.holding_id].append(d)
    return Loaded(hs, txns, lots, disp)


def tax_class_of(h: PortfolioHolding, category: str | None = None) -> tuple[str, str, str]:
    """(effective class, automatic class, why the automatic class)."""
    auto, why = auto_tax_class(h.asset_type, h.name, category or h.category, (h.meta or {}).get("cas_type"),
                               h.nse_symbol, h.isin)  # fmt: skip
    return (h.tax_class or auto), auto, why


def holding_tax(h: PortfolioHolding, category: str | None = None) -> HoldingTax:
    eff, _, _ = tax_class_of(h, category)
    from finresearch.portfolio.service import actions_of

    blocked = tuple((date.fromisoformat(a["ex_date"]), a["reason"]) for a in actions_of(h.meta))
    return HoldingTax(h.id, h.name, h.account, h.isin, eff, is_listed(h.asset_type, h.name, h.meta), h.fmv_2018,
                      bool((h.meta or {}).get("sgb_original_subscriber")), blocked)  # fmt: skip


def _f(x: Decimal | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _signal(h: PortfolioHolding, price: PriceInfo | None,
            imap: dict[str, list[str | None]] | None = None) -> dict[str, str] | None:  # fmt: skip
    from finresearch.disclosures.store import stock_key

    key = stock_key(h.isin, h.nse_symbol, h.bse_code, imap or {}) if h.asset_type == "stock" else None
    if key:  # an ISIN-only holding (a broker holdings statement) resolves through the ISIN map (#200)
        return {"asset": "stock", "instrument": key, "href": f"/stocks/{key}"}
    code = h.scheme_code or (price.scheme_code if price else None)
    if h.asset_type == "mf" and code:
        return {"asset": "fund", "instrument": code, "href": f"/funds/{code}"}
    return None


def xirr_exclusions(excluded: Counter[str]) -> str:
    """Why the overall XIRR leaves holdings out, from each holding's own reason (cash_flows / no current price)."""
    from finresearch.portfolio.valuation import NO_PURCHASE_DATE

    phrase = {
        NO_PURCHASE_DATE: "whose purchase date is unknown (e.g. a broker holdings baseline: import an older order "
                          "history to include them)",
        "opening balance with an unknown cost": "without a known cost",
        "no current price": "without a current price",
    }  # fmt: skip
    parts = [f"{n} {phrase.get(why, f'with {why}')}" for why, n in excluded.most_common()]
    return f"excludes {sum(excluded.values())} holding(s): " + "; ".join(parts)


def snapshot(s: Session, prices: dict[int, PriceInfo], today: date) -> dict[str, Any]:
    data = load(s)
    rows, all_flows = [], []
    alloc: dict[str, dict[str, Decimal]] = {"asset": defaultdict(Decimal), "sector": defaultdict(Decimal),
                                            "cap": defaultdict(Decimal)}  # fmt: skip
    tot = defaultdict(Decimal)
    unknown_cost = unpriced = stale = with_actions = 0
    from finresearch.disclosures.store import isin_map
    from finresearch.portfolio.service import actions_of

    imap = isin_map(s)
    excluded: Counter[str] = Counter()  # holdings left out of the overall XIRR, by the reason cash_flows gave
    for h in data.holdings:
        p = prices.get(h.id) or PriceInfo(error="not priced")
        category = p.category or h.category
        eff, auto, why = tax_class_of(h, category)
        open_lots = [lot for lot in data.lots.get(h.id, []) if lot.open_quantity > Decimal("0.0005")]
        units = sum((lot.open_quantity for lot in open_lots), ZERO)
        known = all(lot.cost_per_unit is not None for lot in open_lots)
        actions = actions_of(h.meta)
        # an unsupported corporate action past its ex-date (#237): the open lots' cost is not what they say until the
        # user enters the allocation, so it is unknown, never the pre-action cost
        if units > 0 and any(a["ex_date"] <= today.isoformat() for a in actions):
            known = False
        with_actions += 1 if actions and units > 0 else 0
        cost = sum(
            (lot.cost_per_unit * lot.open_quantity for lot in open_lots if lot.cost_per_unit is not None),
            ZERO,
        )
        value = p.price * units if p.price is not None and units > 0 else None
        stale_why = statement_stale(p, today) if value is not None else None
        disp = data.disposals.get(h.id, [])
        realised = sum((d.proceeds - d.cost for d in disp if d.cost is not None), ZERO)
        # a sale out of a lot whose cost is unknown (an opening balance, a transfer-in, units beyond the lots) has an
        # unknown gain: it is not in `realised`, and it is counted so the figure never reads as the whole story (#238)
        no_cost = [d for d in disp if d.cost is None]
        divs = sum(
            (abs(t.amount) for t in data.txns.get(h.id, []) if t.kind == "dividend" and t.amount), ZERO
        )
        flows, why_not = cash_flows(data.txns.get(h.id, []), value, today)
        if why_not is None and units > 0 and value is None:
            why_not = "no current price"  # without today's value the flows alone would read as a large loss
        x, x_reason = xirr_or_reason(flows, today) if why_not is None else (None, why_not)
        if stale_why and x is not None:
            x_reason = f"today's value uses an old price: {stale_why}"
        if why_not is None:
            all_flows += flows[:-1] if value else flows
            tot["xirr_value"] += value or ZERO
        elif units > 0 or why_not is not None:
            excluded[why_not] += 1
        unreal = (value - cost) if value is not None and known and units > 0 else None
        det = elss.detect(h.asset_type, h.name, category) if units > 0 else None
        lock = elss.lockin(open_lots, today, p.price, det) if det is not None else None
        if units > 0:
            unknown_cost += 0 if known else 1
            unpriced += 1 if value is None else 0
            stale += 1 if stale_why else 0
        # the display sector every view groups by (limits.sector_label): an ETF or fund by what it holds, never NSE's
        # "Mutual Fund Scheme - ETF" industry; a blank or "-" industry is Unclassified (#264)
        sector = sector_label(h.asset_type, h.sector or (p.industry if h.asset_type == "stock" else None),
                              h.nse_symbol, h.name, eff, h.isin)  # fmt: skip
        if value is not None:
            tot["value"] += value
            debt = h.asset_type == "stock" and is_debt_security(h.isin, h.name, h.nse_symbol)
            label = BONDS_SECTOR if debt else asset_label(h.asset_type, eff)
            alloc["asset"][label] += value
            alloc["sector"][sector] += value
            bucket = cap_bucket(p.market_cap_cr) if h.asset_type == "stock" and eff == "equity" else (
                fund_cap_bucket(category, eff) if h.asset_type == "mf" else "Not equity")  # fmt: skip
            alloc["cap"][bucket] += value
        if known:
            tot["cost"] += cost
        tot["realised"] += realised
        tot["realised_unknown"] += len(no_cost)
        tot["realised_unknown_proceeds"] += sum((d.proceeds for d in no_cost), ZERO)
        tot["dividends"] += divs
        if unreal is not None:
            tot["unrealised"] += unreal
        rows.append({
            "id": h.id, "name": h.name, "account": h.account, "asset_type": h.asset_type, "ikey": h.ikey,
            "isin": h.isin, "nse_symbol": h.nse_symbol, "bse_code": h.bse_code,
            "scheme_code": h.scheme_code or p.scheme_code, "category": category, "sector": h.sector or p.industry, "sector_label": sector,
            "tax_class": eff, "tax_class_auto": auto, "tax_class_why": why, "tax_class_override": h.tax_class,
            "listed": is_listed(h.asset_type, h.name, h.meta), "fmv_2018": _f(h.fmv_2018, 4),
            "sgb_original_subscriber": bool((h.meta or {}).get("sgb_original_subscriber")),
            "units": _f(units, 4), "cost": _f(cost) if known else None, "cost_known": known,
            "avg_cost": _f(cost / units, 4) if known and units > 0 else None,
            "price": _f(p.price, 4), "price_as_of": p.as_of, "price_source": p.source, "price_error": p.error,
            "price_note": p.note, "price_stale": stale_why is not None, "price_stale_reason": stale_why,
            "value": _f(value), "unrealised": _f(unreal),
            "unrealised_pct": _f(unreal / cost * 100) if unreal is not None and cost > 0 else None,
            "realised": _f(realised), "realised_unknown": len(no_cost), "dividends": _f(divs), "xirr": x, "xirr_reason": x_reason,
            "market_cap_cr": _f(p.market_cap_cr), "cap_bucket": (cap_bucket(p.market_cap_cr) if h.asset_type == "stock" and eff == "equity"
                                                                  else "Not equity" if h.asset_type == "stock"
                                                                  else fund_cap_bucket(category, eff)),
            "lots": len(open_lots), "closed": units <= 0, "signal": _signal(h, p, imap), "elss": lock,
            "warnings": (h.meta or {}).get("lot_warnings") or [], "pending_actions": actions,
            "sources": sorted({t.source for t in data.txns.get(h.id, [])}),
            "broker_baseline": any(t.kind == "opening" and (t.meta or {}).get("baseline")
                                   for t in data.txns.get(h.id, [])),
        })  # fmt: skip
    ox, ox_reason = xirr_or_reason(
        [*all_flows, (today, tot["xirr_value"])] if tot["xirr_value"] else all_flows, today
    )
    if excluded and ox is not None:
        ox_reason = xirr_exclusions(excluded)
    stale_note = (f"{stale} holding(s) valued at a statement price more than {STATEMENT_MAX_AGE_TRADING_DAYS} "
                  "trading days old") if stale else None  # fmt: skip
    if stale_note and ox is not None:
        ox_reason = f"{ox_reason}; {stale_note}" if ox_reason else stale_note
    tl = timeline(data)
    realised_unknown = int(tot["realised_unknown"])
    # complete: every open holding has a current (not stale) price and a known cost, and every sale a known cost
    complete = (
        unpriced == 0 and stale == 0 and unknown_cost == 0 and realised_unknown == 0 and tot["value"] > 0
    )
    return {
        "as_of": today.isoformat(), "holdings": rows, "complete": complete,
        "invested": tl[-1]["invested"] if tl else 0.0,
        "summary": {"value": _f(tot["value"]), "cost": _f(tot["cost"]), "unrealised": _f(tot["unrealised"]),
                    "realised": _f(tot["realised"]), "realised_unknown": realised_unknown,
                    "realised_unknown_proceeds": _f(tot["realised_unknown_proceeds"]),
                    "realised_note": (f"excludes {realised_unknown} sale(s) whose cost is unknown "
                                      f"(₹{float(tot['realised_unknown_proceeds']):,.0f} of proceeds): enter the cost "
                                      "and date of those units for the full figure") if realised_unknown else None,
                    "dividends": _f(tot["dividends"]), "xirr": ox,
                    "xirr_reason": ox_reason, "holdings": sum(1 for r in rows if not r["closed"]),
                    "unknown_cost": unknown_cost, "unpriced": unpriced, "stale": stale, "stale_note": stale_note,
                    "pending_actions": with_actions},
        "allocation": {k: [{"label": lab, "value": _f(v)} for lab, v in sorted(d.items(), key=lambda kv: -kv[1])]
                       for k, d in alloc.items()},
        "cap_list": {k: str(v) for k, v in CAP_LIST.items()},
        "timeline": tl,
        "dividends": dividends(data),
        "privacy": PRIVACY,
    }  # fmt: skip


def timeline(data: Loaded) -> list[dict[str, Any]]:
    """Month-end cumulative net invested (buys - sales proceeds), realised P&L and dividends. Past market values are
    not stored, so there is no value line.

    An opening balance whose cost is known (an entered cost and date, or a broker baseline at its average cost) counts
    as invested on its day, at that cost: its units are in the value, so leaving the cost out would make the value
    jump with no money put in (the drawdown/weekly-change metrics read `invested` from here). Openings with an unknown
    cost, and statement openings the lots ignore (an older statement covers them), are not counted."""
    from finresearch.portfolio.lots import superseded_openings

    by_month: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for h in data.holdings:
        txns = data.txns.get(h.id, [])
        skip = superseded_openings(txns)
        for i, t in enumerate(txns):
            m = t.day.strftime("%Y-%m")
            gross = abs(t.amount) if t.amount is not None else ((t.quantity or 0) * (t.price or 0))
            meta = t.meta or {}
            if t.kind == "opening":
                if (
                    i not in skip
                    and t.price is not None
                    and (meta.get("acquired") or meta.get("cost_basis") == "broker_average")
                ):
                    by_month[m]["invested"] += gross + (t.charges or 0)  # the lot's cost (lots.build_lots)
            elif t.kind == "buy" and not meta.get("reinvest"):
                by_month[m]["invested"] += gross + (t.charges or 0)
            elif t.kind == "sell":
                by_month[m]["invested"] -= gross - (t.charges or 0)
            elif t.kind == "dividend" and t.amount:
                by_month[m]["dividends"] += abs(t.amount)
        for d in data.disposals.get(h.id, []):
            if d.cost is not None:
                by_month[d.sold.strftime("%Y-%m")]["realised"] += d.proceeds - d.cost
    out, acc = [], defaultdict(Decimal)
    for m in sorted(by_month):
        for k in ("invested", "realised", "dividends"):
            acc[k] += by_month[m][k]
        out.append({"date": f"{m}-01", "invested": _f(acc["invested"]), "realised": _f(acc["realised"]),
                    "dividends": _f(acc["dividends"])})  # fmt: skip
    return out


def dividends(data: Loaded) -> dict[str, Any]:
    names = {h.id: h.name for h in data.holdings}
    items = [{"day": t.day.isoformat(), "holding_id": t.holding_id, "name": names.get(t.holding_id), "amount": _f(abs(t.amount)),
              "reinvested": bool((t.meta or {}).get("reinvest"))}
             for h in data.holdings for t in data.txns.get(h.id, []) if t.kind == "dividend" and t.amount]  # fmt: skip
    by_fy: dict[int, Decimal] = defaultdict(Decimal)
    for it in items:
        by_fy[fiscal_year(date.fromisoformat(it["day"]))] += Decimal(str(it["amount"]))
    return {"items": sorted(items, key=lambda x: x["day"], reverse=True),
            "by_fy": [{"fy": fy, "label": fy_label(fy), "amount": _f(v)} for fy, v in sorted(by_fy.items())],
            "note": "Dividends are taxed at your slab rate; TDS deducted by the company counts towards it."}  # fmt: skip


# --------------------------------------------------------------------------- tax
def disposal_rows(data: Loaded, categories: dict[int, str | None] | None = None) -> list[DisposalRow]:
    categories = categories or {}
    txn_meta = {t.id: t.meta or {} for ts in data.txns.values() for t in ts}
    out = []
    for h in data.holdings:
        ht = holding_tax(h, categories.get(h.id))
        for d in data.disposals.get(h.id, []):
            m = txn_meta.get(d.txn_id, {})
            out.append(evaluate(DisposalRow(ht, d.acquired, d.sold, d.quantity, d.cost, d.proceeds, d.stt_paid,
                                            d.origin, rbi_redemption=bool(m.get("rbi_redemption")),
                                            held_to_maturity=bool(m.get("held_to_maturity")), txn_id=d.txn_id)))  # fmt: skip
    return out


def disposal_json(r: DisposalRow) -> dict[str, Any]:
    c = r.cls
    return {"holding_id": r.holding.id, "name": r.holding.name, "account": r.holding.account, "fy": r.fy,
            "fy_label": fy_label(r.fy), "tax_class": r.holding.tax_class,
            "acquired": r.acquired.isoformat() if r.acquired else None, "sold": r.sold.isoformat(),
            "quantity": _f(r.quantity, 4), "cost": _f(r.cost), "tax_cost": _f(r.tax_cost), "proceeds": _f(r.proceeds),
            "gain": _f(r.gain), "term": c.term if c else None, "bucket": c.bucket if c else None,
            "rate_pct": None if c is None or c.rate is None else float(c.rate * 100),
            "slab": bool(c and c.rate is None and c.term == "short"), "rule": c.rule.id if c and c.rule else None,
            "holding_days": c.holding_days if c else None, "stt_paid": r.stt_paid, "notes": r.notes}  # fmt: skip


def tax_view(s: Session, prices: dict[int, PriceInfo], today: date, slab: Decimal) -> dict[str, Any]:
    data = load(s)
    cats = {hid: p.category for hid, p in prices.items()}
    rows = disposal_rows(data, cats)
    fys = sorted({r.fy for r in rows} | {fiscal_year(today)}, reverse=True)
    lots_by: dict[int, list[OpenLot]] = {}
    px: dict[int, Decimal] = {}
    for h in data.holdings:
        ht = holding_tax(h, cats.get(h.id))
        open_ = [lot for lot in data.lots.get(h.id, []) if lot.open_quantity > Decimal("0.0005")]
        if elss.detect(h.asset_type, h.name, cats.get(h.id) or h.category) is not None:
            # an ELSS lot within its 3-year lock-in cannot be redeemed, so harvesting never suggests it. The locked
            # lots are the newest (FIFO), so what is left is still a FIFO prefix
            open_ = [lot for lot in open_ if lot.acquired is None or today >= elss.unlock_date(lot.acquired)]
        lots = [
            OpenLot(ht, lot.acquired, lot.open_quantity, lot.cost_per_unit, lot.stt_paid) for lot in open_
        ]
        if lots:
            lots_by[h.id] = lots
        p = prices.get(h.id)
        if p is not None and p.price is not None and p.source and "statement" not in p.source:
            px[h.id] = p.price  # harvesting needs a live price, not an old statement NAV
    types = {h.id: h.asset_type for h in data.holdings}
    return {"as_of": today.isoformat(), "fys": [fy_summary(rows, fy, slab) for fy in fys],
            "disposals": [disposal_json(r) for r in sorted(rows, key=lambda r: (r.sold, r.holding.name), reverse=True)],
            "harvest": harvest(rows, lots_by, px, types, today, slab),
            "rules": [r.to_json() for r in RULES], "verify": VERIFY_NOTE,
            "rules_verified_through": {"fy": VERIFIED_THROUGH_FY, "label": fy_label(VERIFIED_THROUGH_FY),
                                       "source": VERIFIED_SOURCE},
            "caveats": ["Surcharge (capped at 15 % on these gains) and the s.87A rebate (not available against "
                        "special-rate tax) are not modelled.",
                        "Losses carried forward from earlier years are not included.",
                        "Indexation for transfers before 23-Jul-2024 is not modelled.",
                        "F&O and intraday trades are business income and are not in this computation."]}  # fmt: skip


def export_rows(s: Session, prices: dict[int, PriceInfo] | None = None) -> list[DisposalRow]:
    data = load(s)
    return disposal_rows(data, {hid: p.category for hid, p in (prices or {}).items()})
