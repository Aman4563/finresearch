"""End of life of tracked instruments and portfolio-aware targeting (#293). Synthetic instruments only."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest

from finresearch.monitor import lifecycle as L

D0 = date(2026, 10, 1)


def _days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def test_one_missing_day_never_ends_an_instrument():
    st = L.note_pass(None, present=True, day=D0, source="NSE bonds list")
    st = L.note_pass(st, present=False, day=D0 + timedelta(days=1), source="NSE bonds list")
    assert st["missing_days"] == ["2026-10-02"] and not st.get("ended")
    assert L.ended(st, D0 + timedelta(days=1)) is None


def test_nine_missing_passes_do_not_end_ten_do():
    st = None
    for d in _days(9):
        st = L.note_pass(st, present=False, day=d, source="x")
    assert len(st["missing_days"]) == 9 and not st.get("ended")
    st = L.note_pass(st, present=False, day=D0 + timedelta(days=9), source="x")
    assert st["ended"]["reason"] == "missing" and st["ended"]["on"] == "2026-10-10"
    assert L.ended(st, D0 + timedelta(days=9))["reason"] == "missing"


def test_a_retry_on_the_same_day_counts_once_and_a_sighting_resets_the_run():
    st = None
    for _ in range(12):  # a pass retried all day long is still one missing day
        st = L.note_pass(st, present=False, day=D0, source="x")
    assert st["missing_days"] == ["2026-10-01"]
    for d in _days(9):
        st = L.note_pass(st, present=False, day=d, source="x")
    st = L.note_pass(st, present=True, day=D0 + timedelta(days=9), source="x")
    assert st["missing_days"] == [] and st["last_seen"] == "2026-10-10"
    st = L.note_pass(st, present=False, day=D0 + timedelta(days=10), source="x")
    assert len(st["missing_days"]) == 1 and not st.get("ended")


def test_reappearing_revives_a_missing_instrument_but_not_a_matured_bond():
    st = None
    for d in _days(10):
        st = L.note_pass(st, present=False, day=d, source="x")
    st = L.note_pass(st, present=True, day=D0 + timedelta(days=11), source="x")
    assert st["ended"] is None and st["revived"]["revived_on"] == "2026-10-12"
    assert (
        L.ended({"maturity": "2026-10-05", "last_seen": "2026-10-12"}, D0 + timedelta(days=11))["reason"]
        == "matured"
    )


def test_maturity_unknown_is_never_matured():
    assert L.matured(None, D0) is False and L.matured("", D0) is False
    assert L.matured("2026-10-01", D0) is True  # matures today: ended today (no trade settles after it)
    assert L.matured(date(2026, 10, 2), D0) is False
    assert L.ended({}, D0) is None


# --------------------------------------------------------------------------- the daily pass (database)
TODAY = date(2026, 10, 9)
LIVE_BOND, MATURED_BOND, GONE_BOND = (
    "INE0EO107011",
    "INE0EO107029",
    "INE0EO107037",
)  # synthetic debenture ISINs


@pytest.fixture
def lc(env, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import (
        Alert,
        AlertEvalSlot,
        AlertRuleState,
        Company,
        DisclosureFeed,
        InvestorProfile,
        MonitorJob,
        PortfolioDisposal,
        PortfolioHolding,
        PortfolioLot,
        PortfolioSetting,
        PortfolioTxn,  # fmt: skip
        Watch,
    )

    def wipe():
        with session_scope() as s:
            for m in (Alert, AlertRuleState, AlertEvalSlot, MonitorJob, Watch, DisclosureFeed, InvestorProfile,
                      PortfolioSetting, PortfolioDisposal, PortfolioLot, PortfolioTxn, PortfolioHolding):  # fmt: skip
                s.query(m).delete()
            s.query(Company).filter(Company.slug.like("eol-%") | Company.slug.like("bond-ine0eo%")
                                    | Company.slug.like("mf-99990%")).delete(synchronize_session=False)  # fmt: skip

    monkeypatch.setattr(L, "MIN_ROWS", {"bond": 1, "fund": 1, "stock": 1})
    wipe()
    yield env
    wipe()


def _company(s, slug, name, **kw):
    from finresearch.ingest.documents import get_or_create_company

    co = get_or_create_company(s, slug, name)
    for k, v in kw.items():
        setattr(co, k, v)
    s.flush()
    return co


def _hold(s, asset_type, name, *, isin=None, sym=None, code=None, qty="10", open_qty="10"):
    from finresearch.db.models import PortfolioHolding, PortfolioLot

    h = PortfolioHolding(ikey=f"{asset_type}:{isin or sym or code}", account="Demat", asset_type=asset_type,
                         name=name, isin=isin, nse_symbol=sym, scheme_code=code, meta={})  # fmt: skip
    s.add(h)
    s.flush()
    s.add(PortfolioLot(holding_id=h.id, origin="buy", quantity=Decimal(qty), open_quantity=Decimal(open_qty),
                       cost_per_unit=Decimal("100"), acquired=date(2025, 1, 2)))  # fmt: skip
    s.flush()
    return h


def _bond(isin, maturity):
    return NS(isin=isin, maturity=maturity)


def _pass(day, *, bonds=None, navs=None, stocks=None):
    async def b():
        return bonds or []

    async def f():
        return navs or []

    async def st():
        return NS(rows=stocks or [])

    src = L.Sources(bonds=b, nav_rows=f, listings=st)
    return asyncio.run(L.run_pass(day, datetime(day.year, day.month, day.day, 11, 30, tzinfo=UTC), src))


def _trading_days(n):
    out, d = [], TODAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _bond_rule():
    from finresearch.suggest.profile import AlertRule

    return AlertRule(id="b1", kind="bond", metric="rating_changed")


def test_matured_and_missing_bonds_leave_tracked_and_alert_targets_one_missing_day_does_not(lc):
    """#293: a matured bond (maturity in Company.meta) and one missing from NSE's list for 10 trading days are no
    longer tracked, targeted or fetched; a single missing day changes nothing; the held bond is targeted first."""
    from finresearch.alerts.engine import targets
    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.disclosures.store import tracked

    with session_scope() as s:
        _company(s, f"bond-{LIVE_BOND.lower()}", "Example Live NCD", meta={"maturity": "2030-03-31"})
        _company(s, f"bond-{MATURED_BOND.lower()}", "Example Matured NCD", meta={"maturity": "2026-10-09"})
        _company(s, f"bond-{GONE_BOND.lower()}", "Example Vanishing NCD")  # maturity unknown
        _hold(s, "stock", "Example Held NCD", isin="INE0EO107045")  # a held debenture (no library company)
        t = tracked(s, TODAY)
        assert set(t.bonds) == {LIVE_BOND, GONE_BOND, "INE0EO107045"}
        assert t.ended[f"bond:{MATURED_BOND}"]["reason"] == "matured"
    listed = [_bond(LIVE_BOND, date(2030, 3, 31)), _bond("INE0EO107045", date(2031, 1, 1))]
    days = _trading_days(10)
    _pass(days[0], bonds=listed)  # one day missing: still tracked
    with session_scope() as s:
        assert GONE_BOND in tracked(s, days[0]).bonds
    for d in days[1:9]:
        _pass(d, bonds=listed)
    with session_scope() as s:
        assert GONE_BOND in tracked(s, days[8]).bonds  # 9 missing trading days: not yet
        assert s.query(Alert).filter(Alert.kind == L.ALERT_KIND).count() == 0
    res = _pass(days[9], bonds=listed)
    assert res["bond"]["ended"] == [GONE_BOND]
    _pass(days[9], bonds=listed)  # the same day's retry: no second alert
    with session_scope() as s:
        t = tracked(s, days[9])
        assert set(t.bonds) == {LIVE_BOND, "INE0EO107045"}
        assert t.ended[f"bond:{GONE_BOND}"]["reason"] == "missing"
        assert [x.instrument for x in targets(s, _bond_rule())] == ["INE0EO107045", LIVE_BOND]  # held first
        alerts = s.query(Alert).filter(Alert.kind == L.ALERT_KIND).all()
        assert len(alerts) == 1 and alerts[0].data["instrument"] == GONE_BOND
        assert "Example Vanishing NCD" in alerts[0].message
        # the held bond's maturity came from the list; nothing was guessed for the vanished one
        assert L.states(s, "bond")[f"bond:{GONE_BOND}"].get("maturity") is None


def test_a_short_or_failed_bond_list_is_no_pass(lc, monkeypatch):
    from finresearch.db import session_scope

    monkeypatch.setattr(L, "MIN_ROWS", {"bond": 2, "fund": 1, "stock": 1})
    with session_scope() as s:
        _company(s, f"bond-{GONE_BOND.lower()}", "Example Vanishing NCD")
    res = _pass(TODAY, bonds=[_bond(LIVE_BOND, None)])
    assert "not counted" in res["bond"]["skipped"]

    async def boom():
        raise TimeoutError("NSE did not answer")

    res = asyncio.run(L.run_pass(TODAY, datetime(2026, 10, 9, 11, 30, tzinfo=UTC), L.Sources(bonds=boom)))
    assert "TimeoutError" in res["failed"]["bond"]
    with session_scope() as s:
        assert L.states(s, "bond") == {}


def test_library_bond_maturity_is_stored_and_backfilled_unknown_stays_unknown(lc):
    from finresearch.db import session_scope
    from finresearch.db.models import Company
    from finresearch.orchestrator.bond import backfill_maturities

    with session_scope() as s:
        _company(s, f"bond-{LIVE_BOND.lower()}", "Example Live NCD")
        _company(s, f"bond-{GONE_BOND.lower()}", "Example Vanishing NCD")
        res = {r["slug"]: r for r in backfill_maturities(s, [NS(isin=LIVE_BOND, maturity=date(2030, 3, 31))])}
        assert res[f"bond-{LIVE_BOND.lower()}"]["maturity"] == "2030-03-31"
        assert res[f"bond-{GONE_BOND.lower()}"] == {"slug": f"bond-{GONE_BOND.lower()}", "maturity": None,
                                                     "source": "unknown"}  # fmt: skip
        gone = s.query(Company).filter(Company.slug == f"bond-{GONE_BOND.lower()}").one()
        assert "maturity" not in (gone.meta or {})


async def test_mcp_bond_analytics_answers_matured_instead_of_crashing_and_search_drops_matured(monkeypatch):
    """#293: bond_analytics raised ValueError (fincalc's coupon schedule) on a matured bond; it now says so."""
    import json
    from pathlib import Path

    from finresearch.adapters import nse_bonds
    from finresearch.fincalc.dates import today_ist
    from finresearch.mcp_server.server import bond_analytics, nse_bond_search

    fix = Path(__file__).parent / "fixtures" / "nse" / "bonds_live_trimmed.json"
    rows = nse_bonds.parse_live_bonds(json.loads(fix.read_text()))
    old = rows[0].model_copy(update={"isin": "INE0EO107052", "symbol": "EXOLDNCD", "maturity": today_ist()})

    async def fake_live(client=None):
        return [*rows, old]

    monkeypatch.setattr(nse_bonds, "live_bonds", fake_live)
    out = json.loads(await bond_analytics("INE906B07DF8", 1, "dirty", settlement="2029-02-05"))
    assert out["status"] == "matured" and out["matured_on"] == "2029-02-05" and "ytm" not in out
    out = json.loads(await bond_analytics("INE0EO107052", 1, "dirty"))
    assert out["status"] == "matured"
    assert json.loads(await nse_bond_search("EXOLDNCD")) == []  # matures today: left out
    assert json.loads(await nse_bond_search("875NHAI29"))[0]["isin"] == "INE906B07DF8"


def test_ended_fund_and_delisted_stock_watch_stop_once_with_one_alert(lc):
    """#293: a fund AMFI stopped listing and a watched stock gone from NSE's and BSE's lists end after 10 passes: one
    alert each, the stock's watch stops (its daily check no longer fails), neither is fetched or targeted again."""
    from finresearch.alerts.engine import targets
    from finresearch.db import session_scope
    from finresearch.db.models import Alert, MonitorJob, Watch
    from finresearch.disclosures.store import tracked
    from finresearch.suggest.profile import AlertRule

    with session_scope() as s:
        _company(s, "mf-999901", "Example Merged Fund - Direct Growth")
        _company(s, "mf-999902", "Example Live Fund - Direct Growth")
        co = _company(s, "eol-gone", "Example Delisted Ltd", nse_symbol="EXGONE", isin="INE0EO101011")
        w = Watch(company_id=co.id, kind="stock", nse_symbol="EXGONE", exchange="NSE", meta={})
        s.add(w)
        s.flush()
        s.add(MonitorJob(watch_id=w.id, kind="stock_daily", slot="EXGONE:stock:2026-11-01", result={}, params={},
                         due_at=datetime(2026, 11, 1, 11, tzinfo=UTC)))  # fmt: skip
        _hold(s, "stock", "Example Held Ltd", isin="INE0EO101029", sym="EXHELD")
    navs = [NS(code="999902")]
    stocks = [NS(nse_symbol="EXHELD", bse_code=None, isin="INE0EO101029")]
    for d in _trading_days(10):
        res = _pass(d, navs=navs, stocks=stocks)
    assert res["fund"]["ended"] == ["999901"] and res["stock"]["ended"] == ["EXGONE"]
    again = _pass(_trading_days(11)[-1], navs=navs, stocks=stocks)  # an 11th pass: nothing new
    assert again["fund"]["ended"] == [] and again["stock"]["ended"] == []
    with session_scope() as s:
        t = tracked(s, TODAY)
        assert set(t.funds) == {"999902"} and set(t.stocks) == {"EXHELD"}
        w = s.query(Watch).one()
        assert w.active is False and w.meta["ended"]["reason"] == "missing"
        assert s.query(MonitorJob).one().status == "cancelled"
        alerts = s.query(Alert).filter(Alert.kind == L.ALERT_KIND).all()
        assert sorted(a.data["instrument"] for a in alerts) == ["999901", "EXGONE"]
        assert [x.instrument for x in targets(s, AlertRule(id="f", kind="fund", metric="nav"))] == ["999902"]
        assert [x.instrument for x in targets(s, AlertRule(id="s", kind="stock", metric="price"))] == [
            "EXHELD"
        ]


def test_a_renamed_stock_is_found_by_its_isin_and_never_ends(lc):
    from finresearch.db import session_scope
    from finresearch.db.models import Watch

    with session_scope() as s:
        co = _company(s, "eol-renamed", "Example Renamed Ltd", nse_symbol="EXOLD", isin="INE0EO101037")
        s.add(Watch(company_id=co.id, kind="stock", nse_symbol="EXOLD", exchange="NSE", meta={}))
    for d in _trading_days(12):
        res = _pass(d, stocks=[NS(nse_symbol="EXNEW", bse_code=None, isin="INE0EO101037")])
    assert res["stock"]["missing"] == [] and res["stock"]["ended"] == []
    with session_scope() as s:
        assert s.query(Watch).one().active is True


# --------------------------------------------------------------------------- F&O: past expiries
def test_fno_api_drops_past_expiries_and_refuses_an_expired_contract(env, monkeypatch):
    from starlette.testclient import TestClient
    from test_fno import FakeFno  # expiries 2026-10-06 and 2026-10-13

    from finresearch.api import create_app
    from finresearch.fincalc import dates

    monkeypatch.setattr(dates, "now_ist", lambda: datetime(2026, 10, 7, 10, tzinfo=dates.IST))
    with TestClient(create_app(fno_client=FakeFno)) as c:
        assert c.get("/api/fno/NIFTY/expiries").json()["expiries"] == ["2026-10-13"]
        r = c.get("/api/fno/NIFTY/chain", params={"expiry": "2026-10-06"})
        assert r.status_code == 422 and "expired" in r.json()["detail"]
        body = {"symbol": "NIFTY", "expiry": "2026-10-06",
                "legs": [{"right": "call", "strike": "22800", "side": "buy", "lots": 1}]}  # fmt: skip
        assert c.post("/api/fno/strategy", json=body).status_code == 422


async def test_fno_signal_picks_the_first_live_expiry_and_rejects_a_past_one(monkeypatch):
    from test_fno import FakeFno

    from finresearch.fincalc import dates
    from finresearch.signals import fno

    asked = []

    class Spy(FakeFno):
        async def option_chain(self, symbol, expiry):
            asked.append(expiry)
            return await super().option_chain(symbol, expiry)

    monkeypatch.setattr(dates, "now_ist", lambda: datetime(2026, 10, 7, 10, tzinfo=dates.IST))
    monkeypatch.setattr(fno, "_client", Spy)
    with pytest.raises(fno.ExpiredContract):
        await fno.fno_signal("NIFTY", {"expiry": "2026-10-06", "log": "0"})
    # the rest of the signal is test_fno_signal's business; only the expiry chosen matters here
    with contextlib.suppress(Exception):
        await fno.fno_signal("NIFTY", {"log": "0"})
    assert asked == [date(2026, 10, 13)]


def test_rules_pinned_to_an_expired_contract_are_disabled_once_with_a_note(lc):
    from finresearch.alerts.engine import EXPIRED_KIND, retire_expired
    from finresearch.db import session_scope
    from finresearch.db.models import Alert
    from finresearch.suggest.advisor import load_profile, save_profile
    from finresearch.suggest.profile import AlertRule

    with session_scope() as s:
        p = load_profile(s)
        p.alert_rules = [
            AlertRule(
                id="old", kind="fno", metric="spot", instrument="NIFTY", params={"expiry": "2026-10-06"}
            ),
            AlertRule(
                id="live", kind="fno", metric="spot", instrument="NIFTY", params={"expiry": "2026-10-13"}
            ),
            AlertRule(id="any", kind="fno", metric="days_to_expiry", instrument="NIFTY"),
        ]
        save_profile(s, p)
    with session_scope() as s:
        assert retire_expired(s, date(2026, 10, 7)) == ["old"]
    with session_scope() as s:
        assert retire_expired(s, date(2026, 10, 7)) == []  # already off: no second alert
        rules = {r.id: r for r in load_profile(s).alert_rules}
        assert rules["old"].enabled is False and "contract expired" in rules["old"].description
        assert rules["live"].enabled and rules["any"].enabled
        assert s.query(Alert).filter(Alert.kind == EXPIRED_KIND).count() == 1


# --------------------------------------------------------------------------- portfolio-aware targeting
def _valuation_cache(s, weights: dict[int, float]):
    from finresearch.portfolio import cache

    cache.write(s, cache.VALUATION, {"holdings": {str(h): {"weight_pct": w} for h, w in weights.items()}})


def test_held_stocks_get_iv_intraday_sector_pe_and_alert_coverage_by_weight(lc):
    """#293: the IV recorder, the after-close intraday archive, the sector-P/E archive and stock alert rules read
    only stock watches; open holdings are covered now, largest first. A sold-out holding is not."""
    from finresearch.alerts.engine import targets
    from finresearch.db import session_scope
    from finresearch.monitor import archive, intraday, iv
    from finresearch.suggest.profile import AlertRule

    with session_scope() as s:
        small = _hold(s, "stock", "Example Small Ltd", sym="EXSMALL")
        big = _hold(s, "stock", "Example Big Ltd", sym="EXBIG")
        _hold(s, "stock", "Example Sold Ltd", sym="EXSOLD", open_qty="0")
        _valuation_cache(s, {small.id: 5.0, big.id: 30.0})
    lots = {"EXBIG": {}, "EXSMALL": {}, "EXSOLD": {}}
    assert iv.symbols(lots)[3:] == ["EXBIG", "EXSMALL"]
    assert [k for kind, k in intraday.targets(TODAY) if kind == "equity"] == ["EXBIG", "EXSMALL"]
    assert archive._live_watched() == ["EXBIG", "EXSMALL"]
    with session_scope() as s:
        got = targets(s, AlertRule(id="s", kind="stock", metric="price"))
    assert [t.instrument for t in got] == ["EXBIG", "EXSMALL"] and all(t.watch_id is None for t in got)


def test_has_holdings_counts_open_lots_only(lc):
    from finresearch.db import session_scope
    from finresearch.monitor.portfolio_daily import has_holdings

    with session_scope() as s:
        _hold(s, "stock", "Example Sold Ltd", sym="EXSOLD", open_qty="0")
        _hold(s, "mf", "Example Redeemed Fund", code="999903", open_qty="0")
    assert has_holdings() == (False, False)  # only sold-out holdings: the daily passes do not start
    with session_scope() as s:
        _hold(s, "mf", "Example Fund", code="999904")
    assert has_holdings() == (True, True)


async def test_events_cap_keeps_the_largest_positions_and_counts_the_rest(lc, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.monitor import portfolio_daily as pd
    from finresearch.portfolio import cache

    monkeypatch.setattr(pd, "MAX_INSTRUMENTS", 2)
    holdings = {str(i): {"asset_type": "stock", "name": f"Example {sym}", "nse_symbol": sym, "weight_pct": w,
                         "signal": {"instrument": sym}}
                for i, (sym, w) in enumerate((("AAA", 1.0), ("ZZZ", 40.0), ("MMM", 20.0)))}  # fmt: skip
    with session_scope() as s:
        cache.write(s, cache.VALUATION, {"holdings": holdings})
    asked = []

    async def events(sym):
        asked.append(sym)
        return {"actions": [], "board_meetings": [], "results": []}

    deps = NS(pf_stock_events=events, pf_spacing_s=0)
    res = await pd.events_pass(deps, datetime(2026, 10, 9, 11, tzinfo=UTC), TODAY, {})
    assert asked == ["ZZZ", "MMM"] and res["skipped"] == 1 and res["skipped_symbols"] == ["AAA"]


def test_ex_date_reminder_tells_a_holder_to_keep_not_to_buy():
    from finresearch.monitor.jobs import ex_date_message

    ca = NS(
        subject="Interim Dividend - Rs 5 Per Share",
        ex_date=date(2026, 10, 14),
        record_date=date(2026, 10, 14),
    )
    held = ex_date_message("EXHELD", ca, True)
    assert "you hold it" in held and "buy before" not in held and "record date 2026-10-14" in held
    assert ex_date_message("EXWATCH", ca, False).endswith("buy before then to be entitled")


async def test_an_ended_fund_or_stock_is_not_fetched_by_the_daily_signal_and_events_passes(lc):
    from finresearch.db import session_scope
    from finresearch.monitor import portfolio_daily as pd
    from finresearch.portfolio import cache

    holdings = {"1": {"asset_type": "mf", "name": "Example Merged Fund", "weight_pct": 10.0,
                      "signal": {"asset": "fund", "instrument": "999901"}},
                "2": {"asset_type": "stock", "name": "Example Gone Ltd", "nse_symbol": "EXGONE", "weight_pct": 5.0,
                      "signal": {"asset": "stock", "instrument": "EXGONE"}},
                "3": {"asset_type": "stock", "name": "Example Live Ltd", "nse_symbol": "EXLIVE", "weight_pct": 5.0,
                      "signal": {"asset": "stock", "instrument": "EXLIVE"}}}  # fmt: skip
    now = datetime(2026, 10, 9, 11, tzinfo=UTC)
    with session_scope() as s:
        cache.write(s, cache.VALUATION, {"holdings": holdings})
        for k in ("fund:999901", "stock:EXGONE"):
            L.save(s, k, {"ended": {"on": "2026-10-01", "reason": "missing", "detail": "gone"}}, now)
    assert set(pd._instruments({})) == {("stock", "EXLIVE")}
    asked = []

    async def events(sym):
        asked.append(sym)
        return {"actions": [], "board_meetings": [], "results": []}

    await pd.events_pass(NS(pf_stock_events=events, pf_spacing_s=0), now, TODAY, {})
    assert asked == ["EXLIVE"]
