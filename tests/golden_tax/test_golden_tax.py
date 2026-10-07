"""Independent golden tax corpus (#240): every case in cases/*.json against the engine.

The cases were derived by hand from the Income-tax rules by an author who did not read the engine (fincalc.tax,
portfolio.lots, portfolio.tax); each carries its derivation and citations (README.md). This runner only maps a case's
inputs onto the engine and compares. An expectation is never edited to match the engine: where the engine and the
hand derivation disagree for a documented reason, the case's `known_divergence` names the field, the engine's value
and why, and the runner then requires exactly that engine value (so any further drift still fails).

Mapping: each holding's events -> portfolio.lots.Event -> build_lots (FIFO, splits, bonuses, intraday netting);
each disposal -> portfolio.tax.evaluate (classification, grandfathering); each financial year ->
portfolio.tax.fy_summary (set-off, exemption, tax, cess, completeness); ELSS -> portfolio.elss.unlock_date.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from finresearch.portfolio import elss
from finresearch.portfolio.lots import Event, build_lots
from finresearch.portfolio.tax import DisposalRow, HoldingTax, evaluate, fy_summary

CASES = sorted((Path(__file__).parent / "cases").glob("g*.json"))
CENT = Decimal("0.01")


def _dec(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x))


def _money(x: Any) -> Decimal | None:
    return None if x is None else Decimal(str(x)).quantize(CENT)


def _day(x: str | None) -> date | None:
    return None if x is None else date.fromisoformat(x)


def run_engine(case: dict[str, Any]) -> dict[str, Any]:
    """The engine's answer in the corpus's expected-value shape (only the fields the corpus defines)."""
    slab = Decimal(case.get("slab_rate", "0.30"))
    disposals, open_lots, rows, unlock = [], [], [], []
    for hid, h in enumerate(case["holdings"], start=1):
        evs, meta_of = [], {}
        for i, e in enumerate(h["events"], start=1):
            meta = dict(e.get("meta") or {})
            meta_of[i] = meta
            evs.append(Event(i, date.fromisoformat(e["day"]), e["kind"], _dec(e.get("quantity")), _dec(e.get("price")),
                             _dec(e.get("amount")), _dec(e.get("charges")) or Decimal(0),
                             bool(e.get("stt_paid", True)), meta))  # fmt: skip
        book = build_lots(evs)
        ht = HoldingTax(hid, h["name"], "Demat", None, h["tax_class"], bool(h.get("listed", True)),
                        _dec(h.get("fmv_2018")), bool(h.get("sgb_original_subscriber")))  # fmt: skip
        for d in book.disposals:
            m = meta_of.get(d.txn_id, {})
            r = evaluate(DisposalRow(ht, d.acquired, d.sold, d.quantity, d.cost, d.proceeds, d.stt_paid,
                                     "intraday" if d.intraday else d.origin,
                                     rbi_redemption=bool(m.get("rbi_redemption")),
                                     held_to_maturity=bool(m.get("held_to_maturity")), txn_id=d.txn_id))  # fmt: skip
            rows.append(r)
            c = r.cls
            intraday = r.origin == "intraday"
            term = "intraday" if intraday else c.term
            rate = None
            if term in ("short", "long"):
                rate = "slab" if c.rate is None else c.rate
            disposals.append({"holding": h["name"], "sold": r.sold, "acquired": r.acquired, "quantity": r.quantity,
                              "cost": r.cost, "tax_cost": r.tax_cost, "proceeds": r.proceeds,
                              "gain": None if intraday else r.gain, "term": term, "rate": rate})  # fmt: skip
        for lot in book.open_lots:
            open_lots.append({"holding": h["name"], "acquired": lot.acquired, "quantity": lot.open_quantity,
                              "cost_per_unit": lot.cost_per_unit})  # fmt: skip
        if h.get("elss"):
            unlock += [{"acquired": lot.acquired, "redeemable_from": elss.unlock_date(lot.acquired)}
                       for lot in book.lots if lot.acquired is not None]  # fmt: skip
    fys = []
    for fy in sorted({r.fy for r in rows}):
        x = fy_summary(rows, fy, slab)
        fys.append({"fy": fy, "exemption_used": x["exemption"]["used"], "tax": x["tax"], "cess": x["cess"],
                    "total": x["total"], "complete": x["complete"], "unknown_count": x["unknown"],
                    "intraday_count": x["intraday"], "exempt_total": x["exempt"],
                    "losses_carried_short": x["losses_carried"]["short"],
                    "losses_carried_long": x["losses_carried"]["long"]})  # fmt: skip
    return {"disposals": disposals, "open_lots": open_lots, "fy": fys, "elss_unlock": unlock}


MONEY = {"cost", "tax_cost", "proceeds", "gain", "cost_per_unit", "exemption_used", "tax", "cess", "total",
         "exempt_total", "losses_carried_short", "losses_carried_long"}  # fmt: skip
QTY = {"quantity"}
DATES = {"sold", "acquired", "redeemable_from"}


def _norm(key: str, v: Any) -> Any:
    if v is None:
        return None
    if key in MONEY:
        return _money(v)
    if key in QTY:
        return Decimal(str(v)).normalize()
    if key in DATES:
        return v if isinstance(v, date) else _day(v)
    if key == "rate":
        return v if v == "slab" else Decimal(str(v)).normalize()
    return v


def compare(case: dict[str, Any], got: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
    """{field path: (expected, engine)} for every expected field the engine answers differently. Only the keys a
    case states are asserted (the corpus omits what it considers not determinable)."""
    exp = case["expected"]
    out: dict[str, tuple[Any, Any]] = {}
    for section in ("disposals", "open_lots", "elss_unlock"):
        want, have = exp.get(section), got[section]
        if want is None:
            continue
        if len(want) != len(have):
            out[f"{section}.count"] = (len(want), len(have))
        for i, (w, g) in enumerate(zip(want, have, strict=False)):
            for k, v in w.items():
                if k == "holding":
                    continue
                a, b = _norm(k, v), _norm(k, g.get(k))
                if a != b:
                    out[f"{section}[{i}].{k}"] = (a, b)
    have_fy = {x["fy"]: x for x in got["fy"]}
    for w in exp.get("fy", []):
        g = have_fy.get(w["fy"])
        if g is None:
            out[f"fy[{w['fy']}]"] = ("present", None)
            continue
        for k, v in w.items():
            a, b = _norm(k, v), _norm(k, g.get(k))
            if a != b:
                out[f"fy[{w['fy']}].{k}"] = (a, b)
    return out


@pytest.mark.parametrize("path", CASES, ids=[p.stem for p in CASES])
def test_golden_case(path: Path):
    case = json.loads(path.read_text())
    assert case["derivation"] and case["rules"], "every case documents its hand derivation and the rule it applies"
    diffs = compare(case, run_engine(case))
    known = case.get("known_divergence") or {}
    for field, d in known.items():
        assert field in diffs, f"{field}: documented as a divergence but the engine now agrees; remove the entry"
        engine_now = diffs.pop(field)[1]
        assert engine_now == _norm(field.rsplit(".", 1)[-1], d["engine"]), (
            f"{field}: the engine moved from its documented value {d['engine']} to {engine_now}")
        assert d.get("reason"), f"{field}: a known divergence needs its reason"
    assert not diffs, "engine disagrees with the hand derivation:\n" + "\n".join(
        f"  {k}: expected {a}, engine {b}" for k, (a, b) in sorted(diffs.items()))


def test_corpus_size_and_coverage():
    assert len(CASES) >= 25
    covers = " ".join(" ".join(json.loads(p.read_text())["covers"]) for p in CASES).lower()
    for topic in ("fifo", "intraday", "split", "bonus", "grandfather", "sgb", "ncd", "debt mf", "unknown cost",
                  "unknown date", "elss", "stt", "set", "threshold"):  # fmt: skip
        assert topic in covers, topic
