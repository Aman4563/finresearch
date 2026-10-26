"""The control charts over stored series (#368, dq.control): the manifest's row counts, the futures' settle-change
dispersion, NIFTY's IV30 and its gap to India VIX. Made-up symbols (SYN01..SYN25) and hand-derived statistics; test
database only."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, text

TABLES = "fno_bhav, fno_bhav_day, fno_iv_eod, index_eod, dataset_load"
UPTO = date(2026, 9, 30)
FAR = date(2027, 6, 24)  # one far expiry: every return stays within one contract


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def weekdays(n: int) -> list[date]:
    out, d = [], UPTO
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def manifest(s, days, totals, kepts) -> None:
    from finresearch.db.models import FnoBhavDay

    for d, t, k in zip(days, totals, kepts, strict=True):
        s.add(FnoBhavDay(day=d, status="loaded", format="udiff", url="synthetic", rows_total=t, rows_kept=k,
                         rows_dead=0, stock_symbols=[]))  # fmt: skip


def bhav(day: date) -> dict:
    from finresearch.db import session_scope
    from finresearch.dq import control

    with session_scope() as s:
        return {c.check: c for c in control.bhav_charts(s, day)}


def test_a_row_count_step_warns_once_then_reads_beyond(clean):
    from finresearch.db import session_scope

    days = weekdays(61)
    with session_scope() as s:
        manifest(s, days, [*([990, 1010] * 31)[:59], 1100, 1100], ([495, 505] * 31)[:61])
    # log counts: centre ln 999.95, MAD 0.0100003, sigma 0.0148264; 1100 -> u 6.4318, C+ 5.9318 > 5 the first day
    first, second = bhav(days[59]), bhav(days[60])
    c = first["cc_rows_total"]
    assert (c.status, c.critical, c.threshold) == ("warn", False, 5.0)
    assert c.value == pytest.approx(5.9318, abs=1e-3) and f"crossed its limit on {days[59]}" in c.note
    assert first["cc_rows_kept"].status == "ok"
    c2 = second["cc_rows_total"]
    assert c2.status == "ok" and c2.value == pytest.approx(11.8636, abs=2e-3)
    assert f"beyond its limit since {days[59]}" in c2.note


def test_a_small_row_count_move_stays_quiet(clean):
    from finresearch.db import session_scope

    days = weekdays(60)
    with session_scope() as s:
        manifest(s, days, [*([990, 1010] * 30)[:59], 1040], ([495, 505] * 30)[:60])
    c = bhav(days[-1])["cc_rows_total"]  # u = 2.6487 -> C+ 2.1487 < 5
    assert c.status == "ok" and c.value == pytest.approx(2.1487, abs=1e-3)


def futures(s, days, spreads) -> None:
    """25 made-up futures whose day-t log settle change is spread_t x z_i, z_i = -1.2, -1.1 .. 1.2: the cross-section's
    1.4826 x MAD is 1.4826 x 0.6 x spread_t (the median |z| is 0.6)."""
    from finresearch.db.models import FnoBhav

    rows, px = [], {i: 1000.0 for i in range(25)}
    for t, d in enumerate(days):
        for i in range(25):
            if t:
                px[i] = (
                    px[i] * math.exp(spreads[t - 1] * (i - 12) / 10) if spreads[t - 1] is not None else px[i]
                )
            p = Decimal(str(round(px[i], 2)))
            rows.append({"day": d, "symbol": f"SYN{i + 1:02d}", "instrument": "STF", "expiry": FAR,
                         "strike": Decimal(0), "option_type": "XX", "close": p, "settle": p, "oi": 0, "lot_size": 1})  # fmt: skip
    s.execute(insert(FnoBhav), rows)


@pytest.mark.parametrize("last,status", [(0.010, "ok"), (0.030, "warn"), (None, "warn")])
def test_settle_dispersion(clean, last, status):
    """Spreads alternate 1.0 % / 1.2 % (log dispersion: MAD ln(1.2)/2, sigma 0.13516); a 3 % day is u = 7.45 (EWMA
    about 1.5 > 0.987); a copied day (no settle moved, None) charts at the floor, far below."""
    from finresearch.db import session_scope

    days = weekdays(61)
    with session_scope() as s:
        manifest(s, days, [1000] * 61, [500] * 61)
        futures(s, days, [*([0.01, 0.012] * 30)[:59], last])
    c = bhav(days[-1])["cc_settle_dispersion"]
    assert (c.status, c.critical) == (status, False), c.note


def ivs(s, days, iv30, gaps) -> None:
    from finresearch.db.models import FnoIvEod, IndexEod

    for d, iv, g in zip(days, iv30, gaps, strict=True):
        s.add(
            FnoIvEod(day=d, symbol="NIFTY", iv_30d=Decimal(str(iv)), iv_30d_method="interpolated", rate=0.065)
        )
        if g is not None:
            s.add(
                IndexEod(
                    day=d, symbol="INDIA VIX", index_name="India VIX", close=Decimal(str(round(iv + g, 4)))
                )
            )


@pytest.mark.parametrize("last_iv,last_gap,want", [(20.0, 1.5, ("warn", "ok")), (12.5, 5.0, ("ok", "warn")),
                                                   (12.5, None, ("ok", "unknown"))])  # fmt: skip
def test_iv30_level_and_its_gap_to_vix(clean, last_iv, last_gap, want):
    """IV30 alternates 12 / 13 (log sigma 0.059335): 20 is u = 7.93; the VIX - IV30 gap alternates 1.4 / 1.6 (sigma
    0.14826): 5.0 is u = 23.6. Each series flags on its own; no VIX on the day is unknown, never ok."""
    from finresearch.db import session_scope
    from finresearch.dq import control

    days = weekdays(60)
    with session_scope() as s:
        ivs(s, days, [*([12.0, 13.0] * 30)[:59], last_iv], [*([1.4, 1.6] * 30)[:59], last_gap])
    with session_scope() as s:
        got = {c.check: c for c in control.iv_charts(s, days[-1])}
    assert (got["cc_iv30"].status, got["cc_iv30_vix_gap"].status) == want
    assert all(c.dataset == "fno_iv_eod" and not c.critical for c in got.values())


def test_charts_ride_on_their_datasets_and_need_sixty_sessions(clean):
    import dq_synth as ds

    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.dq import fno, ref

    with session_scope() as s:
        ds.manifest(s, ds.T1)  # 30 sessions of history only
        ds.insert_bhav(s, ds.bhav_rows(ds.T0) + ds.bhav_rows(ds.T1))
        s.add(FnoIvEod(day=ds.T1, symbol="NIFTY", iv_30d=13, rate=0.065, iv_30d_method="interpolated"))
    with session_scope() as s:
        got = {c.check: c for c in fno.bhav_checks(s, ds.T1) + ref.iv_checks(s, ds.T1)}
    for name in ("cc_rows_total", "cc_rows_kept", "cc_settle_dispersion", "cc_iv30", "cc_iv30_vix_gap"):
        assert (got[name].status, got[name].critical) == ("unknown", False), (name, got[name].note)
    assert got["cc_rows_total"].note == "needs 60 sessions, has 30"
