"""The AIS check: one financial year of the user's AIS (portfolio.ais) against the app's own records.

Compared, per security (ISIN when the AIS has it, else the normalised name):
* dividends: AIS dividend rows (SFT-015 and TDS s.194/194K [U]) vs the app's dividend transactions;
* sale consideration: AIS "sale of securities and units of MF" rows vs the app's sell transactions at their GROSS value
  (units x price, before charges). Disposal `proceeds` are net of charges (models.PortfolioDisposal), while the AIS
  reports the consideration with STT in its own column (SFT-STT, AIS app bundle) [V], so the gross is the like-for-like
  figure;
* purchases: AIS purchase rows vs the app's buys (gross).
Interest and off-market transfers have no app side (the app tracks no bank, deposit or bond interest): they are listed
for the user to check by hand.

The same dividend or interest can be reported twice, once by the payer's TDS return and once in its SFT (the AIS help
says only the "processed" value is de-duplicated) [V]: a TDS row with an SFT twin (same amount, same or unknown date)
from the same payer is dropped here.

Tolerance: |AIS - app| <= max(₹1, 0.1 % of the AIS amount) is a match (rounding of NAV x units, paise).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fincalc.dates import fiscal_year
from finresearch.fincalc.tax import fy_label
from finresearch.portfolio.ais import FORMAT_NOTE, AisItem

ABS_TOL = Decimal(1)
REL_TOL = Decimal("0.001")
TDS_RATE = Decimal(
    "0.10"
)  # s.194 / s.194K TDS on dividends for a resident with a PAN (Income-tax Act 1961) [V]
COMPARED = ("dividend", "sale", "purchase")
_GENERIC = {"LIMITED", "LTD", "THE", "PLAN", "OPTION", "OPT", "INDIA", "CO", "COMPANY", "CORPORATION", "CORP",
            "AND", "INC", "PVT", "PRIVATE"}  # fmt: skip
_AMC_WORDS = {"MUTUAL", "FUND", "MF", "AMC", "ASSET", "MANAGEMENT", "INVESTMENT", "MANAGERS", "TRUSTEE"}


@dataclass
class AppEntry:
    """One app transaction in the AIS check's terms."""

    holding_id: int
    name: str
    isin: str | None
    asset_type: str  # stock | mf | other
    category: str  # dividend | sale | purchase
    day: date
    amount: Decimal  # gross
    quantity: Decimal | None = None
    charges: Decimal = Decimal(0)


def tokens(name: str | None) -> frozenset[str]:
    t = re.sub(r"[^A-Z0-9 ]", " ", (name or "").upper().replace("&", " AND "))
    return frozenset(w for w in t.split() if w not in _GENERIC)


def names_match(a: str | None, b: str | None) -> bool:
    """Equal after normalising (Ltd/Limited/Plan/Option/punctuation dropped). Deliberately strict: "Example" never
    matches "Example Finance"; a miss shows up as an only-in-AIS row next to an only-in-app row, which the user sees."""
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return ta == tb


def amc_brand(name: str | None) -> frozenset[str]:
    """'Alpha Mutual Fund' / 'Alpha Asset Management Co Ltd' -> {'ALPHA'}: the payer of an MF dividend is the AMC."""
    return frozenset(tokens(name) - _AMC_WORDS)


def within(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= max(ABS_TOL, abs(a) * REL_TOL)


def dedupe(items: list[AisItem]) -> tuple[list[AisItem], int]:
    """Drop TDS rows that duplicate an SFT row of the same payer (same amount; same date or a date missing)."""
    sft = [i for i in items if i.part == "sft"]
    used: set[int] = set()
    out, dropped = list(sft), 0
    for i in items:
        if i.part == "sft":
            continue
        twin = next((k for k, s in enumerate(sft) if k not in used and within(s.amount, i.amount)
                     and (s.day is None or i.day is None or s.day == i.day)), None)  # fmt: skip
        if twin is None:
            out.append(i)
        else:  # keep the SFT row, with the TDS that only the TDS return carries
            used.add(twin)
            dropped += 1
            if i.tds is not None and sft[twin].tds is None:
                out[twin] = replace(sft[twin], tds=i.tds)
    return out, dropped


@dataclass
class _Group:
    category: str
    label: str
    isin: str | None
    items: list[AisItem]
    app: list[AppEntry] = field(default_factory=list)
    match: str | None = None
    deduped: int = 0

    @property
    def whole_year(self) -> bool:
        """A sale/purchase row without a security (the PDF summary): compared with the app's total for the year."""
        return self.category in ("sale", "purchase") and not any(i.security or i.isin for i in self.items)

    @property
    def amc(self) -> bool:
        return bool(tokens(self.label) & _AMC_WORDS) and bool(amc_brand(self.label))


def _groups(items: Iterable[AisItem], categories: tuple[str, ...]) -> list[_Group]:
    by: dict[tuple[str, str], _Group] = {}
    for i in items:
        if i.category not in categories:
            continue
        label = i.security or i.source or i.description or "(unnamed)"
        key = i.isin or " ".join(sorted(tokens(label))) or label
        if i.category in ("sale", "purchase") and not (i.security or i.isin):  # one row for the year's total
            label, key = f"All {i.category}s this year (AIS gives no security detail)", "*"
        g = by.setdefault((i.category, key), _Group(i.category, label, i.isin, []))
        g.items.append(i)
    for g in by.values():
        if g.category in ("dividend", "interest"):
            g.items, g.deduped = dedupe(g.items)
    return list(by.values())


def _assign(groups: list[_Group], app: list[AppEntry]) -> list[AppEntry]:
    """Attach app entries to AIS groups: ISIN first, then exact normalised name, then (MF dividends) the AMC brand,
    then (sale/purchase rows with no security) everything left in that category. Each app entry is used once. Returns the entries left over (only in the app)."""
    left = list(app)
    passes = (
        ("isin", lambda g, e: bool(g.isin) and g.isin == e.isin),
        ("name", lambda g, e: not g.whole_year and names_match(g.label, e.name)),
        ("amc", lambda g, e: g.category == "dividend" and e.asset_type == "mf" and g.amc
                              and amc_brand(g.label) <= tokens(e.name)),
        ("total", lambda g, e: g.whole_year),
    )  # fmt: skip
    for how, ok in passes:
        for g in groups:
            if g.match not in (None, how):
                continue
            hit = [e for e in left if e.category == g.category and ok(g, e)]
            if hit:
                g.app.extend(hit)
                g.match = g.match or how
                taken = {id(e) for e in hit}
                left = [e for e in left if id(e) not in taken]
    return left


FEEDBACK = ("On the AIS page choose the row → Optional (feedback): 'Information is not fully correct' (with the right "
            "value), 'Information relates to other PAN/year', 'Information is duplicate/included in other "
            "information' or 'Information is denied' [U: options as shown in the AIS app, not re-checked].")  # fmt: skip


def _sum(xs: Iterable[Decimal | None]) -> Decimal | None:
    vals = [x for x in xs if x is not None]
    return sum(vals, Decimal(0)) if vals else None


def _explain(g: _Group, ais: Decimal, app: Decimal, status: str) -> tuple[str, str]:
    """(likely cause, what to do) for a row that is not a match."""
    c = g.category
    if status == "only_ais":
        if c == "dividend":
            return ("No dividend from this payer in the app: a holding or a dividend the app does not have (or the "
                    "payer's name differs from the holding's).",
                    "If you received it, add the dividend in the app (or import the CAS/tradebook that has the "
                    "holding). If it is not yours, give AIS feedback. " + FEEDBACK)  # fmt: skip
        return (f"No {c} of this security in the app this year: a transaction is missing, sits in another year, or "
                "the security is named differently.",
                f"Add the missing {'sale' if c == 'sale' else 'purchase'} (import the tradebook/CAS for that "
                "account). If the AIS row is wrong, give AIS feedback. " + FEEDBACK)  # fmt: skip
    diff = ais - app
    if c == "dividend" and within(app, ais * (1 - TDS_RATE)):
        return ("The app amount is about 90 % of the AIS: the app has the dividend net of 10 % TDS (s.194/194K), "
                "the AIS reports it gross.",
                "Record the gross dividend in the app: dividends are taxed gross and the TDS is a credit against "
                "your tax (it shows in Form 26AS).")  # fmt: skip
    if c == "dividend":
        return ("The amounts differ: a dividend missing on one side, a different record date or FY, or a payer "
                "reporting the wrong amount.",
                "Compare the dates in the rows below with your bank credits; add a missing dividend, or give AIS "
                "feedback with the right value. " + FEEDBACK)  # fmt: skip
    charges = sum((e.charges for e in g.app), Decimal(0))
    if charges and within(abs(diff), charges):
        return ("The difference equals the charges on the app's trades: one side is net of brokerage/STT.",
                "Nothing to correct if the gross value is right; the ITR takes the full consideration and the "
                "charges as transfer expenses.")  # fmt: skip
    stt = _sum(i.stt for i in g.items)
    if stt and within(abs(diff), stt):
        return ("The difference equals the STT the AIS shows: one side includes the STT.",
                "Use the consideration before STT; STT on a sale is not a deductible expense (s.48).")  # fmt: skip
    q_ais, q_app = _sum(i.quantity for i in g.items), _sum(e.quantity for e in g.app)
    if q_ais is not None and q_app is not None and q_ais != q_app:
        return (f"Quantity differs (AIS {q_ais.normalize():f}, app {q_app.normalize():f}): a trade missing or in "
                "another FY/account, or a split/bonus counted on one side only.",
                "Add the missing trade or fix the quantity; if the AIS is wrong, give feedback. " + FEEDBACK)  # fmt: skip
    return ("Same securities, different value: a price entered wrongly, or an exchange/RTA reporting error.",
            "Check the contract notes or the CAS; fix the app's trade or give AIS feedback. " + FEEDBACK)  # fmt: skip


def _money(x: Decimal | None) -> float | None:
    return None if x is None else round(float(x), 2)


def _row(g: _Group) -> dict[str, Any]:
    ais = sum((i.amount for i in g.items), Decimal(0))
    app = sum((e.amount for e in g.app), Decimal(0))
    status = "only_ais" if not g.app else "matched" if within(ais, app) else "mismatch"
    cause, action = _explain(g, ais, app, status) if status != "matched" else (None, None)
    return {"category": g.category, "label": g.label, "isin": g.isin, "match": g.match, "status": status,
            "ais_amount": _money(ais), "app_amount": _money(app) if g.app else None,
            "diff": _money(ais - app) if g.app else None, "ais_tds": _money(_sum(i.tds for i in g.items)),
            "ais_quantity": _money(_sum(i.quantity for i in g.items)),
            "app_quantity": _money(_sum(e.quantity for e in g.app)),
            "holdings": sorted({e.name for e in g.app}), "duplicates_dropped": g.deduped,
            "ais_rows": [_ais_json(i) for i in sorted(g.items, key=lambda i: (i.day or date.min))],
            "app_rows": [{"day": e.day.isoformat(), "name": e.name, "amount": _money(e.amount),
                          "quantity": _money(e.quantity)} for e in sorted(g.app, key=lambda e: e.day)],
            "cause": cause, "action": action}  # fmt: skip


def _ais_json(i: AisItem) -> dict[str, Any]:
    return {"day": i.day.isoformat() if i.day else None, "code": i.code, "part": i.part, "source": i.source,
            "tan": i.tan, "security": i.security, "amount": _money(i.amount), "tds": _money(i.tds),
            "quantity": _money(i.quantity), "stt": _money(i.stt)}  # fmt: skip


def _only_app(entries: list[AppEntry]) -> list[dict[str, Any]]:
    by: dict[tuple[str, int], list[AppEntry]] = defaultdict(list)
    for e in entries:
        by[(e.category, e.holding_id)].append(e)
    out = []
    for (cat, _), es in by.items():
        out.append({"category": cat, "label": es[0].name, "isin": es[0].isin, "match": None, "status": "only_app",
                    "ais_amount": None, "app_amount": _money(sum((e.amount for e in es), Decimal(0))), "diff": None,
                    "ais_tds": None, "ais_quantity": None, "app_quantity": _money(_sum(e.quantity for e in es)),
                    "holdings": [es[0].name], "duplicates_dropped": 0, "ais_rows": [],
                    "app_rows": [{"day": e.day.isoformat(), "name": e.name, "amount": _money(e.amount),
                                  "quantity": _money(e.quantity)} for e in sorted(es, key=lambda e: e.day)],
                    "cause": "The AIS has no matching row: the payer/exchange may not have reported it yet (the AIS "
                             "fills in through the year), reported it under another name, or in another FY.",
                    "action": "Nothing to file if your record is right: report the income in the ITR anyway. "
                              "Re-download the AIS later to see if it appears."})  # fmt: skip
    return out


INFO_NOTES = {
    "interest": "The app does not track bank, deposit or bond interest. Check these against your bank/bond statements: "
                "interest is taxed at your slab rate under 'Income from other sources' (savings interest may get "
                "s.80TTA/80TTB relief).",
    "off_market": "Off-market transfers (a gift, a move between your own demat accounts) are not sales. Make sure the "
                  "app has the transfer with the original cost and date, or the lots and gains will be wrong.",
}  # fmt: skip
_ORDER = {"mismatch": 0, "only_ais": 1, "only_app": 2, "matched": 3}


def reconcile(items: list[AisItem], app: list[AppEntry], fy: int) -> dict[str, Any]:
    """The AIS check for one financial year (named by its end year). `items` are that year's AIS rows; `app` may hold
    any years (only `fy` is used)."""
    app_fy = [e for e in app if fiscal_year(e.day) == fy and e.category in COMPARED]
    groups = _groups(items, COMPARED)
    left = _assign(groups, app_fy)
    rows = [_row(g) for g in groups] + _only_app(left)
    rows.sort(key=lambda r: (_ORDER[r["status"]], COMPARED.index(r["category"]), -(r["ais_amount"] or 0),
                             -(r["app_amount"] or 0)))  # fmt: skip
    info = []
    for g in _groups(items, ("interest", "off_market")):
        total = sum((i.amount for i in g.items), Decimal(0))
        info.append({"category": g.category, "label": g.label, "ais_amount": _money(total),
                     "ais_tds": _money(_sum(i.tds for i in g.items)), "duplicates_dropped": g.deduped,
                     "ais_rows": [_ais_json(i) for i in g.items], "note": INFO_NOTES[g.category]})  # fmt: skip
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in _ORDER}
    totals = {}
    for c in COMPARED:
        rs = [r for r in rows if r["category"] == c]
        totals[c] = {"ais": _money(sum((Decimal(str(r["ais_amount"] or 0)) for r in rs), Decimal(0))),
                     "app": _money(sum((Decimal(str(r["app_amount"] or 0)) for r in rs), Decimal(0)))}  # fmt: skip
    return {"fy": fy, "label": fy_label(fy), "counts": counts, "totals": totals, "rows": rows, "info": info,
            "tolerance": f"max(₹{ABS_TOL}, {REL_TOL * 100:g} % of the AIS amount)",
            "notes": [FORMAT_NOTE,
                      "Sale values are compared gross (units × price, before brokerage and STT); the AIS shows STT "
                      "in its own column.",
                      "Dividends reported twice (the payer's TDS return and its SFT) are counted once.",
                      "Interest and off-market transfers are listed for you to check: the app has no record of them.",
                      "A personal check, not tax advice: the ITR must report income even if the AIS misses it."]}  # fmt: skip
