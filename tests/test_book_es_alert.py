"""The risk alert's book ES (#394 -> #398): the nightly pass reads the open book's historical ES97.5 from stored data
only (fnobook.scenarios.stored_es: tonight's marks, the stored futures and IV30) and holds it to the book loss limit.
Before this, the "es" rule always read "can't check" (P3.4 not built). Test database only; synthetic rows."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select, text

from finresearch.alerts.risk_readers import book_readings
from finresearch.fincalc import book_history as bh
from finresearch.fincalc import book_scenarios as bs
from finresearch.fincalc import tradeplan as tp
from finresearch.fincalc.dates import session_clock

DAY, EXP, FAR = date(2026, 10, 21), date(2026, 10, 27), date(2026, 12, 29)
NIGHT = datetime(2026, 10, 21, 15, 30, tzinfo=UTC)  # 21:00 IST
CAP = 1_000_000.0


def _sessions(n: int) -> list[date]:
    out, d = [], DAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


CAL = _sessions(281)  # 280 one-session returns ending tonight
X = [0.0] + [0.012 * math.sin(1.7 * i) for i in range(1, 281)]


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE alert, alert_delivery, alert_rule_state, alert_eval_slot, fno_bhav_day, "
                           "fno_mark, fno_iv_eod, fno_bhav, broker_connection CASCADE"))  # fmt: skip

    wipe()
    yield env
    wipe()


def _store_book() -> None:
    """NIFTY's far future every session (its settles carry X), IV30 15 flat, tonight's same-expiry future and one
    marked short put."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoIvEod, FnoMark

    level = 24000.0
    with session_scope() as s:
        for i, d in enumerate(CAL):
            level *= math.exp(X[i])
            s.add(FnoBhav(day=d, symbol="NIFTY", instrument="IDF", expiry=FAR, strike=D(0), option_type="XX",
                          settle=D(str(round(level, 2))), close=D(str(round(level, 2)))))  # fmt: skip
            s.add(FnoIvEod(symbol="NIFTY", day=d, iv_30d=D("15"), rate=D("0.065")))
        s.add(FnoBhav(day=DAY, symbol="NIFTY", instrument="IDF", expiry=EXP, strike=D(0), option_type="XX",
                      settle=D("24050"), close=D("24050")))  # fmt: skip
        s.add(FnoMark(day=DAY, book="paper", leg_key="paper_leg:1", symbol="NIFTY", expiry=EXP, right="put",
                      strike=D("23800"), qty=-75, price=D("60"), spot=D("24000")))  # fmt: skip


def _want_es() -> float:
    """The same short put scanned over the synthetic history built here (not read back through the loader)."""
    rets = {}
    lv = [24000.0]
    for i in range(1, len(CAL)):
        lv.append(lv[-1] * math.exp(X[i]))
    for i in range(1, len(CAL)):  # the stored settles are rounded to the paisa
        rets[CAL[i]] = (CAL[i - 1], math.log(round(lv[i], 2) / round(lv[i - 1], 2)))
    m = bs.Market(session_clock(DAY), 0.065, {("NIFTY", EXP): 24050.0})
    leg = bs.BookLeg("NIFTY", EXP, "put", 23800.0, -75, 60.0, "settle")
    sc = bh.scan([leg], m, {"NIFTY": bh.Series(rets, dict.fromkeys(CAL, 15.0))}, 1, set())
    return sc.es  # type: ignore[return-value]


def test_stored_es_scans_tonights_marked_book_over_the_stored_history(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.fnobook.scenarios import stored_es

    _store_book()
    with session_scope() as s:
        es, detail = stored_es(s, DAY, holidays=set())
    assert es == pytest.approx(_want_es(), rel=1e-9) and es > 0
    assert "280 start days" in detail and "ES97.5" in detail


def test_stored_es_without_enough_history_is_unknown_with_the_reason(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav
    from finresearch.fnobook.scenarios import stored_es

    _store_book()
    with session_scope() as s:
        s.execute(FnoBhav.__table__.delete().where(FnoBhav.day < CAL[200]))
    with session_scope() as s:
        es, detail = stored_es(s, DAY, holidays=set())
    assert es is None and f"fewer than the {bh.MIN_SCAN_DAYS}" in detail


def test_the_es_reading_is_the_es_over_the_book_loss_limit() -> None:
    got = {(r, i): rd for r, i, rd in book_readings({-0.05: -1.0}, [], CAP, None, "Groww is not connected",
                                                    book_open=True, es=(30_000.0, "ES97.5 detail"))}  # fmt: skip
    rd = got[("es", "BOOK")]
    assert rd.value == pytest.approx(30_000 / (CAP * tp.BOOK_BUDGET_PCT / 100))  # 30k / 80k
    assert "ES97.5 detail" in rd.detail and "P3.4" not in rd.detail
    got = {(r, i): rd for r, i, rd in book_readings({-0.05: -1.0}, [], CAP, None, "Groww is not connected",
                                                    book_open=True, es=(None, "too few start days"))}  # fmt: skip
    assert got[("es", "BOOK")].value is None and got[("es", "BOOK")].detail == "too few start days"
    got = {(r, i): rd for r, i, rd in book_readings({-0.05: -1.0}, [], 0.0, None, "Groww is not connected",
                                                    book_open=True, es=(30_000.0, "x"))}  # fmt: skip
    assert got[("es", "BOOK")].value is None and "capital" in got[("es", "BOOK")].detail


def test_regression_the_nightly_pass_checks_the_book_es_instead_of_cant_check(clean) -> None:
    """Before #394's ES, risk:es was always "unknown" with "not computed yet (P3.4)" while a book was open."""
    from finresearch.db import session_scope
    from finresearch.db.models import AlertEvalSlot, AlertRuleState, FnoBhavDay
    from finresearch.monitor import risk_alerts as job
    from finresearch.suggest import advisor

    _store_book()
    with session_scope() as s:
        s.add(FnoBhavDay(day=DAY, status="loaded"))
        s.add(AlertEvalSlot(slot=f"fno_marks:{DAY}", result={"status": "done", "real_note": None}))
        p = advisor.load_profile(s)
        p.fno_capital_inr = D(int(CAP))
        advisor.save_profile(s, p)
    assert "risk_alerts" in job.risk_step(NIGHT, holidays=set())
    with session_scope() as s:
        st = s.scalars(select(AlertRuleState).where(AlertRuleState.rule_id == "risk:es")).one()
        assert st.status != "unknown", st.note
        assert float(st.value) == pytest.approx(_want_es() / (CAP * tp.BOOK_BUDGET_PCT / 100), abs=1e-6)
