"""#379: a backtest run older than the evidence max age, or from before a market regime change, caps the trade-plan
card and the signal card's Strategy check at WAIT with the reason (never ENTER, never real lots)."""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from test_fno_plan_api import (
    FUNDED_BOOK,
    IC,
    _ic_row,
    _iv_points,
    _no_network,
    _profile,
    _save_backtest,
    _signal,
    client,
)
from test_fno_signal import NOW

from finresearch.fincalc import dates

__all__ = ["_no_network", "client"]  # fixtures used by name

CANDIDATE = _ic_row(status="candidate", expectancy=3000.0, t_stat=2.6, ci_low=400.0, ci_high=5000.0)
HARMFUL = _ic_row(expectancy=-3000.0, t_stat=-2.4, ci_low=-5000.0, ci_high=-200.0)


def _date_runs(created_at: datetime) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("UPDATE fno_backtest_run SET created_at = :c"), {"c": created_at})


def _cards(client, monkeypatch, row, created_at):
    from finresearch.signals import tradeplan as tpl

    async def known(today):
        return FUNDED_BOOK  # #395: real money also needs the real book's funds known

    monkeypatch.setattr(tpl, "load_book", known)
    _iv_points(monkeypatch, high=True)
    _profile("1000000")
    _save_backtest([row])
    _date_runs(created_at)
    card = client.post("/api/fno/plan", json={"symbol": "NIFTY", "expiry": "2026-10-06", "legs": IC}).json()
    return card, _signal(client, monkeypatch, IC)


def test_a_fresh_candidate_run_still_enters(client, monkeypatch):
    card, s = _cards(
        client, monkeypatch, CANDIDATE, datetime(2026, 9, 20, 18, tzinfo=dates.IST)
    )  # 9 days old
    assert card["action"] == "ENTER" and s["action"] == "ENTER" and not card["evidence"]["stale"]
    # saved without provenance: shown as unknown (informational, it does not change the verdict)
    assert "provenance unknown" in card["evidence"]["code_changed"]


def test_a_run_with_the_current_code_hash_reads_unchanged_and_another_reads_changed():
    from finresearch.inventory import provenance as pv
    from finresearch.signals import tradeplan as tpl

    head = {"created_at": "2026-09-20T12:00:00+05:30", "code_hash": pv.code_hash(pv.REPO)}
    assert (
        tpl.evidence_for("iron_condor", "NIFTY", 7, [CANDIDATE], head, today=NOW.date())["code_changed"]
        is None
    )
    ev = tpl.evidence_for(
        "iron_condor", "NIFTY", 7, [CANDIDATE], {**head, "code_hash": "0" * 64}, today=NOW.date()
    )
    assert "code changed since" in ev["code_changed"] and ev["level"] == "proven"


@pytest.mark.parametrize(
    ("created_at", "why"),
    [
        (datetime(2026, 8, 1, 18, tzinfo=dates.IST), "59 days old"),  # 29-Sep − 1-Aug = 59 days > 45
        (
            datetime(2026, 3, 30, 18, tzinfo=dates.IST),
            "01-Apr-2026",
        ),  # before the Apr-2026 STT change (and old)
    ],
)
def test_a_stale_candidate_run_caps_both_cards_at_wait_with_the_reason(client, monkeypatch, created_at, why):
    card, s = _cards(client, monkeypatch, CANDIDATE, created_at)
    assert card["action"] == "WAIT" and s["action"] == "WAIT"
    assert card["size"]["real_lots"] == 0 and s["sizing"]["real_lots"] == 0
    assert "Backtest evidence is current" in card["failed"]
    assert any(why in r for r in card["reasons"]) and any(why in r for r in card["evidence"]["stale"])
    assert card["evidence"]["level"] == "none" and card["evidence"]["raw_level"] == "proven"
    assert any(why in c for c in s["caveats"])


def test_stale_harmful_evidence_still_avoids(client, monkeypatch):
    # staleness never relaxes a harmful match: AVOID stays AVOID
    card, s = _cards(client, monkeypatch, HARMFUL, datetime(2026, 8, 1, 18, tzinfo=dates.IST))
    assert card["action"] == "AVOID" and s["action"] == "AVOID"


def test_now_is_the_clock_of_the_test(client):
    assert dates.now_ist() == NOW  # the cards' "today" is 29-Sep-2026


def test_a_run_of_unknown_date_is_stale_not_proven():
    from finresearch.signals import tradeplan as tpl

    ev = tpl.evidence_for("iron_condor", "NIFTY", 7, [CANDIDATE], None, today=NOW.date())
    assert ev["match"] == "exact" and ev["raw_level"] == "proven" and ev["level"] == "none"
    assert ev["stale"] and "unknown" in ev["stale"][0] and ev["text"].startswith("Stale evidence")
    fresh = tpl.evidence_for("iron_condor", "NIFTY", 7, [CANDIDATE], {"created_at": "2026-09-20T12:00:00+05:30"},
                             today=NOW.date())  # fmt: skip
    assert fresh["level"] == "proven" and fresh["stale"] == []
