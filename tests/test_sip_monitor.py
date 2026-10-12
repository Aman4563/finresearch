"""#297: the nightly SIP-estimate pass (AMFI NAVs, Kite SIPs/orders from fixtures) and the weekly AMFI scheme-master
check. Synthetic holdings only; Kite answers are fixtures shaped like kite.trade/docs/connect/v3/mutual-funds/."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_portfolio import ORIGIN, client, post_cas  # noqa: F401
from test_sip_estimates import VALUE_ISIN, import_sip_cas, nav_row

from finresearch.monitor import scheme_master as sm
from finresearch.monitor import sip_estimates as job
from finresearch.portfolio.connectors.zerodha import KiteMfOrder, KiteSip

FIX = Path(__file__).parent / "fixtures" / "amfi"


@pytest.fixture
def clean(client):  # noqa: F811
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(
            text(
                "DELETE FROM alert_eval_slot WHERE slot LIKE 'sip_estimates:%' OR slot LIKE 'scheme_master:%'"
            )
        )
        s.execute(text("DELETE FROM disclosure_feed WHERE dataset = 'scheme_master'"))
        s.execute(text("DELETE FROM alert WHERE kind = 'fund_scheme_changed'"))
    return client


def ist(y, m, d, hh, mm) -> datetime:  # an IST wall time as UTC
    from finresearch.fincalc.dates import IST

    return datetime(y, m, d, hh, mm, tzinfo=IST).astimezone(UTC)


def test_sip_pass_runs_after_2330_ist_on_trading_days_only():
    hol = {date(2026, 11, 10)}
    assert job.due_slot(ist(2026, 11, 12, 23, 29), hol) is None
    assert job.due_slot(ist(2026, 11, 12, 23, 30), hol) == "sip_estimates:2026-11-12"
    assert job.due_slot(ist(2026, 11, 14, 23, 45), hol) is None  # Saturday
    assert job.due_slot(ist(2026, 11, 10, 23, 45), hol) is None  # a trading holiday


def _rows() -> dict[date, tuple]:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioExpectedTxn

    with session_scope() as s:
        return {r.expected_day: (r.basis, r.status, r.nav_day, r.est_units, r.est_nav)
                for r in s.scalars(select(PortfolioExpectedTxn))}  # fmt: skip


def kite_sip(status="ACTIVE") -> KiteSip:
    return KiteSip("100000000000001", VALUE_ISIN, "EXAMPLE VALUE FUND", status, "monthly", D(3000), 10,
                   date(2026, 12, 10), date(2026, 11, 10))  # fmt: skip


def test_kite_sip_is_the_basis_and_its_orders_give_units(clean):
    import_sip_cas(clean)
    order = KiteMfOrder(
        "o1", VALUE_ISIN, "COMPLETE", "BUY", "sip", D(3000), D("28.5"), D("105.2"), date(2026, 11, 10)
    )

    async def kite(now):
        return [kite_sip()], [order]

    async def navs(day):
        return [nav_row(VALUE_ISIN, "104", date(2026, 10, 12))]

    res = asyncio.run(job.run(date(2026, 11, 12), ist(2026, 11, 12, 23, 30), job.Sources(navs=navs, kite=kite),
                              holidays=set()))  # fmt: skip
    assert res["basis"] == "kite+inference" and res["planned"] == 2 and res["kite_units"] == 1
    # Kite's next instalment 10-Dec: 10-Oct (a Saturday: NAV of Monday 12-Oct) and 10-Nov after the 29-Sep statement.
    # 10-Oct: 2,999.85 ÷ 104 = 28.844711538... → 28.844712; 10-Nov: Kite's own fill (28.5 units at 105.2)
    assert _rows() == {
        date(2026, 10, 10): ("kite", "expected", date(2026, 10, 12), D("28.844712"), D("104")),
        date(2026, 11, 10): ("kite", "expected", date(2026, 11, 10), D("28.5"), D("105.2"))}  # fmt: skip


def test_a_paused_kite_sip_plans_nothing_and_a_kite_failure_falls_back_to_the_inference(clean):
    import_sip_cas(clean)

    async def paused(now):
        return [kite_sip("PAUSED")], []

    async def navs(day):
        return []

    run = lambda kite: asyncio.run(job.run(date(2026, 11, 12), ist(2026, 11, 12, 23, 30),  # noqa: E731
                                           job.Sources(navs=navs, kite=kite), holidays=set()))  # fmt: skip
    assert run(paused)["planned"] == 0 and _rows() == {}

    async def broken(now):
        raise RuntimeError("Kite MF read failed: HTTP 500")

    res = run(broken)
    assert res["kite_failed"] == "Kite MF read failed: HTTP 500" and res["basis"] == "inference"
    # the inferred SIP (5th of the month): 05-Oct and 05-Nov, NAVs unknown -> units unknown, never 0
    assert {d: (b, u) for d, (b, _, _, u, _) in _rows().items()} == {
        date(2026, 10, 5): ("sip-inference", None), date(2026, 11, 5): ("sip-inference", None)}  # fmt: skip


def test_sip_step_claims_the_night_once(clean):
    import_sip_cas(clean)

    async def navs(day):
        return []

    async def kite(now):
        return None  # Zerodha not connected

    src = job.Sources(navs=navs, kite=kite)
    now = ist(2026, 11, 12, 23, 31)
    first = asyncio.run(job.sip_estimates_step(now, src, holidays=set()))
    assert first["sip_estimates"]["planned"] == 2
    assert asyncio.run(job.sip_estimates_step(now, src, holidays=set())) == {}


# --------------------------------------------------------------------------- weekly AMFI scheme master
def test_scheme_master_parse_of_amfis_file():
    from finresearch.adapters.amfi import parse_scheme_master

    rows = parse_scheme_master((FIX / "scheme_master_trimmed.csv").read_text())
    by = {r.code: r for r in rows}
    assert len(rows) == 6
    assert by["100033"].isins == ("INF209K01165",)
    assert by["100034"].isins == ("INF209K01157", "INF209K01CE5")  # two ISINs run together
    assert by["100039"].isins == ()  # no ISIN
    assert by["118804"].isins == ("INF204K01B81",) and by["118804"].scheme_type == "Interval Fund"
    assert by["100033"].closure == date(1995, 2, 24) and by["100373"].closure is None
    with pytest.raises(Exception, match="no column"):
        parse_scheme_master("<html>maintenance</html>\n")


def test_scheme_master_due_once_a_week_from_monday_0700_ist():
    assert sm.due_slot(ist(2026, 10, 12, 6, 59)) is None  # Monday before 07:00
    assert sm.due_slot(ist(2026, 10, 12, 7, 0)) == "scheme_master:2026-W42"
    assert (
        sm.due_slot(ist(2026, 10, 15, 22, 0)) == "scheme_master:2026-W42"
    )  # asleep Monday: Thursday's tick runs it
    assert sm.due_slot(ist(2026, 10, 18, 23, 0)) == "scheme_master:2026-W42"  # Sunday: same week


def _fund(code: str | None, isin: str | None, name: str, folio: str = "1") -> None:
    """An open fund holding (one lot of 10 units): invented names and codes."""
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding, PortfolioLot

    with session_scope() as s:
        h = PortfolioHolding(ikey=f"MF:{code or isin}", account=f"Example folio {folio}", asset_type="mf", name=name,
                             scheme_code=code, isin=isin, meta={})  # fmt: skip
        s.add(h)
        s.flush()
        s.add(PortfolioLot(holding_id=h.id, acquired=date(2026, 1, 5), origin="buy", quantity=D(10),
                           open_quantity=D(10), cost_per_unit=D(100)))  # fmt: skip


def _master_and_navs(extra_codes: int = 6000):
    """A synthetic master and NAVAll big enough to pass MIN_ROWS: filler schemes 200000.. plus the given funds."""
    from finresearch.adapters.amfi import SchemeMaster, SchemeNav

    def m(code, isins=()):
        return SchemeMaster(code, f"Filler {code}", "Example AMC", "Open Ended", "Equity", tuple(isins), None)

    def n(code, day):
        return SchemeNav(code, f"Filler {code}", None, None, None, None, D(10), day, "Equity", "Example AMC")

    master = [m(str(200000 + i)) for i in range(extra_codes)]
    navs = [n(str(200000 + i), date(2026, 10, 9)) for i in range(3000)]
    master += [m("900001", ["INF000Z01011"]), m("900003", ["INF000Z01037"])]
    navs += [n("900001", date(2026, 10, 9)), n("900003", date(2026, 9, 25))]  # 900003: 14 days behind
    return master, navs


def _alerts() -> list[dict]:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import Alert

    with session_scope() as s:
        return [
            {"message": a.message, **a.data}
            for a in s.scalars(select(Alert).where(Alert.kind == sm.ALERT_KIND))
        ]


def _check(master, navs, day=date(2026, 10, 12)):
    from finresearch.db import session_scope

    with session_scope() as s:
        return sm.check(s, master, navs, day, ist(day.year, day.month, day.day, 8, 0))


def test_scheme_master_flags_vanished_codes_isins_and_stale_navs_once_per_fund(clean):
    _fund("900001", "INF000Z01011", "Example Healthy Fund")
    _fund("900002", None, "Example Merged Fund")  # code gone from the master
    _fund(None, "INF000Z01029", "Example Renamed Fund")  # ISIN gone
    _fund("900003", "INF000Z01037", "Example Stalled Fund")  # NAV 25-Sep vs NAVAll's 09-Oct: 14 > 7 days
    _fund(
        "900003", "INF000Z01037", "Example Stalled Fund", folio="2"
    )  # a second folio: still one fund, one alert
    _fund(None, None, "Example Unidentified Fund")
    master, navs = _master_and_navs()
    res = _check(master, navs)
    assert res == {"checked": 4, "unidentified": 1, "flagged": 3, "alerted": 3}
    got = {a["instrument"]: a for a in _alerts()}
    assert set(got) == {"900002", "INF000Z01029", "900003"}
    assert got["900002"]["issues"] == ["code_vanished", "nav_stopped"]  # gone from both files
    assert "its AMFI scheme code 900002 is no longer in AMFI's scheme master" in got["900002"]["message"]
    assert got["INF000Z01029"]["issues"] == ["isin_vanished"]
    assert got["900003"]["issues"] == ["nav_stopped"] and len(got["900003"]["holdings"]) == 2
    assert "last NAV 2026-09-25, while NAVAll has NAVs to 2026-10-09" in got["900003"]["message"]
    # next week: the same issues raise nothing new
    assert _check(master, navs, date(2026, 10, 19))["alerted"] == 0 and len(_alerts()) == 3
    # the stalled NAV resumes (re-armed), then stalls again: one new alert
    from finresearch.adapters.amfi import SchemeNav

    fresh = [r for r in navs if r.code != "900003"] + [
        SchemeNav("900003", "x", None, None, None, None, D(10), date(2026, 10, 9), "Equity", "Example AMC")]  # fmt: skip
    assert _check(master, fresh, date(2026, 10, 26))["flagged"] == 2
    assert _check(master, navs, date(2026, 11, 2))["alerted"] == 1 and len(_alerts()) == 4


def test_a_short_master_or_navall_judges_nothing(clean):
    _fund("900002", None, "Example Merged Fund")
    master, navs = _master_and_navs()
    assert "skipped" in _check(master[:100], navs)
    assert "skipped" in _check(master, navs[:100])
    assert _alerts() == []


def test_schedule_lists_the_new_jobs():
    from finresearch.monitor import scheduler
    from finresearch.suggest.profile import WatchWindows

    sj = scheduler.schedule_json(WatchWindows())
    assert sj["sip_estimates"] == {"after": "23:30", "trading_days": True}
    assert sj["scheme_master"] == {"weekly_from": "Monday 07:00"}


# --------------------------------------------------------------------------- projections follow the statements
def _night(day: date) -> dict:
    async def navs(d):
        return []

    async def kite(now):
        return None

    return asyncio.run(job.run(day, ist(day.year, day.month, day.day, 23, 30), job.Sources(navs=navs, kite=kite),
                               holidays=set()))  # fmt: skip


def test_a_late_instalment_moves_the_inferred_day_without_a_second_row(clean):
    from test_sip_estimates import BUY_07OCT, next_statement

    import_sip_cas(clean)
    assert _night(date(2026, 11, 12))["planned"] == 2  # 05-Oct and 05-Nov
    # the next CAS ends 06-Nov: the October instalment was allotted on 07-Oct (confirms 05-Oct); 05-Nov + 3 days is
    # after its end, so 05-Nov is still expected
    done = post_cas(
        clean, next_statement([BUY_07OCT], "118.776", end="06-Nov-2026"), dry=False, name="c2.pdf"
    ).json()
    assert done["reconciled"], done
    # tonight the inference starts from 07-Oct and plans 07-Nov: the same instalment as 05-Nov, not a second one
    res = _night(date(2026, 11, 12))
    assert (res["planned"], res["pruned"]) == (0, 0)
    assert {d: st for d, (_, st, *_) in _rows().items()} == {date(2026, 10, 5): "confirmed",
                                                             date(2026, 11, 5): "expected"}  # fmt: skip


def test_a_sip_the_statement_shows_missed_loses_its_projections(clean):
    from test_sip_estimates import next_statement

    import_sip_cas(clean)
    _night(date(2026, 11, 12))
    # a CAS to 07-Nov with no October buy: 05-Oct missed; 05-Nov not yet covered (05-Nov + 3 > 07-Nov) ...
    done = post_cas(clean, next_statement([], "90.000", end="07-Nov-2026"), dry=False, name="c3.pdf").json()
    assert done["reconciled"], done
    assert {d: st for d, (_, st, *_) in _rows().items()} == {date(2026, 10, 5): "missed",
                                                             date(2026, 11, 5): "expected"}  # fmt: skip
    # ... but the SIP now reads "missed" (63 days from 05-Sep to 07-Nov), so nothing is projected and 05-Nov goes
    res = _night(date(2026, 11, 12))
    assert (res["planned"], res["pruned"]) == (0, 1)
    assert {d: st for d, (_, st, *_) in _rows().items()} == {date(2026, 10, 5): "missed"}
    snap = clean.get("/api/portfolio").json()
    assert snap["estimated"] is None and all(h["sip_estimate"] is None for h in snap["holdings"])


def test_a_kite_sip_on_a_fund_held_in_two_folios_falls_back_to_the_inference():
    from types import SimpleNamespace as NS

    from finresearch.portfolio.report import Loaded

    lot = NS(open_quantity=D(1))
    a = NS(id=1, asset_type="mf", isin=VALUE_ISIN, name="x", meta={})
    b = NS(id=2, asset_type="mf", isin=VALUE_ISIN, name="y", meta={})
    plans, kite_isins, notes = job.plans_for(Loaded([a, b], {}, {1: [lot], 2: [lot]}, {}), date(2026, 11, 12),
                                             [kite_sip()])  # fmt: skip
    assert plans == [] and kite_isins == {}  # no statement units and no buys: nothing inferred either
    assert notes == [f"holding {i}: a Kite SIP on an ISIN held in 2 folios: inferred instead" for i in (1, 2)]
