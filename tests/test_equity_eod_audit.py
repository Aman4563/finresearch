"""#373 (a)'s acceptance check: the full bhavcopy's close against NSE's price-history bar on random stock-days, exact.
The fetcher is a fake (no network); the sample is reproducible for a seed; it never starts in market hours and
stops after two refusals in a row."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from finresearch.adapters import nse_equity_eod as ee
from finresearch.adapters.nse import NseError

FILE = (Path(__file__).parent / "fixtures" / "equity_eod" / "sec_bhavdata_full_synthetic.csv").read_text()
D1, D2 = date(2024, 1, 15), date(2024, 1, 16)
EVENING = datetime(2026, 10, 21, 14, 0, tzinfo=UTC)  # 19:30 IST
CLOSES = {("ALPHACO", D1): Decimal("104.00"), ("BETAIND", D1): Decimal("2491.35"),
          ("ALPHACO", D2): Decimal("104.00"), ("BETAIND", D2): Decimal("2491.35")}  # fmt: skip


@pytest.fixture
def stored(env):
    from finresearch.db import session_scope
    from finresearch.refseries import equity_eod as store

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE equity_eod, equity_eod_day, dataset_load"))

    wipe()
    with session_scope() as s:
        for d in (D1, D2):
            store.load_day(s, d, ee.url_for(d), FILE.replace("15-Jan-2024", d.strftime("%d-%b-%Y")).encode())
    yield env
    wipe()


def fetcher(closes: dict, calls: list):
    async def fetch(symbol: str, day: date) -> Decimal | None:
        calls.append((symbol, day))
        return closes.get((symbol, day))

    return fetch


async def test_every_sampled_eq_close_matches_the_history_bar(stored):
    from finresearch.refseries import equity_eod as store

    calls: list = []
    res = await store.reconcile_history(10, seed=1, fetch=fetcher(CLOSES, calls), now=lambda: EVENING)
    # only EQ rows are sampled: ALPHACO and BETAIND on two days = 4 stock-days, fewer than asked
    assert res["sampled"] == 4 and res["compared"] == 4 and res["matches"] == 4 and res["mismatches"] == []
    assert sorted(calls) == sorted(CLOSES)


async def test_a_mismatch_and_a_missing_bar_are_listed_never_counted_as_matches(stored):
    from finresearch.refseries import equity_eod as store

    closes = dict(CLOSES) | {("BETAIND", D2): Decimal("2491.40")}
    del closes[("ALPHACO", D2)]
    res = await store.reconcile_history(10, seed=1, fetch=fetcher(closes, []), now=lambda: EVENING)
    assert res["matches"] == 2 and res["compared"] == 3
    assert res["mismatches"] == [{"symbol": "BETAIND", "day": "2024-01-16", "bhavcopy": "2491.3500",
                                  "history": "2491.40"}]  # fmt: skip
    assert res["missing"] == [{"symbol": "ALPHACO", "day": "2024-01-16"}]


async def test_the_sample_is_reproducible_and_capped(stored):
    from finresearch.refseries import equity_eod as store

    a, b = [], []
    await store.reconcile_history(2, seed=7, fetch=fetcher(CLOSES, a), now=lambda: EVENING)
    await store.reconcile_history(2, seed=7, fetch=fetcher(CLOSES, b), now=lambda: EVENING)
    assert len(a) == 2 and a == b


async def test_it_never_starts_in_market_hours(stored):
    from finresearch.refseries import equity_eod as store

    calls: list = []
    market = datetime(2026, 10, 21, 5, 0, tzinfo=UTC)  # 10:30 IST, a Wednesday
    res = await store.reconcile_history(10, seed=1, fetch=fetcher(CLOSES, calls), now=lambda: market)
    assert "market hours" in res["stopped"] and calls == []


async def test_two_refusals_in_a_row_stop_it(stored):
    from finresearch.refseries import equity_eod as store

    calls: list = []

    async def refuse(symbol, day):
        calls.append(symbol)
        raise NseError("NSE HTTP 403 for https://www.nseindia.com/api/x")

    res = await store.reconcile_history(10, seed=1, fetch=refuse, now=lambda: EVENING)
    assert len(calls) == 2 and "refused" in res["stopped"] and len(res["errors"]) == 2 and res["matches"] == 0


def test_the_cli_commands_pass_their_options(monkeypatch):
    from typer.testing import CliRunner

    from finresearch import cli
    from finresearch.refseries import equity_eod as store

    seen: dict = {}

    async def backfill(start, end=None, **kw):
        seen["backfill"] = (start, end, kw["pause"], kw["keep_raw"])
        return {"planned": 0}

    async def reconcile(n, **kw):
        seen["audit"] = (n, kw["seed"])
        return {"mismatches": [{"symbol": "X"}]}

    monkeypatch.setattr(store, "backfill", backfill)
    monkeypatch.setattr(store, "reconcile_history", reconcile)
    r = CliRunner().invoke(
        cli.app, ["equity", "backfill", "--from", "2025-10-01", "--delay", "3", "--no-keep-raw"]
    )
    assert r.exit_code == 0 and seen["backfill"] == (date(2025, 10, 1), None, 3.0, False)
    r = CliRunner().invoke(cli.app, ["audit", "equity-eod", "--sample", "50", "--seed", "4"])
    assert r.exit_code == 1 and seen["audit"] == (50, 4)  # a mismatch fails the command
