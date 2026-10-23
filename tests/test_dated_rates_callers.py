"""#479 (replacing #366/#385's FBIL lookups): the dated callers (`signals.rates.rate_on` / `rate_for`, read by the
stored-day F&O scenarios, fund metrics and the risk alerts) answer from the owner-entered tier, else the RBI repo-rate
proxy, never from stored FBIL rows and never with a fixed 6.5 %; every answer names its tier, date and source.
Expected values are computed here from the stated convention (a simple Actual/365 yield y for D days is continuous
z = ln(1 + y D/365) / (D/365); effective annual (1 + y D/365)^(365/D) - 1), not by calling the code under test.
Test database only; synthetic rows."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

DAY = date(2026, 10, 21)  # a Wednesday; repo 5.50 % since the 07-Oct-2026 hike


def z(y: float, days: int) -> float:
    return math.log(1 + y * days / 365) / (days / 365)


@pytest.fixture
def rates_db(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE rate_curve, owner_rate"))

    wipe()
    yield env
    wipe()


def test_rate_on_never_reads_stored_fbil_rows(rates_db):
    """Behaviour changed on purpose (#479): a stored FBIL par curve used to be the answer; FBIL is paused (#476)."""
    from finresearch.db import session_scope
    from finresearch.db.models import RateCurve
    from finresearch.signals import rates

    with session_scope() as s:
        s.add(RateCurve(series="gsec_par", day=date(2026, 10, 20), tenor="0.25Y", tenor_years=Decimal("0.25"),
                        rate=Decimal("0.0600")))  # fmt: skip
    with session_scope() as s:
        r = rates.rate_on(s, DAY, 30 / 365)
    assert r["known"] and r["tier"] == "proxy" and not r["fallback"]
    assert r["rate"] == pytest.approx(z(0.055, 30), abs=1e-12) and r["as_of"] == "2026-10-07"
    assert "FBIL" not in r["source"] and r["source"].startswith("RBI repo-rate proxy")


def test_there_is_no_fixed_6_5_percent_only_unknown_before_the_table(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    with session_scope() as s:
        old = rates.rate_on(s, date(2014, 12, 31), 0.25)
        y2020 = rates.rate_on(s, date(2020, 1, 15), 0.25)  # repo 5.15 % since 04-Oct-2019
    assert old["rate"] is None and old["known"] is False and old["fallback"] is False
    assert rates.label(old).startswith("unknown: 31-Dec-2014 is before 01-Jan-2015")
    assert y2020["yield"] == pytest.approx(0.0515) and y2020["as_of"] == "2019-10-04"


def test_before_excludes_a_decision_announced_that_day(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    with session_scope() as s:
        same, prior = (
            rates.rate_on(s, date(2026, 10, 7), 0.1),
            rates.rate_on(s, date(2026, 10, 7), 0.1, before=True),
        )
    days = round(0.1 * 365)  # 36 days
    assert same["rate"] == pytest.approx(z(0.055, days)) and prior["rate"] == pytest.approx(z(0.0525, days))


async def test_rate_for_opens_its_own_session_and_labels_the_tier_and_date(rates_db):
    from finresearch.signals import rates

    r = await rates.rate_for(DAY, 1.0)
    assert r["rate"] == pytest.approx(math.log(1.055)) and r["tenor_days"] == 365  # 365 days: z = ln(1 + y)
    assert rates.label(r) == "RBI repo-rate proxy: repo 5.50 % in force from 07-Oct-2026: 5.35% continuous"
    assert "held flat beyond 91 days" in r["note"]


def test_effective_annual_compounds_the_simple_yield_at_its_own_term(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    with session_scope() as s:
        known, none = rates.rate_on(s, DAY, 91 / 365), rates.rate_on(s, date(2014, 6, 30), 91 / 365)
    assert rates.effective_annual(known) == pytest.approx((1 + 0.055 * 91 / 365) ** (365 / 91) - 1)  # 5.61 %
    assert rates.effective_annual(none) is None  # unknown stays unknown, never 6.5 %
    assert rates.effective_annual(rates.given(0.07, "your input")) == 0.07


# ------------------------------------------------------------------ the risk alerts' shock reading names the rate
def test_a_known_shock_reading_says_which_rate_priced_the_book():
    from finresearch.alerts import risk_readers as rr

    note = "rate: RBI repo-rate proxy: repo 5.50 % in force from 07-Oct-2026: 5.46% continuous"
    rows = rr.book_readings({-0.03: -1500.0, 0.03: 900.0}, [note], 100_000.0, None, None, book_open=True)
    shock = next(rd for rid, _, rd in rows if rid == "limit_shock")
    assert shock.value is not None and note in shock.detail
