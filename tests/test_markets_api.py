"""Market-data routes behind the stock, fund and bond pages: NSE and AMFI are faked from recorded fixtures (or
generated NAV series), so nothing here touches the network."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.amfi import SchemeNav, parse_nav_all
from finresearch.adapters.nse import Quote
from finresearch.adapters.nse_bonds import parse_live_bonds
from finresearch.adapters.nse_equity import (
    CorporateAction,
    PriceBar,
    ResultFiling,
    Shareholding,
)
from finresearch.api.markets import MarketSources
from finresearch.fincalc import bonds as b
from finresearch.fincalc import funds

FIX = Path(__file__).parent / "fixtures"
EQ = FIX / "nse" / "equity"
TODAY = date(2026, 9, 28)


def load(name: str):
    return json.loads((EQ / name).read_text())


class FakeEquity:
    """NseEquity's interface over recorded INFY payloads; counts history calls to check chunking and caching."""

    def __init__(self, log: list, cap: int = 5):
        self.log, self.cap = log, cap  # like NSE (~70 rows), only the latest `cap` rows of a range come back

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def history(self, symbol, start, end, series="EQ"):
        self.log.append(("history", symbol, start, end))
        bars = [PriceBar.parse(r) for r in load("history_INFY_20260901_20260928.json")]
        return sorted((x for x in bars if start <= x.day <= end), key=lambda x: x.day)[-self.cap :]

    async def shareholding(self, symbol):
        return sorted((Shareholding.parse(r) for r in load("shareholding_INFY.json")),
                      key=lambda x: x.as_of or date.min, reverse=True)  # fmt: skip

    async def corporate_actions(self, symbol):
        return [CorporateAction.parse(r) for r in load("actions_INFY.json")]

    async def announcements(self, symbol):
        raise RuntimeError("NSE refused")  # one failing part must not blank the overview

    async def results(self, symbol, period="Quarterly"):
        return [ResultFiling.parse(r) for r in load("results_INFY_trimmed.json")]

    async def fetch_bytes(self, url):
        self.log.append(("xbrl", url))
        return (EQ / "results_INFY_Q3FY25_consolidated.xml").read_bytes()


def quote_fixture() -> Quote:
    return Quote.parse(json.loads((FIX / "nse" / "quote_INFY_20260928.json").read_text()))


def nav_series(start: date, end: date) -> list[tuple[date, Decimal]]:
    """Weekday NAVs growing about 12% a year with a regular wiggle (and one 20% dip) - deterministic."""
    out, d, i = [], start, 0
    while d <= end:
        if d.weekday() < 5:
            t = (d - start).days / 365
            dip = Decimal("0.8") if date(2023, 3, 1) <= d <= date(2023, 6, 30) else Decimal(1)
            wiggle = Decimal(1) + Decimal((i % 11) - 5) / 400
            out.append((d, (Decimal(100) * Decimal(1.12**t) * wiggle * dip).quantize(Decimal("0.0001"))))
            i += 1
        d += timedelta(days=1)
    return out


@pytest.fixture
def app_client(env):
    from finresearch.api import create_app

    log: list = []
    schemes = parse_nav_all((FIX / "amfi" / "NAVAll_trimmed.txt").read_text())
    me = next(x for x in schemes if x.code == "120505")
    navs = nav_series(date(2019, 9, 1), me.day)

    async def nav_all():
        return schemes

    async def bonds():
        return parse_live_bonds(json.loads((FIX / "nse" / "bonds_live_trimmed.json").read_text()))

    async def quote(symbol):
        return quote_fixture()

    async def nav_history(scheme, start, end):
        log.append(("navs", scheme.code, start, end))
        return [SchemeNav(scheme.code, scheme.name, scheme.plan, scheme.option, None, None, v, d, scheme.category,
                          scheme.amc) for d, v in navs if start <= d <= end]  # fmt: skip

    async def navs_on(day):
        # every direct-growth peer: NAV on `day` is 1/1.10**years of today's (a clean 10%/yr), ours from `navs`
        out = {}
        for x in schemes:
            if x.nav and x.day and x.category == me.category:
                yrs = round((x.day - day).days / 365.25)
                out[x.code] = SchemeNav(x.code, x.name, x.plan, x.option, None, None,
                                        (x.nav / Decimal("1.10") ** yrs).quantize(Decimal("0.0001")), day, x.category,
                                        x.amc)  # fmt: skip
        mine = funds.nav_on_or_before(navs, day)
        out[me.code] = SchemeNav(
            me.code, me.name, me.plan, me.option, None, None, mine[1], mine[0], me.category, me.amc
        )
        return out

    app = create_app(nav_all=nav_all, bonds=bonds)
    app.state.markets = MarketSources(equity=lambda: FakeEquity(log), quote=quote, nav_history=nav_history,
                                      navs_on=navs_on, today=lambda: TODAY)  # fmt: skip
    with TestClient(app) as c:
        yield c, log, me, navs


# --------------------------------------------------------------------------- stocks
def test_stock_overview_combines_quote_holding_actions_and_survives_a_failed_part(app_client):
    c, _, _, _ = app_client
    r = c.get("/api/stocks/infy/overview").json()
    q = r["quote"]
    assert q["last_price"] == "1003.2" and q["previous_close"] == "1000.2" and q["change"] == "3"
    assert abs(q["change_pct"] - 0.2999) < 1e-3
    assert q["issued_shares"] == "4058232462" and q["market_cap"] == str(
        round(Decimal("4058232462") * Decimal("1003.2"))
    )
    assert r["shareholding"][0]["as_of"] == "2026-06-30" and r["shareholding"][0]["promoter_pct"] == 13.82
    assert all(a["ex_date"] >= b_["ex_date"] for a, b_ in zip(r["corporate_actions"], r["corporate_actions"][1:],
                                                              strict=False) if a["ex_date"] and b_["ex_date"])  # fmt: skip
    assert r["announcements"] == [] and r["errors"][0].startswith("announcements: RuntimeError")
    assert c.get("/api/stocks/bad sym!/overview").status_code == 422


def test_stock_history_walks_back_past_nse_row_cap_and_caches(app_client):
    c, log, _, _ = app_client
    r = c.get("/api/stocks/INFY/history", params={"days": 800}).json()
    calls = [x for x in log if x[0] == "history"]
    # at most a year per request (NSE 404s longer ranges); 19 bars come back 5 at a time, then empty windows
    # step back a year at a time until the start
    assert calls[0][3] == TODAY and all((x[3] - x[2]).days <= 360 for x in calls)
    assert calls[-1][2] == TODAY - timedelta(days=800) and len(calls) == 7
    assert all(later[3] < earlier[2] or later[3] < earlier[3] for earlier, later in pairwise(calls))
    assert r["bars"][-1] == {"date": "2026-09-28", "close": 1003.2, "open": 1002.0, "high": 1009.5, "low": 986.6,
                             "volume": 8567672.0}  # fmt: skip
    assert len(r["bars"]) == 19 and r["week52_high"] == "1728" and r["stats"]["points"] == 19
    assert r["stats"]["max_drawdown"] <= 0 and r["stats"]["annualised_volatility"] > 0
    c.get("/api/stocks/INFY/history", params={"days": 800})
    assert len([x for x in log if x[0] == "history"]) == 7  # served from the cache


def test_stock_results_reads_quarterly_xbrl(app_client):
    c, log, _, _ = app_client
    r = c.get("/api/stocks/INFY/results", params={"quarters": 2}).json()
    assert r["quarters"], r
    q = r["quarters"][-1]
    assert q["revenue"] and q["revenue"] > 0 and q["profit"] and 0 < q["margin"] < 1 and q["eps"] > 0
    assert all(u.startswith("https://nsearchives.nseindia.com/") for k, u in log if k == "xbrl")


# --------------------------------------------------------------------------- funds
def test_fund_analytics_returns_rolling_and_risk(app_client):
    c, _, me, navs = app_client
    r = c.get(f"/api/funds/{me.code}/analytics", params={"years": 5, "rf": "0.065"}).json()
    assert r["scheme"]["scheme_code"] == me.code and r["scheme"]["category"] == me.category
    assert r["navs"][-1]["date"] == me.day.isoformat()
    within = [(d, v) for d, v in navs if d >= date.fromisoformat(r["navs"][0]["date"])]
    assert r["trailing"]["3y"] == round(float(funds.trailing_return(within, 3)), 6)
    assert 0.08 < r["trailing"]["5y"] < 0.16
    roll = r["rolling"]["1y"]
    assert (
        roll["count"] == len(roll["series"]) and sum(h["count"] for h in roll["histogram"]) == roll["count"]
    )
    assert roll["min"] <= roll["p25"] <= roll["median"] <= roll["p75"] <= roll["max"]
    risk = r["risk"]
    assert -0.3 < risk["max_drawdown"] < -0.15 and risk["drawdown_trough"] >= "2023-03-01"
    assert risk["sharpe"] is not None and risk["risk_free_annual"] == 0.065
    assert c.get("/api/funds/999999/analytics").status_code == 404
    assert c.get("/api/funds/abc/analytics").status_code == 422


def test_fund_sip_matches_fincalc_and_compares_lump_sum(app_client):
    c, log, me, navs = app_client
    r = c.get(f"/api/funds/{me.code}/sip", params={"amount": 5000, "years": 3, "day": 5}).json()
    from finresearch.fincalc.dates import add_years

    within = [(d, v) for d, v in navs if add_years(TODAY, -5) - timedelta(days=10) <= d <= TODAY]
    want = funds.sip_outcome(within, 5000, add_years(me.day, -3), me.day, 5)
    assert r["sip"]["instalments"] == want.instalments == 36
    assert r["sip"]["invested"] == 180000.0 and r["sip"]["value"] == round(float(want.value), 2)
    assert r["sip"]["xirr"] == round(float(want.xirr), 6)
    assert r["path"][-1]["value"] == r["sip"]["value"] and r["path"][-1]["lump_sum"] == r["lump_sum"]["value"]
    assert r["lump_sum"]["invested"] == 180000.0 and r["lump_sum"]["cagr"] > 0
    assert len([x for x in log if x[0] == "navs"]) == 1  # one NAV download shared by both requests


def test_fund_peers_ranks_the_scheme_in_its_category(app_client):
    c, _, me, _ = app_client
    r = c.get(f"/api/funds/{me.code}/peers").json()
    assert r["category"] == me.category and r["peers"] >= 1
    p = r["periods"]["1y"]
    assert p["count"] >= 1 and p["scheme"] is not None and 1 <= p["rank"] <= p["count"]
    assert p["p25"] <= p["median"] <= p["p75"]


# --------------------------------------------------------------------------- bonds
def test_bond_analytics_matches_fincalc_with_cash_flows_curve_and_tax(app_client):
    c, _, _, _ = app_client
    r = c.get("/api/bonds/INE906B07DF8/analytics", params={"freq": 1, "tax_slab_pct": 30}).json()
    a = r["analytics"]
    s, mat, cpn, face = TODAY, date(2029, 2, 5), Decimal("0.0875"), Decimal(1000)
    ai = b.accrued_interest(s, mat, cpn, 1, face)
    clean = Decimal("1123.99") - ai
    assert a["accrued_interest"] == round(float(ai), 4) == 56.3356 and a["clean_price"] == 1067.6544
    assert a["dirty_price"] == 1123.99 and r["basis"] == "dirty"
    assert a["ytm"] == round(float(b.ytm(clean, s, mat, cpn, 1, face)), 6)
    assert r["tax_rate"] == 0.312  # 30% slab + 4% cess
    assert a["after_tax_ytm"] == round(
        float(b.after_tax_ytm(clean, s, mat, cpn, 1, Decimal("0.312"), face=face)), 6
    )
    assert a["after_tax_ytm"] < a["ytm"]
    flows = a["cash_flows"]
    assert [f["date"] for f in flows] == ["2027-02-05", "2028-02-05", "2029-02-05"]
    assert flows[-1]["principal"] == 1000 and flows[0]["coupon"] == 87.5
    at_ytm = next(p for p in a["curve"] if p["bp"] == 0)
    assert abs(at_ytm["dirty"] - 1123.99) < 0.01  # the curve passes through today's price
    ups = [x for x in a["sensitivity"] if x["bp"] > 0]
    assert (
        all(x["change_pct"] < 0 for x in ups)
        and abs(ups[0]["change_pct"] - ups[0]["duration_estimate_pct"]) < 0.01
    )


def test_bond_analytics_validates_and_refuses_partly_redeemed(app_client):
    c, _, _, _ = app_client
    assert c.get("/api/bonds/INE906B07DF8/analytics", params={"freq": 3}).status_code == 422
    assert c.get("/api/bonds/INE906B07DF8/analytics", params={"basis": "cum"}).status_code == 422
    assert c.get("/api/bonds/NOTANISIN/analytics").status_code == 422
    assert c.get("/api/bonds/INE000000000/analytics").status_code == 404
    partly = c.get("/api/bonds/INE148I07RB1/analytics", params={"freq": 12}).json()
    assert partly["analytics"] is None and "partly redeemed" in partly["error"]
    clean = c.get("/api/bonds/INE906B07DF8/analytics", params={"basis": "clean", "tax_slab_pct": 0}).json()
    assert clean["analytics"]["clean_price"] == 1123.99 and clean["tax_rate"] == 0
    # with no slab given, the stored profile's slab applies (default 30%)
    assert c.get("/api/bonds/INE906B07DF8/analytics").json()["tax_slab_pct"] in ("30", "30.0")


def test_stock_history_keeps_what_came_back_when_nse_refuses_an_older_piece(app_client, monkeypatch):
    from finresearch.api import markets

    c, log, _, _ = app_client

    class Flaky(FakeEquity):
        async def history(self, symbol, start, end, series="EQ"):
            if end < TODAY:  # the first (latest) piece works, older pieces are refused
                raise RuntimeError("NSE HTTP 404")
            return await super().history(symbol, start, end, series)

    monkeypatch.setattr(markets, "_retry", lambda make, wait_s=0: make())
    c.app.state.markets.equity = lambda: Flaky(log)
    r = c.get("/api/stocks/TCS/history", params={"days": 400}).json()
    assert r["partial"] is True and len(r["bars"]) == 5 and r["bars"][-1]["date"] == "2026-09-28"
