"""#492: the data-quality pass (#368/#424) warns when a scheduled MPC decision (event calendar) is not read from RBI's
RSS feed within 2 days, when an observation contradicts the typed table on a day it covers, and when two items give
other rates for one decision (dataset policy_rate, never critical). Test database only; synthetic observations."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from finresearch.eventcal import refresh
from finresearch.fincalc.dates import IST
from finresearch.fincalc.policy_feed import Observation

URL = "https://www.rbi.org.in/Scripts/BS_PressReleaseDisplay.aspx?prid={}"
TITLE = "Monetary Policy Statement, 2026-27 Resolution of the Monetary Policy Committee December 2 to 4, 2026"
DEC4 = date(2026, 12, 4)
SEEN = datetime(2026, 12, 4, 12, 0, tzinfo=IST)
# the decision of 04-Dec-2026 has two days' grace: through 06-Dec-2026; the 08:00 pass of 07-Dec-2026 warns
LAST_QUIET = datetime(2026, 12, 6, 21, 0, tzinfo=IST)
FIRST_WARN = datetime(2026, 12, 7, 8, 0, tzinfo=IST)


def obs(
    prid: int, decided: date, repo: str | None, sdf: str | None = None, msf: str | None = None
) -> Observation:
    def d(x: str | None) -> Decimal | None:
        return None if x is None else Decimal(x)

    return Observation(
        decided=decided, repo=d(repo), sdf=d(sdf), msf=d(msf), title=TITLE, url=URL.format(prid)
    )


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope
    from finresearch.eventcal import calendar as cal
    from finresearch.eventcal import store

    march = datetime(2026, 3, 23, 21, 0, tzinfo=IST)
    with session_scope() as s:
        s.execute(
            text("TRUNCATE policy_rate_obs, calendar_event, calendar_coverage, dq_check, dataset_status")
        )
        # the 2026-27 schedule's meetings after the typed table's review, complete through 31-Mar-2027
        store.upsert(s, [cal.Event(type="rbi_mpc", tier=1, scope="IN", day=d, at=None, title="t",
                                   source_url=URL.format(62422), known_since=march)
                         for d in (DEC4, date(2027, 2, 5))],
                     [cal.Coverage(type="rbi_mpc", scope="IN", through=date(2027, 3, 31),
                                   source_url=URL.format(62422), known_since=march)])  # fmt: skip
    yield env


def _store(now: datetime, *observations: Observation) -> None:
    from finresearch.db import session_scope

    with session_scope() as s:
        refresh.apply(s, refresh.Found("rbi_rss", observations=list(observations)), now)


def _checks(now: datetime) -> dict[str, tuple]:
    from finresearch.db import session_scope
    from finresearch.dq import policy

    with session_scope() as s:
        return {c.check: (c.status, c.value, c.note, c.critical) for c in policy.checks(s, now)}


def test_a_scheduled_decision_not_read_within_two_days_is_a_warning(clean):
    assert _checks(LAST_QUIET)["mpc_decision_read"][:2] == ("ok", 0)
    status, value, note, critical = _checks(FIRST_WARN)["mpc_decision_read"]
    assert (status, value, critical) == ("warn", 1, False) and "04-Dec-2026" in note


def test_the_warning_reaches_the_stored_pass_and_the_flag_stays(clean, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.dq import report, run
    from finresearch.signals import rates

    with session_scope() as s:
        run.run_all(s, FIRST_WARN, holidays=set(), free_bytes=lambda _p: 50 * 2**30)
    with session_scope() as s:
        health = {d["dataset"]: d for d in report.data_health(s)["datasets"]}
        monkeypatch.setattr(rates, "OBSERVED", None)
        answer = rates.rate_on(s, date(2026, 12, 7), 91 / 365)
    row = health["policy_rate"]
    assert row["status"] == "warn" and row["checks"][0]["check"] == "mpc_decision_read"
    assert "RBI" in row["label"] and answer["stale"] is True


def test_a_decision_read_in_time_raises_nothing(clean):
    _store(SEEN, obs(91004, DEC4, "0.0575", "0.055", "0.06"))
    assert {k: v[0] for k, v in _checks(FIRST_WARN).items()} == {
        "mpc_decision_read": "ok", "table_agreement": "ok", "feed_conflicts": "ok"}  # fmt: skip


def test_a_resolution_read_without_its_repo_rate_still_warns_and_says_so(clean):
    _store(SEEN, obs(91004, DEC4, None))  # seen, but no figure could be read: unknown
    status, value, note, _ = _checks(FIRST_WARN)["mpc_decision_read"]
    assert (status, value) == ("warn", 1) and "no repo rate" in note and URL.format(91004) in note


def test_a_contradiction_of_the_typed_table_is_a_warning_and_the_table_is_kept(clean):
    # the typed table has 5.50 % from 07-Oct-2026; an item claiming 5.75 % for that decision is reported, not used
    _store(SEEN, obs(91003, date(2026, 10, 7), "0.0575"), obs(91004, DEC4, "0.0575", "0.055", "0.06"))
    status, value, note, critical = _checks(FIRST_WARN)["table_agreement"]
    assert (status, value, critical) == ("warn", 1, False)
    assert "07-Oct-2026" in note and "5.75 %" in note and "5.50 %" in note


def test_two_items_giving_other_rates_for_one_decision_are_a_warning(clean):
    _store(SEEN, obs(91004, DEC4, "0.0575", "0.055", "0.06"))
    _store(datetime(2026, 12, 4, 16, 30, tzinfo=IST), obs(91012, DEC4, "0.06", "0.0575", "0.0625"))
    status, value, note, _ = _checks(FIRST_WARN)["feed_conflicts"]
    assert (status, value) == ("warn", 1) and URL.format(91012) in note and "6.00 %" in note


def test_past_the_calendars_coverage_a_missed_decision_cannot_be_ruled_out(clean):
    late = datetime(2027, 4, 3, 8, 0, tzinfo=IST)  # 31-Mar-2027 + 2 days' grace has passed, no later schedule
    _store(SEEN, obs(91004, DEC4, "0.0575", "0.055", "0.06"))
    _store(datetime(2027, 2, 5, 12, 0, tzinfo=IST), obs(91006, date(2027, 2, 5), "0.055", "0.0525", "0.0575"))
    status, _, note, _ = _checks(late)["mpc_decision_read"]
    assert status == "unknown" and "31-Mar-2027" in note


def test_a_decision_read_a_day_after_its_calendar_date_counts_for_that_meeting(clean):
    # the meeting moved to 2-5 Dec and the calendar missed the notice: the decision read on 05-Dec-2026 is that
    # meeting's (before the next scheduled one), so nothing is missing; February's is not due yet
    _store(datetime(2026, 12, 5, 12, 0, tzinfo=IST), obs(91004, date(2026, 12, 5), "0.0575", "0.055", "0.06"))
    assert _checks(datetime(2026, 12, 8, 8, 0, tzinfo=IST))["mpc_decision_read"][:2] == ("ok", 0)


def test_a_later_decision_does_not_hide_a_missed_one(clean):
    # February's decision was read but December's never was: the rates between them were not known
    _store(datetime(2027, 2, 5, 12, 0, tzinfo=IST), obs(91006, date(2027, 2, 5), "0.055", "0.0525", "0.0575"))
    status, value, note, _ = _checks(datetime(2027, 2, 9, 8, 0, tzinfo=IST))["mpc_decision_read"]
    assert (status, value) == ("warn", 1) and "04-Dec-2026" in note


# ---- review of #494: the check agrees with the answers' "possibly stale" flag (fincalc.policy_feed.stale)


def test_a_meeting_the_typed_table_covers_is_not_missing(clean, monkeypatch):
    # the 04-Dec-2026 decision typed into the table, and no MPC schedule in the calendar: nothing is missing from
    # the feed; what is not known is the schedule after the table, so the check is unknown, never a false warning
    from finresearch.db import session_scope
    from finresearch.fincalc import policy_rates as P

    typed = P.PolicyRate(DEC4, Decimal("0.0575"), Decimal("0.055"), "SDF", Decimal("0.06"), TITLE, DEC4,
                         URL.format(91004))  # fmt: skip
    monkeypatch.setattr(P, "TABLE", (*P.TABLE, typed))
    monkeypatch.setattr(P, "_DAYS", [r.effective for r in P.TABLE])
    with session_scope() as s:
        s.execute(text("TRUNCATE calendar_event, calendar_coverage"))
    status, _, note, _ = _checks(FIRST_WARN)["mpc_decision_read"]
    assert status == "unknown" and "after 04-Dec-2026" in note


def test_an_off_cycle_decision_does_not_hide_a_missed_meeting(clean):
    # 04-Dec-2026 never read; an unscheduled decision of 20-Dec-2026 was: the rates of 4-19 Dec are not known, which
    # the answers flag as possibly stale, so the check warns too
    from finresearch.db import session_scope
    from finresearch.fincalc import policy_feed as F
    from finresearch.refseries import policy_obs

    _store(
        datetime(2026, 12, 20, 12, 0, tzinfo=IST), obs(91020, date(2026, 12, 20), "0.06", "0.0575", "0.0625")
    )
    status, value, note, _ = _checks(datetime(2026, 12, 25, 8, 0, tzinfo=IST))["mpc_decision_read"]
    assert (status, value) == ("warn", 1) and "04-Dec-2026" in note
    with session_scope() as s:
        observed = policy_obs.observed(s)
    assert F.stale(date(2026, 12, 10), observed) and F.stale(date(2026, 12, 22), observed) is None
