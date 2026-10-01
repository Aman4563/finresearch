"""Mutual funds: AMFI parsing (recorded files), scheme search and fund arithmetic (golden values)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finresearch.adapters.amfi import parse_nav_all, parse_nav_history, search_schemes
from finresearch.fincalc import funds

AMFI = Path(__file__).parent / "fixtures" / "amfi"


def test_nav_all_carries_category_and_amc():
    rows = parse_nav_all((AMFI / "NAVAll_trimmed.txt").read_text())
    axis = next(r for r in rows if r.code == "120505")
    assert (axis.name, axis.plan, axis.option, axis.nav, axis.day) == (
        "Axis Midcap Fund", "Direct Plan", "Growth Option", Decimal("138.78"), date(2026, 9, 28))  # fmt: skip
    assert axis.category == "Equity Scheme - Mid Cap Fund" and axis.amc == "Axis Mutual Fund"
    assert axis.isin_growth == "INF846K01EH3" and axis.isin_reinvest is None and axis.is_direct_growth
    children = next(r for r in rows if r.name == "Axis Children's Fund")
    assert children.category == "Children’s Fund - Childrens' Fund" or "Children" in children.category


def test_nav_history_and_search():
    hist = parse_nav_history((AMFI / "navhist_axis_sep2026_trimmed.txt").read_text())
    first = hist[0]
    assert first.code == "120505" and first.nav == Decimal("144.46") and first.day == date(2026, 9, 1)
    rows = parse_nav_all((AMFI / "NAVAll_trimmed.txt").read_text())
    top = search_schemes(rows, "axis midcap")[0]
    assert top.code == "120505"  # direct growth first
    assert search_schemes(rows, "120505")[0].code == "120505" and search_schemes(rows, " ") == []


def test_fund_arithmetic_golden_values():
    assert funds.annualised_return(100, 121, date(2024, 1, 1), date(2025, 12, 31)) == Decimal("0.1000000000")
    assert funds.xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 1100)]) == Decimal("0.1")
    # a flat NAV: 12 instalments of ₹1,000 buy 1,200 units worth ₹12,000, XIRR 0
    navs = [(date(2025, 1, 1) + timedelta(days=i), Decimal(10)) for i in range(400)]
    sip = funds.sip_outcome(navs, 1000, date(2025, 1, 1), date(2025, 12, 31))
    assert (sip.instalments, sip.invested, sip.units, sip.value) == (12, 12000, 1200, 12000) and sip.xirr == 0
    # (1.11^10 - 1.10^10) x 1,00,000 = 24,567.8...
    assert round(funds.expense_drag(100000, 10, "0.12", "0.02", "0.01"), 0) == Decimal("24568")
    with pytest.raises(ValueError):
        funds.xirr([(date(2025, 1, 1), -1000)])


def test_trailing_and_rolling_returns():
    # NAV grows 10% a year exactly on each anniversary; linear in between
    navs, d, v = [], date(2020, 1, 1), Decimal(100)
    for year in range(6):
        navs.append((date(2020 + year, 1, 1), Decimal(100) * Decimal("1.1") ** year))
    # 2022-01-01 -> 2025-01-01 is 1,096 days (29-Feb-2024): actual/365 annualising gives 9.990%, not 10%
    assert round(funds.trailing_return(navs, 3), 5) == Decimal("0.09990")
    assert funds.trailing_return(navs, 10) is None
    stats = funds.rolling_returns(navs, 3, step_days=365)
    assert stats.count == 3 and Decimal("0.0998") < stats.minimum <= stats.maximum < Decimal("0.1001")
    assert stats.share_positive == 1
    del d, v


def test_sharpe_is_zero_when_return_equals_the_risk_free_rate():
    navs = [
        (date(2025, 1, 1), 100),
        (date(2025, 1, 2), 101),
        (date(2025, 1, 3), 100),
        (date(2025, 1, 6), 102),
    ]
    r = [Decimal(101) / 100 - 1, Decimal(100) / 101 - 1, Decimal(102) / 100 - 1]
    mean = sum(r) / 3
    assert abs(funds.sharpe_ratio(navs, mean * 252)) < Decimal("1e-20")
    assert funds.sortino_ratio(navs, 0) > 0


def test_leap_day_anchors_do_not_crash():
    """29 Feb has no anniversary in other years: the N-year date falls back to 28 Feb (like add_months)."""
    navs = [(date(2023, 2, 28) + timedelta(days=i), Decimal(100) + i) for i in range(800)]
    assert funds.trailing_return([n for n in navs if n[0] <= date(2024, 2, 29)], 1) is not None
    assert funds.rolling_returns([n for n in navs if n[0] >= date(2024, 2, 29)], 1) is not None
    from finresearch.fincalc.dates import add_years

    assert add_years(date(2024, 2, 29), -1) == date(2023, 2, 28)
    assert add_years(date(2024, 2, 29), 4) == date(2028, 2, 29)


def test_sip_on_a_day_the_month_lacks_falls_on_month_end():
    """A SIP dated the 31st falls on 30-Apr (the month's last day), not the 28th as before. NAV 10 on 28-Apr and 20
    on 30-Apr and 31-May: two ₹1,000 instalments at 20 buy 50 + 50 = 100 units (the old fallback bought 100 + 50)."""
    from finresearch.fincalc import funds as fx

    navs = [(date(2025, 1, 1), 10), (date(2025, 4, 28), 10), (date(2025, 4, 30), 20), (date(2025, 5, 31), 20)]
    o = fx.sip_outcome(navs, 1000, date(2025, 4, 1), date(2025, 5, 31), 31)
    assert o.instalments == 2 and o.units == Decimal(100) and o.value == Decimal(2000)
    assert o.xirr == Decimal(0)


AMFI_FIX = Path(__file__).parent / "fixtures" / "amfi"

SIX_COL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Flexi Cap Fund)

Example Mutual Fund

999001;INF000X01011;-;Example Flexi Cap Fund - Direct Plan - Growth;105.5512;30-Sep-2026
"""


def test_amfi_columns_come_from_the_header_line():
    """AMFI's older NAVAll had six columns (no Plan/Option): positional parsing silently dropped every row."""
    rows = parse_nav_all(SIX_COL)
    assert len(rows) == 1
    r = rows[0]
    assert (r.code, r.isin_growth, r.isin_reinvest, r.nav, r.day) == (
        "999001",
        "INF000X01011",
        None,
        Decimal("105.5512"),
        date(2026, 9, 30),
    )
    assert r.name == "Example Flexi Cap Fund - Direct Plan - Growth" and r.plan is None
    assert r.category == "Equity Scheme - Flexi Cap Fund" and r.amc == "Example Mutual Fund"
    # the recorded eight-column files read exactly as before
    full = parse_nav_all((AMFI_FIX / "NAVAll_trimmed.txt").read_text())
    assert full and all(x.code.isdigit() for x in full) and any(x.plan for x in full)


async def test_a_navall_block_page_raises_instead_of_reading_as_no_funds():
    import httpx

    from finresearch.adapters.amfi import AmfiClient, AmfiError
    from finresearch.adapters.http import PoliteClient

    async def nosleep(_s):
        return None

    pc = PoliteClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, html="<html>Maintenance</html>")),
                      cache_dir=None, sleep=nosleep, default_rate=0, host_rates={})  # fmt: skip
    async with AmfiClient(pc) as amfi:
        with pytest.raises(AmfiError) as ei:
            await amfi.nav_all()
    await pc.aclose()
    from finresearch.adapters.http import is_transient

    assert is_transient(ei.value)  # never cached as "no data"


async def test_navs_on_keeps_the_last_real_nav_over_a_later_na():
    from finresearch.adapters.amfi import AmfiClient

    text = (AMFI_FIX / "navhist_axis_sep2026_trimmed.txt").read_text()
    rows = parse_nav_history(text)
    code = next(r.code for r in rows if r.nav is not None)
    last = max(r.day for r in rows if r.code == code)
    na = next(line for line in text.splitlines() if line.startswith(code + ";"))
    f = na.split(";")
    f[-2], f[-1] = "N.A.", (last + timedelta(days=1)).strftime("%d-%b-%Y")
    text += "\n" + ";".join(f) + "\n"

    class Fake(AmfiClient):
        async def _text(self, url, params=None):
            return text

    got = (await Fake(client=object()).navs_on(last + timedelta(days=1)))[code]  # type: ignore[arg-type]
    assert got.nav is not None and got.day == last


def test_amfi_probe_day_is_the_last_trading_day():
    import json as _json

    from finresearch.adapters.amfi import probe_day
    from finresearch.adapters.nse_holidays import parse_holidays

    hol = set(parse_holidays(_json.loads((Path(__file__).parent / "fixtures" / "nse" / "holidays_2026_trading_cm.json")
                                         .read_text())))  # fmt: skip
    # Sunday 4-Oct-2026 and Monday 5-Oct: the last trading day is Thursday 1-Oct (Friday 2-Oct is Gandhi Jayanti)
    assert probe_day(date(2026, 10, 4), hol) == date(2026, 10, 1)
    assert probe_day(date(2026, 10, 5), hol) == date(2026, 10, 1)
    assert probe_day(date(2026, 10, 1), hol) == date(2026, 9, 30)


async def test_amc_code_map_is_not_saved_when_the_probe_found_few_amcs(tmp_path):
    from finresearch.adapters.amfi import MIN_AMC_CODES, AmfiClient, SchemeNav

    found = {"n": 3}
    asked: list[int] = []

    class Fake(AmfiClient):
        async def history(self, start, end, amc_code=None):
            asked.append(amc_code)
            if amc_code is not None and amc_code <= found["n"]:
                return [
                    SchemeNav("1", "x", None, None, None, None, Decimal(1), start, None, f"AMC {amc_code}")
                ]
            return []

    amfi = Fake(client=object(), cache_dir=tmp_path)  # type: ignore[arg-type]
    few = await amfi.amc_codes(date(2026, 10, 3))  # a Saturday: only liquid-fund AMCs publish
    assert len(few) == 3 and not (tmp_path / "amfi_amc_codes.json").exists()
    found["n"] = MIN_AMC_CODES + 5
    full = await amfi.amc_codes(date(2026, 10, 1))
    assert len(full) == MIN_AMC_CODES + 5 and (tmp_path / "amfi_amc_codes.json").exists()
    asked.clear()
    assert await amfi.amc_codes(date(2026, 10, 1)) == full and asked == []  # served from the cache


async def test_fund_nav_alerts_probe_a_published_day_with_a_cached_code_map(monkeypatch, tmp_path):
    from finresearch.adapters import amfi as amfi_mod
    from finresearch.alerts import compute
    from finresearch.fincalc import dates

    monkeypatch.setattr(dates, "today_ist", lambda: date(2026, 10, 5))
    seen = {}

    async def scheme_history(self, scheme, start, end, probe):
        seen.update(cache_dir=self.cache_dir, probe=probe, end=end)
        return []

    monkeypatch.setattr(amfi_mod.AmfiClient, "scheme_history", scheme_history)
    await compute._live_navs(object())
    assert seen["cache_dir"] is not None  # the code map persists between alert passes
    assert seen["probe"] < seen["end"] == date(2026, 10, 5)  # never today (NAVs are published in the evening)
