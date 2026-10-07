"""Chart-ready, cited insights for a research run, derived deterministically from the claim ledger.

No model is involved: every number here is a ledger claim (verified or needs_review, never contradicted,
unsupported or superseded by a correction) and carries its claim id and status, so the dashboard can show where it
came from. Structured parts of the synthesis step (reasons for/against, scenarios, checklist) and the report's own
ranked-risk list are passed through with their citations. Anything the ledger does not hold is left out, never
estimated. The only arithmetic is display arithmetic done with Decimal: `fraction` units shown as %, and
year-on-year changes via `fincalc.growth.pct_change`, each labelled with the claims it was computed from.

`build_insights` is a pure function over plain dicts (the API serialisation of claims), so it is tested offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from finresearch.fincalc.growth import pct_change

USABLE = ("verified", "needs_review")
STATUS_RANK = {"verified": 0, "needs_review": 1}
CITE_ANY = re.compile(r"\[C(\d+)\]|\(C(\d+)\)|(?<![\w\[])C(\d{2,})(?![\w\]])")

# the stream(s) whose claims make up the company's own financial statements, per research kind
FINANCIAL_STREAMS = {
    "ipo_report": ("financials", "valuation", "business", "risks"),
    "stock_report": ("stock_fundamentals", "stock_valuation"),
    "fund_report": (),
    "bond_report": ("bond_issuer", "bond_news"),
}
PRIMARY_STREAM = {
    "ipo_report": "financials",
    "stock_report": "stock_fundamentals",
    "bond_report": "bond_issuer",
}


# --------------------------------------------------------------------------- units and numbers
def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def norm_unit(unit: str | None) -> tuple[str, Decimal] | None:
    """(display unit, factor) for a ledger unit; None when the unit is unknown (the point is then left out).

    Rupee scales are never converted between each other (a point must equal the value its claim cites); only
    `fraction` is shown as % (x100), which is exact."""
    if not unit:
        return None
    u = re.sub(r"\s*\(.*?\)\s*", " ", unit).strip().lower()
    table = {
        "inr crore": "₹ Cr", "inr cr": "₹ Cr", "₹ crore": "₹ Cr", "rs crore": "₹ Cr",
        "inr million": "₹ Mn", "inr mn": "₹ Mn", "₹ million": "₹ Mn", "rs million": "₹ Mn",
        "inr lakh": "₹ L", "inr billion": "₹ Bn", "inr": "₹", "₹": "₹", "rs": "₹",
        "inr per share": "₹/share", "inr/share": "₹/share", "₹ per share": "₹/share", "₹/share": "₹/share",
        "inr per unit": "₹/unit", "%": "%", "percent": "%", "x": "x", "times": "x", "ratio": "x",
        "days": "days", "usd million": "US$ Mn", "usd billion": "US$ Bn", "usd": "US$", "shares": "shares",
        "years": "years", "count": "count", "payments/year": "payments/yr",
    }  # fmt: skip
    if u == "fraction":
        return "%", Decimal(100)
    label = table.get(u)
    return (label, Decimal(1)) if label else None


def fmt_num(d: Decimal, places: int = 2) -> str:
    """Indian digit grouping (12,34,567.89), trailing zeros trimmed."""
    neg = d < 0
    q = abs(d).quantize(Decimal(1).scaleb(-places)) if places else abs(d).quantize(Decimal(1))
    s = f"{q:f}"
    whole, _, frac = s.partition(".")
    frac = frac.rstrip("0")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join([*groups, tail])
    return ("-" if neg else "") + whole + (f".{frac}" if frac else "")


def fmt_value(d: Decimal, unit: str) -> str:
    n = fmt_num(d, 3 if unit == "x" and 0 < abs(d) < 1 else 2)  # 0.057x subscribed must not read as 0.06x
    if unit == "%":
        return f"{n}%"
    if unit == "x":
        return f"{n}x"
    if unit == "₹":
        return f"₹{n}"
    if unit.startswith("₹ "):
        return f"₹{n} {unit[2:]}"
    if unit.startswith("₹/"):
        return f"₹{n}"  # the unit (per share / per unit) is shown next to the value
    if unit.startswith("US$"):
        return f"US${n}{unit[3:]}"
    return f"{n} {unit}"


# --------------------------------------------------------------------------- periods
_FY = r"FY\s?'?(\d{4}|\d{2})"
_REJECT = re.compile(r"\bTTM\b|\bLTM\b|\bvs\b|standalone|guidance|forward|\bFY\s?\d{2,4}E\b|"
                     rf"{_FY}\s*(?:-|–|to)\s*(?:{_FY}|\d{{2,4}})|H[12]\s?FY|9M\s?FY|pro ?forma|estimate|post-", re.I)  # fmt: skip


@dataclass(frozen=True)
class Period:
    label: str  # "FY26" | "Q1 FY27"
    freq: str  # "annual" | "quarterly"
    sort: int  # year*10 + quarter (annual = year*10 + 5)


def _year(tok: str) -> int:
    y = int(tok)
    return y + 2000 if y < 100 else y


def parse_period(period: str | None) -> Period | None:
    """A fiscal year or quarter from a ledger period string; None for ranges, TTM, dates and anything unclear."""
    if not period or _REJECT.search(period):
        return None
    q = re.search(rf"\bQ([1-4])\s?{_FY}\b", period, re.I)
    if q:
        y = _year(q.group(2))
        return Period(f"Q{q.group(1)} FY{y % 100:02d}", "quarterly", y * 10 + int(q.group(1)))
    if re.search(r"\bQ[1-4]\b", period):
        return None  # "FY2025 Q1" style: ambiguous, leave it out
    fy = re.findall(rf"\b{_FY}\b", period, re.I)
    if len(fy) == 1:
        y = _year(fy[0])
        return Period(f"FY{y % 100:02d}", "annual", y * 10 + 5)
    return None


# --------------------------------------------------------------------------- claims
@dataclass
class C:
    id: int
    stream: str
    metric: str
    value: Decimal | None
    unit: str | None
    period: str | None
    status: str
    importance: str
    statement: str
    claim_type: str
    corrects: int | None

    @property
    def rank(self) -> tuple[int, int, int]:
        # verified first, high importance first, then the newest claim (later passes and corrections)
        return (STATUS_RANK.get(self.status, 9), 0 if self.importance == "high" else 1, -self.id)


def _claims(raw: list[dict[str, Any]]) -> tuple[list[C], dict[int, C]]:
    all_ = [C(id=int(x["id"]), stream=x.get("stream") or "", metric=(x.get("metric") or "").strip(),
              value=_dec(x.get("value")), unit=x.get("unit"), period=x.get("period"), status=x.get("status") or "",
              importance=x.get("importance") or "normal", statement=x.get("statement") or "",
              claim_type=x.get("claim_type") or "", corrects=x.get("corrects_claim_id"))
            for x in raw]  # fmt: skip
    by_id = {c.id: c for c in all_}
    superseded = {c.corrects for c in all_ if c.corrects and c.status in USABLE}
    usable = [c for c in all_ if c.status in USABLE and c.id not in superseded]
    return usable, by_id


def point(c: C, unit: tuple[str, Decimal] | None = None) -> dict[str, Any] | None:
    """A cited value: display value (unit-normalised), the raw ledger value, claim id and status."""
    if c.value is None:
        return None
    u = unit or norm_unit(c.unit)
    if u is None:
        return None
    v = c.value * u[1]
    return {"value": float(v), "display": fmt_value(v, u[0]), "unit": u[0], "raw": f"{c.value.normalize():f}",
            "raw_unit": c.unit, "period": c.period, "claim_id": c.id, "status": c.status, "metric": c.metric}  # fmt: skip


def _best(cands: list[C]) -> C | None:
    return min(cands, key=lambda c: c.rank) if cands else None


def _find(
    claims: list[C], pattern: str, *, streams: tuple[str, ...] | None = None, numeric: bool = True
) -> list[C]:
    rx = re.compile(pattern)
    return [c for c in claims if rx.search(c.metric) and (streams is None or c.stream in streams)
            and (c.value is not None or not numeric)]  # fmt: skip


def pick(claims: list[C], pattern: str, **kw: Any) -> dict[str, Any] | None:
    """The best usable claim for a metric pattern, as a point."""
    for c in sorted(_find(claims, pattern, **kw), key=lambda c: c.rank):
        p = point(c)
        if p:
            return p
    return None


def humanise(metric: str) -> str:
    special = {"pe": "P/E", "pb": "P/B"}
    acr = {"eps", "pat", "pbt", "ebitda", "ebit", "roe", "roce", "roa", "cfo", "cfi", "cff", "fcf", "nav", "ter",
           "aum", "ytm", "gnpa", "crar", "ttm", "yoy", "qoq", "cc", "dso", "gfa", "esop", "ofs", "ipo", "sip", "kmp",
           "ev", "nii", "qib", "bnii", "snii", "rii", "gsec", "sbi", "fd", "rbi", "wc", "ai", "tcv",
           "usd", "inr", "ltm", "caro", "mf", "amc", "cagr", "dps", "bvps", "npa"}  # fmt: skip
    words = [
        special.get(w) or (w.upper() if w in acr else w) for w in metric.replace("-", "_").split("_") if w
    ]
    s = " ".join(words)
    return s[:1].upper() + s[1:]


# --------------------------------------------------------------------------- financial series
# (key, label, group, metric aliases in preference order); the first alias present in a period wins
FAMILIES: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("revenue", "Revenue from operations", "income",
     ("revenue_from_operations", "revenue", "total_revenue", "net_sales", "revenue_ops")),
    ("total_income", "Total income", "income", ("total_income",)),
    ("ebitda", "EBITDA", "income", ("ebitda",)),
    ("ebit", "Operating profit (EBIT)", "income", ("ebit", "operating_profit")),
    ("pbt", "Profit before tax", "income", ("pbt", "profit_before_tax")),
    ("pat", "Net profit (PAT)", "income",
     ("pat", "profit_attributable_to_owners", "net_profit", "profit_after_tax", "profit_for_the_year",
      "consolidated_pat")),
    ("eps", "Earnings per share (basic)", "per_share", ("basic_eps", "eps", "basic_diluted_eps")),
    ("ebitda_margin", "EBITDA margin", "margin", ("ebitda_margin",)),
    ("operating_margin", "Operating margin", "margin", ("operating_margin", "ebit_margin")),
    ("pat_margin", "Net profit margin", "margin", ("pat_margin", "net_margin", "net_profit_margin")),
    ("gross_margin", "Gross margin", "margin", ("gross_margin",)),
    ("roe", "Return on equity (ROE)", "returns",
     ("roe", "roe_closing_equity", "return_on_net_worth", "ronw", "return_on_equity")),
    ("roce", "Return on capital employed (ROCE)", "returns", ("roce", "roce_gross", "return_on_capital_employed")),
    ("roa", "Return on assets (ROA)", "returns", ("roa", "roa_pct")),
    ("borrowings", "Total borrowings", "balance", ("total_borrowings", "borrowings", "total_debt", "gross_debt")),
    ("net_debt", "Net debt", "balance", ("net_debt",)),
    ("equity", "Net worth (equity)", "balance", ("total_equity", "net_worth", "shareholders_equity")),
    ("debt_to_equity", "Debt / equity", "balance", ("debt_to_equity",)),
    ("net_debt_to_equity", "Net debt / equity", "balance", ("net_debt_to_equity",)),
    ("net_debt_to_ebitda", "Net debt / EBITDA", "balance", ("net_debt_to_ebitda",)),
    ("loan_book", "Loan book", "balance", ("consolidated_loan_book", "loan_book", "gross_loan_book")),
    ("gross_stage3", "Gross stage 3 (bad loans)", "returns", ("gross_stage3_pct", "gnpa_pct", "gnpa")),
    ("crar", "Capital adequacy (CRAR)", "returns", ("crar_pct", "crar")),
    ("cfo", "Cash from operations", "cash",
     ("cfo", "cash_from_operations", "net_cash_from_operating_activities", "operating_cash_flow")),
    ("fcf", "Free cash flow", "cash", ("fcf", "free_cash_flow")),
    ("capex", "Capital expenditure", "cash", ("capex",)),
]  # fmt: skip
FAMILY_OF = {m: f for f in FAMILIES for m in f[3]}


def financial_series(claims: list[C], streams: tuple[str, ...], primary: str | None) -> list[dict[str, Any]]:
    """Metric families (and any other metric with 2+ periods) as annual / quarterly series, one per unit."""
    cells: dict[tuple[str, str, str], dict[str, tuple[tuple, C, Period]]] = {}
    for c in claims:
        if c.stream not in streams or c.value is None or c.claim_type == "factual":
            continue
        per = parse_period(c.period)
        u = norm_unit(c.unit)
        if per is None or u is None:
            continue
        fam = FAMILY_OF.get(c.metric)
        if fam and fam[2] == "per_share" and u[0] == "₹":
            u = ("₹/share", u[1])  # "INR" on an EPS claim is per share
        key = fam[0] if fam else c.metric
        alias_rank = fam[3].index(c.metric) if fam else 0
        rank = (alias_rank, 0 if c.stream == primary else 1, *c.rank)
        slot = cells.setdefault((key, per.freq, u[0]), {})
        if per.label not in slot or rank < slot[per.label][0]:
            slot[per.label] = (rank, c, per)
    out = []
    fam_order = {f[0]: i for i, f in enumerate(FAMILIES)}
    for (key, freq, unit), slot in cells.items():
        fam = next((f for f in FAMILIES if f[0] == key), None)
        if fam is None and len(slot) < 2:
            continue  # a stray one-off metric is not a series
        pts = []
        for _rank, c, per in sorted(slot.values(), key=lambda t: t[2].sort):
            p = point(c, (unit, norm_unit(c.unit)[1]))  # type: ignore[index]
            if p:
                pts.append({**p, "period": per.label, "period_raw": c.period})
        out.append({"key": key, "label": fam[1] if fam else humanise(key), "group": fam[2] if fam else "other",
                    "freq": freq, "unit": unit, "points": pts, "family": fam is not None})  # fmt: skip
    # families first (in their order), INR before other currencies, annual before quarterly, longer series first
    out.sort(key=lambda s: (fam_order.get(s["key"], 999), s["unit"].startswith("US$"), s["freq"] != "annual",
                            -len(s["points"]), s["key"]))  # fmt: skip
    return out


def _main_series(series: list[dict[str, Any]], key: str, freq: str = "annual") -> dict[str, Any] | None:
    return next((s for s in series if s["key"] == key and s["freq"] == freq and not s["unit"].startswith("US$")),
                None)  # fmt: skip


def _delta(series: dict[str, Any] | None) -> dict[str, Any] | None:
    """Change between the last two annual points (fincalc pct_change), cited to both claims."""
    if not series or len(series["points"]) < 2:
        return None
    a, b = series["points"][-2], series["points"][-1]
    ch = pct_change(Decimal(a["raw"]), Decimal(b["raw"]))
    if ch is None:
        return None
    return {"pct": float((ch * 100).quantize(Decimal("0.01"))), "vs": a["period"], "claim_ids": [a["claim_id"],
            b["claim_id"]]}  # fmt: skip


# --------------------------------------------------------------------------- valuation and peers
MULTIPLES: list[tuple[str, str, str]] = [
    # P/E aliases in preference order: the report's headline (trailing reported / pre-issue basic) first
    (
        "pe",
        "P/E",
        r"^(trailing_pe_reported|trailing_pe\w*|pe_basic\w*|pe|pe_ratio|pe_ttm|pe_post_issue\w*|pe_ratio_upper_band)$",
    ),
    ("pb", "P/B", r"^(pb|pb_fy\d+|price_to_book)$"),
    ("ev_ebitda", "EV/EBITDA", r"^ev_ebitda(_fy\d+)?$"),
    ("ev_ebit", "EV/EBIT", r"^ev_ebit$"),
    ("ps", "Price / sales", r"^price_to_sales(_fy\d+)?$"),
    ("ev_sales", "EV / sales", r"^ev_to_sales$"),
    ("dividend_yield", "Dividend yield", r"^dividend_yield$"),
    ("fcf_yield", "FCF yield", r"^fcf_yield$"),
]


def multiples(claims: list[C]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key, label, rx in MULTIPLES:
        aliases = rx.strip("^$()").split("|")

        def pref(c: C, aliases: list[str] = aliases) -> tuple:
            idx = next((i for i, a in enumerate(aliases) if re.fullmatch(a, c.metric)), 99)
            top = 0 if re.search(r"\bcap\b|upper", c.period or "", re.I) else 1  # IPO: the upper band first
            return (idx, top, *c.rank)

        for c in sorted(_find(claims, rx), key=pref):
            p = point(c)
            k = (key, p["display"] if p else "")
            if p and k not in seen and not c.stream.startswith("fund"):
                seen.add(k)
                out.append({**p, "key": key, "label": label, "context": c.period})
    return out


_NAME_STOP = re.compile(
    r"\s\(|:|\s(?:trailing|reported|was|is|traded|last traded|carries|peer-implied)\s", re.I
)


def peer_name(c: C, by_id: dict[int, C]) -> str:
    """The peer's name from the start of the claim statement; a correction borrows the corrected claim's name."""
    st = re.sub(r"^\[(?:correction|retirement) of C\d+\]\s*", "", c.statement)
    m = _NAME_STOP.search(st)
    name = (st[: m.start()] if m else st).strip(" .,-")
    bad = not name or len(name) > 60 or re.search(r"\d|%", name)
    if bad and c.corrects and c.corrects in by_id and by_id[c.corrects].id != c.id:
        return peer_name(by_id[c.corrects], by_id)
    if bad:
        suffix = re.sub(r"^.*peer_?", "", c.metric)
        return suffix.upper() if suffix and suffix not in ("pe", "pb", "pe_ttm") else f"C{c.id}"
    return re.sub(r"\s+Limited$|\s+Ltd\.?$", "", name)


PEER_METRICS = [
    ("pe", "P/E", r"^peer_(pe|pe_ttm|current_pe)$"),
    ("pb", "P/B", r"^peer_implied_pb$"),
    ("current_yield", "Current yield", r"^current_yield_peer_\w+$"),
]


def peers(
    claims: list[C], by_id: dict[int, C], subject: dict[str, dict[str, Any] | None]
) -> list[dict[str, Any]]:
    out = []
    for key, label, rx in PEER_METRICS:
        rows: dict[str, tuple[tuple, dict[str, Any]]] = {}
        for c in _find(claims, rx):
            p = point(c)
            if not p:
                continue
            name = peer_name(c, by_id)
            if name not in rows or c.rank < rows[name][0]:
                rows[name] = (c.rank, {**p, "name": name})
        if len(rows) < 2:
            continue
        items = sorted((r for _, r in rows.values()), key=lambda r: r["value"])
        own = subject.get(key)
        out.append({"key": key, "label": label, "unit": items[0]["unit"], "rows": items,
                    "subject": {**own, "name": "This company"} if own else None})  # fmt: skip
    return out


# --------------------------------------------------------------------------- IPO
SUB_CATS = [("qib", "QIB"), ("nii", "NII (all)"), ("bnii", "bNII (>₹10L)"), ("snii", "sNII (₹2–10L)"),
            ("retail", "Retail"), ("employee", "Employees"), ("total", "Total")]  # fmt: skip
NSE_CODES = {"1": "qib", "2": "nii", "2.1": "bnii", "2.2": "snii", "3": "retail"}


def ipo_block(claims: list[C], watch: dict[str, Any] | None) -> dict[str, Any]:
    g = lambda rx: pick(claims, rx)  # noqa: E731
    band = {
        "low": g(r"^price_band_lower$"),
        "high": g(r"^price_band_upper$"),
        "face_value": g(r"^face_value$"),
    }
    lot = {"shares": g(r"^lot_size$")}
    for c in sorted(_find(claims, r"^lot_value$"), key=lambda c: c.rank):
        side = (
            "cap"
            if re.search(r"cap|upper", c.period or "", re.I)
            else "floor"
            if re.search(r"floor|lower", c.period or "", re.I)
            else None
        )
        if side and f"cost_{side}" not in lot:
            lot[f"cost_{side}"] = point(c)
    issue = {"total": g(r"^(total_issue_size|offer_size_rhp|issue_size)$"), "fresh": g(r"^fresh_issue_amount$"),
             "ofs": g(r"^ofs_amount$")}  # fmt: skip
    proceeds = []
    parts = _find(claims, r"^objects_(?!capex_and_)\w+$") + _find(claims, r"^general_corporate_purposes\w*$")
    seen: set[str] = set()
    for c in sorted(parts, key=lambda c: c.rank):
        p = point(c)
        if p and c.metric not in seen:
            seen.add(c.metric)
            label = humanise(re.sub(r"^objects_", "", c.metric)).replace("GCP", "General corporate purposes")
            label = {"Capex": "Capital expenditure", "Repayment": "Debt repayment"}.get(label, label)
            proceeds.append({**p, "label": label.replace(" cap", ""), "cap": c.metric.endswith("_cap")})
    anchor = {"shares": g(r"^(anchor_total_shares|anchor_portion_shares)$"),
              "amount": g(r"^(anchor_allocation_total|anchor_amount)$"),
              "mf_shares": g(r"^anchor_mf_shares$"), "insurance_shares": g(r"^anchor_insurance_shares$")}  # fmt: skip
    holding = {
        "pre": g(r"^(promoter_holding_pre_issue|promoter_pre_offer_holding_pct|promoter_holding_pre_offer)$"),
        "post": g(r"^(promoter_holding_post_issue|promoter_holding_post_offer_cap)$"),
        "post_floor": g(r"^(promoter_holding_post_offer_floor|promoter_post_offer_holding_pct_floor)$"),
    }
    # subscription by category at the latest timestamp the ledger has
    subs: dict[str, dict[str, Any]] = {}
    sub_claims = _find(claims, r"^(qib|nii|bnii|snii|retail|rii|employee|total)_subscription\w*$")
    latest = max((c.period or "" for c in sub_claims), default="")
    for c in sorted((c for c in sub_claims if (c.period or "") == latest), key=lambda c: c.rank):
        cat = c.metric.split("_")[0].replace("rii", "retail")
        if cat not in subs and (p := point(c)):
            subs[cat] = p
    subscription = [{**subs[k], "category": k, "label": lab} for k, lab in SUB_CATS if k in subs]
    reservation = []
    for c in _find(claims, r"^(reservation_split|category_reservation|offer_structure)$", numeric=False):
        for key, rx in (("QIB", r"QIB[^%.;]*?(\d{1,2})\s?%"), ("NII", r"(?:NII|Non-Institutional)[^%.;]*?(\d{1,2})\s?%"),
                        ("Retail", r"Retail[^%.;]*?(\d{1,2})\s?%")):  # fmt: skip
            m = re.search(rx, c.statement)
            if m and not any(r["label"] == key for r in reservation):
                reservation.append({"label": key, "value": float(m.group(1)), "claim_id": c.id, "status": c.status,
                                    "display": f"{m.group(1)}%"})  # fmt: skip
    dates = []
    for c in sorted(_find(claims, r"_date$", numeric=False), key=lambda c: (c.period or "", c.rank)):
        if (
            c.period
            and re.match(r"^\d{4}-\d{2}-\d{2}$", c.period)
            and not any(d["label"] == c.metric for d in dates)
        ):
            dates.append({"label": c.metric, "title": humanise(c.metric.removesuffix("_date")), "date": c.period,
                          "claim_id": c.id, "status": c.status})  # fmt: skip
    timeline = []
    claimed = {d["date"] for d in dates}
    if watch:
        for key, title in (("open_date", "Bidding opens"), ("close_date", "Bidding closes"),
                           ("allotment_date", "Allotment (expected)"), ("listing_date", "Listing (expected)")):  # fmt: skip
            if watch.get(key) and watch[key] not in claimed:  # a cited date beats the monitor's expectation
                timeline.append({"title": title, "date": watch[key], "source": "monitor"})
    for d in dates:
        timeline.append(
            {"title": d["title"], "date": d["date"], "claim_id": d["claim_id"], "status": d["status"]}
        )
    timeline.sort(key=lambda d: d["date"])
    snaps = []
    for s in (watch or {}).get("snapshots", []):
        row: dict[str, Any] = {"as_of": s["as_of"], "source": s.get("source")}
        tot = _dec(s.get("total_times"))
        row["total"] = float(tot) if tot is not None else None
        for cat in s.get("categories") or []:
            k = NSE_CODES.get(str(cat.get("code") or ""))
            v = _dec(cat.get("times"))
            if k and v is not None:
                row[k] = float(v.quantize(Decimal("0.01")))
        snaps.append(row)
    return {"price_band": band, "lot": lot, "issue": issue, "proceeds": proceeds, "anchor": anchor,
            "holding": holding, "subscription": subscription, "subscription_as_of": latest or None,
            "subscription_timeline": snaps, "reservation": reservation, "timeline": timeline}  # fmt: skip


def _lab(p: dict[str, Any] | None, **extra: Any) -> dict[str, Any] | None:
    return {**p, **extra} if p else None


# --------------------------------------------------------------------------- funds
def fund_block(claims: list[C], by_id: dict[int, C]) -> dict[str, Any]:
    g = lambda rx, **kw: pick(claims, rx, **kw)  # noqa: E731
    returns = []
    for h in ("1", "3", "5"):
        row = {"horizon": f"{h}Y",
               "fund": g(rf"^(return_{h}y_annualised|return_{h}y)$"),
               "benchmark": g(rf"^benchmark_return_{h}y$"),
               "category": g(rf"^category_median_return_{h}y$"),
               "index_fund": g(rf"^index_fund_return_{h}y$")}  # fmt: skip
        if any(row[k] for k in ("fund", "benchmark", "category", "index_fund")):
            returns.append(row)
    peer_rows: dict[str, dict[str, Any]] = {}
    for h in ("1", "3", "5"):
        for c in sorted(_find(claims, rf"^peer_return_{h}y$"), key=lambda c: c.rank):
            name = peer_name(c, by_id)
            r = peer_rows.setdefault(name, {"name": name})
            if f"r{h}y" not in r and (p := point(c)):
                r[f"r{h}y"] = p
    own = {f"r{h}y": row["fund"] for row in returns if (h := row["horizon"][0]) and row["fund"]}
    rolling = {
        k: g(rf"^rolling_3y_return_{k}$|^rolling_3y_{k}_5y$") for k in ("minimum", "median", "maximum")
    }
    rolling = {"min": rolling["minimum"] or g(r"^rolling_3y_min_5y$"), "median": rolling["median"],
               "max": rolling["maximum"] or g(r"^rolling_3y_max_5y$")}  # fmt: skip
    phases = []
    for c in sorted(_find(claims, r"^phase_return_\w+$"), key=lambda c: (c.period or "", c.rank)):
        p = point(c)
        if p and not any(x["metric"] == c.metric for x in phases):
            phases.append(
                {**p, "label": humanise(c.metric.removeprefix("phase_return_")).replace("Covid", "COVID")}
            )
    risk = [x for x in (
        _lab(g(r"^volatility_annualised$", streams=("facts",)), label="Volatility (a year)", key="volatility"),
        _lab(g(r"^max_drawdown$", streams=("facts",)), label="Worst fall (max drawdown)", key="max_drawdown"),
        _lab(g(r"^max_drawdown_10y$"), label="Worst fall, 10 years", key="max_drawdown_10y"),
        _lab(g(r"^sharpe_10y$"), label="Sharpe ratio, 10 years", key="sharpe"),
        _lab(g(r"^sortino_10y$"), label="Sortino ratio, 10 years", key="sortino"),
    ) if x]  # fmt: skip
    costs = [x for x in (
        _lab(g(r"^(ter_direct|expense_ratio_direct|expense_ratio)$"), label="This fund, Direct"),
        _lab(g(r"^ter_regular$"), label="This fund, Regular"),
        _lab(g(r"^ter_direct_index_peer$"), label="Index fund alternative"),
    ) if x]  # fmt: skip
    alloc = []
    for c in sorted(_find(claims, r"^(market_cap_allocation_\w+|cash_allocation)$"), key=lambda c: c.rank):
        p = point(c)
        if p and not any(a["metric"] == c.metric for a in alloc):
            alloc.append({**p, "label": humanise(c.metric.removeprefix("market_cap_allocation_")).replace(
                "Largecap", "Large cap").replace("Midcap", "Mid cap").replace("Smallcap", "Small cap")
                .replace("Cash allocation", "Cash & others")})  # fmt: skip
    return {"returns": returns, "peers": [dict(own, name="This fund"), *peer_rows.values()] if own else
            list(peer_rows.values()), "rolling": rolling, "phases": phases, "risk": risk, "costs": costs,
            "allocation": alloc, "nav": g(r"^nav$"), "aum": g(r"^aum$")}  # fmt: skip


# --------------------------------------------------------------------------- bonds
def bond_block(claims: list[C], by_id: dict[int, C]) -> dict[str, Any]:
    g = lambda rx, **kw: pick(claims, rx, **kw)  # noqa: E731
    yields = [x for x in (
        _lab(g(r"^ytm$"), label="This bond: YTM at last price"),
        _lab(g(r"^coupon_rate$"), label="This bond: coupon"),
        _lab(g(r"^gsec_yield_\w+$"), label="2-year G-sec"),
        _lab(g(r"^sbi_fd_rate\w*$"), label="SBI FD (2–3 years)"),
        _lab(g(r"^short_duration_fund\w*$"), label="Short-duration debt fund (3Y)"),
        _lab(g(r"^rbi_repo_rate$"), label="RBI repo rate"),
    ) if x]  # fmt: skip
    for c in sorted(_find(claims, r"^current_yield_peer_\w+$"), key=lambda c: c.rank):
        if (p := point(c)) and not any(y["metric"] == c.metric for y in yields):
            yields.append(
                {**p, "label": f"Peer {c.metric.removeprefix('current_yield_peer_')} (current yield)"}
            )
    ratings = []
    rated = _find(claims, r"^(credit_)?rating(_\w+)?$", numeric=False)
    for c in sorted(rated, key=lambda c: (c.metric not in ("rating", "credit_rating"), c.rank)):
        if c.value is None and len(ratings) < 6 and "sensitivity" not in c.metric:
            ratings.append({"agency": humanise(c.metric.removeprefix("credit_rating_").removeprefix("credit_")),
                            "text": _sentence(c.statement), "claim_id": c.id, "status": c.status})  # fmt: skip
    maturity = _best(_find(claims, r"^maturity_year$"))
    return {
        "coupon": g(r"^coupon_rate$"), "face_value": g(r"^face_value$"), "last_price": g(r"^last_price$"),
        "frequency": g(r"^coupon_frequency$"), "issue_size": g(r"^issue_size$"),
        "maturity": {"date": maturity.period, "claim_id": maturity.id, "status": maturity.status} if maturity else None,
        "yields": yields, "ratings": ratings,
        "accrued": g(r"^accrued_interest$"), "duration": g(r"^modified_duration$"),
    }  # fmt: skip


def _sentence(text: str, limit: int = 180) -> str:
    first = re.split(r"(?<=[\w)])(?<!\bLtd)(?<!\bNo)\.\s+(?=[A-Z\[])", text, maxsplit=1)[0].rstrip(".")
    return first if len(first) <= limit else first[: limit - 1].rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------- report text
def norm_cites(text: str) -> str:
    """Bare C123 / (C123) / (C1/C2) references become [C123] so the dashboard renders them as chips."""
    text = re.sub(r"\((C\d+(?:\s*[/,;]\s*C\d+)*)\)", lambda m: "".join(f"[{x.strip()}]" for x in re.split(r"[/,;]", m.group(1))), text)  # fmt: skip
    return re.sub(r"(?<![\w\[])C(\d{3,})(?![\w\]])", r"[C\1]", text)


def cite_ids(text: str) -> list[int]:
    return sorted({int(next(g for g in m.groups() if g)) for m in CITE_ANY.finditer(text)})


def _section(md: str, heading: str) -> str | None:
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if re.match(r"^##\s", line) and re.search(heading, line, re.I):
            end = next((j for j in range(i + 1, len(lines)) if re.match(r"^##\s", lines[j])), len(lines))
            return "\n".join(lines[i + 1 : end])
    return None


SEVERITY = re.compile(r"\((high|medium-high|medium|low-medium|low)\)", re.I)


def ranked_risks(md: str | None) -> list[dict[str, Any]]:
    """The report's numbered risk list: title, text, stated severity (None when the report gives none), citations."""
    body = (_section(md, r"ranked risks|key risks|risks and") or _section(md, r"risk")) if md else None
    if not body:
        return []
    items: list[list[str]] = []
    for line in body.split("\n"):
        if re.match(r"^\d+\.\s", line):
            items.append([line])
        elif items and (line.startswith(("  ", "\t")) and line.strip()):
            items[-1].append(line)
        elif items and line.strip() and not line.startswith(" "):
            break  # the numbered list has ended (e.g. a litigation summary follows)
    out = []
    for n, item in enumerate(items, 1):
        first = re.sub(r"^\d+\.\s+", "", item[0])
        m = re.match(r"\*\*(.+?)\*\*\s*(.*)", first)
        title, rest = (m.group(1), m.group(2)) if m else (first, "")
        sev = SEVERITY.search(title) or SEVERITY.search(rest)
        title = SEVERITY.sub("", title).strip(" .:")
        subs = [re.sub(r"^\s*[-*]\s+", "", x).strip() for x in item[1:]]
        text = " ".join(x for x in [rest.strip(), *subs] if x)
        out.append({"rank": n, "title": title, "text": norm_cites(text), "severity": sev.group(1).lower() if sev else None,
                    "claim_ids": cite_ids(" ".join(item))})  # fmt: skip
    return out


def case_points(points: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for p in points or []:
        if isinstance(p, dict) and p.get("point"):
            out.append({"text": norm_cites(str(p["point"])), "weight": p.get("weight"),
                        "claim_ids": [int(x) for x in p.get("claim_ids") or [] if str(x).isdigit()]})  # fmt: skip
    return out


def verdict_block(kind: str, syn: dict[str, Any]) -> dict[str, Any]:
    word = syn.get("overall_verdict") or syn.get("verdict")
    return {"word": word, "confidence": syn.get("confidence"), "horizon": syn.get("horizon"),
            "entry_zone": norm_cites(syn["entry_zone"]) if syn.get("entry_zone") else None,
            "price_or_yield": norm_cites(syn["price_or_yield"]) if syn.get("price_or_yield") else None,
            "suits": norm_cites(syn["suits"]) if syn.get("suits") else None,
            "condition": norm_cites(syn["condition"]) if syn.get("condition") else None,
            "listing": norm_cites(syn["verdict_listing"]) if syn.get("verdict_listing") else None,
            "long_term": norm_cites(syn["verdict_long_term"]) if syn.get("verdict_long_term") else None,
            "summary": norm_cites(syn["executive_summary"]) if syn.get("executive_summary") else None}  # fmt: skip


# --------------------------------------------------------------------------- key numbers and plain English
def _tile(
    p: dict[str, Any] | None,
    label: str,
    term: str | None = None,
    delta: dict | None = None,
    hint: str | None = None,
) -> dict[str, Any] | None:
    if not p:
        return None
    return {**p, "label": label, "term": term, "delta": delta, "hint": hint or p.get("period")}


def _cite(*ps: dict[str, Any] | None) -> str:
    return "".join(f"[C{p['claim_id']}]" for p in ps if p)


def _latest(s: dict[str, Any] | None) -> dict[str, Any] | None:
    return s["points"][-1] if s and s["points"] else None


def key_numbers_and_story(kind: str, claims: list[C], series: list[dict[str, Any]], mult: list[dict[str, Any]],
                          ipo: dict | None, fund: dict | None, bond: dict | None,
                          peer_sets: list[dict[str, Any]]) -> tuple[list[dict], list[str]]:  # fmt: skip
    rev, pat = _main_series(series, "revenue"), _main_series(series, "pat")
    roe = _main_series(series, "roe")
    tiles: list[dict | None] = []
    story: list[str] = []

    def growth_line() -> None:
        if rev and len(rev["points"]) >= 2:
            a, b = rev["points"][0], rev["points"][-1]
            line = f"Revenue went from {a['display']} in {a['period']} to {b['display']} in {b['period']} {_cite(a, b)}"
            if pat and len(pat["points"]) >= 2:
                c, d = pat["points"][0], pat["points"][-1]
                line += f"; net profit went from {c['display']} to {d['display']} over {c['period']}–{d['period']} {_cite(c, d)}"
            story.append(line + ".")

    pe = next((m for m in mult if m["key"] == "pe"), None)
    if kind == "ipo_report" and ipo:
        band, lot, issue = ipo["price_band"], ipo["lot"], ipo["issue"]
        subs = {s["category"]: s for s in ipo["subscription"]}
        tiles += [
            _tile(band["high"], "Price band (upper)", "Price band",
                  hint=f"band {band['low']['display']}–{band['high']['display'][1:]} a share" if band["low"] and band["high"] else None),
            _tile(lot.get("cost_cap") or lot.get("cost_floor"), "One lot costs", "Lot",
                  hint=f"{lot['shares']['display']} per lot" if lot.get("shares") else None),
            _tile(issue["total"], "Issue size", "OFS vs fresh issue"),
            _tile(pe, "P/E at the upper band", "P/E", hint=pe["context"] if pe else None),
            _tile(_latest(rev), "Revenue", None, _delta(rev)),
            _tile(_latest(pat), "Net profit (PAT)", None, _delta(pat)),
            _tile(_latest(roe), "Return on equity", None),
            _tile(subs.get("total"), "Subscribed (total)", "Subscription (x times)", hint=ipo["subscription_as_of"]),
        ]  # fmt: skip
        if band["low"] and band["high"]:
            s = f"The IPO sells shares at {band['low']['display']}–{band['high']['display'][1:]} each {_cite(band['low'], band['high'])}"
            if lot.get("shares"):
                s += f"; you bid in lots of {lot['shares']['display']} {_cite(lot['shares'])}"
                if lot.get("cost_cap"):
                    s += f", so one lot blocks {lot['cost_cap']['display']} at the top of the band {_cite(lot['cost_cap'])}"
            story.append(s + ".")
        if issue["fresh"] and issue["ofs"]:
            story.append(f"Of the money raised, {issue['fresh']['display']} is new money for the company (fresh issue) "
                         f"and {issue['ofs']['display']} goes to existing shareholders who are selling (OFS) "
                         f"{_cite(issue['fresh'], issue['ofs'])}.")  # fmt: skip
        growth_line()
        if pe:
            line = f"At the top of the band you pay {pe['display']} its last full year's earnings per share (P/E) {_cite(pe)}"
            ps = next((p for p in peer_sets if p["key"] == "pe"), None)
            if ps:
                lo, hi = ps["rows"][0], ps["rows"][-1]
                line += f"; listed peers trade between {lo['display']} ({lo['name']}) and {hi['display']} ({hi['name']}) {_cite(lo, hi)}"
            story.append(line + ".")
        if subs.get("total"):
            line = f"By {ipo['subscription_as_of']}, investors had bid for {subs['total']['display']} the shares on offer {_cite(subs['total'])}"
            if subs.get("qib"):
                line += f"; big institutions (QIB) {subs['qib']['display']} {_cite(subs['qib'])}"
            if subs.get("retail"):
                line += f", retail {subs['retail']['display']} {_cite(subs['retail'])}"
            story.append(line + " (INTERIM while bidding is open).")
    elif kind == "stock_report":
        price = pick(claims, r"^(last_price|close_price)$", streams=("facts", "stock_valuation"))
        mcap = pick(claims, r"^market_cap$", streams=("stock_valuation",)) or pick(
            claims, r"^market_cap(italization)?$"
        )
        dy = next((m for m in mult if m["key"] == "dividend_yield"), None)
        hi, lo = pick(claims, r"^week52_high$"), pick(claims, r"^week52_low$")
        tiles += [
            _tile(price, "Share price", None, hint=price["period"] if price else None),
            _tile(mcap, "Market cap", None),
            _tile(pe, "P/E (trailing)", "P/E", hint=pe["context"] if pe else None),
            _tile(dy, "Dividend yield", None),
            _tile(_latest(rev), "Revenue", None, _delta(rev)),
            _tile(_latest(pat), "Net profit", None, _delta(pat)),
            _tile(_latest(roe), "Return on equity", None),
            _tile(next((m for m in mult if m["key"] == "fcf_yield"), None), "FCF yield", None),
        ]  # fmt: skip
        if price and hi and lo:
            story.append(f"The share last traded at {price['display']}, against a 52-week range of {lo['display']}–{hi['display'][1:]} "
                         f"{_cite(price, lo, hi)}.")  # fmt: skip
        if pe:
            line = (
                f"At that price you pay {pe['display']} the last year's earnings per share (P/E) {_cite(pe)}"
            )
            if dy:
                line += f", and the dividend alone yields {dy['display']} a year {_cite(dy)}"
            story.append(line + ".")
        growth_line()
    elif kind == "fund_report" and fund:
        by_h = {r["horizon"]: r for r in fund["returns"]}
        ter = fund["costs"][0] if fund["costs"] else None
        dd = next((r for r in fund["risk"] if r["key"] in ("max_drawdown_10y", "max_drawdown")), None)
        tiles += [
            _tile(fund["nav"], "NAV", "NAV"), _tile(fund["aum"], "Fund size (AUM)", None),
            *[_tile(by_h[h]["fund"], f"{h} return (a year)", "CAGR") for h in ("1Y", "3Y", "5Y") if h in by_h],
            _tile(ter, "Expense ratio (Direct)", "Expense ratio"),
            _tile(dd, "Worst fall", "Drawdown"),
            _tile(next((r for r in fund["risk"] if r["key"] == "volatility"), None), "Volatility", None),
        ]  # fmt: skip
        for h in ("5Y", "3Y"):
            r = by_h.get(h)
            if r and r["fund"]:
                line = f"Over {h[0]} years the fund returned {r['fund']['display']} a year {_cite(r['fund'])}"
                extra = [f"{lab} {r[k]['display']} {_cite(r[k])}" for k, lab in (("benchmark", "its benchmark"),
                         ("category", "the category median")) if r.get(k)]  # fmt: skip
                story.append(line + (f", against {' and '.join(extra)}" if extra else "") + ".")
                break
        if dd:
            story.append(
                f"The worst fall from a peak in that record was {dd['display']} {_cite(dd)}: be ready to sit through that."
            )
        if ter:
            line = f"It costs {ter['display']} a year in the Direct plan {_cite(ter)}"
            idx = next((c for c in fund["costs"] if c["label"].startswith("Index")), None)
            if idx:
                line += f"; an index-fund alternative costs {idx['display']} {_cite(idx)}"
            story.append(line + ".")
    elif kind == "bond_report" and bond:
        ytm = next((y for y in bond["yields"] if y["label"].startswith("This bond: YTM")), None)
        tiles += [
            _tile(bond["coupon"], "Coupon (a year)", "Coupon"), _tile(bond["last_price"], "Last traded price", None),
            _tile(bond["face_value"], "Face value (repaid at maturity)", None),
            _tile(ytm, "Yield to maturity", "YTM"), _tile(bond["frequency"], "Coupon payments a year", None),
            _tile(bond["issue_size"], "Series size", None),
        ]  # fmt: skip
        if bond["coupon"] and bond["face_value"]:
            line = f"The bond pays {bond['coupon']['display']} a year on its {bond['face_value']['display']} face value {_cite(bond['coupon'], bond['face_value'])}"
            if bond["frequency"]:
                line += f", {bond['frequency']['display'].split()[0]} times a year {_cite(bond['frequency'])}"
            if bond["maturity"]:
                line += f", and repays the face value on {bond['maturity']['date']} [C{bond['maturity']['claim_id']}]"
            story.append(line + ".")
        if bond["last_price"] and bond["face_value"]:
            lp, fv = Decimal(bond["last_price"]["raw"]), Decimal(bond["face_value"]["raw"])
            rel = "above" if lp > fv else "below" if lp < fv else "at"
            story.append(f"It last traded at {bond['last_price']['display']}, {rel} the {bond['face_value']['display']} you get back "
                         f"at maturity {_cite(bond['last_price'], bond['face_value'])}"
                         + (": the premium is lost by maturity." if rel == "above" else "."))  # fmt: skip
        if bond["ratings"]:
            r = bond["ratings"][0]
            story.append(f"Credit rating: {r['text']} [C{r['claim_id']}].")
    return [t for t in tiles if t], story


# --------------------------------------------------------------------------- quality
def quality(raw: list[dict[str, Any]], cited: set[int]) -> dict[str, Any]:
    statuses = ("verified", "needs_review", "unverified", "contradicted", "unsupported")
    by_stream: dict[str, dict[str, int]] = {}
    totals = dict.fromkeys(statuses, 0)
    for x in raw:
        st = x.get("status") or "unverified"
        row = by_stream.setdefault(x.get("stream") or "other", {**dict.fromkeys(statuses, 0), "total": 0})
        row[st] = row.get(st, 0) + 1
        row["total"] += 1
        totals[st] = totals.get(st, 0) + 1
    cited_rows = [x for x in raw if int(x["id"]) in cited]
    cv = sum(1 for x in cited_rows if x.get("status") == "verified")
    n = len(raw)
    # evidence grades of the claims the report cites (#242); claims serialised before grades have none
    by_grade = dict.fromkeys("ABCDU", 0)
    for x in cited_rows:
        if x.get("evidence_grade") in by_grade:
            by_grade[x["evidence_grade"]] += 1
    return {"total": n, "by_status": totals, "verified_pct": round(100 * totals["verified"] / n, 1) if n else None,
            "cited_by_grade": by_grade,
            "cited": len(cited_rows), "cited_verified_pct": round(100 * cv / len(cited_rows), 1) if cited_rows else None,
            "by_stream": [{"stream": k, **v} for k, v in sorted(by_stream.items(), key=lambda kv: -kv[1]["total"])]}  # fmt: skip


def fair_values(claims: list[C]) -> list[dict[str, Any]]:
    """Fair-value / entry-zone estimates. Each is tagged with its `group` and `role` (low / mid / high, from the
    metric name or the period) so a low and a high of the same estimate can be drawn as one range; a lone value
    stays a point. Nothing is paired that the ledger does not pair."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for c in sorted(
        _find(claims, r"^(fair_value\w*|entry_zone\w*|max_buy_price)$"),
        key=lambda c: (STATUS_RANK.get(c.status, 9), c.metric, c.rank),
    ):
        p = point(c)
        if not p:
            continue
        m = re.search(r"_(low|high|mid)(?=_|$)", c.metric)
        role = m.group(1) if m else None
        if role is None and (pm := re.match(r"\s*(low|high|mid)\b", c.period or "", re.I)):
            role = pm.group(1).lower()
        group = re.sub(r"_(low|high|mid)(?=_|$)", "", c.metric)
        mult = re.search(r"_(\d+)(?:_(\d+))?x$", group)
        group = group[: mult.start()] if mult else group
        key = (group, role or c.metric)
        if key in seen:
            continue  # the best-ranked claim (verified first) for each end of the range
        seen.add(key)
        basis = f"{mult.group(1)}{'.' + mult.group(2) if mult.group(2) else ''}x" if mult else None
        out.append({**p, "label": humanise(group), "group": group, "role": role, "basis": basis})
    return sorted(out, key=lambda f: (f["group"], f["value"]))


# --------------------------------------------------------------------------- entry point
def build_insights(*, run_id: int, kind: str, claims: list[dict[str, Any]], synthesis: dict[str, Any] | None,
                   report_markdown: str | None, subject: dict[str, Any] | None = None,
                   watch: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
    usable, by_id = _claims(claims)
    syn = synthesis or {}
    cited = {int(x) for x in re.findall(r"\[C(\d+)\]", report_markdown or "")}
    series = financial_series(usable, FINANCIAL_STREAMS.get(kind, ()), PRIMARY_STREAM.get(kind))
    mult = multiples(usable)
    own_pe = next((m for m in mult if m["key"] == "pe"), None)
    own_pb = next((m for m in mult if m["key"] == "pb"), None)
    peer_sets = peers(usable, by_id, {"pe": own_pe, "pb": own_pb})
    ipo = ipo_block(usable, watch) if kind == "ipo_report" else None
    fund = fund_block(usable, by_id) if kind == "fund_report" else None
    bond = bond_block(usable, by_id) if kind == "bond_report" else None
    # the Summary's headline tiles and plain-English lines use VERIFIED claims only: a needs-review figure (e.g. a
    # yield the report itself dropped) may appear in charts and evidence with its badge, never as a headline
    ver = [c for c in usable if c.status == "verified"]
    v_mult = multiples(ver)
    tiles, story = key_numbers_and_story(
        kind,
        ver,
        financial_series(ver, FINANCIAL_STREAMS.get(kind, ()), PRIMARY_STREAM.get(kind)),
        v_mult,
        ipo_block(ver, watch) if kind == "ipo_report" else None,
        fund_block(ver, by_id) if kind == "fund_report" else None,
        bond_block(ver, by_id) if kind == "bond_report" else None,
        peers(ver, by_id, {"pe": next((m for m in v_mult if m["key"] == "pe"), None), "pb": None}),
    )
    fair = fair_values(usable)
    from finresearch.api.accuracy import accuracy_block, triangulation_block

    scenarios = []
    for s in syn.get("scenarios") or []:
        if isinstance(s, dict) and (s.get("price_low") is not None or s.get("price_high") is not None):
            scenarios.append({"name": s.get("name"), "horizon": s.get("horizon"), "low": s.get("price_low"),
                              "high": s.get("price_high"), "likelihood": s.get("likelihood"),
                              "rationale": norm_cites(s.get("rationale") or "")})  # fmt: skip
    return {
        "run_id": run_id, "kind": kind, "subject": subject or {},
        "verdict": verdict_block(kind, syn),
        "plain_english": story,
        "key_numbers": tiles,
        "financials": {"series": series},
        "valuation": {"multiples": mult, "peers": peer_sets, "fair_value": fair},
        "ipo": ipo, "fund": fund, "bond": bond,
        "scenarios": scenarios,
        "pros": case_points(syn.get("reasons_for")), "cons": case_points(syn.get("reasons_against")),
        "risks": ranked_risks(report_markdown),
        "checklist": [{"text": norm_cites(str(x)), "claim_ids": cite_ids(str(x))} for x in syn.get("action_checklist") or []],
        "alternatives": [norm_cites(str(x)) for x in syn.get("alternatives") or []],
        "quality": quality(claims, cited),
        "accuracy": accuracy_block(claims, cited),
        "triangulation": triangulation_block(claims, kind, fair),
    }  # fmt: skip


# --------------------------------------------------------------------------- "since this report" (roadmap item 4)
STALE_DAYS = 90  # a stock report older than a quarter has missed at least one results filing


def price_bands(fair: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The report's per-share fair-value / entry-zone estimates as bands (low-high) or points, from `fair_values`."""
    groups: dict[str, dict[str, Any]] = {}
    for f in fair:
        if f.get("unit") not in ("₹/share", "₹"):
            continue
        g = groups.setdefault(f["group"], {"group": f["group"], "label": f["label"], "low": None, "high": None,
                                           "mid": None, "claim_ids": [], "verified": True})  # fmt: skip
        role = f.get("role") or "mid"
        if g.get(role) is None:
            g[role] = f["value"]
            g["claim_ids"].append(f["claim_id"])
            g["verified"] = g["verified"] and f.get("status") == "verified"
    out = []
    for g in groups.values():
        lo, hi = g["low"], g["high"]
        if lo is None and hi is None:
            lo = hi = g["mid"]
        if lo is None or hi is None:
            continue
        out.append({**g, "low": min(lo, hi), "high": max(lo, hi)})
    return out


def freshness(*, run_id: int, kind: str, symbol: str | None, report_at: datetime | None, now: datetime,
              price: float | None, price_as_of: str | None, fair: list[dict[str, Any]],
              announcements: list[dict[str, Any]], stale_days: int = STALE_DAYS) -> dict[str, Any]:  # fmt: skip
    """What changed since the report: days elapsed, the live price against the report's entry zone and fair-value
    bands, filings made since, and whether a re-run is worth it. Pure: callers pass the clock and the data."""
    days = (now - report_at).days if report_at else None
    bands = []
    for b in price_bands(fair):
        pos = None
        if price is not None:
            pos = "below" if price < b["low"] else "above" if price > b["high"] else "inside"
        bands.append({**b, "position": pos,
                      "distance": None if price is None else
                      (price / b["low"] - 1 if pos == "below" else price / b["high"] - 1 if pos == "above" else 0.0)})  # fmt: skip
    since = sorted((a for a in announcements if report_at and a.get("at") and datetime.fromisoformat(a["at"]) > report_at),
                   key=lambda a: a["at"], reverse=True)  # fmt: skip
    results = [a for a in since if a.get("results_period_end")]
    reasons: list[str] = []
    if days is not None and days > stale_days:
        reasons.append(f"The report is {days} days old (over {stale_days}).")
    if results:
        reasons.append(f"{len(results)} results filing{'s' if len(results) > 1 else ''} since the report.")
    for b in bands:  # only a verified estimate can fire the review trigger
        if b["position"] == "above" and "fair" in b["group"] and b["verified"]:
            reasons.append(f"The price is above the report's {b['label'].lower()} band (a review trigger).")
    return {"run_id": run_id, "kind": kind, "symbol": symbol,
            "report_at": report_at.isoformat() if report_at else None, "now": now.isoformat(), "days_since": days,
            "price": price, "price_as_of": price_as_of, "bands": bands,
            "new_filings": {"count": len(since), "results": len(results), "items": since[:8]},
            "stale": bool(reasons), "suggest_rerun": (days is not None and days > stale_days) or bool(results),
            "reasons": reasons}  # fmt: skip
