"""#239: one canonical value history. The reconstructed series (portfolio.history) is canonical for past days and is
stored for the synchronous readers (portfolio.series); snapshots are the "as shown" record, reconciled against it.
Synthetic numbers only; every expected figure is worked out in the comment next to it."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

import pytest

from finresearch.portfolio.series import Series, Snap, reconcile

IST = timezone(timedelta(hours=5, minutes=30))
FRI, MON, TUE, WED = date(2026, 9, 25), date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)


def at(d: date, hh: int) -> datetime:
    return datetime(d.year, d.month, d.day, hh, 0, tzinfo=IST)


def ser(**kw) -> Series:
    base = {"days": [FRI, MON, TUE, WED], "value": [100_000.0, 100_000.0, 100_000.0, 100_000.0],
            "invested": [100_000.0] * 4, "complete": [True] * 4}  # fmt: skip
    return Series(**{**base, **kw})


def one(s: Series, snap: Snap, added=(), today=WED) -> dict:
    out = reconcile(s, [snap], list(added), today)
    assert out["checked"] == 1
    return out


# --------------------------------------------------------------------------- reconcile: tolerance and reasons
def test_inside_the_tolerance_is_not_reported():
    # 1,00,900 vs 1,00,000: ₹900 (above the ₹100 floor) but 0.9 % (inside 1 %) -> fine
    assert one(ser(), Snap(TUE, 100_900.0, True, at(TUE, 16)))["differ"] == []
    # a tiny portfolio: ₹1,000 vs ₹1,050 is 5 % but only ₹50 -> fine
    s = ser(value=[1000.0] * 4)
    assert one(s, Snap(TUE, 1050.0, True, at(TUE, 16)))["differ"] == []


@pytest.mark.parametrize(("snap", "series_kw", "added", "reason"), [
    # incomplete snapshot (unpriced / stale statement price / unknown cost) first
    (Snap(TUE, 90_000.0, False, at(TUE, 16)), {}, [], "the snapshot was incomplete"),
    # the reconstruction itself forward-filled an old price that day
    (Snap(TUE, 90_000.0, True, at(TUE, 16)), {"complete": [True, True, False, True]}, [],
     "the reconstruction used a price more than 10 days old"),
    # a holding the reconstruction cannot price
    (Snap(TUE, 90_000.0, True, at(TUE, 16)), {"excluded": ["Unlisted Co (Demat): no price history source"]}, [],
     "the reconstruction leaves out holdings without a price history: Unlisted Co"),
    # a trade dated Monday entered on Wednesday, after Tuesday's snapshot
    (Snap(TUE, 90_000.0, True, at(TUE, 16)), {}, [(MON, at(WED, 10))],
     "1 transaction(s) dated on or before this day were added after the snapshot"),
    # written at 11:00 IST on its own day: last traded prices
    (Snap(TUE, 90_000.0, True, at(TUE, 11)), {}, [], "the snapshot was taken during the session"),
    # Saturday: compared with Friday's close
    (Snap(date(2026, 9, 26), 90_000.0, True, at(date(2026, 9, 26), 12)), {}, [],
     "no prices on 2026-09-26 (not a trading day): compared with the close of 2026-09-25"),
    (Snap(TUE, 90_000.0, True, at(TUE, 16)), {}, [], "the price basis differs"),
])  # fmt: skip
def test_each_difference_carries_its_reason(snap, series_kw, added, reason):
    out = one(ser(**series_kw), snap, added)
    (d,) = out["differ"]
    # 90,000 vs 1,00,000: -₹10,000, -10 %
    assert (d["snapshot"], d["reconstructed"], d["diff"], d["diff_pct"]) == (
        90_000.0,
        100_000.0,
        -10_000.0,
        -10.0,
    )
    assert d["reason"].startswith(reason)
    assert out["ok"] is False and out["canonical"].startswith("reconstructed")


def test_days_outside_the_series_and_an_unbuilt_today_are_not_compared():
    s = ser()
    snaps = [Snap(date(2026, 9, 24), 1.0, True, None), Snap(date(2026, 10, 1), 1.0, True, None)]
    # Thursday before the series; Thursday after it (today, the series ends Wednesday) -> neither compared
    assert reconcile(s, snaps, [], date(2026, 10, 1))["checked"] == 0


def test_series_rows_and_value_on():
    s = ser(value=[1.0, 2.0, 3.0, 4.0], complete=[True, False, True, True])
    assert s.rows() == [
        (FRI, 1.0, 100_000.0),
        (TUE, 3.0, 100_000.0),
        (WED, 4.0, 100_000.0),
    ]  # complete days only
    assert s.rows(TUE) == [(TUE, 3.0, 100_000.0), (WED, 4.0, 100_000.0)]
    assert s.value_on(date(2026, 9, 27)) == (1.0, True, FRI)  # Sunday -> Friday's close
    assert s.value_on(MON) == (2.0, False, MON)
    assert s.value_on(date(2026, 9, 24)) is None


# --------------------------------------------------------------------------- stored series (DB)
@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, "
                       "portfolio_import, portfolio_snapshot, portfolio_setting, wealth_asset, wealth_valuation, "
                       "wealth_loan, wealth_goal, wealth_policy CASCADE"))  # fmt: skip
    return env


def _buy(s, day: date):
    from finresearch.portfolio.service import manual_txn

    manual_txn(s, {"asset_type": "stock", "name": "Example Delta Ltd", "isin": "INE000D01019", "account": "Demat",
                   "day": day, "kind": "buy", "quantity": D(10), "price": D(100)})  # fmt: skip


def _store(s, days, values):
    from finresearch.portfolio import series
    from finresearch.portfolio.history import History

    h = History(days=days, value=values, invested=[1000.0] * len(days), complete=[True] * len(days),
                start_reason=f"first transaction on {days[0]}")  # fmt: skip
    assert series.save(s, h, series.current_fingerprint(s), WED)


def test_a_stored_series_goes_out_of_date_when_the_transactions_change(db):
    from finresearch.db import session_scope
    from finresearch.portfolio import series

    with session_scope() as s:
        _buy(s, date(2026, 8, 3))
        _store(s, [date(2026, 8, 3), date(2026, 8, 31)], [1000.0, 1100.0])
    with session_scope() as s:
        got, why = series.load(s)
        assert why is None and got.value == [1000.0, 1100.0]
        _buy(s, date(2026, 8, 10))  # a backdated trade: every later day's value changes
    with session_scope() as s:
        got, why = series.load(s)
    assert got is None and "out of date" in why  # never silently the old numbers


def test_wealth_history_reads_past_months_from_the_series(db):
    from finresearch.db import session_scope
    from finresearch.db.models import PortfolioSnapshot
    from finresearch.wealth.service import history, load

    today = date(2026, 10, 5)
    with session_scope() as s:
        _buy(s, date(2026, 7, 1))
        # the series starts on 3-Aug (e.g. prices only from then): July's month-end is unknown, not ₹0
        _store(s, [date(2026, 8, 3), date(2026, 8, 31), date(2026, 9, 30)], [1000.0, 1100.0, 1200.0])
        # an "as shown" snapshot of 31-Aug that differs (it was taken mid-session): past days ignore it
        s.add(
            PortfolioSnapshot(
                day=date(2026, 8, 31), value=D(5000), invested=D(1000), by_asset={}, complete=True
            )
        )
        s.add(
            PortfolioSnapshot(
                day=date(2026, 10, 5), value=D(1250), invested=D(1000), by_asset={}, complete=True
            )
        )
    with session_scope() as s:
        pts = {p["date"]: p for p in history(load(s), today)}
    assert min(pts) == "2026-07-31"  # the chart starts with the first trade (1-Jul), not the first snapshot
    jul = pts["2026-07-31"]
    assert (
        jul["portfolio"] is None
        and not jul["complete"]
        and "before the reconstructed value history" in jul["missing"][0]
    )
    aug = pts["2026-08-31"]
    assert (aug["portfolio"], aug["portfolio_day"], aug["portfolio_source"]) == (
        1100.0,
        "2026-08-31",
        "reconstructed",
    )
    assert pts["2026-09-30"]["portfolio"] == 1200.0
    now = pts["2026-10-05"]  # today: the latest "as shown" valuation
    assert (now["portfolio"], now["portfolio_day"], now["portfolio_source"]) == (
        1250.0,
        "2026-10-05",
        "as shown",
    )


def test_data_health_names_the_reconciliation_differences():
    from finresearch.portfolio.health import history

    perf = {"summary": {"days": 3}, "reconciliation": {"checked": 2, "tolerance_pct": 1.0, "differ": [
        {"day": "2026-09-29", "reason": "the snapshot was incomplete"}]}}  # fmt: skip
    row = history(perf, None, True)
    assert row["detail"].endswith("; 1 of 2 saved valuation day(s) differ from it by more than 1 % (latest "
                                  "2026-09-29: the snapshot was incomplete)")  # fmt: skip


def test_the_live_monitor_builds_the_value_history():
    # no network: only that the live deps wire the builder the daily pass calls (#239)
    from finresearch.monitor.jobs import Deps
    from finresearch.monitor.portfolio_daily import live_history

    assert Deps.live().pf_history is live_history
    assert (
        Deps(ipo_detail=None, quote=None).pf_history is None
    )  # tests and partial deps: the pass skips it and says so


# --------------------------------------------------------------------------- the week's change says why it is missing
def _week_series(s, last: date, n: int = 10):
    """n consecutive days ending on `last`, value 1,000 rising by 10 a day, invested flat at 1,000."""
    days = [last - timedelta(days=n - 1 - i) for i in range(n)]
    _store(s, days, [1000.0 + 10 * i for i in range(n)])


def test_week_change_reads_the_series_and_says_why_when_it_cannot(db):
    """#264: the dashboard strip, the weekly digest and the brief's daily line were silently empty when the stored
    value history was out of date. Now each carries the reason."""
    from datetime import UTC

    from finresearch.api.brief import strip
    from finresearch.db import session_scope
    from finresearch.monitor.digest import build_brief, build_digest, digest_text, save_settings

    now = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 08:30 IST on Thu 1-Oct
    with session_scope() as s:
        _buy(s, date(2026, 9, 1))
        _week_series(s, date(2026, 9, 30))  # 21-Sep .. 30-Sep
        save_settings(s, {"daily_performance": True})
    with session_scope() as s:
        week = strip(s, now)
        # from 24-Sep (1,030: the last day on or before 1-Oct minus 7) to 30-Sep (1,090): +60, no new money
        assert (week["week"]["from"], week["week"]["change"], week["week_why"]) == ("2026-09-24", 60.0, None)
        brief = build_brief(s, now)
        assert brief["performance"]["from"] == "2026-09-29" and brief["performance_why"] is None
        _buy(s, date(2026, 9, 15))  # a backdated trade: the stored history is now out of date
    with session_scope() as s:
        week = strip(s, now)
        assert week["week"] is None and week["week_why"].startswith("unavailable: the value history built on")
        assert "out of date" in week["week_why"]
        d = build_digest(s, now)
        assert d["value"] is None and "out of date" in d["value_why"]
        assert "Weekly change unavailable: the value history built on" in digest_text(d)
        brief = build_brief(s, now)
        assert brief["performance"] is None and "out of date" in brief["performance_why"]


def test_week_change_is_unavailable_when_the_history_ends_too_long_ago(db):
    from datetime import UTC

    from finresearch.api.brief import strip
    from finresearch.db import session_scope

    now = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)  # 1-Oct IST
    with session_scope() as s:
        _buy(s, date(2026, 9, 1))
        # ends 26-Sep, 5 days before 1-Oct (more than HISTORY_MAX_AGE_DAYS = 4): the old code still printed a "week"
        # of 24-Sep .. 26-Sep as if it were the last seven days
        _week_series(s, date(2026, 9, 26))
    with session_scope() as s:
        week = strip(s, now)
    assert week["week"] is None
    assert week["week_why"].startswith(
        "unavailable: the value history is out of date (its last day is 2026-09-26"
    )
