"""P4.5 (#398): the stored-series readers (alerts.risk_readers): IV30 shocks from fno_iv_eod, the overnight gap of the
front future from fno_bhav, and per open short leg (fno_mark) the risk-neutral touch probability, the chance one
overnight gap jumps past the strike, and the tier-1 event check for index positions. A series not stored for the
day, or tonight's marks not written, reads unknown with the reason. Test database only; synthetic rows."""

from __future__ import annotations

import itertools
import math
import statistics
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import text

from finresearch.fincalc import risk_alerts as ra

DAY = date(2026, 10, 21)
NOW = datetime(2026, 10, 21, 16, 30, tzinfo=UTC)  # 22:00 IST
EXP = date(2026, 10, 27)


def _days(n: int) -> list[date]:
    out, d = [], DAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_iv_eod, fno_bhav, fno_mark, alert_eval_slot, calendar_event, "
                           "calendar_coverage, broker_connection CASCADE"))  # fmt: skip

    wipe()
    yield env
    wipe()


def _ivs(sym: str, bump: float, n: int = 270) -> list[float]:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod

    ivs = [15.0 + (0.5 if i % 2 else 0.0) for i in range(n)]
    ivs[-1] = ivs[-2] + bump
    with session_scope() as s:
        for d, v in zip(_days(n), ivs, strict=True):
            s.add(FnoIvEod(symbol=sym, day=d, iv_30d=D(str(v)), rate=D("0.065")))
    return ivs


def _futures(sym: str, last_open: float, n: int = 300) -> list[float]:
    """One contract per month-end-ish expiry far away, closes alternating ±1 %; today's open `last_open`."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav

    closes = [24000 * (1.01 if i % 2 else 1.0) for i in range(n)]
    with session_scope() as s:
        for i, (d, c) in enumerate(zip(_days(n), closes, strict=True)):
            op = last_open if i == n - 1 else c
            s.add(FnoBhav(day=d, symbol=sym, instrument="IDF", expiry=date(2027, 12, 30), strike=D(0),
                          option_type="XX", open=D(str(round(op, 2))), close=D(str(round(c, 2)))))  # fmt: skip
    return closes


def _marks(rows: list[dict], status: str = "done", note: str | None = None) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, FnoMark

    with session_scope() as s:
        for i, r in enumerate(rows):
            s.add(FnoMark(day=DAY, book="paper", leg_key=f"paper_leg:{i}", symbol=r.get("symbol", "NIFTY"), expiry=EXP,
                          right=r.get("right", "put"), strike=D(str(r["strike"])), qty=r.get("qty", -75),
                          spot=D("24000"), iv=None if r.get("iv") is None else D(str(r["iv"])),
                          t_years=D(str(4 / 365))))  # fmt: skip
        s.add(AlertEvalSlot(slot=f"fno_marks:{DAY}", result={"status": status, "real_note": note}))


def _read(fn, *a):
    from finresearch.db import session_scope

    with session_scope() as s:
        return {(r, i): rd for r, i, rd in fn(s, *a)}


def test_iv_shock_and_gap_read_the_stored_series_and_name_them(clean) -> None:
    from finresearch.alerts.risk_readers import series_readings

    ivs = _ivs("NIFTY", bump=4.0)
    closes = _futures("NIFTY", last_open=24000 * 0.96)  # the previous close (index 298) is 24000
    got = _read(series_readings, ["NIFTY"], DAY)
    assert got[("iv_shock", "NIFTY")].value == pytest.approx(ra.iv_shock(ivs).value)
    assert (
        got[("iv_shock", "NIFTY")].value >= ra.SHOCK_Z
        and "fno_iv_eod.iv_30d" in got[("iv_shock", "NIFTY")].series
    )
    assert got[("cum_shock", "NIFTY")].value == pytest.approx(ra.cum_shock(ivs).value)
    rets = [math.log(b / a) for a, b in itertools.pairwise(closes[:-1])]  # up to the previous close
    want = abs(math.log(0.96)) / statistics.stdev(rets[-252:])
    assert got[("gap", "NIFTY")].value == pytest.approx(want) and "fno_bhav" in got[("gap", "NIFTY")].series


def test_a_series_not_stored_for_the_day_reads_unknown(clean) -> None:
    from finresearch.alerts.risk_readers import series_readings

    _ivs("BANKNIFTY", bump=4.0)
    got = _read(series_readings, ["BANKNIFTY"], DAY + timedelta(days=1))
    assert (
        got[("iv_shock", "BANKNIFTY")].value is None and "2026-10-22" in got[("iv_shock", "BANKNIFTY")].detail
    )
    assert got[("gap", "BANKNIFTY")].value is None


def test_short_legs_get_touch_gap_through_and_event_readings(clean) -> None:
    from finresearch.alerts.risk_readers import leg_readings
    from finresearch.db import session_scope
    from finresearch.db.models import CalendarCoverage, CalendarEvent

    _futures("NIFTY", last_open=24000.0)
    _marks(
        [{"strike": 23800, "iv": 0.14}, {"strike": 23000, "qty": 75, "iv": 0.16}]
    )  # a short and a long put
    with session_scope() as s:
        s.add(CalendarEvent(type="rbi_mpc", tier=1, scope="IN", day=date(2026, 10, 23), title="RBI policy",
                            source_url="https://example.org/rbi", known_since=NOW - timedelta(days=90)))  # fmt: skip
        for t in ("rbi_mpc", "union_budget", "general_election"):
            s.add(CalendarCoverage(type=t, scope="IN", through=date(2027, 3, 31), source_url="https://example.org",
                                   known_since=NOW - timedelta(days=90)))  # fmt: skip
    got = _read(leg_readings, DAY, NOW)
    inst = "NIFTY 2026-10-27 23800PE"
    f = 24000 * math.exp(0.065 * 4 / 365)
    assert got[("touch", inst)].value == pytest.approx(ra.touch_prob(f, 23800, 0.14, 4 / 365))
    assert "risk-neutral" in got[("touch", inst)].detail
    assert not any(i.endswith("23000PE") for _, i in got)  # a long leg is not at risk of a gap through it
    # 299 same-contract gaps: 298 of +-ln(1.01) = 0.995 % (opens equal the closes, which alternate +-1 %) and
    # tonight's 0; the strike is |ln(23800/24000)| = 0.837 % away, so 298 of 299 gaps jump past it
    assert got[("gap_stop", inst)].value == pytest.approx(298 / 299)
    assert got[("event", "NIFTY")].value == 1.0 and "RBI" in got[("event", "NIFTY")].detail


def _groww() -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import BrokerConnection

    with session_scope() as s:
        s.add(BrokerConnection(key="groww", enabled=True))


def test_marks_not_written_or_an_unread_groww_book_read_unknown(clean) -> None:
    from finresearch.alerts.risk_readers import leg_readings

    _groww()  # a book that may hold positions
    got = _read(leg_readings, DAY, NOW)
    assert got[("touch", "BOOK")].value is None and "fno_marks" in got[("touch", "BOOK")].detail
    assert got[("gap_stop", "BOOK")].value is None
    _marks([], note="Groww positions not read today (last read never)")
    got = _read(leg_readings, DAY, NOW)
    assert got[("gap_stop", "BOOK")].value is None and "not read today" in got[("gap_stop", "BOOK")].detail


def test_a_short_leg_without_an_iv_reads_unknown_for_that_leg(clean) -> None:
    from finresearch.alerts.risk_readers import leg_readings

    _futures("NIFTY", last_open=24000.0)
    _marks([{"strike": 23800, "iv": None}])
    got = _read(leg_readings, DAY, NOW)
    assert got[("touch", "NIFTY 2026-10-27 23800PE")].value is None


def test_with_no_paper_trade_open_and_groww_not_connected_there_is_no_book_to_check(clean) -> None:
    from finresearch.alerts.risk_readers import leg_readings

    assert _read(leg_readings, DAY, NOW) == {}  # marks not written, but the book is known to be empty
