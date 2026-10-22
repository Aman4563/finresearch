"""The opportunity-cost line (#381): each strategy's out-of-sample F&O P&L after tax beside holding NIFTY's approximate
total-return index on the same capital over the same days. Synthetic index rows in the test database."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from finresearch.backtest import benchmark


def test_index_window_chains_the_approximate_tri():
    # closes 100, 101, 102 with a 1.26 % yield (0.005 % a session): TRI 100 -> 100 x (1.01 + 0.00005) = 101.005 ->
    # 101.005 x (102/101 + 0.00005) = 102.0051
    rows = [(date(2024, 7, 1), 100.0, 1.26), (date(2024, 7, 2), 101.0, 1.26), (date(2024, 7, 3), 102.0, 1.26)]
    w = benchmark.index_window(rows, date(2024, 7, 1), date(2024, 7, 3))
    assert w is not None
    assert w[0] == (date(2024, 7, 1), 100.0)
    assert w[1][0] == date(2024, 7, 3) and w[1][1] == pytest.approx(101.005 * (102 / 101 + 0.00005))


def test_index_window_is_unknown_when_the_rows_do_not_cover_it():
    rows = [(date(2024, 7, 15), 100.0, 1.2), (date(2024, 7, 16), 101.0, 1.2)]
    assert benchmark.index_window(rows, date(2024, 7, 1), date(2024, 7, 16)) is None  # starts 14 days late
    assert benchmark.index_window([*rows[:1], (date(2024, 7, 16), 101.0, None)], date(2024, 7, 15),
                                  date(2024, 7, 16)) is None  # an unknown yield breaks the chain  # fmt: skip
    assert benchmark.index_window([], date(2024, 7, 1), date(2024, 7, 2)) is None


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE index_eod"))

    wipe()
    yield env
    wipe()


def _report() -> dict:
    oos = {"from": "2024-07-01", "to": "2024-07-31", "trades": 4, "total_net": 20_000.0}
    return {"book": {"capital": 1_000_000.0},
            "strategies": [{"key": "nifty_pcs_m", "status": "fails", "windows": {"oos": oos}},
                           {"key": "banknifty_ic_m", "status": "no_data"}]}  # fmt: skip


def test_attach_adds_the_line_to_every_strategy_with_an_oos_window(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import IndexEod

    with session_scope() as s:
        for i in range(31):
            d = date(2024, 7, 1) + timedelta(days=i)
            s.add(
                IndexEod(
                    day=d, symbol="NIFTY", index_name="Nifty 50", close=Decimal(100 + i), div_yield=Decimal(0)
                )
            )
    rep = _report()
    with session_scope() as s:
        benchmark.attach(rep, s, slab_rate=Decimal("0.30"))
    o = rep["strategies"][0]["opportunity"]
    # 1-31 Jul 2024: held a month and sold after 23-Jul-2024, so short-term at 20 % plus 4 % cess
    assert o["index_return"] == pytest.approx(0.30) and o["index_tax_rate"] == pytest.approx(0.20)
    assert o["index_after_tax"] == pytest.approx(300_000 - 300_000 * 0.20 * 1.04)
    assert o["fno_after_tax"] == pytest.approx(20_000 - 20_000 * 0.30 * 1.04)
    assert o["benchmark"] == benchmark.BENCHMARK_LABEL
    assert "opportunity" not in rep["strategies"][1]


def test_attach_without_index_rows_is_unknown(clean):
    from finresearch.db import session_scope

    rep = _report()
    with session_scope() as s:
        benchmark.attach(rep, s, slab_rate=Decimal("0.30"))
    o = rep["strategies"][0]["opportunity"]
    assert o["excess_after_tax"] is None and "unknown" in o["reason"]
