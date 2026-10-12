"""Which holdings a valuation asks a price source about (#293).

A sold-out stock holding (no open lot) has no value to price: quoting it asked NSE (or Groww) about it on every
portfolio load and daily pass. Its realised P&L, dividends and tax come from its transactions and disposals, never
from a quote, so it is left out of the price fetch and the report shows it without a live price.

Closed mutual-fund holdings stay in: every fund is priced from one AMFI NAVAll download (no request per holding), and
that row's AMFI category decides the tax class of the fund's past sales (portfolio.report.snapshot builds the
capital-gains rows from the prices' categories), so dropping it could reclassify a realised gain.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select

OPEN_EPS = Decimal("0.0005")  # the units below which report.snapshot treats a lot as closed


def open_holding_ids(session) -> set[int]:
    from finresearch.db.models import PortfolioLot

    return set(session.scalars(select(PortfolioLot.holding_id).where(PortfolioLot.open_quantity > OPEN_EPS)))


def to_price(holdings: Sequence[Any], open_ids: set[int] | None = None) -> list[Any]:
    """The holdings to fetch prices for: every open holding and every fund; never a closed stock."""
    if open_ids is None:
        from finresearch.db import session_scope

        with session_scope() as s:
            open_ids = open_holding_ids(s)
    return [h for h in holdings if h.id in open_ids or h.asset_type == "mf"]
