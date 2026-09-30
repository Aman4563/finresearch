"""Report "Accuracy checks" and "Valuation triangulation" blocks, derived deterministically from the claim ledger.

* `accuracy_block` — the accounting-identity / scale checks of `verify.identities` for the report reader, with the
  claims involved and whether the report cites them.
* `triangulation_block` — reverse DCF, a DCF sensitivity grid, a seeded Monte Carlo fair-value distribution, the
  peer-multiple percentile and the triangulated band (roadmap §C.7; fincalc.valuation / fincalc.montecarlo).

Honesty rules: every input is a ledger claim (its id and status travel with it) or an explicitly labelled
ASSUMPTION with its rationale; nothing is estimated to fill a gap. When the ledger lacks what a method needs, the
method is left out and the reason is listed under `missing` ("not enough inputs"). No model is involved.
"""

from __future__ import annotations

import re
import statistics
from decimal import Decimal
from typing import Any

from finresearch.fincalc import montecarlo, valuation
from finresearch.verify.identities import Fact, check_claims, facts_from

YEARS = 10
SEED = 20260930
TERMINAL = montecarlo.Range(0.03, 0.04, 0.05, "ASSUMPTION: long-run nominal growth 3–5 %, below India's nominal GDP "
                                              "growth; a terminal rate above the economy's is not sustainable")  # fmt: skip
DEFAULT_COE = montecarlo.Range(0.12, 0.13, 0.14, "ASSUMPTION (no discount rate in the ledger): India 10-year G-sec "
                                                 "≈ 6.5–7.2 % + equity risk premium ≈ 5–7 %")  # fmt: skip
ERP = (0.05, 0.06, 0.07)


# --------------------------------------------------------------------------- accuracy checks
def accuracy_block(claims: list[dict[str, Any]], cited: set[int]) -> dict[str, Any]:
    rep = check_claims(claims).to_dict()
    for chk in rep["checks"]:
        chk["cited"] = [i for i in chk["claim_ids"] if i in cited]
        chk.pop("id", None)
    return rep


# --------------------------------------------------------------------------- ledger inputs
def _rank(f: Fact) -> tuple[int, int, int]:
    return (0 if f.status == "verified" else 1, 0 if f.importance == "high" else 1, -f.id)


def _metric(
    facts: list[Fact], pattern: str, kind: str | tuple[str, ...], *, period: str | None = None
) -> list[Fact]:
    rx = re.compile(pattern)
    kinds = (kind,) if isinstance(kind, str) else kind
    out = [f for f in facts if rx.search(f.metric.lower()) and f.kind in kinds
           and (f.currency in (None, "INR"))
           and (period is None or re.search(period, f.period_raw or "", re.I))]  # fmt: skip
    return sorted(out, key=_rank)


def _period_sort(f: Fact) -> tuple[int, int]:
    m = re.fullmatch(r"(?:Q([1-4]))?FY(\d{4})", f.period or "")
    if not m:
        return (0, 0)
    return (int(m.group(2)), int(m.group(1) or 5) if m.group(1) else 5)


def _latest_annual(facts: list[Fact], keys: tuple[str, ...]) -> Fact | None:
    c = [f for f in facts if f.key in keys and f.kind == "money" and f.currency == "INR" and (f.period or "")
         .startswith("FY") and f.basis != "standalone"]  # fmt: skip
    return max(c, key=lambda f: (_period_sort(f), -_rank(f)[0], -_rank(f)[1])) if c else None


def _src(f: Fact) -> dict[str, Any]:
    return {"claim_id": f.id, "status": f.status, "metric": f.metric, "period": f.period_raw}


def _inr(x: float) -> str:
    a = abs(x)
    if a >= 1e7:
        return f"₹{x / 1e7:,.2f} Cr"
    return f"₹{x:,.2f}"


def _input(name: str, value: float | None, display: str, source: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(source, str):
        return {
            "name": name,
            "value": value,
            "display": display,
            "source": source,
            "claim_id": None,
            "status": None,
        }
    return {"name": name, "value": value, "display": display, "source": f"C{source['claim_id']}",
            "claim_id": source["claim_id"], "status": source["status"]}  # fmt: skip


def _history(facts: list[Fact]) -> list[dict[str, Any]]:
    out = []
    seen = set()
    for f in sorted(facts, key=_rank):
        m = f.metric.lower()
        if f.kind != "pct" or not re.match(r"^(revenue|pat|eps|net_profit)\w*_(cagr|growth)", m):
            continue
        if re.search(r"_cc|usd|guidance|segment|qoq", m) or f.basis == "standalone":
            continue
        key = (m.split("_")[0], f.period_raw)
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": f"{f.metric} ({f.period_raw})", "value": float(f.value), "claim_ids": [f.id],
                    "status": f.status})  # fmt: skip
    return out[:8]


def valuation_inputs(claims: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Pick the triangulation inputs out of the ledger; every choice carries its claim id or is an assumption."""
    facts, _ = facts_from(claims)
    missing: list[str] = []
    notes: list[str] = []
    ipo = kind == "ipo_report"
    price_f = (_metric(facts, r"^(price_band_upper|cap_price|issue_price_upper|issue_price)$", "per_share") if ipo
               else _metric(facts, r"^(last_price|close_price|current_price|share_price|cmp|ltp|market_price)$",
                            ("per_share", "money")))  # fmt: skip
    if not ipo:  # the newest market price
        price_f = sorted(price_f, key=lambda f: (f.period_raw or "", -_rank(f)[0]), reverse=True)
    shares_f = (_metric(facts, r"^(post_issue_shares|post_offer_shares\w*)$", "shares", period=r"cap|upper")
                or _metric(facts, r"^(post_issue_shares|post_offer_shares\w*)$", "shares") if ipo
                else _metric(facts, r"^(shares_outstanding|current_shares_outstanding|total_shares_outstanding)$",
                             "shares"))  # fmt: skip
    fcf = _latest_annual(facts, ("fcf",))
    pat = _latest_annual(facts, ("pat_owners", "pat"))
    out: dict[str, Any] = {"inputs": [], "missing": missing, "notes": notes, "kind": kind}
    price = float(price_f[0].value) if price_f else None
    shares = float(shares_f[0].value) if shares_f else None
    if price_f:
        out["inputs"].append(_input("Price" + (" (cap of the band)" if ipo else ""), price, f"₹{price:,.2f}",
                                    _src(price_f[0])))  # fmt: skip
    else:
        missing.append("a price (cap price or market price)")
    if shares_f:
        out["inputs"].append(_input("Shares" + (" (post-issue)" if ipo else " outstanding"), shares,
                                    f"{shares:,.0f}", _src(shares_f[0])))  # fmt: skip
    else:
        missing.append("the share count" + (" after the issue" if ipo else ""))
    cf = None
    fcf_note = None
    if (
        fcf is None
    ):  # derive CFO − capex for the same year when both are in the ledger (labelled, never silent)
        cfo = _latest_annual(facts, ("cfo",))
        capex = [f for f in facts if f.metric.lower() in ("capex", "capital_expenditure") and cfo is not None
                 and f.period == cfo.period and f.kind == "money" and f.currency == "INR"]  # fmt: skip
        if cfo is not None and capex:
            v = float(cfo.value - abs(capex[0].value))
            fcf_note = f"CFO C{cfo.id} − capex C{capex[0].id} = {_inr(v)} ({cfo.period_raw})"
            if v > 0:
                basis = "FCF (CFO − capex)"
                cf = Fact(**{**cfo.__dict__, "value": Decimal(str(v)), "metric": "cfo_minus_capex"})
    if cf is None and fcf is not None and fcf.value > 0:
        cf, basis = fcf, "FCF"
    elif cf is None and pat is not None and pat.value > 0:
        cf, basis = pat, "PAT (earnings proxy)"
        why = (
            f"Free cash flow is negative ({_inr(float(fcf.value))}, C{fcf.id}); "
            if fcf is not None
            else f"Free cash flow is negative ({fcf_note}); "
            if fcf_note
            else "No free cash flow in the ledger; "
        )
        notes.append(
            why + "earnings stand in for cash flow, which overstates value while heavy capex continues."
        )
    elif cf is None:
        basis = None
        missing.append("a positive cash flow or profit for the latest year")
    if cf is not None:
        out["inputs"].append(
            _input(f"Base cash flow: {basis}", float(cf.value), _inr(float(cf.value)), _src(cf))
        )
    out["cash_flow_basis"] = basis
    # net debt: most recent explicit figure; else borrowings − cash; never guessed
    nd = sorted(
        _metric(facts, r"^net_debt$", "money"), key=lambda f: (_period_sort(f), -_rank(f)[0]), reverse=True
    )
    nc = sorted(_metric(facts, r"^(net_cash|net_cash_position)$", "money"), key=lambda f: (_period_sort(f),),
                reverse=True)  # fmt: skip
    nd = [f for f in nd if not re.search(r"pro ?forma|post|estimate", f.period_raw or "", re.I)]
    net_debt = 0.0
    if nd:
        net_debt = float(nd[0].value)
        out["inputs"].append(_input("Net debt", net_debt, _inr(net_debt), _src(nd[0])))
    elif nc:
        net_debt = -float(nc[0].value)
        out["inputs"].append(_input("Net cash (negative net debt)", net_debt, _inr(net_debt), _src(nc[0])))
    else:
        notes.append(
            "No net debt / net cash in the ledger: the DCF value is used as equity value unadjusted."
        )
    out["net_debt"] = net_debt
    proceeds = 0.0
    if ipo:
        fi = _metric(facts, r"^(fresh_issue_amount|fresh_issue_size|fresh_issue)$", "money")
        if fi:
            proceeds = float(fi[0].value)
            out["inputs"].append(_input("Fresh-issue proceeds (into the company)", proceeds, _inr(proceeds),
                                        _src(fi[0])))  # fmt: skip
        else:
            notes.append(
                "No fresh-issue amount in the ledger: IPO proceeds are not added to the equity bridge."
            )
    out["fresh_issue_proceeds"] = proceeds
    # discount rate: a ledger rate, else a ledger risk-free rate + ERP, else the documented default assumption
    coe = _metric(facts, r"^(cost_of_equity\w*|wacc\w*|discount_rate\w*|required_return\w*)$", "pct")
    rf = _metric(
        facts, r"(g_?sec|gsec|government_bond|risk_free)\w*(10y|10_year|10yr)|10y\w*(g_?sec|yield)", "pct"
    )
    if coe:
        v = float(coe[0].value)
        disc = montecarlo.Range(v - 0.01, v, v + 0.01, f"C{coe[0].id} ± 1 pp")
        out["inputs"].append(_input("Discount rate", v, f"{v * 100:.2f}% (± 1 pp)", _src(coe[0])))
    elif rf:
        v = float(rf[0].value)
        disc = montecarlo.Range(
            v + ERP[0], v + ERP[1], v + ERP[2], f"C{rf[0].id} + ASSUMED equity risk premium 5–7 %"
        )
        out["inputs"].append(_input("Discount rate", v + ERP[1], f"{(v + ERP[1]) * 100:.2f}% (risk-free C{rf[0].id} "
                                    "+ assumed ERP 5–7 %)", _src(rf[0])))  # fmt: skip
    else:
        disc = DEFAULT_COE
        out["inputs"].append(
            _input("Discount rate", DEFAULT_COE.mode, "12–14 % (mode 13 %)", DEFAULT_COE.source)
        )
    out["inputs"].append(_input("Terminal growth", TERMINAL.mode, "3–5 % (mode 4 %)", TERMINAL.source))
    # a growth claim that fails its own recomputation (verify/identities) is not used as history
    failing = {i for c in check_claims(claims).failing() if c.family == "growth" for i in c.claim_ids[:1]}
    hist = [h for h in _history(facts) if not set(h["claim_ids"]) & failing]
    out["history"] = hist
    if hist:
        h = statistics.median(x["value"] for x in hist)
        h = max(-0.10, min(0.30, h))
        if h > 0:
            growth = montecarlo.Range(0.5 * h, 0.75 * h, h, "ASSUMPTION: history fades — the ledger's median "
                                      f"historical growth {h * 100:.1f}% is the optimistic end, half of it the "
                                      "pessimistic end (base rates: high growth regresses to the mean)")  # fmt: skip
        else:
            growth = montecarlo.Range(-0.05, 0.0, 0.05, "ASSUMPTION: history shows no growth; −5 %…+5 %")
        out["inputs"].append(_input("Growth, next 10 years", growth.mode, f"{growth.low * 100:.1f}–"
                                    f"{growth.high * 100:.1f} % (mode {growth.mode * 100:.1f} %)", growth.source))  # fmt: skip
    else:
        growth = None
        missing.append("historical growth (a revenue / PAT / EPS growth or CAGR claim)")
    out.update(price=price, shares=shares, cash_flow=float(cf.value) if cf is not None else None,
               discount=disc, growth=growth, price_claim=price_f[0].id if price_f else None)  # fmt: skip
    out["_facts"] = facts
    return out


# --------------------------------------------------------------------------- peers
def _peers(facts: list[Fact], ipo: bool) -> dict[str, Any] | None:
    peers = [f for f in facts if re.fullmatch(r"peer_pe(_ttm)?", f.metric.lower()) and f.kind == "multiple"]
    own_c = [f for f in facts if f.kind == "multiple" and re.fullmatch(
        r"(pe_post_issue_diluted|pe_basic\w*|pe_ratio_upper_band|pe_ratio|trailing_pe_reported|trailing_pe|pe)",
        f.metric.lower())]  # fmt: skip
    if ipo:
        own_c = [f for f in own_c if re.search(r"cap|upper", f"{f.metric} {f.period_raw}", re.I)] or own_c
    own_c.sort(key=lambda f: (0 if "post_issue" in f.metric else 1, _rank(f)))
    if len(peers) < 3 or not own_c:
        return None
    own = own_c[0]
    try:
        st = valuation.peer_stats([f.value for f in peers], own.value)
    except ValueError:
        return None
    return {"metric": "P/E", "n": st.n, "q1": float(st.q1), "median": float(st.median), "q3": float(st.q3),
            "own": float(own.value), "own_claim": own.id, "own_label": f"{own.metric} ({own.period_raw})",
            "percentile": float(st.percentile) if st.percentile is not None else None,
            "claim_ids": [f.id for f in peers], "values": sorted(float(f.value) for f in peers if f.value > 0)}  # fmt: skip


# --------------------------------------------------------------------------- the block
def triangulation_block(claims: list[dict[str, Any]], kind: str, fair: list[dict[str, Any]] | None = None
                        ) -> dict[str, Any] | None:  # fmt: skip
    """None for kinds with no equity valuation (funds, bonds)."""
    if kind not in ("ipo_report", "stock_report"):
        return None
    vi = valuation_inputs(claims, kind)
    facts = vi.pop("_facts")
    price, shares, cf = vi["price"], vi["shares"], vi["cash_flow"]
    disc, growth = vi.pop("discount"), vi.pop("growth")
    out: dict[str, Any] = {"unit": "₹/share", "years": YEARS, "seed": SEED, **{k: vi[k] for k in (
        "inputs", "missing", "notes", "cash_flow_basis", "history", "price", "price_claim")}}  # fmt: skip
    common = {"years": YEARS, "net_debt": vi["net_debt"], "fresh_issue_proceeds": vi["fresh_issue_proceeds"]}
    methods: dict[str, tuple[float, float]] = {}
    if price and shares and cf:
        g_star = valuation.reverse_dcf(price, shares, cf, disc.mode, TERMINAL.mode, **common)
        out["reverse_dcf"] = {
            "implied_growth": float(g_star) if g_star is not None else None,
            "discount_rate": disc.mode, "terminal_growth": TERMINAL.mode, "years": YEARS,
            "note": None if g_star is not None else "no growth rate between −50 % and +100 % a year reproduces "
                                                    "the price with these inputs",
        }  # fmt: skip
    else:
        out["reverse_dcf"] = None
    if price and shares and cf and growth:
        mc = montecarlo.simulate_dcf(cf, shares, growth, disc, TERMINAL, price=price, seed=SEED, **common)
        out["monte_carlo"] = {"p5": mc.p5, "p25": mc.p25, "p50": mc.p50, "p75": mc.p75, "p95": mc.p95,
                              "mean": mc.mean, "prob_above_price": mc.prob_above_price, "n": mc.n,
                              "rejected": mc.rejected, "histogram": mc.histogram,
                              "ranges": {"growth": growth.__dict__, "discount_rate": disc.__dict__,
                                         "terminal_growth": TERMINAL.__dict__}}  # fmt: skip
        rates = [round(disc.mode + d, 4) for d in (-0.01, -0.005, 0, 0.005, 0.01)]
        tgs = [round(TERMINAL.mode + d, 4) for d in (-0.01, 0, 0.01)]
        grid = valuation.dcf_grid(cf, growth.mode, shares, rates, tgs, **common)
        out["grid"] = {"growth": growth.mode, "discount_rates": rates, "terminal_growths": tgs,
                       "values": [[float(v) if v is not None else None for v in row] for row in grid]}  # fmt: skip
        methods["DCF (Monte Carlo P25–P75)"] = (mc.p25, mc.p75)
    else:
        out["monte_carlo"] = None
        out["grid"] = None
    peers = _peers(facts, kind == "ipo_report")
    if peers and price:
        # the price scaled by (peer multiple ÷ own multiple): consistent with whichever EPS the own multiple used
        k = price / peers["own"]
        peers["implied"] = {"low": peers["q1"] * k, "mid": peers["median"] * k, "high": peers["q3"] * k}
        methods["Peer P/E (IQR)"] = (peers["implied"]["low"], peers["implied"]["high"])
    elif not peers:
        out["missing"].append("≥ 3 peer P/E multiples and the company's own P/E")
    out["peers"] = peers
    groups: dict[str, dict[str, float]] = {}
    for f in fair or []:
        # fair-value ranges only: an entry zone / max buy price is a buying rule, not a value estimate
        if (f.get("unit") in ("₹/share", "₹") and f.get("role") in ("low", "high")
                and str(f.get("group", "")).startswith("fair_value")):  # fmt: skip
            groups.setdefault(f["label"], {})[f["role"]] = f["value"]
    for label, g in groups.items():
        if "low" in g and "high" in g:
            methods[f"Report: {label}"] = (g["low"], g["high"])
    if methods:
        b = valuation.triangulate(methods)
        out["band"] = {"low": float(b.low), "high": float(b.high),
                       "intersection": [float(x) for x in b.intersection] if b.intersection else None,
                       "disagree": b.disagree, "spread": float(b.spread),
                       "methods": [{"name": k, "low": float(v[0]), "high": float(v[1])} for k, v in methods.items()]}  # fmt: skip
    else:
        out["band"] = None
    out["status"] = (
        "ok" if (out["monte_carlo"] or out["reverse_dcf"] or out["peers"]) else "not_enough_inputs"
    )
    return out
