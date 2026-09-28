"""SME issues: series detection, unknown SME category times, two-lot applications, market-maker facts, discovery."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from finresearch.adapters.nse import parse_ipo_detail
from finresearch.suggest.profile import Profile
from finresearch.suggest.rules import is_sme, lot_limits, subscription_metrics
from finresearch.verify.baseline import parse_baseline

FIX = Path(__file__).parent / "fixtures" / "nse"


def load(name, series):
    return parse_ipo_detail(name.split("_")[2], json.loads((FIX / name).read_text()), series=series)


def test_sme_category_times_are_unknown_not_zero():
    """Live BMISL (SME, day 4): NSE prints 0.00x for every category because it publishes no offered shares, while
    1.27x is subscribed overall. Treating 0.00 as real would fire a 'QIB < 1x' rule."""
    d = load("ipo_detail_BMISL_SME_20260929.json", "SME")
    qib = d.combined.category("1")
    assert qib.shares_offered in (None, 0) and qib.times is None
    assert d.combined.category("2").shares_bid == Decimal("788400")
    assert all(m.value is None for m in subscription_metrics(d).values())
    mainboard = load("ipo_detail_ORIENTCABL_20260928_1636_live.json", "EQ")
    assert round(mainboard.combined.category("1").times, 2) == Decimal("0.05")  # mainboard unchanged


def test_sme_baseline_reads_lot_size_and_market_maker_portion():
    d = load("ipo_detail_PAPADMALJI_SME_20260929.json", "SME")
    facts = {f.metric: f.value for f in parse_baseline(d.issue_info)}
    assert facts["lot_size"] == 1600 and facts["price_band_upper"] == 72
    assert facts["market_maker_portion_shares"] == 144000
    assert is_sme(d.issue_info, d) and not is_sme({"Issue Size": "Fresh issue of 10 shares"})


def test_sme_applications_are_two_lots_for_individuals():
    lot_cost = Decimal(72 * 1600)  # ₹1,15,200 a lot: two lots = ₹2,30,400 (> ₹2 lakh)
    assert lot_limits(Profile(capital_per_ipo_inr=Decimal(250000)), lot_cost, sme=True) == {
        "by_capital": 2, "by_category": 2, "min_lots": 2}  # fmt: skip
    assert lot_limits(Profile(capital_per_ipo_inr=Decimal(150000)), lot_cost, sme=True)["by_capital"] == 1
    assert lot_limits(Profile(capital_per_ipo_inr=Decimal(500000), category="shni"), lot_cost, sme=True)[
        "min_lots"] == 3  # fmt: skip


def test_enforcement_skips_when_capital_cannot_cover_two_sme_lots():
    from finresearch.suggest.advisor import Suggestion, enforce
    from finresearch.suggest.rules import Inputs

    p = Profile(capital_per_ipo_inr=Decimal(150000), rules=[])
    s = Suggestion(
        action="APPLY", category="retail", lots=1, exit_plan="x", rationale_markdown="x", confidence="low"
    )
    out = enforce(s, Inputs(), lot_limits(p, Decimal(115200), sme=True), p)
    assert out["action"] == "SKIP" and "minimum application" in out["enforcement_notes"][-1]


def test_nse_candidates_include_the_sme_basis_of_price_archive():
    from finresearch.ingest.discover import nse_candidates

    d = load("ipo_detail_PAPADMALJI_SME_20260929.json", "SME")
    titles = {c.title for c in nse_candidates(d.issue_info)}
    assert titles == {"NSE Red Herring Prospectus", "NSE Ratios / Basis of Issue Price"}


async def test_ipo_detail_falls_back_to_the_sme_series():
    import httpx
    import respx

    from finresearch.adapters.nse import NseClient

    sme = json.loads((FIX / "ipo_detail_PAPADMALJI_SME_20260929.json").read_text())
    with respx.mock:
        respx.get(url__startswith="https://www.nseindia.com/market-data").mock(
            return_value=httpx.Response(200, text="<html></html>")
        )
        route = respx.get("https://www.nseindia.com/api/ipo-detail")
        route.side_effect = lambda req: httpx.Response(200, json=sme if req.url.params["series"] == "SME" else
                                                       {"issueInfo": {"dataList": []}})  # fmt: skip
        async with NseClient() as nse:
            d = await nse.ipo_detail("PAPADMALJI")
    assert d.series == "SME" and d.issue_info["Lot Size"] == "1600 Equity Shares"


async def test_monitor_records_sme_subscription_from_the_current_issues_total(env, tmp_path):
    from datetime import datetime

    from finresearch.adapters.nse import IpoIssue
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, SubscriptionSnapshotRow, Watch
    from finresearch.fincalc.dates import IST
    from finresearch.ingest.documents import get_or_create_company
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.scheduler import tick
    from finresearch.monitor.watch import upsert_watch

    d = load("ipo_detail_BMISL_SME_20260929.json", "SME")
    with session_scope() as s:
        for model in (Alert, MonitorJob, Watch, SubscriptionSnapshotRow):
            s.query(model).delete()
        co = get_or_create_company(s, "sme-" + tmp_path.name[-8:], "Bench Mark")
        co.nse_symbol = "BMISL"
        slug = co.slug
    w = upsert_watch(slug, d.issue_info)

    async def detail(symbol):
        return d

    async def current():
        return [
            IpoIssue(symbol="BMISL", company="Bench Mark", series="SME", times_subscribed=Decimal("1.27"))
        ]

    async def quote(symbol):
        raise RuntimeError("not listed")

    close = datetime.fromisoformat(w["close_date"])
    await tick(Deps(ipo_detail=detail, quote=quote, current_issues=current),
               datetime(close.year, close.month, close.day, 17, 20, tzinfo=IST))  # fmt: skip
    with session_scope() as s:
        snap = s.query(SubscriptionSnapshotRow).filter_by(nse_symbol="BMISL").one()
        assert snap.total_times == Decimal("1.27") and snap.source == "nse_current_issues"
        final = s.query(Alert).filter_by(kind="subscription_final").one().message
        assert "total 1.27x" in final and "QIB" not in final  # unknown SME category times are not reported
