"""Unknown charges block pricing (#365): before a key's first sourced row, or in a period recorded as unknown, the rate
is unknown (None), never borrowed from a later date; order_costs and exercise_stt refuse (ChargesUnknown) and the
backtest skips the entry. Hand-checked against fincalc.charges' RATES."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from finresearch.fincalc import charges as ch

LEG = [ch.TradeLeg("option", "sell", 100.0, 50)]


def test_a_date_before_every_sourced_row_is_unknown():
    r = ch.rates_as_of(date(2004, 1, 2))
    assert {v.status for v in r.values()} == {"unknown"} and {v.rate for v in r.values()} == {None}
    assert set(ch.unknown_keys(date(2004, 1, 2))) == set(ch.KEYS)
    with pytest.raises(ch.ChargesUnknown, match="stt_option_sell"):
        ch.order_costs(LEG, date(2004, 1, 2))
    with pytest.raises(ch.ChargesUnknown):
        ch.exercise_stt(1000.0, date(2004, 1, 2))


def test_a_rate_is_never_borrowed_from_a_later_row(monkeypatch):
    rows = tuple(r for r in ch.RATES if not (r.key == "sebi_fee" and r.effective_from < date(2021, 4, 1)))
    monkeypatch.setattr(ch, "RATES", rows)
    assert ch.rates_as_of(date(2021, 3, 31))["sebi_fee"].rate is None
    assert ch.unknown_keys(date(2021, 3, 31)) == ["sebi_fee"]
    assert ch.rates_as_of(date(2021, 4, 1))["sebi_fee"].rate is not None
    with pytest.raises(ch.ChargesUnknown, match="sebi_fee"):
        ch.order_costs(LEG, date(2021, 3, 31))


def test_an_override_makes_an_unknown_key_known():
    c = ch.order_costs(LEG, date(2004, 1, 2), overrides={k: "0" for k in ch.KEYS})
    assert c.total == pytest.approx(20.0)  # brokerage only


def test_unknown_rows_only_precede_known_ones():
    """Unknown is a prefix per key: a date with every rate known stays priceable for every later date (exits,
    expiries), so checking the entry day is enough for the backtest."""
    for key in ch.KEYS:
        rows = sorted((r for r in ch.RATES if r.key == key), key=lambda r: r.effective_from)
        seen_known = False
        for r in rows:
            assert not (seen_known and r.rate is None), (
                f"{key}: an unknown row after a known one ({r.effective_from})"
            )
            seen_known |= r.rate is not None


def test_backtest_skips_an_entry_whose_charges_are_unknown(monkeypatch):
    import test_backtest_engine as te

    from finresearch.backtest import engine, specs

    monkeypatch.setattr(ch, "RATES", tuple(r for r in ch.RATES if r.key != "stamp_option_buy"))
    x = te.put_day()
    st = engine.SpecState(replace(specs.by_key("nifty_pcs_m"), iv_pct_min=None))
    assert engine.try_enter(st, x, "NIFTY", te.clk(x), [15.0], te.BOOK) is None
    assert st.skips == {"charges unknown on the trade date": 1}  # never priced at 0 or at a later rate


SPREAD = [
    ch.TradeLeg("option", "buy", 150.0, 65),
    ch.TradeLeg("option", "sell", 60.0, 65),
]  # 9,750 buy, 3,900 sell


def test_vault_years_unknown_keys_follow_the_sources():
    # option-sale STT before Jun-2008 was on strike + premium; service tax reached exchange charges only on
    # 16-May-2008. Stamp duty before Jul-2020 (Maharashtra's) and NSE's options charge before Apr-2017 (PTI) are
    # known since the owner's decisions of 23-Oct-2026 (#471; tests/test_charges_owner_1023.py)
    assert set(ch.unknown_keys(date(2008, 3, 3))) == {"stt_option_sell", "stt_option_exercise", "gst"}
    assert ch.unknown_keys(date(2010, 6, 1)) == []
    assert ch.unknown_keys(date(2018, 6, 1)) == []
    assert ch.unknown_keys(date(2020, 7, 1)) == []


@pytest.mark.parametrize(
    ("on", "key", "rate"),
    [
        (
            date(2008, 6, 2),
            "stt_option_sell",
            "0.00017",
        ),  # Finance Act 2008, NSE/F&A/10706: 0.017 % of premium
        (date(2016, 5, 31), "stt_option_sell", "0.00017"),  # unchanged in 2013 (NSE/FATAX/23500)
        (date(2013, 5, 31), "stt_futures_sell", "0.00017"),  # Finance Act 2006
        (date(2009, 9, 30), "exchange_futures", "0.00002"),  # Rs 2/lakh (NSE/F&A/13029 "presently")
        (date(2009, 10, 1), "exchange_futures", "0.000019"),  # Rs 1.90/lakh first slab (NSE/F&A/13029)
        (
            date(2017, 4, 1),
            "exchange_option",
            "0.0005",
        ),  # Rs 50/lakh above the flat first band (NSE/FA/34601)
        (date(2009, 6, 30), "sebi_fee", "0.000002"),  # Rs 20/crore
        (date(2009, 7, 1), "sebi_fee", "0.000001"),  # Rs 10/crore
        (date(2014, 5, 23), "sebi_fee", "0.000002"),  # Rs 20/crore (SEBI gazette 2014)
        (date(2017, 4, 1), "sebi_fee", "0.0000015"),  # Rs 15/crore
        (date(2019, 4, 1), "sebi_fee", "0.000001"),  # Rs 10/crore
        (date(2020, 6, 1), "sebi_fee", "0.0000005"),  # halved June 2020 to March 2021 (SEBI PR 24/2020)
        (date(2008, 5, 16), "gst", "0.1236"),  # service tax 12 % + 3 % cesses
        (date(2009, 2, 24), "gst", "0.103"),
        (date(2012, 4, 1), "gst", "0.1236"),
        (date(2015, 6, 1), "gst", "0.14"),
        (date(2015, 11, 15), "gst", "0.145"),
        (date(2016, 6, 1), "gst", "0.15"),
        (date(2017, 7, 1), "gst", "0.18"),
    ],
)
def test_pre_2021_rates(on, key, rate):
    assert ch.rates_as_of(on)[key].rate == Decimal(rate)


def test_a_mid_2020_spread_costs_the_mid_2020_rates():
    c = ch.order_costs(SPREAD, date(2020, 8, 3))
    # STT 3,900 x 0.05 % = 1.95; NSE 13,650 x 0.05 % = 6.825; SEBI 13,650 x Rs 5/crore = 0.006825;
    # stamp 9,750 x 0.003 % = 0.2925; brokerage 40; GST 18 % x (40 + 6.825 + 0.006825) = 8.4297285
    assert c.lines == pytest.approx({"stt": 1.95, "exchange": 6.825, "sebi": 0.006825, "stamp": 0.2925,
                                     "brokerage": 40.0, "gst": 8.4297285})  # fmt: skip


def test_exercise_stt_before_sep_2019_is_on_the_settlement_value():
    # 0.125 % (Finance Act 2008) of the settlement price x quantity until 31-Aug-2019, of the intrinsic value after
    assert ch.exercise_stt(500 * 75, date(2018, 6, 28), settlement_value=10500 * 75) == pytest.approx(984.375)
    with pytest.raises(ch.ChargesUnknown, match="settlement value"):
        ch.exercise_stt(500 * 75, date(2018, 6, 28))
    assert ch.exercise_stt(500 * 75, date(2019, 9, 26), settlement_value=10500 * 75) == pytest.approx(46.875)


def test_paper_settlement_passes_the_settlement_value():
    from finresearch.fincalc import paper

    legs = [
        paper.PaperLeg("call", 10000.0, "buy", 75, 300.0),
        paper.PaperLeg("put", 10000.0, "buy", 75, 100.0),
    ]
    assert paper.settle(legs, 10500.0, date(2018, 6, 28)).exercise_stt == pytest.approx(
        984.375
    )  # the put is OTM
