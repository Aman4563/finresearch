"""IPO signal provider (roadmap §D.1), the item-15 subscription archive and the base-rates API."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from finresearch.fincalc import ipo as fipo
from finresearch.fincalc.dates import IST
from finresearch.signals import ipo as sig_ipo
from finresearch.suggest.profile import Profile, Rule, default_profile

FIX = Path(__file__).parent / "fixtures" / "nse"
NOW = datetime(2026, 9, 29, 15, 0, tzinfo=IST)


def orient(qib: str | None = None, retail: str | None = None, listing: str | None = None):
    from finresearch.adapters.nse import parse_ipo_detail

    raw = json.loads((FIX / "ipo_detail_ORIENTCABL_20260928_1636_live.json").read_text())
    if listing:
        raw["metaInfo"] = {"listingDate": listing}
    d = parse_ipo_detail("ORIENTCABL", raw)
    for c in d.combined.categories:
        if c.code == "1" and qib is not None:
            c.shares_bid = c.shares_offered * Decimal(qib)
        if c.code == "3" and retail is not None:
            c.shares_bid = c.shares_offered * Decimal(retail)
    return d


def history(n_hot_gain: int = 18, n_hot: int = 20, n_cold_gain: int = 4, n_cold: int = 10) -> list[dict]:
    """Post-2022 rows: >100x QIB issues mostly gain, <1x QIB issues mostly lose."""
    rows = []
    for i in range(n_hot):
        rows.append({"symbol": f"H{i}", "series": "EQ", "listing_date": "2024-01-10", "qib_times": 150,
                     "total_times": 60, "return_open": 0.30 if i < n_hot_gain else -0.05, "post_2022": True})  # fmt: skip
    for i in range(n_cold):
        rows.append({"symbol": f"C{i}", "series": "EQ", "listing_date": "2024-02-10", "qib_times": 0.5,
                     "total_times": 1, "return_open": 0.02 if i < n_cold_gain else -0.08, "post_2022": True})  # fmt: skip
    return rows


def sources(
    detail=None,
    rows=None,
    profile: Profile | None = None,
    snapshot=None,
    artefact=None,
    stock=None,
    record=None,
):
    async def ipo_detail(symbol, series):
        if detail is None:
            raise RuntimeError("no page")
        return detail

    async def issue_terms(symbol, series):
        return detail.terms

    return sig_ipo.Sources(history_rows=lambda: rows if rows is not None else history(),
                           latest_snapshot=lambda s: snapshot, ipo_detail=ipo_detail, issue_terms=issue_terms,
                           profile=lambda: profile or default_profile(), artefact=lambda: artefact,
                           now=lambda: NOW, stock_signal=stock, record=record)  # fmt: skip


async def test_hot_qib_book_gives_apply_with_base_rate_probability_and_ev():
    s = await sig_ipo.compute("orientcabl", {}, sources(orient(qib="150", retail="20")))
    assert s.action == "APPLY" and s.validation.status == "base_rate" and s.validation.n == 20
    assert s.probability == pytest.approx((18 + 1) / (20 + 2), abs=1e-4)  # Laplace-smoothed 18/20
    lo, hi = s.probability_interval
    assert lo == pytest.approx(0.6990, abs=1e-3) and hi == pytest.approx(0.9721, abs=1e-3)  # Wilson 18/20
    # EV of one lot = P(allot) x lot cost x E[r]; Orient: 55 x ₹272 = ₹14,960, retail 20x -> 5%
    z = s.sizing
    assert z["lot_cost"] == 14960 and z["p_allot"] == pytest.approx(0.05, abs=1e-6)
    mean = (18 * 0.30 + 2 * -0.05) / 20
    assert z["expected_return_mean"] == pytest.approx(mean) and z["ev_per_lot"] == pytest.approx(
        0.05 * 14960 * mean
    )
    assert z["max_lots"] == 1 and z["max_lots"] <= z["limits"]["by_capital"]
    assert s.score == pytest.approx(200 * (s.probability - 0.5), abs=0.01)
    assert sum(f.contribution for f in s.factors) == pytest.approx(s.score, abs=0.01)
    assert (
        any("FINAL subscription" in c for c in s.caveats) and "not investment advice" in s.disclaimer.lower()
    )
    assert "GMP" not in " ".join(f.name for f in s.factors)


async def test_weak_qib_book_is_skipped_by_the_default_qib_floor_rule():
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient()))  # the fixture's QIB is 0.05x
    assert s.action == "SKIP" and s.base_rate["n"] == 10
    assert s.caveats[0].startswith("Your rules say skip: qib-floor")
    # no post-2022 issue closed below 1x QIB: no probability, but the investor's rule still decides
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(), rows=history(n_cold=0)))
    assert s.action == "SKIP" and s.probability is None and s.validation.status == "rule_based"
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(), rows=history(n_cold=0), profile=Profile()))
    assert s.action == "NO_SIGNAL" and "<1x" in s.caveats[0]


async def test_p_listing_gain_rule_sets_the_personal_threshold():
    rule = Rule(id="p-gain", metric="p_listing_gain", op="<", value=Decimal("0.9"))
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), profile=Profile(rules=[rule])))
    assert s.action == "SKIP" and "p-gain" in s.caveats[0]  # 86% < 90%
    ok = Rule(id="p-gain", metric="p_listing_gain", op="<", value=Decimal("0.6"))
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), profile=Profile(rules=[ok])))
    assert s.action == "APPLY" and s.sizing["rules"][0]["status"] == "clear"


async def test_missing_inputs_give_no_signal_with_a_reason():
    s = await sig_ipo.compute("NEWCO", {}, sources(None))
    assert s.action == "NO_SIGNAL" and "unavailable" in s.caveats[0] and s.probability is None
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), rows=[]))
    assert s.action == "NO_SIGNAL" and "harvest" in s.caveats[0]
    snap = {"base_rates": {"qib": {**fipo.base_rates(history()), "as_of": "2024-02-10"}}}
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), rows=[], artefact=snap))
    assert (
        s.action == "APPLY" and "snapshot" in s.base_rate["description"]
    )  # the committed snapshot stands in
    sme = orient(qib="150")
    sme.series = "SME"
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(sme))
    assert s.action == "NO_SIGNAL" and "mainboard" in s.caveats[0]
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="30")))  # no history in the 10-50x band
    assert s.action == "NO_SIGNAL" and "10–50x" in s.caveats[0]


async def test_archived_snapshot_is_preferred_over_the_live_page():
    cats = [c.model_dump(mode="json") for c in orient(qib="150").combined.categories]
    snap = {"categories": cats, "as_of": datetime(2026, 9, 29, 13, 0, tzinfo=IST), "source": "nse_combined"}
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(), snapshot=snap))  # live page says 0.05x
    assert s.action == "APPLY" and any("archived snapshot" in c for c in s.caveats)


async def test_after_listing_default_is_sell_unless_the_stock_signal_says_buy():
    listed = orient(qib="150", listing="2026-09-29")
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(listed))
    assert s.action == "SELL_AT_LISTING" and s.validation.status == "rule_based" and "-31.2%" in s.caveats[0]

    async def buy(symbol):
        return SimpleNamespace(action="BUY")

    s = await sig_ipo.compute("ORIENTCABL", {}, sources(listed, stock=buy))
    assert s.action == "HOLD_AFTER_LISTING"
    rows = [*history(), {"symbol": "ORIENTCABL", "series": "EQ", "listing_date": "2026-10-02", "qib_times": 2,
                         "return_open": 0.05, "list_open": 285.6, "issue_price": 272, "post_2022": True}]  # fmt: skip
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), rows=rows))
    assert s.action == "SELL_AT_LISTING" and s.sizing["listing"]["return_open"] == 0.05


async def test_model_is_used_only_when_its_walk_forward_passed():
    from test_ipo_model import _synthetic

    from finresearch.evals import ipo_model as im

    rows = _synthetic(signal=True)
    for r in rows:
        r["listing_date"] = r["listing_date"].replace("2026", "2025")
    rep = im.walk_forward(rows)
    assert rep["gate"]["passes"]
    snapshot_rows = history()
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150", retail="20"), rows=snapshot_rows,
                                                         artefact=rep))  # fmt: skip
    assert s.validation.status == "backtested" and "walk-forward" in s.validation.description
    lo, hi = s.probability_interval
    assert lo <= s.probability <= hi or hi - lo < 0.5  # measured range from the reliability bins
    assert sum(f.contribution for f in s.factors) == pytest.approx(s.score, abs=0.05)
    failed = {**rep, "gate": {**rep["gate"], "passes": False}}
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), artefact=failed))
    assert s.validation.status == "base_rate" and "did not pass" in s.validation.description


def test_listing_gain_metric_for_rules(monkeypatch):
    from finresearch.suggest.rules import Metric

    monkeypatch.setattr(sig_ipo, "history_rows_db", lambda: history())
    m = sig_ipo.listing_gain_metric(Metric(Decimal(150), "NSE combined subscription"))
    assert m.value == Decimal("0.8636") and "20 post-2022" in m.source
    assert sig_ipo.listing_gain_metric(Metric(None, "x")).value is None


async def test_apply_and_skip_are_logged_in_the_forecast_ledger_to_resolve_on_listing():
    logged = []
    s = await sig_ipo.compute(
        "ORIENTCABL", {}, sources(orient(qib="150"), record=lambda *a: logged.append(a))
    )
    sig, resolve_on, inputs = logged[0]
    assert sig is s and date(2026, 9, 30) < resolve_on <= date(2026, 10, 6)  # T+3 after the 29-Sep close
    assert inputs["symbol"] == "ORIENTCABL" and inputs["issue_price"] == "272"
    logged.clear()
    await sig_ipo.compute("NEWCO", {}, sources(None, record=lambda *a: logged.append(a)))
    await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150", listing="2026-09-29"),
                                                    record=lambda *a: logged.append(a)))  # fmt: skip
    assert logged == []  # NO_SIGNAL and after-listing calls are not forecasts of the listing event


async def test_record_live_writes_a_listing_gain_forecast(clean):
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Forecast

    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150")))
    sig_ipo.record_live(s, date(2026, 10, 2), {"symbol": "ORIENTCABL", "issue_price": "272"})
    sig_ipo.record_live(
        s, date(2026, 10, 2), {"symbol": "ORIENTCABL", "issue_price": "272"}
    )  # same day: one row
    with session_scope() as db:
        rows = db.scalars(select(Forecast)).all()
        assert len(rows) == 1 and rows[0].event_kind == "listing_gain" and rows[0].source == "signal:ipo"
        assert rows[0].probability == pytest.approx(s.probability) and rows[0].inputs["issue_price"] == "272"


@pytest.fixture
def clean(env):
    """This module's tables start empty (the test database is shared by the whole session)."""
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast, IpoHistory, SubscriptionArchiveSlot, SubscriptionSnapshotRow

    with session_scope() as s:
        for model in (Forecast, IpoHistory, SubscriptionArchiveSlot, SubscriptionSnapshotRow):
            s.query(model).delete()
    return env


# --------------------------------------------------------------------------- item 15: archive every open book
def test_archive_slots_follow_bidding_hours():
    from finresearch.monitor.jobs import archive_slot

    hol: set[date] = {date(2026, 10, 2)}
    at = lambda d, h, m: datetime(2026, 9, d, h, m, tzinfo=IST)  # noqa: E731
    assert archive_slot(at(29, 11, 0), hol) == "2026-09-29T11:00"
    assert archive_slot(at(29, 11, 44), hol) == "2026-09-29T11:00"
    assert archive_slot(at(29, 11, 45), hol) is None and archive_slot(at(29, 10, 59), hol) is None
    assert archive_slot(at(29, 17, 20), hol) == "2026-09-29T17:15"
    assert archive_slot(datetime(2026, 10, 2, 13, 5, tzinfo=IST), hol) is None  # exchange holiday
    assert archive_slot(at(27, 13, 5), hol) is None  # Sunday
    assert archive_slot(datetime(2026, 9, 29, 7, 35, tzinfo=UTC), hol) == "2026-09-29T13:00"  # UTC input


async def test_archive_snapshots_every_open_issue_once_per_slot(clean):
    from sqlalchemy import select

    from finresearch.adapters.nse import IpoIssue
    from finresearch.db import session_scope
    from finresearch.db.models import SubscriptionArchiveSlot, SubscriptionSnapshotRow
    from finresearch.monitor.jobs import Deps, archive_open_books

    issues = [IpoIssue(symbol="ORIENTCABL", company="Orient", series="EQ", issue_start=date(2026, 9, 25),
                       issue_end=date(2026, 9, 29)),
              IpoIssue(symbol="NOBOOK", company="No book", series="EQ", issue_start=date(2026, 9, 29),
                       issue_end=date(2026, 10, 1)),
              IpoIssue(symbol="LATER", company="Later", series="EQ", issue_start=date(2026, 10, 5),
                       issue_end=date(2026, 10, 7))]  # fmt: skip
    calls = []

    async def current():
        return issues

    async def detail(sym):
        calls.append(sym)
        if sym == "NOBOOK":
            from finresearch.adapters.nse import parse_ipo_detail

            return parse_ipo_detail(sym, {})
        return orient()

    deps = Deps(
        ipo_detail=detail, quote=None, current_issues=current, archive_books=True, archive_spacing_s=0
    )
    now = datetime(2026, 9, 29, 13, 2, tzinfo=IST)
    res = await archive_open_books(deps, now, holidays=set())
    assert (
        res["archived"] == ["ORIENTCABL"]
        and res["no_book"] == ["NOBOOK"]
        and calls == ["ORIENTCABL", "NOBOOK"]
    )
    assert await archive_open_books(deps, now, holidays=set()) is None  # the slot is taken
    assert await archive_open_books(Deps(ipo_detail=detail, quote=None, current_issues=current), now,
                                    holidays=set()) is None  # off unless enabled (live deps enable it)  # fmt: skip
    res = await archive_open_books(deps, datetime(2026, 9, 29, 15, 1, tzinfo=IST), holidays=set())
    assert res["unchanged"] == ["ORIENTCABL"]  # same NSE timestamp: de-duplicated
    with session_scope() as s:
        rows = s.scalars(select(SubscriptionSnapshotRow)).all()
        assert (
            len(rows) == 1
            and rows[0].source == "nse_combined"
            and rows[0].raw["archive_slot"] == "2026-09-29T13:00"
        )
        assert {r.slot for r in s.scalars(select(SubscriptionArchiveSlot))} == {
            "2026-09-29T13:00",
            "2026-09-29T15:00",
        }
    snap = sig_ipo.latest_snapshot_db("ORIENTCABL")
    assert snap and snap["source"] == "nse_combined" and len(snap["categories"]) > 5


async def test_archive_releases_the_slot_when_the_issue_list_fails(clean):
    from finresearch.monitor.jobs import Deps, archive_open_books

    async def boom():
        raise RuntimeError("NSE down")

    deps = Deps(ipo_detail=None, quote=None, current_issues=boom, archive_books=True, archive_spacing_s=0)
    now = datetime(2026, 9, 29, 11, 5, tzinfo=IST)
    with pytest.raises(RuntimeError):
        await archive_open_books(deps, now, holidays=set())

    async def empty():
        return []

    res = await archive_open_books(Deps(ipo_detail=None, quote=None, current_issues=empty, archive_books=True),
                                   now, holidays=set())  # fmt: skip
    assert res == {
        "slot": "2026-09-29T11:00",
        "open": 0,
        "archived": [],
        "unchanged": [],
        "no_book": [],
        "errors": [],
    }


# --------------------------------------------------------------------------- API
def test_base_rates_endpoint_reads_the_harvested_table(clean, monkeypatch):
    from fastapi.testclient import TestClient

    from finresearch.api import create_app
    from finresearch.db import session_scope
    from finresearch.db.models import IpoHistory

    monkeypatch.setattr(sig_ipo, "_CACHE", {})
    with session_scope() as s:
        for i, r in enumerate(history()):
            s.add(IpoHistory(symbol=r["symbol"], series="EQ", ipo_start=date(2024, 1, 1 + i % 28),
                             listing_date=date.fromisoformat(r["listing_date"]), qib_times=r["qib_times"],
                             nii_times=1, retail_times=1, total_times=r["total_times"], public_book_cr=100,
                             return_open=r["return_open"], post_2022=True, status="complete"))  # fmt: skip
    with TestClient(create_app()) as c:
        body = c.get("/api/ipo/base-rates").json()
        assert body["source"] == "database" and body["n"] == 30 and body["as_of"] == "2024-02-10"
        hot = next(x for x in body["cells"] if x["band"] == ">100x" and x["regime"] == "post_2022")
        assert hot["n"] == 20 and hot["p_loss"] == 0.1 and "FINAL" in body["caveat"]
        assert c.get("/api/ipo/base-rates", params={"by": "total"}).json()["by"] == "total"
        assert c.get("/api/ipo/base-rates", params={"by": "gmp"}).status_code == 422
