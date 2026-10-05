"""The stock page's earnings-surprise panel (#181): the latest quarters' SUE (fincalc.surprise), the latest SUE's
decile against the experiment's events, and the [0,+1] reaction versus NIFTYBEES.

Informational only. The pre-registered experiment (evals/experiments/earnings_surprise/) decides whether SUE may
enter signals/stock.py; until its results.json says it passed, every answer is labelled "experimental — not part of
the signal", and signals/stock.py does not read this module.

Per stock: the two NSE filing indexes, the XBRL of up to SHOW + 12 quarters (each cached on disk for good,
api.markets._xbrl), corporate actions, and two short price windows (the stock and NIFTYBEES) around the latest t0.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from finresearch.config import REPO_ROOT
from finresearch.fincalc import surprise as S

RESULTS = REPO_ROOT / "evals" / "experiments" / "earnings_surprise" / "results.json"
SHOW = 4  # quarters with a SUE on the panel
NEED = SHOW + 12  # SUE needs q-4 and eight differences before q (back to q-12)
BENCHMARK = "NIFTYBEES"
LABEL = "Experimental — not part of the signal"


@lru_cache(maxsize=1)
def experiment() -> dict[str, Any]:
    """The committed experiment summary (verdict and the SUE reference distribution); {} before the first run."""
    try:
        r = json.loads(RESULTS.read_text())["results"]
    except (FileNotFoundError, KeyError, ValueError):
        return {}
    return {"passes": r.get("passes", {}), "reference": r.get("reference", {}), "deciles": r.get("deciles", []),
            "primary": r.get("primary"), "tradable": r.get("tradable")}  # fmt: skip


def filing_rows(results: list[Any], integrated: list[Any]) -> list[dict[str, Any]]:
    """The adapters' typed index rows in evals.earnings_harvest's shape. An integrated filing keeps its ORIGINAL
    broadcast time (IntegratedFiling.filed_at), never the revision's."""
    out = [{"end": f.period_to, "consolidated": bool(f.consolidated), "at": f.filed_at, "xbrl": f.xbrl,
            "revised": False, "source": "nse_financial_results"} for f in results]  # fmt: skip
    out += [{"end": f.period_end, "consolidated": bool(f.consolidated), "at": f.filed_at, "xbrl": f.xbrl,
             "revised": (f.sub_type or "").lower().startswith("revis"), "source": "nse_integrated_filing"}
            for f in integrated]  # fmt: skip
    rows = [r for r in out if r["end"] and r["at"]]  # rows without an XBRL still date the announcement
    for r in rows:
        r["xbrl"] = r["xbrl"] if r["xbrl"] and r["xbrl"].endswith(".xml") else None
        # NSE gives a time of day; a bare date parses to midnight and counts as after the close
        r["has_time"] = (r["at"].hour, r["at"].minute, r["at"].second) != (0, 0, 0)
    return rows


async def _figures(eq: Any, symbol: str, errors: list[str]) -> tuple[bool, list[dict[str, Any]]]:
    """The basis and the last NEED first-reported quarters in evals.earnings_harvest's on-disk shape."""
    from finresearch.api.markets import _xbrl
    from finresearch.evals.earnings_harvest import _quarter_facts, choose_basis, first_reported

    res: list[Any] = []
    integ: list[Any] = []
    try:
        res = await eq.results(symbol, "Quarterly")
    except Exception as e:  # reported; the integrated index may still hold the recent quarters
        errors.append(f"financial results index: {type(e).__name__}: {e}"[:200])
    try:
        integ = await eq.integrated_filings(symbol)
    except Exception as e:
        errors.append(f"integrated filing index: {type(e).__name__}: {e}"[:200])
    rows = filing_rows(res, integ)
    basis = choose_basis(rows)
    out = []
    for end, f in sorted(first_reported(rows, basis).items())[-NEED:]:
        try:
            facts = _quarter_facts(await _xbrl(eq, f["xbrl"]))
        except Exception as e:  # that quarter is a gap; SUE says what it lacks
            errors.append(f"{end} XBRL: {type(e).__name__}: {e}"[:200])
            continue
        if facts is not None:
            out.append({"quarter_end": end.isoformat(), "announced": f["announced"].isoformat(),
                        "has_time": f["has_time"], "filed": f["filed"].isoformat(),
                        "revised_only": f["revised_only"], "xbrl": f["xbrl"], **facts})  # fmt: skip
    return basis, out


async def _reaction(eq: Any, symbol: str, announced: datetime, has_time: bool,
                    actions: list[tuple[date, str]]) -> dict[str, Any]:  # fmt: skip
    """t0 and the [0,+1] abnormal return vs NIFTYBEES from two short history requests."""
    from finresearch.fincalc.signals import adjust_for_actions

    day = announced.astimezone(S.IST).date()
    lo, hi = day - timedelta(days=12), day + timedelta(days=12)
    bars = await eq.history(symbol, lo, hi)
    mkt = await eq.history(BENCHMARK, lo, hi)
    stock = {b.day: float(b.close) for b in bars if b.close}
    index = {b.day: float(b.close) for b in mkt if b.close}
    sessions = sorted(set(stock) | set(index))
    t0 = S.event_session(announced, has_time, sessions)
    if stock:
        days = sorted(stock)
        adj = adjust_for_actions(days, [stock[d] for d in days], actions)
        stock = dict(zip(adj.days, adj.close, strict=True))
    r = None if t0 is None else S.abnormal_return(stock, index, sessions, t0, 0, 1)
    return {"t0": t0.isoformat() if t0 else None, "reaction": r,
            "reaction_note": None if r is not None else "the session after t0 has not closed yet, or a close is missing"}  # fmt: skip


def _decile(sue: float | None, ref: dict[str, Any]) -> int | None:
    """Decile 1..10 of `sue` against the experiment's cut-offs (nine values: the 10th..90th percentiles)."""
    import bisect

    cut = ref.get("cutoffs") or []
    return None if sue is None or len(cut) != 9 else bisect.bisect_right(cut, sue) + 1


async def live(eq: Any, symbol: str) -> dict[str, Any]:
    """The panel for one NSE stock. Figures as first reported, on one basis; SUE per fincalc.surprise."""
    from finresearch.evals.earnings_surprise import quarters as split_quarters
    from finresearch.fincalc.dates import fiscal_quarter_label

    errors: list[str] = []
    basis, qs = await _figures(eq, symbol, errors)
    try:
        acts = [(a.ex_date, a.subject) for a in await eq.corporate_actions(symbol) if a.ex_date]
    except Exception as e:  # without them a split inside the window would read as a surprise: say so
        acts = []
        errors.append(
            f"corporate actions (split/bonus adjustment not applied): {type(e).__name__}: {e}"[:200]
        )
    d = {"quarters": qs}
    eps_q, info = split_quarters(d, "eps")
    rev_q, _ = split_quarters(d, "revenue")
    eps_by = {q.end: q.value for q in eps_q}
    rows = []
    for end in sorted(info)[-SHOW:]:
        v, why = S.sue(eps_q, end, acts)
        rv, _ = S.sue(rev_q, end, per_share=False)
        inf = info[end]
        rows.append({"quarter_end": end.isoformat(), "label": fiscal_quarter_label(end),
                     "announced": inf["announced"].isoformat(), "announcement_plausible": inf["plausible"],
                     "eps": eps_by.get(end), "eps_year_ago": eps_by.get(S.quarter_back(end, 4)),
                     "sue": v, "sue_reason": why, "revenue_sue": rv,
                     "decile": _decile(v, experiment().get("reference", {}))})  # fmt: skip
    latest = rows[-1] if rows else None
    if latest and latest["announcement_plausible"]:
        inf = info[date.fromisoformat(latest["quarter_end"])]
        try:
            latest.update(await _reaction(eq, symbol, inf["announced"], inf["has_time"], acts))
        except Exception as e:
            errors.append(f"price reaction: {type(e).__name__}: {e}"[:200])
    exp = experiment()
    promoted = bool(exp.get("passes", {}).get("promote"))
    return {"status": "ok" if rows else "no_data", "symbol": symbol, "experimental": not promoted,
            "label": None if promoted else LABEL, "basis": "consolidated" if basis else "standalone",
            "quarters": rows, "latest": latest, "errors": errors,
            "reference": {**exp.get("reference", {}), "source": "evals/experiments/earnings_surprise/RESULTS.md"},
            "experiment": {k: exp.get(k) for k in ("passes", "primary", "tradable")},
            "method": "SUE = (EPS_q - EPS_q-4) / s.d. of that difference over the 8 quarters before q (at least 6); "
                      "EPS as first reported, split/bonus adjusted; reaction = stock minus NIFTYBEES return from the "
                      "close before t0 to the close of t0+1 (t0 = next session when results came after 15:30 IST)",
            "benchmark": BENCHMARK}  # fmt: skip
