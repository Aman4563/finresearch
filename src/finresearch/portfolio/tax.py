"""Capital-gains tax on the portfolio: tax class per holding, realised gains per financial year with the ₹1.25 lakh
exemption meter, tax-loss and gain-harvesting suggestions, and a CSV for a chartered accountant.

All tax arithmetic is fincalc.tax's dated rule table; this module maps holdings and lots onto it. Everything is a
personal estimate (fincalc.tax.VERIFY_NOTE).

Harvesting, in short (research notes 30-Sep-2026):
- India has **no wash-sale rule**: selling to book a loss (or a gain inside the exemption) and buying back is allowed.
  The rebuy resets the cost and the 12-month clock.
- Sell and rebuy on **different days**: the same stock bought and sold on one day in one account is an intraday
  trade (speculative business income), not a capital gain.
- Costs: STT 0.1 % on each side of a delivery trade (verified, Budget 2024 memo), stamp duty on the buy, brokerage,
  exchange and DP charges, the bid-ask spread and a day out of the market; funds: exit load (check the scheme) and a
  NAV gap.
- Avoid the s.94(7)/(8) dividend- and bonus-stripping windows (a loss bought around a record date is disallowed), and
  the sham-transaction doctrine is a (small) litigation risk. GAAR needs a tax benefit above ₹3 crore.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import (
    GRANDFATHER_DATE,
    VERIFY_NOTE,
    Classification,
    Gain,
    classify,
    fy_label,
    fy_tax,
    tax_delta,
    unverified_note,
)

STT_DELIVERY = Decimal("0.001")  # each side of a delivery equity trade (Budget 2024 memo p.46) [V]
STAMP_DELIVERY_BUY = Decimal("0.00015")  # 0.015 % on the buy (Indian Stamp Act schedule via broker pages) [S]
STAMP_MF_BUY = Decimal("0.00005")  # 0.005 % on MF purchases [S]
HARVEST_NOTES = [
    "India has no wash-sale rule: you may sell and buy back the same security. The buyback resets your cost and the "
    "12-month holding clock.",
    "Buy back on a later trading day: a same-day sell and buy in one account is an intraday trade (business income), "
    "not a capital gain.",
    "Costs: STT 0.1 % on each side, stamp duty, brokerage and DP charges, the spread and a day out of the market; for "
    "funds, any exit load and the NAV gap.",
    "Avoid buying around a dividend or bonus record date and selling at a loss soon after: s.94(7)/(8) disallow such "
    "losses.",
]


# --------------------------------------------------------------------------- tax class of a holding
EQUITY_CATEGORY = ("equity scheme", "elss", "aggressive hybrid", "arbitrage", "equity savings")
DEBT_CATEGORY = ("debt scheme", "conservative hybrid", "liquid", "overnight", "money market", "gilt", "credit risk",
                 "banking and psu", "corporate bond", "duration", "floater", "dynamic bond")  # fmt: skip
OTHER_WORDS = ("gold", "silver", "overseas", "international", "global", "nasdaq", "s&p 500", "us equity", "fund of fund",
               "fof")  # fmt: skip
DEBT_WORDS = ("liquid", "gilt", "g-sec", "gsec", "sdl", "bond", "debt", "crisil", "money market", "overnight",
              "target maturity", "psu", "treasury", "t-bill", "duration", "credit risk", "floater", "income fund",
              "arbitrage")  # fmt: skip
EQUITY_NAME_WORDS = ("flexi cap", "large cap", "mid cap", "small cap", "multi cap", "large & mid", "elss", "tax saver",
                     "focused", "contra", "dividend yield", "value fund", "equity")  # fmt: skip
EQUITY_INDEX_WORDS = ("nifty", "sensex", "bse ", "midcap", "smallcap", "next 50", "bank", "momentum", "quality",
                      "value", "alpha", "equity")  # fmt: skip


# A debenture or bond held as a listed security (an NCD bought on the exchange arrives as a "stock" holding). Indian
# ISINs carry the security type in characters 8-9: "01" equity shares, "07"/"08" debentures and bonds (observed on
# exchange-listed NCDs and bonds, e.g. INE027E07998, INE549K07IQ3) [unverified against NSDL's code list]; the name
# catches the rest. ETFs are excluded first ("Bharat Bond ETF" is a fund unit).
_DEBT_NAME = re.compile(r"\bNCDs?\b|-NCD\b|\bdebentures?\b|\bbonds?\b|\bnon[- ]convertible\b", re.I)


def is_debt_security(isin: str | None, name: str | None, symbol: str | None = None) -> bool:
    """True for a listed debenture/bond (NCD) held like a share: taxed as an 'other' listed security, never equity."""
    n, sym = name or "", (symbol or "").upper()
    if (
        re.search(r"\bETF\b|\bBEES\b", n, re.I)
        or sym.endswith(("BEES", "ETF", "IETF"))
        or "sovereign gold" in n.lower()
    ):
        return False
    code = (isin or "").upper()
    if len(code) == 12 and code.startswith("INE") and code[7:9] in ("07", "08"):
        return True
    return bool(_DEBT_NAME.search(n))


def auto_tax_class(asset_type: str, name: str, category: str | None, cas_type: str | None,
                   symbol: str | None, isin: str | None = None) -> tuple[str, str]:  # fmt: skip
    """(tax class, why) from what the app knows. The >65 % tests need the fund's holdings, which the app does not
    have, so this maps SEBI categories and names; the user can override it per holding."""
    n = f" {name.lower()} "
    sym = (symbol or "").upper()
    if asset_type == "stock":
        if sym.startswith("SGB") or "sovereign gold" in n:
            return "sgb", "Sovereign Gold Bond"
        metal = any(w in n or w.upper() in sym for w in ("gold", "silver"))
        if metal and ("etf" in n or "bees" in n or sym.endswith(("BEES", "ETF", "IETF"))):
            return "other_mf", "gold/silver ETF (a fund unit, not equity)"
        if is_debt_security(isin, name, symbol):
            return (
                "other",
                "listed debenture/bond (NCD): an 'other' listed security, not equity (no STT, no s.112A)",
            )
        if sym in ("LIQUIDBEES", "LIQUIDCASE", "LIQUIDETF") or any(
            w in n for w in ("liquid", "gilt", "bharat bond")
        ):
            return "debt_mf", "debt/liquid ETF"
        return "equity", "listed share (or equity ETF)"
    if asset_type != "mf":
        return "other", "not a share or a fund"
    cat = (category or "").lower()
    if any(w in n for w in OTHER_WORDS) or "fund of funds" in cat or "fof" in cat:
        return "other_mf", "gold/international/fund-of-funds: neither equity-oriented nor debt"
    if any(c in cat for c in EQUITY_CATEGORY):
        return "equity", f"AMFI category {category}"
    if any(c in cat for c in DEBT_CATEGORY):
        return "debt_mf", f"AMFI category {category}"
    if not cat and "arbitrage" in n:
        return "equity", "arbitrage fund (by name): equity-oriented"
    if not cat and any(w in n for w in EQUITY_NAME_WORDS) and not any(w in n for w in DEBT_WORDS):
        return "equity", "equity fund (by name; no AMFI category matched)"
    if "index" in cat or "etf" in cat or not cat:
        if any(w in n for w in DEBT_WORDS):
            return "debt_mf", "debt index fund/ETF (by name)"
        if any(w in n for w in EQUITY_INDEX_WORDS):
            return "equity", "equity index fund/ETF (by name)"
    if cas_type and cas_type.upper() == "EQUITY":
        return "equity", "casparser scheme type EQUITY"
    if cas_type and cas_type.upper() == "DEBT":
        return "debt_mf", "casparser scheme type DEBT"
    return "other_mf", f"unclassified fund ({category or 'no category'}): set its tax class"


def is_listed(asset_type: str, name: str, meta: dict[str, Any] | None = None) -> bool:
    meta = meta or {}
    if "listed" in meta:
        return bool(meta["listed"])
    return asset_type == "stock" or " etf" in f" {name.lower()}"


# --------------------------------------------------------------------------- realised rows
@dataclass
class HoldingTax:
    """What the tax view needs about one holding."""

    id: int
    name: str
    account: str
    isin: str | None
    tax_class: str
    listed: bool
    fmv_2018: Decimal | None
    sgb_original: bool = False


@dataclass
class DisposalRow:
    holding: HoldingTax
    acquired: date | None
    sold: date
    quantity: Decimal
    cost: Decimal | None  # actual
    proceeds: Decimal
    stt_paid: bool
    origin: str
    rbi_redemption: bool = False
    held_to_maturity: bool = False
    txn_id: int | None = None
    # computed
    cls: Classification | None = None
    tax_cost: Decimal | None = None  # after grandfathering
    gain: Decimal | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def fy(self) -> int:
        return fiscal_year(self.sold)


def evaluate(row: DisposalRow) -> DisposalRow:
    """Classify a disposal and compute its taxable gain (grandfathering applied where it can be)."""
    h = row.holding
    if row.origin == "intraday":
        row.cls = Classification(
            "unknown", None, None, "intraday", "intraday trade: speculative business income"
        )
        row.notes.append("Intraday (same-day buy and sell): speculative business income, not capital gains")
        return row
    row.cls = classify(h.tax_class, row.acquired, row.sold, listed=h.listed, stt_paid=row.stt_paid,
                       rbi_redemption=row.rbi_redemption, original_subscriber=h.sgb_original,
                       held_to_maturity=row.held_to_maturity)  # fmt: skip
    if row.cost is None:
        row.cls = Classification(
            "unknown", row.cls.rule, None, "unknown", "cost unknown: enter it to compute tax"
        )
        row.notes.append("Cost unknown (opening balance or transfer-in): enter the cost and date")
        return row
    if row.cls.term == "unknown":
        row.notes.append(f"Term unknown ({row.cls.reason}): the gain can't be classified short- or long-term, so it is "
                         "left out of the tax")  # fmt: skip
    cost = row.cost
    # s.55(2)(ac) (s.90(7) of the 2025 Act) steps the cost up only for a long-term capital asset "referred to in
    # section 112A", i.e. a transfer taxed under the equity regime. An equity holding sold without STT is classified
    # under the general rules (fincalc.tax.classify) and keeps its actual cost.
    if (row.cls.bucket == "equity_lt" and row.acquired is not None
            and row.acquired <= GRANDFATHER_DATE):  # fmt: skip
        if h.fmv_2018 is None:
            row.notes.append("Bought before 1-Feb-2018: enter the FMV on 31-Jan-2018 for grandfathering (actual cost "
                             "used, which may overstate the gain)")  # fmt: skip
        else:
            fmv_total = h.fmv_2018 * row.quantity
            cost = max(cost, min(fmv_total, row.proceeds))
            row.notes.append(
                "Grandfathered: cost = higher of actual and lower of (31-Jan-2018 FMV, sale value)"
            )
    row.tax_cost = cost
    row.gain = row.proceeds - cost if row.cls.term != "exempt" else Decimal(0)
    return row


def gains_of(rows: Iterable[DisposalRow]) -> list[Gain]:
    """Capital-gains rows for fincalc.tax.fy_tax. Intraday trades are speculative business income, not capital
    gains: they are left out here (fy_summary counts them separately) instead of being reported as disposals with an
    "unknown acquisition date or cost"."""
    return [Gain(_amount(r), r.cls, r.sold, str(r.txn_id or ""))
            for r in rows if r.cls is not None and r.origin != "intraday"]  # fmt: skip


def _amount(r: DisposalRow) -> Decimal:
    """The Gain amount fy_tax sees. An exempt transfer (an SGB redeemed by RBI) has no taxable gain (`r.gain` 0), but
    fy_tax reports the exempt gain itself (`exempt_total`), which is proceeds - cost: before #240's golden case g09
    it was always ₹0. fy_tax never taxes an exempt Gain, so this changes no tax."""
    if r.cls is not None and r.cls.term == "exempt" and r.tax_cost is not None:
        return r.proceeds - r.tax_cost
    return r.gain if r.gain is not None else Decimal(0)


def is_unclassified(r: DisposalRow) -> bool:
    """A capital-gains disposal whose term (short / long / exempt) is not known: no acquisition date, no cost, or no
    rule. Its tax cannot be computed, and it must never count as ₹0. Intraday trades are business income, not this."""
    return r.origin != "intraday" and (r.cls is None or r.cls.term == "unknown")


def unclassified(rows: Iterable[DisposalRow]) -> dict[str, Any]:
    """What could not be classified: {count, gain (the known gains among them), no_cost (count without a cost),
    no_date (count without an acquisition date), detail}. `count` 0 means complete."""
    bad = [r for r in rows if is_unclassified(r)]
    gain = sum((r.gain for r in bad if r.gain is not None), Decimal(0))
    no_cost = sum(1 for r in bad if r.cost is None)
    no_date = sum(1 for r in bad if r.acquired is None)
    detail = ""
    if bad:
        why = [f"{n} without {w}" for n, w in ((no_date, "an acquisition date"), (no_cost, "a cost")) if n]
        detail = (f"Tax incomplete: {len(bad)} disposal(s) / ₹{float(gain):,.0f} of gains can't be classified short- "
                  f"or long-term ({'; '.join(why) or 'no rule'}), so they are not in the tax. Enter the dates and "
                  "costs for a full figure")  # fmt: skip
        if no_cost:
            detail += f" (the gain of the {no_cost} without a cost is unknown too)"
    return {"count": len(bad), "gain": _f(gain), "no_cost": no_cost, "no_date": no_date, "detail": detail}


def rules_note(fys: Iterable[int]) -> dict[str, Any]:
    """{rules_verified, rules_note} for outputs that use the rules of the given financial years (#220)."""
    notes = [n for fy in sorted(set(fys)) if (n := unverified_note(fy)) is not None]
    return {"rules_verified": not notes, "rules_note": "; ".join(notes) or None}


def fy_summary(rows: Sequence[DisposalRow], fy: int, slab: Decimal) -> dict[str, Any]:
    ev = [r for r in rows if r.fy == fy]
    t = fy_tax(gains_of(ev), fy, slab)
    st = sum((r.gain for r in ev if r.gain is not None and r.cls and r.cls.term == "short"), Decimal(0))
    lt = sum((r.gain for r in ev if r.gain is not None and r.cls and r.cls.term == "long"), Decimal(0))
    eq_lt = sum(
        (r.gain for r in ev if r.gain is not None and r.cls and r.cls.bucket == "equity_lt"), Decimal(0)
    )
    unk = unclassified(ev)
    complete = unk["count"] == 0
    # an unclassified gain may be short or long, equity or not, a gain or a loss: the tax on the rest is neither a
    # lower nor an upper bound of the year's tax, so it is never shown as the year's figure (#213)
    return {"fy": fy, "label": t.label, "disposals": len(ev), "stcg": _f(st), "ltcg": _f(lt), "equity_ltcg": _f(eq_lt),
            "exempt": _f(t.exempt_total), "unknown": unk["count"], "complete": complete, "unclassified": unk,
            **rules_note([fy]),
            "intraday": sum(1 for r in ev if r.origin == "intraday"),
            "exemption": {"limit": _f(t.exemption_limit), "used": _f(t.exemption_used),
                          "remaining": _f(t.exemption_remaining), "complete": complete},
            "slices": [{"bucket": s.bucket, "rate_pct": None if s.rate is None else float(s.rate * 100),
                        "slab": s.rate is None, "long": s.long, "gain": _f(s.amount), "set_off": _f(s.set_off),
                        "exempted": _f(s.exempted), "taxable": _f(s.taxable)} for s in t.slices],
            "losses_carried": {"short": _f(t.losses_unabsorbed_short), "long": _f(t.losses_unabsorbed_long)},
            "tax": _f(t.tax) if complete else None, "cess": _f(t.cess) if complete else None,
            "total": _f(t.total) if complete else None, "total_classified": _f(t.total),
            "slab_rate_pct": float(slab * 100), "notes": t.notes}  # fmt: skip


def _f(x: Decimal | None) -> float | None:
    return None if x is None else round(float(x), 2)


# --------------------------------------------------------------------------- harvesting
@dataclass
class OpenLot:
    holding: HoldingTax
    acquired: date | None
    quantity: Decimal
    cost_per_unit: Decimal | None
    stt_paid: bool = True


def _round_trip_cost(tax_class: str, value: Decimal, asset_type: str) -> Decimal:
    if asset_type == "mf" and tax_class != "sgb":
        return value * (STAMP_MF_BUY + (Decimal("0.00001") if tax_class == "equity" else 0))
    return value * (STT_DELIVERY * 2 + STAMP_DELIVERY_BUY)


def harvest(rows: Sequence[DisposalRow], lots_by_holding: dict[int, list[OpenLot]], prices: dict[int, Decimal],
            asset_types: dict[int, str], today: date, slab: Decimal) -> dict[str, Any]:  # fmt: skip
    """Suggestions for the current financial year. Selling always takes a holding's oldest lots first (FIFO), so each
    suggestion sells a prefix of a holding's open lots, never a lot picked from the middle.

    - Gain harvesting: long-term equity gains up to the exemption still unused this year, booked tax-free and bought
      back at a higher cost (lower future tax).
    - Loss harvesting: when the year has taxable gains, sell loss-making lots to set the loss off; the saving is the
      change in this year's tax (fincalc.tax.tax_delta), not a guess."""
    fy = fiscal_year(today)
    base = [r for r in rows if r.fy == fy]
    base_gains = gains_of(base)
    now = fy_tax(base_gains, fy, slab)
    unk = unclassified(base)
    complete = unk["count"] == 0
    # with an unclassified disposal this year the unused exemption is not known (it may be equity LTCG, or a loss set
    # off first): "tax-free" gain harvesting could then be taxable, so none is suggested (#213)
    headroom = now.exemption_remaining if complete else Decimal(0)
    gain_ideas, loss_ideas = [], []
    for hid, lots in lots_by_holding.items():
        price = prices.get(hid)
        if price is None or not lots:
            continue
        h = lots[0].holding
        rows_for = []
        for lot in lots:
            if lot.cost_per_unit is None or lot.acquired is None:
                break  # FIFO would sell a lot whose cost is unknown first: stop here
            dr = evaluate(DisposalRow(h, lot.acquired, today, lot.quantity, lot.cost_per_unit * lot.quantity,
                                      price * lot.quantity, lot.stt_paid, "buy"))  # fmt: skip
            rows_for.append((lot, dr))
        if not rows_for:
            continue
        # gain harvesting: the longest prefix of long-term, in-profit equity lots, trimmed to the headroom
        if h.tax_class == "equity" and headroom > 0:
            qty, gain = Decimal(0), Decimal(0)
            for lot, dr in rows_for:
                if dr.cls is None or dr.cls.bucket != "equity_lt" or dr.gain is None or dr.gain <= 0:
                    break
                room = headroom - gain
                if room <= 0:
                    break
                per_unit = dr.gain / lot.quantity
                take = min(lot.quantity, (room / per_unit).to_integral_value(rounding="ROUND_FLOOR"))
                if take <= 0:
                    break
                qty += take
                gain += per_unit * take
                if take < lot.quantity:
                    break
            if qty > 0:
                value = qty * price
                gain_ideas.append({"holding_id": hid, "name": h.name, "account": h.account, "sell_units": _f(qty),
                                   "price": _f(price), "value": _f(value), "gain": _f(gain),
                                   "future_tax_saved_up_to": _f(gain * Decimal("0.125")),
                                   "est_costs": _f(_round_trip_cost(h.tax_class, value, asset_types.get(hid, "stock"))),
                                   "why": "Long-term gain inside this year's unused ₹1.25 lakh exemption: tax-free now; "
                                          "buying back raises your cost, so less LTCG later"})  # fmt: skip
        # loss harvesting: the prefix with the most negative total gain
        best, best_q, acc, acc_q, extra = None, Decimal(0), Decimal(0), Decimal(0), []
        chosen: list[DisposalRow] = []
        for _lot, dr in rows_for:
            if dr.gain is None:
                break
            acc += dr.gain
            acc_q += dr.quantity
            extra.append(dr)
            if acc < 0 and (best is None or acc < best):
                best, best_q, chosen = acc, acc_q, list(extra)
        if best is not None:
            delta = tax_delta(base_gains, gains_of(chosen), fy, slab)
            value = best_q * price
            if delta < 0:
                loss_ideas.append({"holding_id": hid, "name": h.name, "account": h.account, "sell_units": _f(best_q),
                                   "price": _f(price), "value": _f(value), "loss": _f(best),
                                   "short_term": any(r.cls and r.cls.term == "short" for r in chosen),
                                   "tax_saved": _f(-delta), "estimate": not complete,
                                   "est_costs": _f(_round_trip_cost(h.tax_class, value, asset_types.get(hid, "stock"))),
                                   "why": "Booking this loss lowers this year's tax on gains already realised"})  # fmt: skip
    gain_ideas.sort(key=lambda x: -(x["gain"] or 0))
    loss_ideas.sort(key=lambda x: -(x["tax_saved"] or 0))
    fy_end = date(fy, 3, 31)
    notes = list(HARVEST_NOTES)
    if not complete:
        notes.insert(0, unk["detail"] + ". Gain harvesting is not suggested (the unused exemption is unknown) and the "
                     "loss-harvesting savings are estimates that leave those disposals out.")  # fmt: skip
    rn = rules_note([fy])
    if rn["rules_note"]:
        notes.insert(0, rn["rules_note"])
    return {"fy": fy, "label": fy_label(fy), "days_left": (fy_end - today).days, "deadline": fy_end.isoformat(),
            "exemption_remaining": _f(headroom) if complete else None, "tax_so_far": _f(now.total) if complete else None,
            "complete": complete, "unclassified": unk, **rn, "gain_harvest": gain_ideas[:20],
            "loss_harvest": loss_ideas[:20], "notes": notes, "verify": VERIFY_NOTE}  # fmt: skip


# --------------------------------------------------------------------------- CSV for a CA
CSV_COLUMNS = ["financial_year", "holding", "isin", "account", "tax_class", "acquired", "sold", "quantity",
               "actual_cost", "fmv_31jan2018_per_unit", "cost_for_tax", "sale_value_net", "gain", "term", "rate",
               "rule", "stt_paid", "notes"]  # fmt: skip


def export_csv(rows: Sequence[DisposalRow], fy: int | None = None) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    from finresearch.signals.base import DISCLAIMER

    w.writerow(["# Personal estimate from FinResearch's dated rule table. " + VERIFY_NOTE])
    w.writerow(["# PERSONAL – NOT FOR DISTRIBUTION. " + DISCLAIMER])
    fys = sorted({r.fy for r in rows if fy is None or r.fy == fy})
    for y in fys:  # what the CA must know before using the figures (#213, #220)
        unk = unclassified(r for r in rows if r.fy == y)
        if unk["count"]:
            w.writerow([f"# {fy_label(y)}: " + unk["detail"]])
        if (stale := unverified_note(y)) is not None:
            w.writerow([f"# {stale}"])
    w.writerow(CSV_COLUMNS)
    for r in sorted(rows, key=lambda r: (r.sold, r.holding.name)):
        if fy is not None and r.fy != fy:
            continue
        c = r.cls
        rate = "" if c is None else ("slab" if c.rate is None and c.term == "short" else
                                     ("" if c.rate is None else f"{float(c.rate * 100):g}%"))  # fmt: skip
        w.writerow([fy_label(r.fy), r.holding.name, r.holding.isin or "", r.holding.account, r.holding.tax_class,
                    r.acquired.isoformat() if r.acquired else "", r.sold.isoformat(), _plain(r.quantity),
                    _plain(r.cost), _plain(r.holding.fmv_2018), _plain(r.tax_cost), _plain(r.proceeds),
                    _plain(r.gain), c.term if c else "", rate, c.rule.id if c and c.rule else "",
                    "yes" if r.stt_paid else "no", "; ".join(r.notes)])  # fmt: skip
    return out.getvalue()


def _plain(x: Decimal | None) -> str:
    return "" if x is None else format(x.quantize(Decimal("0.01")), "f")
