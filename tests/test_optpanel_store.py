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
        for d in (*days, D2):
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
