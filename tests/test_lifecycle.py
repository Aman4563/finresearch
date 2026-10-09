"""End of life of tracked instruments and portfolio-aware targeting (#293). Synthetic instruments only."""

from __future__ import annotations

from datetime import date, timedelta

from finresearch.monitor import lifecycle as L

D0 = date(2026, 10, 1)


def _days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def test_one_missing_day_never_ends_an_instrument():
    st = L.note_pass(None, present=True, day=D0, source="NSE bonds list")
    st = L.note_pass(st, present=False, day=D0 + timedelta(days=1), source="NSE bonds list")
    assert st["missing_days"] == ["2026-10-02"] and not st.get("ended")
    assert L.ended(st, D0 + timedelta(days=1)) is None


def test_nine_missing_passes_do_not_end_ten_do():
    st = None
    for d in _days(9):
        st = L.note_pass(st, present=False, day=d, source="x")
    assert len(st["missing_days"]) == 9 and not st.get("ended")
    st = L.note_pass(st, present=False, day=D0 + timedelta(days=9), source="x")
    assert st["ended"]["reason"] == "missing" and st["ended"]["on"] == "2026-10-10"
    assert L.ended(st, D0 + timedelta(days=9))["reason"] == "missing"


def test_a_retry_on_the_same_day_counts_once_and_a_sighting_resets_the_run():
    st = None
    for _ in range(12):  # a pass retried all day long is still one missing day
        st = L.note_pass(st, present=False, day=D0, source="x")
    assert st["missing_days"] == ["2026-10-01"]
    for d in _days(9):
        st = L.note_pass(st, present=False, day=d, source="x")
    st = L.note_pass(st, present=True, day=D0 + timedelta(days=9), source="x")
    assert st["missing_days"] == [] and st["last_seen"] == "2026-10-10"
    st = L.note_pass(st, present=False, day=D0 + timedelta(days=10), source="x")
    assert len(st["missing_days"]) == 1 and not st.get("ended")


def test_reappearing_revives_a_missing_instrument_but_not_a_matured_bond():
    st = None
    for d in _days(10):
        st = L.note_pass(st, present=False, day=d, source="x")
    st = L.note_pass(st, present=True, day=D0 + timedelta(days=11), source="x")
    assert st["ended"] is None and st["revived"]["revived_on"] == "2026-10-12"
    assert L.ended({"maturity": "2026-10-05", "last_seen": "2026-10-12"}, D0 + timedelta(days=11))["reason"] == "matured"


def test_maturity_unknown_is_never_matured():
    assert L.matured(None, D0) is False and L.matured("", D0) is False
    assert L.matured("2026-10-01", D0) is True  # matures today: ended today (no trade settles after it)
    assert L.matured(date(2026, 10, 2), D0) is False
    assert L.ended({}, D0) is None
