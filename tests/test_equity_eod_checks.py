"""Data-quality checks on the full equity bhavcopy (#373 (a)): rows present, row ranges, re-uploads from the lineage
table, and the reconciliation of its closes with the official closes the stock peer build read from NSE's quotes
(the old path), exact to the paisa. Test database only; synthetic files and peer stores."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from sqlalchemy import text

from finresearch.adapters import nse_equity_eod as ee

FILE = (Path(__file__).parent / "fixtures" / "equity_eod" / "sec_bhavdata_full_synthetic.csv").read_text()
D = date(2024, 1, 15)


@pytest.fixture
def clean(env, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.dq import equity

    monkeypatch.setattr(equity, "MIN_EQ_ROWS", 2)  # the fixture lists two EQ stocks; a real day ~2,700

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE equity_eod, equity_eod_day, dataset_load"))

    wipe()
    yield env
    wipe()


def load(body: str = FILE) -> None:
    from finresearch.db import session_scope
    from finresearch.refseries import equity_eod as store

    with session_scope() as s:
        store.load_day(s, D, ee.url_for(D), body.encode())


def checks(day: date = D) -> dict:
    from finresearch.db import session_scope
    from finresearch.dq import equity

    with session_scope() as s:
        return {c.check: c for c in equity.equity_checks(s, day)}


def peers(rows: dict, as_of: date = D) -> None:
    from finresearch.signals import stock_peers as SP

    SP.save({"version": 1, "as_of": as_of.isoformat(), "generated_at": "x", "universe": "NIFTY 500",
             "universe_source": SP.UNIVERSE_URL, "rows": rows, "failed": {}})  # fmt: skip


def qc(price: float, day: date = D, kind: str = "official_close") -> dict:
    return {"symbol": "x", "quote_close": {"price": price, "kind": kind, "day": day.isoformat()}}


def test_a_clean_day_passes_and_the_reconciliation_is_unknown_without_quotes(clean):
    load()
    got = checks()
    assert {k: c.status for k, c in got.items()} == {"rows_present": "ok", "row_ranges": "ok", "reupload": "ok",
                                                     "close_vs_quote": "unknown"}  # fmt: skip
    assert got["close_vs_quote"].critical is False and got["reupload"].critical is False


def test_a_day_not_loaded_is_unknown_never_ok(clean):
    assert {c.status for c in checks().values()} == {"unknown"}


def test_too_few_rows_and_impossible_prices_fail(clean, monkeypatch):
    from finresearch.dq import equity

    load(FILE.replace("2525.00, 2480.10", "2470.00, 2480.10"))  # BETAIND high below its low
    got = checks()
    assert got["row_ranges"].status == "fail" and got["row_ranges"].value == 1
    monkeypatch.setattr(equity, "MIN_EQ_ROWS", 1000)
    assert checks()["rows_present"].status == "fail"


def test_quote_closes_reconcile_exactly_with_the_bhavcopy(clean):
    load()
    peers({"ALPHACO": qc(104.00), "BETAIND": qc(2491.35), "GAMMALTD": qc(21.93)})
    c = checks()["close_vs_quote"]
    assert (c.status, c.value, c.threshold) == ("ok", 0, 0)
    assert "3 stock(s)" in c.note


def test_a_one_paisa_difference_fails_the_reconciliation(clean):
    load()
    peers({"ALPHACO": qc(104.01), "BETAIND": qc(2491.35)})
    c = checks()["close_vs_quote"]
    assert c.status == "fail" and c.value == 1 and "ALPHACO" in c.note and c.critical is False


def test_only_official_closes_of_the_same_day_are_compared(clean):
    load()
    peers({"ALPHACO": qc(999.0, kind="last_traded"),  # not the close: never compared
           "BETAIND": qc(999.0, day=date(2024, 1, 12)),  # another day's quote
           "NOSUCH": qc(5.0)})  # not in the bhavcopy  # fmt: skip
    assert checks()["close_vs_quote"].status == "unknown"


def test_the_dataset_runs_in_the_nightly_pass(clean):
    from finresearch.db import session_scope
    from finresearch.dq import run

    ds = next(d for d in run.DATASETS if d.name == "equity_eod")
    assert ds.critical is False and ds.sla == time(20, 30)
    load()
    with session_scope() as s:
        assert ds.last(s) == D
        out = run.run_all(s, datetime.combine(D, time(16, 0), UTC), holidays=set())  # 21:30 IST
    assert out["status"]["equity_eod"] == "ok"  # fresh, clean; the non-critical reconciliation unknown
