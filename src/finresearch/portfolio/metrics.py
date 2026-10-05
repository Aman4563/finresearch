"""Portfolio metrics for the alert rules (finresearch.alerts.portfolio contract) and the value history behind them.

    alert_metrics(session) -> {"allocation_drift_pp": (value, source[, detail]), "ltcg_headroom_inr": (...), ...}

Synchronous and offline: it reads the database only. Market values come from the latest snapshot written when the
portfolio was last valued (the /portfolio page or GET /api/portfolio), so the source string always says its date.

- ltcg_headroom_inr: the unused s.112A / s.198 exemption in the current financial year, after this year's realised
  equity LTCG and set-off (fincalc.tax.fy_tax). Needs no prices.
- allocation_drift_pp: the largest |weight - target| in percentage points across asset classes, from the latest
  complete snapshot and the targets saved on /portfolio (Allocation tab).
- drawdown_pct: how far a time-weighted value index sits below its peak, in % (0 at a new high). The index chains
  day-to-day returns with net new money removed ((V_t - flow_t) / V_{t-1}), so buying more or selling does not look
  like a gain or a loss. Only days on which every holding was priced count.

The daily-layer metrics (PORTFOLIO_METRICS below) read what the monitor's daily portfolio pass stored
(monitor.portfolio_daily -> portfolio.cache) plus the transactions; each says the date of the data it used, and
returns None with the reason when it cannot be computed. Rules of thumb (10 % per position, 25 % per sector) are the
registry's *default thresholds*, which the user changes; they are labelled as such there.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioSetting, PortfolioSnapshot

ASSET_CLASSES = (
    "Stocks",
    "Equity funds",
    "Debt funds",
    "Gold & international funds",
    "Sovereign Gold Bonds",
    "Other",
)


def get_targets(s: Session) -> dict[str, float]:
    row = s.get(PortfolioSetting, "targets")
    return {k: float(v) for k, v in ((row.value or {}) if row else {}).items() if k in ASSET_CLASSES}


def set_targets(s: Session, targets: dict[str, Any]) -> dict[str, float]:
    clean = {k: round(float(v), 2) for k, v in targets.items() if k in ASSET_CLASSES and v not in (None, "")}
    if any(v < 0 or v > 100 for v in clean.values()):
        raise ValueError("each target must be between 0 and 100 %")
    total = sum(clean.values())
    if clean and abs(total - 100) > 0.5:
        raise ValueError(f"targets must add up to 100 % (they add up to {total:g} %)")
    row = s.get(PortfolioSetting, "targets")
    if row is None:
        s.add(PortfolioSetting(key="targets", value=clean))
    else:
        row.value = clean
    s.flush()
    return clean


def drift(by_asset: dict[str, float], targets: dict[str, float]) -> list[dict[str, Any]]:
    """Weight vs target per asset class (pp), largest gap first. Empty without targets or value."""
    total = sum(by_asset.values())
    if not targets or total <= 0:
        return []
    rows = []
    for k in sorted(set(targets) | set(by_asset)):
        w = by_asset.get(k, 0.0) / total * 100
        t = targets.get(k, 0.0)
        rows.append({"label": k, "weight_pct": round(w, 2), "target_pct": t, "drift_pp": round(w - t, 2)})
    return sorted(rows, key=lambda r: (-abs(r["drift_pp"]), -r["drift_pp"]))  # ties: overweight first


def record_snapshot(s: Session, day: date, value: float, invested: float, by_asset: dict[str, float],
                    complete: bool) -> None:  # fmt: skip
    """Upsert the day's value (the latest valuation of the day wins)."""
    vals = {"day": day, "value": Decimal(str(round(value, 2))), "invested": Decimal(str(round(invested, 2))),
            "by_asset": {k: round(v, 2) for k, v in by_asset.items()}, "complete": complete}  # fmt: skip
    stmt = insert(PortfolioSnapshot).values(**vals)
    s.execute(
        stmt.on_conflict_do_update(index_elements=["day"], set_={k: v for k, v in vals.items() if k != "day"})
    )


def drawdown(snaps: list[tuple[date, float, float]]) -> tuple[float | None, str]:
    """(drawdown %, how) from (day, value, cumulative net invested) rows in date order."""
    rows = [r for r in snaps if r[1] > 0]
    if len(rows) < 2:
        return None, "needs at least two days of complete valuations (open /portfolio on different days)"
    index, peak = 1.0, 1.0
    for (_, v0, i0), (_, v1, i1) in pairwise(rows):
        index *= (v1 - (i1 - i0)) / v0
        peak = max(peak, index)
    return round(
        (index / peak - 1) * 100, 2
    ), f"time-weighted value index over {len(rows)} valuation days ({rows[0][0]} to {rows[-1][0]})"


PORTFOLIO_METRICS = (
    "allocation_drift_pp", "ltcg_headroom_inr", "drawdown_pct", "holding_day_move_pct", "holding_signal_changed",
    "reduce_signal_weight_pct", "days_to_next_lt_lot", "lt_wait_tax_saved_inr", "ltcg_used_pct",
    "max_position_pct", "max_fund_pct", "max_sector_pct", "n_effective", "sip_missed", "dividend_received",
    "days_to_holding_ex_date", "days_to_holding_results_meeting", "days_since_holding_results",
    "fund_ter_change_pp", "advance_tax_due_inr", "regular_plan_value_inr", "unpriced_holdings",
    "days_to_elss_unlock",
)  # fmt: skip
NEGATIVE_ACTIONS = ("REDUCE", "SELL", "AVOID", "EXIT")
FRESH_DAYS = 5  # the daily valuation is used for alerts only when it is at most this many days old
NO_DAILY = (
    "no daily valuation yet (the monitor values the portfolio after each close; `finresearch serve` runs it)"
)

Out = dict[str, tuple[Any, ...]]


def D(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def _q(x: float | Decimal, places: str = "0.01") -> Decimal:
    return Decimal(str(x)).quantize(Decimal(places))


def slab_rate(session: Session) -> Decimal:
    from finresearch.suggest.advisor import load_profile

    try:
        return Decimal(str(load_profile(session).tax_slab_pct)) / 100
    except Exception:
        return Decimal("0.30")


def cached_prices(v: dict[str, Any]) -> dict[int, Decimal]:
    """Live prices only: a fallback from an old statement (a CAS NAV, a broker statement's close) is not today's
    price, as in report.tax_view and the daily valuation job."""
    return {int(h): Decimal(str(x["price"])) for h, x in (v.get("holdings") or {}).items()
            if x.get("price") is not None and "statement" not in (x.get("price_source") or "")}  # fmt: skip


def grouped_weights(v: dict[str, Any], only: str | None = None) -> dict[str, tuple[float, str]]:
    """Weight % per instrument (a stock or fund held in several accounts counts once) -> (weight, name).
    `only="stock"` keeps single stocks, `only="fund"` mutual funds and ETFs (portfolio.limits.is_fund_like)."""
    from finresearch.portfolio.limits import is_fund_like

    out: dict[str, tuple[float, str]] = {}
    for h in (v.get("holdings") or {}).values():
        if not h.get("weight_pct"):
            continue
        if only is not None:
            fund = is_fund_like(h.get("asset_type"), h.get("nse_symbol"), h.get("name"))
            if fund != (only == "fund") or (only == "stock" and h.get("asset_type") != "stock"):
                continue
        key = h.get("nse_symbol") or h.get("bse_code") or h.get("scheme_code") or h["name"]
        w, _ = out.get(key, (0.0, h["name"]))
        out[key] = (w + h["weight_pct"], h["name"])
    return out


def n_effective(weights_pct: list[float]) -> float | None:
    """1 / HHI with weights as fractions (HHI = Σ w²): the number of equal holdings with the same concentration."""
    tot = sum(weights_pct)
    if tot <= 0:
        return None
    hhi = sum((w / tot) ** 2 for w in weights_pct)
    return 1 / hhi


def lt_watch(session: Session, data: Any, v: dict[str, Any], today: date) -> list[dict[str, Any]]:
    from finresearch.portfolio.report import disposal_rows
    from finresearch.portfolio.tax_watch import open_lots_with_prices, turning_long_term

    cats = {int(h): x.get("category") for h, x in (v.get("holdings") or {}).items()}
    lots = open_lots_with_prices(data, cached_prices(v), cats)
    return turning_long_term(lots, disposal_rows(data, cats), today, slab_rate(session))


def elss_unlocks(session: Session, data: Any, today: date, days: int | None = None) -> list[dict[str, Any]]:
    """Every future unlock of the ELSS holdings' locked lots (portfolio.elss), soonest first, within `days` when given:
    {holding_id, name, account, day, days, units, value, verified}. The category is the daily valuation's AMFI
    category, else the holding's own or its name (unverified); the value uses the valuation's live price (None
    without one)."""
    from finresearch.portfolio import cache, elss

    v = cache.read(session, cache.VALUATION)
    px = cached_prices(v)
    out = []
    for h in data.holdings:
        lots = [lot for lot in data.lots.get(h.id, []) if lot.open_quantity > elss.EPS]
        det = elss.detect(h.asset_type, h.name, elss.category_of(h, v)) if lots else None
        if det is None:
            continue
        view = elss.lockin(lots, today, px.get(h.id), det)
        for lot in view["lots"]:
            if lot["status"] != "locked":
                continue
            d = date.fromisoformat(lot["unlocks"])
            if days is None or (d - today).days <= days:
                out.append({"holding_id": h.id, "name": h.name, "account": h.account, "day": lot["unlocks"],
                            "days": (d - today).days, "units": lot["units"],
                            "value": None if px.get(h.id) is None else
                            round(float(Decimal(str(lot["units"])) * px[h.id]), 2),
                            "verified": det.verified})  # fmt: skip
    # one row per holding and day (a month's SIP instalments bought on one day unlock together)
    merged: dict[tuple[int, str], dict[str, Any]] = {}
    for x in out:
        k = (x["holding_id"], x["day"])
        if k in merged:
            m = merged[k]
            m["units"] = round(m["units"] + x["units"], 4)
            m["value"] = (
                None if m["value"] is None or x["value"] is None else round(m["value"] + x["value"], 2)
            )
        else:
            merged[k] = dict(x)
    return sorted(merged.values(), key=lambda x: (x["day"], x["name"]))


def elss_unlock_metric(session: Session, data: Any, today: date) -> tuple[Any, ...]:
    from finresearch.portfolio import cache, elss

    v = cache.read(session, cache.VALUATION)
    if not any(elss.detect(h.asset_type, h.name, elss.category_of(h, v)) for h in data.holdings
               if any(lot.open_quantity > elss.EPS for lot in data.lots.get(h.id, []))):  # fmt: skip
        return (None, "no ELSS holding (AMFI category, else 'ELSS' in the name)")
    ahead = elss_unlocks(session, data, today)
    if not ahead:
        return (None, "every ELSS unit is past its 3-year lock-in (or its purchase date is unknown)")
    x = ahead[0]
    val = f" (₹{x['value']:,.0f})" if x["value"] is not None else ""
    return (
        Decimal(x["days"]),
        "open ELSS lots and their 3-year lock-in from allotment (portfolio.elss)",
        f"{x['name']}: {x['units']:g} units{val} unlock on {x['day']}",
    )


def advance_tax(session: Session, data: Any, today: date) -> dict[str, Any]:
    from finresearch.fincalc.dates import fiscal_year
    from finresearch.fincalc.tax import fy_tax
    from finresearch.fincalc.tax_calendar import advance_tax_estimate
    from finresearch.portfolio.report import disposal_rows
    from finresearch.portfolio.tax import gains_of

    fy = fiscal_year(today)
    slab = slab_rate(session)
    rows = [r for r in disposal_rows(data) if r.fy == fy]
    cg = fy_tax(gains_of(rows), fy, slab).total
    divs = sum((abs(t.amount) for ts in data.txns.values() for t in ts
                if t.kind == "dividend" and t.amount and fiscal_year(t.day) == fy), Decimal(0))  # fmt: skip
    return advance_tax_estimate(cg, divs, slab, today, fy)


def _fresh(v: dict[str, Any], today: date) -> str | None:
    """None when the daily valuation is recent, else why it cannot be used."""
    if not v.get("day"):
        return NO_DAILY
    age = (today - date.fromisoformat(v["day"])).days
    return None if age <= FRESH_DAYS else f"the last daily valuation is from {v['day']} ({age} days old)"


def alert_metrics(session: Session) -> Out:
    """Every portfolio metric: (value, source) or (value, source, detail); value None = not computable (the source
    says why)."""
    from finresearch.fincalc.dates import fiscal_year, today_ist
    from finresearch.fincalc.tax import fy_label
    from finresearch.portfolio import cache
    from finresearch.portfolio.report import disposal_rows, load
    from finresearch.portfolio.sip import sip_health
    from finresearch.portfolio.tax import fy_summary

    out: Out = {}
    data = load(session)
    if not data.holdings:
        reason = "the portfolio is not set up yet: no holdings (import them on /portfolio)"
        return {k: (None, reason) for k in PORTFOLIO_METRICS}
    today = today_ist()
    fy = fiscal_year(today)
    summary = fy_summary(disposal_rows(data), fy, Decimal("0.30"))  # the headroom does not depend on the slab
    ex = summary["exemption"]
    out["ltcg_headroom_inr"] = (Decimal(str(ex["remaining"])),
                                f"{fy_label(fy)}: ₹{ex['used']:,.0f} of ₹{ex['limit']:,.0f} "
                                "used by realised equity LTCG (fincalc.tax)")  # fmt: skip
    out["ltcg_used_pct"] = (_q(Decimal(str(ex["used"])) / Decimal(str(ex["limit"])) * 100),
                            f"{fy_label(fy)}: ₹{ex['used']:,.0f} of the ₹{ex['limit']:,.0f} exemption used (fincalc.tax)")  # fmt: skip
    snaps = session.scalars(select(PortfolioSnapshot).where(PortfolioSnapshot.complete.is_(True))
                            .order_by(PortfolioSnapshot.day)).all()  # fmt: skip
    if not snaps:
        out["allocation_drift_pp"] = (
            None,
            "no complete valuation yet (open /portfolio once every holding has a price)",
        )
    else:
        last = snaps[-1]
        targets = get_targets(session)
        rows = drift({k: float(v) for k, v in (last.by_asset or {}).items()}, targets)
        if not rows:
            out["allocation_drift_pp"] = (None, "no target allocation set (Portfolio → Allocation → Targets)")
        else:
            top = rows[0]
            out["allocation_drift_pp"] = (Decimal(str(abs(top["drift_pp"]))),
                                          f"{top['label']} {top['weight_pct']:g} % vs target {top['target_pct']:g} % "
                                          f"(valuation of {last.day.isoformat()})")  # fmt: skip
    dd, how = drawdown([(x.day, float(x.value), float(x.invested)) for x in snaps])
    out["drawdown_pct"] = (None if dd is None else Decimal(str(dd)), how)

    # ------------------------------------------------------------------ from transactions and lots (no prices)
    sips = sip_health(data.holdings, data.txns, today)
    late = [x for x in sips if x.status != "on track"]
    out["sip_missed"] = (Decimal(len(late)), f"{len(sips)} SIP(s) inferred from monthly purchases; "
                         f"{len(late)} missed or stopped (no instalment for over 35 days)",
                         ", ".join(f"{x.name}: {x.status}, last {x.last}" for x in late[:3]) or None)  # fmt: skip
    out["days_to_elss_unlock"] = elss_unlock_metric(session, data, today)
    at = advance_tax(session, data, today)
    nxt = at["next"]
    if nxt is None:
        out["advance_tax_due_inr"] = (Decimal(0), "no advance-tax instalment left this financial year")
    else:
        out["advance_tax_due_inr"] = (
            Decimal(str(nxt["amount"])),
            f"estimate: {nxt['cumulative_pct']:g} % of ₹{at['total']:,.0f} tax on this year's realised gains and "
            f"dividends, by {nxt['due']} (fincalc.tax_calendar; [unverified] schedule; salary, TDS and tax paid "
            "unknown)" + (" — below the ₹10,000 threshold" if at["below_threshold"] else ""),
        )

    # ------------------------------------------------------------------ from the monitor's daily pass
    v = cache.read(session, cache.VALUATION)
    stale = _fresh(v, today)
    vday = v.get("day")
    ran_today = vday == today.isoformat()
    if stale:
        for k in ("holding_day_move_pct", "max_position_pct", "max_fund_pct", "max_sector_pct", "n_effective",
                  "regular_plan_value_inr", "unpriced_holdings", "days_to_next_lt_lot", "lt_wait_tax_saved_inr",
                  "dividend_received"):  # fmt: skip
            out[k] = (None, stale)
    else:
        src = f"daily valuation of {vday}"
        moves = v.get("moves") or {}
        if not moves:
            out["holding_day_move_pct"] = (None, f"{src}: no earlier valuation day to compare with yet")
        else:
            hid, mv = max(moves.items(), key=lambda kv: abs(kv[1]))
            out["holding_day_move_pct"] = (_q(abs(mv)), f"{src} vs {v.get('moves_vs')} (price change per holding)",
                                           f"{v['holdings'][hid]['name']} {mv:+.2f}%")  # fmt: skip
        gw = grouped_weights(v)
        # the single-stock limit is for single stocks: a diversified fund or ETF is checked against its own
        # threshold (max_fund_pct, portfolio.limits.FUND_LIMIT_PCT), never against the stock limit
        for key, only, what in (("max_position_pct", "stock", "single stock"),
                                ("max_fund_pct", "fund", "fund or ETF")):  # fmt: skip
            part = grouped_weights(v, only)
            if part:
                w, name = max(part.values(), key=lambda x: x[0])
                out[key] = (_q(w), f"{src}: largest {what} by value (share of the whole portfolio)", name)
            else:
                out[key] = (None, f"{src}: no priced {what} held")
        if gw:
            ne = n_effective([w for w, _ in gw.values()])
            out["n_effective"] = (
                _q(ne),
                f"{src}: 1 / Σ weight² over {len(gw)} holdings (funds count as one each)",
            )
        else:
            out["n_effective"] = (None, f"{src}: no priced holding")
        sectors = {k: x for k, x in (v.get("sectors") or {}).items()
                   if x and k not in ("Funds (no look-through)", "Unclassified")}  # fmt: skip
        if sectors and v.get("value"):
            k, x = max(sectors.items(), key=lambda kv: kv[1])
            out["max_sector_pct"] = (_q(x / v["value"] * 100), f"{src}: largest sector of directly held stocks, as % "
                                     "of the whole portfolio (NSE industry; funds not looked through)", k)  # fmt: skip
        else:
            out["max_sector_pct"] = (None, f"{src}: no stock with a known sector")
        funds = [h for h in (v.get("holdings") or {}).values() if h["asset_type"] == "mf"]
        if funds:
            reg = [h for h in funds if h.get("plan") == "regular" and h.get("value")]
            unknown_plan = sum(1 for h in funds if h.get("plan") is None)
            out["regular_plan_value_inr"] = (_q(sum(h["value"] for h in reg)),
                                             f"{src}: {len(reg)} of {len(funds)} funds in a regular plan (scheme name)"
                                             + (f"; {unknown_plan} with no plan in the name" if unknown_plan else ""),
                                             ", ".join(h["name"] for h in reg[:3]) or None)  # fmt: skip
        else:
            out["regular_plan_value_inr"] = (Decimal(0), f"{src}: no mutual funds held")
        bad = (v.get("unpriced") or []) + (v.get("stale") or [])
        out["unpriced_holdings"] = (Decimal(len(bad)), f"{src}: holdings with no price or a price older than "
                                    "4 days / a statement NAV", ", ".join(x["name"] for x in bad[:3]) or None)  # fmt: skip
        lw = lt_watch(session, data, v, today)
        if lw:
            out["days_to_next_lt_lot"] = (Decimal(min(x["days"] for x in lw)), f"{src}: open lots in profit turning "
                                          "long-term within 30 days", f"{lw[0]['name']} on {lw[0]['lt_date']}")  # fmt: skip
            saved = sum(Decimal(str(x["saved"])) for x in lw)
            out["lt_wait_tax_saved_inr"] = (_q(saved), f"{src}: tax now minus tax after the long-term date, at today's "
                                            "price (fincalc.tax.tax_delta; FIFO: older lots sell first)",
                                            f"{len(lw)} lot(s); largest {lw[0]['name']} ₹{lw[0]['saved']:,.0f}")  # fmt: skip
        else:
            out["days_to_next_lt_lot"] = (None, f"{src}: no lot in profit turns long-term within 30 days")
            out["lt_wait_tax_saved_inr"] = (
                Decimal(0),
                f"{src}: no lot in profit turns long-term within 30 days",
            )
        divs = v.get("new_dividends") or []
        out["dividend_received"] = (Decimal(int(bool(divs) and ran_today)), f"{src}: dividends recorded since the "
                                    "previous daily pass", ", ".join(f"{d['name']} ₹{d['amount']:,.0f}"
                                                                     for d in divs[:3]) or None)  # fmt: skip

    sg = cache.read(session, cache.SIGNALS)
    if not sg.get("day"):
        out["holding_signal_changed"] = out["reduce_signal_weight_pct"] = (None, "no holdings' signals yet (computed "
                                                                           "in the daily pass)")  # fmt: skip
    else:
        ch = sg.get("changes") or []
        fresh = sg["day"] == today.isoformat()
        out["holding_signal_changed"] = (Decimal(int(bool(ch) and fresh)), f"holdings' signals of {sg['day']} "
                                         "(signals.* providers; not logged in the forecast ledger)",
                                         "; ".join(f"{c['name']} {'(informational) ' if c.get('informational') else ''}"
                                                   f"{c['from']} -> {c['to']}" for c in ch[:3]) or None)  # fmt: skip
        # an informational signal is no instruction (#193): a stock never counts here while signals.stock shows no
        # proven edge, also from a reading cached before that switch
        from finresearch.signals.stock import CALLS_ENABLED

        neg = [x for x in (sg.get("items") or {}).values() if x.get("action") in NEGATIVE_ACTIONS
               and not ((x.get("call") or {}).get("status") == "informational"
                        or (x.get("asset") == "stock" and not CALLS_ENABLED))]  # fmt: skip
        out["reduce_signal_weight_pct"] = (_q(sum(x.get("weight_pct") or 0 for x in neg)),
                                           f"holdings' signals of {sg['day']}: share of the portfolio whose signal "
                                           "says REDUCE or SELL", ", ".join(f"{x['name']} {x['action']}" for x in neg[:3]) or None)  # fmt: skip

    ev = cache.read(session, cache.EVENTS)
    if not ev.get("day"):
        for k in ("days_to_holding_ex_date", "days_to_holding_results_meeting", "days_since_holding_results"):
            out[k] = (None, "no holdings' events yet (read in the daily pass from NSE)")
    else:
        esrc = f"NSE data read {ev['day']}"
        ahead = sorted((date.fromisoformat(a["ex_date"]), st["name"], a["subject"]) for st in ev["stocks"].values()
                       for a in st["actions"] if date.fromisoformat(a["ex_date"]) >= today)  # fmt: skip
        out["days_to_holding_ex_date"] = ((Decimal((ahead[0][0] - today).days), f"{esrc}: corporate actions",
                                           f"{ahead[0][1]}: {ahead[0][2]} (ex {ahead[0][0]})") if ahead else
                                          (None, f"{esrc}: no upcoming ex-date for a stock holding"))  # fmt: skip
        mt = sorted((date.fromisoformat(m["day"]), st["name"]) for st in ev["stocks"].values()
                    for m in st["board_meetings"] if m["results"] and date.fromisoformat(m["day"]) >= today)  # fmt: skip
        out["days_to_holding_results_meeting"] = ((Decimal((mt[0][0] - today).days), f"{esrc}: board meetings",
                                                   f"{mt[0][1]} board meeting on {mt[0][0]} (results)") if mt else
                                                  (None, f"{esrc}: no results board meeting announced for a stock holding"))  # fmt: skip
        filed = sorted(((date.fromisoformat(st["last_results"]), st["name"]) for st in ev["stocks"].values()
                        if st.get("last_results")), reverse=True)  # fmt: skip
        out["days_since_holding_results"] = ((Decimal((today - filed[0][0]).days), f"{esrc}: financial-results filings",
                                              f"{filed[0][1]} filed results on {filed[0][0]}") if filed else
                                             (None, f"{esrc}: no results filing found for a stock holding"))  # fmt: skip

    tr = cache.read(session, cache.TER)
    known = [f for f in (tr.get("funds") or {}).values() if f.get("ter") is not None]
    if not known:
        out["fund_ter_change_pp"] = (
            None,
            "no TER read for a held fund yet (AMFI TER file, in the daily pass)",
        )
    else:
        changed = [
            f for f in known if f.get("changed_on") == today.isoformat() and f.get("prev_ter") is not None
        ]
        if changed:
            f = max(changed, key=lambda f: f["ter"] - f["prev_ter"])
            out["fund_ter_change_pp"] = (_q(f["ter"] - f["prev_ter"], "0.0001"), f"AMFI TER file ({f['day']})",
                                         f"{f['name']}: {f['prev_ter']}% -> {f['ter']}%")  # fmt: skip
        else:
            out["fund_ter_change_pp"] = (Decimal(0), f"AMFI TER file read {tr.get('day')}: no change for "
                                         f"{len(known)} held fund(s) since the previous read")  # fmt: skip
    return out
