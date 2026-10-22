"""The F&O ban-period rule (P1.12, #374; PREREG.md §3), pure: hysteresis per stock in session order, unknown until a
day decides it and after any missing file, and the comparison against NSE's dated notices."""

from __future__ import annotations

from datetime import date, timedelta

from finresearch.optpanel import ban

D0 = date(2026, 3, 2)


def days(n: int) -> list[date]:
    return [D0 + timedelta(days=i) for i in range(n)]


def test_hysteresis_enters_above_95_and_leaves_only_below_80() -> None:
    us = [0.50, 0.90, 0.96, 0.90, 0.81, 0.80, 0.79, 0.85, 0.951]
    got = [s for _, s in ban.states(list(zip(days(len(us)), us, strict=True)))]
    assert got == [False, False, True, True, True, True, False, False, True]


def test_exactly_95_does_not_enter() -> None:
    assert [s for _, s in ban.states([(D0, 0.95)])] == [
        None
    ]  # undecided start: 0.95 is neither > 95 % nor < 80 %
    assert [s for _, s in ban.states([(D0, 0.5), (D0 + timedelta(1), 0.95)])] == [False, False]


def test_unknown_until_decided_and_after_a_missing_file() -> None:
    us = [0.85, 0.90, 0.97, None, 0.90, 0.70, 0.90]
    got = [s for _, s in ban.states(list(zip(days(len(us)), us, strict=True)))]
    # 0.85 and 0.90 cannot say whether the stock was already banned; a missing day forgets the state
    assert got == [None, None, True, None, None, False, False]


def test_states_are_for_the_next_session() -> None:
    assert ban.states([(D0, 0.99)]) == [(D0, True)]  # keyed by the file's day D: the ban applies to D+1


def test_compare_with_a_notice_counts_unknowns_apart() -> None:
    derived = {"AAA": True, "BBB": False, "CCC": None, "DDD": True}
    notice = {"AAA", "BBB", "EEE"}
    got = ban.compare(derived, notice)
    assert got == {"agree": ["AAA"], "missed": ["BBB"], "extra": ["DDD"], "unknown": ["CCC"],
                   "not_in_file": ["EEE"], "match": False}  # fmt: skip
    assert ban.compare({"AAA": True, "BBB": False}, {"AAA"})["match"] is True
