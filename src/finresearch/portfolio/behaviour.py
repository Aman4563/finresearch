"""The behaviour report: how the investor actually trades, computed from the app's own transactions (feature #5).

Personal data: computed on this machine from the local database and public price history; never sent to an LLM (no
agent, advisor prompt or MCP tool imports this module).

Sections, each with its method and source:

- **Disposition effect** (Odean 1998, "Are Investors Reluctant to Realize Their Losses?", J. Finance 53(5):1775-1798,
  doi:10.1111/0022-1082.00072, p. 1781-1783 and footnote 6). On each day a sale takes place in a portfolio of two or
  more stocks, each stock sold is a realised gain (net sale price above its average purchase price) or a realised
  loss (below); each stock held at the start of that day and not sold is a paper gain if both the day's high and low
  are above its average purchase price, a paper loss if both are below, and neither if the average purchase price
  lies between them. Days without a sale count nothing. Commissions are added to the purchase price and deducted
  from the sale price; for a potential (paper) sale the commission is the average commission per share paid on
  purchase. Prices are split-adjusted. Then

      PGR = RG / (RG + PG)        PLR = RL / (RL + PL)
      SE(PGR - PLR) = sqrt(PGR(1 - PGR) / (RG + PG) + PLR(1 - PLR) / (RL + PL))     t = (PGR - PLR) / SE

  The unit counted is a *stock on a sale day*, never a lot or a share. Departures from the paper, all stated in the
  payload: (a) the "portfolio" is all of the investor's accounts together and a stock held in two accounts is one
  stock (average purchase price over all its open units); (b) the app stores daily closes only, so unless a day's
  high and low are supplied the close stands in for both (`method`); (c) the headline counts stocks only, as the
  paper does; funds are reported separately as a labelled variant (NAV against average cost).
- **Turnover** (Barber & Odean 2000, "Trading Is Hazardous to Your Wealth", J. Finance 55(2):773-806, p. 781): monthly
  sales turnover = units of the beginning-of-month positions sold during month t × beginning-of-month price ÷
  beginning-of-month portfolio value; monthly purchase turnover = units bought in month t-1 still held at the start
  of month t × beginning-of-month price ÷ the same value; monthly turnover = the average of the two; annual = mean
  monthly × 12. "Beginning of month" = the last close on or before the month's first day. Every asset class counts
  (the paper counts common stocks only), so SIP instalments count as purchases.
- **Holding periods**: realised disposals (FIFO lots, net of charges), winners vs losers by unit-weighted median days
  held; open lots in profit vs in loss by age today. Intraday rows and rows with an unknown cost or date are left out.
- **Trading frequency vs returns**: per financial year, trades (one per instrument, side and day), turnover, the
  portfolio's time-weighted return over the part of the year inside the window, and NIFTYBEES's total return over
  the same days. One investor's years are a handful of points: a table, never a fitted relationship.
- **Cost of churn**: charges on every buy and sell in the window (brokerage, STT, stamp duty, ... as imported) plus
  the capital-gains tax (incl. cess) attributable to the window's disposals in each financial year
  (`fincalc.tax.tax_delta` against the year's other disposals), of which on short-term gains; and the short-term
  sales made within 60 days of turning long-term (`tax_watch.lt_date`).
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from finresearch.portfolio.lots import EPS, Event, build_lots

ZERO = Decimal(0)
PRIVACY = "Computed on this machine from your local database; never sent to an LLM."
ODEAN_SOURCE = ("Odean (1998), J. Finance 53(5):1775-1798, doi:10.1111/0022-1082.00072: PGR, PLR and the SE of "
                "their difference (p. 1782-1783, footnote 6)")  # fmt: skip
TURNOVER_SOURCE = (
    "Barber & Odean (2000), J. Finance 55(2):773-806, p. 781 (monthly sales and purchase turnover)"
)
NEAR_LT_DAYS = (
    60  # a short-term sale this close to the long-term date is flagged (feature research §5 item 5)
)
FORWARD_DAYS = 84  # Odean (1998) Table VI: returns over the next 84 trading days after a sale day
PRICE_STALE_DAYS = 10  # a beginning-of-month close older than this does not value the position


# --------------------------------------------------------------------------- disposition effect (Odean 1998)
@dataclass
class Held:
    """A stock held at the start of a sale day and not sold that day."""

    key: str
    name: str
    avg_cost: (
        Decimal | None
    )  # average purchase price per unit incl. buy commissions, split-adjusted; None = unknown
    commission: Decimal  # average buy commission per unit: the assumed commission of a potential sale
    high: Decimal | None  # the day's high (the close when only closes are known); None = no price that day
    low: Decimal | None


@dataclass
class Sold:
    """A stock sold on a sale day (all its sales that day together)."""

    key: str
    name: str
    avg_cost: Decimal | None  # at the start of the day
    net_price: Decimal | None  # Σ(gross - charges) / Σ units of the day's sales


@dataclass
class SaleDay:
    day: date
    n_stocks: int  # stocks in the portfolio at the start of the day (sold or not)
    held: list[Held] = field(default_factory=list)
    sold: list[Sold] = field(default_factory=list)


def paper_status(h: Held) -> str:
    """gain | loss | neither | unpriced | unknown_cost. Gain: high and low (each less the per-unit commission) both above
    the average purchase price; loss: both below; neither: the average lies between them (or equals one)."""
    if h.avg_cost is None:
        return "unknown_cost"
    if h.high is None or h.low is None:
        return "unpriced"
    hi, lo = h.high - h.commission, h.low - h.commission
    if hi > h.avg_cost and lo > h.avg_cost:
        return "gain"
    if hi < h.avg_cost and lo < h.avg_cost:
        return "loss"
    return "neither"


def realised_status(s: Sold) -> str:
    if s.avg_cost is None or s.net_price is None:
        return "unknown_cost"
    if s.net_price > s.avg_cost:
        return "gain"
    if s.net_price < s.avg_cost:
        return "loss"
    return "neither"


def _prop(a: int, b: int) -> float | None:
    return a / (a + b) if a + b else None


def disposition(days: Iterable[SaleDay]) -> dict[str, Any]:
    """Odean's counts and ratios over sale days. A day with fewer than two stocks in the portfolio is skipped."""
    rg = rl = pg = pl = 0
    counted, single, lines = 0, 0, []
    unpriced = unknown = 0
    for d in sorted(days, key=lambda x: x.day):
        if not d.sold:
            continue
        if d.n_stocks < 2:
            single += 1
            continue
        counted += 1
        line: dict[str, Any] = {"day": d.day.isoformat(), "realised_gains": [], "realised_losses": [],
                                "paper_gains": [], "paper_losses": [], "neither": [], "not_counted": []}  # fmt: skip
        for s in d.sold:
            st = realised_status(s)
            if st == "gain":
                rg += 1
                line["realised_gains"].append(s.name)
            elif st == "loss":
                rl += 1
                line["realised_losses"].append(s.name)
            elif st == "neither":
                line["neither"].append(s.name)
            else:
                unknown += 1
                line["not_counted"].append(f"{s.name} (cost unknown)")
        for h in d.held:
            st = paper_status(h)
            if st == "gain":
                pg += 1
                line["paper_gains"].append(h.name)
            elif st == "loss":
                pl += 1
                line["paper_losses"].append(h.name)
            elif st == "neither":
                line["neither"].append(h.name)
            elif st == "unpriced":
                unpriced += 1
                line["not_counted"].append(f"{h.name} (no price that day)")
            else:
                unknown += 1
                line["not_counted"].append(f"{h.name} (cost unknown)")
        lines.append(line)
    pgr, plr = _prop(rg, pg), _prop(rl, pl)
    diff = se = t = None
    if pgr is not None and plr is not None:
        diff = pgr - plr
        se = math.sqrt(pgr * (1 - pgr) / (rg + pg) + plr * (1 - plr) / (rl + pl))
        t = diff / se if se > 0 else None
    if pgr is None or plr is None:
        reading = "not enough sale days with both gains and losses to compare"
    elif t is None:
        reading = "no variation to test"
    elif diff > 0 and t >= 1.96:
        reading = "you realise gains more readily than losses (a disposition effect, |t| ≥ 1.96)"
    elif diff < 0 and t <= -1.96:
        reading = "you realise losses more readily than gains (|t| ≥ 1.96)"
    else:
        reading = "no clear difference: |t| < 1.96 (with few sale days this is expected, not reassuring)"
    return {"realised_gains": rg, "paper_gains": pg, "realised_losses": rl, "paper_losses": pl,
            "pgr": pgr, "plr": plr, "difference": diff, "se": se, "t": t,
            "ratio": (pgr / plr) if pgr is not None and plr else None,
            "sale_days": counted, "single_stock_days": single, "unpriced": unpriced, "unknown_cost": unknown,
            "reading": reading, "days": lines, "source": ODEAN_SOURCE}  # fmt: skip


# --------------------------------------------------------------------------- building sale days from transactions
@dataclass
class Group:
    """One instrument across every account that holds it (the unit Odean counts)."""

    key: str
    name: str
    asset_type: str
    events: list[list[Event]]  # one event list per holding (FIFO runs inside a holding)


def _start_of_day(events: Sequence[Event], day: date) -> list[Event]:
    """Events that happened before `day`, plus the day's own splits and bonuses (the day's prices are ex-date)."""
    return [e for e in events if e.day < day or (e.day == day and e.kind in ("split", "bonus"))]


def position_at(g: Group, day: date) -> tuple[Decimal, Decimal | None, Decimal]:
    """(units, average purchase price incl. buy commissions or None when any open lot's cost is unknown, average buy
    commission per unit) of `g` at the start of `day`, over every account."""
    units, cost, comm = ZERO, ZERO, ZERO
    known = True
    for evs in g.events:
        pre = _start_of_day(evs, day)
        with_c = build_lots(pre).open_lots
        bare = build_lots([Event(e.id, e.day, e.kind, e.quantity, e.price, e.amount, ZERO, e.stt_paid, e.meta)
                           for e in pre]).open_lots  # fmt: skip
        for lot, b in zip(with_c, bare, strict=False):
            units += lot.open_quantity
            if lot.cost_per_unit is None:
                known = False
                continue
            cost += lot.cost_per_unit * lot.open_quantity
            if b.cost_per_unit is not None:
                comm += (lot.cost_per_unit - b.cost_per_unit) * lot.open_quantity
    if units <= EPS:
        return ZERO, None, ZERO
    return units, (cost / units if known else None), comm / units


PriceOf = Callable[[str, date], tuple[Decimal, Decimal] | None]  # (group key, day) -> (high, low)


def sale_days(groups: Sequence[Group], start: date, end: date, price_of: PriceOf) -> list[SaleDay]:
    """Sale days in [start, end] for the given groups (e.g. stocks only). `price_of` gives the day's high and low (the
    close twice when only closes are known)."""
    sells: dict[date, dict[str, list[Event]]] = defaultdict(lambda: defaultdict(list))
    for g in groups:
        for evs in g.events:
            for e in evs:
                if e.kind == "sell" and start <= e.day <= end and e.quantity:
                    sells[e.day][g.key].append(e)
    out = []
    for day in sorted(sells):
        state = {g.key: position_at(g, day) for g in groups}
        held_keys = [g for g in groups if state[g.key][0] > EPS]
        sd = SaleDay(day, len(held_keys))
        for g in held_keys:
            _, avg, comm = state[g.key]
            if g.key in sells[day]:
                evs = sells[day][g.key]
                q = sum((e.quantity for e in evs if e.quantity), ZERO)
                gross = [e.gross() for e in evs]
                net = (
                    None
                    if any(x is None for x in gross) or q <= 0
                    else (
                        sum((x for x in gross if x is not None), ZERO) - sum((e.charges for e in evs), ZERO)
                    )
                    / q
                )
                sd.sold.append(Sold(g.key, g.name, avg, net))
            else:
                p = price_of(g.key, day)
                sd.held.append(Held(g.key, g.name, avg, comm, p[0] if p else None, p[1] if p else None))
        # a stock sold that day that was not held at its start (bought and sold the same day) is not counted
        out.append(sd)
    return out


# --------------------------------------------------------------------------- after the sale (Odean 1998, Table VI)
def forward_return(returns: dict[date, float], day: date, n: int = FORWARD_DAYS) -> float | None:
    """Compounded return over the next `n` return days after `day` (unit-adjusted daily returns), None when fewer than
    `n` days exist yet."""
    ks = [d for d in sorted(returns) if d > day][:n]
    if len(ks) < n:
        return None
    v = 1.0
    for d in ks:
        v *= 1 + returns[d]
    return v - 1


def after_sale(days: Sequence[SaleDay], returns: dict[str, dict[date, float]],
               bench: dict[date, float] | None) -> dict[str, Any]:  # fmt: skip
    """Mean excess return (over NIFTYBEES, when known) in the 84 trading days after each sale day: winners you sold vs
    losers you kept. Odean found the winners sold beat the losers kept by 3.4 pp over the next year (Table VI)."""
    sold_w, kept_l = [], []
    for d in days:
        if d.n_stocks < 2 or not d.sold:
            continue
        b = forward_return(bench, d.day) if bench else None
        for s in d.sold:
            if (
                realised_status(s) == "gain"
                and (r := forward_return(returns.get(s.key, {}), d.day)) is not None
            ):
                sold_w.append(r - (b or 0.0))
        for h in d.held:
            if paper_status(h) == "loss" and (r := forward_return(returns.get(h.key, {}), d.day)) is not None:
                kept_l.append(r - (b or 0.0))
    a = sum(sold_w) / len(sold_w) if sold_w else None
    c = sum(kept_l) / len(kept_l) if kept_l else None
    return {"horizon_trading_days": FORWARD_DAYS, "winners_sold": {"n": len(sold_w), "mean_excess": a},
            "losers_kept": {"n": len(kept_l), "mean_excess": c},
            "difference": (a - c) if a is not None and c is not None else None,
            "excess_over": "NIFTYBEES total return" if bench else "nothing (benchmark unavailable): raw returns",
            "source": "Odean (1998), Table VI (84 trading days)"}  # fmt: skip


# --------------------------------------------------------------------------- turnover (Barber & Odean 2000)
def month_starts(start: date, end: date) -> list[date]:
    out, d = [], date(start.year, start.month, 1)
    if d < start:
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    while d <= end:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def _prev_month(m: date) -> date:
    return date(m.year - (m.month == 1), (m.month - 2) % 12 + 1, 1)


def _units_before(g: Group, day: date) -> Decimal:
    return sum((build_lots([e for e in evs if e.day < day]).units for evs in g.events), ZERO)


def _traded(g: Group, kind: str, lo: date, hi: date) -> Decimal:
    return sum(
        (e.quantity or ZERO for evs in g.events for e in evs if e.kind == kind and lo <= e.day < hi), ZERO
    )


def turnover(groups: Sequence[Group], start: date, end: date,
             close_before: Callable[[str, date], Decimal | None]) -> dict[str, Any]:  # fmt: skip
    """Barber & Odean's monthly turnover for every whole month starting in [start, end]. `close_before(key, d)` is the
    last close strictly before day `d` (the beginning-of-month price), None when unknown or stale."""
    months = []
    for m in month_starts(start, end):
        nxt = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        if nxt - timedelta(days=1) > end:
            break  # a month not yet over is left out
        value, sold, bought, unpriced = ZERO, ZERO, ZERO, []
        for g in groups:
            u = _units_before(g, m)
            if u <= EPS:
                continue
            p = close_before(g.key, m)
            if p is None:
                unpriced.append(g.name)
                continue
            value += u * p
            sold += min(_traded(g, "sell", m, nxt), u) * p
            bought += min(_traded(g, "buy", _prev_month(m), m), u) * p
        if value <= 0:
            months.append({"month": m.isoformat()[:7], "value": None, "sales": None, "purchases": None,
                           "turnover": None, "unpriced": unpriced})  # fmt: skip
            continue
        s, b = float(sold / value), float(bought / value)
        months.append({"month": m.isoformat()[:7], "value": float(value), "sales": s, "purchases": b,
                       "turnover": (s + b) / 2, "unpriced": unpriced})  # fmt: skip
    vals = [x["turnover"] for x in months if x["turnover"] is not None]
    mean = sum(vals) / len(vals) if vals else None
    sales = [x["sales"] for x in months if x["sales"] is not None]
    return {"months": months, "mean_monthly": mean, "annual": mean * 12 if mean is not None else None,
            "annual_sales": (sum(sales) / len(sales)) * 12 if sales else None, "n_months": len(vals),
            "source": TURNOVER_SOURCE,
            "note": "Every asset class counts (the paper: common stocks), so SIP instalments count as purchases; "
                    "a round trip inside one month is not counted (only beginning-of-month positions are)."}  # fmt: skip


# --------------------------------------------------------------------------- holding periods
@dataclass
class Realised:
    name: str
    acquired: date | None
    sold: date
    quantity: Decimal
    cost: Decimal | None
    proceeds: Decimal
    origin: str


def weighted_median(pairs: Sequence[tuple[float, float]]) -> float | None:
    """The value at which half the total weight lies on each side (lower median on an exact tie)."""
    pairs = sorted((v, w) for v, w in pairs if w > 0)
    total = sum(w for _, w in pairs)
    if not total:
        return None
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def holding_periods(rows: Iterable[Realised], open_lots: Iterable[tuple[str, date | None, Decimal, Decimal | None,
                    Decimal | None]], today: date) -> dict[str, Any]:  # fmt: skip
    """Realised winners vs losers (days held, unit-weighted median) and today's open lots in profit vs in loss by age.
    `open_lots`: (name, acquired, units, cost per unit, price today)."""
    win, loss, skipped = [], [], 0
    for r in rows:
        if r.origin == "intraday" or r.cost is None or r.acquired is None:
            skipped += 1
            continue
        days = (r.sold - r.acquired).days
        g = r.proceeds - r.cost
        (win if g > 0 else loss if g < 0 else []).append((float(days), float(r.quantity)))
    o_win, o_loss, o_unknown = [], [], 0
    for _, acq, q, cpu, px in open_lots:
        if acq is None or cpu is None or px is None:
            o_unknown += 1
            continue
        age = float((today - acq).days)
        (o_win if px > cpu else o_loss if px < cpu else []).append((age, float(q)))

    def side(xs: list[tuple[float, float]]) -> dict[str, Any]:
        return {"n": len(xs), "median_days": weighted_median(xs),
                "mean_days": (sum(v * w for v, w in xs) / sum(w for _, w in xs)) if xs else None}  # fmt: skip

    w, lo = side(win), side(loss)
    note = None
    if w["median_days"] is not None and lo["median_days"] is not None:
        note = ("winners were sold sooner than losers" if w["median_days"] < lo["median_days"]
                else "losers were not held longer than winners")  # fmt: skip
    return {"realised": {"winners": w, "losers": lo, "skipped": skipped, "reading": note},
            "open": {"in_profit": side(o_win), "in_loss": side(o_loss), "unknown": o_unknown},
            "weighting": "unit-weighted (a lot of 100 units counts 100 times a lot of 1)"}  # fmt: skip


# --------------------------------------------------------------------------- trading frequency vs returns
def trades_of(groups: Sequence[Group], start: date, end: date) -> list[tuple[date, str, str]]:
    """(day, side, group key): one trade per instrument, side and day (partial fills are one decision)."""
    out = set()
    for g in groups:
        for evs in g.events:
            for e in evs:
                if e.kind in ("buy", "sell") and start <= e.day <= end and not e.meta.get("reinvest"):
                    out.add((e.day, e.kind, g.key))
    return sorted(out)


def period_return(days: Sequence[date], index: Sequence[float], lo: date, hi: date) -> float | None:
    """Return of a level series between the last day on or before `lo` and the last day on or before `hi`."""
    i = max((k for k, d in enumerate(days) if d <= lo), default=None)
    j = max((k for k, d in enumerate(days) if d <= hi), default=None)
    if i is None or j is None or j <= i or not index[i]:
        return None
    return index[j] / index[i] - 1


def frequency_vs_returns(trades: Sequence[tuple[date, str, str]], start: date, end: date,
                         days: Sequence[date], index: Sequence[float] | None,
                         bench: Sequence[float] | None, monthly_turnover: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:  # fmt: skip
    from finresearch.fincalc.dates import fiscal_year
    from finresearch.fincalc.tax import fy_label

    rows = []
    fy = fiscal_year(start)  # named by its end year, as the rest of the app does: 2027 = FY 2026-27
    while date(fy - 1, 4, 1) <= end:
        lo, hi = max(start, date(fy - 1, 4, 1)), min(end, date(fy, 3, 31))
        n = sum(1 for d, _, _ in trades if lo <= d <= hi)
        tv = [m["turnover"] for m in monthly_turnover if m["turnover"] is not None
              and lo <= date.fromisoformat(m["month"] + "-01") <= hi]  # fmt: skip
        r = period_return(days, index, lo - timedelta(days=1), hi) if index else None
        b = period_return(days, bench, lo - timedelta(days=1), hi) if bench else None
        rows.append({"fy": fy, "label": fy_label(fy), "from": lo.isoformat(),
                     "to": hi.isoformat(), "trades": n, "turnover_annual": (sum(tv) / len(tv) * 12) if tv else None,
                     "twr": r, "benchmark": b, "excess": (r - b) if r is not None and b is not None else None})  # fmt: skip
        fy += 1
    return rows


# --------------------------------------------------------------------------- cost of churn
def churn_cost(charges: Decimal, by_fy: dict[int, tuple[Decimal, Decimal]], near_lt: list[dict[str, Any]],
               avg_value: float | None, years: float,
               unclassified_by_fy: dict[int, int] | None = None) -> dict[str, Any]:  # fmt: skip
    """`by_fy`: fy -> (tax on the window's disposals, of which on short-term gains). `unclassified_by_fy`: fy -> how
    many of that year's capital-gains disposals could not be classified (portfolio.tax.unclassified). Such a year's
    tax leaves them out, so its change is an estimate, never the figure (#238): `complete` False, `estimate` True."""
    from finresearch.fincalc.tax import fy_label

    unk = {fy: n for fy, n in (unclassified_by_fy or {}).items() if n}
    tax = sum((t for t, _ in by_fy.values()), ZERO)
    st = sum((s for _, s in by_fy.values()), ZERO)
    total = charges + tax
    drag = float(total) / avg_value / years if avg_value and years > 0 else None
    note = None
    if unk:
        note = (f"Estimate: {sum(unk.values())} disposal(s) in {', '.join(fy_label(fy) for fy in sorted(unk))} have "
                "an unknown cost or purchase date and are left out of the tax, so the tax and total are incomplete")  # fmt: skip
    return {"charges": float(charges), "tax": float(tax), "tax_short_term": float(st), "total": float(total),
            "complete": not unk, "estimate": bool(unk), "incomplete_note": note,
            "by_fy": [{"fy": fy, "tax": float(t), "tax_short_term": float(s), "complete": fy not in unk,
                       "unclassified": unk.get(fy, 0)} for fy, (t, s) in sorted(by_fy.items())],
            "drag_pct_a_year": drag * 100 if drag is not None else None,
            "near_long_term": near_lt,
            "how": "charges on buys and sells in the window + the change in each year's capital-gains tax (incl. "
                   "cess) caused by the window's sales (fincalc.tax.tax_delta); drag = total ÷ average portfolio "
                   "value ÷ years"}  # fmt: skip


def median(xs: Sequence[float]) -> float | None:
    return statistics.median(xs) if xs else None


# --------------------------------------------------------------------------- the report from the database
def group_key(h: Any) -> str:
    """One key per instrument across accounts: the ISIN, else the exchange code or scheme code, else the holding key."""
    if h.isin:
        return f"ISIN:{h.isin.upper()}"
    if h.nse_symbol:
        return f"NSE:{h.nse_symbol.upper()}"
    if h.bse_code:
        return f"BSE:{h.bse_code}"
    if h.scheme_code:
        return f"MF:{h.scheme_code}"
    return h.ikey


def groups_of(data: Any) -> tuple[list[Group], dict[int, str]]:
    """`report.Loaded` -> groups (one per instrument) and holding id -> group key."""
    from finresearch.portfolio.service import events_of

    by: dict[str, Group] = {}
    hk: dict[int, str] = {}
    for h in data.holdings:
        k = group_key(h)
        hk[h.id] = k
        g = by.setdefault(k, Group(k, h.name, h.asset_type, []))
        g.events.append(events_of(data.txns.get(h.id, [])))
    return list(by.values()), hk


def build_report(data: Any, hist: Any, start: date, end: date, today: date, slab: Decimal) -> dict[str, Any]:
    """The whole report for [start, end]. `hist` is the portfolio's `history.History` (None or not ok: the sections
    that need prices say so and the rest is still computed)."""
    from finresearch.fincalc.tax import tax_delta
    from finresearch.portfolio.history import returns_of
    from finresearch.portfolio.report import disposal_rows
    from finresearch.portfolio.tax import gains_of, unclassified
    from finresearch.portfolio.tax_watch import lt_date

    groups, hk = groups_of(data)
    ok = hist is not None and getattr(hist, "ok", False)
    closes: dict[str, dict[date, float]] = {}
    rets: dict[str, dict[date, float]] = {}
    last_price: dict[str, Decimal] = {}
    if hist is not None:
        for p in hist.positions:
            for hid in p.holding_ids:
                k = hk.get(hid)
                if k and p.key in hist.closes:
                    closes[k] = hist.closes[p.key]
                    rets[k] = returns_of(hist.closes[p.key], hist.factors.get(p.key))
                if k and p.price is not None:
                    last_price[k] = Decimal(str(p.price))

    def price_of(key: str, d: date) -> tuple[Decimal, Decimal] | None:
        c = closes.get(key, {}).get(d)
        return None if c is None else (Decimal(str(c)), Decimal(str(c)))

    def close_before(key: str, d: date) -> Decimal | None:
        cs = closes.get(key) or {}
        prior = [x for x in cs if x < d]
        if not prior:
            return None
        last = max(prior)
        return Decimal(str(cs[last])) if (d - last).days <= PRICE_STALE_DAYS else None

    stocks = [g for g in groups if g.asset_type == "stock"]
    funds = [g for g in groups if g.asset_type == "mf"]
    days_s = sale_days(stocks, start, end, price_of)
    method = (
        "close-based: the app stores daily closes, so the close stands in for the day's high and low (Odean "
        "uses both; a stock whose average purchase price lies between them counts as neither)"
    )
    disp = {**disposition(days_s), "scope": "stocks", "method": method,
            "portfolio": "all your accounts together; a stock held in two accounts is one stock"}  # fmt: skip
    disp_f = {**disposition(sale_days(funds, start, end, price_of)), "scope": "mutual funds (variant: NAV vs average "
              "cost; Odean studied stocks only)", "method": "NAV of the day (funds have one price a day)"}  # fmt: skip
    bench_rets = None
    if ok and hist.benchmark is not None:
        lv = dict(zip(hist.days, hist.benchmark.level, strict=False))
        bench_rets = returns_of(lv)
    after = after_sale(days_s, rets, bench_rets) if ok else None

    turn = turnover(groups, start, end, close_before) if closes else {
        "months": [], "mean_monthly": None, "annual": None, "annual_sales": None, "n_months": 0,
        "source": TURNOVER_SOURCE, "note": "no price history: turnover needs beginning-of-month prices"}  # fmt: skip

    rows = disposal_rows(data)
    window = [r for r in rows if start <= r.sold <= end]
    real = [
        Realised(r.holding.name, r.acquired, r.sold, r.quantity, r.cost, r.proceeds, r.origin) for r in window
    ]
    open_lots = []
    for h in data.holdings:
        for lot in data.lots.get(h.id, []):
            if lot.open_quantity > EPS:
                open_lots.append((h.name, lot.acquired, lot.open_quantity, lot.cost_per_unit,
                                  last_price.get(hk[h.id])))  # fmt: skip
    periods = holding_periods(real, open_lots, today)

    trades = trades_of(groups, start, end)
    idx = list(hist.index) if ok else None
    blv = list(hist.benchmark.level) if ok and hist.benchmark is not None else None
    freq = frequency_vs_returns(trades, start, end, hist.days if ok else [], idx, blv, turn["months"])

    charges = sum((t.charges or ZERO for ts in data.txns.values() for t in ts
                   if t.kind in ("buy", "sell") and start <= t.day <= end), ZERO)  # fmt: skip
    by_fy: dict[int, tuple[Decimal, Decimal]] = {}
    unk_by_fy: dict[int, int] = {}
    for fy in sorted({r.fy for r in window}):
        unk_by_fy[fy] = unclassified(r for r in rows if r.fy == fy)["count"]
        w = [r for r in window if r.fy == fy]
        base = [r for r in rows if r.fy == fy and not (start <= r.sold <= end)]
        tax = tax_delta(gains_of(base), gains_of(w), fy, slab)
        w_st = [r for r in w if r.cls and r.cls.term == "short"]
        w_rest = [r for r in w if not (r.cls and r.cls.term == "short")]
        st = tax_delta(gains_of(base + w_rest), gains_of(w_st), fy, slab)
        by_fy[fy] = (tax, st)
    near = []
    for r in window:
        if r.cls is None or r.cls.term != "short" or r.acquired is None or r.gain is None or r.gain <= 0:
            continue
        d = lt_date(r.holding, r.acquired, r.sold)
        if d is not None and (d - r.sold).days <= NEAR_LT_DAYS:
            near.append({"name": r.holding.name, "sold": r.sold.isoformat(), "long_term_from": d.isoformat(),
                         "days_short": (d - r.sold).days, "gain": float(r.gain)})  # fmt: skip
    vals = [v for d, v in zip(hist.days, hist.value, strict=False) if start <= d <= end] if ok else []
    avg_value = sum(vals) / len(vals) if vals else None
    years = max((min(end, today) - start).days, 1) / 365.25
    churn = churn_cost(charges, by_fy, near, avg_value, years, unk_by_fy)

    warnings = []
    if not ok:
        warnings.append("no value history (" + (getattr(hist, "reason", None) or "not built") + "): turnover, "
                        "paper gains/losses, returns and the churn drag need prices")  # fmt: skip
    return {"from": start.isoformat(), "to": end.isoformat(), "trades": len(trades),
            "trades_by_side": {s: sum(1 for _, x, _ in trades if x == s) for s in ("buy", "sell")},
            "disposition": disp, "disposition_funds": disp_f, "after_sale": after, "turnover": turn,
            "holding_periods": periods, "frequency_vs_returns": freq, "churn": churn,
            "warnings": warnings, "privacy": PRIVACY,
            "disclaimer": "A description of your own past trades, not advice. Few trades make every figure noisy."}  # fmt: skip
