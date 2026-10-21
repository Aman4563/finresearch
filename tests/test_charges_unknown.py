"""Unknown charges block pricing (#365): before a key's first sourced row, or in a period recorded as unknown, the rate
is unknown (None), never borrowed from a later date; order_costs and exercise_stt refuse (ChargesUnknown) and the
backtest skips the entry. Hand-checked against fincalc.charges' RATES."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

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
