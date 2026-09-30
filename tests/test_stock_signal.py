"""The stock signal provider, its forensic and backtest routes, and the 'since this report' strip: offline, with
recorded XBRL fixtures, synthetic prices and a frozen clock."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from finresearch.adapters.nse import Quote
from finresearch.adapters.nse_equity import Announcement, PriceBar
from finresearch.adapters.xbrl import parse_results_xbrl
from finresearch.signals import registry
from finresearch.signals import stock as st
from finresearch.suggest.profile import Profile

EQ = Path(__file__).parent / "fixtures" / "nse" / "equity"
IST_NOON = datetime(2026, 9, 30, 6, 30, tzinfo=UTC)

BUCKETS = {
    "uptrend / strong momentum": {"n": 1200, "hits": 720, "p": 0.6, "n_effective": 100, "wilson95": [0.502, 0.691],
                                  "mean_excess_12m": 0.06, "sd_excess_12m": 0.3},
    "downtrend / negative momentum": {"n": 900, "hits": 360, "p": 0.4, "n_effective": 75, "wilson95": [0.294, 0.515],
                                      "mean_excess_12m": -0.03, "sd_excess_12m": 0.3},
}  # fmt: skip
BACKTEST = {"stats": {"from": "2015-01-30", "to": "2026-08-31", "months": 139,
                      "cagr": {"strategy": 0.16, "equal_weight_universe": 0.13, "nifty50_price": 0.11},
                      "excess_cagr_vs_equal_weight": 0.03, "max_drawdown": {"strategy": -0.3},
                      "monthly_hit_rate_vs_equal_weight": {"rate": 0.55, "wilson95": [0.47, 0.63]},
                      "newey_west_t_excess_vs_equal_weight": 1.1},
            "buckets": {**BUCKETS, "all": {"n": 5000, "p": 0.55}}, "months": [{"next": "2015-02-27", "strategy": 0.01, "equal_weight": 0.0,
                                             "nifty50": -0.01}]}  # fmt: skip


def bars(n: int, daily: float, start: date = date(2023, 6, 1)) -> list[PriceBar]:
    out, d, i = [], start, 0
    while len(out) < n:
        if d.weekday() < 5:
            c = Decimal(str(round(1000 * math.exp(daily * i) * (1 + 0.004 * (i % 3 - 1)), 2)))
            out.append(PriceBar(day=d, open=c, high=c * Decimal("1.01"), low=c * Decimal("0.99"), close=c,
                                prev_close=None, vwap=None, volume=Decimal(1000), value_inr=None, trades=None))  # fmt: skip
            i += 1
        d += timedelta(days=1)
    return out


def annual() -> dict[date, dict]:
    x = parse_results_xbrl((EQ / "integrated_INFY_Q4FY26_consolidated.xml").read_bytes())
    cur = {**x.year_to_date.facts, **x.balance_sheet.facts}
    prev = {k: v * Decimal("0.93") for k, v in cur.items()}  # a synthetic prior year on the same basis
    meta = {"consolidated": True, "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/X.xml",
            "company_type": "Main Board", "revenue_basis": "revenue_from_operations", "filed_at": None}  # fmt: skip
    return {date(2026, 3, 31): {"facts": cur, **meta}, date(2025, 3, 31): {"facts": prev, **meta}}


def raw(
    n_bars: int = 400, daily: float = 0.0012, industry: str = "Computers - Software & Consulting"
) -> dict:
    b = bars(n_bars, daily)
    q = Quote(symbol="INFY", company="Infosys Limited", last_price=b[-1].close, industry=industry)
    quarters = [{"period_end": f"{y}-{m:02d}-{d}", "eps": 15.0 + i, "filed_at": f"{fy}-{fm:02d}-20T18:00:00+05:30"}
                for i, (y, m, d, fy, fm) in enumerate([(2023, 12, 31, 2024, 1), (2024, 3, 31, 2024, 4),
                                                       (2024, 6, 30, 2024, 7), (2024, 9, 30, 2024, 10),
                                                       (2024, 12, 31, 2025, 1), (2025, 3, 31, 2025, 4)])]  # fmt: skip
    holding = {"quarters": [{"as_of": "2025-06-30", "groups": {"promoter": 14.5, "fii": 33.0, "dii": 36.0}},
                            {"as_of": "2026-06-30", "groups": {"promoter": 14.4, "fii": 31.0, "dii": 38.5}}]}  # fmt: skip
    return {"quote": q, "bars": b, "actions": [], "results": {"quarters": quarters}, "annual": annual(),
            "shareholding": holding, "errors": []}  # fmt: skip


@pytest.fixture
def sources(monkeypatch):
    state = {"raw": raw(), "backtest": BACKTEST, "profile": Profile(risk_appetite="medium"), "logged": []}

    async def load(sym):
        return state["raw"]

    def record(sig, **kw):
        state["logged"].append((sig, kw))
        return len(state["logged"])

    monkeypatch.setattr(st, "SOURCES", st.StockSources(load=load, backtest=lambda: state["backtest"],
                                                        profile=lambda: state["profile"], record=record,
                                                        today=lambda: date(2026, 9, 30)))  # fmt: skip
    monkeypatch.setattr(st, "LOG_FORECASTS", True)
    monkeypatch.setattr(st, "_cache", {})
    return state


def run(sym="INFY"):
    return asyncio.run(st.stock_signal(sym, {}))


def test_rising_stock_gets_a_backtested_bucket_probability_and_rule_based_action(sources):
    s = run()
    assert s.asset == "stock" and s.event == st.EVENT and s.horizon == "12 months"
    assert s.probability == 0.6 and s.probability_interval == (0.502, 0.691)
    assert s.base_rate["n"] == 1200 and "effective n of 100" in s.base_rate["description"]
    assert s.validation.status == "rule_based" and s.validation.n == 100
    assert "Not backtested: the composite weights" in s.validation.description
    assert s.validation.metrics["excess_cagr"] == 0.03
    names = [f.name for f in s.factors]
    assert names[:2] == ["Momentum (12-1 month, risk-adjusted)", "Trend (price vs 200-day average)"]
    assert s.factors[0].contribution == 40 and s.factors[1].contribution == 20  # both saturated
    assert s.score == round(sum(f.contribution for f in s.factors), 1)
    assert s.action in ("BUY", "ACCUMULATE") and "not investment advice" in s.disclaimer.lower()
    assert any("Survivorship" in c for c in s.caveats) and any("Costs" in c for c in s.caveats)
    # logged in the forecast ledger under the event its resolver scores, resolving in a year
    ((sig, kw),) = sources["logged"]
    assert sig is s and kw["event_kind"] == "excess_return_12m" and kw["resolve_on"] == date(2027, 9, 30)
    assert kw["inputs"]["benchmark"] == "NIFTYBEES" and kw["inputs"]["start_date"] == "2026-09-30"
    from finresearch.signals.ledger import STOCK_EVENT

    assert s.event == STOCK_EVENT
    inst = next(f for f in s.factors if f.name == "FII + DII holding change")
    assert inst.value == 0.5 and inst.contribution == pytest.approx(1.25)  # (31+38.5)-(33+36) = +0.5 pp
    pe = next(f for f in s.factors if f.name == "P/E vs its own history")
    assert pe.value is not None and pe.value > 0


def test_sizing_is_capped_by_the_profile_and_never_above_it(sources):
    s = run()
    z = s.sizing
    assert z["profile_cap"] == 0.08 and z["weight"] <= 0.08 and z["cap_source"].startswith("default")
    assert z["stop_price"] < float(sources["raw"]["quote"].last_price) and 0 < z["stop_distance"] < 0.2
    sources["profile"] = Profile(risk_appetite="high", max_position_pct=Decimal(3))
    st._cache.clear()
    z = run().sizing
    assert z["weight"] <= 0.03 and z["cap_source"] == "profile max_position_pct"


def _factor(s, name):
    return next(f for f in s.factors if f.name == name)


def test_quarterly_filer_pe_says_its_basis(sources):
    pe = _factor(run(), "P/E vs its own history")
    assert (
        "TTM from four quarters to 31 Mar 2025" in pe.explanation
        and pe.source == "fincalc:signals.ttm_from_periods"
    )
    assert pe.value == pytest.approx(
        float(sources["raw"]["quote"].last_price) / (17 + 18 + 19 + 20), abs=0.05
    )


def test_half_yearly_filer_gets_a_pe_from_two_half_years(sources):
    """A half-yearly filer (its March filing reports Oct-Mar) never has four quarters: H1 + H2 make the year."""
    r = raw()
    r["results"] = {"quarters": [], "periods": [
        {"period_start": "2023-04-01", "period_end": "2023-09-30", "eps": 12.0, "filed_at": "2023-11-10T18:00:00+05:30",
         "consolidated": True},
        {"period_start": "2023-10-01", "period_end": "2024-03-31", "eps": 13.0, "filed_at": "2024-05-10T18:00:00+05:30",
         "consolidated": True},
        {"period_start": "2024-04-01", "period_end": "2024-09-30", "eps": 14.0, "filed_at": "2024-11-10T18:00:00+05:30",
         "consolidated": True}]}  # fmt: skip
    sources["raw"] = r
    s = run()
    pe = _factor(s, "P/E vs its own history")
    price = float(r["quote"].last_price)
    assert pe.value == pytest.approx(price / (13 + 14), abs=0.05)  # H2 FY24 + H1 FY25, to 30 Sep 2024
    assert "TTM from two half-years to 30 Sep 2024 (Oct 2023-Mar 2024 + Apr 2024-Sep 2024)" in pe.explanation
    assert "percentile of its daily history since 2024-05-10" in pe.explanation and pe.contribution != 0
    assert s.score == round(sum(f.contribution for f in s.factors), 1)


def test_missing_factors_are_listed_with_null_value_and_the_reason(sources):
    r = raw()
    r["results"] = {"quarters": []}
    r["errors"] = ["results: HTTPError"]
    r["shareholding"] = {"quarters": [{"as_of": "2026-06-30", "groups": {"promoter": 14.4}}]}
    r["annual"] = {}
    sources["raw"] = r
    s = run()
    by = {f.name: f for f in s.factors}
    pe, inst, prom, fz = (by["P/E vs its own history"], by["FII + DII holding change"], by["Promoter holding change"],
                          by["Forensic flags"])  # fmt: skip
    assert all(f.value is None and f.contribution == 0 for f in (pe, inst, prom, fz))
    assert "The results could not be loaded (results: HTTPError)" in pe.explanation
    assert "Needs two filed shareholding patterns to compare; 1 of the 1" in inst.explanation
    assert "Needs two filed shareholding patterns" in prom.explanation
    assert (
        "No annual results XBRL was found" in fz.explanation and "no data, not a clean bill" in fz.explanation
    )
    assert s.action != "NO_SIGNAL" and s.score == round(sum(f.contribution for f in s.factors), 1)


def test_loss_making_and_untileable_results_explain_the_missing_pe(sources):
    r = raw()
    q = r["results"]["quarters"]
    r["results"] = {"quarters": [{**x, "eps": -5.0} for x in q]}
    sources["raw"] = r
    pe = _factor(run(), "P/E vs its own history")
    assert pe.value is None and pe.contribution == 0 and "a loss, so P/E has no meaning" in pe.explanation
    st._cache.clear()
    r = raw()
    r["results"] = {"quarters": [q[0], q[2], q[4]]}  # every other quarter: no twelve months tile
    sources["raw"] = r
    pe = _factor(run(), "P/E vs its own history")
    assert pe.value is None and "do not add up to any twelve months" in pe.explanation
    assert "missing for the year to Dec 2024: Jan 2024-Mar 2024, Jul 2024-Sep 2024" in pe.explanation


def test_no_red_flag_is_shown_as_a_zero_factor(sources):
    s = run()
    assert not any(f.name.startswith("Forensic flag:") for f in s.factors)
    fz = _factor(s, "Forensic flags")
    assert (
        fz.value == 0 and fz.contribution == 0 and "None of the 5 forensic scores computed" in fz.explanation
    )


def test_falling_stock_and_short_history(sources):
    sources["raw"] = raw(daily=-0.0012)
    s = run()
    assert s.probability == 0.4 and s.score < 0 and s.action in ("REDUCE", "SELL")
    assert (
        s.sizing["weight"] == 0 and s.sizing["binding"] == "quarter-Kelly ceiling"
    )  # negative edge: no position
    st._cache.clear()
    sources["raw"] = raw(n_bars=120)
    s = run()
    assert s.action == "NO_SIGNAL" and s.probability is None and "13 months" in s.caveats[0]
    assert len(sources["logged"]) == 1  # the falling stock's signal was logged; NO_SIGNAL is not


def test_the_backtest_verdict_is_stated_on_the_signal(sources):
    c = run().caveats[0]
    assert c.startswith("Backtest: the momentum + trend portfolio returned 16.0% a year against 13.0%")
    assert "no demonstrated edge" in c and "within noise of the 55% base rate" in c  # t = 1.1, CI spans 50 %


def test_without_a_backtest_there_is_no_probability(sources):
    sources["backtest"] = None
    s = run()
    assert s.probability is None and s.base_rate is None and "missing" in s.validation.description
    assert s.caveats[0].startswith("No backtest results")


def test_forensic_card_scores_and_guard(sources):
    f = st.forensic(sources["raw"])
    assert (
        f["fiscal_year_end"] == "2026-03-31"
        and f["prior_year_end"] == "2025-03-31"
        and f["basis"] == "consolidated"
    )
    by = {s["key"]: s for s in f["scores"]}
    assert by["altman"]["value"] == pytest.approx(7.354855, abs=1e-5) and by["altman"]["flag"] == "safe"
    assert by["beneish"]["value"] is not None and by["piotroski"]["components"]["out_of"] == 8
    bank = raw(industry="Private Sector Bank")
    assert all(s["value"] is None and "Not meaningful" in s["reason"] for s in st.forensic(bank)["scores"])


def test_red_flags_cost_ten_points_each(sources):
    r = sources["raw"]
    cur = r["annual"][date(2026, 3, 31)]["facts"]
    cur["cfo"] = Decimal(
        -1
    )  # cash burn with a profit: accruals and CFO/EBITDA flags (and Beneish TATA jumps)
    s = run()
    flags = [f for f in s.factors if f.name.startswith("Forensic flag")]
    assert flags and all(f.contribution == -10 for f in flags[:3])


@pytest.fixture
def client(sources, monkeypatch):
    from finresearch.api import create_app

    monkeypatch.setattr(registry, "_PROVIDERS", {"stock": st.stock_signal})
    monkeypatch.setattr(registry, "_loaded", True)
    with TestClient(create_app()) as c:
        yield c


def test_signal_forensic_and_backtest_routes(client):
    j = client.get("/api/signals/stock/infy").json()
    assert (
        j["instrument"] == "INFY"
        and j["probability_interval"] == [0.502, 0.691]
        and j["sizing"]["weight"] > 0
    )
    assert client.get("/api/signals/stock/not a symbol").status_code == 422
    f = client.get("/api/stocks/INFY/forensic").json()
    assert len(f["scores"]) == 5 and "Screening flags" in f["disclaimer"]
    b = client.get("/api/backtests/stock").json()
    assert "months" not in b and b["equity_curve"] == [{"date": "2015-02-27", "strategy": 1.01, "equal_weight": 1.0,
                                                         "nifty50": 0.99}]  # fmt: skip
    assert "months" in client.get("/api/backtests/stock?months=1").json()


# --------------------------------------------------------------------------- since this report
V = {"status": "verified"}
FAIR = [
    {"group": "fair_value", "label": "Fair value", "role": "low", "value": 1400.0, "unit": "₹/share", "claim_id": 1, **V},
    {"group": "fair_value", "label": "Fair value", "role": "high", "value": 1700.0, "unit": "₹/share", "claim_id": 2, **V},
    {"group": "entry_zone", "label": "Entry zone", "role": "low", "value": 1300.0, "unit": "₹/share", "claim_id": 3, **V},
    {"group": "entry_zone", "label": "Entry zone", "role": "high", "value": 1450.0, "unit": "₹/share", "claim_id": 4, **V},
    {"group": "fair_value_crore", "label": "Equity value", "role": None, "value": 5e5, "unit": "₹ Cr", "claim_id": 5, **V},
    {"group": "fair_value_pb", "label": "Fair value P/B", "role": None, "value": 700.0, "unit": "₹/share", "claim_id": 6,
     "status": "needs_review"},
]  # fmt: skip


def test_freshness_with_a_frozen_clock():
    from finresearch.api.insights import freshness

    at = datetime(2026, 6, 1, 12, tzinfo=UTC)
    anns = [{"at": "2026-07-23T17:40:00+05:30", "category": "Outcome of Board Meeting", "text": "results",
             "results_period_end": "2026-06-30"},
            {"at": "2026-05-01T10:00:00+05:30", "category": "General", "text": "old", "results_period_end": None}]  # fmt: skip
    r = freshness(run_id=9, kind="stock_report", symbol="INFY", report_at=at, now=IST_NOON, price=1750.0,
                  price_as_of=None, fair=FAIR, announcements=anns)  # fmt: skip
    assert r["days_since"] == 120 and r["stale"] and r["suggest_rerun"]
    bands = {b["group"]: b for b in r["bands"]}
    assert set(bands) == {
        "fair_value",
        "entry_zone",
        "fair_value_pb",
    }  # a crore-valued estimate is not per share
    assert bands["fair_value_pb"]["position"] == "above" and not bands["fair_value_pb"]["verified"]
    assert not any("P/B" in x for x in r["reasons"])  # an unverified estimate never fires the review trigger
    assert bands["fair_value"]["position"] == "above" and bands["fair_value"]["distance"] == pytest.approx(
        1750 / 1700 - 1
    )
    assert bands["entry_zone"]["position"] == "above"
    assert r["new_filings"]["count"] == 1 and r["new_filings"]["results"] == 1
    assert any("120 days old" in x for x in r["reasons"]) and any(
        "above the report's fair value" in x for x in r["reasons"]
    )
    fresh = freshness(run_id=9, kind="stock_report", symbol="INFY", report_at=IST_NOON - timedelta(days=5),
                      now=IST_NOON, price=1350.0, price_as_of=None, fair=FAIR, announcements=[])  # fmt: skip
    assert not fresh["stale"] and not fresh["suggest_rerun"] and fresh["days_since"] == 5
    assert {b["group"]: b["position"] for b in fresh["bands"]} == {
        "fair_value": "below",
        "entry_zone": "inside",
        "fair_value_pb": "above",
    }


def test_since_routes_read_the_run_and_live_data(env, monkeypatch):
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope
    from finresearch.db.models import Claim, Company, ResearchRun
    from finresearch.ingest.documents import get_or_create_company

    with session_scope() as s:
        s.query(Company).filter(Company.nse_symbol == "SINCECO").update({"nse_symbol": None})
        co = get_or_create_company(s, f"since-co-{datetime.now().timestamp():.0f}", "Since Co")
        co.nse_symbol = "SINCECO"
        run_ = ResearchRun(company_id=co.id, kind="stock_report", status="done", manifest={},
                           finished_at=datetime(2026, 6, 1, 12, tzinfo=UTC))  # fmt: skip
        s.add(run_)
        s.flush()
        for metric, v in (("fair_value_low", "1400"), ("fair_value_high", "1700")):
            s.add(Claim(run_id=run_.id, stream="valuation", statement=metric, claim_type="numeric", metric=metric,
                        value=Decimal(v), unit="INR per share", status="verified"))  # fmt: skip
        run_id = run_.id

    class Eq:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def announcements(self, sym):
            return [Announcement(symbol=sym, at=datetime(2026, 7, 23, 12, tzinfo=UTC), category="Results",
                                 text="results", attachment=None, results_period_end=date(2026, 6, 30))]  # fmt: skip

    async def quote(sym):
        return Quote(symbol=sym, last_price=Decimal("1500"))

    app = create_app(clock=lambda: IST_NOON)
    app.state.markets = MarketSources(equity=Eq, quote=quote)
    with TestClient(app) as c:
        r = c.get(f"/api/runs/{run_id}/since").json()
        assert r["days_since"] == 120 and r["price"] == 1500 and r["bands"][0]["position"] == "inside"
        assert r["new_filings"]["results"] == 1 and r["suggest_rerun"]
        assert c.get("/api/stocks/SINCECO/since-report").json()["run_id"] == run_id
        assert c.get("/api/stocks/NOREPORT/since-report").json() == {"run_id": None, "symbol": "NOREPORT"}
        assert c.get("/api/runs/999999999/since").status_code == 404


def test_signal_lands_in_the_real_forecast_ledger(env, sources, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.signals.ledger import RESOLVERS

    monkeypatch.setattr(st, "SOURCES", st.StockSources(load=lambda s: asyncio.sleep(0, sources["raw"]),
                                                        backtest=lambda: BACKTEST, profile=lambda: Profile(),
                                                        today=lambda: date(2026, 9, 30)))  # fmt: skip
    with session_scope() as s:
        s.query(Forecast).filter(Forecast.instrument == "INFY", Forecast.asset == "stock").delete()
    run()
    run()  # the same day: still one open forecast
    with session_scope() as s:
        rows = s.query(Forecast).filter(Forecast.instrument == "INFY", Forecast.asset == "stock").all()
        assert len(rows) == 1 and rows[0].event_kind == "excess_return_12m" and rows[0].probability == 0.6
        assert rows[0].validation_status == "rule_based" and rows[0].inputs["benchmark"] == "NIFTYBEES"
    assert ("stock", "excess_return_12m") in RESOLVERS
