"""Lineage per dataset load (#368): body sha256, parser version and re-upload detection. Test database only;
synthetic bhavcopy fixture (tests/fixtures/fno_bhav)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select, text

FX = Path(__file__).parent / "fixtures" / "fno_bhav"
UDIFF = (FX / "udiff_20260930_sample.csv").read_text()
DAY = date(2026, 9, 30)


def zipped(t: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bhav.csv", t)
    return buf.getvalue()


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE fno_bhav, fno_bhav_day, fno_iv_eod, fno_contract, dataset_load"))

    wipe()
    yield env
    wipe()


def loads():
    from finresearch.db import session_scope
    from finresearch.db.models import DatasetLoad

    with session_scope() as s:
        return [(r.dataset, r.day, r.sha256, r.prior_sha256, r.parser_version, r.rows, r.url)
                for r in s.scalars(select(DatasetLoad).order_by(DatasetLoad.id))]  # fmt: skip


def test_bhav_load_records_lineage_and_flags_a_changed_reupload(clean):
    from finresearch.adapters import nse_fno_bhav as fb
    from finresearch.db import session_scope
    from finresearch.fnohist import store

    first = zipped(UDIFF)
    with session_scope() as s:
        store.load_day(s, DAY, "udiff", "u1", first, [])
    with session_scope() as s:  # the same body again: no re-upload
        store.load_day(s, DAY, "udiff", "u1", first, [])
    lines = UDIFF.splitlines()
    second = zipped("\n".join(lines[:-1]) + "\n")  # NSE re-uploads the day with one row fewer
    with session_scope() as s:
        store.load_day(s, DAY, "udiff", "u2", second, [])
    sha1, sha2 = hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest()
    got = loads()
    assert [(g[0], g[1], g[2], g[3]) for g in got] == [
        ("fno_bhav", DAY, sha1, None), ("fno_bhav", DAY, sha1, None), ("fno_bhav", DAY, sha2, sha1)]
    assert {g[4] for g in got} == {fb.PARSER_VERSION}
    assert got[0][5] == 274 and got[2][6] == "u2"  # the rows kept without RELIANCE's options (302 with them)


def test_reuploads_lists_changed_days(clean):
    from finresearch.db import session_scope
    from finresearch.dq import lineage

    with session_scope() as s:
        lineage.record_load(s, "index_eod", DAY, b"a", url="x", parser_version="1", rows=3)
        lineage.record_load(s, "index_eod", DAY, b"b", url="x", parser_version="1", rows=3)
        lineage.record_load(s, "index_eod", date(2026, 9, 29), b"c", url="x", parser_version="1", rows=3)
    with session_scope() as s:
        assert lineage.reuploads(s, "index_eod", DAY) == 1
        assert lineage.reuploads(s, "index_eod", date(2026, 9, 29)) == 0
        assert lineage.reuploads(s, "fno_bhav", DAY) == 0


def test_index_and_span_loads_record_lineage(clean):
    from finresearch.adapters import nse_index_eod as ie
    from finresearch.adapters import nse_span as sp
    from finresearch.db import session_scope
    from finresearch.fnohist import span
    from finresearch.refseries import index_eod

    csv = (Path(__file__).parent / "fixtures" / "index_eod" / "ind_close_all_current_synthetic.csv").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("nsccl.20261016.s.spn", (Path(__file__).parent / "fixtures" / "span" / "nsccl_synthetic.spn").read_bytes())
    spn = buf.getvalue()
    with session_scope() as s:
        s.execute(text("TRUNCATE index_eod, index_eod_day, span_param, span_day"))
        n = index_eod.load_day(s, date(2022, 1, 3), "ix", csv)
        m = span.load_day(s, date(2026, 10, 16), "sp", spn)
    got = {g[0]: g for g in loads()}
    assert got["index_eod"][1:] == (date(2022, 1, 3), hashlib.sha256(csv).hexdigest(), None, ie.PARSER_VERSION, n, "ix")
    assert got["span"][1:] == (date(2026, 10, 16), hashlib.sha256(spn).hexdigest(), None, sp.PARSER_VERSION, m, "sp")
    with session_scope() as s:
        s.execute(text("TRUNCATE index_eod, index_eod_day, span_param, span_day"))
