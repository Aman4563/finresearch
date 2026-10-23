"""Participant OI, derived IV, index close and SPAN checks (#368) on synthetic days, with the issue's reconciliations:
(a) participant futures longs vs the bhavcopy's OI / lot, (b) the bhavcopy underlying vs the index close, (c) India
VIX - IV30 against its own median and MAD. Test database only."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import dq_synth as ds
import pytest
from sqlalchemy import insert, text

TABLES = "fno_bhav, fno_bhav_day, fno_participant_oi, fno_iv_eod, index_eod, index_eod_day, span_param, span_day, fo_volt"


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def seed(s, parts=None, idx=None):
    from finresearch.db.models import FnoParticipantOi, IndexEod, IndexEodDay

    ds.manifest(s, ds.T1)
    ds.insert_bhav(s, ds.bhav_rows(ds.T0) + ds.bhav_rows(ds.T1))
    s.execute(insert(FnoParticipantOi), ds.participants(ds.T1) if parts is None else parts)
    s.execute(insert(IndexEod), ds.index_rows(ds.T1) if idx is None else idx)
    s.add(IndexEodDay(day=ds.T1, status="loaded", url="synthetic", rows=3))


def by_check(checks):
    return {c.check: c for c in checks}


def test_participants_reconcile_with_bhavcopy(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref

    with session_scope() as s:
        seed(s)
        res = by_check(ref.participant_checks(s, ds.T1))
    # #371 added the non-critical reupload check: unknown here, as the seeded rows have no lineage row
    assert {k: c.status for k, c in res.items()} == {"participants_present": "ok", "long_short_identity": "ok",
                                                     "reupload": "unknown", "bhav_fut_idx": "ok",
                                                     "bhav_fut_stk": "ok"}  # fmt: skip
    assert res["bhav_fut_idx"].value == 0


def test_participant_mismatch_and_missing_row(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref

    parts = ds.participants(ds.T1)
    parts[0] = {**parts[0], "fut_idx_long": parts[0]["fut_idx_long"] + 500}  # 500 contracts too many
    with session_scope() as s:
        seed(s, parts=parts)
        res = by_check(ref.participant_checks(s, ds.T1))
    assert res["bhav_fut_idx"].status == "fail" and res["bhav_fut_idx"].value == 500
    assert res["long_short_identity"].status == "fail"
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
        seed(s, parts=ds.participants(ds.T1)[:3])
        assert by_check(ref.participant_checks(s, ds.T1))["participants_present"].status == "fail"


def test_participants_without_lot_sizes_are_unknown(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref

    with session_scope() as s:
        seed(s)
        s.execute(text("UPDATE fno_bhav SET lot_size = NULL WHERE day = :d"), {"d": ds.T1})
        assert by_check(ref.participant_checks(s, ds.T1))["bhav_fut_idx"].status == "unknown"


def test_index_close_checks_and_underlying_reconciliation(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref

    with session_scope() as s:
        seed(s)
        res = by_check(ref.index_checks(s, ds.T1))
    # other_row_ranges (non-critical, unread indices) added on purpose in the replay false-alarm fix
    assert {k: c.status for k, c in res.items()} == {"expected_symbols": "ok", "row_ranges": "ok",
                                                     "other_row_ranges": "ok", "underlying_vs_close": "ok"}  # fmt: skip
    idx = [r for r in ds.index_rows(ds.T1) if r["symbol"] != "INDIA VIX"]
    idx[0] = {**idx[0], "close": idx[0]["close"] + Decimal("1.00")}  # NIFTY's close off by a rupee
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
        seed(s, idx=idx)
        res = by_check(ref.index_checks(s, ds.T1))
    assert res["expected_symbols"].status == "fail" and "INDIA VIX" in res["expected_symbols"].note
    assert res["underlying_vs_close"].status == "fail" and res["underlying_vs_close"].value == 1


def _idx(sym, o, h, lo, c):
    d = {k: (None if v is None else Decimal(v)) for k, v in zip(("open", "high", "low", "close"), (o, h, lo, c),
                                                                 strict=True)}  # fmt: skip
    return {"day": ds.T1, "symbol": sym, "index_name": sym, **d}


def test_unread_indices_with_nse_quirks_do_not_fail_critical(clean):
    """The replay's index false alarms: NIFTY50 DIVIDEND POINTS reset to 0 each April with zero high/low, and a
    NIFTY50 USD row with high < low in NSE's own file. Neither index is read by the app: no critical failure."""
    from finresearch.db import session_scope
    from finresearch.dq import ref

    extra = [_idx("NIFTY50 DIVIDEND POINTS", "0", "0", "0", "0"),
             _idx("NIFTY50 DIVIDEND POINTS 2", "12.5", None, None, "12.5"),
             _idx("NIFTY50 USD", "9800", "9790", "9810", "9800")]  # fmt: skip
    with session_scope() as s:
        seed(s, idx=ds.index_rows(ds.T1) + extra)
        res = by_check(ref.index_checks(s, ds.T1))
    assert res["row_ranges"].status == "ok"
    other = res["other_row_ranges"]
    assert other.status == "warn" and not other.critical and other.note == "NIFTY50 USD"
    assert not any(c.critical and c.status == "fail" for c in res.values())


def test_bad_row_in_a_read_index_still_fails_critical(clean):
    from finresearch.db import session_scope
    from finresearch.dq import ref

    for sym, bad in (("NIFTY", {"close": Decimal("30000")}), ("BANKNIFTY", {"high": Decimal("1")}),
                     ("INDIA VIX", {"close": Decimal("0")})):  # fmt: skip
        idx = [{**r, **bad} if r["symbol"] == sym else r for r in ds.index_rows(ds.T1)]
        with session_scope() as s:
            s.execute(text(f"TRUNCATE {TABLES}"))
            seed(s, idx=idx)
            res = by_check(ref.index_checks(s, ds.T1))
        assert res["row_ranges"].status == "fail" and res["row_ranges"].critical, sym
    with session_scope() as s:  # a dividend-points index going negative is still shown
        s.execute(text(f"TRUNCATE {TABLES}"))
        seed(s, idx=[*ds.index_rows(ds.T1), _idx("NIFTY50 DIVIDEND POINTS", "1", "1", "1", "-1")])
        assert by_check(ref.index_checks(s, ds.T1))["other_row_ranges"].status == "warn"


def test_vix_iv30_gap_outlier(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod, IndexEod
    from finresearch.dq import ref

    days = [ds.T1 - timedelta(days=i) for i in range(40, -1, -1)]
    with session_scope() as s:
        for i, d in enumerate(days):
            iv = 14.0 + (i % 3) * 0.1  # gaps VIX - IV30 = -1.0, -1.1, -1.2: median -1.1, MAD 0.1
            vix = 13.0 if d != ds.T1 else 20.0  # today's gap +6: far beyond 4 MAD
            s.add(IndexEod(day=d, symbol="INDIA VIX", index_name="India VIX", close=Decimal(str(vix))))
            s.add(FnoIvEod(day=d, symbol="NIFTY", iv_30d=Decimal(str(iv)), rate=Decimal("0.065")))
        s.flush()
        res = by_check(ref.iv_checks(s, ds.T1))
    assert res["vix_iv30_gap"].status == "warn" and not res["vix_iv30_gap"].critical


def test_iv_ranges_and_stock_rv(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.dq import ref

    with session_scope() as s:
        s.add(FnoIvEod(day=ds.T1, symbol="NIFTY", iv_30d=Decimal("13"), rv_20=Decimal("12"), rate=Decimal("0.065"),
                       iv_30d_method="interpolated"))  # fmt: skip
        s.add(FnoIvEod(day=ds.T1, symbol="ABCSTK", iv_30d=Decimal("250"), rv_20=Decimal("140"), rate=Decimal("0.065"),
                       iv_30d_method="flat"))  # fmt: skip
        s.flush()
        res = by_check(ref.iv_checks(s, ds.T1))
    assert res["present"].status == "ok"
    assert res["iv_range"].status == "fail" and res["iv_range"].value == 1
    assert res["stock_rv20_high"].status == "warn" and res["stock_rv20_high"].value == 1
    assert res["flat_iv30_share"].value == 0.5


def test_span_checks(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FoVolt, SpanDay, SpanParam
    from finresearch.dq import ref

    with session_scope() as s:
        assert {c.status for c in ref.span_checks(s, ds.T1)} == {"unknown"}
        s.add(SpanDay(day=ds.T1, status="loaded", rows=2))
        s.add(
            SpanParam(
                day=ds.T1,
                symbol="NIFTY",
                price=Decimal(25000),
                price_scan=Decimal(1500),
                vol_scan=Decimal("0.04"),
            )
        )
        s.add(
            SpanParam(
                day=ds.T1,
                symbol="ABCSTK",
                price=Decimal(1200),
                price_scan=Decimal(0),
                vol_scan=Decimal("1.5"),
            )
        )
        s.flush()
        res = by_check(ref.span_checks(s, ds.T1))
        assert res["row_ranges"].status == "fail" and res["row_ranges"].value == 1
        assert res["fo_volt_present"].status == "fail"
        s.add(FoVolt(day=ds.T1, symbol="NIFTY"))
        s.flush()
        assert by_check(ref.span_checks(s, ds.T1))["fo_volt_present"].status == "ok"
