"""The stock peer build on the stored full bhavcopy (#373 (a), Settings.peer_price_source = "equity_eod"): peers'
prices are the bhavcopy's official closes, the year-ago close comes from the store when it covers that session, and
NSE's quote is read only to refresh issued shares and the classification (QUOTE_TTL_DAYS, or at once on a sign of a
corporate action). A stock missing from the bhavcopy keeps the old quote path. Synthetic data; expected values are
worked by hand in the comments."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from test_stock_peers import FakeEq, fake_results  # noqa: F401  (the per-stock fakes of the quote path)

from finresearch.adapters import nse_equity_eod as ee

pytestmark = pytest.mark.usefixtures("fake_results")
HEAD = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, AVG_PRICE, "
        "TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER")  # fmt: skip
TODAY = date(
    2026, 9, 28
)  # a Monday; a year earlier is Sunday 28-Sep-2025, so Friday 26-Sep-2025 is that session
SHARES = 4058232462  # INFY's issuedSize in the quote fixture (tests/fixtures/nse/quote_INFY_20260928.json)


def bhav(day: date, *rows: tuple[str, str, str]) -> bytes:
    """A full bhavcopy with (symbol, prev_close, close) EQ rows; other columns are filler."""
    lines = [HEAD] + [f"{s}, EQ, {day:%d-%b-%Y}, {p}, {c}, {c}, {c}, {c}, {c}, {c}, 100, 1.00, 10, 50, 50.00"
                      for s, p, c in rows]  # fmt: skip
    return ("\n".join(lines) + "\n").encode()


def load(day: date, *rows: tuple[str, str, str]) -> None:
    from finresearch.db import session_scope
    from finresearch.refseries import equity_eod as store

    with session_scope() as s:
        store.load_day(s, day, ee.url_for(day), bhav(day, *rows))


@pytest.fixture
def eod(env, monkeypatch):
    from finresearch.db import session_scope
    from finresearch.signals import stock_peers as SP

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE equity_eod, equity_eod_day, dataset_load"))

    wipe()
    monkeypatch.setattr(SP, "PAUSE_S", 0)
    yield SP
    wipe()


async def test_price_from_the_bhavcopy_and_a_quote_only_for_the_metadata(eod):
    SP = eod
    load(date(2025, 9, 26), ("INFY", "780.00", "790.00"))
    load(TODAY, ("INFY", "990.00", "1000.00"))
    eq = FakeEq()
    prices = SP.EodPrices(TODAY, holidays=set())
    row = SP.compute_row("INFY", await SP.fetch_inputs(eq, "INFY", TODAY, eod=prices), TODAY)
    m = row["metrics"]
    assert m["price"]["value"] == 1000.0 and m["price"]["source"] == ee.url_for(TODAY)
    assert m["market_cap"]["value"] == pytest.approx(SHARES * 1000.0)
    assert m["return_1y"]["value"] == pytest.approx(
        1000 / 790 - 1
    )  # the stored year-ago close, not history's 800
    assert eq.calls == [
        "actions",
        "quote",
    ]  # no history request; the quote only filled issued shares + levels
    assert row["basic_industry"] == "Computers - Software & Consulting"
    assert row["price_source"] == "equity_eod"
    assert row["quote_close"]["price"] == pytest.approx(1003.2) and row["quote_close"]["day"] == "2026-09-28"
    assert prices.counts == {"quotes": 1, "history": 0, "fallback": 0}


async def test_the_next_night_needs_no_quote_and_no_history(eod):
    SP = eod
    load(date(2025, 9, 26), ("INFY", "780.00", "790.00"))
    load(date(2025, 9, 29), ("INFY", "790.00", "795.00"))
    load(TODAY, ("INFY", "990.00", "1000.00"))
    first = SP.EodPrices(TODAY, holidays=set())
    row = SP.compute_row("INFY", await SP.fetch_inputs(FakeEq(), "INFY", TODAY, eod=first), TODAY)
    first.save_meta()
    nxt = date(2026, 9, 29)
    load(nxt, ("INFY", "1000.00", "1010.00"))  # previous close = the stored close: no corporate action
    eq = FakeEq()
    prices = SP.EodPrices(nxt, holidays=set())
    again = SP.compute_row("INFY", await SP.fetch_inputs(eq, "INFY", nxt, row, eod=prices), nxt)
    assert eq.calls == [] and prices.counts == {"quotes": 0, "history": 0, "fallback": 0}
    assert again["metrics"]["price"]["value"] == 1010.0
    assert again["metrics"]["market_cap"]["value"] == pytest.approx(SHARES * 1010.0)
    assert again["metrics"]["return_1y"]["value"] == pytest.approx(1010 / 795 - 1)  # Mon 29-Sep-2025's close
    assert "quote_close" not in again


async def test_an_adjusted_previous_close_re_reads_the_quote(eod):
    SP = eod
    load(date(2025, 9, 26), ("INFY", "780.00", "790.00"))
    load(date(2025, 9, 29), ("INFY", "790.00", "795.00"))
    load(TODAY, ("INFY", "990.00", "1000.00"))
    first = SP.EodPrices(TODAY, holidays=set())
    await SP.fetch_inputs(FakeEq(), "INFY", TODAY, eod=first)
    first.save_meta()
    nxt = date(2026, 9, 29)
    load(nxt, ("INFY", "500.00", "505.00"))  # NSE's previous close 500 != stored 1000: a 1:2 split went ex
    eq = FakeEq()
    prices = SP.EodPrices(nxt, holidays=set())
    await SP.fetch_inputs(eq, "INFY", nxt, eod=prices)
    assert eq.calls.count("quote") == 1 and prices.counts["quotes"] == 1


async def test_a_stale_quote_cache_is_re_read(eod):
    SP = eod
    load(TODAY, ("INFY", "990.00", "1000.00"))
    first = SP.EodPrices(TODAY, holidays=set())
    await SP.fetch_inputs(FakeEq(), "INFY", TODAY, eod=first)
    first.save_meta()
    later = date(2026, 11, 2)  # 35 days on, past QUOTE_TTL_DAYS
    load(later, ("INFY", "1000.00", "1000.00"))
    eq = FakeEq()
    await SP.fetch_inputs(eq, "INFY", later, eod=SP.EodPrices(later, holidays=set()))
    assert "quote" in eq.calls


async def test_a_stock_missing_from_the_bhavcopy_keeps_the_quote_path(eod):
    SP = eod
    load(TODAY, ("OTHER", "10.00", "11.00"))
    eq = FakeEq()
    prices = SP.EodPrices(TODAY, holidays=set())
    row = SP.compute_row("INFY", await SP.fetch_inputs(eq, "INFY", TODAY, eod=prices), TODAY)
    assert row["metrics"]["price"]["value"] == pytest.approx(1003.2) and row["price_source"] == "quote"
    assert eq.calls == ["quote", "actions", "history"] and prices.counts == {"quotes": 1, "history": 1,
                                                                             "fallback": 1}  # fmt: skip


async def test_a_year_ago_session_not_in_the_store_uses_the_history_request(eod):
    SP = eod
    load(TODAY, ("INFY", "990.00", "1000.00"))  # nothing stored for Sep-2025
    eq = FakeEq()
    prices = SP.EodPrices(TODAY, holidays=set())
    row = SP.compute_row("INFY", await SP.fetch_inputs(eq, "INFY", TODAY, eod=prices), TODAY)
    assert "history" in eq.calls and prices.counts["history"] == 1
    assert row["metrics"]["return_1y"]["value"] == pytest.approx(1000 / 800 - 1)  # FakeEq's history bar


async def test_an_unknown_holiday_list_never_trusts_the_store_for_the_year_ago_close(eod):
    SP = eod
    load(date(2025, 9, 26), ("INFY", "780.00", "790.00"))
    load(TODAY, ("INFY", "990.00", "1000.00"))
    eq = FakeEq()
    prices = SP.EodPrices(TODAY, holidays=None)
    prices.holidays_for = lambda _d: None  # the year's NSE holiday list is not loaded
    await SP.fetch_inputs(eq, "INFY", TODAY, eod=prices)
    assert "history" in eq.calls


async def test_build_reports_the_request_counts_and_refresh_waits_for_todays_file(eod, monkeypatch):
    SP = eod
    load(date(2025, 9, 26), ("INFY", "780.00", "790.00"))
    load(TODAY, ("INFY", "990.00", "1000.00"))
    data = await SP.build(FakeEq(), TODAY, ["INFY", "MISSING"], eod=SP.EodPrices(TODAY, holidays=set()))
    assert data["price_source"] == "equity_eod"
    assert data["requests"] == {"quotes": 2, "history": 1, "fallback": 1}  # MISSING went the quote way
    from finresearch.config import get_settings

    monkeypatch.setattr(get_settings(), "peer_price_source", "equity_eod")
    with pytest.raises(RuntimeError, match="not loaded"):
        await SP.refresh(date(2026, 9, 29))  # no file stored for that day: the monitor retries later
