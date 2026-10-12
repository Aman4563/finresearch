"""#297: expected SIP instalments between statements, on synthetic data only (cas_synth's invented schemes).

Expected values are worked by hand in the comments: stamp duty 0.005 % of the amount (₹3,000 → ₹0.15), units =
(amount − stamp duty) ÷ that day's NAV."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal as D

from cas_synth import Scheme, Statement, build_cas, sip_statement
from test_portfolio import ORIGIN, client, post_cas  # noqa: F401  (the portfolio API fixture)

from finresearch.adapters.amfi import SchemeNav
from finresearch.portfolio import sip_estimates as se

VALUE_ISIN = "INF000X01050"  # the invented Value Fund's ISIN in cas_synth.sip_statement


def nav_row(isin: str, nav: str, day: date, code: str = "999901") -> SchemeNav:
    return SchemeNav(code, "Example Value Fund - Direct Plan - Growth", "Direct", "Growth", isin, None, D(nav), day,
                     "Equity Scheme - Value Fund", "Example Mutual Fund")  # fmt: skip


def test_units_net_of_stamp_duty_and_unknown_without_a_nav():
    assert se.stamp_duty(D("3000")) == D("0.15")  # 3,000 × 0.00005
    assert se.stamp_duty(D("40000")) == D("2.00")  # the CAS row in cas_synth
    # (3,000 − 0.15) ÷ 104.25 = 2,999.85 ÷ 104.25 = 28.775539568... → 28.775540
    assert se.estimate_units(D("3000"), D("104.25")) == D("28.775540")
    assert se.estimate_units(D("3000"), None) is None  # unknown, never 0
    assert se.estimate_units(D("3000"), D("0")) is None


def test_instalment_days_and_nav_day():
    # last 05-Sep, statement to 29-Sep, today 10-Nov: 05-Oct and 05-Nov
    assert se.inferred_days(date(2026, 9, 5), date(2026, 9, 29), date(2026, 11, 10)) == [date(2026, 10, 5),
                                                                                         date(2026, 11, 5)]  # fmt: skip
    assert (
        se.inferred_days(date(2026, 9, 5), date(2026, 10, 5), date(2026, 11, 4)) == []
    )  # 05-Oct is in the statement
    # Kite: next instalment 10-Dec, statement to 29-Sep, today 15-Nov: 10-Oct and 10-Nov
    assert se.kite_days(date(2026, 12, 10), date(2026, 9, 29), date(2026, 11, 15)) == [date(2026, 10, 10),
                                                                                       date(2026, 11, 10)]  # fmt: skip
    assert se.nav_day(date(2026, 12, 5)) == date(2026, 12, 7)  # Saturday -> Monday
    assert se.nav_day(date(2026, 11, 5), {date(2026, 11, 5)}) == date(2026, 11, 6)  # a holiday -> next day
    assert se.nav_day(date(2026, 10, 5)) == date(2026, 10, 5)


def import_sip_cas(client) -> None:  # noqa: F811
    """cas_synth's invented ISINs fail the checksum, so the importer keeps none: give the Value Fund an ISIN as a
    real CAS would."""
    from sqlalchemy import update

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding

    post_cas(client, build_cas(sip_statement()), dry=False)
    with session_scope() as s:
        s.execute(update(PortfolioHolding).where(PortfolioHolding.name.like("Example Value Fund%"))
                  .values(isin=VALUE_ISIN))  # fmt: skip


def _plan_and_fill(today: date, navs) -> dict:
    from finresearch.db import session_scope
    from finresearch.portfolio.report import load

    with session_scope() as s:
        data = load(s)
        plans = [p for h in data.holdings if (p := se.plan_inferred(h, data.txns.get(h.id, []), today))]
        added = se.store_plans(s, plans)
        filled = asyncio.run(se.fill_navs(s, today, navs))
    return {"plans": plans, "added": added, **filled}


def test_only_a_running_sip_is_planned_and_units_come_from_that_days_nav(client):  # noqa: F811
    import_sip_cas(client)
    asked: list[date] = []

    async def navs(day):
        asked.append(day)
        return [nav_row(VALUE_ISIN, "104.25", date(2026, 10, 5))] if day == date(2026, 10, 5) else []

    got = _plan_and_fill(date(2026, 11, 10), navs)
    # Value: SIP on the 5th, "not in a statement yet"; Index: its 05-Sep instalment was rejected -> no plan;
    # Midcap: two buys, no SIP
    assert [(p.amount, p.days, p.basis) for p in got["plans"]] == [
        (D("3000"), (date(2026, 10, 5), date(2026, 11, 5)), "sip-inference")]  # fmt: skip
    assert got["added"] == 2 and (got["estimated"], got["nav_unknown"]) == (1, 1)
    assert asked == [date(2026, 10, 5), date(2026, 11, 5)]  # one NAV request per day
    # a second run adds nothing and asks again only for the unknown NAV
    asked.clear()
    again = _plan_and_fill(date(2026, 11, 10), navs)
    assert again["added"] == 0 and asked == [date(2026, 11, 5)]


def _snap(today: date, price: str) -> dict:
    from finresearch.db import session_scope
    from finresearch.portfolio.report import load, snapshot
    from finresearch.portfolio.valuation import PriceInfo

    with session_scope() as s:
        prices = {h.id: PriceInfo(price=D(price), as_of=today.isoformat(), source="AMFI NAV")
                  for h in load(s).holdings}  # fmt: skip
        return snapshot(s, prices, today)


def _counts() -> tuple[int, int, int]:
    from sqlalchemy import func, select

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioDisposal, PortfolioLot, PortfolioTxn

    with session_scope() as s:
        return tuple(s.scalar(select(func.count()).select_from(t)) for t in (PortfolioTxn, PortfolioLot,
                                                                              PortfolioDisposal))  # fmt: skip


def test_estimates_are_shown_apart_and_never_enter_the_totals(client):  # noqa: F811
    import_sip_cas(client)
    today = date(2026, 11, 10)
    before, counts = _snap(today, "105"), _counts()

    async def navs(day):
        return [nav_row(VALUE_ISIN, "104.25", date(2026, 10, 5))] if day == date(2026, 10, 5) else []

    _plan_and_fill(today, navs)
    after = _snap(today, "105")
    for k in ("summary", "complete", "allocation", "timeline", "invested", "dividends"):
        assert after[k] == before[k], k  # the estimates change no total, allocation or completeness
    assert _counts() == counts  # no transaction, lot or disposal was written
    rows = {h["name"].split(" - ")[0]: h for h in after["holdings"]}
    for name in ("Example Value Fund", "Example Index Fund"):
        assert (
            rows[name]["units"] == {h["name"].split(" - ")[0]: h for h in before["holdings"]}[name]["units"]
        )
    est = rows["Example Value Fund"]["sip_estimate"]
    # 05-Oct known (28.775540), 05-Nov NAV unknown: the total is unknown, never the partial sum
    assert (est["instalments"], est["units"], est["units_unknown"], est["estimated_value"]) == (
        2,
        None,
        1,
        None,
    )
    assert est["label"] == ("units estimated for 1 of 2 instalments (2 instalments since 2026-09-29; 1 NAV unknown), "
                            "waiting for statement")  # fmt: skip
    assert rows["Example Index Fund"]["sip_estimate"] is None
    assert after["estimated"]["estimated_value"] is None and after["estimated"]["amount"] == 6000.0

    async def navs2(day):
        return [nav_row(VALUE_ISIN, "105", date(2026, 11, 5))]

    _plan_and_fill(today, navs2)
    done = _snap(today, "105")
    est = {h["name"].split(" - ")[0]: h for h in done["holdings"]}["Example Value Fund"]["sip_estimate"]
    # 05-Nov: 2,999.85 ÷ 105 = 28.570000; total 28.775540 + 28.570000 = 57.345540; × 105 = 6,021.2817
    assert est["units"] == 57.3455 and est["estimated_value"] == 6021.28
    assert est["label"] == "+57.346 units estimated (2 instalments since 2026-09-29), waiting for statement"
    assert done["estimated"] == {"funds": 1, "instalments": 2, "amount": 6000.0, "estimated_value": 6021.28,
                                 "value_unknown_funds": 0, "note": done["estimated"]["note"]}  # fmt: skip
    assert done["summary"] == before["summary"] and done["complete"] == before["complete"]


def next_statement(rows: list[tuple[str, str, str, str, str, str]], close: str) -> bytes:
    """The Value Fund from 01-Oct to 10-Nov-2026 (opening 90 units), with `rows`."""
    value = Scheme("EXVF", "Example Value Fund - Direct Plan - Growth", "INF000X01050", "90.000", rows, close,
                   "10-Nov-2026", "105.0000", f"{D(close) * 105:,.2f}", "12,000.00")  # fmt: skip
    return build_cas(Statement(start="01-Oct-2026", end="10-Nov-2026", schemes=[value]))


def _statuses() -> dict[date, tuple[str, bool]]:
    from sqlalchemy import select

    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioExpectedTxn

    with session_scope() as s:
        return {r.expected_day: (r.status, r.matched_txn_id is not None)
                for r in s.scalars(select(PortfolioExpectedTxn))}  # fmt: skip


OCT5, NOV5 = date(2026, 10, 5), date(2026, 11, 5)
BUY_07OCT = ("07-Oct-2026", "SIP Purchase Instalment 4/12", "3,000.00", "28.776", "104.2500", "118.776")


def test_next_cas_confirms_a_matching_buy_and_misses_the_rest(client):  # noqa: F811
    import_sip_cas(client)

    async def navs(day):
        return []

    _plan_and_fill(date(2026, 11, 10), navs)
    assert _statuses() == {OCT5: ("expected", False), NOV5: ("expected", False)}
    done = post_cas(client, next_statement([BUY_07OCT], "118.776"), dry=False, name="cas2.pdf").json()
    assert done["reconciled"], done
    # 07-Oct ₹3,000 is 2 days from 05-Oct (within ±3) at the same amount: confirmed; 05-Nov + 3 = 08-Nov is covered
    # by the statement (to 10-Nov) with no buy: missed
    assert _statuses() == {OCT5: ("confirmed", True), NOV5: ("missed", False)}
    snap = client.get("/api/portfolio").json()
    assert snap["estimated"] is None
    assert all(h["sip_estimate"] is None for h in snap["holdings"])
    # deleting that statement reopens the instalment its buy had confirmed
    assert client.delete(f"/api/portfolio/imports/{done['import_id']}", headers=ORIGIN).status_code == 200
    assert _statuses()[OCT5] == ("expected", False)


def test_rejected_and_out_of_band_instalments(client):  # noqa: F811
    import_sip_cas(client)

    async def navs(day):
        return []

    _plan_and_fill(date(2026, 11, 10), navs)
    # 05-Oct: ₹3,040 is 1.33 % over ₹3,000 (outside ±1 %): not a match -> missed; 05-Nov: a rejected purchase
    rows = [("06-Oct-2026", "SIP Purchase Instalment 4/12", "3,040.00", "29.160", "104.2500", "119.160"),
            ("05-Nov-2026", "*** SIP Purchase Rejected - Insufficient Balance ***", "", "", "", "")]  # fmt: skip
    done = post_cas(client, next_statement(rows, "119.160"), dry=False, name="cas3.pdf").json()
    assert done["reconciled"], done
    assert _statuses() == {OCT5: ("missed", False), NOV5: ("rejected", False)}


def test_an_instalment_near_the_statement_end_waits_for_the_next_one(client):  # noqa: F811
    import_sip_cas(client)

    async def navs(day):
        return []

    _plan_and_fill(date(2026, 11, 10), navs)
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioHolding

    with session_scope() as s:
        h = s.query(PortfolioHolding).filter(PortfolioHolding.isin == VALUE_ISIN).one()
        # a statement to 07-Nov covers 05-Nov but not 05-Nov + 3 days: the buy may be in the next statement
        assert se.reconcile(s, h.id, date(2026, 11, 7), []) == {"confirmed": 0, "missed": 1, "rejected": 0}
    assert _statuses()[NOV5] == ("expected", False)
