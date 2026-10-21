"""The acceptance checks of #366 over stored data (read-only): the F&O bhavcopy's underlying price against the index
close, and India VIX against the app's IV30. Synthetic rows in the test database."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text


@pytest.fixture
def clean(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE index_eod, fno_bhav, fno_iv_eod"))

    wipe()
    yield env
    wipe()


def _bhav(s, day, sym, und):
    from finresearch.db.models import FnoBhav

    s.add(FnoBhav(day=day, symbol=sym, instrument="IDF", expiry=date(2024, 1, 25), strike=Decimal(0), option_type="XX",
                  underlying=und))  # fmt: skip


def _idx(s, day, sym, close):
    from finresearch.db.models import IndexEod

    s.add(IndexEod(day=day, symbol=sym, index_name=sym, close=close))


def test_underlying_vs_close_counts_exact_matches_and_lists_the_rest(clean):
    from finresearch.db import session_scope
    from finresearch.refseries import checks

    d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
    with session_scope() as s:
        _bhav(s, d1, "NIFTY", Decimal("21665.80"))
        _idx(s, d1, "NIFTY", Decimal("21665.8"))  # exact
        _bhav(s, d2, "NIFTY", Decimal("21517.35"))
        _idx(s, d2, "NIFTY", Decimal("21517.40"))  # 0.05 off: an exception
        _bhav(s, d3, "NIFTY", Decimal("21658.60"))  # no index row: missing, not a match
    with session_scope() as s:
        res = checks.underlying_vs_close(s, symbols=("NIFTY",), start=date(2024, 1, 1))
    assert res["pairs"] == 2 and res["exact"] == 1 and res["exact_share"] == 0.5
    assert res["mismatches"] == [{"day": "2024-01-03", "symbol": "NIFTY", "underlying": 21517.35, "close": 21517.40,
                                  "diff": pytest.approx(-0.05)}]  # fmt: skip
    assert res["missing_close"] == [{"day": "2024-01-04", "symbol": "NIFTY"}]


def test_vix_vs_iv30_joins_india_vix_with_nifty_iv30(clean):
    from finresearch.db import session_scope
    from finresearch.db.models import FnoIvEod
    from finresearch.refseries import checks

    days = [date(2024, 1, d) for d in (2, 3, 4, 5)]
    with session_scope() as s:
        for d, v, iv in zip(
            days, ("14.0", "15.0", "14.5", "16.0"), ("13.0", "14.0", "13.5", None), strict=True
        ):
            _idx(s, d, "INDIA VIX", Decimal(v))
            s.add(
                FnoIvEod(
                    symbol="NIFTY", day=d, rate=Decimal("0.065"), iv_30d=None if iv is None else Decimal(iv)
                )
            )
    with session_scope() as s:
        res = checks.vix_vs_iv30(s, start=date(2024, 1, 1))
    assert (
        res["n"] == 3
        and res["median_gap"] == pytest.approx(1.0)
        and res["change_correlation"] == pytest.approx(1)
    )
    assert res["first_day"] == "2024-01-02" and res["last_day"] == "2024-01-04"


def test_cli_backfill_and_audit(clean, monkeypatch):
    from typer.testing import CliRunner

    from finresearch import cli
    from finresearch.refseries import index_eod as store

    seen: dict = {}

    async def fake_backfill(start, end=None, **kw):
        seen.update(start=start, end=end, **kw)
        return {"planned": 0, "loaded": 0, "no_file": 0, "failed": {}, "rows": 0, "from_raw": 0}

    monkeypatch.setattr(store, "backfill", fake_backfill)
    r = CliRunner().invoke(
        cli.app, ["index", "backfill", "--from", "2013-03-01", "--to", "2013-03-31", "--delay", "2"]
    )
    assert r.exit_code == 0, r.output
    assert seen["start"] == date(2013, 3, 1) and seen["end"] == date(2013, 3, 31) and seen["pause"] == 2.0
    r = CliRunner().invoke(cli.app, ["audit", "index-eod", "--from", "2024-01-01"])
    assert r.exit_code == 0, r.output
    assert '"pairs": 0' in r.output and '"n": 0' in r.output
