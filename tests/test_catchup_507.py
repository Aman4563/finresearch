"""Catch-up after an outage (#507): the window arithmetic, pure. Expected days are read off the 2026 calendar by
hand: 1-Sep-2026 is a Tuesday, 9-Oct a Friday, 30-Oct a Friday, 23-Nov a Monday; Friday 2-Oct (Gandhi Jayanti) is an
NSE holiday."""

from __future__ import annotations

from datetime import date

from finresearch.monitor import catchup as cu

TODAY = date(2026, 11, 23)  # a Monday
D = date


def test_an_ordinary_night_keeps_the_jobs_lookback():
    w = cu.window(TODAY, 4, D(2026, 11, 20))
    assert (w.start, w.extended, w.uncovered) == (D(2026, 11, 19), False, None)
    assert w.summary() is None


def test_an_outage_starts_the_day_after_the_last_loaded_day():
    # loaded up to Friday 30-Oct, down for three weeks: the equity job's 4 days would start at 19-Nov
    w = cu.window(TODAY, 4, D(2026, 10, 30))
    assert (w.start, w.extended, w.uncovered) == (D(2026, 10, 31), True, None)
    assert w.summary() == {"from": "2026-10-31", "days": 19}  # 19 days further back than 19-Nov


def test_the_bound_caps_the_window_and_the_rest_is_reported():
    # 23-Nov minus 45 days is 9-Oct. Loaded up to Tuesday 1-Sep: sessions from Wednesday 2-Sep to Thursday 8-Oct are
    # 3 + 5 + 5 + 5 + 5 (28-Sep to 2-Oct) + 4 (5-Oct to 8-Oct) = 27 weekdays, 26 without the 2-Oct holiday
    w = cu.window(TODAY, 4, D(2026, 9, 1), holidays={D(2026, 10, 2)})
    assert w.start == D(2026, 10, 9)
    assert w.uncovered == (D(2026, 9, 2), D(2026, 10, 8), 26)
    assert w.summary() == {"from": "2026-10-09", "days": 41,
                           "uncovered": {"from": "2026-09-02", "to": "2026-10-08", "sessions": 26}}  # fmt: skip
    assert cu.window(TODAY, 4, D(2026, 9, 1), holidays=set()).uncovered == (D(2026, 9, 2), D(2026, 10, 8), 27)


def test_a_gap_of_holidays_and_weekends_beyond_the_bound_is_not_a_gap():
    # last loaded Wednesday 7-Oct; Thursday 8-Oct is the only day before the 9-Oct floor
    assert cu.window(TODAY, 4, D(2026, 10, 7), holidays={D(2026, 10, 8)}).uncovered is None
    # an unknown holiday list never hides a gap: the weekday counts as a session
    assert cu.window(TODAY, 4, D(2026, 10, 7), holidays=None).uncovered == (D(2026, 10, 8), D(2026, 10, 8), 1)
    # bound 42: the floor is Monday 12-Oct; last loaded Friday 9-Oct leaves only Saturday and Sunday
    w = cu.window(TODAY, 4, D(2026, 10, 9), bound=42, holidays=set())
    assert (w.start, w.uncovered) == (D(2026, 10, 12), None)


def test_a_hole_inside_the_bound_moves_the_start_back():
    # today's neighbours are loaded, but the store's plan still wants 3-Nov and 4-Nov. A day older than the bound
    # (30-Sep) or inside the normal window (21-Nov) does not move the start
    missing = [D(2026, 9, 30), D(2026, 11, 3), D(2026, 11, 4), D(2026, 11, 21)]
    w = cu.window(TODAY, 4, D(2026, 11, 20), missing=missing)
    assert (w.start, w.uncovered) == (D(2026, 11, 3), None)


def test_an_empty_store_and_a_fresh_store_never_change_the_lookback():
    assert cu.window(TODAY, 10, None).start == D(2026, 11, 13)  # history is the CLI backfill's job
    assert cu.window(TODAY, 10, TODAY).start == D(2026, 11, 13)
    assert cu.window(TODAY, 10, D(2026, 11, 20)).start == D(2026, 11, 13)  # never shrinks to 21-Nov


def test_a_bound_shorter_than_the_lookback_never_shrinks_it():
    w = cu.window(TODAY, 10, D(2026, 10, 30), bound=3, holidays=set())
    assert w.start == D(2026, 11, 13) and not w.extended
    # everything from Monday 2-Nov to Thursday 12-Nov is then beyond reach: 5 + 4 sessions
    assert w.uncovered == (D(2026, 11, 2), D(2026, 11, 12), 9)


def test_merge_takes_the_file_that_is_further_behind():
    a = cu.window(TODAY, 5, D(2026, 11, 20))
    b = cu.window(TODAY, 5, D(2026, 9, 1), holidays=set())
    m = cu.merge(a, b)
    assert (m.start, m.normal, m.uncovered) == (
        D(2026, 10, 9),
        D(2026, 11, 18),
        (D(2026, 9, 2), D(2026, 10, 8), 27),
    )


def test_sessions_are_weekdays_that_are_not_known_holidays():
    assert cu.is_session(D(2026, 11, 23), set()) and not cu.is_session(
        D(2026, 11, 22), set()
    )  # Monday, Sunday
    assert not cu.is_session(D(2026, 10, 2), {D(2026, 10, 2)}) and cu.is_session(D(2026, 10, 2), None)


def test_the_schedule_names_the_bounds():
    from finresearch.monitor import scheduler
    from finresearch.suggest.profile import WatchWindows

    sj = scheduler.schedule_json(WatchWindows(), running=True)
    assert sj["catch_up"] == {"max_days": 45, "span_max_days": 14}
    assert sj["equity_eod"] == {"after": "19:00", "lookback_days": 4, "requests_per_night": 1}  # unchanged


def test_holes_older_than_the_bound_are_reported_not_fetched():
    # loaded right up to Friday 20-Nov, but the store's plan still wants the week of 14-Sep (older than the 9-Oct
    # floor). Saturday 19-Sep is a day the F&O store would probe: not a session, so not counted
    older = [D(2026, 9, n) for n in (14, 15, 16, 17, 18, 19)]
    w = cu.window(TODAY, 4, D(2026, 11, 20), older=older, holidays=set())
    assert (w.start, w.extended) == (D(2026, 11, 19), False)  # nothing older than the bound is fetched
    assert w.uncovered == (D(2026, 9, 14), D(2026, 9, 18), 5)
    assert w.summary() == {"from": "2026-11-19", "days": 0,
                           "uncovered": {"from": "2026-09-14", "to": "2026-09-18", "sessions": 5}}  # fmt: skip
    # with a continuous gap as well (loaded to 1-Sep): one range, a day named twice counted once
    both = cu.window(TODAY, 4, D(2026, 9, 1), older=[D(2026, 9, 2), D(2026, 9, 3)], holidays=set())
    assert both.uncovered == (D(2026, 9, 2), D(2026, 10, 8), 27)
    # a day inside the bound passed as `older` by mistake is not reported as out of reach
    assert cu.window(TODAY, 4, D(2026, 11, 20), older=[D(2026, 10, 12)], holidays=set()).uncovered is None
