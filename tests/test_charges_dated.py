"""Dated charges for 2022-24 (#382 (f), #365): the rate in force on the trade date, read from NSE's circulars, not the
latest rate used for every earlier date. Expected values worked out by hand from the circulars' rates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import charges as ch

SPREAD = [
    ch.TradeLeg("option", "buy", 150.0, 65),
    ch.TradeLeg("option", "sell", 60.0, 65),
]  # 9,750 buy, 3,900 sell


@pytest.mark.parametrize(
    ("on", "stt", "exch_opt", "exch_fut", "fut_stt"),
    [
        (
            date(2022, 6, 1),
            "0.0005",
            "0.00053",
            "0.00002",
            "0.0001",
        ),  # NSE FATAX/32385, FA/46730, FATAX/23500
        (date(2023, 3, 31), "0.0005", "0.00053", "0.00002", "0.0001"),
        (
            date(2023, 4, 1),
            "0.000625",
            "0.000505",
            "0.00002",
            "0.000125",
        ),  # Finance Act 2023; FA/56129 (+IPFT)
        (date(2024, 4, 1), "0.000625", "0.0005", "0.0000198", "0.000125"),  # FA/61137 (+IPFT)
        (
            date(2024, 10, 1),
            "0.001",
            "0.0003553",
            "0.0000183",
            "0.0002",
        ),  # Finance (No. 2) Act 2024; FA/64232
    ],
)
def test_rates_follow_the_trade_date(on, stt, exch_opt, exch_fut, fut_stt):
    r = ch.rates_as_of(on)
    assert r["stt_option_sell"].rate == Decimal(stt)
    assert r["exchange_option"].rate == Decimal(exch_opt)
    assert r["exchange_futures"].rate == Decimal(exch_fut)
    assert r["stt_futures_sell"].rate == Decimal(fut_stt)
    assert r["stt_option_exercise"].rate == Decimal("0.00125")  # on intrinsic value since 1-Sep-2019
    assert r["sebi_fee"].rate == Decimal("0.000001") and r["stamp_option_buy"].rate == Decimal("0.00003")
    assert r["gst"].rate == Decimal("0.18")
    for row in r.values():  # every row in force in 2022-24 has a real start date and a source, none borrowed
        assert row.effective_from <= on and "used for an earlier date" not in row.note and row.source


def test_a_2022_spread_costs_the_2022_rates():
    c = ch.order_costs(SPREAD, date(2022, 6, 1))
    # STT 3,900 x 0.05 % = 1.95; NSE 13,650 x 0.053 % = 7.2345; SEBI 13,650 x Rs 10/crore = 0.01365;
    # stamp 9,750 x 0.003 % = 0.2925; brokerage 40; GST 18 % x (40 + 7.2345 + 0.01365) = 8.504667
    assert c.lines["stt"] == pytest.approx(1.95)
    assert c.lines["exchange"] == pytest.approx(7.2345)
    assert c.lines["gst"] == pytest.approx(8.504667)
    assert c.total == pytest.approx(1.95 + 7.2345 + 0.01365 + 0.2925 + 40 + 8.504667)
    # mid-2023: STT 0.0625 % and NSE 0.05 % + IPFT Rs 50/crore
    c23 = ch.order_costs(SPREAD, date(2023, 6, 1))
    assert c23.lines["stt"] == pytest.approx(2.4375) and c23.lines["exchange"] == pytest.approx(6.89325)


def test_exercise_stt_and_the_verified_status():
    assert ch.exercise_stt(300 * 50, date(2022, 6, 30)) == pytest.approx(
        18.75
    )  # 0.125 % of the intrinsic value
    r = ch.rates_as_of(date(2023, 6, 1))
    assert {
        r[k].status for k in ("stt_option_sell", "stt_futures_sell", "exchange_option", "exchange_futures")
    } == {"verified"}
    assert "nseindia.com" in r["stt_option_sell"].source and "nseindia.com" in r["exchange_option"].source
