"""#479: owner-entered risk-free rates (refseries.owner_rates, table owner_rate) and the signals.rates tier that reads
them. Test database only; the yields are the ones RBI printed (typed here as literals)."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

OCT14, OCT20 = date(2026, 10, 14), date(2026, 10, 20)


@pytest.fixture
def owner_db(env, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    monkeypatch.setattr(rates, "OWNER", None)  # read the table (conftest stubs it to nothing)
    with session_scope() as s:
        s.execute(text("TRUNCATE owner_rate"))
    yield env
    with session_scope() as s:
        s.execute(text("TRUNCATE owner_rate"))


def test_an_entry_is_stored_as_a_fraction_and_replaced_by_a_new_one_for_the_same_day_and_tenor(owner_db):
    from finresearch.db import session_scope
    from finresearch.refseries import owner_rates as O

    with session_scope() as s:
        O.add(s, OCT14, "91D", Decimal("5.7281"), "RBI auction result, 14-Oct-2026", today=OCT20)
    with session_scope() as s:
        O.add(s, OCT14, "91D", Decimal("5.7300"), None, today=OCT20)
    with session_scope() as s:
        got = O.entries(s)
    assert [(e.day, e.tenor_days, e.yld, e.source) for e in got] == [(OCT14, 91, Decimal("0.057300"), None)]


@pytest.mark.parametrize(
    ("day", "tenor", "pct", "why"),
    [
        # before the policy table: the repo change since the entry's day would be unknown
        (date(2014, 12, 31), "91D", "5", "01-Jan-2015"),
        (date(2026, 10, 21), "91D", "5", "future"),
        (OCT14, "0D", "5", "tenor"),
        (OCT14, "365D", "5", "tenor"),
        (OCT14, "3M", "5", "tenor"),
        (OCT14, "91D", "0", "yield"),
        (OCT14, "91D", "25", "yield"),
    ],
)
def test_entries_are_validated(owner_db, day, tenor, pct, why):
    from finresearch.db import session_scope
    from finresearch.refseries import owner_rates as O

    with session_scope() as s, pytest.raises(ValueError, match=why):
        O.add(s, day, tenor, Decimal(pct), None, today=OCT20)


def test_remove_says_whether_there_was_an_entry(owner_db):
    from finresearch.db import session_scope
    from finresearch.refseries import owner_rates as O

    with session_scope() as s:
        O.add(s, OCT14, "182D", Decimal("6.2896"), None, today=OCT20)
    with session_scope() as s:
        assert O.remove(s, OCT14, "182D") is True
    with session_scope() as s:
        assert O.remove(s, OCT14, "182D") is False and O.entries(s) == []


def test_rate_on_reads_the_owner_tier_in_the_callers_session(owner_db):
    from finresearch.db import session_scope
    from finresearch.refseries import owner_rates as O
    from finresearch.signals import rates

    with session_scope() as s:
        O.add(s, OCT14, "91D", Decimal("5.7281"), None, today=OCT20)
    with session_scope() as s:
        r = rates.rate_on(s, OCT20, 91 / 365)
        before = rates.rate_on(s, OCT14, 91 / 365, before=True)
    assert r["tier"] == "owner" and r["rate"] == pytest.approx(math.log(1 + 0.057281 * 91 / 365) * 365 / 91)
    assert before["tier"] == "proxy" and before["yield"] == pytest.approx(0.055)


async def test_an_unreadable_owner_table_falls_through_to_the_proxy_and_says_so(owner_db, monkeypatch):
    from finresearch.refseries import owner_rates as O
    from finresearch.signals import rates

    def broken(s):
        raise RuntimeError("no table")

    monkeypatch.setattr(O, "entries", broken)
    r = await rates.rate_for(OCT20, 91 / 365)
    assert r["tier"] == "proxy" and r["yield"] == pytest.approx(0.055)
    assert "could not be read (RuntimeError)" in r["note"]
