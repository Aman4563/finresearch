"""The event-volatility panel (#391) from the stored end-of-day IVs, index closes and the point-in-time calendar."""

import math
from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from finresearch.fincalc import bustime
from finresearch.fincalc.dates import IST

ELECTION = date(2024, 6, 4)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_iv_eod, index_eod, calendar_event, calendar_coverage"))

    wipe()
    yield env
    wipe()


def _seed(coverage: bool = True) -> None:
    from test_eventcal import _cov, _ev

    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod, IndexEod
    from finresearch.eventcal import calendar as cal
    from finresearch.eventcal import store

    with session_scope() as s:
        # the issue's example: 3-Jun-2024, near 13-Jun at 26.74 %, next 20-Jun at 22.58 %
        s.add(FnoIvEod(symbol="NIFTY", day=date(2024, 6, 3), near_expiry=date(2024, 6, 13), near_iv=Decimal("26.74"),
                       next_expiry=date(2024, 6, 20), next_iv=Decimal("22.58"), rate=Decimal("0.065")))  # fmt: skip
        for d, c in [
            (date(2024, 5, 31), 22530.70),
            (date(2024, 6, 3), 23263.90),
            (date(2024, 6, 4), 21884.50),
        ]:
            s.add(IndexEod(day=d, symbol="NIFTY", index_name="Nifty 50", close=Decimal(str(c))))
        cov = [_cov(k, date(2024, 12, 31), date(2024, 1, 1)) for k in cal.TIER1_TYPES] if coverage else []
        store.upsert(s, [_ev("general_election", ELECTION, date(2024, 3, 16))], cov)


def _build(now: datetime, calendar=None, symbol: str = "NIFTY") -> dict:
    from finresearch.db import session_scope
    from finresearch.signals import event_vol

    with session_scope() as s:
        return event_vol.build(s, symbol, now, calendar=calendar)


def test_calendar_clock_reproduces_the_issue_example(clean):
    _seed()
    got = _build(datetime(2024, 6, 3, 18, 0, tzinfo=IST))
    assert got["status"] == "ok" and got["clock"] == "calendar"
    (ev,) = got["upcoming"]
    assert ev["day"] == "2024-06-04" and ev["move"] == pytest.approx(0.0369476829, rel=1e-6)
    assert got["elevation"]["status"] == "elevated"
    assert got["history"] == []  # the results have not happened yet


def test_business_clock_when_the_holiday_list_covers_both_expiries(clean):
    _seed()
    cal = bustime.Calendar(date(2024, 1, 1), date(2024, 12, 31))
    got = _build(datetime(2024, 6, 3, 18, 0, tzinfo=IST), calendar=cal)
    # from 3-Jun 15:30: 8 sessions and 2 weekend days to 13-Jun, 13 and 4 to 20-Jun; Y = 252 + 113 x 0.10
    t1, t2 = (8 + 0.2) / 263.3, (13 + 0.4) / 263.3
    want = (0.2674**2 - 0.2258**2) / (1 / t1 - 1 / t2)
    assert got["clock"] == "business"
    assert got["upcoming"][0]["j2"] == pytest.approx(want, rel=1e-9)


def test_history_pairs_the_realised_move_with_the_implied_one_known_before_it(clean):
    _seed()
    got = _build(datetime(2024, 6, 5, 18, 0, tzinfo=IST))
    assert got["upcoming"] == []  # the event has passed: no longer upcoming
    (h,) = got["history"]
    assert h["type"] == "general_election" and h["n"] == 1
    r = math.log(21884.50 / 23263.90)
    assert h["moves"][0]["r"] == pytest.approx(r, rel=1e-9)
    assert h["moves"][0]["implied_move"] == pytest.approx(0.0369476829, rel=1e-6)
    assert h["fairness"]["rho"] == pytest.approx(r * r / 0.0369476829**2, rel=1e-5)


def test_uncovered_calendar_never_reads_as_no_event(clean):
    from finresearch.db import session_scope

    _seed(coverage=False)
    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event"))
    got = _build(datetime(2024, 6, 3, 18, 0, tzinfo=IST))
    assert got["upcoming"] == [] and got["elevation"]["status"] == "unknown"
    assert got["window"]["status"] == "unknown"


def test_no_stored_iv_or_a_stock_is_said_so(clean):
    assert _build(datetime(2024, 6, 3, 18, 0, tzinfo=IST))["status"] == "no_data"
    assert _build(datetime(2024, 6, 3, 18, 0, tzinfo=IST), symbol="EXMPL")["status"] == "not_index"


def test_a_row_dated_today_is_not_read_before_the_close(clean):
    _seed()
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod

    with session_scope() as s:  # a later row whose close is not yet known at noon
        s.add(FnoIvEod(symbol="NIFTY", day=date(2024, 6, 4), near_expiry=date(2024, 6, 13), near_iv=Decimal("30"),
                       next_expiry=date(2024, 6, 20), next_iv=Decimal("20"), rate=Decimal("0.065")))  # fmt: skip
    got = _build(datetime(2024, 6, 4, 12, 0, tzinfo=IST))
    assert got["as_of"] == "2024-06-03" and got["upcoming"][0]["move"] == pytest.approx(
        0.0369476829, rel=1e-6
    )
