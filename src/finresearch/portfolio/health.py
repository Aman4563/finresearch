"""Portfolio data health (#219): how complete the data behind each analysis is, what a gap blocks, and how to fix it.

One row per input, each with a coverage % (None = could not be measured), a status and a fix link:

    row               coverage measured as                                              weight
    purchase_dates    open lots with a known acquisition date, weighted by value          20
                      (units x price; units x cost when unpriced; lots with neither are counted, not weighed)
    priced            open holdings with a current price, by count                       20
                      (a statement price past its age limit is not current: valuation.statement_stale)
    dividends         completed FYs with stock holdings that have >= 1 dividend recorded  10
    lookthrough       fund value covered by a month-end fund portfolio (/api/lookthrough) 10
    ais               AIS imported for the last completed FY (yes/no)                    10
    history           daily returns in the value history, against the 120 that beta and  10
                      risk contribution need (volatility needs 60; VaR and Sharpe 250)
    corporate_actions held stocks whose corporate actions were synced in the last           10
                      ACTIONS_SYNC_MAX_DAYS and show no unresolved unsupported action (demerger, rights,
                      merger ... recorded by "Sync corporate actions", #237). Never synced or synced too
                      long ago = not known to be clean (#264); none synced at all = unknown
    targets           target allocation set (yes/no)                                       5
    goals_age         age set (half) and at least one goal (half)                         5

Overall = Σ weight x coverage / Σ weight over the rows that apply (a row that does not apply, e.g. look-through without
funds, drops out). A row that could not be measured counts as 0 %: unknown is never treated as complete. The weights
are a simple, stated judgement (prices and dates feed nearly every figure, so they count double), not a model.
Personal data: computed locally, never sent to an LLM.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import PortfolioAis, PortfolioHolding, PortfolioLot, PortfolioTxn
from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import fy_label

WEIGHTS = {"purchase_dates": 20, "priced": 20, "corporate_actions": 10, "dividends": 10, "lookthrough": 10, "ais": 10,
           "history": 10, "targets": 5, "goals_age": 5}  # fmt: skip
HISTORY_NEED = {
    "volatility": 60,
    "beta and risk contribution": 120,
    "VaR and Sharpe": 250,
}  # portfolio.analytics
HISTORY_FULL = 120
# A held stock's corporate actions count as checked for this many days after a successful "Sync corporate actions".
# A stated judgement, not a rule: exchanges announce an action's record date a few working days to weeks ahead, so a
# month-old read can miss an action announced since.
ACTIONS_SYNC_MAX_DAYS = 30
OPEN = Decimal("0.0005")
HOW = ("Overall = Σ weight × coverage ÷ Σ weight of the rows that apply. Weights: purchase dates 20, prices 20, "
       "corporate actions synced and resolved 10, dividends 10, fund look-through 10, AIS 10, performance history 10, targets 5, "
       "goals and age 5. A check that could not run counts as 0 %.")  # fmt: skip


def _status(cov: float | None) -> str:
    if cov is None:
        return "unknown"
    return "ok" if cov >= 99.5 else "missing" if cov <= 0 else "partial"


def _row(key: str, label: str, cov: float | None, detail: str, blocks: str, fix: str, href: str,
         applies: bool = True) -> dict[str, Any]:  # fmt: skip
    return {"key": key, "label": label, "weight": WEIGHTS[key],
            "coverage_pct": None if cov is None else round(cov, 1),
            "status": _status(cov) if applies else "not_applicable", "detail": detail, "blocks": blocks,
            "fix": fix, "href": href}  # fmt: skip


def purchase_dates(lots: list[PortfolioLot], price: dict[int, float | None]) -> dict[str, Any]:
    known = total = Decimal(0)
    unweighed = 0
    for lot in lots:
        p = price.get(lot.holding_id)
        unit = Decimal(str(p)) if p is not None else lot.cost_per_unit
        if unit is None:
            unweighed += 1
            continue
        w = lot.open_quantity * unit
        total += w
        known += w if lot.acquired is not None else 0
    undated = sum(1 for lot in lots if lot.acquired is None)
    cov = float(known * 100 / total) if total > 0 else (None if lots else 100.0)
    detail = f"{undated} of {len(lots)} open lot(s) have no purchase date"
    if unweighed:
        detail += f"; {unweighed} lot(s) without a price or cost could not be weighed"
        cov = None if total == 0 else cov
    return _row("purchase_dates", "Purchase dates known", cov, detail,
                "XIRR, the short/long-term tax split, LTCG harvesting and lots turning long-term",
                "Import an older tradebook or CAS, or open the holding and enter the purchase date",
                "/portfolio#import", applies=bool(lots))  # fmt: skip


def priced(rows: list[dict[str, Any]]) -> dict[str, Any]:
    held = [r for r in rows if not r.get("closed")]
    pending = sum(1 for r in held if r.get("pending"))
    # a statement price past its age limit (valuation.statement_stale) is not a current price (#238)
    stale = sum(1 for r in held if r.get("price_stale") and not r.get("pending"))
    ok = sum(
        1 for r in held if r.get("value") is not None and not r.get("pending") and not r.get("price_stale")
    )
    cov = (ok * 100 / len(held)) if held and not pending else None if held else 100.0
    detail = f"{ok} of {len(held)} holding(s) priced" + (f"; {pending} still loading" if pending else "")
    if stale:
        detail += f"; {stale} only by an old statement price"
    return _row("priced", "Holdings with a current price", cov, detail,
                "current value, unrealised P&L, allocation, risk and XIRR for the unpriced holdings",
                "Check the NSE symbol, ISIN or AMFI scheme code on the unpriced holding", "/portfolio#holdings",
                applies=bool(held))  # fmt: skip


def corporate_actions(rows: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """Held stocks whose corporate actions are known to be clean: synced within ACTIONS_SYNC_MAX_DAYS (the holding's
    `actions_synced`) and without an unresolved unsupported corporate action (#237). A stock never synced, or synced
    too long ago, is not known to be clean, so it does not count as covered; when no held stock was ever synced the
    row is unknown, never ok (#264). An action the exchange feed never listed cannot be seen here."""
    held = [r for r in rows if r.get("asset_type") == "stock" and not r.get("closed")]
    bad = [r for r in held if r.get("pending_actions")]

    def fresh(r: dict[str, Any]) -> bool:
        d = r.get("actions_synced")
        return d is not None and (today - date.fromisoformat(d)).days <= ACTIONS_SYNC_MAX_DAYS

    never = [r for r in held if not r.get("actions_synced") and not r.get("pending_actions")]
    stale = [r for r in held if r.get("actions_synced") and not fresh(r) and not r.get("pending_actions")]
    clean = [r for r in held if fresh(r) and not r.get("pending_actions")]
    if not held:
        cov: float | None = 100.0
    elif not any(r.get("actions_synced") for r in held) and not bad:
        cov = None  # never synced: unknown, not "none recorded"
    else:
        cov = len(clean) * 100 / len(held)
    parts = []
    if bad:
        parts.append(f"{len(bad)} of {len(held)} stock holding(s) have an unresolved corporate action: "
                     + "; ".join(f"{r.get('name') or r.get('id')}: {r['pending_actions'][0]['reason']}"
                                 for r in bad[:5]))  # fmt: skip
    if never:
        parts.append(f"{len(never)} of {len(held)} stock holding(s) never synced, so their corporate actions are "
                     "unknown")  # fmt: skip
    if stale:
        parts.append(f"{len(stale)} of {len(held)} stock holding(s) last synced more than {ACTIONS_SYNC_MAX_DAYS} "
                     f"days ago (oldest {min(r['actions_synced'] for r in stale)})")  # fmt: skip
    if clean and not parts:
        parts.append(
            f"none recorded for {len(held)} stock holding(s) (synced within {ACTIONS_SYNC_MAX_DAYS} days)"
        )
    return _row("corporate_actions", "Corporate actions synced and resolved", cov, "; ".join(parts),
                "the cost, unrealised P&L, XIRR and the tax of every sale after an action's ex-date for those holdings",
                "Open the holding: enter the cost allocation as a manual transaction, or mark the action resolved" if bad
                else "Run \"Sync corporate actions\" on the Import tab",
                "/portfolio#holdings" if bad else "/portfolio#import", applies=bool(held))  # fmt: skip


def dividends(s: Session, today: date) -> dict[str, Any]:
    """Completed FYs in which a stock was held, and whether any dividend is recorded for that FY. A year without one
    can be genuine (not every stock pays), so this reads "recorded", not "missing"."""
    stocks = {h.id for h in s.scalars(select(PortfolioHolding).where(PortfolioHolding.asset_type == "stock"))}
    spans: dict[int, list[date]] = defaultdict(list)
    paid: set[int] = set()
    open_ids = {
        h for (h,) in s.execute(select(PortfolioLot.holding_id).where(PortfolioLot.open_quantity > OPEN))
    }
    for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id.in_(stocks))):
        if t.kind == "dividend":
            paid.add(fiscal_year(t.day))
        elif t.kind in ("buy", "opening", "sell"):
            spans[t.holding_id].append(t.day)
    last_done = fiscal_year(today) - 1
    years: set[int] = set()
    for hid, days in spans.items():
        start, end = (
            fiscal_year(min(days)),
            (fiscal_year(today) if hid in open_ids else fiscal_year(max(days))),
        )
        years |= set(range(start, min(end, last_done) + 1))
    per_fy = [{"fy": y, "label": fy_label(y), "recorded": y in paid} for y in sorted(years)]
    cov = (sum(x["recorded"] for x in per_fy) * 100 / len(per_fy)) if per_fy else None
    detail = ", ".join(f"{x['label']}: {'yes' if x['recorded'] else 'none recorded'}" for x in per_fy) or \
        "no completed financial year with stock holdings"  # fmt: skip
    row = _row("dividends", "Dividends recorded per FY", cov, detail,
               "dividend income for tax, total return and the AIS dividend match",
               "Import the broker's dividend report or add the dividends by hand (a year with none may be genuine)",
               "/portfolio#import", applies=bool(per_fy))  # fmt: skip
    row["per_fy"] = per_fy
    return row


def _fund_coverage(top: dict[str, Any]) -> float | None:
    """Share of fund value that is looked through, from /api/lookthrough's `coverage` block: `pct` when given, else
    (fund % of the portfolio - % in funds not looked through) / fund % (both are % of the whole portfolio)."""
    if top.get("pct") is not None:
        return float(top["pct"])
    fund, missing = top.get("fund_pct"), top.get("not_looked_through_pct")
    if fund is None or missing is None or fund <= 0:
        return None
    return max(0.0, (float(fund) - float(missing)) / float(fund) * 100)


def lookthrough(lt: dict[str, Any] | None, has_funds: bool, error: str | None) -> dict[str, Any]:
    """Coverage as /api/lookthrough reports it: a top-level `coverage.pct` if present, else
    `concentration.fund_coverage_pct` (fund value with a month-end portfolio)."""
    cov = None
    if lt:
        top = lt.get("coverage")
        cov = _fund_coverage(top) if isinstance(top, dict) else None
        if cov is None:
            cov = (lt.get("concentration") or {}).get("fund_coverage_pct")
    detail = (f"look-through could not be computed ({error})" if error else
              "fund value covered by a month-end fund portfolio" if cov is not None else
              "the look-through reported no coverage (for example, the funds have no current value yet)")  # fmt: skip
    return _row(
        "lookthrough",
        "Fund look-through coverage",
        None if cov is None else float(cov),
        detail,
        "true stock and sector exposure through funds, and fund overlap",
        "Fetch or upload the funds' month-end portfolio files",
        "/portfolio/lookthrough",
        applies=has_funds,
    )


def ais(s: Session, today: date, has_holdings: bool) -> dict[str, Any]:
    fy = fiscal_year(today) - 1
    have = s.get(PortfolioAis, fy) is not None
    return _row("ais", f"AIS imported for {fy_label(fy)}", 100.0 if have else 0.0,
                "imported" if have else f"no AIS for {fy_label(fy)}, the last completed year",
                "the AIS check of dividends, sales and purchases before filing the ITR",
                "Download the AIS (JSON or PDF) from the income-tax portal and import it", "/portfolio#import",
                applies=has_holdings)  # fmt: skip


def history(perf: dict[str, Any] | None, error: str | None, has_holdings: bool) -> dict[str, Any]:
    days = None
    if perf is not None:
        days = ((perf.get("summary") or {}).get("days") or 0) if perf.get("available", True) else 0
    returns = max(0, days - 1) if days is not None else None
    cov = None if returns is None else min(returns, HISTORY_FULL) * 100 / HISTORY_FULL
    if returns is None:
        detail = f"the value history could not be built ({error or 'no answer'})"
    else:
        short = [f"{k} ({n})" for k, n in HISTORY_NEED.items() if returns < n]
        detail = f"{returns} daily returns" + (f"; too short for {', '.join(short)}" if short else "")
        rec = (perf or {}).get("reconciliation") or {}
        if rec.get(
            "differ"
        ):  # the "as shown" snapshots against the canonical history (portfolio.series, #239)
            detail += (f"; {len(rec['differ'])} of {rec['checked']} saved valuation day(s) differ from it by more "
                       f"than {rec['tolerance_pct']:g} % (latest {rec['differ'][-1]['day']}: "
                       f"{rec['differ'][-1]['reason']})")  # fmt: skip
    return _row("history", "Performance history length", cov, detail,
                "volatility (60 days of returns), beta and risk contribution (120), VaR and Sharpe (250)",
                "Import older transactions: the daily history is rebuilt from them", "/portfolio#import",
                applies=has_holdings)  # fmt: skip


def targets(t: dict[str, float]) -> dict[str, Any]:
    return _row("targets", "Target allocation set", 100.0 if t else 0.0, "set" if t else "no target allocation",
                "allocation drift, rebalancing suggestions and drift alerts", "Set targets on the Allocation tab",
                "/portfolio#allocation")  # fmt: skip


def goals_age(age: int | None, goals: int) -> dict[str, Any]:
    cov = (50.0 if age is not None else 0.0) + (50.0 if goals else 0.0)
    detail = f"age {'set' if age is not None else 'not set'}; {goals} goal(s)"
    return _row("goals_age", "Goals and age set", cov, detail,
                "the equity glide path, goal funding and the household checks",
                "Add your age and goals on the Wealth page", "/wealth")  # fmt: skip


def overall(rows: list[dict[str, Any]]) -> dict[str, Any]:
    use = [r for r in rows if r["status"] != "not_applicable"]
    w = sum(r["weight"] for r in use)
    pct = round(sum(r["weight"] * (r["coverage_pct"] or 0) for r in use) / w, 1) if w else None
    unknown = [r["label"] for r in use if r["status"] == "unknown"]
    if pct is None:
        verdict = "No portfolio data yet"
    elif pct >= 90:
        verdict = "Data mostly complete: the analysis is as reliable as its methods allow"
    elif pct >= 60:
        verdict = "Analysis partially reliable: some figures rest on incomplete data"
    else:
        verdict = "Analysis unreliable: key data is missing"
    if unknown:
        verdict += f" ({len(unknown)} check(s) could not run and count as 0 %)"
    return {"pct": pct, "verdict": verdict, "unknown": unknown, "how": HOW}


def compute(s: Session, snap: dict[str, Any], today: date, *, lt: dict[str, Any] | None, lt_error: str | None,
            perf: dict[str, Any] | None, perf_error: str | None) -> dict[str, Any]:  # fmt: skip
    from finresearch.db.models import WealthGoal
    from finresearch.portfolio.metrics import get_targets
    from finresearch.suggest.advisor import load_profile

    rows_in = snap.get("holdings") or []
    held = [r for r in rows_in if not r.get("closed")]
    price = {r["id"]: r.get("price") for r in rows_in if not r.get("pending")}
    lots = list(s.scalars(select(PortfolioLot).where(PortfolioLot.open_quantity > OPEN)))
    has_funds = any(r.get("asset_type") == "mf" for r in held)
    goals = s.scalar(select(func.count()).select_from(WealthGoal)) or 0
    rows = [purchase_dates(lots, price), priced(rows_in), corporate_actions(rows_in, today), dividends(s, today),
            lookthrough(lt, has_funds, lt_error), ais(s, today, bool(held)), history(perf, perf_error, bool(held)),
            targets(get_targets(s)), goals_age(load_profile(s).household.age, goals)]  # fmt: skip
    return {"as_of": today.isoformat(), "rows": rows, "overall": overall(rows),
            "privacy": "Computed on this machine from your local database; never sent to an LLM."}  # fmt: skip
