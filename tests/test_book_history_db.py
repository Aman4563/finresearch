"""The book-history loader over the stored bhavcopy and IV30 (fnohist.book_history, #394): synthetic fno_bhav and
fno_iv_eod rows in the test database, rolling contracts at different premiums so a return across two contracts
would show."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

START = date(2025, 6, 2)  # a Monday


def _sessions(n: int) -> list[date]:
    out, d = [], START
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _sessions(60)
EXPIRIES = [DAYS[19], DAYS[39], DAYS[59], DAYS[59] + timedelta(days=28)]
X = [0.0] + [0.01 * math.sin(1.3 * i) for i in range(1, 60)]  # NIFTY's log returns
GAP = 0.002  # every open = the same contract's previous close x e^{GAP}


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav, fno_iv_eod"))

    wipe()
    yield env
    wipe()


def _store(symbol: str, base: float, beta: float, ivs: bool = True) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoIvEod

    level = base
    with session_scope() as s:
        for i, d in enumerate(DAYS):
            prev = level
            level *= math.exp(beta * X[i])
            for e in [e for e in EXPIRIES if e >= d][:2]:
                carry = 1 + 0.005 * (EXPIRIES.index(e) % 2)
                s.add(FnoBhav(day=d, symbol=symbol, instrument="IDF", expiry=e, strike=Decimal(0), option_type="XX",
                              open=Decimal(str(round(prev * carry * math.exp(GAP), 2))),
                              close=Decimal(str(round(level * carry, 2))),
                              settle=Decimal(str(round(level * carry, 2)))))  # fmt: skip
            if ivs:
                s.add(
                    FnoIvEod(symbol=symbol, day=d, iv_30d=Decimal(str(12 + i * 0.1)), rate=Decimal("0.065"))
                )


def test_the_loader_reads_returns_within_one_contract_and_iv30(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import book_history as lh

    _store("NIFTY", 24000.0, 1.0)
    _store("BANKNIFTY", 52000.0, 1.3, ivs=False)
    with session_scope() as s:
        h = lh.load(s, ["BANKNIFTY"], DAYS[-1])
    n = h.series["NIFTY"]
    assert sorted(n.returns) == DAYS[1:]
    for i in range(1, 60):
        d0, r = n.returns[DAYS[i]]
        assert d0 == DAYS[i - 1] and r == pytest.approx(X[i], abs=2e-6)  # rounding of the stored settles
    assert n.iv30[DAYS[5]] == pytest.approx(12.5)
    assert h.series["BANKNIFTY"].returns[DAYS[30]][1] == pytest.approx(1.3 * X[30], abs=2e-6)
    assert h.series["BANKNIFTY"].iv30 == {}
    # NIFTY's overnight gaps (same contract) and its close-to-close moves on the front contract's expiry days
    assert len(h.gaps) == 59 and all(g == pytest.approx(GAP, abs=2e-6) for g in h.gaps)
    assert h.expiry_returns == pytest.approx([X[19], X[39], X[59]], abs=2e-6)


def test_nothing_after_today_is_read(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import book_history as lh

    _store("NIFTY", 24000.0, 1.0)
    with session_scope() as s:
        h = lh.load(s, [], DAYS[30])
    assert max(h.series["NIFTY"].returns) == DAYS[30] and max(h.series["NIFTY"].iv30) == DAYS[30]


def test_an_empty_store_has_no_nifty_series(clean):
    from finresearch.db import session_scope
    from finresearch.fnohist import book_history as lh

    with session_scope() as s:
        h = lh.load(s, ["NIFTY"], DAYS[-1])
    assert "NIFTY" not in h.series and h.gaps == [] and h.expiry_returns == []
