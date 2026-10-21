"""#379: model inventory, evidence staleness (age and market-regime changes) and run provenance."""

from __future__ import annotations

from datetime import UTC, date, datetime

from finresearch.inventory import regimes


# --------------------------------------------------------------------------- staleness
def test_a_recent_run_after_every_regime_change_is_current():
    assert regimes.staleness(date(2026, 10, 12), date(2026, 10, 22)) == []


def test_a_run_older_than_max_age_is_stale_with_its_age():
    # 22-Oct-2026 − 6-Sep-2026 = 46 days (Sep has 30: 24 + 22)
    why = regimes.staleness(date(2026, 9, 6), date(2026, 10, 22))
    assert len(why) == 1 and "46 days old" in why[0] and "45" in why[0]
    assert regimes.staleness(date(2026, 9, 7), date(2026, 10, 22)) == []  # exactly 45 days: still current


def test_a_run_from_before_a_regime_change_is_stale_even_when_young():
    # NIFTY weekly expiry moved from Thursday to Tuesday from 1-Sep-2025
    why = regimes.staleness(date(2025, 8, 30), date(2025, 9, 10))
    assert len(why) == 1 and "Tuesday" in why[0] and "01-Sep-2025" in why[0]


def test_stt_changes_come_from_the_dated_charges_table():
    days = {c.day for c in regimes.regime_changes() if c.tag == "stt"}
    assert days == {date(2024, 10, 1), date(2026, 4, 1)}  # fincalc/charges.py stt_* rows
    why = regimes.staleness(date(2026, 3, 25), date(2026, 4, 10))
    assert len(why) == 1 and "STT" in why[0]


def test_weekly_expiry_cut_of_nov_2024_is_a_regime_change():
    why = regimes.staleness(date(2024, 11, 15), date(2024, 11, 25))
    assert len(why) == 1 and "20-Nov-2024" in why[0]


def test_an_unknown_run_date_is_stale_never_current():
    why = regimes.staleness(None, date(2026, 10, 22))
    assert why and "unknown" in why[0]


def test_run_day_reads_iso_and_datetimes_in_ist():
    # 20:00 UTC on 21-Oct is 01:30 IST on 22-Oct
    assert regimes.run_day("2026-10-21T20:00:00+00:00") == date(2026, 10, 22)
    assert regimes.run_day(datetime(2026, 10, 21, 20, 0, tzinfo=UTC)) == date(2026, 10, 22)
    assert regimes.run_day(None) is None and regimes.run_day("garbage") is None
