"""Live data the F&O book reads, behind one replaceable object (tests swap `SOURCES` for fixtures; no network there).

* chain(symbol, expiry): the option chain, Groww first then NSE (`adapters.groww_fno.GrowwFirstFno`).
* settlement(symbol, expiry): the underlying's close on the expiry day, the price NSE settles index options at and
  stock options are exercised at (NSE F&O final settlement: the closing price of the underlying on expiry day). Index
  closes from NSE's index history, stock closes from NSE's (unadjusted) equity history. None until it is published.
* board_meetings(symbol): NSE board meetings (results dates) of a stock underlying.
* expiries(symbol): the contract's live expiries (NSE contract info).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from finresearch.adapters.nse_fno import INDEX_NAMES

NSE_SETTLEMENT_NOTE = "NSE: the underlying's closing price on the expiry day"


class LiveSources:
    async def chain(self, symbol: str, expiry: date) -> Any:
        from finresearch.adapters.groww_fno import GrowwFirstFno

        async with GrowwFirstFno() as f:
            return await f.option_chain(symbol, expiry)

    async def expiries(self, symbol: str) -> list[date]:
        from finresearch.adapters.groww_fno import GrowwFirstFno

        async with GrowwFirstFno() as f:
            exp, _ = await f.contract_info(symbol)
        return exp

    async def settlement(self, symbol: str, expiry: date) -> tuple[float, str] | None:
        from finresearch.adapters.nse_equity import NseEquity

        sym = symbol.upper()
        async with NseEquity() as eq:
            if sym in INDEX_NAMES:
                bars = await eq.index_history(INDEX_NAMES[sym], expiry - timedelta(days=7), expiry)
                src = f"NSE index history ({INDEX_NAMES[sym]}) close; {NSE_SETTLEMENT_NOTE}"
            else:
                bars = await eq.history(sym, expiry - timedelta(days=7), expiry)
                src = f"NSE equity history close (unadjusted); {NSE_SETTLEMENT_NOTE}"
        for b in bars:
            if b.day == expiry and b.close:
                return float(b.close), src
        return None

    async def board_meetings(self, symbol: str) -> list[Any]:
        from finresearch.adapters.nse_equity import NseEquity

        async with NseEquity() as eq:
            return await eq.board_meetings(symbol)


SOURCES: Any = LiveSources()
