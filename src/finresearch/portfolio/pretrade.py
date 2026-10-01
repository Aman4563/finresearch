"""The pre-trade checklist (feature #5): what a planned buy or sell does to the portfolio, shown before it is recorded.

Every item is `{key, label, status, value, detail, source}` with status ok | warn | block | info | unknown. "block"
means the trade as entered cannot be done (selling more units than are held, ELSS units still locked); nothing is
ever placed or stopped by the app: it has no order placement.

Data: the local database and what the monitor's daily pass left in `portfolio_setting` (cache:valuation,
cache:signals). The signal of an instrument not held is read live from the app's own signal provider (public market
data) only when `live_signal` is passed. Personal data: never sent to an LLM.

Items:
- position size: the trade's value against the portfolio's value (the latest daily valuation);
- concentration after the trade: the instrument's and its sector's weight after, against the profile's single-stock
  limit (else the risk-appetite default the stock signal uses, signals.stock.CAPS) and the 25 % sector rule of thumb
  (portfolio.analytics.SECTOR_LIMIT_PCT); HHI and the effective number of holdings before and after (a buy is
  assumed to bring new money; a sale's proceeds leave the portfolio);
- tax (sales): the open lots the sale takes first-in-first-out (CBDT Circular 768), each taxed with the dated rules
  (portfolio.tax.evaluate), the change in this financial year's tax incl. cess against the year's realised gains
  (fincalc.tax.tax_delta), and lots that turn long-term within 60 days (portfolio.tax_watch.lt_date) with the tax
  saved by waiting at today's price;
- exit load (fund sales): the load the user entered for the scheme (the app has no exit-load data);
- ELSS lock-in (fund sales): units of an ELSS are locked for 3 years from each purchase (Equity Linked Savings
  Scheme, 2005, para 5, Ministry of Finance notification; also each SIP instalment separately). The scheme is
  recognised by "ELSS" / "tax saver" in its name or category [unverified against AMFI's category];
- the signal and its uncertainty: action, probability with its interval and how the method was validated (n);
- surveillance flags (ASM/GSM, pledge): `RED_FLAGS`, a seam until the surveillance feed exists;
- your thesis: the open journal entries for the instrument, with the exit condition you wrote;
- recent activity: decisions in the last 30 days (Barber & Odean 2000: the most active traders earned least).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioHolding, PortfolioTxn, TradeNote

ZERO = Decimal(0)
NEAR_LT_DAYS = 60
ELSS_LOCK_YEARS = 3
ACTIVITY_DAYS = 30
# (instrument key, asset type) -> list of {"label", "detail", "level"} or None when the feed is not available
RED_FLAGS: Callable[[str, str], list[dict[str, Any]] | None] | None = None
SIGNAL_LIVE_TIMEOUT_S = 20.0


@dataclass
class Plan:
    side: str  # buy | sell
    quantity: Decimal
    price: Decimal
    day: date
    holding_id: int | None = None
    asset_type: str = "stock"
    name: str = ""
    nse_symbol: str | None = None
    bse_code: str | None = None
    scheme_code: str | None = None
    isin: str | None = None
    charges: Decimal = (
        ZERO  # expected charges of this trade (brokerage, STT, stamp duty ...); 0 = not entered
    )


def item(key: str, label: str, status: str, value: Any = None, detail: str = "", source: str = "",
         **extra: Any) -> dict[str, Any]:  # fmt: skip
    return {
        "key": key,
        "label": label,
        "status": status,
        "value": value,
        "detail": detail,
        "source": source,
        **extra,
    }


def _f(x: Decimal | None, nd: int = 2) -> float | None:
    return None if x is None else round(float(x), nd)


def _inr(x: Decimal | float) -> str:
    return f"₹{float(x):,.0f}"


def _key_of(p: Plan, h: PortfolioHolding | None) -> str:
    from finresearch.portfolio.behaviour import group_key

    if h is not None:
        return group_key(h)
    if p.isin:
        return f"ISIN:{p.isin.upper()}"
    if p.nse_symbol:
        return f"NSE:{p.nse_symbol.upper()}"
    if p.bse_code:
        return f"BSE:{p.bse_code}"
    if p.scheme_code:
        return f"MF:{p.scheme_code}"
    return f"NAME:{p.name.strip().lower()}"


def stock_limit(s: Session) -> tuple[float, str]:
    from finresearch.signals.stock import CAPS
    from finresearch.suggest.advisor import load_profile

    prof = load_profile(s)
    if prof.max_position_pct:
        return float(prof.max_position_pct), "your profile's max position"
    risk = prof.risk_appetite or "medium"
    return CAPS.get(risk, 0.08) * 100, f"default for a {risk}-risk profile (signals.stock.CAPS)"


# --------------------------------------------------------------------------- size and concentration
def concentration_items(s: Session, p: Plan, key: str, h: PortfolioHolding | None) -> list[dict[str, Any]]:
    from finresearch.portfolio import analytics_math as am
    from finresearch.portfolio import cache
    from finresearch.portfolio.analytics import SECTOR_LIMIT_PCT
    from finresearch.portfolio.behaviour import group_key

    val = cache.read(s, cache.VALUATION)
    trade = p.quantity * p.price
    if not val.get("holdings") or not val.get("value"):
        why = "unknown: the daily valuation has not run yet (it runs after the close, or open /portfolio)"
        return [
            item("position_size", "Position size", "unknown", None, why, "cache:valuation"),
            item("concentration", "Concentration after the trade", "unknown", None, why, "cache:valuation"),
        ]
    total = Decimal(str(val["value"]))
    hold = {int(k): v for k, v in val["holdings"].items()}
    rows = {r.id: r for r in s.scalars(select(PortfolioHolding).where(PortfolioHolding.id.in_(list(hold))))}
    by_key: dict[str, Decimal] = {}
    sector_of: dict[str, str | None] = {}
    for hid, v in hold.items():
        if v.get("value") is None or hid not in rows:
            continue
        k = group_key(rows[hid])
        by_key[k] = by_key.get(k, ZERO) + Decimal(str(v["value"]))
        sector_of[k] = v.get("sector") or sector_of.get(k)
    limit, limit_src = stock_limit(s)
    size_pct = trade / total * 100 if total > 0 else None
    out = [item("position_size", "Position size", "warn" if p.side == "buy" and size_pct is not None and
                size_pct > Decimal(str(limit)) else "ok", _f(size_pct),
                f"{_inr(trade)} is {float(size_pct):.1f} % of the portfolio ({_inr(total)} at the last valuation, "
                f"{val.get('day')})" if size_pct is not None else "the portfolio has no value yet",
                "cache:valuation", trade_value=_f(trade), portfolio_value=_f(total))]  # fmt: skip
    before = dict(by_key)
    after = dict(by_key)
    if p.side == "buy":
        after[key] = after.get(key, ZERO) + trade
    else:
        after[key] = max(ZERO, after.get(key, ZERO) - trade)
    after = {k: v for k, v in after.items() if v > 0}
    tot_after = sum(after.values(), ZERO)
    if tot_after <= 0:
        out.append(item("concentration", "Concentration after the trade", "info", 0.0,
                        "the sale empties the portfolio", "cache:valuation"))  # fmt: skip
        return out
    w_before = before.get(key, ZERO) / total * 100 if total > 0 else ZERO
    w_after = after.get(key, ZERO) / tot_after * 100
    sector = (h.sector if h is not None and h.sector else None) or sector_of.get(key)
    sec_after = None
    if sector:
        sec_after = (
            sum((v for k, v in after.items() if sector_of.get(k) == sector or k == key), ZERO)
            / tot_after
            * 100
        )
    nb = am.n_effective([float(v) for v in before.values()]) if before else None
    na = am.n_effective([float(v) for v in after.values()])
    status, notes = "ok", []
    is_stock = p.asset_type == "stock"
    if is_stock and w_after > Decimal(str(limit)):
        status = "warn"
        notes.append(f"above your single-stock limit of {limit:g} % ({limit_src})")
    if is_stock and sec_after is not None and sec_after > Decimal(str(SECTOR_LIMIT_PCT)):
        status = "warn"
        notes.append(
            f"{sector} would be {float(sec_after):.1f} %, above the {SECTOR_LIMIT_PCT:g} % rule of thumb [W]"
        )
    detail = (f"weight {float(w_before):.1f} % → {float(w_after):.1f} %; effective number of holdings "
              f"{nb:.1f} → {na:.1f}" if nb is not None else f"weight → {float(w_after):.1f} %")  # fmt: skip
    if notes:
        detail += "; " + "; ".join(notes)
    out.append(item("concentration", "Concentration after the trade", status, _f(w_after), detail,
                    "cache:valuation; portfolio.analytics_math.n_effective (1/HHI)", weight_before=_f(w_before),
                    sector=sector, sector_after=_f(sec_after), n_effective_before=round(nb, 2) if nb else None,
                    n_effective_after=round(na, 2), limit_pct=limit, limit_source=limit_src))  # fmt: skip
    return out


# --------------------------------------------------------------------------- tax, exit load, ELSS lock
def is_elss(h: PortfolioHolding) -> bool:
    text = f"{h.name} {h.category or ''}".lower()
    return h.asset_type == "mf" and ("elss" in text or "tax saver" in text or "taxsaver" in text)


def sell_items(s: Session, p: Plan, h: PortfolioHolding | None) -> list[dict[str, Any]]:
    from finresearch.api.portfolio_analytics import get_settings_row
    from finresearch.fincalc.dates import fiscal_year
    from finresearch.fincalc.tax import add_months, tax_delta
    from finresearch.portfolio.report import disposal_rows, holding_tax, load
    from finresearch.portfolio.tax import DisposalRow, evaluate, gains_of
    from finresearch.portfolio.tax_watch import lt_date
    from finresearch.suggest.advisor import load_profile

    if h is None:
        return [item("tax", "Tax on this sale", "block", None, "you do not hold this instrument in the app: choose a "
                     "holding to sell", "portfolio")]  # fmt: skip
    data = load(s)
    ht = holding_tax(h)
    lots = [lot for lot in data.lots.get(h.id, []) if lot.open_quantity > Decimal("0.0005")]
    lots.sort(key=lambda lot: (lot.acquired or date.min, lot.id))
    held = sum((lot.open_quantity for lot in lots), ZERO)
    out: list[dict[str, Any]] = []
    if p.quantity > held + Decimal("0.0005"):
        return [item("units", "Units held", "block", _f(held, 4), f"you hold {float(held):g} units in {h.account}; "
                     f"the sale is for {float(p.quantity):g}", "portfolio lots")]  # fmt: skip
    gross = p.quantity * p.price
    pieces: list[tuple[Any, Decimal]] = []
    left = p.quantity
    for lot in lots:
        if left <= 0:
            break
        t = min(left, lot.open_quantity)
        pieces.append((lot, t))
        left -= t
    rows, unknown, near = [], 0, []
    fy = fiscal_year(p.day)
    base = gains_of([r for r in disposal_rows(data) if r.fy == fy])
    slab = Decimal(str(load_profile(s).tax_slab_pct)) / 100
    for lot, t in pieces:
        proceeds = p.price * t - (p.charges * t / p.quantity if p.quantity else ZERO)
        cost = lot.cost_per_unit * t if lot.cost_per_unit is not None else None
        row = evaluate(DisposalRow(ht, lot.acquired, p.day, t, cost, proceeds, lot.stt_paid, lot.origin))
        rows.append(row)
        if row.gain is None:
            unknown += 1
        if lot.acquired is not None and cost is not None and row.gain is not None and row.gain > 0:
            d = lt_date(ht, lot.acquired, p.day)
            if d is not None and (d - p.day).days <= NEAR_LT_DAYS:
                later = evaluate(
                    DisposalRow(ht, lot.acquired, d, t, cost, proceeds, lot.stt_paid, lot.origin)
                )
                fy_l = fiscal_year(d)
                t_now = tax_delta(base, gains_of([row]), fy, slab)
                t_later = tax_delta(base if fy_l == fy else [], gains_of([later]), fy_l, slab)
                near.append({"acquired": lot.acquired.isoformat(), "units": float(t), "long_term_from": d.isoformat(),
                             "days": (d - p.day).days, "tax_now": _f(t_now), "tax_later": _f(t_later),
                             "saved": _f(t_now - t_later)})  # fmt: skip
    tax = tax_delta(base, gains_of(rows), fy, slab)
    st = sum((r.gain for r in rows if r.gain is not None and r.cls and r.cls.term == "short"), ZERO)
    lt = sum((r.gain for r in rows if r.gain is not None and r.cls and r.cls.term == "long"), ZERO)
    gain = sum((r.gain for r in rows if r.gain is not None), ZERO)
    status = "unknown" if unknown else "ok"
    detail = (f"gain {_inr(gain)} (short-term {_inr(st)}, long-term {_inr(lt)}); this year's tax changes by "
              f"{_inr(tax)} incl. cess at your {float(slab * 100):g} % slab")  # fmt: skip
    if unknown:
        detail += f"; {unknown} lot(s) have an unknown cost or date: enter them for a full figure"
    if not p.charges:
        detail += "; charges not entered (they reduce the gain)"
    out.append(item("tax", "Tax on this sale", status, _f(tax), detail,
                    "portfolio.tax (dated rules) + fincalc.tax.tax_delta over this year's realised gains",
                    gain=_f(gain), short_term=_f(st), long_term=_f(lt), lots=len(pieces), fy=fy))  # fmt: skip
    if near:
        saved = sum((Decimal(str(x["saved"] or 0)) for x in near), ZERO)
        out.append(item("long_term_soon", "Turns long-term soon", "warn" if saved > 0 else "info", _f(saved),
                        f"{len(near)} lot(s) turn long-term within {NEAR_LT_DAYS} days; waiting would save about "
                        f"{_inr(saved)} at today's price (the price may move)",
                        "portfolio.tax_watch.lt_date", lots=near))  # fmt: skip
    if h.asset_type == "mf":
        st_row = get_settings_row(s)["exit_loads"].get(str(h.id))
        if not st_row:
            out.append(item("exit_load", "Exit load", "unknown", None, "the app has no exit-load data: enter the "
                            "scheme's load and period under Costs on /portfolio", "your settings"))  # fmt: skip
        else:
            pct, days = Decimal(str(st_row["pct"])) / 100, int(st_row.get("days") or 0)
            load_amt = sum((p.price * t * pct for lot, t in pieces
                            if lot.acquired is not None and (p.day - lot.acquired).days < days), ZERO)  # fmt: skip
            out.append(item("exit_load", "Exit load", "warn" if load_amt > 0 else "ok", _f(load_amt),
                            f"{float(pct * 100):g} % on units held under {days} days: {_inr(load_amt)}",
                            "your settings (scheme document)"))  # fmt: skip
        if is_elss(h):
            locked = sum((t for lot, t in pieces if lot.acquired is None
                          or add_months(lot.acquired, 12 * ELSS_LOCK_YEARS) > p.day), ZERO)  # fmt: skip
            out.append(item("elss_lock", "ELSS lock-in", "block" if locked > 0 else "ok", _f(locked, 4),
                            (f"{float(locked):g} of the units are within {ELSS_LOCK_YEARS} years of purchase (or the "
                             "date is unknown) and cannot be redeemed" if locked > 0 else
                             f"every unit sold is over {ELSS_LOCK_YEARS} years old"),
                            "ELSS 2005 (3-year lock-in per purchase); scheme recognised by name [unverified]"))  # fmt: skip
    out.append(item("proceeds", "Proceeds", "info", _f(gross - p.charges), f"{_inr(gross)} less charges "
                    f"{_inr(p.charges)}", "your entry"))  # fmt: skip
    return out


# --------------------------------------------------------------------------- signal, red flags, thesis, activity
SignalFetch = Callable[[str, str], Awaitable[Any]]


async def signal_item(
    s: Session, p: Plan, h: PortfolioHolding | None, live: SignalFetch | None
) -> dict[str, Any]:
    import asyncio

    from finresearch.portfolio import cache

    asset, code = None, None
    if p.asset_type == "stock":
        sym = (h.nse_symbol if h else None) or p.nse_symbol
        bse = (h.bse_code if h else None) or p.bse_code
        asset, code = "stock", (sym.upper() if sym else (f"BSE:{bse}" if bse else None))
    elif p.asset_type == "mf":
        asset, code = "fund", (h.scheme_code if h else None) or p.scheme_code
    if not code:
        return item(
            "signal", "Signal", "unknown", None, "no exchange symbol or scheme code to read a signal for", ""
        )
    got = (cache.read(s, cache.SIGNALS).get("items") or {}).get(f"{asset}:{code}")
    src = f"daily pass {got.get('day')}" if got else ""
    if got is None and live is not None:
        try:
            sig = await asyncio.wait_for(live(asset, code), SIGNAL_LIVE_TIMEOUT_S)
            got = {"action": sig.action, "score": sig.score, "probability": sig.probability,
                   "probability_interval": list(sig.probability_interval) if sig.probability_interval else None,
                   "event": sig.event, "horizon": sig.horizon, "validation": sig.validation.status,
                   "n": sig.validation.n}  # fmt: skip
            src = "live signal"
        except Exception as e:
            return item(
                "signal", "Signal", "unknown", None, f"couldn't read the signal ({type(e).__name__})", "live"
            )
    if got is None:
        return item(
            "signal", "Signal", "unknown", None, "not computed yet for this instrument", "cache:signals"
        )
    action = str(got.get("action") or "")
    against = (p.side == "buy" and action in ("SELL", "REDUCE", "AVOID")) or (
        p.side == "sell" and action in ("BUY",)
    )
    prob, ci = got.get("probability"), got.get("probability_interval")
    unc = []
    if prob is not None:
        unc.append(f"P({got.get('event') or 'event'}) = {prob:.0%}" + (f" (95 % interval {ci[0]:.0%}–{ci[1]:.0%})"
                   if ci else " (no interval)"))  # fmt: skip
    unc.append(
        f"validation: {got.get('validation') or 'unknown'}" + (f", n = {got['n']}" if got.get("n") else "")
    )
    return item("signal", "Signal", "warn" if against else "info", action,
                f"{action} ({got.get('horizon') or ''}); " + "; ".join(unc) +
                (". The signal points the other way" if against else ""),
                src, probability=prob, probability_interval=ci, validation=got.get("validation"), n=got.get("n"))  # fmt: skip


def red_flag_item(key: str, asset_type: str) -> dict[str, Any]:
    flags = RED_FLAGS(key, asset_type) if RED_FLAGS is not None else None
    if flags is None:
        return item("red_flags", "Surveillance and pledge flags", "unknown", None,
                    "not available yet: the ASM/GSM, F&O ban and pledge feed is not built in this version", "")  # fmt: skip
    if not flags:
        return item("red_flags", "Surveillance and pledge flags", "ok", 0, "none found", "exchange lists")
    return item("red_flags", "Surveillance and pledge flags", "warn", len(flags),
                "; ".join(f["label"] for f in flags), "exchange lists", flags=flags)  # fmt: skip


def thesis_item(s: Session, p: Plan, key: str, h: PortfolioHolding | None) -> dict[str, Any]:
    q = select(TradeNote).where(TradeNote.status.in_(("active", "planned")), TradeNote.side == "buy")
    notes = [n for n in s.scalars(q.order_by(TradeNote.id.desc()))
             if (h is not None and n.holding_id == h.id) or n.instrument == key]  # fmt: skip
    if not notes:
        return item("thesis", "Your thesis", "info", None,
                    "no journal entry for this instrument yet: write why, for how long, and what would prove you "
                    "wrong" if p.side == "buy" else "no thesis was written when you bought: judge the sale on its own "
                    "merits", "journal")  # fmt: skip
    n = notes[0]
    detail = f"{n.thesis or '(no thesis written)'}"
    if n.invalidation:
        detail += f" — exit if: {n.invalidation}"
    if p.side == "sell":
        detail += ". Has that exit condition been met, or is this a reaction to the price?"
    return item("thesis", "Your thesis", "info", n.id, detail, "journal", notes=[x.id for x in notes])


def activity_item(s: Session, day: date) -> dict[str, Any]:
    from finresearch.portfolio.journal import is_decision

    txns = s.scalars(select(PortfolioTxn).where(PortfolioTxn.day > day - timedelta(days=ACTIVITY_DAYS),
                                                PortfolioTxn.day <= day)).all()  # fmt: skip
    n = len({(t.holding_id, t.day, t.kind) for t in txns if is_decision(t)})
    return item("activity", "Your recent trading", "info", n, f"{n} buy/sell decision(s) in the last {ACTIVITY_DAYS} "
                "days. The most active traders earned 11.4 % a year against 17.9 % for the market (Barber & Odean "
                "2000)", "portfolio transactions")  # fmt: skip


# --------------------------------------------------------------------------- the checklist
async def checklist(s: Session, p: Plan, *, live_signal: SignalFetch | None = None) -> dict[str, Any]:
    h = s.get(PortfolioHolding, p.holding_id) if p.holding_id else None
    if p.holding_id and h is None:
        raise LookupError(f"unknown holding {p.holding_id}")
    if h is not None:
        p.asset_type = h.asset_type
        p.name = h.name
    key = _key_of(p, h)
    items: list[dict[str, Any]] = []
    if p.side == "sell":
        items += sell_items(s, p, h)
    items += concentration_items(s, p, key, h)
    items.append(await signal_item(s, p, h, live_signal))
    items.append(red_flag_item(key, p.asset_type))
    items.append(thesis_item(s, p, key, h))
    items.append(activity_item(s, p.day))
    worst = "block" if any(i["status"] == "block" for i in items) else \
        "warn" if any(i["status"] == "warn" for i in items) else "ok"  # fmt: skip
    return {"side": p.side, "instrument": key, "name": p.name, "holding_id": h.id if h else None,
            "asset_type": p.asset_type, "quantity": float(p.quantity), "price": float(p.price),
            "value": _f(p.quantity * p.price), "day": p.day.isoformat(), "status": worst, "items": items,
            "note": "A checklist, not advice: it shows what the trade does; the decision is yours."}  # fmt: skip
