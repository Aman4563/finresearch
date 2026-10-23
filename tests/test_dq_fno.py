"""F&O bhavcopy post-load checks (#368) against seeded faults (acceptance 1, synthetic): each fault type the issue lists
is injected into a clean synthetic day and must be flagged; the clean day must pass every check. Test database only."""

from __future__ import annotations

import dq_synth as ds
import pytest
from sqlalchemy import text

TABLES = "fno_bhav, fno_bhav_day, dataset_load"


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def run(t0_rows=None, t1_rows=None, setup=None):
    from finresearch.db import session_scope
    from finresearch.dq import fno

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
        ds.manifest(s, ds.T1)
        ds.insert_bhav(s, ds.bhav_rows(ds.T0) if t0_rows is None else t0_rows)
        ds.insert_bhav(s, ds.bhav_rows(ds.T1) if t1_rows is None else t1_rows)
        if setup:
            setup(s)
    with session_scope() as s:
        return {c.check: c for c in fno.bhav_checks(s, ds.T1)}


def flagged(res) -> set[str]:
    return {k for k, c in res.items() if c.status in ("fail", "warn")}


def test_clean_day_passes_every_check(clean):
    res = run()
    assert flagged(res) == set(), {k: (c.status, c.note) for k, c in res.items()}
    assert {"futures_continuity", "expiry_continuity", "expired_contracts", "stale_settle", "oi_lot_multiple",
            "turnover_units", "price_jump", "row_ranges", "rows_total_z", "rows_kept_z", "reupload",
            "duplicate_body"} <= set(res)  # fmt: skip


def test_dropped_symbol(clean):
    rows = [r for r in ds.bhav_rows(ds.T1) if r["symbol"] != "XYZSTK"]
    assert "futures_continuity" in flagged(run(t1_rows=rows))


def test_duplicated_day_and_stale_settle(clean):
    copy = [{**r, "day": ds.T1} for r in ds.bhav_rows(ds.T0)]
    assert "stale_settle" in flagged(run(t1_rows=copy))
    settle_only = [
        {**r, "settle": p["settle"]} for r, p in zip(ds.bhav_rows(ds.T1), ds.bhav_rows(ds.T0), strict=True)
    ]
    assert "stale_settle" in flagged(run(t1_rows=settle_only))


def test_wrong_trade_date_same_body_as_another_day(clean):
    def setup(s):
        from finresearch.dq.lineage import record_load

        record_load(s, "fno_bhav", ds.T0, b"day-file", url="u", parser_version="1", rows=1)
        record_load(s, "fno_bhav", ds.T1, b"day-file", url="u", parser_version="1", rows=1)

    assert "duplicate_body" in flagged(run(setup=setup))


def test_lakh_rupee_swap(clean):
    rows = [{**r, "turnover": r["turnover"] / 100000} for r in ds.bhav_rows(ds.T1)]
    assert "turnover_units" in flagged(run(t1_rows=rows))


def test_units_lots_swap(clean):
    rows = [{**r, "oi": r["oi"] // r["lot_size"] + 1} for r in ds.bhav_rows(ds.T1)]
    assert "oi_lot_multiple" in flagged(run(t1_rows=rows))


def test_expiring_oi_kept(clean):
    from datetime import date

    old = [{**r, "expiry": date(2026, 9, 29)} for r in ds.bhav_rows(ds.T1) if r["expiry"] == ds.E1]
    assert "expired_contracts" in flagged(run(t1_rows=ds.bhav_rows(ds.T1) + old))


def test_fake_bonus_jump(clean):
    from decimal import Decimal

    half = Decimal("0.5")
    rows = [{**r, **{k: r[k] * half for k in ("open", "high", "low", "close", "settle")}, "turnover": r["turnover"] * half}
            if r["symbol"] == "ABCSTK" else r for r in ds.bhav_rows(ds.T1)]  # fmt: skip
    res = run(t1_rows=rows)
    assert "price_jump" in flagged(res) and not res["price_jump"].critical


def test_missing_expiry(clean):
    rows = [r for r in ds.bhav_rows(ds.T1) if not (r["instrument"] == "IDO" and r["expiry"] == ds.E2)]
    assert "expiry_continuity" in flagged(run(t1_rows=rows))


def test_reupload_with_changed_rows(clean):
    def setup(s):
        from finresearch.dq.lineage import record_load

        record_load(s, "fno_bhav", ds.T1, b"first", url="u", parser_version="1", rows=2)
        record_load(s, "fno_bhav", ds.T1, b"second", url="u", parser_version="1", rows=1)

    assert "reupload" in flagged(run(setup=setup))


def test_row_count_collapse(clean):
    from finresearch.db.models import FnoBhavDay

    def setup(s):
        s.get(FnoBhavDay, ds.T1).rows_kept = 5

    assert "rows_kept_z" in flagged(run(setup=setup))


def test_not_loaded_day_is_unknown_never_ok(clean):
    from finresearch.db import session_scope
    from finresearch.dq import fno

    with session_scope() as s:
        res = fno.bhav_checks(s, ds.T1)
    assert res and all(c.status == "unknown" for c in res)


# --- replay false alarms (2024-01-01 .. 2026-10-22): expiries NSE moved or never traded, renamed symbols


def test_expiry_on_a_special_holiday_is_moved_not_lost():
    """MIDCPNIFTY's 22-Jan-2024 expiry: 22-Jan was declared a holiday, the contract settled on the earlier session,
    so on 23-Jan it is gone. Reasoned (expiry_moved), not a critical loss."""
    from datetime import date

    from finresearch.dq.fno import classify_lost_expiries

    groups = {("MIDCPNIFTY", "IDO", date(2024, 1, 22)): 5000, ("MIDCPNIFTY", "IDO", date(2024, 1, 29)): 900}
    lost, moved, idle = classify_lost_expiries(groups, {("MIDCPNIFTY", "IDO", date(2024, 1, 29))},
                                               date(2024, 1, 20), date(2024, 1, 23))  # fmt: skip
    assert lost == [] and idle == 0 and moved == ["MIDCPNIFTY IDO 2024-01-22 fell between sessions 2024-01-20 and "
                                                  "2024-01-23"]  # fmt: skip
    # the holiday list explains a later expiry too
    groups = {("MIDCPNIFTY", "IDO", date(2024, 1, 25)): 5000}
    assert classify_lost_expiries(groups, set(), date(2024, 1, 20), date(2024, 1, 23),
                                  holidays={date(2024, 1, 25)})[:2] == ([], ["MIDCPNIFTY IDO 2024-01-25 is an NSE "
                                                                             "holiday"])  # fmt: skip


def test_idle_far_expiry_is_not_required_but_one_with_oi_is():
    """MIDCPNIFTY IDO 2024-05-27 absent on 2024-04-12: a far expiry with no open interest (the store drops rows with no
    OI and no trades). The same expiry with OI, or a near expiry without OI, is still a critical loss."""
    from datetime import date

    from finresearch.dq.fno import classify_lost_expiries

    p, d = date(2024, 4, 10), date(2024, 4, 12)
    near, nxt, far = date(2024, 4, 29), date(2024, 5, 27 - 7), date(2024, 5, 27)
    keep = {("MIDCPNIFTY", "IDO", near), ("MIDCPNIFTY", "IDO", nxt)}
    groups = {("MIDCPNIFTY", "IDO", near): 100, ("MIDCPNIFTY", "IDO", nxt): 50, ("MIDCPNIFTY", "IDO", far): 0}
    assert classify_lost_expiries(groups, keep, p, d) == ([], [], 1)
    assert classify_lost_expiries({**groups, ("MIDCPNIFTY", "IDO", far): 75}, keep, p, d)[0] == [
        ("MIDCPNIFTY", "IDO", far)]  # fmt: skip
    no_oi_near = {**groups, ("MIDCPNIFTY", "IDO", near): 0}
    assert classify_lost_expiries(no_oi_near, keep - {("MIDCPNIFTY", "IDO", near)}, p, d)[0] == [
        ("MIDCPNIFTY", "IDO", near)]  # fmt: skip


def test_far_idle_expiry_and_moved_expiry_in_the_store(clean):
    from datetime import date

    far, e0 = date(2026, 12, 29), date(2026, 10, 1)
    t0 = ds.bhav_rows(ds.T0)
    idle = [
        {**r, "expiry": far, "oi": 0, "contracts": 1}
        for r in t0
        if r["instrument"] == "IDO" and r["expiry"] == ds.E1
    ]
    near = [{**r, "expiry": e0} for r in t0 if r["instrument"] == "IDO" and r["expiry"] == ds.E1]
    moved = [
        {**r, "expiry": ds.T1}
        for r in ds.bhav_rows(ds.T1)
        if r["instrument"] == "IDO" and r["expiry"] == ds.E1
    ]
    res = run(t0_rows=t0 + idle + near, t1_rows=ds.bhav_rows(ds.T1) + moved)
    assert res["expiry_continuity"].status == "ok" and "1 far expiry" in res["expiry_continuity"].note
    assert res["expiry_moved"].status == "warn" and not res["expiry_moved"].critical
    assert "2026-10-01→2026-09-30" in res["expiry_moved"].note


def test_renamed_symbol_is_a_symbol_change_not_a_drop(clean):
    """L&TFH -> LTF (2024-04-23), TATAMOTORS -> TMPV (2025-10-24): the open futures continue under a new symbol."""
    rows = [{**r, "symbol": "NEWSTK"} if r["symbol"] == "XYZSTK" else r for r in ds.bhav_rows(ds.T1)]
    res = run(t1_rows=rows)
    assert res["futures_continuity"].status == "ok"
    sc = res["symbol_changed"]
    assert sc.status == "warn" and not sc.critical and sc.note == "symbol changed XYZSTK→NEWSTK"


def test_unexplained_vanish_with_an_unrelated_new_listing_stays_critical(clean):
    """A drop on the day an unrelated stock enters F&O (another lot size, or a price nowhere near) is not a rename."""
    t1 = ds.bhav_rows(ds.T1)
    for change in ({"lot_size": 300}, {"close": t1[0]["close"] * 3}):
        new = [{**r, "symbol": "NEWSTK", **change} for r in t1 if r["symbol"] == "XYZSTK"]
        res = run(t1_rows=[r for r in t1 if r["symbol"] != "XYZSTK"] + new)
        assert res["futures_continuity"].status == "fail" and res["futures_continuity"].critical, change
        assert "XYZSTK" in res["futures_continuity"].note and res["symbol_changed"].status == "ok"
