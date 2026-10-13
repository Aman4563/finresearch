"""#316: the SGB redemption exemption after 1-Apr-2026 (original subscriber, maturity or premature RBI redemption,
never assumed), the listed treatment of G-secs/SDLs/T-bills, and Groww's basket margin calculator as the one
read-only POST. Synthetic instruments only (made-up names, fake ISINs). Expected values are worked out by hand."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal as D

from finresearch.fincalc.tax import classify
from finresearch.portfolio.tax import auto_tax_class, holding_subscriber, lot_subscription


# --------------------------------------------------------------------------- 1. SGB: who subscribed
def test_lot_subscription_from_the_acquiring_transaction():
    assert lot_subscription("buy", {"sgb_subscription": True}) is True  # entered as bought at issue
    assert lot_subscription("buy", {"sgb_subscription": False}) is False
    # a broker tradebook/API trade: bought on the exchange (secondary market)
    assert lot_subscription("buy", {"exchange": "NSE", "trade_id": "T1"}) is False
    assert lot_subscription("buy", {"order_id": "O1"}) is False
    # a holdings-sync opening carries the listing exchange, which proves nothing; a bare manual buy neither
    assert lot_subscription("opening", {"exchange": "NSE", "statement_opening": True}) is None
    assert lot_subscription("buy", {}) is None and lot_subscription("buy", None) is None


def test_holding_setting_is_three_way_and_the_old_unticked_checkbox_is_not_an_answer():
    assert holding_subscriber({"sgb_subscribed": True}) is True
    assert holding_subscriber({"sgb_subscribed": False}) is False
    assert holding_subscriber({}) is None and holding_subscriber(None) is None
    assert holding_subscriber({"sgb_original_subscriber": True}) is True
    assert holding_subscriber({"sgb_original_subscriber": False}) is None  # saved by every unticked save


def test_sgb_paths_after_apr_2026():
    sub, maturity, early = date(2018, 11, 20), date(2026, 11, 20), date(2026, 5, 20)
    for sold in (maturity, early):  # maturity and premature RBI redemption (after year 5) alike
        assert classify("sgb", sub, sold, rbi_redemption=True, original_subscriber=True).term == "exempt"
        sec = classify("sgb", sub, sold, rbi_redemption=True, original_subscriber=False)
        assert (sec.term, sec.rate, sec.rule.id) == ("long", D("0.125"), "other-listed-2024")
        assert classify("sgb", sub, sold, rbi_redemption=True).term == "unknown"
    # an exchange sale is taxable even for the original subscriber; 12-month listed rule
    assert classify("sgb", sub, early, original_subscriber=True).term == "long"
    short = classify("sgb", date(2025, 6, 1), date(2026, 6, 1), original_subscriber=True)  # exactly 12 months
    assert (short.term, short.rate) == ("short", None)


# the portfolio API fixture (TODAY there is 2026-09-30); tests taking it carry a noqa for F811
from test_portfolio import ORIGIN, client  # noqa: E402,F401

SGB = {"asset_type": "stock", "name": "SGB 2018-19 SERIES XX", "nse_symbol": "SGBXX26", "account": "Manual",
       "day": "2018-05-14", "kind": "buy", "quantity": "10", "price": "3100", "stt_paid": False}  # fmt: skip


def _redeem(c, hid, meta=None):
    # premature RBI redemption after the fifth year, 14-May-2026 (tax year 2026-27): 10 @ 9,800
    r = c.post("/api/portfolio/transactions", headers=ORIGIN,
               json={"holding_id": hid, "day": "2026-05-14", "kind": "redemption", "quantity": "10",
                     "price": "9800", "stt_paid": False, "meta": meta or {}})  # fmt: skip
    assert r.status_code < 300, r.text


def _tax(c):
    tax = c.get("/api/portfolio/tax").json()
    (d,) = tax["disposals"]
    return d, next(x for x in tax["fys"] if x["fy"] == 2027)


def _set(c, hid, v):
    r = c.put(
        f"/api/portfolio/holdings/{hid}", headers=ORIGIN, json={"flags": {"sgb_original_subscriber": v}}
    )
    assert r.status_code == 200, r.text


def test_sgb_premature_redemption_through_the_api(client):  # noqa: F811
    hid = client.post("/api/portfolio/transactions", headers=ORIGIN, json=SGB).json()["holding_id"]
    _redeem(client, hid, {"held_to_maturity": False})
    h = next(x for x in client.get("/api/portfolio").json()["holdings"] if x["id"] == hid)
    assert h["sgb_original_subscriber"] is None
    d, fy = _tax(client)  # not set: unknown, the year incomplete (never exempt, never a guessed figure)
    assert d["term"] == "unknown" and "subscribed at issue" in d["reason"] and fy["complete"] is False
    _set(client, hid, True)  # subscribed at issue: exempt; 10 x (9,800 - 3,100) = 67,000 exempt
    d, fy = _tax(client)
    assert (d["term"], d["rule"], fy["exempt"], fy["total"]) == (
        "exempt",
        "sgb-redemption-2026",
        67000.0,
        0.0,
    )
    _set(client, hid, False)  # bought on the exchange: LTCG 67,000 x 12.5 % = 8,375 + 4 % cess 335 = 8,710
    d, fy = _tax(client)
    assert (d["term"], d["rate_pct"], fy["total"]) == ("long", 12.5, 8710.0)
    _set(client, hid, None)  # cleared: unknown again
    assert _tax(client)[0]["term"] == "unknown"


def test_sgb_exchange_bought_lot_overrides_the_holding_setting(client):  # noqa: F811
    hid = client.post("/api/portfolio/transactions", headers=ORIGIN,
                      json={**SGB, "meta": {"sgb_subscription": False}}).json()["holding_id"]  # fmt: skip
    _set(client, hid, True)
    _redeem(client, hid)
    d, fy = _tax(client)
    assert (d["term"], fy["total"]) == ("long", 8710.0)


# --------------------------------------------------------------------------- 2. G-secs, SDLs, T-bills
def test_government_securities_are_listed_with_the_12_month_rule():
    # s.2(42A) first proviso (Finance (No.2) Act 2024): listed security, long-term after more than 12 months;
    # s.112 12.5 % for transfers from 23-Jul-2024, no s.112A exemption
    for isin, name, sym in (("IN0020230085", "7.18% GOI 2033", "718GS2033"),
                            ("IN1920240012", "7.25% Example SDL 2034", None),
                            ("IN002025Z123", "364 DAY TBILL 14-MAY-2026", None)):  # fmt: skip
        cls, why = auto_tax_class("stock", name, None, None, sym, isin)
        assert cls == "other", why
        exact = classify(cls, date(2025, 3, 10), date(2026, 3, 10), listed=True, debt=True)
        assert (exact.term, exact.rate) == ("short", None)  # held exactly 12 months: short at slab
        later = classify(cls, date(2025, 3, 10), date(2026, 3, 11), listed=True, debt=True)
        assert (later.term, later.rate, later.rule.id) == ("long", D("0.125"), "other-listed-2024")
    assert re.search(
        r"s\.2\(42A\)", auto_tax_class("stock", "7.18% GOI 2033", None, None, None, "IN0020230085")[1]
    )
