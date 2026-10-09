"""#264: the look-through "true top holdings" table and its largest-sector figure read each direct holding's raw
sector (NSE's "Mutual Fund Scheme - ETF" industry, BSE's "-"), while every other view groups by
portfolio.limits.sector_label. Synthetic holdings only (made-up names and ISINs)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest


@pytest.fixture
def db(env):
    from sqlalchemy import text

    from finresearch.db import session_scope

    with session_scope() as s:
        s.execute(text("TRUNCATE portfolio_disposal, portfolio_lot, portfolio_txn, portfolio_holding, portfolio_import, "
                       "portfolio_snapshot, portfolio_setting CASCADE"))  # fmt: skip
    yield session_scope


def _buy(s, name, symbol, isin):
    from finresearch.portfolio.service import manual_txn

    return manual_txn(s, {"asset_type": "stock", "name": name, "account": "Manual", "isin": isin, "nse_symbol": symbol,
                          "day": date(2024, 1, 2), "kind": "buy", "quantity": D(10), "price": D(100)}).holding_id  # fmt: skip


def test_lookthrough_direct_holdings_use_the_display_sector(db, tmp_path):
    from finresearch.portfolio.limits import FUNDS_SECTOR, UNCLASSIFIED_SECTOR
    from finresearch.portfolio.lookthrough import PortfolioStore, lookthrough_exposure
    from finresearch.portfolio.report import snapshot
    from finresearch.portfolio.valuation import PriceInfo

    today = date(2026, 9, 30)
    with db() as s:
        etf = _buy(s, "Example Nifty 50 ETF", "EXNIFTYBEES", "INF000X01019")
        bse = _buy(s, "Example Bse Only Ltd", "EXBSEONLY", "INE000X01017")
        it = _buy(s, "Example Software Ltd", "EXSOFT", "INE000Y01015")
        prices = {etf: PriceInfo(price=D(250), industry="Mutual Fund Scheme - ETF"),
                  bse: PriceInfo(price=D(120), industry="-"),
                  it: PriceInfo(price=D(130), industry="Computers - Software & Consulting")}  # fmt: skip
        rows = {r["name"]: r for r in snapshot(s, prices, today)["holdings"]}
        # the raw field is kept (the holding editor round-trips the user's own label); the display label is added
        assert rows["Example Nifty 50 ETF"]["sector"] == "Mutual Fund Scheme - ETF"
        assert rows["Example Nifty 50 ETF"]["sector_label"] == FUNDS_SECTOR
        assert rows["Example Bse Only Ltd"]["sector_label"] == UNCLASSIFIED_SECTOR
        assert rows["Example Software Ltd"]["sector_label"] == "Computers - Software & Consulting"
        out = lookthrough_exposure(s, prices, today, store=PortfolioStore(tmp_path), amfi={})
    by_name = {x["name"]: x["sector"] for x in out["stocks"]}
    labels = {x["label"] for x in out["sectors"]}
    assert "Mutual Fund Scheme - ETF" not in labels and "-" not in labels
    assert by_name.get("Example Bse Only Ltd") == UNCLASSIFIED_SECTOR
    assert by_name.get("Example Software Ltd") == "Computers - Software & Consulting"
    # an ETF held directly is grouped as a fund, not as a sector
    assert by_name["Example Nifty 50 ETF"] == FUNDS_SECTOR
