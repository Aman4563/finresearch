"""P4.2 Stage 2 (#395): the registered statistics of the seen-period study: calendar-month P&L paths, the worst month,
the drawdown, the moving-block bootstrap over months, the size-matched control and the trades-taken test. Expected
values are hand arithmetic (or a probability worked out by hand for the bootstrap)."""

from __future__ import annotations

from datetime import date

import pytest

from finresearch.riskbudget import stats

C = 1_000_000.0
EQ = [(date(2022, 1, 3), 1_010_000.0), (date(2022, 1, 31), 1_005_000.0), (date(2022, 2, 1), 1_020_000.0),
      (date(2022, 2, 28), 1_000_000.0)]  # fmt: skip


def test_months_keep_each_calendar_months_daily_path() -> None:
    assert stats.months(EQ, C) == [("2022-01", [10_000.0, -5_000.0]), ("2022-02", [15_000.0, -20_000.0])]
    assert stats.worst_month(stats.months(EQ, C)) == -5_000.0  # January +5,000, February −5,000


def test_the_drawdown_runs_from_the_peak_and_the_capital_is_the_first_peak() -> None:
    # cumulated 10,000, 5,000, 20,000, 0 → the fall from 20,000 to 0
    assert stats.max_drawdown([10_000.0, -5_000.0, 15_000.0, -20_000.0]) == 20_000.0
    assert stats.max_drawdown([-3_000.0, 1_000.0]) == 3_000.0  # below the starting capital at once
    assert stats.max_drawdown([1.0, 2.0]) == 0.0


def test_moving_blocks_are_three_consecutive_months_from_any_full_start() -> None:
    idx = stats.block_indices(5, 3, 200, 395)  # ceil(5 / 3) = 2 blocks, cut to 5 months
    assert idx.shape == (200, 5)
    for row in idx.tolist():
        a, b = row[0], row[3]
        assert row[:3] == [a, a + 1, a + 2] and row[3:] == [b, b + 1] and 0 <= a <= 2 and 0 <= b <= 2
    assert {r[0] for r in idx.tolist()} == {0, 1, 2}  # every start is drawn


def flat(n: int) -> list[tuple[str, list[float]]]:
    return [(f"m{i:02d}", [0.0, 0.0]) for i in range(n)]


@pytest.mark.parametrize(("bad", "p"), [(5, 0.7**4), (0, 0.9**4)])
def test_the_worst_month_p_is_the_chance_the_bad_month_is_not_drawn(bad, p) -> None:
    # 12 months, starts 0..9, 4 blocks a draw. B never loses; the control loses ₹1,000 in month `bad` alone, so
    # ΔW = W_b − W_c > 0 exactly when that month is drawn. Month 5 is covered by starts 3, 4, 5 (3 of 10); month 0
    # only by start 0: p = (1 − 3/10)^4 = 0.2401 and (1 − 1/10)^4 = 0.6561.
    c = flat(12)
    c[bad] = (c[bad][0], [-1_000.0, 0.0])
    got = stats.primary(flat(12), c)
    assert got["worst_month"]["p"] == pytest.approx(p, abs=0.025)
    # D_c = ₹1,000 exactly when the bad month is drawn, even as the first month (the capital is the first peak)
    assert got["max_drawdown"]["p"] == got["worst_month"]["p"]
    assert got["worst_month"]["control"] == -1_000.0 and got["worst_month"]["budgeted"] == 0.0
    assert got["passes"] is False


def test_both_tails_must_be_lower_for_the_primary_to_pass() -> None:
    dip = [(f"m{i:02d}", [-100.0, 100.0]) for i in range(12)]  # every month dips ₹100 and recovers: D_c = 100
    got = stats.primary(flat(12), dip)
    assert got["max_drawdown"]["p"] == 0.0 and got["max_drawdown"]["passes"] is True
    assert got["worst_month"]["p"] == 1.0 and got["passes"] is False  # every month nets 0 in both books
    worse = [(f"m{i:02d}", [-100.0, 50.0]) for i in range(12)]  # every month nets −50 and dips
    both = stats.primary(flat(12), worse)
    assert both["worst_month"]["p"] == 0.0 and both["max_drawdown"]["p"] == 0.0 and both["passes"] is True


def test_identical_books_never_pass_and_too_few_months_are_unknown() -> None:
    same = [(f"m{i:02d}", [-100.0, 30.0]) for i in range(12)]
    got = stats.primary(same, same)
    assert got["worst_month"]["p"] == 1.0 and got["max_drawdown"]["p"] == 1.0 and got["passes"] is False
    short = stats.primary(flat(11), flat(11))
    assert short["passes"] is False and short["worst_month"]["p"] is None and "11" in short["reason"]


def test_the_control_is_the_uncapped_book_scaled_to_the_budgeted_books_mean_open_risk() -> None:
    days = [date(2022, 3, d) for d in (1, 2, 3, 4)]
    k = stats.control_k(list(zip(days, [0.0, 40_000.0, 40_000.0, 0.0], strict=True)),
                        list(zip(days, [0.0, 80_000.0, 80_000.0, 0.0], strict=True)))  # fmt: skip
    assert k == 0.5  # mean 20,000 / mean 40,000
    assert stats.scaled([(days[0], 1_010_000.0), (days[1], 980_000.0)], 0.5, C) == [
        (days[0], 1_005_000.0), (days[1], 990_000.0)]  # fmt: skip
    assert stats.control_k([(days[0], 0.0)], [(days[0], 0.0)]) is None  # nothing ever open: unknown
    assert stats.control_k([(days[0], None)], [(days[0], 10.0)]) is None  # an unknown open risk: unknown


@pytest.mark.parametrize(("b", "u", "fall", "passes"), [(7, 10, 0.3, True), (6, 10, 0.4, True),
                                                        (5, 10, 0.5, False), (3, 0, None, False)])  # fmt: skip
def test_trades_taken_may_fall_by_at_most_forty_percent(b, u, fall, passes) -> None:
    got = stats.secondary(b, u)
    assert got["passes"] is passes and (
        got["fall"] == pytest.approx(fall) if fall is not None else got["fall"] is None
    )
