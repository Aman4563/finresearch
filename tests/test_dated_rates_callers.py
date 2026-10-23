"""#366 / #385: the non-registered callers that used a fixed 6.5 % now use the dated FBIL rate, and say which one
(tenor, curve date, source), or "fallback 6.5 %, no curve" with the reason. Expected values are computed here from
the formulas (r = 2 ln(1 + y/2) for a half-yearly par yield y; effective annual (1 + y/2)^2 - 1), not by calling the
code under test. Test database only; synthetic rows."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

DAY = date(2026, 10, 21)  # a Wednesday
PRIOR = date(2026, 10, 20)


def _curve(day: date, pts: tuple[tuple[str, str], ...] = (("0.25", "0.0600"), ("1", "0.0640"))) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import RateCurve

    with session_scope() as s:
        for years, y in pts:
            s.add(RateCurve(series="gsec_par", day=day, tenor=f"{years}Y", tenor_years=Decimal(years),
                            rate=Decimal(y)))  # fmt: skip


@pytest.fixture
def rates_db(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE rate_curve"))

    wipe()
    yield env
    wipe()


# ------------------------------------------------------------------ signals.rates: the dated rate or a labelled 6.5 %
def test_rate_on_reads_the_stored_curve_and_names_tenor_date_and_source(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    _curve(PRIOR)
    with session_scope() as s:
        r = rates.rate_on(s, DAY, 30 / 365)
    assert r["known"] and not r["fallback"]
    assert r["rate"] == pytest.approx(2 * math.log(1.03))  # 6.00 % half-yearly at the 0.25-year point
    assert r["tenor_years"] == 0.25 and r["as_of"] == "2026-10-20" and "FBIL G-sec par yield" in r["source"]


def test_rate_on_without_a_curve_is_the_labelled_fallback_with_the_reason(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    with session_scope() as s:
        r = rates.rate_on(s, DAY, 30 / 365)
    assert r["rate"] == 0.065 and r["fallback"] is True and r["known"] is False
    assert r["source"] == "fallback 6.5 %, no curve" and "no FBIL gsec_par curve" in r["note"]
    assert rates.label(r).startswith("fallback 6.5 %, no curve: no FBIL gsec_par curve")


def test_before_excludes_the_days_own_curve(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    _curve(PRIOR)
    _curve(DAY, (("0.25", "0.0700"),))
    with session_scope() as s:
        same, prior = rates.rate_on(s, DAY, 0.1), rates.rate_on(s, DAY, 0.1, before=True)
    assert same["rate"] == pytest.approx(2 * math.log(1.035)) and prior["rate"] == pytest.approx(2 * math.log(1.03))


async def test_rate_for_opens_its_own_session_and_labels_the_tenor(rates_db):
    from finresearch.signals import rates

    _curve(PRIOR)
    r = await rates.rate_for(DAY, 1.0)
    assert r["rate"] == pytest.approx(2 * math.log(1.032)) and r["tenor_years"] == 1.0
    assert "1-year point" in rates.label(r) and "2026-10-20" in rates.label(r)


def test_effective_annual_is_the_half_yearly_par_yield_compounded(rates_db):
    from finresearch.db import session_scope
    from finresearch.signals import rates

    _curve(PRIOR)
    with session_scope() as s:
        known, none = rates.rate_on(s, DAY, 0.25), rates.rate_on(s, date(2020, 1, 1), 0.25)
    assert rates.effective_annual(known) == pytest.approx(1.03**2 - 1)  # 6.09 % a year
    assert rates.effective_annual(none) == 0.065  # the fallback is the earlier stated annual default
