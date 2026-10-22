"""The beta reader over stored futures settles (fnohist.betas, #394, #395): synthetic fno_bhav rows in the test
database, rolling monthly contracts, the registered spec."""

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


DAYS = _sessions(261)  # 260 returns: the window holds the last 250
EXPIRIES = [DAYS[i] for i in range(19, 261, 20)] + [DAYS[-1] + timedelta(days=28), DAYS[-1] + timedelta(days=56)]
X = [0.012 * math.sin(1.7 * i) + 0.004 * math.cos(0.3 * i) for i in range(len(DAYS))]  # NIFTY's log returns


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav"))

    wipe()
    yield env
    wipe()


def _store(symbol: str, base: float, beta: float, skip: set[date] = frozenset()) -> None:
    """Front and next contract settles each session, each contract at its own constant premium to the level (so a
    return across two contracts would show a jump), so every within-contract log return is beta x NIFTY's."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav

    level = base
    with session_scope() as s:
        for i, d in enumerate(DAYS):
            level *= math.exp(beta * X[i]) if i else 1.0
            if d in skip:
                continue
            live = [e for e in EXPIRIES if e >= d][:2]
            for e in live:
                carry = 1 + 0.004 * (EXPIRIES.index(e) % 2)
                s.add(FnoBhav(day=d, symbol=symbol, instrument="IDF", expiry=e, strike=Decimal(0), option_type="XX",
                              settle=Decimal(f"{level * carry:.2f}")))  # fmt: skip


def test_the_stored_series_give_the_registered_beta_with_its_window(clean):
    from finresearch.fnohist import betas

    _store("NIFTY", 24000.0, 1.0)
    _store("BANKNIFTY", 52000.0, 1.2)
    got = betas.load(["banknifty", "NIFTY"], DAYS[-1])
    assert set(got) == {"BANKNIFTY"}
    e = got["BANKNIFTY"]
    assert e.known and e.beta == pytest.approx(1.2, abs=2e-4) and e.se < 1e-3
    assert e.n == 250 and e.missing == 0 and e.as_of == DAYS[-1] and e.window_start == DAYS[-250]
    assert e.hi == pytest.approx(max(1.0, e.beta + 1.645 * e.se)) and e.half_life == 63


def test_a_low_beta_stock_is_shocked_at_least_like_nifty(clean):
    from finresearch.fnohist import betas

    _store("NIFTY", 24000.0, 1.0)
    _store("ITC", 400.0, 0.5)
    e = betas.load(["ITC"], DAYS[-1])["ITC"]
    assert e.beta == pytest.approx(0.5, abs=2e-3) and e.hi == 1.0 and e.lo == pytest.approx(e.beta - 1.645 * e.se)


@pytest.mark.parametrize(
    ("symbol", "skip", "today", "needle"),
    [
        ("NEWSTOCK", set(DAYS[:200]), DAYS[-1], "only 60 paired returns (needs 120)"),
        ("BANKNIFTY", {DAYS[-2]}, DAYS[-1], "missing in the last 5 sessions"),
        ("BANKNIFTY", set(), DAYS[-1] + timedelta(days=10), "stale"),
    ],
)
def test_short_gapped_or_stale_history_leaves_beta_unknown(clean, symbol, skip, today, needle):
    from finresearch.fnohist import betas

    _store("NIFTY", 24000.0, 1.0)
    _store(symbol, 1000.0, 1.1, skip)
    e = betas.load([symbol], today)[symbol]
    assert not e.known and e.beta is None and needle in e.reason


def test_no_nifty_series_leaves_every_beta_unknown(clean):
    from finresearch.fnohist import betas

    _store("BANKNIFTY", 52000.0, 1.2)
    e = betas.load(["BANKNIFTY"], DAYS[-1])["BANKNIFTY"]
    assert not e.known and "NIFTY" in e.reason
