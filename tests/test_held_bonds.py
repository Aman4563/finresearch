"""Held bonds and SGBs (#294): redemption and interest transactions, maturity valuation, Government-security tax
class, s.50AA for unlisted debentures, coupon/maturity alerts, AIS interest and the IPO allotment fill. Synthetic
instruments only (made-up names, fake ISINs). Expected values are worked out by hand in the comments."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from finresearch.fincalc.tax import classify
from finresearch.portfolio import bonds
from finresearch.portfolio.lots import Event, build_lots
from finresearch.portfolio.tax import auto_tax_class, is_debt_security, redemption_flags, sovereign_kind


# --------------------------------------------------------------------------- lots
def test_redemption_closes_lots_fifo_and_interest_moves_no_units():
    # 10 bought @1,050 on 2023-06-15, 5 more @990 on 2024-01-10; coupon ₹900 on 2024-06-15 (income only);
    # redemption of all 15 @1,000 on 2025-06-15: FIFO gives (10, cost 10,500, proceeds 10,000) then (5, 4,950, 5,000)
    book = build_lots([Event(1, date(2023, 6, 15), "buy", D(10), D(1050)),
                       Event(2, date(2024, 1, 10), "buy", D(5), D(990)),
                       Event(3, date(2024, 6, 15), "interest", amount=D(900)),
                       Event(4, date(2025, 6, 15), "redemption", D(15), D(1000))])  # fmt: skip
    assert book.units == 0 and book.interest == [(date(2024, 6, 15), D(900))] and book.dividends == []
    assert [(d.quantity, d.cost, d.proceeds, d.intraday) for d in book.disposals] == [
        (D(10), D(10500), D(10000), False), (D(5), D(4950), D(5000), False)]  # fmt: skip
    assert book.warnings == []


def test_redemption_on_a_buy_day_is_never_intraday():
    # a T-bill bought and another lot redeemed the same day: the redemption is not a same-day trade
    book = build_lots([Event(1, date(2025, 1, 2), "buy", D(100), D(98)),
                       Event(2, date(2025, 4, 3), "buy", D(100), D(99)),
                       Event(3, date(2025, 4, 3), "redemption", D(100), D(100))])  # fmt: skip
    (d,) = book.disposals
    assert (d.intraday, d.acquired, d.cost, d.proceeds) == (False, date(2025, 1, 2), D(9800), D(10000))


# --------------------------------------------------------------------------- tax class
@pytest.mark.parametrize("isin,name,symbol,kind", [
    ("IN0020230085", "7.18% GOI 2033", "718GS2033", "gsec"),
    ("IN002025Z123", "364 DAY TBILL 14-MAY-2026", None, "tbill"),
    ("IN002026X099", "Example 91 day bill", None, "tbill"),
    ("IN1520230123", "7.20% Example State 2033", None, "sdl"),
    (None, "7.20% MH SDL 2033", None, "sdl"),
    ("IN0020170101", "SGB 2017-18 SERIES X", "SGBNOV25", "sgb"),
    ("INE000A01011", "Example Textiles Ltd", "EXMPL", None),
    ("INE027E07998", "Example Finance NCD", None, None),  # a corporate NCD: debt, not sovereign
])  # fmt: skip
def test_sovereign_kind(isin, name, symbol, kind):
    assert sovereign_kind(isin, name, symbol) == kind


def test_government_securities_are_other_listed_never_equity():
    # before #294 an IN00 ISIN with a plain name ("7.18% GOI 2033") fell through to "equity"
    for isin, name in (("IN0020230085", "7.18% GOI 2033"), ("IN002025Z123", "364 DAY TBILL"),
                       ("IN1520230123", "Example State 2033")):  # fmt: skip
        cls, why = auto_tax_class("stock", name, None, None, None, isin)
        assert cls == "other" and ("s.112A" in why or "slab" in why), (name, cls, why)
        assert is_debt_security(isin, name)
    assert auto_tax_class("stock", "SGB 2017-18 SERIES X", None, None, "SGBNOV25", "IN0020170101")[0] == "sgb"
    assert not is_debt_security("IN0020170101", "SGB 2017-18 SERIES X", "SGBNOV25")
    assert auto_tax_class("stock", "Example Gilt ETF", None, None, "GILTBEES", "INF000000001")[0] == "debt_mf"


def test_s50aa_unlisted_debenture_from_23_jul_2024():
    bought = date(2021, 1, 10)
    # before 23-Jul-2024: an unlisted asset held over 36 months is long-term (other-unlisted-pre2024)
    assert classify("other", bought, date(2024, 7, 22), listed=False, debt=True).term == "long"
    # on and after 23-Jul-2024: s.50AA deems it short-term at the slab rate whatever the holding period
    c = classify("other", bought, date(2024, 7, 23), listed=False, debt=True)
    assert (c.term, c.rate, c.bucket, c.rule.id) == ("short", None, "slab_st", "unlisted-debt-50aa")
    # no acquisition date needed: the term does not depend on it
    assert classify("other", None, date(2025, 1, 10), listed=False, debt=True).term == "short"
    # a listed NCD and an unlisted non-debt asset are untouched
    assert classify("other", bought, date(2025, 1, 10), listed=True, debt=True).term == "long"
    assert classify("other", bought, date(2025, 1, 10), listed=False, debt=False).term == "long"


def test_redemption_flags():
    assert redemption_flags("redemption", {}, "sgb") == (True, False)  # maturity never assumed
    assert redemption_flags("redemption", {"held_to_maturity": True}, "sgb") == (True, True)
    assert redemption_flags("redemption", {}, "other") == (False, False)  # an NCD's redemption is a transfer
    assert redemption_flags("sell", {}, "sgb") == (False, False)  # an exchange sale of an SGB is taxable
    assert redemption_flags("sell", {"rbi_redemption": True}, "sgb") == (True, False)


def test_sgb_redemption_after_apr_2026_needs_original_subscriber_held_to_maturity():
    bought, sold = date(2018, 11, 20), date(2026, 11, 20)
    assert classify("sgb", bought, sold, rbi_redemption=True, original_subscriber=True,
                    held_to_maturity=True).term == "exempt"  # fmt: skip
    # premature (not held to maturity) or a secondary-market buyer: a listed security held > 12 months
    c = classify("sgb", bought, sold, rbi_redemption=True, original_subscriber=True, held_to_maturity=False)
    assert (c.term, c.rate) == ("long", D("0.125"))
    assert classify("sgb", bought, sold, rbi_redemption=True, held_to_maturity=True).term == "long"
    # before 1-Apr-2026 any RBI redemption (early ones too) is exempt under s.47(viic)
    assert classify("sgb", bought, date(2025, 11, 20), rbi_redemption=True).term == "exempt"


# --------------------------------------------------------------------------- terms, coupons, windows
def test_sgb_schedule_from_the_issue_date():
    # issued 12-Feb-2018: matures 12-Feb-2026 (8 years); interest half-yearly on 12-Feb / 12-Aug; early redemption on
    # coupon dates from the fifth anniversary (12-Feb-2023) up to before maturity
    t = bonds.terms_of("sgb", {"issue_date": "2018-02-12"})
    assert (t.maturity, t.freq, t.face_value) == (date(2026, 2, 12), 2, None)
    assert bonds.next_coupon(t, date(2022, 9, 1)) == (date(2023, 2, 12), None)
    assert bonds.next_coupon(t, date(2023, 2, 12)) == (date(2023, 2, 12), None)  # due today
    assert bonds.sgb_windows(t, date(2018, 3, 1)) == [date(2023, 2, 12), date(2023, 8, 12), date(2024, 2, 12),
                                                      date(2024, 8, 12)]  # fmt: skip
    assert bonds.sgb_windows(t, date(2025, 9, 1)) == []  # only the maturity is left
    assert bonds.redemption_value(t) == (None, None)  # RBI's gold-price average: unknown until entered
    assert bonds.redemption_value(bonds.terms_of("sgb", {"redemption_price": "9500"}))[0] == D(9500)


def test_unknown_terms_stay_unknown():
    bond = bonds.terms_of("bond", {}, "2027-06-30", "NSE's capital-market bond list")
    assert bond.maturity == date(2027, 6, 30) and bond.sources["maturity"] == "NSE's capital-market bond list"
    nc, why = bonds.next_coupon(bond, date(2026, 9, 30))
    assert nc is None and "frequency unknown" in why  # never a guessed schedule
    assert bonds.redemption_value(bond) == (None, None)  # no face value entered
    assert bonds.next_coupon(bonds.terms_of("bond", {}), date(2026, 9, 30))[1].startswith("maturity unknown")
    assert not bonds.matured(
        bonds.terms_of("bond", {}), date(2099, 1, 1)
    )  # unknown maturity is never matured
    tb = bonds.terms_of("tbill", {"maturity": "2026-05-14"})
    assert bonds.next_coupon(tb, date(2026, 1, 1))[0] is None and tb.face_value == D(100)
    g = bonds.terms_of("gsec", {"maturity": "2033-09-19", "coupon_pct": "7.18"})
    assert (g.freq, g.face_value) == (2, D(100)) and bonds.next_coupon(g, date(2026, 9, 30))[0] == date(
        2027, 3, 19
    )


def test_clean_terms_validates():
    assert bonds.clean_terms({"maturity": "2030-01-15", "freq": "2", "coupon_pct": "9.1", "face_value": ""}) == {
        "maturity": "2030-01-15", "freq": 2, "coupon_pct": "9.1"}  # fmt: skip
    for bad in ({"freq": 3}, {"maturity": "soon"}, {"face_value": "-5"}, {"colour": "red"},
                {"issue_date": "2030-01-01", "maturity": "2029-01-01"}):  # fmt: skip
        with pytest.raises(ValueError):
            bonds.clean_terms(bad)


# --------------------------------------------------------------------------- through the API
# the portfolio API fixture (TODAY there is 2026-09-30); tests taking it carry a noqa for F811
from test_portfolio import ORIGIN, client  # noqa: E402,F401

NCD = {"asset_type": "stock", "name": "Example Finance NCD 9% 2026", "nse_symbol": "EXMPL", "isin": "INE000Z07011",
       "account": "Manual", "day": "2024-03-01", "kind": "buy", "quantity": "10", "price": "1020"}  # fmt: skip


def _holding(c, hid):
    return next(x for x in c.get("/api/portfolio").json()["holdings"] if x["id"] == hid)


def test_matured_bond_is_never_valued_at_its_last_price(client):  # noqa: F811
    hid = client.post("/api/portfolio/transactions", headers=ORIGIN, json=NCD).json()["holding_id"]
    h = _holding(client, hid)
    # before any terms: a listed NCD priced from its NSE quote (250 in the fake), labelled dirty
    assert (h["value"], h["price_basis"], h["bond"]["kind"], h["bond"]["matured"]) == (
        2500.0,
        "dirty",
        "bond",
        False,
    )
    assert h["tax_class"] == "other"
    # matured on 30-Jun-2026 (before TODAY), face value unknown: the quote is ignored and the value is unknown
    r = client.put(
        f"/api/portfolio/holdings/{hid}", headers=ORIGIN, json={"bond_terms": {"maturity": "2026-06-30"}}
    )
    assert r.status_code == 200
    snap = client.get("/api/portfolio").json()
    h = next(x for x in snap["holdings"] if x["id"] == hid)
    assert h["value"] is None and h["price"] is None and h["bond"]["status"] == "matured — record redemption"
    assert "matured on 2026-06-30" in h["price_error"] and "redemption value is unknown" in h["price_error"]
    assert (
        snap["complete"] is False
        and snap["summary"]["matured_bonds"] == 1
        and snap["summary"]["unpriced"] == 1
    )
    # face value ₹1,000 entered: valued at 10 × 1,000 = 10,000 (the amount the issuer owes), still incomplete
    client.put(f"/api/portfolio/holdings/{hid}", headers=ORIGIN,
               json={"bond_terms": {"maturity": "2026-06-30", "face_value": "1000"}})  # fmt: skip
    snap = client.get("/api/portfolio").json()
    h = next(x for x in snap["holdings"] if x["id"] == hid)
    assert (h["value"], h["price"], h["price_stale"]) == (10000.0, 1000.0, False)
    assert h["price_source"].startswith("redemption value at maturity") and snap["complete"] is False
    assert client.put(f"/api/portfolio/holdings/{hid}", headers=ORIGIN,
                      json={"bond_terms": {"freq": 5}}).status_code == 422  # fmt: skip

    # the last coupon (interest: income, no units) and the redemption at face close the holding
    assert client.post("/api/portfolio/transactions", headers=ORIGIN,
                       json={"holding_id": hid, "day": "2026-06-30", "kind": "interest"}).status_code == 422  # fmt: skip
    client.post("/api/portfolio/transactions", headers=ORIGIN,
                json={"holding_id": hid, "day": "2026-06-30", "kind": "interest", "amount": "900"})  # fmt: skip
    assert _holding(client, hid)["units"] == 10.0
    r = client.post("/api/portfolio/transactions", headers=ORIGIN,
                    json={"holding_id": hid, "day": "2026-06-30", "kind": "redemption", "quantity": "10",
                          "price": "1000", "stt_paid": False})  # fmt: skip
    assert r.status_code == 201
    snap = client.get("/api/portfolio").json()
    h = next(x for x in snap["holdings"] if x["id"] == hid)
    # bought 10 @1,020 (10,200), redeemed @1,000 (10,000): a ₹200 loss; interest ₹900 kept apart from dividends
    assert (h["closed"], h["realised"], h["interest"], h["dividends"]) == (True, -200.0, 900.0, 0.0)
    assert snap["summary"]["matured_bonds"] == 0 and snap["summary"]["interest"] == 900.0
    tax = client.get("/api/portfolio/tax").json()
    (d,) = tax["disposals"]
    # held 2024-03-01 -> 2026-06-30 (> 12 months), a listed NCD: long-term capital loss under s.112 rules
    assert (d["term"], d["gain"], d["rate_pct"], d["rule"]) == ("long", -200.0, 12.5, "other-listed-2024")


def test_sgb_redemption_by_rbi_is_exempt(client):  # noqa: F811
    body = {"asset_type": "stock", "name": "SGB 2017-18 SERIES X", "nse_symbol": "SGBNOV25", "account": "Manual",
            "day": "2017-11-20", "kind": "buy", "quantity": "10", "price": "3000", "stt_paid": False}  # fmt: skip
    hid = client.post("/api/portfolio/transactions", headers=ORIGIN, json=body).json()["holding_id"]
    client.put(
        f"/api/portfolio/holdings/{hid}", headers=ORIGIN, json={"bond_terms": {"issue_date": "2017-11-20"}}
    )
    h = _holding(client, hid)
    # issue + 8 years = 20-Nov-2025, before TODAY: matured, and an SGB's redemption value is never its face
    assert h["bond"]["maturity"] == "2025-11-20" and h["bond"]["matured"] and h["value"] is None
    client.post("/api/portfolio/transactions", headers=ORIGIN,
                json={"holding_id": hid, "day": "2025-11-20", "kind": "redemption", "quantity": "10",
                      "price": "12000", "stt_paid": False, "meta": {"held_to_maturity": True}})  # fmt: skip
    tax = client.get("/api/portfolio/tax").json()
    (d,) = tax["disposals"]
    assert (d["term"], d["gain"], d["rule"]) == ("exempt", 0.0, "sgb-redemption")
    fy = next(x for x in tax["fys"] if x["fy"] == 2026)
    assert fy["exempt"] == 90000.0 and fy["total"] == 0.0  # 10 × (12,000 - 3,000), s.47(viic)
