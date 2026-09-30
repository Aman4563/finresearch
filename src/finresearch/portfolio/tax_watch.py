"""Holding-period watch: open lots that turn long-term within the next N days, with the tax a sale would cost today
against the tax after the date (fincalc.tax, the same dated rule table as the tax view).

For each open lot with a known cost, date and a current price:
- lt_date = the first day on which a sale would be long-term (held *more than* the rule's months);
- tax_now = the change in this year's tax (incl. cess) if the lot were sold today (fincalc.tax.tax_delta over this
  year's realised gains, so set-off and the unused ₹1.25 lakh exemption are counted);
- tax_later = the same for a sale on lt_date at today's price (in that date's financial year; a year with no
  realised gains yet when it is the next one);
- saved = tax_now - tax_later. Only lots in profit are listed.

FIFO: Indian tax takes the oldest lot of the same holding (demat account / folio) first (CBDT Circular 768), so
selling this lot means selling the older open lots of the holding first; `older_lots` says how many. The price may
move before the date: the figures use today's price and are an estimate.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import tax_delta
from finresearch.portfolio.tax import DisposalRow, HoldingTax, evaluate, gains_of

WINDOW_DAYS = 30


def lt_date(h: HoldingTax, acquired: date, today: date, horizon: int = 800) -> date | None:
    """The first day after `today` on which a sale is long-term, or None (never long-term, e.g. a debt fund bought
    after 1-Apr-2023, or already long-term)."""
    from finresearch.fincalc.tax import classify

    def term(d: date) -> str:
        return classify(h.tax_class, acquired, d, listed=h.listed).term

    if term(today) != "short":
        return None
    for months in (12, 24, 36):  # the rule's threshold: only these exist in the table
        from finresearch.fincalc.tax import add_months

        d = add_months(acquired, months) + timedelta(days=1)
        if d > today and (d - today).days <= horizon and term(d) == "long":
            return d
    return None


def turning_long_term(lots: list[dict[str, Any]], base_rows: list[DisposalRow], today: date, slab: Decimal,
                      window_days: int = WINDOW_DAYS) -> list[dict[str, Any]]:  # fmt: skip
    """`lots`: dicts with holding (HoldingTax), acquired, quantity, cost_per_unit, stt_paid, price, older_lots.
    `base_rows`: realised disposals (for this year's set-off and exemption). Largest saving first."""
    fy = fiscal_year(today)
    base_now = gains_of([r for r in base_rows if r.fy == fy])
    out = []
    for lot in lots:
        h: HoldingTax = lot["holding"]
        acquired, qty, cpu, price = lot["acquired"], lot["quantity"], lot["cost_per_unit"], lot["price"]
        if acquired is None or cpu is None or price is None or qty <= 0:
            continue
        d = lt_date(h, acquired, today)
        if d is None or (d - today).days > window_days:
            continue

        stt = lot.get("stt_paid", True)
        now_row, later_row = (evaluate(DisposalRow(h, acquired, sold, qty, cpu * qty, price * qty, stt, "buy"))
                              for sold in (today, d))  # fmt: skip
        if now_row.gain is None or now_row.gain <= 0:
            continue
        tax_now = tax_delta(base_now, gains_of([now_row]), fy, slab)
        fy_later = fiscal_year(d)
        base_later = base_now if fy_later == fy else []
        tax_later = tax_delta(base_later, gains_of([later_row]), fy_later, slab)
        out.append({"holding_id": h.id, "name": h.name, "account": h.account, "acquired": acquired.isoformat(),
                    "quantity": float(qty), "lt_date": d.isoformat(), "days": (d - today).days,
                    "gain": float(now_row.gain.quantize(Decimal("0.01"))),
                    "tax_now": float(tax_now.quantize(Decimal("0.01"))),
                    "tax_later": float(tax_later.quantize(Decimal("0.01"))),
                    "saved": float((tax_now - tax_later).quantize(Decimal("0.01"))),
                    "older_lots": lot.get("older_lots", 0), "price": float(price)})  # fmt: skip
    return sorted(out, key=lambda x: (-x["saved"], x["days"]))


def open_lots_with_prices(data: Any, prices: dict[int, Decimal], categories: dict[int, str | None] | None = None
                          ) -> list[dict[str, Any]]:  # fmt: skip
    """Open lots of every holding (a report.Loaded) in FIFO order, with the holding's price and how many older open
    lots precede each one."""
    from finresearch.portfolio.report import holding_tax

    categories = categories or {}
    out = []
    for h in data.holdings:
        ht = holding_tax(h, categories.get(h.id))
        open_lots = [lot for lot in data.lots.get(h.id, []) if lot.open_quantity > Decimal("0.0005")]
        open_lots.sort(key=lambda lot: (lot.acquired or date.min, lot.id))
        for i, lot in enumerate(open_lots):
            out.append({"holding": ht, "acquired": lot.acquired, "quantity": lot.open_quantity,
                        "cost_per_unit": lot.cost_per_unit, "stt_paid": lot.stt_paid, "price": prices.get(h.id),
                        "older_lots": i})  # fmt: skip
    return out
