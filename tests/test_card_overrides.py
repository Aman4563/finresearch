"""The override log (#376 (d), evals/experiments/card_ledger/ADDENDUM.md B1.2): decisions the owner records on logged
cards, append-only, never inferred. Synthetic cards only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

LOGGED = datetime(2026, 10, 23, 5, 0, tzinfo=UTC)


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:  # TRUNCATE is not a row UPDATE/DELETE: the append-only triggers allow it
        s.execute(text("TRUNCATE forecast, card_log, card_override"))
    yield


def _log(actions: list[tuple[str, str | None]]) -> int:
    """A synthetic suggest response with one card per (action, qualifier)."""
    from finresearch.db import session_scope
    from finresearch.fnobook import card_log

    resp = {"symbol": "NIFTY", "cards": [{"title": f"card {i}", "action": a, "qualifier": q, "legs": []}
                                         for i, (a, q) in enumerate(actions)]}  # fmt: skip
    with session_scope() as s:
        r = card_log.row("suggest", symbol="NIFTY", expiry=None, inputs={}, cards=card_log.summarise("suggest", resp),
                         response=resp, created_at=LOGGED)  # fmt: skip
        s.add(r)
        s.flush()
        return r.id


@pytest.mark.parametrize(("action", "qualifier", "want"), [
    ("ENTER", None, "ENTER"), ("PAPER", None, "NO PROVEN EDGE"), ("ENTER", "no proven edge yet", "NO PROVEN EDGE"),
    ("NO PROVEN EDGE", None, "NO PROVEN EDGE"), ("WAIT", None, "WAIT"), ("AVOID", None, "AVOID"),
    (None, None, None), ("SOMETHING", None, None),
])  # fmt: skip
def test_verdict_class_is_unknown_unless_named(action, qualifier, want):
    from finresearch.fnobook import overrides as ov

    assert ov.verdict_class(action, qualifier) == want


@pytest.mark.parametrize(("verdict", "decision", "want"), [
    ("ENTER", "traded", False), ("ENTER", "skipped", True), ("NO PROVEN EDGE", "traded", True),
    ("NO PROVEN EDGE", "skipped", False), ("WAIT", "traded", True), ("AVOID", "skipped", False),
    (None, "traded", None),
])  # fmt: skip
def test_an_override_is_trading_against_or_skipping_an_enter(verdict, decision, want):
    from finresearch.fnobook import overrides as ov

    assert ov.is_override(verdict, decision) is want


def test_record_copies_the_card_class_and_refuses_unknown_cards(clean):
    from finresearch.db import session_scope
    from finresearch.fnobook import overrides as ov

    i = _log([("ENTER", None), ("WAIT", None)])
    with session_scope() as s:
        row = ov.record(s, card_log_id=i, card_index=1, decision="traded", note="  hunch  ")
        assert (row["verdict"], row["decision"], row["override"], row["note"]) == (
            "WAIT",
            "traded",
            True,
            "hunch",
        )
        for bad in ({"card_log_id": i + 99, "card_index": 0}, {"card_log_id": i, "card_index": 2}):
            with pytest.raises(ValueError, match="no logged card"):
                ov.record(s, decision="traded", **bad)
        with pytest.raises(ValueError, match="decision"):
            ov.record(s, card_log_id=i, card_index=0, decision="maybe")


def test_the_log_is_append_only(clean):
    from sqlalchemy.exc import DBAPIError

    from finresearch.db import session_scope
    from finresearch.fnobook import overrides as ov

    i = _log([("ENTER", None)])
    with session_scope() as s:
        ov.record(s, card_log_id=i, card_index=0, decision="traded")
    for sql in ("UPDATE card_override SET decision = 'skipped'", "DELETE FROM card_override"):
        with pytest.raises(DBAPIError, match="append-only"), session_scope() as s:
            s.execute(text(sql))


def test_the_latest_decision_per_card_counts_and_unrecorded_cards_are_not_skipped(clean):
    from finresearch.db import session_scope
    from finresearch.fnobook import overrides as ov

    i = _log([("ENTER", None), ("AVOID", None), ("WAIT", None), (None, None)])
    with session_scope() as s:
        ov.record(s, card_log_id=i, card_index=0, decision="traded")
        ov.record(s, card_log_id=i, card_index=0, decision="skipped")  # changed mind: the latest counts
        ov.record(s, card_log_id=i, card_index=1, decision="skipped")
        ov.record(s, card_log_id=i, card_index=3, decision="traded")  # no verdict: unknown class
        got = ov.latest(s)
        assert [(d["card_index"], d["decision"]) for d in got] == [
            (0, "skipped"),
            (1, "skipped"),
            (3, "traded"),
        ]
        assert len(ov.history(s)) == 4  # every row is kept
        r = ov.rate(got)
        # card 2 (WAIT) has no decision: never counted as skipped; card 3's class is unknown: left out, counted
        assert (r["decisions"], r["overrides"], r["unknown_class"]) == (2, 1, 1)
        assert r["rate"] == pytest.approx(0.5)


def test_rate_is_unknown_with_no_decision():
    from finresearch.fnobook import overrides as ov

    assert ov.rate([])["rate"] is None
