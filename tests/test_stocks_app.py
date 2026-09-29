"""Stocks in the app: search NSE's equity list, add a company, watch it, and the daily after-close check."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.nse_equity import (
    Announcement,
    CorporateAction,
    PriceBar,
    Shareholding,
    parse_equity_list,
    search_equities,
)
from finresearch.fincalc.dates import IST

EQ = Path(__file__).parent / "fixtures" / "nse" / "equity"


def equity_text() -> str:
    return (EQ / "EQUITY_L_head.csv").read_text()


def test_equity_list_search_ranks_symbol_then_name():
    eq = parse_equity_list(equity_text())
    infy = next(e for e in eq if e.symbol == "INFY")
    assert infy.name == "Infosys Limited" and infy.isin == "INE009A01021" and infy.listed == date(1995, 2, 8)
    assert [e.symbol for e in search_equities(eq, "infy")][:1] == ["INFY"]
    assert "INFY" in [e.symbol for e in search_equities(eq, "infosys")]
    assert [e.symbol for e in search_equities(eq, "tata consultancy")] == ["TCS"]
    assert search_equities(eq, "   ") == []


@pytest.fixture
def client(env):
    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, Company, MonitorJob, Watch

    with session_scope() as s:
        for m in (Alert, MonitorJob, Watch):
            s.query(m).delete()
        # other tests' companies may use these symbols (with documents attached): detach the symbol instead
        s.query(Company).filter(Company.nse_symbol.in_(["INFY", "TCS"])).update({"nse_symbol": None})
        import uuid

        s.query(Company).filter(Company.slug == "infosys").update({"slug": f"infosys-{uuid.uuid4().hex[:8]}"})

    async def eq_list():
        return equity_text()

    with TestClient(create_app(equity_list=eq_list)) as c:
        yield c


def test_search_add_and_watch_a_stock(client):
    hits = client.get("/api/stocks/search", params={"q": "infosys"}).json()
    assert hits[0]["symbol"] == "INFY" and hits[0]["slug"] is None
    made = client.post("/api/companies", json={"nse_symbol": "infy"}).json()
    assert made == {"slug": "infosys", "name": "Infosys Limited", "nse_symbol": "INFY", "created": True}
    again = client.post("/api/companies", json={"nse_symbol": "INFY"}).json()
    assert again["created"] is False and again["slug"] == "infosys"
    assert client.get("/api/stocks/search", params={"q": "INFY"}).json()[0]["slug"] == "infosys"
    assert client.post("/api/companies", json={"nse_symbol": "NOPE"}).status_code == 404
    w = client.post("/api/watches", json={"company": "infosys", "kind": "stock"}).json()
    assert w["kind"] == "stock" and w["open_date"] is None and w["active"]
    rows = client.get("/api/watches").json()
    assert rows[0]["nse_symbol"] == "INFY" and rows[0]["kind"] == "stock"


def _bar(d, close):
    return PriceBar(day=d, open=None, high=None, low=None, close=Decimal(close), prev_close=None, vwap=None,
                    volume=None, value_inr=None, trades=None)  # fmt: skip


class Market:
    def __init__(self):
        self.snap = {
            "bars": [_bar(date(2026, 9, 25), "1000"), _bar(date(2026, 9, 28), "1003.2")],
            "announcements": [Announcement(symbol="INFY", at=None, category="Outcome", text="", attachment=None,
                                           results_period_end=date(2026, 6, 30))],
            "actions": [CorporateAction(symbol="INFY", subject="Dividend - Rs 25 Per Share", ex_date=date(2026, 6, 10),
                                        record_date=date(2026, 6, 10), dividend_per_share=Decimal(25))],
            "shareholding": [Shareholding(symbol="INFY", as_of=date(2026, 6, 30), promoter_pct=Decimal("13.82"),
                                          public_pct=None, employee_trusts_pct=None, submitted=None, xbrl=None)],
        }  # fmt: skip

    async def snapshot(self, symbol):
        return self.snap


async def test_stock_daily_check_alerts_only_on_changes(client):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import tick

    client.post("/api/companies", json={"nse_symbol": "INFY"})
    client.post("/api/watches", json={"company": "infosys", "kind": "stock"})
    mk = Market()
    deps = Deps(ipo_detail=None, quote=None, stock_snapshot=mk.snapshot)

    stats = await tick(deps, datetime(2026, 9, 28, 16, 35, tzinfo=IST))
    assert stats["done"] == 1  # today's after-close check; the history is recorded without alerts
    with session_scope() as s:
        assert s.query(Alert).count() == 0
        slots = sorted(j.slot for j in s.query(MonitorJob))
    assert (
        slots[0] == "INFY:stock_daily:2026-09-28" and "INFY:stock_daily:2026-10-01" in slots
    )  # 3 days ahead

    mk.snap["announcements"].append(Announcement(symbol="INFY", at=None, category="Outcome", text="", attachment=None,
                                                 results_period_end=date(2026, 9, 30)))  # fmt: skip
    mk.snap["actions"].append(CorporateAction(symbol="INFY", subject="Interim Dividend - Rs 24 Per Share",
                                              ex_date=date(2026, 10, 2), record_date=date(2026, 10, 2),
                                              dividend_per_share=Decimal(24)))  # fmt: skip
    mk.snap["shareholding"].insert(0, Shareholding(symbol="INFY", as_of=date(2026, 9, 30), promoter_pct=Decimal("12.70"),
                                                   public_pct=None, employee_trusts_pct=None, submitted=None, xbrl=None))  # fmt: skip
    mk.snap["bars"].append(_bar(date(2026, 9, 29), "940"))
    await tick(deps, datetime(2026, 9, 29, 16, 35, tzinfo=IST))
    with session_scope() as s:
        alerts = {a.kind: (a.level, a.message) for a in s.query(Alert)}
    assert alerts["results"][0] == "action" and "2026-09-30" in alerts["results"][1]
    assert "Interim Dividend - Rs 24" in alerts["corporate_action"][1]
    assert alerts["ex_date_soon"][0] == "action" and "goes ex on 2026-10-02" in alerts["ex_date_soon"][1]
    assert alerts["holding_change"][0] == "warn" and "-1.12 pp" in alerts["holding_change"][1]
    assert alerts["big_move"][0] == "warn" and "-6.30%" in alerts["big_move"][1]
    await tick(deps, datetime(2026, 9, 30, 16, 35, tzinfo=IST))
    with session_scope() as s:
        assert s.query(Alert).count() == len(alerts)  # nothing new, nothing repeated


async def test_first_check_still_flags_an_imminent_ex_date(client):
    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import tick

    client.post("/api/companies", json={"nse_symbol": "INFY"})
    client.post("/api/watches", json={"company": "infosys", "kind": "stock"})
    mk = Market()
    mk.snap["actions"].append(CorporateAction(symbol="INFY", subject="Interim Dividend - Rs 24 Per Share",
                                              ex_date=date(2026, 10, 2), record_date=date(2026, 10, 2),
                                              dividend_per_share=Decimal(24)))  # fmt: skip
    mk.snap["bars"].append(_bar(date(2026, 9, 29), "940"))  # a big move, but no baseline yet
    deps = Deps(ipo_detail=None, quote=None, stock_snapshot=mk.snapshot)
    await tick(deps, datetime(2026, 9, 29, 16, 35, tzinfo=IST))
    await tick(deps, datetime(2026, 9, 30, 16, 35, tzinfo=IST))
    with session_scope() as s:
        alerts = [(a.kind, a.message) for a in s.query(Alert)]
    assert [k for k, _ in alerts] == ["ex_date_soon"] and "goes ex on 2026-10-02" in alerts[0][1]
