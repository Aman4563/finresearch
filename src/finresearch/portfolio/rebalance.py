"""Tax-aware rebalancing suggestions (feature #8): turn allocation drift into an illustrative list of steps.

Illustrative only, not investment advice: the app is not a SEBI-registered investment adviser or research analyst
(SEBI (Investment Advisers) Regulations 2013; roadmap D.8). Nothing is placed: the app has no order placement.

Classes and targets are the portfolio's own (portfolio.metrics.ASSET_CLASSES, the Targets panel on /portfolio); a
class without a target has a target of 0 %, as in metrics.drift.

1. Cash-flow rebalancing. New money (a SIP instalment, a lump sum) goes to the underweight classes first: it moves
   the mix toward the target with no sale, so no tax and no exit load. Vanguard: "Instead of buying or selling
   investments to rebalance, move dividends and interest to your portfolio's underweighted asset classes" [V]
   (https://investor.vanguard.com/investor-resources-education/portfolio-management/rebalancing-your-portfolio,
   read 5-Oct-2026); Jaconetti, Kinniry & Zilbering, "Best practices for portfolio rebalancing", Vanguard 2010 [U].
   The money fills the largest rupee shortfalls first (water-filling: each underweight class is brought up to the
   same remaining shortfall), and anything beyond every shortfall is split by the target weights.
2. Sells, only when a class is still outside its band after step 1. Band: |weight - target| > abs_pp OR
   > rel_pct % of the target, whichever is breached first (the "5/25" rule of thumb [W]; Vanguard's example
   rebalances at a 5-point drift [V]). Defaults 5 pp and 25 %, both settable; a class with a 0 % target uses the
   absolute band only. When any class is outside, every overweight class is sold back to its target.
   Sales are FIFO (CBDT Circular 768; portfolio.lots): a holding's oldest open lot is always sold first, so the
   plan never picks a lot. Among the holdings of a class, the next FIFO slice is chosen in this order:
   short-term loss, long-term loss, long-term equity gain within this year's unused s.112A / s.198 exemption
   (₹1.25 lakh, fincalc.tax.exemption_limit), other long-term gain, short-term gain; within a group, the lowest
   gain per rupee sold first. A holding whose oldest lot is a short-term gain therefore waits behind the others
   even if its newer lots are at a loss.
   Never sold: units still in an exit-load period or an ELSS 3-year lock (both are the newest lots, so what is left
   is still a FIFO prefix), lots with an unknown cost or date (the tax cannot be computed), holdings without a live
   price (an old statement NAV is not today's price), and Sovereign Gold Bonds (a market sale gives up the tax-free
   RBI redemption at maturity).
3. Buys: the sale proceeds net of charges go to the underweight classes in the same way as step 1. Class level only;
   the holdings you already own in the class are listed as examples, never as a recommendation.

Tax per step is the change in this financial year's tax incl. cess against the gains already realised this year
and the earlier steps of the plan (fincalc.tax.tax_delta), so the exemption is never counted twice.
Charges: STT 0.1 % on a delivery sale of shares, 0.001 % on the redemption of equity-oriented fund units, stamp
duty 0.015 % on share purchases and 0.005 % on fund purchases (portfolio.tax constants and their sources).
Brokerage, DP, exchange and SEBI charges and GST are not modelled.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_DOWN, Decimal
from typing import Any

from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import Gain, fy_label, fy_tax, tax_delta
from finresearch.portfolio.elss import unlock_date
from finresearch.portfolio.metrics import ASSET_CLASSES
from finresearch.portfolio.tax import (
    STAMP_DELIVERY_BUY,
    STAMP_MF_BUY,
    STT_DELIVERY,
    DisposalRow,
    HoldingTax,
    evaluate,
    gains_of,
)

ZERO = Decimal(0)
EPS = Decimal("0.0005")  # units below this are rounding noise (portfolio.lots.EPS)
STT_EQUITY_MF_SELL = Decimal("0.00001")  # 0.001 % on redeeming equity-oriented fund units (as portfolio.tax)
MF_UNIT_STEP = Decimal("0.001")  # fund units are allotted to 3 decimals
DEFAULT_ABS_PP = 5.0
DEFAULT_REL_PCT = 25.0
FUND_CLASSES = ("Equity funds", "Debt funds", "Gold & international funds")
DISCLAIMER = ("Illustrative, not investment advice. FinResearch is not a SEBI-registered investment adviser or "
              "research analyst; these steps show what your own targets imply under the stated assumptions. Check "
              "the tax with a CA before you act.")  # fmt: skip

CATEGORIES = {
    0: "Short-term loss",
    1: "Long-term loss",
    2: "Long-term gain within the exemption",
    3: "Long-term gain",
    4: "Short-term gain",
}


@dataclass
class Lot:
    acquired: date | None
    quantity: Decimal  # open units
    cost_per_unit: Decimal | None
    stt_paid: bool = True
    origin: str = "buy"


@dataclass
class Position:
    """One holding as the planner sees it. `lots` are the open lots, oldest first (FIFO order)."""

    holding: HoldingTax
    asset_type: str  # stock | mf | ...
    asset_class: str  # a portfolio.metrics.ASSET_CLASSES label
    value: Decimal | None  # today's value (None = unpriced: not in the allocation)
    price: Decimal | None  # a live price, else None (not sellable)
    lots: list[Lot] = field(default_factory=list)
    elss: bool = False
    exit_load: tuple[Decimal, int] | None = None  # (fraction, days) as the user entered it
    price_note: str = ""


# --------------------------------------------------------------------------- bands and cash flow
def band_pp(target_pct: float, abs_pp: float = DEFAULT_ABS_PP, rel_pct: float = DEFAULT_REL_PCT) -> float:
    """The band half-width in pp: the tighter of the absolute and relative bands (0 % target: absolute only)."""
    if target_pct <= 0 or rel_pct <= 0:
        return abs_pp
    return min(abs_pp, rel_pct / 100 * target_pct)


def weights(values: Mapping[str, Decimal]) -> dict[str, float]:
    total = sum(values.values(), ZERO)
    return {k: (float(v / total * 100) if total > 0 else 0.0) for k, v in values.items()}


def outside(
    values: Mapping[str, Decimal], targets: Mapping[str, float], abs_pp: float, rel_pct: float
) -> list[str]:
    w = weights(values)
    return [
        k for k in values if abs(w[k] - targets.get(k, 0.0)) > band_pp(targets.get(k, 0.0), abs_pp, rel_pct)
    ]


def fill(values: Mapping[str, Decimal], targets: Mapping[str, float], cash: Decimal) -> dict[str, Decimal]:
    """Split `cash` across classes: the rupee shortfalls against the targets at the new total are filled largest
    first (every underweight class is brought down to the same remaining shortfall L); cash beyond all shortfalls is
    split by the target weights. Returns class -> amount (only positive amounts)."""
    if cash <= 0:
        return {}
    total = sum(values.values(), ZERO) + cash
    short = {k: Decimal(str(t)) / 100 * total - values.get(k, ZERO) for k, t in targets.items()}
    short = {k: d for k, d in short.items() if d > 0}
    need = sum(short.values(), ZERO)
    if need <= cash:
        out = dict(short)
        rest = cash - need
        tsum = sum((Decimal(str(t)) for t in targets.values()), ZERO)
        if rest > 0 and tsum > 0:
            for k, t in targets.items():
                out[k] = out.get(k, ZERO) + rest * Decimal(str(t)) / tsum
        return {k: v for k, v in out.items() if v > 0}
    # water level L with sum(max(0, d - L)) = cash: walk the shortfalls from the largest down
    ds = sorted(short.values(), reverse=True)
    level, acc = ZERO, ZERO
    for i, d in enumerate(ds):
        acc += d
        nxt = ds[i + 1] if i + 1 < len(ds) else ZERO
        # with the i+1 largest classes above the level: sum = acc - (i+1)·L
        cand = (acc - cash) / (i + 1)
        if cand >= nxt:
            level = cand
            break
    return {k: d - level for k, d in short.items() if d - level > 0}


# --------------------------------------------------------------------------- what can be sold
@dataclass
class Sellable:
    lots: list[Lot]  # the FIFO prefix that may be sold
    blocked_units: Decimal
    reason: str  # why the rest cannot be sold ("" when everything can)


def sellable(p: Position, today: date) -> Sellable:
    """The open lots that can be sold, oldest first, stopping at the first lot that cannot (FIFO: a sale of more
    units would have to take that lot)."""
    out: list[Lot] = []
    for i, lot in enumerate(p.lots):
        why = ""
        if lot.acquired is None or lot.cost_per_unit is None:
            why = "a lot with an unknown cost or date (enter it to compute the tax)"
        elif p.elss and today < unlock_date(lot.acquired):
            why = f"ELSS units within the 3-year lock-in (the next unlock is {unlock_date(lot.acquired)})"
        elif p.exit_load is not None and p.exit_load[0] > 0 and (today - lot.acquired).days < p.exit_load[1]:
            why = (f"units within the {p.exit_load[1]}-day exit-load period ({float(p.exit_load[0] * 100):g} % load, "
                   f"as you entered it)")  # fmt: skip
        if why:
            return Sellable(out, sum((x.quantity for x in p.lots[i:]), ZERO), why)
        out.append(lot)
    return Sellable(out, ZERO, "")


def sell_charges(p: Position, gross: Decimal) -> Decimal:
    if p.asset_type == "stock" and p.holding.tax_class == "equity":
        return (
            gross * STT_DELIVERY
        )  # an equity ETF bought as a "stock" pays 0.001 %, overstated here (stated)
    if p.asset_type == "mf" and p.holding.tax_class == "equity":
        return gross * STT_EQUITY_MF_SELL
    return ZERO


def buy_stamp(asset_class: str, amount: Decimal) -> Decimal:
    return amount * (STAMP_MF_BUY if asset_class in FUND_CLASSES else STAMP_DELIVERY_BUY)


# --------------------------------------------------------------------------- the sell list
@dataclass
class Piece:
    pos: Position
    lot: Lot
    units: Decimal
    category: int


def _step(p: Position) -> Decimal:
    return MF_UNIT_STEP if p.asset_type == "mf" else Decimal(1)


def _round(x: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    return (x / step).to_integral_value(rounding=rounding) * step


def _row(p: Position, lot: Lot, units: Decimal, sold: date, proceeds: Decimal) -> DisposalRow:
    cost = lot.cost_per_unit * units if lot.cost_per_unit is not None else None
    return evaluate(
        DisposalRow(p.holding, lot.acquired, sold, units, cost, proceeds, lot.stt_paid, lot.origin)
    )


def _head(p: Position, lot: Lot, left: Decimal, today: date, gains: list[Gain], fy: int,
          slab: Decimal) -> tuple[int, Decimal, Decimal]:  # fmt: skip
    """(category, gain per rupee, the most units this category allows) for the next FIFO slice of a holding."""
    assert p.price is not None
    r = _row(p, lot, left, today, p.price * left)
    g = r.gain or ZERO
    per_unit = g / left
    per_rupee = per_unit / p.price if p.price > 0 else ZERO
    term = r.cls.term if r.cls else "unknown"
    if g < 0 or (g == 0 and term != "long"):
        return (0 if term == "short" else 1), per_rupee, left
    if g == 0:
        return 1, per_rupee, left
    if term == "long" and r.cls is not None and r.cls.bucket == "equity_lt":
        room = fy_tax(gains, fy, slab).exemption_remaining
        if room > 0:
            fit = min(left, _round(room / per_unit, _step(p)))
            if fit > 0:
                return 2, per_rupee, fit
    return (3 if term == "long" else 4), per_rupee, left


def pick(positions: Sequence[Position], amount: Decimal, today: date, base: list[Gain], fy: int, slab: Decimal,
         state: dict[int, tuple[int, Decimal]], prefix: dict[int, list[Lot]]) -> tuple[list[Piece], Decimal]:  # fmt: skip
    """Sell about `amount` (gross ₹) from `positions`, one FIFO slice at a time in the tax-aware order. `state` holds
    each holding's place in its sellable prefix (lot index, units left in that lot) and is updated. Returns the
    pieces and the amount that could not be sold."""
    pieces: list[Piece] = []
    gains = list(base)
    while amount > 0:
        best: tuple[tuple[int, Decimal], Position, Lot, Decimal] | None = None
        for p in positions:
            hid = p.holding.id
            lots = prefix.get(hid) or []
            i, left = state.get(hid, (0, lots[0].quantity if lots else ZERO))
            if p.price is None or p.price <= 0 or i >= len(lots) or left <= EPS:
                continue
            cat, per_rupee, most = _head(p, lots[i], left, today, gains, fy, slab)
            key = (cat, per_rupee)
            if best is None or key < best[0]:
                best = (key, p, lots[i], most)
        if best is None:
            break
        (cat, _), p, lot, most = best
        assert p.price is not None
        want = _round(amount / p.price, _step(p), "ROUND_HALF_UP")
        take = min(most, want)
        if take <= 0:
            break  # less than half a unit left to sell
        pieces.append(Piece(p, lot, take, cat))
        gains += gains_of([_row(p, lot, take, today, p.price * take)])
        amount -= take * p.price
        hid = p.holding.id
        lots = prefix[hid]
        i, left = state.get(hid, (0, lots[0].quantity))
        left -= take
        state[hid] = (
            (i + 1, lots[i + 1].quantity if i + 1 < len(lots) else ZERO) if left <= EPS else (i, left)
        )
    return pieces, max(ZERO, amount)


# --------------------------------------------------------------------------- the plan
def _f(x: Decimal | float | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _examples(positions: Sequence[Position], cls: str) -> list[str]:
    names = sorted({p.holding.name for p in positions if p.asset_class == cls and (p.value or ZERO) > 0})
    return names[:5]


def _steps(pieces: list[Piece], today: date, base: list[Gain], fy: int,
           slab: Decimal) -> tuple[list[dict[str, Any]], list[Gain]]:  # fmt: skip
    """Merge consecutive pieces of one holding and category into a step; tax each step against everything before."""
    groups: list[list[Piece]] = []
    for pc in pieces:
        if groups and groups[-1][0].pos is pc.pos and groups[-1][0].category == pc.category:
            groups[-1].append(pc)
        else:
            groups.append([pc])
    out, prior = [], list(base)
    for n, g in enumerate(groups, 1):
        p = g[0].pos
        assert p.price is not None
        units = sum((x.units for x in g), ZERO)
        gross = units * p.price
        charges = sell_charges(p, gross)
        rows = [_row(p, x.lot, x.units, today, (x.units * p.price) - charges * x.units / units) for x in g]
        gs = gains_of(rows)
        tax = tax_delta(prior, gs, fy, slab)
        prior += gs
        gain = sum((r.gain or ZERO for r in rows), ZERO)
        st = sum((r.gain or ZERO for r in rows if r.cls and r.cls.term == "short"), ZERO)
        out.append({
            "step": n, "holding_id": p.holding.id, "name": p.holding.name, "account": p.holding.account,
            "asset_class": p.asset_class, "category": CATEGORIES[g[0].category], "units": _f(units, 4),
            "price": _f(p.price, 4), "gross": _f(gross), "charges": _f(charges), "exit_load": 0.0,
            "net": _f(gross - charges), "gain": _f(gain), "short_term": _f(st), "long_term": _f(gain - st),
            "tax": _f(tax),
            "notes": (["no exit load entered for this fund: assumed none (check the scheme document)"]
                      if p.asset_type == "mf" and p.exit_load is None else []),
            "lots": [{"acquired": r.acquired.isoformat() if r.acquired else None, "units": _f(r.quantity, 4),
                      "gain": _f(r.gain), "term": r.cls.term if r.cls else None,
                      "bucket": r.cls.bucket if r.cls else None} for r in rows],
        })  # fmt: skip
    return out, prior[len(base) :]


def plan(positions: Sequence[Position], targets: Mapping[str, float], today: date, realised: Sequence[DisposalRow],
         slab: Decimal, *, new_money: Decimal = ZERO, abs_pp: float = DEFAULT_ABS_PP,
         rel_pct: float = DEFAULT_REL_PCT) -> dict[str, Any]:  # fmt: skip
    """The rebalancing plan. `realised`: the disposals already booked (any year; this FY's are used)."""
    fy = fiscal_year(today)
    base = gains_of([r for r in realised if r.fy == fy])
    head = {"as_of": today.isoformat(), "fy": fy, "fy_label": fy_label(fy), "disclaimer": DISCLAIMER,
            "bands": {"abs_pp": abs_pp, "rel_pct": rel_pct}, "new_money": _f(new_money), "targets": dict(targets),
            "assumptions": ASSUMPTIONS, "sources": SOURCES}  # fmt: skip
    classes = [
        k for k in ASSET_CLASSES if k in targets or any(p.asset_class == k and p.value for p in positions)
    ]
    v0 = {k: sum((p.value or ZERO for p in positions if p.asset_class == k), ZERO) for k in classes}
    unpriced = [p.holding.name for p in positions if p.value is None and p.lots]
    head["unpriced"] = unpriced
    if not targets:
        return {**head, "status": "no_targets", "message": "Set a target allocation first (Targets, above)."}
    if sum(v0.values(), ZERO) + new_money <= 0:
        return {**head, "status": "no_value", "message": "Nothing is valued yet: no holding has a price."}
    # step 1: new money to the underweight classes
    cash = fill(v0, targets, new_money)
    cash_rows = [{"asset_class": k, "amount": _f(a), "stamp": _f(buy_stamp(k, a)), "examples": _examples(positions, k)}
                 for k, a in sorted(cash.items(), key=lambda kv: -kv[1])]  # fmt: skip
    v1 = {k: v0[k] + cash.get(k, ZERO) - buy_stamp(k, cash.get(k, ZERO)) for k in classes}
    hit = outside(v1, targets, abs_pp, rel_pct)
    # step 2: sells, only when a band is still breached
    skipped: list[dict[str, Any]] = []
    prefix: dict[int, list[Lot]] = {}
    for p in positions:
        if not p.lots:
            continue
        why, units = "", sum((x.quantity for x in p.lots), ZERO)
        if p.asset_class == "Sovereign Gold Bonds" or p.holding.tax_class == "sgb":
            why = "Sovereign Gold Bond: a market sale gives up the tax-free redemption at maturity (sell it yourself if you mean to)"
        elif p.price is None:
            why = p.price_note or "no live price today"
        else:
            sl = sellable(p, today)
            prefix[p.holding.id] = sl.lots
            if sl.blocked_units > EPS:
                why, units = sl.reason, sl.blocked_units
        if why:
            skipped.append({"holding_id": p.holding.id, "name": p.holding.name, "asset_class": p.asset_class,
                            "units": _f(units, 4), "reason": why})  # fmt: skip
    pieces: list[Piece] = []
    unfilled: list[dict[str, Any]] = []
    sold: dict[str, Decimal] = {}
    if hit:
        total1 = sum(v1.values(), ZERO)
        over = {k: v1[k] - Decimal(str(targets.get(k, 0.0))) / 100 * total1 for k in classes}
        state: dict[int, tuple[int, Decimal]] = {}
        for k, amt in sorted(over.items(), key=lambda kv: -kv[1]):
            if amt <= 0:
                continue
            got, left = pick([p for p in positions if p.asset_class == k and p.holding.id in prefix], amt, today,
                             base + gains_of(_rows_of(pieces, today)), fy, slab, state, prefix)  # fmt: skip
            pieces += got
            sold[k] = sum((x.units * (x.pos.price or ZERO) for x in got), ZERO)
            if left > 0 and left >= Decimal(1):
                why = sorted({s["reason"] for s in skipped if s["asset_class"] == k}) or [
                    "no units left to sell"
                ]
                unfilled.append({"asset_class": k, "amount": _f(left), "why": why})
    steps, plan_gains = _steps(pieces, today, base, fy, slab)
    return _finish(head, positions, classes, targets, v0, v1, cash_rows, hit, steps, sold, skipped, unfilled, base,
                   plan_gains, fy, slab, abs_pp, rel_pct)  # fmt: skip


def _rows_of(pieces: list[Piece], today: date) -> list[DisposalRow]:
    return [_row(x.pos, x.lot, x.units, today, x.units * (x.pos.price or ZERO)) for x in pieces]


def _finish(head: dict[str, Any], positions: Sequence[Position], classes: list[str], targets: Mapping[str, float],
            v0: dict[str, Decimal], v1: dict[str, Decimal], cash_rows: list[dict[str, Any]], hit: list[str],
            steps: list[dict[str, Any]], sold: dict[str, Decimal], skipped: list[dict[str, Any]],
            unfilled: list[dict[str, Any]], base: list[Gain], plan_gains: list[Gain], fy: int, slab: Decimal,
            abs_pp: float, rel_pct: float) -> dict[str, Any]:  # fmt: skip
    # step 3: the net proceeds to the underweight classes
    net = sum((Decimal(str(s["net"])) for s in steps), ZERO)
    v2 = {k: v1[k] - sold.get(k, ZERO) for k in classes}
    buys = fill(v2, targets, net)
    buy_rows = [{"asset_class": k, "amount": _f(a), "stamp": _f(buy_stamp(k, a)), "examples": _examples(positions, k)}
                for k, a in sorted(buys.items(), key=lambda kv: -kv[1])]  # fmt: skip
    v3 = {k: v2[k] + buys.get(k, ZERO) - buy_stamp(k, buys.get(k, ZERO)) for k in classes}
    w0, w1, w3 = weights(v0), weights(v1), weights(v3)
    alloc = []
    for k in classes:
        t = float(targets.get(k, 0.0))
        b = band_pp(t, abs_pp, rel_pct)
        alloc.append({"label": k, "target_pct": t, "band_pp": round(b, 2),
                      "before_pct": round(w0.get(k, 0.0), 2), "after_cash_pct": round(w1.get(k, 0.0), 2),
                      "after_pct": round(w3.get(k, 0.0), 2), "before_value": _f(v0[k]), "after_value": _f(v3[k]),
                      "outside_before": abs(w0.get(k, 0.0) - t) > b, "outside_after_cash": k in hit,
                      "outside_after": abs(w3.get(k, 0.0) - t) > b})  # fmt: skip
    before, after = fy_tax(base, fy, slab), fy_tax(base + plan_gains, fy, slab)
    stamp = sum((Decimal(str(r["stamp"])) for r in cash_rows + buy_rows), ZERO)
    totals = {"sold_gross": _f(sum((Decimal(str(s["gross"])) for s in steps), ZERO)),
              "sell_charges": _f(sum((Decimal(str(s["charges"])) for s in steps), ZERO)), "stamp_duty": _f(stamp),
              "exit_load": 0.0, "gain": _f(sum((Decimal(str(s["gain"])) for s in steps), ZERO)),
              "tax": _f(after.total - before.total), "bought": _f(sum(buys.values(), ZERO)),
              "new_money": head["new_money"], "exemption_before": _f(before.exemption_remaining),
              "exemption_after": _f(after.exemption_remaining), "tax_so_far": _f(before.total)}  # fmt: skip
    if steps or unfilled:
        status = "rebalance"
    elif cash_rows:
        status = "cash_only"
    else:
        status = "within_bands"
    msg = {"within_bands": "Every class is inside its band: nothing to sell.",
           "cash_only": "The new money alone keeps every class inside its band: no sale needed.",
           "rebalance": "A class is outside its band after the new money: the steps below sell back to target."}  # fmt: skip
    return {**head, "status": status, "message": msg[status], "outside": hit, "allocation": alloc,
            "cash_flow": cash_rows, "sells": steps, "buys": buy_rows, "unfilled": unfilled, "skipped": skipped,
            "totals": totals}  # fmt: skip


ASSUMPTIONS = [
    "Targets are yours (Targets panel); a class without a target counts as 0 %.",
    "Bands: a class is outside when |weight - target| exceeds the absolute band (default 5 pp) or the relative band "
    "(default 25 % of the target), whichever is tighter; a 0 % target uses the absolute band only.",
    "New money goes to the underweight classes first, largest rupee shortfall first; beyond every shortfall it is "
    "split by the target weights.",
    "Sales happen only when a class is still outside its band after the new money, and then every overweight class "
    "is sold back to its target (not just to the band edge).",
    "Sales are first-in-first-out (CBDT Circular 768): the oldest open lot of a holding is always sold first.",
    "Order between holdings: short-term losses, long-term losses, long-term equity gains within this year's unused "
    "₹1.25 lakh exemption, other long-term gains, short-term gains last; the lowest gain per rupee first in a group.",
    "Not sold: units within an exit-load period (from the loads you entered under Costs; a fund without one is "
    "assumed to have none), ELSS units within 3 years of allotment, lots with an unknown cost or date, holdings "
    "without a live price, and Sovereign Gold Bonds.",
    "Tax: this financial year's tax incl. 4 % cess at your slab, against the gains already realised this year "
    "(set-off and the exemption applied); surcharge, carried-forward losses and the s.87A rebate are not modelled.",
    "Tax is paid later from your own money; the sale proceeds net of STT are reinvested.",
    "Charges: STT 0.1 % on share sales, 0.001 % on equity fund redemptions, stamp duty 0.015 % on share buys and "
    "0.005 % on fund buys. Brokerage, DP, exchange and SEBI charges and GST are not included.",
    "Prices: today's live prices; whole shares for stocks, fund units to 3 decimals. Prices move before you act.",
    "Buys are by class, not by security: the holdings named are ones you already own in that class, not picks.",
]
SOURCES = [
    "Vanguard, Rebalancing your portfolio [V]: https://investor.vanguard.com/investor-resources-education/"
    "portfolio-management/rebalancing-your-portfolio (read 5-Oct-2026)",
    "Jaconetti, Kinniry & Zilbering, Best practices for portfolio rebalancing, Vanguard 2010 [U]",
    "CBDT Circular 768 (FIFO for securities in demat form)",
    "Finance (No.2) Act 2024: s.111A 20 %, s.112A 12.5 % above ₹1.25 lakh (fincalc.tax rules table)",
    "SEBI (Investment Advisers) Regulations 2013 / (Research Analysts) Regulations 2014: this is not advice",
]


# --------------------------------------------------------------------------- from the database
def positions_from_db(s: Any, prices: Mapping[int, Any], today: date) -> list[Position]:
    """Positions from the stored holdings and lots with the prices of valuation.fetch_prices. The value counts any
    price (as the allocation on /portfolio does); a sale needs a live one ("statement" prices are not today's)."""
    from finresearch.api.portfolio_analytics import get_settings_row
    from finresearch.portfolio import elss
    from finresearch.portfolio.report import holding_tax, load
    from finresearch.portfolio.valuation import asset_label

    data = load(s)
    loads = get_settings_row(s)["exit_loads"]
    out = []
    for h in data.holdings:
        lots = [x for x in data.lots.get(h.id, []) if x.open_quantity > EPS]
        if not lots:
            continue
        lots.sort(key=lambda x: (x.acquired or date.min, x.id))  # FIFO, as pretrade.sell_items
        pi = prices.get(h.id)
        category = getattr(pi, "category", None) or h.category
        ht = holding_tax(h, category)
        units = sum((x.open_quantity for x in lots), ZERO)
        price = getattr(pi, "price", None)
        live = price is not None and "statement" not in (getattr(pi, "source", None) or "")
        note = "" if live else ("only an old statement price, not today's" if price is not None else
                                (getattr(pi, "error", None) or "no price"))  # fmt: skip
        el = loads.get(str(h.id)) if h.asset_type == "mf" else None
        out.append(Position(
            holding=ht, asset_type=h.asset_type, asset_class=asset_label(h.asset_type, ht.tax_class),
            value=price * units if price is not None else None, price=price if live else None,
            lots=[Lot(x.acquired, x.open_quantity, x.cost_per_unit, x.stt_paid, x.origin) for x in lots],
            elss=elss.detect(h.asset_type, h.name, category) is not None,
            exit_load=(Decimal(str(el["pct"])) / 100, int(el.get("days") or 0)) if el else None, price_note=note,
        ))  # fmt: skip
    return out


def rebalance_view(s: Any, prices: Mapping[int, Any], today: date, slab: Decimal, *, new_money: Decimal = ZERO,
                   abs_pp: float = DEFAULT_ABS_PP, rel_pct: float = DEFAULT_REL_PCT) -> dict[str, Any]:  # fmt: skip
    from finresearch.portfolio.metrics import get_targets
    from finresearch.portfolio.report import disposal_rows, load

    cats = {hid: getattr(p, "category", None) for hid, p in prices.items()}
    realised = [r for r in disposal_rows(load(s), cats) if r.fy == fiscal_year(today)]
    out = plan(positions_from_db(s, prices, today), get_targets(s), today, realised, slab, new_money=new_money,
               abs_pp=abs_pp, rel_pct=rel_pct)  # fmt: skip
    out["slab_pct"] = float(slab * 100)
    return out
