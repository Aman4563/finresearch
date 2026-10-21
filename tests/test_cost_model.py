"""One cost model for the backtest and the paper book (#382 (d), #363): the registered slippage tiers in one place,
and the paper book's default slippage from the recorded quotes' measured median half-spread once enough sessions
exist (else the labelled 0.5 % assumption). Synthetic quotes only; medians worked out by hand."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from finresearch.backtest import costs, pricing, specs


@pytest.mark.parametrize(
    ("contracts", "frac"),
    [(5000, 0.01), (1000, 0.01), (999, 0.02), (10, 0.04), (1, 0.08), (0, 0.15), (None, 0.15)],
)
def test_tier_fraction_is_the_registered_table(contracts, frac):
    assert (
        costs.tier_fraction(contracts) == frac
    )  # specs.Book.slippage_tiers; unknown volume = the widest tier


def test_backtest_and_trade_plan_use_the_same_tiers():
    from types import SimpleNamespace

    from finresearch.signals.fno import _half_spread

    assert pricing.slippage(100.0, 150, specs.BOOK) == costs.slippage(100.0, 150, specs.BOOK) == 2.0
    q = SimpleNamespace(bid=None, ask=None, volume=150)
    assert _half_spread(q, None, 100.0) == (100.0, 2.0, "slippage tiers (no live bid/ask)")
    assert (
        _half_spread(SimpleNamespace(bid=None, ask=None, volume=None), None, 1.0)[1] == 0.15
    )  # widest, >= 0.10


def test_measured_median_needs_enough_sessions():
    d0 = date(2026, 7, 1)
    rows = [(d0 + timedelta(days=i), r) for i in range(costs.MIN_MEASURED_SESSIONS - 1) for r in (0.01, 0.03)]
    assert costs.measured_half_spread(rows) is None  # 39 sessions: not enough, never a guess
    rows += [(d0 + timedelta(days=60), 0.02), (d0 + timedelta(days=60), 0.05)]
    m = costs.measured_half_spread(rows)
    # 80 values: 39 x 0.01, 39 x 0.03, 0.02, 0.05 -> sorted, the 40th and 41st are 0.02 and 0.03: median 0.025
    assert m is not None and m.median_pct == pytest.approx(2.5) and m.rows == 80
    assert m.sessions == costs.MIN_MEASURED_SESSIONS and m.first == d0 and m.last == d0 + timedelta(days=60)


def quote_row(at: datetime, bid: int | None, ask: int | None, flags: int = 0) -> dict:
    return dict(snapshot_at=at, requested_at=at, expiry=date(2026, 12, 31), strike=2_500_000, bid=bid, ask=ask,
                flags=flags, underlying="NIFTY", option_type="CE", source="groww")  # fmt: skip


def test_paper_default_slippage_from_the_recorded_quotes(env):
    from finresearch.db import session_scope
    from finresearch.fnohist import quotes as fq

    with session_scope() as s:
        s.execute(text("TRUNCATE option_quote"))
        assert costs.paper_default(s, date(2026, 10, 20)) == (0.5, costs.ASSUMED_SOURCE)
    t0 = datetime(2026, 7, 1, 9, 45, tzinfo=UTC)
    rows = []
    for i in range(costs.MIN_MEASURED_SESSIONS):
        at = t0 + timedelta(days=i)
        rows += [quote_row(at, 9_900, 10_100),  # mid 100, half-spread 1: 1 %
                 quote_row(at + timedelta(minutes=1), 9_700, 10_300),  # 3 %
                 quote_row(at + timedelta(minutes=2), None, 10_000, fq.NO_BID),  # no mid: left out
                 quote_row(at + timedelta(minutes=3), 10_000, 10_000, fq.LOCKED)]  # fmt: skip
    fq.store(rows)
    with session_scope() as s:
        pct, src = costs.paper_default(s, date(2026, 10, 20))
        assert pct == pytest.approx(2.0)  # the median of 40 x 1 % and 40 x 3 %
        assert "measured" in src and "40 sessions" in src
        s.execute(text("TRUNCATE option_quote"))
