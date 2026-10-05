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
  limit (else 5 / 8 / 10 % by risk appetite: portfolio.limits.position_limit, shared with the stock signal and
  /portfolio) and the 25 % sector rule of thumb
  (portfolio.analytics.SECTOR_LIMIT_PCT); HHI and the effective number of holdings before and after (a buy is
  assumed to bring new money; a sale's proceeds leave the portfolio);
- tax (sales): the open lots the sale takes first-in-first-out (CBDT Circular 768), each taxed with the dated rules
  (portfolio.tax.evaluate), the change in this financial year's tax incl. cess against the year's realised gains
  (fincalc.tax.tax_delta), and lots that turn long-term within 60 days (portfolio.tax_watch.lt_date) with the tax
  saved by waiting at today's price;
- exit load (fund sales): the load the user entered for the scheme (the app has no exit-load data);
- ELSS lock-in (fund sales): units of an ELSS are locked for 3 years from the allotment of each lot (every SIP
  instalment, IDCW reinvestment and switch-in separately; portfolio.elss cites the rule). The scheme is recognised by
  AMFI's category (the daily valuation's NAVAll category), else by its name [unverified]. Redemptions are FIFO and
  the locked lots are the newest, so the units that can be sold today are the unlocked ones: a sale above them is
  blocked, and the item says how many can be sold and when the next lot unlocks;
- the signal and its uncertainty: action, probability with its interval and how the method was validated (n);
- surveillance flags (ASM/GSM, F&O ban, pledge): finresearch.disclosures (`RED_FLAGS` overrides it in tests);
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
EPS_UNITS = Decimal("0.0005")  # units below this are rounding noise (portfolio.lots.EPS)
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
    """(limit %, the rule that applied) from portfolio.limits.position_limit, shared with /portfolio and the signal."""
    from finresearch.portfolio.limits import position_limit
    from finresearch.suggest.advisor import load_profile

    lim = position_limit(load_profile(s))
    return lim.pct, lim.rule


# --------------------------------------------------------------------------- size and concentration
def concentration_items(s: Session, p: Plan, key: str, h: PortfolioHolding | None) -> list[dict[str, Any]]:
    from finresearch.portfolio import analytics_math as am
    from finresearch.portfolio import cache
    from finresearch.portfolio.analytics import SECTOR_LIMIT_PCT
    from finresearch.portfolio.behaviour import group_key

    val = cache.read(s, cache.VALUATION)
    trade = p.quantity * p.price
    if not val.get("holdings") or not val.get("value"):
        why = "unknown: the monitor's daily valuation has not run yet (it runs after the close)"
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
        notes.append(f"above your single-stock limit ({limit_src})")
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
def elss_item(s: Session, p: Plan, h: PortfolioHolding, lots: list[Any]) -> dict[str, Any] | None:
    """The ELSS lock-in item of a fund sale, or None when the fund is not an ELSS."""
    from finresearch.portfolio import cache, elss

    v = cache.read(s, cache.VALUATION)
    det = elss.detect(h.asset_type, h.name, elss.category_of(h, v))
    if det is None:
        return None
    unlocked, locked, unknown = elss.split_units(lots, p.day)
    view = elss.lockin(lots, p.day, p.price, det)
    nxt = view["next_unlock"]
    when = f"; next {nxt['units']:g} unit(s) unlock on {nxt['day']}" if nxt else ""
    # FIFO takes undated lots (an opening balance) first, but their lock cannot be known: they are not counted as
    # sellable, and a sale that needs them is "unknown" rather than "ok"
    in_sale_locked = max(ZERO, p.quantity - unlocked - unknown)
    if p.quantity <= unlocked + EPS_UNITS:
        status, detail = "ok", (f"every unit sold is past its 3-year lock-in: you can redeem up to "
                                f"{float(unlocked):g} unit(s) today{when}")  # fmt: skip
    elif p.quantity <= unlocked + unknown + EPS_UNITS:
        status, detail = "unknown", (f"only {float(unlocked):g} unit(s) are known to be unlocked; "
                                     f"{float(unknown):g} have no purchase date (enter it to check the lock){when}")  # fmt: skip
    else:
        status, detail = "block", (f"you can redeem at most {float(unlocked):g} unit(s) today: {float(locked):g} are "
                                   f"still within 3 years of allotment{when}"
                                   + (f"; {float(unknown):g} have no purchase date" if unknown > 0 else ""))  # fmt: skip
    src = f"{elss.SOURCE}; {det.why}"
    return item("elss_lock", "ELSS lock-in", status, _f(in_sale_locked, 4), detail, src,
                sellable_units=_f(unlocked, 4), locked_units=_f(locked, 4), unknown_units=_f(unknown, 4),
                next_unlock=nxt, verified=det.verified, schedule=view["schedule"])  # fmt: skip


def sell_items(s: Session, p: Plan, h: PortfolioHolding | None) -> list[dict[str, Any]]:
    from finresearch.api.portfolio_analytics import get_settings_row
    from finresearch.fincalc.dates import fiscal_year
    from finresearch.fincalc.tax import tax_delta
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
        if (got := elss_item(s, p, h, lots)) is not None:
            out.append(got)
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
        from finresearch.disclosures.store import isin_map, stock_key

        asset, code = "stock", stock_key((h.isin if h else None) or p.isin, (h.nse_symbol if h else None) or p.nse_symbol,
                                         (h.bse_code if h else None) or p.bse_code, isin_map(s))  # fmt: skip
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
                   "n": sig.validation.n, "call": getattr(sig, "call", None)}  # fmt: skip
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
    prob, ci = got.get("probability"), got.get("probability_interval")
    if asset == "stock":
        from finresearch.signals import stock as stock_signal

        call = got.get("call") or {}
        if call.get("status") == "informational" or not stock_signal.CALLS_ENABLED:
            return _informational_item(got, call, stock_signal, src)
    against = (p.side == "buy" and action in ("SELL", "REDUCE", "AVOID")) or (
        p.side == "sell" and action in ("BUY",)
    )
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


def _informational_item(
    got: dict[str, Any], call: dict[str, Any], stock_signal: Any, src: str
) -> dict[str, Any]:
    """The stock signal while it has no proven edge (#193): its factor tilt and probability next to the base rate,
    never "the signal points the other way". A reading cached before the switch (no `call`) gets its tilt from the
    score on the same cut-offs."""
    score = got.get("score")
    by_action = {
        "BUY": 50.0,
        "ACCUMULATE": 20.0,
        "HOLD": 0.0,
        "REDUCE": -20.0,
        "SELL": -50.0,
    }  # cut-off scores
    if score is None:
        score = by_action.get(str(call.get("composite_action") or got.get("action") or ""))
    tilt = call.get("tilt") or (
        stock_signal.tilt(float(score)) if score is not None else "factor tilt unknown"
    )
    prob, ci = got.get("probability"), got.get("probability_interval")
    parts = [f"{stock_signal.INFORMATIONAL_LABEL}: {tilt} ({got.get('horizon') or ''})"]
    if call.get("probability_vs_base"):
        parts.append(call["probability_vs_base"])
    elif prob is not None:
        parts.append(f"P({got.get('event') or 'event'}) = {prob:.0%}" + (f" (95 % interval {ci[0]:.0%}–{ci[1]:.0%})"
                     if ci else ""))  # fmt: skip
    parts.append("not a reason to trade either way")
    return item("signal", "Signal", "info", "INFORMATIONAL", "; ".join(parts), src, probability=prob,
                probability_interval=ci, validation=got.get("validation"), n=got.get("n"), tilt=tilt,
                informational=True)  # fmt: skip


def overall_status(items: list[dict[str, Any]]) -> str:
    """block > warn > incomplete > ok. A check that could not be done ("unknown": data unavailable, no signal yet)
    makes the checklist incomplete: "nothing flagged" would imply it was checked and found clean."""
    statuses = {i["status"] for i in items}
    for s in ("block", "warn"):
        if s in statuses:
            return s
    return "incomplete" if "unknown" in statuses else "ok"


def red_flag_item(key: str, asset_type: str) -> dict[str, Any]:
    if RED_FLAGS is not None:
        flags = RED_FLAGS(key, asset_type)
    else:
        from finresearch.disclosures.views import pretrade_flags

        flags = pretrade_flags(key, asset_type)
    if flags is None:
        return item("red_flags", "Surveillance and pledge flags", "unknown", None,
                    "unavailable: NSE's ASM/GSM, F&O ban or pledge data has no recent good read (not the same as "
                    "none)", "finresearch.disclosures")  # fmt: skip
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
def _flag_key(s: Session, key: str, h: PortfolioHolding | None) -> str:
    """The key the red-flag lookup resolves (disclosures.views.pretrade_flags): an "ISIN:" key goes through the stored
    ISIN map; when the map does not know the ISIN yet, the holding's own NSE symbol is used if it is a real one."""
    from finresearch.disclosures.store import isin_map
    from finresearch.portfolio.importers import nse_symbol_or_none

    if key.startswith("ISIN:") and h is not None and key[5:] not in isin_map(s):
        sym = nse_symbol_or_none(h.nse_symbol)
        if sym:
            return f"NSE:{sym}"
    return key


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
    items.append(red_flag_item(_flag_key(s, key, h), p.asset_type))
    items.append(thesis_item(s, p, key, h))
    items.append(activity_item(s, p.day))
    worst = overall_status(items)
    return {"side": p.side, "instrument": key, "name": p.name, "holding_id": h.id if h else None,
            "asset_type": p.asset_type, "quantity": float(p.quantity), "price": float(p.price),
            "value": _f(p.quantity * p.price), "day": p.day.isoformat(), "status": worst, "items": items,
            "incomplete": [i["label"] for i in items if i["status"] == "unknown"],
            "note": "A checklist, not advice: it shows what the trade does; the decision is yours."}  # fmt: skip
