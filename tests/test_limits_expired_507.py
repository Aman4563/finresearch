"""#507: `finresearch bridge limits` showed "87% used, resets Mon 18:10" on 23-Nov-2026 from a reading taken on
26-Oct-2026. A stored reading whose window has reset is expired: it is shown with when it was last seen, and the
current usage is unknown (never the old figure, never 0). The router's decisions (`availability`) do not change."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from typer.testing import CliRunner

from finresearch.bridge.limits import LimitTracker, display_state, window_views
from finresearch.bridge.types import RateLimitSnapshot, Tier

SEEN = datetime(2026, 10, 26, 8, 12, tzinfo=UTC).timestamp()  # the reading: Monday 26-Oct-2026
RESET_5H = datetime(2026, 10, 26, 12, 40, tzinfo=UTC).timestamp()  # its 5-hour window ended that evening
RESET_7D = datetime(2026, 10, 29, 3, 30, tzinfo=UTC).timestamp()
NOW = datetime(2026, 11, 23, 6, 0, tzinfo=UTC).timestamp()  # four weeks later
OLD = {"status": "allowed", "five_hour_utilization": 0.87, "five_hour_resets_at": int(RESET_5H),
       "seven_day_utilization": 0.41, "seven_day_resets_at": int(RESET_7D), "observed_at": SEEN}  # fmt: skip


def test_a_window_whose_reset_has_passed_is_expired_with_when_it_was_seen():
    v = window_views(OLD, NOW)
    assert v["five_hour"] == {"state": "expired", "utilization": None, "last_utilization": 0.87,
                              "resets_at": int(RESET_5H), "last_seen": SEEN}  # fmt: skip
    assert v["seven_day"]["state"] == "expired" and v["seven_day"]["utilization"] is None


def test_each_window_is_judged_on_its_own():
    # an hour after the reading: both windows still run; a day later only the 7-day one does
    assert {k: x["state"] for k, x in window_views(OLD, SEEN + 3600).items()} == {
        "five_hour": "current", "seven_day": "current"}  # fmt: skip
    v = window_views(OLD, SEEN + 86400)
    assert (v["five_hour"]["state"], v["five_hour"]["utilization"]) == ("expired", None)
    assert (v["seven_day"]["state"], v["seven_day"]["utilization"]) == ("current", 0.41)


def test_without_a_reset_time_the_age_of_the_reading_decides():
    snap = {"five_hour_utilization": 0.5, "seven_day_utilization": 0.2, "observed_at": SEEN}
    v = window_views(snap, SEEN + 6 * 3600)  # six hours on: a 5-hour window has reset, a 7-day one has not
    assert (v["five_hour"]["state"], v["seven_day"]["state"]) == ("expired", "current")
    # a reading with no time at all (observed_at 0.0) is not from 1970 and is not current: unknown age, expired
    v = window_views({"five_hour_utilization": 0.5, "observed_at": 0.0}, NOW)
    assert v == {"five_hour": {"state": "expired", "utilization": None, "last_utilization": 0.5,
                               "resets_at": None, "last_seen": None}}  # fmt: skip
    assert window_views({"status": "allowed"}, NOW) == {} and window_views(None, NOW) == {}


def test_display_state_blanks_expired_usage_and_keeps_the_stored_state(tmp_path):
    tr = LimitTracker(tmp_path, clock=lambda: NOW)
    tr.record_snapshot(Tier.CLAUDE_MAX, RateLimitSnapshot(**OLD))
    stored = tr.describe()
    shown = display_state(stored, NOW)["claude_max"]["snapshot"]
    assert shown["five_hour_utilization"] is None and shown["seven_day_utilization"] is None
    assert shown["five_hour_expired"] is True and shown["five_hour_last_utilization"] == 0.87
    assert shown["five_hour_resets_at"] == int(RESET_5H) and shown["observed_at"] == SEEN
    assert tr.describe() == stored and stored["claude_max"]["snapshot"]["five_hour_utilization"] == 0.87
    assert (
        tr.snapshot(Tier.CLAUDE_MAX).five_hour_utilization == 0.87
    )  # the budget pre-flight's input: untouched
    assert tr.availability(Tier.CLAUDE_MAX) == (True, None)  # as before: an expired reading never blocks
    # while the window runs, nothing is blanked
    live = display_state(stored, SEEN + 60)["claude_max"]["snapshot"]
    assert live["five_hour_utilization"] == 0.87 and live["five_hour_expired"] is False
    assert "five_hour_last_utilization" not in live


def test_an_undated_reading_is_shown_without_a_date():
    state = {"claude_max": {"snapshot": {"five_hour_utilization": 0.3, "observed_at": 0.0}}}
    shown = display_state(state, NOW)["claude_max"]["snapshot"]
    assert shown["observed_at"] is None and shown["five_hour_expired"] is True


def test_bridge_limits_prints_expired_not_current_usage(env):
    from finresearch.cli import app

    LimitTracker(env.state_dir).record_snapshot(Tier.CLAUDE_MAX, RateLimitSnapshot(**OLD))
    out = CliRunner().invoke(app, ["bridge", "limits"]).output
    seen = time.strftime("%a %d %b %Y %H:%M", time.localtime(SEEN))
    assert "five_hour: expired" in out and f"last seen {seen}: 87% used" in out, out
    assert "current usage unknown" in out
    assert "87% used, resets" not in out  # before #507: "five_hour: 87% used, resets Mon 18:10"


def test_bridge_limits_prints_a_running_window_with_its_date(env):
    from finresearch.cli import app

    soon = int(time.time()) + 3600
    snap = RateLimitSnapshot(status="allowed", five_hour_utilization=0.25, five_hour_resets_at=soon,
                             observed_at=time.time())  # fmt: skip
    LimitTracker(env.state_dir).record_snapshot(Tier.CLAUDE_MAX, snap)
    out = CliRunner().invoke(app, ["bridge", "limits"]).output
    assert f"five_hour: 25% used, resets {time.strftime('%a %d %b %H:%M', time.localtime(soon))}" in out, out
    assert "expired" not in out


def test_api_limits_never_serves_an_expired_reading_as_usage(env):
    from starlette.testclient import TestClient

    from finresearch.api import create_app

    LimitTracker(env.state_dir).record_snapshot(Tier.CLAUDE_MAX, RateLimitSnapshot(**OLD))
    with TestClient(create_app(monitor=False)) as c:
        snap = c.get("/api/limits").json()["tiers"]["claude_max"]["snapshot"]
    # the home page's plan tile and its "% used" line read these two fields
    assert snap["five_hour_utilization"] is None and snap["seven_day_utilization"] is None
    assert snap["five_hour_expired"] is True and snap["five_hour_last_utilization"] == 0.87
    assert snap["seven_day_last_utilization"] == 0.41 and snap["observed_at"] == SEEN
