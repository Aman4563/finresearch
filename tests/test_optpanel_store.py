"""Building the options panel from the kept raw zips (P1.12, #374): behind the registration, outside the vault,
portfolio-blind, resumable and idempotent, with lineage per file. Test database only; synthetic zips in a tmp
data dir; no network."""

from __future__ import annotations

import hashlib
import math
from datetime import date, timedelta
from decimal import Decimal

import pytest
from optpanel_synth import csv_text, underlying, zipped
from sqlalchemy import func, select, text

from finresearch.adapters import nse_fno_bhav as fb

D1, D2 = date(2026, 3, 2), date(2026, 3, 3)
EXPS = [date(2026, 3, 31), date(2026, 4, 28)]
TABLES = "optpanel_eod, optpanel_row, optpanel_day, dataset_load, fno_bhav, fno_bhav_day"


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))
    yield env
    with session_scope() as s:
        s.execute(text(f"TRUNCATE {TABLES}"))


def zip_for(day: date, alpha_vol: float = 0.30) -> bytes:
    lines = underlying(day, "ZZALPHA", 1000.0, alpha_vol, EXPS, lot=100) + underlying(
        day, "ZZBETA", 250.0, 0.45, EXPS, lot=400
    )
    return zipped(csv_text(lines))


def put_raw(day: date, body: bytes) -> bytes:
    from finresearch.fnohist.store import raw_path

    p = raw_path(day, fb.url_for(day, "udiff"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return body


def sessions(day: date, n: int) -> list[date]:
    out, d = [], day
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return sorted(out)


def seed_futures(days: list[date], settles: list[float]) -> None:
    """ZZALPHA's March future in fno_bhav (stored for every stock), and the bhavcopy manifest's sessions."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, FnoBhavDay

    with session_scope() as s:
        for d, p in zip(days, settles, strict=True):
            s.add(FnoBhav(day=d, symbol="ZZALPHA", instrument="STF", expiry=EXPS[0], strike=Decimal(0),
                          option_type="XX", settle=Decimal(str(p)), close=Decimal(str(p)), oi=5000, contracts=500,
                          lot_size=100))  # fmt: skip
        for d in {*days, D1, D2}:
            s.merge(FnoBhavDay(day=d, status="loaded"))


def test_build_writes_the_panel_with_rv20_rows_and_lineage(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, OptPanelDay, OptPanelEod, OptPanelRow
    from finresearch.optpanel import prereg, store

    prior = sessions(D1, 22)
    settles = [round(1000.0 * math.exp(0.01 * (i % 3 - 1)), 2) for i in range(22)] + [
        1004.0
    ]  # 23 settles: 22 returns
    seed_futures([*prior, D1], settles)
    body = put_raw(D1, zip_for(D1))
    out = store.build(D1, D1, today=D2)
    assert out["built"] == 1 and out["underlyings"] == 2
    with session_scope() as s:
        a = s.get(OptPanelEod, (D1, "ZZALPHA"))
        assert float(a.near_iv) == pytest.approx(30.0, abs=0.3) and a.near_expiry == EXPS[0]
        rets = [math.log(settles[i + 1] / settles[i]) for i in range(22)][-20:]
        m = sum(rets) / 20
        want = math.sqrt(sum((x - m) ** 2 for x in rets) / 19 * 252) * 100
        assert float(a.rv_20) == pytest.approx(want, abs=0.001)
        assert s.get(OptPanelEod, (D1, "ZZBETA")).rv_20 is None  # no stored futures history: unknown, not 0
        assert s.scalar(select(func.count()).select_from(OptPanelRow).where(OptPanelRow.day == D1)) > 0
        m = s.get(OptPanelDay, D1)
        assert m.status == "built" and m.spec_hash == prereg.HASH and m.underlyings == 2 and m.with_iv == 2
        lin = s.scalars(select(DatasetLoad).where(DatasetLoad.dataset == "optpanel")).one()
        assert lin.day == D1 and lin.sha256 == hashlib.sha256(body).hexdigest() and lin.rows == 2
        assert lin.url == fb.url_for(D1, "udiff")


def test_the_manifest_records_no_stock_list() -> None:
    from finresearch.db.models import OptPanelDay

    cols = set(OptPanelDay.__table__.c.keys())
    assert not any("symbol" in c or "stock" in c for c in cols), cols


def test_build_refuses_a_changed_registration_and_writes_nothing(clean, monkeypatch) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import OptPanelDay
    from finresearch.optpanel import params, prereg, store

    seed_futures([], [])
    put_raw(D1, zip_for(D1))
    monkeypatch.setattr(params, "ROWS_LN_BAND", 0.3)
    with pytest.raises(prereg.PreregistrationError):
        store.build(D1, D2, today=D2)
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OptPanelDay)) == 0


def test_build_refuses_the_vault() -> None:
    from finresearch.optpanel import store
    from finresearch.vault.registry import VaultSealed

    with pytest.raises(VaultSealed):
        store.build(date(2021, 12, 31), date(2022, 1, 5), today=D2)


def test_build_never_reads_the_held_or_watched_list(clean, monkeypatch) -> None:
    """The panel is the same whatever the portfolio is: the tracked list is never asked for, and a tracked stock's
    full option rows in fno_bhav (different prices) do not leak into its panel row."""
    from finresearch.db import session_scope
    from finresearch.db.models import FnoBhav, OptPanelEod
    from finresearch.disclosures import store as dstore
    from finresearch.fnohist import store as fstore
    from finresearch.optpanel import store

    def boom(*a, **k):
        raise AssertionError("the panel read the held or watched list")

    monkeypatch.setattr(dstore, "tracked", boom)
    monkeypatch.setattr(fstore, "tracked_stocks", boom)
    seed_futures([], [])
    put_raw(D1, zip_for(D1))
    with session_scope() as s:  # ZZBETA "held": its options stored in fno_bhav at a 90 % volatility
        for r in fb.parse_udiff(csv_text(underlying(D1, "ZZBETA", 250.0, 0.90, EXPS, lot=400)))[0]:
            if r.right != "XX":
                s.add(FnoBhav(day=D1, symbol=r.symbol, instrument=r.instrument, expiry=r.expiry, strike=r.strike,
                              option_type=r.right, settle=r.settle, close=r.close, oi=r.oi, contracts=r.contracts))  # fmt: skip
    store.build(D1, D1, today=D2)
    with session_scope() as s:
        assert float(s.get(OptPanelEod, (D1, "ZZBETA")).near_iv) == pytest.approx(45.0, abs=0.3)


def test_build_resumes_rebuilds_idempotently_and_marks_missing_raw(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad, OptPanelDay, OptPanelEod
    from finresearch.optpanel import store

    seed_futures([], [])  # D2 is a loaded session whose raw zip was not kept
    put_raw(D1, zip_for(D1))
    first = store.build(D1, D2, today=D2)
    assert (first["built"], first["no_raw"]) == (1, 1)
    again = store.build(D1, D2, today=D2)
    assert again["planned"] == 1 and again["built"] == 0  # D1 skipped; D2 (no raw) tried again
    put_raw(D1, zip_for(D1, alpha_vol=0.20))
    store.build(D1, D1, today=D2, rebuild=True)
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(OptPanelEod).where(OptPanelEod.day == D1)) == 2
        assert float(s.get(OptPanelEod, (D1, "ZZALPHA")).near_iv) == pytest.approx(20.0, abs=0.3)
        assert s.get(OptPanelDay, D2).status == "no_raw"
        loads = s.scalars(
            select(DatasetLoad).where(DatasetLoad.dataset == "optpanel").order_by(DatasetLoad.id)
        ).all()
        assert len(loads) == 2 and loads[1].prior_sha256 == loads[0].sha256  # the changed zip is seen


def test_coverage_is_the_share_of_sessions_with_a_fitted_atm_iv(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import OptPanelDay, OptPanelEod
    from finresearch.optpanel import store

    with session_scope() as s:
        for d in (D1, D2, date(2026, 3, 4)):
            s.add(OptPanelDay(day=d, status="built"))
        s.add(OptPanelEod(day=D1, symbol="ZZALPHA", near_iv=Decimal("30")))
        s.add(OptPanelEod(day=D2, symbol="ZZALPHA", near_iv=None))
        s.add(OptPanelEod(day=D1, symbol="ZZBETA", near_iv=Decimal("45")))
        got = store.coverage(s, D1, date(2026, 3, 4))
    assert got["sessions"] == 3
    by = {r["symbol"]: r for r in got["stocks"]}
    assert by["ZZALPHA"] == {"symbol": "ZZALPHA", "rows": 2, "with_iv": 1, "pct": pytest.approx(100 / 3)}
    assert by["ZZBETA"]["pct"] == pytest.approx(100 / 3)


def test_participant_reconciliation_compares_stock_option_oi_and_keeps_unknowns(clean) -> None:
    from finresearch.db import session_scope
    from finresearch.db.models import FnoParticipantOi, OptPanelDay
    from finresearch.optpanel import store

    with session_scope() as s:
        s.execute(text("TRUNCATE fno_participant_oi"))
        s.add(OptPanelDay(day=D1, status="built", sto_call_oi_contracts=100, sto_put_oi_contracts=60))
        s.add(OptPanelDay(day=D2, status="built", sto_call_oi_contracts=None, sto_put_oi_contracts=None))
        zero = {k: 0 for k in ("fut_idx_long", "fut_idx_short", "fut_stk_long", "fut_stk_short", "opt_idx_call_long",
                               "opt_idx_put_long", "opt_idx_call_short", "opt_idx_put_short", "opt_stk_call_short",
                               "opt_stk_put_short", "total_long", "total_short")}  # fmt: skip
        for d in (D1, D2):
            for who, c, p in (("Client", 70, 40), ("FII", 30, 21)):
                s.add(FnoParticipantOi(day=d, client_type=who, opt_stk_call_long=c, opt_stk_put_long=p,
                                       source="test", **zero))  # fmt: skip
        got = {r["day"]: r for r in store.reconcile_participants(s, D1, D2)}
        s.execute(text("TRUNCATE fno_participant_oi"))
    assert got[D1.isoformat()] == {"day": D1.isoformat(), "call": [100, 100], "put": [60, 61], "match": False}
    assert got[D2.isoformat()]["match"] is None  # a lot size was unknown: not a match, not a mismatch
