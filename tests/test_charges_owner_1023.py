"""The owner's vault-charge decisions of 23-Oct-2026 (#471): stamp duty before 1-Jul-2020 at Maharashtra's 0.002 % on the
buy side, and NSE's options transaction charge before 1-Apr-2017 at Rs 50 per lakh of premium, both labelled
secondary; option-sale STT Jan-May 2008 stays unknown. Expected values worked out by hand from the rates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import charges as ch

SPREAD = [
    ch.TradeLeg("option", "buy", 150.0, 65),
    ch.TradeLeg("option", "sell", 60.0, 65),
]  # 9,750 buy, 3,900 sell: 13,650 premium turnover


def test_a_2015_option_spread_is_priced_with_the_new_rows():
    c = ch.order_costs(SPREAD, date(2015, 8, 3))
    # STT 3,900 x 0.017 % = 0.663 (Finance Act 2008); NSE 13,650 x Rs 50/lakh = 6.825 (PTI, owner #471);
    # SEBI 13,650 x Rs 20/crore = 0.0273 (2014 gazette); stamp 9,750 x 0.002 % = 0.195 (Maharashtra, owner #471);
    # brokerage 2 x 20 = 40; service tax 14 % x (40 + 6.825 + 0.0273) = 6.559322
    assert c.lines == pytest.approx({"stt": 0.663, "exchange": 6.825, "sebi": 0.0273, "stamp": 0.195,
                                     "brokerage": 40.0, "gst": 6.559322})  # fmt: skip
    assert c.total == pytest.approx(54.269622)


def test_a_2019_option_spread_is_priced_with_the_maharashtra_stamp_duty():
    c = ch.order_costs(SPREAD, date(2019, 6, 3))
    # STT 3,900 x 0.05 % = 1.95; NSE 13,650 x 0.05 % = 6.825 (NSE/FA/34601, unchanged); SEBI 13,650 x Rs 10/crore
    # = 0.01365; stamp 9,750 x 0.002 % = 0.195; brokerage 40; GST 18 % x (40 + 6.825 + 0.01365) = 8.430957
    assert c.lines == pytest.approx({"stt": 1.95, "exchange": 6.825, "sebi": 0.01365, "stamp": 0.195,
                                     "brokerage": 40.0, "gst": 8.430957})  # fmt: skip
    assert c.total == pytest.approx(57.414607)
    fut = ch.order_costs([ch.TradeLeg("future", "buy", 11800.0, 75)], date(2019, 6, 3))
    assert fut.lines["stamp"] == pytest.approx(17.70)  # 8,85,000 x 0.002 %


def test_the_new_rows_are_labelled_secondary_with_their_sources():
    r = ch.rates_as_of(date(2015, 8, 3))
    for k in ("stamp_option_buy", "stamp_futures_buy"):
        assert r[k].rate == Decimal("0.00002") and r[k].status == "secondary"
        assert r[k].source == ch.MH_STAMP_2005 and r[k].effective_from == date(2005, 5, 7)
        assert "Maharashtra" in r[k].note and "owner" in r[k].note and "#471" in r[k].note
    x = r["exchange_option"]
    assert x.rate == Decimal("0.0005") and x.status == "secondary" and x.source == ch.PTI_2017
    assert x.note.startswith("secondary (PTI)") and "#471" in x.note
    # the rows from 1-Jul-2020 and 1-Apr-2017 are unchanged
    assert ch.rates_as_of(date(2020, 7, 1))["stamp_option_buy"].rate == Decimal("0.00003")
    assert ch.rates_as_of(date(2017, 4, 1))["exchange_option"].source == ch.NSE_TX_2017


def test_unknown_stays_unknown():
    # option-sale STT Jan-May 2008 (strike + premium basis), exercise STT before Jun-2008 and service tax on NSE's
    # charges before 16-May-2008 are still unknown, so a Jan-2008 option entry is still refused
    assert set(ch.unknown_keys(date(2008, 3, 3))) == {"stt_option_sell", "stt_option_exercise", "gst"}
    with pytest.raises(ch.ChargesUnknown, match="stt_option_sell"):
        ch.order_costs(SPREAD, date(2008, 3, 3))
    # before the ordinance (7-May-2005) stamp duty is unknown, never the later rate
    assert ch.rates_as_of(date(2005, 5, 6))["stamp_option_buy"].rate is None
