"""P4.5 (#398): the book-level risk readings (alerts.risk_readers.book_readings): limit utilisation against the
common-shock budget and the book loss limit, margin use, and the ES and stressed-margin rules that cannot be
checked until P3.4 / P4.3 exist. Unknown inputs read None with the reason, never 0. Synthetic numbers only."""

from __future__ import annotations

import pytest

from finresearch.alerts.risk_readers import book_readings
from finresearch.fincalc import tradeplan as tp

CAP = 1_000_000.0
SHOCK = {-0.03: -20_000.0, -0.05: -52_000.0, -0.10: -70_000.0, 0.05: 4_000.0}


def _by(rows):
    return {(r, i): rd for r, i, rd in rows}


def test_limit_utilisation_is_the_worst_shock_loss_over_each_budget() -> None:
    got = _by(book_readings(SHOCK, [], CAP, None, "Groww is not connected", book_open=True))
    u = got[("limit_shock", "BOOK")]
    assert u.value == pytest.approx(70_000 / (CAP * tp.SHOCK_BUDGET_PCT / 100))  # 70k / 60k
    assert "-10%" in u.detail and "fincalc.tradeplan.SHOCK_MOVES" in u.series
    assert got[("stress_breach", "BOOK")].value == pytest.approx(70_000 / (CAP * tp.BOOK_BUDGET_PCT / 100))


def test_unknown_shock_or_capital_reads_unknown_never_zero() -> None:
    got = _by(book_readings(None, ["a leg has no mark"], CAP, None, "Groww is not connected", book_open=True))
    assert (
        got[("limit_shock", "BOOK")].value is None
        and "a leg has no mark" in got[("limit_shock", "BOOK")].detail
    )
    got = _by(book_readings(SHOCK, [], 0.0, None, "Groww is not connected", book_open=True))
    assert got[("limit_shock", "BOOK")].value is None and "capital" in got[("limit_shock", "BOOK")].detail


def test_es_and_stressed_margin_are_cant_check_while_a_book_is_open_and_absent_when_it_is_empty() -> None:
    got = _by(book_readings(SHOCK, [], CAP, None, "Groww is not connected", book_open=True))
    assert got[("es", "BOOK")].value is None and "P3.4" in got[("es", "BOOK")].detail
    assert got[("margin_stress", "BOOK")].value is None and "P4.3" in got[("margin_stress", "BOOK")].detail
    empty = _by(
        book_readings(dict.fromkeys(SHOCK, 0.0), [], CAP, None, "Groww is not connected", book_open=False)
    )
    assert ("es", "BOOK") not in empty and ("margin_stress", "BOOK") not in empty
    assert empty[("limit_shock", "BOOK")].value == 0.0  # an empty book, known: exactly 0


def test_margin_use_is_utilisation_over_the_cap_and_unknown_when_funds_are_stale() -> None:
    funds = {"cash": 400_000.0, "margin_used": 150_000.0, "collateral": 200_000.0}
    want = tp.margin_utilisation(150_000.0, 0.0, 400_000.0, 200_000.0) / tp.MARGIN_UTIL_MAX
    got = _by(book_readings(SHOCK, [], CAP, funds, None, book_open=True))
    assert got[("margin", "BOOK")].value == pytest.approx(want)  # max(150/600, 150*0.5/400) / 0.5 = 0.5
    stale = _by(book_readings(SHOCK, [], CAP, None, "Groww's funds were not synced today", book_open=True))
    assert stale[("margin", "BOOK")].value is None
    off = _by(
        book_readings(SHOCK, [], CAP, None, "Groww is not connected: the margin is unknown", book_open=True)
    )
    assert ("margin", "BOOK") not in off  # no real book, so no margin to watch
