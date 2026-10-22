"""One session's 30-day model-free variance (P3.6, #389; vrp/measure.py): monthly expiries, India VIX's roll, the
two-term combination on a flat-volatility chain and market-settle filtering."""

import math
from datetime import date, timedelta

import pytest

from finresearch.fincalc import black76 as b76
from finresearch.fincalc import mfiv
from finresearch.vrp import measure as me

E = [
    date(2024, 1, 4),
    date(2024, 1, 11),
    date(2024, 1, 25),
    date(2024, 2, 1),
    date(2024, 2, 29),
    date(2024, 3, 28),
]
SESSIONS = [
    date(2024, 1, 1) + timedelta(i) for i in range(120) if (date(2024, 1, 1) + timedelta(i)).weekday() < 5
]


def test_monthly_expiries_and_the_roll() -> None:
    m = me.monthly_expiries(E)
    assert m == [date(2024, 1, 25), date(2024, 2, 29), date(2024, 3, 28)]
    assert me.terms(date(2024, 1, 10), m, SESSIONS) == (date(2024, 1, 25), date(2024, 2, 29))
    # 22, 23, 24, 25 Jan: four sessions left on the 19th -> no roll; three on the 22nd -> rolled
    assert me.terms(date(2024, 1, 19), m, SESSIONS) == (date(2024, 1, 25), date(2024, 2, 29))
    assert me.terms(date(2024, 1, 22), m, SESSIONS) == (date(2024, 2, 29), date(2024, 3, 28))
    assert me.terms(date(2024, 1, 25), m, SESSIONS) == (date(2024, 2, 29), date(2024, 3, 28))  # expiry day
    assert me.terms(date(2024, 2, 29), m, SESSIONS) is None  # one monthly left


def _chain(f, t, sig, rate=0.065):
    df = math.exp(-rate * t)
    return [mfiv.Strike(k, b76.price("call", f, k, t, df, sig), b76.price("put", f, k, t, df, sig))
            for k in range(14_000, 30_001, 50)]  # fmt: skip


def test_flat_chain_gives_sigma_squared_at_thirty_days() -> None:
    d = date(2024, 1, 25)
    pair = (date(2024, 2, 29), date(2024, 3, 28))
    strikes = {e: _chain(21_000.0, (e - d).days / 365, 0.15) for e in pair}
    out = me.mf30(d, pair, strikes, {e: 21_000.0 for e in pair})
    assert out.value == pytest.approx(0.15**2, rel=3e-3)
    assert out.near.t == pytest.approx(35 * 1440 / 525_600)
    miss = me.mf30(d, pair, {pair[0]: strikes[pair[0]]}, {})
    assert miss.value is None and miss.reason.startswith("next term 2024-03-28")


def test_strikes_keep_listed_rows_but_price_only_market_settles() -> None:
    rows = [(100.0, "call", 5.0, 5.0, 50), (100.0, "put", 4.0, 3.0, 50), (105.0, "call", 2.0, 2.0, 3)]
    out = me.strikes_from_settles(rows)
    assert out == [mfiv.Strike(100.0, 5.0, None), mfiv.Strike(105.0, None, None)]
