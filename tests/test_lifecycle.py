"""End of life of tracked instruments and portfolio-aware targeting (#293). Synthetic instruments only."""

from __future__ import annotations

import asyncio
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
    assert L.ended({"maturity": "2026-10-05", "last_seen": "2026-10-12"}, D0 + timedelta(days=11))["reason"] == "matured"


def test_maturity_unknown_is_never_matured():
    assert L.matured(None, D0) is False and L.matured("", D0) is False
    assert L.matured("2026-10-01", D0) is True  # matures today: ended today (no trade settles after it)
    assert L.matured(date(2026, 10, 2), D0) is False
    assert L.ended({}, D0) is None


# --------------------------------------------------------------------------- the daily pass (database)
TODAY = date(2026, 10, 9)
LIVE_BOND, MATURED_BOND, GONE_BOND = "INE0EO107011", "INE0EO107029", "INE0EO107037"  # synthetic debenture ISINs


@pytest.fixture
def lc(env, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.db.models import (Alert, AlertEvalSlot, AlertRuleState, Company, DisclosureFeed, InvestorProfile,
                                       MonitorJob, PortfolioDisposal, PortfolioHolding, PortfolioLot, PortfolioSetting,
                                       PortfolioTxn, Watch)  # fmt: skip

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
