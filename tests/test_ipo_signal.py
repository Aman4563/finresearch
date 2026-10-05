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

    # the stock signal shown as informational (#193) keeps its composite BUY for this IPO rule: behaviour unchanged
    async def informational_buy(symbol):
        return SimpleNamespace(
            action="INFORMATIONAL", call={"status": "informational", "composite_action": "BUY"}
        )

    s = await sig_ipo.compute("ORIENTCABL", {}, sources(listed, stock=informational_buy))
    assert s.action == "HOLD_AFTER_LISTING" and "rates it BUY" in s.method

    async def informational_reduce(symbol):
        return SimpleNamespace(
            action="INFORMATIONAL", call={"status": "informational", "composite_action": "REDUCE"}
        )

    s = await sig_ipo.compute("ORIENTCABL", {}, sources(listed, stock=informational_reduce))
    assert s.action == "SELL_AT_LISTING"
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

    from finresearch.monitor import jobs

    assert await archive_open_books(Deps(ipo_detail=None, quote=None, current_issues=empty, archive_books=True),
                                    now, holidays=set()) is None  # released, but retried after ARCHIVE_RETRY_S  # fmt: skip
    jobs._archive_retry_at.clear()  # five minutes later
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
        blend = body["model"]["blend"]  # the committed E-IPO-1 artefact (#147)
        assert 0 < blend["lambda"] < 1 and blend["gate"]["passes"] and "reliability" not in blend["pooled"]
        assert c.get("/api/ipo/base-rates", params={"by": "total"}).json()["by"] == "total"
        assert c.get("/api/ipo/base-rates", params={"by": "gmp"}).status_code == 422


# --------------------------------------------------------------------------- the provider's cache
async def test_ipo_signal_is_cached_per_inputs_and_logs_once(monkeypatch):
    import httpx

    calls = {"detail": 0}
    logged: list = []
    snap = {"as_of": datetime(2026, 9, 29, 14, 0, tzinfo=IST)}
    state = {"down": False, "snapshot": None, "profile": default_profile()}
    detail = orient(qib="150", retail="20")

    async def ipo_detail(symbol, series):
        calls["detail"] += 1
        if state["down"]:
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
        return detail

    async def issue_terms(symbol, series):
        return detail.terms

    src = sig_ipo.Sources(history_rows=history, latest_snapshot=lambda s: state["snapshot"], ipo_detail=ipo_detail,
                          issue_terms=issue_terms, profile=lambda: state["profile"], artefact=lambda: None,
                          now=lambda: NOW, record=lambda sig, on, inp: logged.append(sig.instrument))  # fmt: skip
    monkeypatch.setattr(sig_ipo, "SOURCES", src)
    monkeypatch.setattr(sig_ipo, "_SIGNALS", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr("time.time", lambda: clock["t"])
    assert (
        sig_ipo.signal_ttl(NOW) == sig_ipo.SIGNAL_TTL_BIDDING_S
    )  # 15:00 IST on a trading day: bidding hours
    assert sig_ipo.signal_ttl(datetime(2026, 9, 29, 18, 0, tzinfo=IST)) == sig_ipo.SIGNAL_TTL_S

    first = await sig_ipo.ipo_signal("ORIENTCABL", {})
    again = await sig_ipo.ipo_signal("orientcabl", {})
    assert first.action == "APPLY" and again is first and calls["detail"] == 1
    assert logged == ["ORIENTCABL"]  # a cache hit never records the forecast again
    state["snapshot"] = snap  # a new recorded book is a new input: recomputed
    await sig_ipo.ipo_signal("ORIENTCABL", {})
    assert calls["detail"] == 2
    state["profile"] = Profile(capital_per_ipo_inr=Decimal(30000))  # a profile edit too
    await sig_ipo.ipo_signal("ORIENTCABL", {})
    assert calls["detail"] == 3
    clock["t"] += sig_ipo.SIGNAL_TTL_BIDDING_S + 1  # expired in bidding hours
    await sig_ipo.ipo_signal("ORIENTCABL", {})
    assert calls["detail"] == 4

    # a failure is never cached: NSE unreachable, then back
    monkeypatch.setattr(sig_ipo, "_SIGNALS", {})
    state.update(down=True, snapshot=None)
    down = await sig_ipo.ipo_signal("ORIENTCABL", {"series": "EQ"})
    assert down.action == "NO_SIGNAL" and "unavailable" in down.caveats[0]
    state["down"] = False
    back = await sig_ipo.ipo_signal("ORIENTCABL", {"series": "EQ"})
    assert back.action == "APPLY"


async def test_sme_card_needs_no_nse_request():
    asked: list = []

    async def ipo_detail(symbol, series):
        asked.append(symbol)
        raise AssertionError("no NSE request for an SME card")

    src = sources(orient())
    src.ipo_detail = ipo_detail
    s = await sig_ipo.compute("ACMEUNIV", {"series": "sme"}, src)
    assert s.action == "NO_SIGNAL" and s.caveats[0] == sig_ipo.SME_REASON and asked == []


# ------------------------------------------------------------------------------------------------ sizing by category
# Audit #137: an sNII / bNII applicant was told "at most 1 lot", which is a retail bid. The lottery unit in those
# categories is the minimum category application (ICDR Reg 32(3A)), so the size is that minimum, and the expected value
# is per application. A retail book below 1x is not a lottery at all.
async def test_small_nii_applicant_is_sized_at_the_minimum_snii_application():
    prof = Profile(category="shni", capital_per_ipo_inr=Decimal(500000))
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), profile=prof))
    z = s.sizing
    # Orient: lot ₹14,960; the first whole lot above ₹2 lakh is 14 lots (₹2,09,440)
    assert s.action == "APPLY" and z["min_lots"] == 14 and z["max_lots"] == 14
    snii = z["p_allot"]
    assert snii == pytest.approx(1 / 21.3418, rel=1e-6)  # the fixture's sNII book (2.2), 4 dp
    assert z["ev_per_application"] == pytest.approx(snii * 14 * 14960 * z["expected_return_mean"])
    assert z["ev_per_lot"] == pytest.approx(snii * 14960 * z["expected_return_mean"])
    assert "minimum" in z["reason"] and "sNII" in z["reason"]


async def test_nii_applicant_who_cannot_afford_the_minimum_application_skips():
    prof = Profile(category="shni", capital_per_ipo_inr=Decimal(100000))  # 6 lots, below the 14-lot minimum
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150"), profile=prof))
    assert s.action == "SKIP" and s.sizing["max_lots"] == 0
    assert "minimum" in s.caveats[0] and "₹2,09,440" in s.caveats[0]


async def test_undersubscribed_retail_book_is_not_a_lottery():
    prof = Profile(capital_per_ipo_inr=Decimal(60000))  # 4 lots of ₹14,960
    s = await sig_ipo.compute("ORIENTCABL", {}, sources(orient(qib="150", retail="0.8"), profile=prof))
    z = s.sizing
    assert s.action == "APPLY" and z["p_allot"] == 1.0
    assert z["max_lots"] == 4 and "in full" in z["reason"]


# --------------------------------------------------------------------------- E-IPO-1 shrinkage blend (#147)
def _blend(lam: float = 0.45, passes: bool = True):
    from test_ipo_model import _synthetic

    from finresearch.evals import ipo_model as im

    fm = im.fit(im.usable(_synthetic(signal=True)))
    return {"ship": "S", "calibrator": {"variant": "S", "params": {"lambda": lam}}, "final_model": fm,
            "n_rows": 440, "min_cell": 5, "variants_tested": ["T", "S", "P"],
            "gate": {"years_evaluated": list(range(2019, 2026)), "years_passed": [2019, 2020, 2022, 2023, 2025],
                     "passes": passes},
            "pooled": {"n": 288, "brier": 0.1451, "brier_table": 0.1510, "bss_vs_table": 0.039, "auc": 0.833,
                       "reliability": [{"n": 58, "mean_p": 0.35, "observed": 0.38},
                                       {"n": 58, "mean_p": 0.9, "observed": 0.91}]},
            "folds": [{"year": y, "bss_vs_table": b} for y, b in
                      zip(range(2019, 2026), (0.43, 0.55, -0.05, 0.002, 0.18, -0.03, 0.02), strict=True)]}  # fmt: skip


def _closes(n: int = 30, last: date = date(2026, 9, 28)):
    from datetime import timedelta

    return [(last - timedelta(days=n - i), Decimal(20000 + 10 * i)) for i in range(n + 1)]


async def test_blend_runs_in_shadow_beside_the_base_rate_call_and_fills_live_features():
    detail = orient(qib="150", retail="20")
    logged = []
    src = sources(detail, record=lambda sig, day, inputs: logged.append((sig, inputs)))
    src.calibrated = lambda: _blend()

    async def closes(today):
        return _closes()

    src.nifty_closes = closes
    s = await sig_ipo.compute("ORIENTCABL", {}, src)
    # the live call is unchanged: the base-rate table (#151 shadow mode)
    p_cell = fipo.smoothed_rate(18, 20)  # the >100x band in history(): 18 of 20 gained
    assert s.method == sig_ipo.BASE_RATE_METHOD and s.validation.status == "base_rate"
    assert s.probability == pytest.approx(p_cell, abs=6e-5)
    assert sig_ipo.BLEND_CAVEAT not in s.caveats and not any(f.name.startswith("Model:") for f in s.factors)
    from finresearch.evals import ipo_model as im

    book = sig_ipo.book_of(None, detail)
    feat = await sig_ipo._live_features_full(
        book, detail.terms.price_high, history(), NOW.date(), detail, src
    )
    assert feat["nifty20"] == pytest.approx(
        20300 / 20100 - 1, abs=1e-6
    )  # 20 sessions back, 6 dp as harvested
    assert feat["ofs_share"] == pytest.approx(0.4203)  # ₹232 cr OFS of ₹552 cr, the harvester's parser
    p_model = float(im.predict(_blend()["final_model"], [feat])["p"][0])
    sh = s.shadow
    assert sh["status"] == "shadow" and sh["method"] == sig_ipo.BLEND_METHOD
    assert sh["probability"] == pytest.approx(0.45 * p_model + 0.55 * p_cell, abs=6e-5)
    lo, hi = sh["probability_interval"]
    assert lo <= sh["probability"] <= hi  # the range always contains the forecast
    assert (
        "5 of 7" in sh["backtest"] and "2022 (+0.002)" in sh["backtest"] and "SHADOW.md" in sh["description"]
    )
    assert s.to_json()["shadow"]["probability"] == sh["probability"]
    assert sig_ipo.calibrated_interval(_blend(), 0.2)[0] == 0.2  # below the lowest bin: stretched down to p
    # both are logged: the call under its method, the blend as its own method with validation "shadow"
    assert [x.method for x, _ in logged] == [sig_ipo.BASE_RATE_METHOD, sig_ipo.BLEND_METHOD]
    shadow_sig, inputs = logged[1]
    assert shadow_sig.validation.status == "shadow" and shadow_sig.probability == sh["probability"]
    assert inputs["shadow_of"] == sig_ipo.BASE_RATE_METHOD and shadow_sig.event == s.event


async def test_shadow_uses_the_regime_rate_for_a_thin_band_and_is_off_without_a_passing_artefact():
    detail = orient(qib="150", retail="20")
    rows = history(n_hot_gain=3, n_hot=3)  # the >100x band has 3 issues: below MIN_CELL
    src = sources(detail, rows=rows)
    src.calibrated = lambda: _blend(lam=0.0)  # λ = 0: the shadow IS its reference
    s = await sig_ipo.compute("ORIENTCABL", {}, src)
    regime = fipo.base_rates(rows)["regime_totals"]["post_2022"]
    p_regime = fipo.smoothed_rate(round(regime["p_gain"] * regime["n"]), regime["n"])
    assert s.shadow["probability"] == pytest.approx(p_regime, abs=6e-5) and "regime" in s.shadow["reference"]
    assert any("Nifty 50 20-session return unavailable" in c for c in s.shadow["caveats"])  # no nifty source
    for bad in (None, _blend(passes=False), {**_blend(), "ship": None}):
        src.calibrated = lambda bad=bad: bad
        s = await sig_ipo.compute("ORIENTCABL", {}, src)
        assert (
            s.validation.status == "base_rate" and s.method == sig_ipo.BASE_RATE_METHOD and s.shadow is None
        )


def test_committed_blend_artefact_is_ready_and_consistent():
    from finresearch.evals.ipo_calibration import load_signal_artefact

    art = load_signal_artefact()
    assert sig_ipo.calibrated_ready(art)
    assert art["gate"]["passes"] and len(art["gate"]["years_passed"]) >= 5
    assert art["pooled"]["brier"] < art["pooled"]["brier_table"]
    assert 0 < art["calibrator"]["params"]["lambda"] < 1


async def test_an_archive_pass_that_nse_refused_entirely_is_retried_in_the_window(clean, monkeypatch):
    """Regression: a 403 on every issue's detail still finalised the slot with archived=[], so that bidding-day
    snapshot was lost for good."""
    import time

    from finresearch.adapters.nse import IpoIssue
    from finresearch.monitor import jobs
    from finresearch.monitor.jobs import Deps, archive_open_books

    issues = [IpoIssue(symbol="ORIENTCABL", company="Orient", series="EQ", issue_start=date(2026, 9, 25),
                       issue_end=date(2026, 9, 29))]  # fmt: skip
    refuse = [True]

    async def current():
        return issues

    async def detail(sym):
        if refuse[0]:
            raise RuntimeError("NSE refused /api/ipo-detail after re-warm: HTTP 403")
        return orient()

    deps = Deps(
        ipo_detail=detail, quote=None, current_issues=current, archive_books=True, archive_spacing_s=0
    )
    jobs._archive_retry_at.clear()
    now = datetime(2026, 9, 29, 11, 2, tzinfo=IST)
    first = await archive_open_books(deps, now, holidays=set())
    assert first["archived"] == [] and len(first["errors"]) == 1
    refuse[0] = False
    assert await archive_open_books(deps, now, holidays=set()) is None  # not before ARCHIVE_RETRY_S
    monkeypatch.setattr(time, "time", lambda: jobs._archive_retry_at["2026-09-29T11:00"] + 1)
    again = await archive_open_books(deps, datetime(2026, 9, 29, 11, 8, tzinfo=IST), holidays=set())
    assert again is not None and again["archived"] == ["ORIENTCABL"]
    jobs._archive_retry_at.clear()
