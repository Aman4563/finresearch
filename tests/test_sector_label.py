"""ETFs, funds and SGBs are grouped by what they hold, never as a sector of their own (#209)."""

from __future__ import annotations

from types import SimpleNamespace

from finresearch.portfolio.limits import (
    COMMODITY_SECTOR,
    DEBT_ETF_SECTOR,
    FUNDS_SECTOR,
    is_real_sector,
    sector_label,
)

ETF_INDUSTRY = "Mutual Fund Scheme - ETF"  # what NSE's quote gives for an ETF


def test_etfs_are_grouped_by_what_they_hold():
    assert (
        sector_label("stock", ETF_INDUSTRY, "SILVERBEES", "Example Silver ETF", "other_mf")
        == COMMODITY_SECTOR
    )
    assert (
        sector_label("stock", ETF_INDUSTRY, "GOLDBEES", "Example Gold BeES", "other_mf") == COMMODITY_SECTOR
    )
    assert (
        sector_label("stock", ETF_INDUSTRY, "LIQUIDBEES", "Example Liquid BeES", "debt_mf") == DEBT_ETF_SECTOR
    )
    assert sector_label("stock", ETF_INDUSTRY, "NIFTYBEES", "Example Nifty 50 BeES", "equity") == FUNDS_SECTOR
    assert sector_label("mf", None, None, "Example Flexi Cap Fund", "equity") == FUNDS_SECTOR
    assert sector_label("stock", None, None, "2.50% Example SGB 2030", "sgb") == COMMODITY_SECTOR


def test_stocks_keep_their_industry_even_with_gold_in_the_name():
    assert sector_label("stock", "Jewellery", "EXGOLD", "Example Gold Jewellers Ltd", "equity") == "Jewellery"
    assert sector_label("stock", None, "EXAMPLE", "Example Ltd", "equity") == "Unclassified"


def test_only_business_sectors_count():
    assert is_real_sector("Public Sector Bank")
    for label in (FUNDS_SECTOR, COMMODITY_SECTOR, DEBT_ETF_SECTOR, "Unclassified", "", None):
        assert not is_real_sector(label)


def test_a_large_etf_does_not_raise_a_sector_flag():
    """Concentration: a 40 % silver ETF and a 40 % Nifty ETF were one 80 % 'Mutual Fund Scheme - ETF' sector; now
    neither is a sector, so only the real 20 % bank sector is checked (below the 25 % rule of thumb)."""
    from finresearch.portfolio.analytics import _sector

    sectors = {
        "NSE:SILVERBEES": ETF_INDUSTRY,
        "NSE:NIFTYBEES": ETF_INDUSTRY,
        "NSE:EXBANK": "Public Sector Bank",
    }
    pos = [SimpleNamespace(key="NSE:SILVERBEES", name="Example Silver ETF", asset_type="stock", tax_class="other_mf"),
           SimpleNamespace(key="NSE:NIFTYBEES", name="Example Nifty 50 BeES", asset_type="stock", tax_class="equity"),
           SimpleNamespace(key="NSE:EXBANK", name="Example Bank Ltd", asset_type="stock", tax_class="equity")]  # fmt: skip
    got = [_sector(p, sectors) for p in pos]
    assert got == [COMMODITY_SECTOR, FUNDS_SECTOR, "Public Sector Bank"]
    assert [g for g in got if is_real_sector(g)] == ["Public Sector Bank"]
