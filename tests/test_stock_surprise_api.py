"""GET /api/stocks/{symbol}/surprises on a synthetic company (Example Ltd), offline (#181)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from finresearch.adapters.nse_equity import ResultFiling
from finresearch.fincalc import surprise as S

ENDS = [S.quarter_back(date(2026, 6, 30), 15 - k) for k in range(16)]  # Sep-2022 .. Jun-2026
EPS = [5, 6, 7, 8, 6, 8, 7, 10, 9, 8, 10, 11, 15, 9, 11, 13]  # the last four quarters: Sep-25 .. Jun-26


def _row(end: date, cons: str, when: str) -> dict:
    return {"toDate": end.strftime("%d-%b-%Y"), "fromDate": S.quarter_back(end, 1).strftime("%d-%b-%Y"),
            "consolidated": cons, "filingDate": when, "audited": "Un-Audited",
            "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/EX_{end:%Y%m}_{cons[:3]}.xml"}  # fmt: skip


class FakeEq:
    def __init__(self):
        self.calls: list[str] = []
        self.sessions = [date(2026, 7, 1) + timedelta(days=k) for k in range(40)]
        self.sessions = [d for d in self.sessions if d.weekday() < 5]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def results(self, symbol, period="Quarterly"):
        # every quarter: standalone 15:00 IST then consolidated 19:00 IST, 20 days after the quarter end
        rows = []
        for e in ENDS:
            day = (e + timedelta(days=20)).strftime("%d-%b-%Y")
            rows += [_row(e, "Non-Consolidated", f"{day} 15:00"), _row(e, "Consolidated", f"{day} 19:00")]
        return [ResultFiling.parse(r) for r in rows]

    async def integrated_filings(self, symbol, kind=None):
        return []

    async def corporate_actions(self, symbol):
        return []

    async def history(self, symbol, start, end, series="EQ"):
        self.calls.append(symbol)
        # sessions from Wed 1-Jul-2026; t0 = Mon 20-Jul is the 14th: the stock moves 100 -> 108 over t0, t0+1
        px = {"EXAMPLE": [100] * 13 + [104, 108, 110, 110], "NIFTYBEES": [50] * 13 + [50, 51, 51, 51]}[symbol]
        days = self.sessions[: len(px)]
        return [SimpleNamespace(day=d, close=c) for d, c in zip(days, px, strict=True) if start <= d <= end]


@pytest.fixture
def client(env, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.api import markets as M
    from finresearch.evals import earnings_harvest as H
    from finresearch.signals import stock_surprise as SS

    async def fake_xbrl(eq, url):
        end = date(int(url.split("_")[1][:4]), int(url.split("_")[1][4:]), 1)
        k = next(i for i, e in enumerate(ENDS) if (e.year, e.month) == (end.year, end.month))
        return json.dumps({"k": k, "cons": url.endswith("_Con.xml")}).encode()

    def facts(data: bytes):
        d = json.loads(data)
        e = ENDS[d["k"]]
        return {"start": S.quarter_back(e, 1).isoformat(), "end": e.isoformat(),
                "eps": float(EPS[d["k"]]) * (1 if d["cons"] else 0.5), "revenue_basis": "revenue_from_operations",
                "revenue": 1e9 * EPS[d["k"]]}  # fmt: skip

    monkeypatch.setattr(M, "_xbrl", fake_xbrl)
    monkeypatch.setattr(H, "_quarter_facts", facts)
    monkeypatch.setattr(SS, "experiment", lambda: {"passes": {"primary": False, "tradable": False, "promote": False},
                                                   "reference": {"cutoffs": [-2, -1, -.5, -.2, 0, .2, .5, 1, 2], "n": 900}})  # fmt: skip
    app = create_app()
    eq = FakeEq()
    app.state.markets = M.MarketSources(equity=lambda: eq)
    with TestClient(app) as c:
        yield c, eq


def test_surprises_api_hand_computed(client):
    c, eq = client
    r = c.get("/api/stocks/EXAMPLE/surprises").json()
    assert r["status"] == "ok" and r["experimental"] is True and r["label"].startswith("Experimental")
    assert r["basis"] == "consolidated"  # consolidated filed for every quarter
    assert [q["label"] for q in r["quarters"]] == ["Q2 FY26", "Q3 FY26", "Q4 FY26", "Q1 FY27"]
    last = r["latest"]
    # Jun-26 13 vs Jun-25 11: d = 2; past differences 1,1,6,1,3,0,3,2: mean 2.125, squared deviations 24.875,
    # s = sqrt(24.875 / 7) = 1.885092 -> SUE = 1.060956 (consolidated EPS, not the standalone half)
    assert last["eps"] == 13 and last["eps_year_ago"] == 11
    assert last["sue"] == pytest.approx(2 / (24.875 / 7) ** 0.5) and last["sue"] == pytest.approx(
        1.060956, abs=1e-6
    )
    assert last["revenue_sue"] == pytest.approx(last["sue"])
    assert last["decile"] == 9  # above the 1.0 cut-off, below 2.0
    # the standalone came out at 15:00 IST on Mon 20-Jul, before the close: t0 is that day
    assert last["t0"] == "2026-07-20"
    assert last["reaction"] == pytest.approx(108 / 100 - 51 / 50)  # close of 17-Jul -> close of 21-Jul
    assert eq.calls == ["EXAMPLE", "NIFTYBEES"]
    # cached: a second call fetches nothing
    c.get("/api/stocks/EXAMPLE/surprises")
    assert eq.calls == ["EXAMPLE", "NIFTYBEES"]
