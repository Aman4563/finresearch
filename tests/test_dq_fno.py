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
