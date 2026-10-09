"""F&O data with Groww first (#267): the option chain from the user's Groww Trade API when the Groww connection is on
and its session valid, NSE's option chain otherwise or on any Groww error. Expiries, strikes, lot sizes and the
underlying's closes stay on NSE (`NseFno`), which publishes them for everyone. Analysis only: nothing here can place
an order (Groww requests go through adapters.groww_market's GET-only allowlist)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from finresearch.adapters.nse_fno import NseFno, OptionChain

log = logging.getLogger(__name__)


class GrowwFirstFno(NseFno):
    def __init__(self, client: Any = None, market: Any = None) -> None:
        super().__init__(client)
        self.market = market

    async def option_chain(self, symbol: str, expiry: date) -> OptionChain:
        from finresearch.adapters import groww_market

        market = self.market or groww_market.MARKET
        if market.token():
            try:
                chain = await market.option_chain(symbol, expiry)
                return chain.model_copy(update={"source": groww_market.SOURCE})
            except Exception as e:  # Groww failed or has no chain: NSE answers
                log.info("Groww option chain for %s failed (%s): using NSE", symbol, type(e).__name__)
        return await super().option_chain(symbol, expiry)
