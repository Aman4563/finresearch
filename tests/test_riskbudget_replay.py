"""P4.2 Stage 2 (#395): the one-book replay of the registered sellers. Its uncapped arm is the registered engine run
on one book: with one spec it is backtest.engine.run exactly, and specs on two underlyings each trade as they would
alone while the book's margin has room. Synthetic history only (tests/riskbudget_synth.py)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
import riskbudget_synth as rs

from finresearch.backtest import engine, specs
from finresearch.riskbudget import limits, replay

START, END = date(2022, 3, 1), date(2022, 6, 30)


def seller(key: str) -> specs.Spec:
    return replace(specs.by_key(key), iv_pct_min=None)  # the synthetic IV is flat, so the filter is off here


def before_last(trades: list[dict], last: date) -> list[dict]:
    return [t for t in trades if date.fromisoformat(t["exit_day"]) < last]


@pytest.mark.parametrize("key", ["nifty_pcs_m", "nifty_ic_w"])
def test_one_spec_uncapped_is_the_engines_run(key) -> None:
    h = rs.history(START, END, bank=False)
    spec = seller(key)
    want = engine.run([spec], h, START, END)[key]
    got = replay.replay(h, [spec], START, END, rules=("uncapped",))
    arm, last = got.arms["uncapped"], got.last
    assert len(want["trades"]) >= 3
    assert before_last(arm.trades(), last) == before_last(want["trades"], last)
    assert [e for e in arm.equity if e[0] < last] == [e for e in want["equity"] if e[0] < last]


def test_specs_on_two_underlyings_share_one_book_and_each_trades_as_alone() -> None:
    h = rs.history(START, END)
    pcs, ic = seller("nifty_pcs_m"), seller("banknifty_ic_m")
    got = replay.replay(h, [pcs, ic], START, END, rules=("uncapped",))
    arm = got.arms["uncapped"]
    for sp in (pcs, ic):
        alone = engine.run([sp], h, START, END)[sp.key]["trades"]
        mine = [t for t in arm.trades() if t["spec"] == sp.key]
        assert len(alone) >= 2 and before_last(mine, got.last) == before_last(alone, got.last), sp.key
    # the book's equity is the sum of both specs' P&L on one capital
    e = dict(arm.equity)
    one = {k: dict(engine.run([sp], h, START, END)[sp.key]["equity"]) for k, sp in (("a", pcs), ("b", ic))}
    for d in [d for d in e if d < got.last][::10]:
        assert e[d] == pytest.approx(one["a"][d] + one["b"][d] - specs.BOOK.capital)


def test_the_period_ends_flat_on_its_last_session() -> None:
    h = rs.history(START, END)
    got = replay.replay(h, [seller("nifty_pcs_m"), seller("banknifty_ic_m")], START, END, rules=("uncapped",))
    arm = got.arms["uncapped"]
    assert got.last == max(d for d in h.days["NIFTY"] if d <= END) and not arm.open()
    ends = [t for t in arm.trades() if t["reason"] == "period_end"]
    assert ends and all(t["exit_day"] == got.last.isoformat() for t in ends)
    assert not any(
        t["entry_day"] == got.last.isoformat() for t in arm.trades()
    )  # no entry on the last session
    assert arm.equity[-1] == (
        got.last,
        pytest.approx(specs.BOOK.capital + sum(t["net"] for t in arm.trades())),
    )


# --------------------------------------------------------------------------- the book-level limits
@pytest.fixture(scope="module")
def nifty_book():
    h = rs.history(START, date(2022, 8, 31), bank=False)
    sp = [seller(k) for k in ("nifty_pcs_m", "nifty_ccs_m", "nifty_ic_w")]
    return replay.replay(h, sp, START, date(2022, 8, 31))


def test_the_budgeted_book_never_holds_more_open_risk_than_b_book(nifty_book) -> None:
    u, b = nifty_book.arms["uncapped"], nifty_book.arms["budgeted"]
    assert max(r for _, r in u.risk) > 80_000  # the uncapped book does go beyond 8 % of ₹10 lakh
    assert max(r for _, r in b.risk) <= 80_000
    assert b.refused[limits.BOOK_FULL] > 0 and len(b.entries) < len(u.entries)
    assert all(e["lots"] <= e["trade_lots"] for e in b.entries)
    assert sum(b.cut.values()) == sum(e["lots"] < e["trade_lots"] for e in b.entries)


def test_the_card_rule_waits_for_thirty_closed_uncapped_trades(nifty_book) -> None:
    k, u = nifty_book.arms["card_rule"], nifty_book.arms["uncapped"]
    assert all(len([t for t in u.trades() if t["spec"] == key]) < 30 for key in u.states)
    assert k.entries == [] and k.refused[limits.KELLY_UNKNOWN] > 0


def test_the_month_stop_refuses_entries(monkeypatch) -> None:
    from finresearch.fincalc import tradeplan

    monkeypatch.setattr(
        tradeplan, "MONTH_LOSS_STOP_PCT", 0.001
    )  # any loss of ₹10 or more this month stops it
    h = rs.history(START, END, bank=False)
    got = replay.replay(
        h, [seller("nifty_pcs_m"), seller("nifty_ccs_m")], START, END, rules=("uncapped", "budgeted")
    )
    b = got.arms["budgeted"]
    assert b.refused[limits.MONTH] > 0 and len(b.entries) < len(got.arms["uncapped"].entries)


def test_banknifty_waits_for_its_registered_beta_in_the_budgeted_book() -> None:
    # futures only from 3-Jan-2022, chains from 1-Mar: the beta needs 120 paired returns (the 121st session on)
    h = rs.history(date(2022, 1, 3), date(2022, 8, 31), chains_from=START)
    known = sorted(h.days["NIFTY"])[120]
    got = replay.replay(
        h, [seller("banknifty_ic_m")], START, date(2022, 8, 31), rules=("uncapped", "budgeted")
    )
    u, b = got.arms["uncapped"], got.arms["budgeted"]
    assert any(date.fromisoformat(e["day"]) < known for e in u.entries)
    assert b.refused[limits.SHOCK_UNKNOWN + " (beta)"] > 0
    assert b.entries and all(date.fromisoformat(e["day"]) >= known for e in b.entries)


def test_the_month_base_is_the_previous_months_last_equity() -> None:
    arm = replay.Arm("budgeted", {})
    arm.roll_month(date(2022, 3, 1), 1_000_000.0)
    assert arm.month_base == 1_000_000.0  # nothing recorded yet: the capital
    arm.equity += [(date(2022, 3, 30), 990_000.0), (date(2022, 3, 31), 985_000.0)]
    arm.roll_month(date(2022, 3, 31), 1_000_000.0)
    assert arm.month_base == 1_000_000.0  # same month: unchanged
    arm.roll_month(date(2022, 4, 1), 1_000_000.0)
    assert arm.month_base == 985_000.0
