"""AIS import and the AIS check (portfolio.ais, portfolio.ais_recon). Synthetic fixtures only (fake PAN ABCDE1234F);
every expected value below is worked out by hand from the fixture, not copied from the code."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.portfolio.ais import classify, parse_ais_json, scrub
from finresearch.portfolio.ais_recon import AppEntry, reconcile
from finresearch.portfolio.importers import StatementError

FIX = Path(__file__).parent / "fixtures" / "ais" / "ais_synthetic.json"
SECRETS = ("ABCDE1234F", "abcde1234f", "Test Investor", "9876543210", "test.investor@example.com", "Example Street",
           "123456789012", "XXXXXX4321", "IN30012345678901", "1201090000123456", "Some Relative", "01/01/1990")  # fmt: skip
D = Decimal


def test_parse_reads_fy_and_categories_and_skips_salary():
    st = parse_ais_json(FIX.read_bytes())
    assert st.fy == 2026  # "2025-26" is named by its end year, like fincalc.dates.fiscal_year
    assert st.ignored == 1  # the TDS-192 salary row; Part A is never walked
    cats = sorted(i.category for i in st.items)
    assert cats == sorted(["dividend"] * 6 + ["interest"] * 3 + ["sale"] * 3 + ["purchase", "off_market"])
    sale = next(i for i in st.items if i.isin == "INE000X01011" and i.category == "sale")
    assert (sale.amount, sale.quantity, sale.stt, sale.day) == (
        D("12345.00"),
        D("10"),
        D("12.35"),
        date(2025, 11, 3),
    )
    div = next(i for i in st.items if i.part == "tds" and i.source == "SAMPLE INDUSTRIES LIMITED")
    assert (div.tan, div.tds, div.code) == ("MUMS54321C", D("1000.00"), "194")


def test_no_identifier_survives_parsing():
    st = parse_ais_json(FIX.read_bytes())
    dumped = json.dumps([i.to_json() for i in st.items])
    for s in SECRETS:
        assert s not in dumped, s


@pytest.mark.parametrize(("text", "want"), [
    ("EXAMPLE BANK LTD (ABCDE1234F)", "EXAMPLE BANK LTD"),
    ("Savings A/c XXXXXX4321 Example Bank", "Savings A/c Example Bank"),
    ("Paid to 9876543210 / a@b.com", "Paid to"),
    ("EXAMPLE LIMITED MUMA12345B INE000X01011", "EXAMPLE LIMITED MUMA12345B INE000X01011"),
    ("Exxon Ltd", "Exxon Ltd"),
])  # fmt: skip
def test_scrub(text, want):
    assert scrub(text) == want


@pytest.mark.parametrize(("code", "desc", "want"), [
    ("SFT-017", "Off market debit transactions", "off_market"),
    ("194K", "Income in respect of units of Mutual Fund", "dividend"),
    ("194", None, "dividend"),
    ("194A", None, "interest"),
    ("SFT-018", "Purchase of securities and units of mutual funds", "purchase"),
    ("192", "Salary received (Section 192)", None),
])  # fmt: skip
def test_classify_description_beats_code(code, desc, want):
    assert classify(code, desc) == want


def test_not_json_is_a_safe_error():
    with pytest.raises(StatementError) as e:
        parse_ais_json(b"\x00\x01ABCDE1234F encrypted blob")
    assert "ABCDE1234F" not in str(e.value)
    with pytest.raises(StatementError):
        parse_ais_json(b'{"hello": "world"}')


def _app() -> list[AppEntry]:
    ex, sm = ("Example Ltd", "INE000X01011"), ("Sample Industries Ltd", "INE000X01029")
    gamma, alpha = "Gamma Bluechip Fund - Direct Growth", "Alpha Flexi Cap Fund - Direct IDCW"
    return [
        AppEntry(1, *ex, "stock", "dividend", date(2025, 7, 10), D("1500")),
        AppEntry(1, *ex, "stock", "dividend", date(2025, 3, 10), D("700")),  # FY 2024-25: not in this check
        AppEntry(1, *ex, "stock", "sale", date(2025, 11, 3), D("12345.00"), D("10"), D("25")),
        AppEntry(2, *sm, "stock", "dividend", date(2025, 8, 20), D("9000")),  # recorded net of 10 % TDS
        AppEntry(2, *sm, "stock", "sale", date(2026, 1, 12), D("10000"), D("5")),
        AppEntry(3, gamma, None, "mf", "sale", date(2026, 2, 20), D("5000"), D("100")),
        AppEntry(3, gamma, None, "mf", "purchase", date(2025, 6, 5), D("20000"), D("400")),
        AppEntry(4, alpha, None, "mf", "dividend", date(2025, 12, 15), D("2500")),
        AppEntry(4, alpha, None, "mf", "dividend", date(2025, 12, 15), D("3500")),
        AppEntry(5, "Beta Ltd", None, "stock", "dividend", date(2025, 6, 1), D("250")),
    ]  # fmt: skip


def test_reconcile_hand_computed():
    st = parse_ais_json(FIX.read_bytes())
    out = reconcile(st.items, _app(), 2026)
    rows = {(r["category"], r["label"].upper()): r for r in out["rows"]}
    assert out["counts"] == {"mismatch": 2, "only_ais": 1, "only_app": 1, "matched": 5}
    # Example Ltd dividend: TDS-194 and SFT-015 report the same ₹1,500 -> counted once, equals the app
    r = rows[("dividend", "EXAMPLE LIMITED")]
    assert (r["status"], r["ais_amount"], r["app_amount"], r["duplicates_dropped"]) == (
        "matched",
        1500,
        1500,
        1,
    )
    # Sample: AIS gross ₹10,000 (TDS ₹1,000), app ₹9,000 = 90 % -> the net-of-TDS cause
    r = rows[("dividend", "SAMPLE INDUSTRIES LIMITED")]
    assert (r["status"], r["diff"], r["ais_tds"]) == ("mismatch", 1000, 1000)
    assert "net of 10 % TDS" in r["cause"]
    assert rows[("dividend", "DEMO POWER LIMITED")]["status"] == "only_ais"
    # the MF dividend is paid by the AMC: matched on the brand, 2,500 + 3,500 = 6,000
    r = rows[("dividend", "ALPHA MUTUAL FUND")]
    assert (r["status"], r["match"], r["app_amount"]) == ("matched", "amc", 6000)
    assert rows[("dividend", "BETA LTD")]["status"] == "only_app"
    # sales: Example by ISIN, gross 10 x 1,234.50 = 12,345 (charges ignored); Sample 10,500 vs 10,000, same quantity
    assert rows[("sale", "EXAMPLE LIMITED")]["match"] == "isin"
    r = rows[("sale", "SAMPLE INDUSTRIES LIMITED")]
    assert (r["status"], r["diff"], r["ais_quantity"], r["app_quantity"]) == ("mismatch", 500, 5, 5)
    assert r["cause"].startswith("Same securities, different value")
    # Gamma: 4,999.95 vs 5,000 is ₹0.05 apart, inside max(₹1, 0.1 %)
    r = rows[("sale", "GAMMA BLUECHIP FUND - DIRECT PLAN - GROWTH")]
    assert (r["status"], r["match"]) == ("matched", "name")
    assert rows[("purchase", "GAMMA BLUECHIP FUND DIRECT GROWTH")]["status"] == "matched"
    # interest and off-market: listed, not compared; savings interest from TDS-194A and SFT-016 counted once
    info = {(i["category"], i["label"]): i for i in out["info"]}
    bank = info[("interest", "EXAMPLE BANK LTD")]
    assert (bank["ais_amount"], bank["duplicates_dropped"], bank["ais_tds"]) == (3200, 1, 320)
    assert info[("off_market", "EXAMPLE LIMITED")]["ais_amount"] == 6000
    assert out["totals"]["dividend"] == {"ais": 1500 + 10000 + 800 + 6000, "app": 1500 + 9000 + 6000 + 250}
    assert out["rows"][0]["status"] == "mismatch"


def test_charges_sized_difference_is_explained():
    from finresearch.portfolio.ais import AisItem

    ais = [AisItem("sale", "sft", D("50000"), security="Example Ltd", quantity=D("10"))]
    app = [AppEntry(1, "Example Ltd", None, "stock", "sale", date(2025, 5, 2), D("49940"), D("10"), D("60"))]
    r = reconcile(ais, app, 2026)["rows"][0]
    assert r["status"] == "mismatch" and "charges" in r["cause"]
