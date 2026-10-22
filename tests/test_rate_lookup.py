"""#366: the dated rate lookup (refseries.rate_lookup, signals.rates.risk_free_on). A date without a stored curve
is unknown (rate None with a reason), never the fixed 6.5 %. Expected values are computed here from the formulas,
not by calling the code under test."""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text

MON, TUE, FRI = date(2022, 1, 3), date(2022, 1, 4), date(2022, 1, 7)


@pytest.fixture
def seeded(env):
    from finresearch.db import session_scope
    from finresearch.db.models import RateCurve

    with session_scope() as s:
        s.execute(text("TRUNCATE rate_curve"))
        for tenor, years, y in (("0.25Y", "0.25", "0.040"), ("0.5Y", "0.5", "0.041"), ("1Y", "1", "0.045")):
            s.add(
                RateCurve(
                    series="gsec_par", day=MON, tenor=tenor, tenor_years=Decimal(years), rate=Decimal(y)
                )
            )
        s.add(
            RateCurve(
                series="tbill", day=MON, tenor="3M", tenor_years=Decimal(91) / 365, rate=Decimal("0.0362")
            )
        )
    yield env


def test_par_rate_interpolates_and_converts_to_continuous(seeded):
    from finresearch.db import session_scope
    from finresearch.refseries.rate_lookup import par_rate_on

    with session_scope() as s:
        r = par_rate_on(s, TUE, 0.375)  # Tuesday: Monday's curve, one day old
    y = 0.0405  # halfway between 4.0 % (0.25y) and 4.1 % (0.5y)
    assert r["known"] and r["as_of"] == "2022-01-03" and r["par_yield"] == pytest.approx(y)
    assert r["rate"] == pytest.approx(2 * math.log(1 + y / 2))
    assert "FBIL" in r["source"] and "2022-01-03" in r["source"]


def test_short_expiry_uses_the_shortest_tenor_and_says_so(seeded):
    from finresearch.db import session_scope
    from finresearch.refseries.rate_lookup import par_rate_on

    with session_scope() as s:
        r = par_rate_on(s, MON, 7 / 365)
    assert r["par_yield"] == pytest.approx(0.040) and "shortest" in r["note"]


def test_no_curve_is_unknown_never_the_fixed_rate(seeded):
    from finresearch.db import session_scope
    from finresearch.refseries.rate_lookup import par_rate_on

    with session_scope() as s:
        before = par_rate_on(s, date(2021, 12, 31), 0.5)
        stale = par_rate_on(s, date(2022, 1, 10), 0.5)  # 7 days after the last curve: too old
    for r in (before, stale):
        assert r["known"] is False and r["rate"] is None and r["par_yield"] is None
        assert "0.065" not in str(r) and r["note"]
    assert "older than" in stale["note"]


def test_before_excludes_the_same_day_curve(seeded):
    """A backtest deciding during the session must not read the curve FBIL publishes that evening."""
    from finresearch.db import session_scope
    from finresearch.refseries.rate_lookup import par_rate_on

    with session_scope() as s:
        assert par_rate_on(s, MON, 0.5, before=True)["known"] is False
        assert par_rate_on(s, TUE, 0.5, before=True)["as_of"] == "2022-01-03"


def test_tbill_rate_is_converted_on_a_stated_basis(seeded):
    from finresearch.db import session_scope
    from finresearch.refseries.rate_lookup import tbill_rate_on

    with session_scope() as s:
        r = tbill_rate_on(s, FRI, "3M")
        missing = tbill_rate_on(s, FRI, "6M")
    assert r["yield"] == pytest.approx(0.0362)
    assert r["rate"] == pytest.approx(math.log(1 + 0.0362 * 91 / 365) * 365 / 91)
    assert "assumed" in r["basis"]
    assert missing["known"] is False and missing["rate"] is None


async def test_signals_risk_free_on_reads_the_store(seeded):
    from finresearch.signals.rates import risk_free_on

    r = await risk_free_on(TUE, 0.5)
    assert r["known"] and r["rate"] == pytest.approx(2 * math.log(1 + 0.041 / 2)) and r["fallback"] is False
    gone = await risk_free_on(date(2020, 1, 1), 0.5)
    assert gone["rate"] is None and gone["known"] is False
