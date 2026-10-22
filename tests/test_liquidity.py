"""Days to liquidate (#348, portfolio.liquidity): position units ÷ (participation × average daily traded shares) from
the stored NSE full bhavcopy. Expected values by hand; synthetic symbols; test database only."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from finresearch.portfolio import liquidity as lq

END = date(2026, 3, 31)
LOADED = [END - timedelta(days=i) for i in range(28) if (END - timedelta(days=i)).weekday() < 5][:20]


def pos(key, units, value, asset_type="stock", nse_symbol=None):
    return SimpleNamespace(key=key, name=key.split(":")[1] + " Ltd", asset_type=asset_type, units=units,
                           value=value, nse_symbol=nse_symbol)  # fmt: skip


def test_adv_needs_enough_days_with_trades():
    rows = {d: (40_000 if i < 10 else 60_000, Decimal("100")) for i, d in enumerate(LOADED)}
    a = lq.adv(LOADED, rows)
    # (10 × 40,000 + 10 × 60,000) / 20 = 50,000 shares; turnover 100 lakh = ₹1 crore a day
    assert (a.shares, a.rupees, a.days, a.days_with_rows) == (50_000, 10_000_000, 20, 20) and a.reason is None
    few = lq.adv(LOADED, {d: rows[d] for d in LOADED[:14]})
    assert few.shares is None and few.reason == "traded on 14 of the last 20 stored sessions; needs 15"
    none = lq.adv(LOADED[:9], rows)
    assert none.shares is None and none.reason == "only 9 sessions of NSE volume are stored; needs 15"
    unknown_qty = lq.adv(
        LOADED, {**rows, LOADED[0]: (None, None)}
    )  # a '-' cell is unknown, not a zero-volume day
    assert unknown_qty.days_with_rows == 19 and unknown_qty.shares == pytest.approx(
        (9 * 40_000 + 10 * 60_000) / 19
    )


def test_days_to_liquidate_by_hand():
    advs = {"AAA": lq.Adv(50_000, 10_000_000, 20, 20), "BBB": lq.Adv(None, None, 20, 3, "traded on 3 of 20")}
    out = lq.liquidity([pos("NSE:AAA", 30_000, 600_000, nse_symbol="AAA"),
                        pos("NSE:BBB", 100, 100_000, nse_symbol="BBB"),
                        pos("BSE:500001", 10, 50_000),
                        pos("MF:111", 1000, 250_000, asset_type="mf")], advs, participation=0.2)  # fmt: skip
    rows = {r["key"]: r for r in out["positions"]}
    # 30,000 / (0.2 × 50,000) = 3 trading days
    assert rows["NSE:AAA"]["days"] == pytest.approx(3.0) and rows["NSE:AAA"]["adv_shares"] == 50_000
    assert rows["NSE:AAA"]["pct_of_adv"] == pytest.approx(60.0)  # 30,000 is 60 % of one day's volume
    assert rows["NSE:BBB"]["days"] is None and rows["NSE:BBB"]["reason"] == "traded on 3 of 20"
    assert rows["BSE:500001"]["days"] is None and "no NSE volume" in rows["BSE:500001"]["reason"]
    assert rows["MF:111"]["days"] is None and rows["MF:111"]["status"] == "not_applicable"
    s = out[
        "summary"
    ]  # value 10,00,000: AAA 6 lakh within 5 days; BBB + BSE unknown 1.5 lakh; the fund 2.5 lakh n/a
    assert s["within"] == {"1": 0.0, "5": 0.6, "20": 0.6}
    assert s["unknown"] == pytest.approx(0.15) and s["not_applicable"] == pytest.approx(0.25)
    assert s["max_days"] == pytest.approx(3.0) and s["complete"] is False
    assert out["participation"] == 0.2


def test_participation_must_be_a_fraction():
    with pytest.raises(ValueError):
        lq.liquidity([], {}, participation=0)
    with pytest.raises(ValueError):
        lq.liquidity([], {}, participation=1.5)


def test_read_advs_from_the_store(env):
    from finresearch.db import session_scope
    from finresearch.db.models import EquityEod, EquityEodDay

    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day"))
        for i, d in enumerate(LOADED):
            s.add(EquityEodDay(day=d, status="loaded", rows=2))
            s.add(EquityEod(day=d, symbol="AAA", series="EQ", close=Decimal(100), qty=1000 * (i + 1),
                            turnover_lacs=Decimal("1.5")))  # fmt: skip
            s.add(
                EquityEod(day=d, symbol="AAA", series="N1", close=Decimal(1000), qty=999_999)
            )  # a bond: not stock
        s.add(EquityEodDay(day=END + timedelta(days=1), status="no_file"))  # not a session
        s.add(EquityEodDay(day=END - timedelta(days=60), status="loaded", rows=1))  # older than the last 20
    with session_scope() as s:
        got = lq.read_advs(s, ["AAA", "ZZZ"], END)
    # mean of 1,000 … 20,000 = 10,500; 1.5 lakh = ₹1,50,000
    assert got["AAA"].shares == pytest.approx(10_500) and got["AAA"].rupees == pytest.approx(150_000)
    assert got["AAA"].first == LOADED[-1] and got["AAA"].last == LOADED[0]
    assert (
        got["ZZZ"].shares is None
        and got["ZZZ"].reason == "traded on 0 of the last 20 stored sessions; needs 15"
    )
    with session_scope() as s:
        s.execute(text("TRUNCATE equity_eod, equity_eod_day"))
