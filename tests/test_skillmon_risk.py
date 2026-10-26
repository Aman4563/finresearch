"""The Kupiec risk-model trigger (#378, skill-monitor-v2): Kupiec per calendar quarter of a VaR's logged exceptions,
and "not for limits" after 3 consecutive failing quarters. The Kupiec values are worked by hand from LR =
-2 ln[(1-p)^(n-x) p^x] + 2 ln[(1-x/n)^(n-x) (x/n)^x], p-value erfc(√(LR/2)); a quarter that is untested or still
waiting for an outcome is never a pass."""

from __future__ import annotations

from datetime import date

import pytest
from skillmon_synth import Q1, Q2, Q3, Q4, quarter, store

from finresearch.skillmon import risk

AFTER_Q3 = date(2026, 10, 26)  # Q3 2026 is the last complete quarter


def test_calendar_quarters() -> None:
    assert [risk.quarter_of(d) for d in (date(2026, 3, 31), date(2026, 4, 1), date(2026, 12, 31))] == [
        (2026, 1), (2026, 2), (2026, 4)]  # fmt: skip


@pytest.mark.parametrize(("x", "status", "lr", "p"), [(0, "fail", 6.155195, 0.013103), (3, "pass", 0.0, 1.0),
                                                     (7, "fail", 4.149678, 0.041642), (6, "pass", 2.478506, 0.115412)])  # fmt: skip
def test_each_quarter_is_a_kupiec_test_at_five_percent(x: int, status: str, lr: float, p: float) -> None:
    (q,) = risk.quarters(quarter(Q3, 60, x), AFTER_Q3)
    assert q["quarter"] == "2026-Q3" and q["days"] == 60 and q["exceptions"] == x and q["status"] == status
    assert q["lr"] == pytest.approx(lr, abs=1e-6) and q["p"] == pytest.approx(p, abs=1e-6)


def test_three_failing_quarters_in_a_row_mark_the_model_not_for_limits() -> None:
    rows = quarter(Q1, 60, 0) + quarter(Q2, 60, 7, first_id=100) + quarter(Q3, 60, 0, first_id=200)
    r = risk.evaluate(rows, AFTER_Q3)
    assert r["status"] == "not for limits" and r["run"] == ["2026-Q1", "2026-Q2", "2026-Q3"]
    middle_pass = quarter(Q1, 60, 0) + quarter(Q2, 60, 3, first_id=100) + quarter(Q3, 60, 0, first_id=200)
    assert risk.evaluate(middle_pass, AFTER_Q3)["status"] == "not triggered"


def test_the_mark_stays_after_later_passing_quarters() -> None:
    fails = quarter(date(2025, 1, 1), 60, 0) + quarter(date(2025, 4, 1), 60, 0, first_id=100)
    fails += quarter(date(2025, 7, 1), 60, 0, first_id=200)
    passes = [
        *quarter(Q1, 60, 3, first_id=300),
        *quarter(Q2, 60, 3, first_id=400),
        *quarter(Q3, 60, 3, first_id=500),
    ]
    r = risk.evaluate(fails + passes, AFTER_Q3)
    assert r["status"] == "not for limits" and r["run"] == ["2025-Q1", "2025-Q2", "2025-Q3"]


def test_an_untested_quarter_inside_a_failing_run_is_unknown_never_a_pass() -> None:
    rows = quarter(date(2025, 10, 1), 60, 0) + quarter(Q1, 49, 0, first_id=100)  # 49 days: insufficient data
    rows += quarter(Q2, 60, 0, first_id=200) + quarter(Q3, 60, 0, first_id=300)
    r = risk.evaluate(rows, AFTER_Q3)
    assert [q["status"] for q in r["quarters"]] == ["fail", "insufficient data", "fail", "fail"]
    assert r["status"] == "unknown"


def test_a_quarter_waiting_for_an_outcome_is_pending_and_void_days_do_not_count() -> None:
    rows = (
        quarter(Q1, 60, 0) + quarter(Q2, 60, 0, first_id=100) + quarter(Q3, 60, 0, first_id=200, open_rows=1)
    )
    r = risk.evaluate(rows, AFTER_Q3)
    assert (
        r["quarters"][-1]["status"] == "pending" and r["status"] == "insufficient data"
    )  # 2 tested quarters
    with_pass = quarter(date(2025, 10, 1), 60, 3, first_id=300) + rows
    assert risk.evaluate(with_pass, AFTER_Q3)["status"] == "unknown"  # Q1-Q3 hold no pass; Q3 is pending
    (q,) = risk.quarters(quarter(Q3, 60, 0, void=15), AFTER_Q3)
    assert q["days"] == 45 and q["status"] == "insufficient data" and q["p"] is None


def test_the_current_quarter_never_counts_and_too_few_quarters_are_insufficient() -> None:
    rows = quarter(Q2, 60, 0) + quarter(Q3, 60, 0, first_id=100) + quarter(Q4, 60, 0, first_id=200)
    r = risk.evaluate(rows, AFTER_Q3)  # Q4 2026 has not ended on 26-Oct-2026
    assert [q["quarter"] for q in r["quarters"]] == ["2026-Q2", "2026-Q3"]
    assert r["status"] == "insufficient data" and "2 of the 3" in r["why"]
    assert risk.evaluate([], AFTER_Q3)["status"] == "insufficient data"


def test_one_outcome_per_day_the_newest_row() -> None:
    rows = quarter(Q3, 60, 0)
    rows.append(risk.Obs(rows[0].day, "resolved", 1, 999))  # a later row for the first day: an exception
    (q,) = risk.quarters(rows, AFTER_Q3)
    assert q["days"] == 60 and q["exceptions"] == 1


# --------------------------------------------------------------------------- the VaR ledger (test database only)
@pytest.fixture
def ledger(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    def wipe() -> None:
        with session_scope() as s:
            s.execute(text("TRUNCATE forecast"))

    wipe()
    yield env
    wipe()


def test_the_logged_var_rows_mark_both_var_models_not_for_limits(ledger) -> None:
    from finresearch.db import session_scope

    store(quarter(Q1, 60, 0) + quarter(Q2, 60, 0, first_id=100) + quarter(Q3, 60, 9, first_id=200))
    with session_scope() as s:
        m = risk.marks(s, AFTER_Q3)
    assert set(m) == {"risk_var_ledger", "risk_var"}
    for key in m:
        assert m[key]["status"] == "not for limits" and m[key]["run"] == ["2026-Q1", "2026-Q2", "2026-Q3"]
    assert [q["exceptions"] for q in m["risk_var"]["quarters"]] == [0, 0, 9]


def test_an_unreadable_ledger_row_never_lets_the_trigger_read_not_triggered(ledger) -> None:
    from finresearch.db import session_scope

    store(
        quarter(Q1, 60, 3) + quarter(Q2, 60, 3, first_id=100) + quarter(Q3, 60, 3, first_id=200), drop_as_of=1
    )
    with session_scope() as s:
        m = risk.marks(s, AFTER_Q3)["risk_var_ledger"]
    assert m["status"] == "unknown" and m["unreadable"] == 1 and "no readable day" in m["why"]
    with session_scope() as s:
        assert (
            risk.marks(s, date(2026, 1, 15))["risk_var"]["status"] == "insufficient data"
        )  # nothing complete yet


def test_a_ledger_row_whose_inputs_are_not_an_object_is_unreadable_not_a_crash(ledger) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import Forecast
    from finresearch.portfolio import var_ledger as vl

    store(quarter(Q1, 60, 3) + quarter(Q2, 60, 3, first_id=100) + quarter(Q3, 60, 3, first_id=200))
    with session_scope() as s:
        s.add(Forecast(asset=vl.ASSET, instrument=vl.INSTRUMENT, source="monitor:portfolio", event_kind=vl.EVENT_KIND,
                       event=vl.EVENT, horizon="1 trading day", resolve_on=Q3, probability=0.05, action="RISK",
                       method=vl.METHOD, validation_status="rule_based", inputs=["not", "an", "object"],
                       status="resolved", outcome=1, dedupe_key="malformed"))  # fmt: skip
    with session_scope() as s:
        m = risk.marks(s, AFTER_Q3)["risk_var"]
    assert m["status"] == "unknown" and m["unreadable"] == 1
