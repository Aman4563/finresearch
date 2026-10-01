"""ELSS lock-in tracker (issue #176) on synthetic CAS-like lots only (Example names, fake scheme codes, test database).

Every expected value is worked out by hand in the comments from the rule in portfolio.elss: each lot is locked for 3
years from its allotment and can be redeemed from the day after the third anniversary.

The shared scenario (`SCENARIO`), valued on 1-Oct-2026 at a NAV of ₹50:
- 10-May-2022 lump sum 20 units                  → unlocks 11-May-2025
- SIP of 10 units on 10-Aug, 10-Sep, 10-Oct and 10-Nov-2023 → unlock 11-Aug, 11-Sep, 11-Oct and 11-Nov-2026
- bonus 1:10 on 15-Jan-2024: 60 units held → 6 bonus units, taken as allotted that day → unlock 16-Jan-2027
- redemption of 15 units on 15-Jun-2025: FIFO takes them from the May-2022 lot (free since 11-May-2025) → 5 left
Open on 1-Oct-2026: unlocked 5 + 10 (Aug) + 10 (Sep) = 25 units = ₹1,250; locked 10 (Oct) + 10 (Nov) + 6 (bonus)
= 26 units = ₹1,300. Next unlock 11-Oct-2026 (10 days): 10 units, ₹500. Schedule by month: 2026-10 10 units ₹500,
2026-11 10 units ₹500, 2027-01 6 units ₹300.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from finresearch.portfolio import elss
from finresearch.portfolio.lots import Event, build_lots

TODAY = date(2026, 10, 1)
ORIGIN = {"Origin": "http://127.0.0.1:3000", "X-FinResearch": "1"}
ELSS_NAME = "Example ELSS Tax Saver Fund - Direct Plan - Growth"
ELSS_CODE = "999901"
ELSS_CAT = "Equity Scheme - ELSS"

SCENARIO = [
    ("2022-05-10", "buy", "20", "10"),
    ("2023-08-10", "buy", "10", "20"),
    ("2023-09-10", "buy", "10", "20"),
    ("2023-10-10", "buy", "10", "20"),
    ("2023-11-10", "buy", "10", "20"),
    ("2024-01-15", "bonus", None, None),
    ("2025-06-15", "sell", "15", "40"),
]


def events() -> list[Event]:
    out = []
    for i, (d, kind, q, p) in enumerate(SCENARIO):
        meta = {"a": 1, "b": 10} if kind == "bonus" else {}
        out.append(
            Event(i + 1, date.fromisoformat(d), kind, D(q) if q else None, D(p) if p else None, meta=meta)
        )
    return out


# --------------------------------------------------------------------------- pure
def test_lockin_of_sip_lots_after_a_redemption_and_a_bonus():
    book = build_lots(events())
    assert [(lot.acquired.isoformat(), lot.origin, lot.open_quantity) for lot in book.open_lots] == [
        ("2022-05-10", "buy", D(5)), ("2023-08-10", "buy", D(10)), ("2023-09-10", "buy", D(10)),
        ("2023-10-10", "buy", D(10)), ("2023-11-10", "buy", D(10)), ("2024-01-15", "bonus", D(6)),
    ]  # fmt: skip
    det = elss.detect("mf", ELSS_NAME, ELSS_CAT)
    v = elss.lockin(book.lots, TODAY, D(50), det)
    assert (v["unlocked_units"], v["unlocked_value"], v["locked_units"], v["locked_value"]) == (
        25,
        1250,
        26,
        1300,
    )
    assert v["sellable_units"] == 25 and v["unknown_units"] == 0
    assert v["next_unlock"] == {"day": "2026-10-11", "days": 10, "units": 10, "value": 500}
    assert [(m["month"], m["first"], m["units"], m["value"], m["lots"]) for m in v["schedule"]] == [
        ("2026-10", "2026-10-11", 10, 500, 1), ("2026-11", "2026-11-11", 10, 500, 1), ("2027-01", "2027-01-16", 6, 300, 1),
    ]  # fmt: skip
    assert [x["status"] for x in v["lots"]] == [
        "unlocked",
        "unlocked",
        "unlocked",
        "locked",
        "locked",
        "locked",
    ]
    assert elss.BONUS_NOTE in v["notes"]  # the bonus lot is still locked, and its rule is unverified
    assert v["detected"] == {"verified": True, "why": "AMFI category Equity Scheme - ELSS"}
    # the same split as Decimals for the sale caps
    assert elss.split_units(book.lots, TODAY) == (D(25), D(26), D(0))
    # without a price the units are still known, the values are not
    nv = elss.lockin(book.lots, TODAY)
    assert nv["locked_units"] == 26 and nv["locked_value"] is None and nv["next_unlock"]["value"] is None


def test_unlock_day_is_the_day_after_the_third_anniversary():
    assert elss.unlock_date(date(2023, 10, 1)) == date(2026, 10, 2)
    lot = [elss_lot(date(2023, 10, 1), 7)]
    assert elss.split_units(lot, date(2026, 10, 1)) == (
        D(0),
        D(7),
        D(0),
    )  # the anniversary itself: still locked
    assert elss.split_units(lot, date(2026, 10, 2)) == (D(7), D(0), D(0))
    # 29-Feb: the third anniversary falls back to 28-Feb-2027, so the units are free from 1-Mar-2027
    assert elss.unlock_date(date(2024, 2, 29)) == date(2027, 3, 1)
    # a 31st: 31-Mar-2023 -> 31-Mar-2026 -> free 1-Apr-2026
    assert elss.unlock_date(date(2023, 3, 31)) == date(2026, 4, 1)


def test_undated_opening_balance_is_neither_locked_nor_sellable():
    lots = [elss_lot(None, 12, "opening"), elss_lot(date(2026, 1, 5), 3)]
    v = elss.lockin(lots, TODAY, D(10))
    assert (v["unknown_units"], v["unknown_value"], v["sellable_units"], v["locked_units"]) == (12, 120, 0, 3)
    assert elss.UNKNOWN_NOTE in v["notes"]


@pytest.mark.parametrize(
    ("asset", "name", "category", "verified"),
    [
        ("mf", ELSS_NAME, "Equity Scheme - ELSS", True),
        (
            "mf",
            "Example ELSS- Tax Saver Fund",
            "Equity Schemes - ELSS- Tax Saver Fund",
            True,
        ),  # AMFI's 2nd spelling
        ("mf", "Example Tax Plan Series 3", "ELSS", True),  # AMFI's close-ended heading
        ("mf", "Example Long Term Advantage Plan", "Equity Scheme - ELSS", True),  # no 'ELSS' in the name
        (
            "mf",
            "Example ELSS Tax Saver Nifty 50 Index Fund",
            "Index Funds - Equity Funds",
            False,
        ),  # passive ELSS
        ("mf", "Example Tax Saver Fund", None, False),  # no category known: name only
        ("mf", "Example Tax Saver Fund", "Equity Scheme - Flexi Cap Fund", None),  # AMFI says it is not ELSS
        ("mf", "Example Flexi Cap Fund", "Equity Scheme - Flexi Cap Fund", None),
        ("stock", "Example ELSS Ltd", None, None),
    ],
)
def test_detection_prefers_amfi_category(asset, name, category, verified):
    got = elss.detect(asset, name, category)
    assert (got.verified if got else None) == verified
    if got is not None and not got.verified:
        assert "[unverified]" in got.why


def elss_lot(acquired, units, origin="buy"):
    from types import SimpleNamespace

    return SimpleNamespace(acquired=acquired, open_quantity=D(units), origin=origin)


# --------------------------------------------------------------------------- through the API
@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from finresearch.adapters.amfi import SchemeNav
    from finresearch.api import create_app
    from finresearch.api.markets import MarketSources
    from finresearch.db import session_scope

    async def nav_all():
        return [SchemeNav(ELSS_CODE, ELSS_NAME, "Direct", "Growth", None, None, D("50"), date(2026, 9, 30), ELSS_CAT,
                          "Example MF"),
                SchemeNav("999902", "Example Long Term Advantage Fund - Direct Plan - Growth", "Direct", "Growth",
                          None, None, D("50"), date(2026, 9, 30), ELSS_CAT, "Example MF")]  # fmt: skip

    async def quote(symbol):
        raise LookupError(symbol)

    async def no_signal(asset, code):
        raise LookupError("offline test")

    with session_scope() as s:
        s.execute(text("TRUNCATE trade_note, alert, portfolio_disposal, portfolio_lot, portfolio_txn, "
                       "portfolio_holding, portfolio_import, portfolio_snapshot, portfolio_setting, investor_profile "
                       "CASCADE"))  # fmt: skip
    app = create_app(nav_all=nav_all)
    app.state.markets = MarketSources(quote=quote, today=lambda: TODAY)
    app.state.journal_today = TODAY
    app.state.journal_signal = no_signal
    with TestClient(app) as c:
        yield c


def add(c, **kw) -> dict:
    r = c.post(
        "/api/portfolio/transactions", headers=ORIGIN, json={"asset_type": "mf", "account": "Manual", **kw}
    )
    assert r.status_code == 201, r.text
    return r.json()


def scenario(c, name=ELSS_NAME, code=ELSS_CODE) -> int:
    hid = None
    for d, kind, q, p in SCENARIO:
        body = {"day": d, "kind": kind, **({"quantity": q, "price": p} if q else {}),
                **({"meta": {"a": 1, "b": 10}} if kind == "bonus" else {})}  # fmt: skip
        hid = add(c, **({"holding_id": hid} if hid else {"name": name, "scheme_code": code}), **body)[
            "holding_id"
        ]
    return hid


def write_valuation(hid: int, category: str | None, price: float = 50.0) -> None:
    from finresearch.db import session_scope
    from finresearch.portfolio import cache

    with session_scope() as s:
        cache.write(s, cache.VALUATION, {"day": TODAY.isoformat(), "value": 2550.0, "holdings": {str(hid): {
            "name": "x", "asset_type": "mf", "units": 51.0, "price": price, "price_source": "AMFI NAVAll",
            "price_as_of": "2026-09-30", "value": 2550.0, "category": category, "scheme_code": None}}})  # fmt: skip


def pretrade(c, hid: int, qty: str) -> dict:
    out = c.post("/api/journal/pretrade", headers=ORIGIN,
                 json={"side": "sell", "holding_id": hid, "quantity": qty, "price": "50"}).json()  # fmt: skip
    return {i["key"]: i for i in out["items"]} | {"_status": out["status"]}


def test_portfolio_row_and_holding_detail_carry_the_lockin(client):
    hid = scenario(client)
    (h,) = client.get("/api/portfolio").json()["holdings"]
    e = h["elss"]
    assert h["units"] == 51 and e["detected"]["verified"] is True  # category from the AMFI NAVAll row
    assert (e["unlocked_units"], e["unlocked_value"], e["locked_units"], e["locked_value"]) == (
        25,
        1250,
        26,
        1300,
    )
    assert e["next_unlock"] == {"day": "2026-10-11", "days": 10, "units": 10, "value": 500}
    assert [m["month"] for m in e["schedule"]] == ["2026-10", "2026-11", "2027-01"]
    d = client.get(f"/api/portfolio/holdings/{hid}").json()["elss"]  # the NAVAll rows are cached by now
    assert d["locked_units"] == 26 and d["locked_value"] == 1300 and d["detected"]["verified"] is True


def test_pretrade_caps_the_sale_at_the_unlocked_units(client):
    hid = scenario(client)
    ok = pretrade(client, hid, "25")["elss_lock"]
    assert ok["status"] == "ok" and ok["value"] == 0 and ok["sellable_units"] == 25
    assert "redeem up to 25 unit(s) today" in ok["detail"] and "2026-10-11" in ok["detail"]
    over = pretrade(client, hid, "26")
    b = over["elss_lock"]
    # FIFO: 25 unlocked units first, the 26th comes from the Oct-2023 instalment (locked until 11-Oct-2026)
    assert b["status"] == "block" and b["value"] == 1 and (b["sellable_units"], b["locked_units"]) == (25, 26)
    assert "at most 25 unit(s)" in b["detail"] and over["_status"] == "block"
    assert b["next_unlock"]["day"] == "2026-10-11" and b["verified"] is False  # no daily valuation: name only
    write_valuation(hid, ELSS_CAT)
    assert pretrade(client, hid, "26")["elss_lock"]["verified"] is True  # AMFI category from the daily pass


def test_category_detects_an_elss_whose_name_does_not_say_so(client):
    """Regression: the old name-only check missed an ELSS named without 'ELSS'/'tax saver' (AMFI files e.g. 'HDFC
    Long Term Advantage Plan' under Equity Scheme - ELSS) and allowed selling its locked units."""
    hid = scenario(client, "Example Long Term Advantage Fund - Direct Plan - Growth", "999902")
    assert "elss_lock" not in pretrade(client, hid, "26")  # no category known yet, and the name says nothing
    write_valuation(hid, ELSS_CAT)
    assert pretrade(client, hid, "26")["elss_lock"]["status"] == "block"
    (h,) = client.get("/api/portfolio").json()["holdings"]
    assert h["elss"]["locked_units"] == 26


def test_harvest_never_suggests_locked_elss_units(client):
    """Regression: a lot held 12-36 months is long-term for tax but still locked; gain harvesting suggested it."""
    hid = add(client, name=ELSS_NAME, scheme_code=ELSS_CODE, day="2023-08-10", kind="buy", quantity="10",
              price="20")["holding_id"]  # fmt: skip
    add(client, holding_id=hid, day="2024-06-10", kind="buy", quantity="10", price="20")
    tax = client.get("/api/portfolio/tax").json()
    # 10-Aug-2023 lot: free from 11-Aug-2026, gain (50 - 20) x 10 = ₹300. The 10-Jun-2024 lot is long-term (over 12
    # months) and in profit but locked until 11-Jun-2027: before the fix the suggestion was 20 units / ₹600
    (g,) = tax["harvest"]["gain_harvest"]
    assert (g["sell_units"], g["gain"]) == (10.0, 300.0)


def test_alert_metric_brief_and_calendar(client, monkeypatch):
    from finresearch.alerts.registry import metrics_for
    from finresearch.api.brief import calendar_view
    from finresearch.db import session_scope
    from finresearch.fincalc import dates
    from finresearch.monitor.digest import brief_text, build_brief, build_digest, digest_text
    from finresearch.portfolio.metrics import alert_metrics

    hid = scenario(client)
    write_valuation(hid, ELSS_CAT)
    monkeypatch.setattr(dates, "today_ist", lambda: TODAY)
    with session_scope() as s:
        m = alert_metrics(s)
        cal = calendar_view(s, datetime(2026, 10, 1, 3, 5, tzinfo=UTC))
        b1 = build_brief(s, datetime(2026, 10, 1, 3, 5, tzinfo=UTC))
        b5 = build_brief(s, datetime(2026, 10, 5, 3, 5, tzinfo=UTC))
        dg = build_digest(s, datetime(2026, 10, 4, 3, 35, tzinfo=UTC))
    val, source, detail = m["days_to_elss_unlock"]
    assert (
        val == 10
        and detail == f"{ELSS_NAME}: 10 units (₹500) unlock on 2026-10-11"
        and "portfolio.elss" in source
    )
    assert "days_to_elss_unlock" in {x.key for x in metrics_for("portfolio")}
    # 45-day calendar from 1-Oct: 11-Oct (10 units) and 11-Nov (41 days); the bonus lot (16-Jan-2027) is beyond it
    un = [(x["day"], x["units"], x["value"]) for x in cal["elss_unlocks"]]
    assert un == [("2026-10-11", 10, 500), ("2026-11-11", 10, 500)]
    assert [e["day"] for e in cal["items"] if e["kind"] == "elss_unlock"] == ["2026-10-11", "2026-11-11"]
    # the brief lists this month's unlocks; the dated event appears once it is within the week (5-Oct: 6 days)
    assert [x["day"] for x in b1["elss_unlocks"]] == ["2026-10-11"]
    assert not any(e["kind"] == "elss_unlock" for e in b1["events"])
    (ev,) = [e for e in b5["events"] if e["kind"] == "elss_unlock"]
    assert (
        ev["title"]
        == f"{ELSS_NAME}: 10 ELSS units (about ₹500) finish the 3-year lock-in and can be redeemed"
    )
    assert "ELSS units" in brief_text(b5)
    assert [x["day"] for x in dg["elss_unlocks"]] == ["2026-10-11"] and "ELSS unlock 11 Oct" in digest_text(
        dg
    )
    # once every lot is free, the metric says so instead of a number
    monkeypatch.setattr(dates, "today_ist", lambda: date(2027, 1, 16))
    with session_scope() as s:
        assert alert_metrics(s)["days_to_elss_unlock"][0] is None
