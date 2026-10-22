"""#383 daily F&O marks (fnobook.marks): one row per open leg per session from the stored bhavcopy, never overwritten;
unknown prices, spots and IVs stay NULL with a reason. Synthetic contracts and prices only."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import delete, select

from finresearch.fincalc.dates import IST

EXPIRY = date(2026, 10, 27)
DAY = date(2026, 10, 20)


def _clean():
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoExitRule, FnoMark, PaperLeg, PaperStrategy

    with session_scope() as s:
        for m in (FnoMark, FnoExitRule, PaperLeg, PaperStrategy):
            s.execute(delete(m))
        s.execute(delete(FnoBhav).where(FnoBhav.symbol == "NIFTY", FnoBhav.expiry == EXPIRY))


@pytest.fixture
def db(env):
    _clean()
    yield env
    _clean()


def bhav(s, day, strike, kind, settle, underlying=D("25100")):
    from finresearch.db.models import FnoBhav

    s.add(FnoBhav(day=day, symbol="NIFTY", instrument="IDO", expiry=EXPIRY, strike=D(strike), option_type=kind,
                  close=settle + 1, settle=settle, underlying=underlying))  # fmt: skip


def spread(s, opened=datetime(2026, 10, 19, 11, 0, tzinfo=IST)):
    from finresearch.fnobook import book
    from finresearch.signals.fno import LegIn

    q = [
        book.Quote(120.0, "Groww LTP", "NIFTY26OCT25000CE"),
        book.Quote(50.0, "Groww LTP", "NIFTY26OCT25200CE"),
    ]
    return book.record(s, symbol="NIFTY", expiry=EXPIRY, lot=75, legs_in=[LegIn("call", 25000, "buy", 1),
                       LegIn("call", 25200, "sell", 1)], quotes=q, slippage_pct=1.0, brokerage=20, now=opened)  # fmt: skip


def test_paper_legs_are_marked_from_the_bhavcopy_with_implied_iv(db):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark
    from finresearch.fincalc import options as o
    from finresearch.fnobook import marks

    with session_scope() as s:
        st = spread(s)
        sid, leg_ids = st.id, [x.id for x in st.legs]
        bhav(s, DAY, 25000, "CE", D("180.00"))
        bhav(s, DAY, 25200, "CE", D("80.00"))
    with session_scope() as s:
        got = marks.record(s, DAY)
    assert got == {"paper": 2, "real": 0, "unpriced": 0}
    with session_scope() as s:
        rows = {r.leg_key: r for r in s.scalars(select(FnoMark))}
    a, b = rows[f"paper_leg:{leg_ids[0]}"], rows[f"paper_leg:{leg_ids[1]}"]
    assert (a.qty, b.qty) == (75, -75) and a.paper_id == sid and a.book == "paper"
    assert a.price == D("180.00") and "settle" in a.price_source
    assert a.spot == D("25100.00")
    t = 7 / 365  # 15:30 IST on the 20th to 15:30 IST on the 27th
    assert float(a.t_years) == pytest.approx(t, abs=1e-8)
    assert float(a.iv) == pytest.approx(o.implied_vol("call", 180, 25100, 25000, t, marks.RATE), abs=1e-6)


def test_marks_are_written_once_and_never_overwritten(db):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoMark
    from finresearch.fnobook import marks

    with session_scope() as s:
        spread(s)
        bhav(s, DAY, 25000, "CE", D("180.00"))
        bhav(s, DAY, 25200, "CE", D("80.00"))
    with session_scope() as s:
        marks.record(s, DAY)
    with session_scope() as s:
        s.execute(delete(FnoBhav).where(FnoBhav.strike == D(25000), FnoBhav.day == DAY))
        bhav(s, DAY, 25000, "CE", D("999.00"))
    with session_scope() as s:
        assert marks.record(s, DAY) == {"paper": 0, "real": 0, "unpriced": 0}
    with session_scope() as s:
        assert D("999.00") not in set(s.scalars(select(FnoMark.price)))


def test_missing_bhavcopy_row_is_unknown_never_zero(db):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark
    from finresearch.fnobook import marks

    with session_scope() as s:
        spread(s)
        bhav(s, DAY, 25000, "CE", D("180.00"))  # the short 25200 call has no stored row
    with session_scope() as s:
        assert marks.record(s, DAY) == {"paper": 2, "real": 0, "unpriced": 1}
    with session_scope() as s:
        r = s.scalars(select(FnoMark).where(FnoMark.strike == D(25200))).one()
    assert r.price is None and r.iv is None and "no stored bhavcopy row" in r.note


def test_closed_and_not_yet_open_trades_are_not_marked(db):
    from finresearch.db import session_scope
    from finresearch.fnobook import marks

    with session_scope() as s:
        spread(s, opened=datetime(2026, 10, 21, 10, 0, tzinfo=IST))  # opened after DAY
        st = spread(s)
        st.status, st.closed_at = "closed", datetime(2026, 10, 20, 14, 0, tzinfo=IST)  # closed during DAY
        bhav(s, DAY, 25000, "CE", D("180.00"))
        bhav(s, DAY, 25200, "CE", D("80.00"))
    with session_scope() as s:
        assert marks.record(s, DAY) == {"paper": 0, "real": 0, "unpriced": 0}


def test_real_legs_keep_an_unknown_direction_unknown(db):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark
    from finresearch.fnobook import marks

    with session_scope() as s:
        bhav(s, DAY, 24500, "PE", D("60.00"))
        bhav(s, DAY, 25500, "CE", D("30.00"))
    legs = [marks.RealLeg("NIFTY26OCT24500PE", "NIFTY", EXPIRY, "put", 24500.0, -75),
            marks.RealLeg("NIFTY26OCT25500CE", "NIFTY", EXPIRY, "call", 25500.0, None)]  # fmt: skip
    with session_scope() as s:
        assert marks.record(s, DAY, real_legs=legs) == {"paper": 0, "real": 2, "unpriced": 0}
    with session_scope() as s:
        rows = {r.leg_key: r for r in s.scalars(select(FnoMark))}
    assert rows["groww:NIFTY26OCT24500PE"].qty == -75
    assert rows["groww:NIFTY26OCT24500PE"].position_key == "NIFTY:2026-10-27"
    assert rows["groww:NIFTY26OCT25500CE"].qty is None and rows["groww:NIFTY26OCT25500CE"].price == D("30.00")


# --------------------------------------------------------------------------- the nightly monitor step
ROWS = {"NIFTY26OCT24500PE": {"instrument_type": "PE", "expiry_date": "2026-10-27", "strike_price": "24500",
                                  "underlying_symbol": "NIFTY", "lot_size": "75"}}  # fmt: skip


class FakeInstruments:
    def contract(self, sym):
        return ROWS.get(sym)


class FakeMarket:
    async def instruments(self):
        return FakeInstruments()


NIGHT = datetime(2026, 10, 20, 20, 0, tzinfo=IST)


def _loaded_day(s):
    from finresearch.db.models import AlertEvalSlot, FnoBhavDay

    s.merge(FnoBhavDay(day=DAY, status="loaded"))
    s.execute(delete(AlertEvalSlot).where(AlertEvalSlot.slot.like("fno_marks:%")))


def test_monitor_step_marks_paper_and_todays_real_positions_once(db):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.db.models import FnoMark
    from finresearch.monitor.fno_marks import fno_marks_step

    with session_scope() as s:
        _loaded_day(s)
        spread(s)
        for k, kind, p in ((25000, "CE", "180"), (25200, "CE", "80"), (24500, "PE", "60")):
            bhav(s, DAY, k, kind, D(p))
    stored = {"status": "ok", "at": "2026-10-20T15:40:00+05:30", "rows": [
        {"symbol": "NIFTY26OCT24500PE", "quantity": -75, "direction_known": True},
        {"symbol": "NIFTY26NOVXXXXX", "quantity": 75, "direction_known": True}]}  # fmt: skip
    out = asyncio.run(fno_marks_step(NIGHT, holidays=set(), market=FakeMarket(), stored=stored))
    assert out["fno_marks"]["paper"] == 2 and out["fno_marks"]["real"] == 1
    assert out["fno_marks"]["real_unresolved"] == 1  # not in the instruments file: skipped, counted
    assert asyncio.run(fno_marks_step(NIGHT, holidays=set(), market=FakeMarket(), stored=stored)) == {}
    with session_scope() as s:
        assert s.scalars(select(FnoMark.qty).where(FnoMark.book == "real")).all() == [-75]


def test_monitor_step_skips_real_positions_not_read_today(db):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.monitor.fno_marks import fno_marks_step

    with session_scope() as s:
        _loaded_day(s)
    stored = {"status": "ok", "at": "2026-10-17T15:40:00+05:30",
              "rows": [{"symbol": "NIFTY26OCT24500PE", "quantity": -75, "direction_known": True}]}  # fmt: skip
    out = asyncio.run(fno_marks_step(NIGHT, holidays=set(), market=FakeMarket(), stored=stored))
    assert out["fno_marks"]["real"] == 0 and "not read today" in out["fno_marks"]["real_note"]


def test_monitor_step_waits_for_the_bhavcopy(db):
    import asyncio

    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay
    from finresearch.monitor.fno_marks import fno_marks_step

    with session_scope() as s:
        s.execute(delete(FnoBhavDay).where(FnoBhavDay.day == DAY))
    assert (
        asyncio.run(fno_marks_step(NIGHT, holidays=set(), market=FakeMarket(), stored={"status": "not_read"}))
        == {}
    )


def test_cli_backfills_paper_marks_from_stored_days(db):
    from typer.testing import CliRunner

    from finresearch.cli import app
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhavDay

    with session_scope() as s:
        s.merge(FnoBhavDay(day=DAY, status="loaded"))
        spread(s)
        bhav(s, DAY, 25000, "CE", D("180.00"))
        bhav(s, DAY, 25200, "CE", D("80.00"))
    r = CliRunner().invoke(app, ["fno", "marks", "--from", "2026-10-19", "--to", "2026-10-20"])
    assert r.exit_code == 0, r.output
    assert "1 loaded days: 2 paper marks written (0 unpriced)" in r.output
    r = CliRunner().invoke(app, ["fno", "review", "--month", "2026-10"])
    assert r.exit_code == 0, r.output
    assert "F&O review (Oct 2026): 0 paper trades closed" in r.output
