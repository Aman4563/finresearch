"""Financial invariants (#221) on a synthetic portfolio: Example names, fake ISINs and scheme codes, test database.

The portfolio (valued on 30-Sep-2026):
- a CAMS CAS (tests/cas_synth.py): Example Flexi Cap Fund with a 50-unit opening balance of unknown cost, purchases
  and a redemption of 60 units (FIFO: the 50 opening units, then 10 bought); Example Short Duration Fund, 300 units;
- Example Split Ltd (stock): buy 2 @ ₹1,000 (5-Jan-2024) and 10 @ ₹1,000 (10-Jan-2024); sell 4 @ ₹1,200 (1-Mar-2024:
  the 2-unit lot sells out, 2 from the second); split ₹10 -> ₹2 (x5, 1-Jun-2024); bonus 1:1 (1-Sep-2024); an intraday
  pair: buy 5 @ ₹300 and sell 5 @ ₹310 on 3-Feb-2025; sell 30 @ ₹250 (2-Jun-2025, a partial sale);
- Example Bank Ltd (stock): buy 20 @ ₹400 (10-Jan-2025);
- Example ELSS Tax Saver Fund: 3 SIP lots of 10 units.

Split Ltd by hand, in today's units (x5 for anything before 1-Jun-2024): acquired 2 x 5 + 10 x 5 + 40 (bonus on the
40 held) + 5 (intraday) = 105; disposed (2 + 2) x 5 + 5 + 30 = 55; open 10 (second lot) + 40 (bonus) = 50.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from datetime import date
from decimal import Decimal as D

import numpy as np
import pytest
from cas_synth import PASSWORD, build_cas, sample_statement
from fastapi.testclient import TestClient

from finresearch.adapters.amfi import SchemeNav
from finresearch.adapters.nse import Quote
from finresearch.api.markets import MarketSources
from finresearch.portfolio.analytics_math import risk_contributions
from finresearch.portfolio.lots import Event, build_lots

TODAY = date(2026, 9, 30)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
ELSS = ("Example ELSS Tax Saver Fund - Direct Plan - Growth", "999901", "Equity Scheme - ELSS")
SPLIT_TXNS = [  # (day, kind, quantity, price, meta)
    ("2024-01-05", "buy", "2", "1000", {}),
    ("2024-01-10", "buy", "10", "1000", {}),
    ("2024-03-01", "sell", "4", "1200", {}),
    ("2024-06-01", "split", None, None, {"from": 10, "to": 2}),
    ("2024-09-01", "bonus", None, None, {"a": 1, "b": 1}),
    ("2025-02-03", "buy", "5", "300", {}),
    ("2025-02-03", "sell", "5", "310", {}),
    ("2025-06-02", "sell", "30", "250", {}),
]


# --------------------------------------------------------------------------- pure: the lot engine
def _factor_after(splits: list[tuple[date, D]], day: date) -> D:
    """Today's units per unit held on `day`: the product of the splits after it."""
    f = D(1)
    for d, x in splits:
        if d > day:
            f *= x
    return f


def test_units_conserved_through_split_bonus_partial_sale_and_intraday():
    evs = [Event(i, date.fromisoformat(d), k, D(q) if q else None, D(p) if p else None, meta=m)
           for i, (d, k, q, p, m) in enumerate(SPLIT_TXNS, 1)]  # fmt: skip
    book = build_lots(evs)
    splits = [(date(2024, 6, 1), D(5))]
    acquired = sum((lot.quantity for lot in book.lots), D(0))
    open_ = sum((lot.open_quantity for lot in book.lots), D(0))
    disposed = sum((d.quantity * _factor_after(splits, d.sold) for d in book.disposals), D(0))
    assert (acquired, open_, disposed) == (D(105), D(50), D(55))  # hand-computed above
    for lot in book.lots:  # and lot by lot (before #221 the sold-out 2-unit lot kept its pre-split count, 2)
        out = sum((d.quantity * _factor_after(splits, d.sold) for d in book.disposals if d.lot is lot), D(0))
        assert lot.quantity == lot.open_quantity + out, lot
    assert [d.intraday for d in book.disposals].count(True) == 1


def test_disposal_gain_is_proceeds_minus_cost():
    evs = [Event(i, date.fromisoformat(d), k, D(q) if q else None, D(p) if p else None, meta=m)
           for i, (d, k, q, p, m) in enumerate(SPLIT_TXNS, 1)]  # fmt: skip
    book = build_lots(evs)
    got = [(d.sold.isoformat(), d.quantity, d.cost, d.proceeds, d.gain) for d in book.disposals]
    # 1-Mar-2024: 2 + 2 units at ₹1,000 sold at ₹1,200 -> gain 400 each; intraday 5 x (310 - 300) = 50;
    # 2-Jun-2025: 30 units of the second lot at ₹200 (1,000 / 5) sold at ₹250 -> 30 x 50 = 1,500
    assert got == [("2024-03-01", D(2), D(2000), D(2400), D(400)), ("2024-03-01", D(2), D(2000), D(2400), D(400)),
                   ("2025-02-03", D(5), D(1500), D(1550), D(50)), ("2025-06-02", D(30), D(6000), D(7500), D(1500))]  # fmt: skip
    for d in book.disposals:
        assert d.gain == d.proceeds - d.cost


@pytest.mark.parametrize("n", [1, 2, 5, 12])
def test_risk_contributions_sum_to_one(n):
    rng = np.random.default_rng(221 + n)
    returns = rng.normal(0.0005, 0.01, size=(250, n)) + rng.normal(0, 0.005, size=(250, 1))  # a common factor
    w = rng.dirichlet(np.ones(n))
    rc, vol = risk_contributions(list(w), returns.tolist())
    assert abs(sum(rc) - 1) < 1e-9 and vol > 0
    if n == 1:
        assert rc == [pytest.approx(1.0)]


# --------------------------------------------------------------------------- the whole portfolio, through the API
class _NoActions:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def corporate_actions(self, symbol):
        return []


@pytest.fixture
def client(env):
    from sqlalchemy import text

    from finresearch.api import create_app
    from finresearch.db import session_scope

    navs = [("100001", "Example Flexi Cap Fund - Direct Plan - Growth", "INF000X01019", "130",
             "Equity Scheme - Flexi Cap Fund"),
            ("100002", "Example Short Duration Fund - Direct Plan - Growth", "INF000X01027", "105",
             "Debt Scheme - Short Duration Fund"),
            (ELSS[1], ELSS[0], None, "50", ELSS[2])]  # fmt: skip

    async def nav_all():
        return [SchemeNav(code, name, "Direct", "Growth", isin, None, D(nav), date(2026, 9, 29), cat, "Example MF")
                for code, name, isin, nav, cat in navs]  # fmt: skip

    async def quote(symbol):
        px = {"EXSPLIT": (D(260), D(10**9), "Textiles"), "EXBANK": (D(480), D(10**8), "Banks")}
        if symbol not in px:
            raise LookupError(symbol)
        p, shares, ind = px[symbol]
        return Quote(symbol=symbol, last_price=p, issued_shares=shares, industry=ind)

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, "
                       "portfolio_import, portfolio_snapshot, portfolio_setting"))  # fmt: skip
    app = create_app(nav_all=nav_all)
    app.state.markets = MarketSources(quote=quote, equity=lambda: _NoActions(), today=lambda: TODAY)
    with TestClient(app) as c:
        yield c


def _add(c, **kw) -> int:
    r = c.post("/api/portfolio/transactions", headers=ORIGIN, json={"account": "Manual", **kw})
    assert r.status_code == 201, r.text
    return r.json()["holding_id"]


def _build(c) -> dict:
    pdf = build_cas(sample_statement())
    done = c.post("/api/portfolio/import/cas", headers=ORIGIN, json={"filename": "cas.pdf", "password": PASSWORD,
                  "content_b64": base64.b64encode(pdf).decode(), "dry_run": False}).json()  # fmt: skip
    hid = None
    for d, k, q, p, m in SPLIT_TXNS:
        who = {"holding_id": hid} if hid else {"name": "Example Split Ltd", "isin": "INE000X01011",
                                               "nse_symbol": "EXSPLIT"}  # fmt: skip
        hid = _add(c, asset_type="stock", day=d, kind=k, **who, **({"quantity": q, "price": p} if q else {}),
                   **({"meta": m} if m else {}))  # fmt: skip
    _add(c, asset_type="stock", name="Example Bank Ltd", isin="INE000X02019", nse_symbol="EXBANK", day="2025-01-10",
         kind="buy", quantity="20", price="400")  # fmt: skip
    eh = None
    for d in ("2023-08-10", "2023-09-10", "2024-08-12"):
        who = {"holding_id": eh} if eh else {"name": ELSS[0], "scheme_code": ELSS[1]}
        eh = _add(c, asset_type="mf", day=d, kind="buy", quantity="10", price="20", **who)
    return done


def test_portfolio_invariants(client):
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioDisposal, PortfolioHolding, PortfolioLot, PortfolioTxn
    from finresearch.portfolio.service import lot_units

    done = _build(client)
    # statement closing units = lot units after a reconciled import
    assert done["reconciled"] and done["reconciliation"]
    for r in done["reconciliation"]:
        assert r["ok"] and D(r["statement_units"]) == D(r["lot_units"]), r
    with session_scope() as s:
        units = lot_units(s)
        for r in done["reconciliation"]:
            assert units[(r["ikey"], r["account"])] == D(r["statement_units"])
        # acquired (split/bonus-adjusted) = open + disposed, holding by holding and lot by lot
        splits: dict[int, list[tuple[date, D]]] = defaultdict(list)
        for t in s.scalars(select(PortfolioTxn).where(PortfolioTxn.kind == "split")):
            splits[t.holding_id].append((t.day, D(str(t.meta["from"])) / D(str(t.meta["to"]))))
        holdings = s.scalars(select(PortfolioHolding)).all()
        assert len(holdings) == 5
        for h in holdings:
            lots = s.scalars(select(PortfolioLot).where(PortfolioLot.holding_id == h.id)).all()
            disp = s.scalars(select(PortfolioDisposal).where(PortfolioDisposal.holding_id == h.id)).all()
            assert all(d.lot_id is not None for d in disp)  # no oversell in this portfolio
            for lot in lots:
                out = sum((d.quantity * _factor_after(splits[h.id], d.sold) for d in disp if d.lot_id == lot.id), D(0))
                assert lot.quantity == lot.open_quantity + out, (h.name, lot.acquired)
            # disposal gain = proceeds - cost wherever the cost is known (the opening units' cost is not)
            for d in disp:
                if d.cost is not None:
                    lot = next(x for x in lots if x.id == d.lot_id)
                    # the lot's cost per unit is in today's units; a sale before a split sold pre-split units
                    want = lot.cost_per_unit * d.quantity * _factor_after(splits[h.id], d.sold)
                    assert abs(d.cost - want) <= D("0.01"), (h.name, d.sold)
        split_lots = [x for h in holdings if h.name == "Example Split Ltd"
                      for x in s.scalars(select(PortfolioLot).where(PortfolioLot.holding_id == h.id))]  # fmt: skip
        assert sum((x.quantity for x in split_lots), D(0)) == 105 and sum((x.open_quantity for x in split_lots), D(0)) == 50
    snap = client.get("/api/portfolio").json()
    rows = [h for h in snap["holdings"] if h["value"] is not None]
    assert len(rows) == 5  # every holding is priced
    total = snap["summary"]["value"]
    # sum of holding values = portfolio value (each value rounded to the paisa: at most 0.005 per row)
    assert abs(sum(h["value"] for h in rows) - total) <= 0.005 * len(rows) + 1e-9
    # allocation shares (asset, sector, cap) each sum to 100 %
    for dim in ("asset", "sector", "cap"):
        vals = [x["value"] for x in snap["allocation"][dim]]
        assert abs(sum(vals) - total) <= 0.005 * len(vals) + 1e-9, dim
        assert abs(sum(v / total * 100 for v in vals) - 100) <= 0.01, dim
    # the tax view: gain = sale value - cost for tax, and = proceeds - cost (no grandfathering here)
    tax = client.get("/api/portfolio/tax").json()
    for d in tax["disposals"]:
        if d["cost"] is not None and d["term"] != "exempt":
            assert d["tax_cost"] == d["cost"] and d["gain"] == pytest.approx(d["proceeds"] - d["cost"], abs=0.01)
        else:
            assert d["gain"] is None and d["term"] == "unknown"  # the 50 opening units: never a ₹0 gain
    realised = sum(h["realised"] for h in snap["holdings"])
    assert realised == pytest.approx(snap["summary"]["realised"], abs=0.01 * len(snap["holdings"]))
