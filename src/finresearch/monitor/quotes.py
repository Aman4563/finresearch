"""The forward quote recorder (#363, P1.1): option quotes at registered times, for a measured cost model.

What: NIFTY and BANKNIFTY, the near and the next listed expiry (Groww's instruments file decides: NIFTY lists weekly
expiries, BANKNIFTY monthly), the 10 listed strikes either side of the one nearest the index LTP, calls and puts:
2 x 2 x 21 x 2 = 168 contracts per snapshot (fnohist.quotes.PER_SNAPSHOT).

When (IST, trading days; an unknown holiday list skips the day, monitor.market_days fails closed): from each of
fnohist.quotes.SNAPSHOT_TIMES (09:30, 12:00, 15:15, 15:25) for WINDOW. A sweep is claimed in `alert_eval_slot`
("quotes:<day>:<HHMM>", monitor.disclosures.claim/finish), so two monitor processes never record it twice. One
attempt per slot: a failed sweep's retry time (disclosures.RETRY_AFTER, 30 minutes) falls after its window closes,
and a quote taken late would no longer be the registered snapshot. Quotes are forward-only data: a missed slot is
lost and shows up in the day's checks.

How: one Groww LTP call for both indices, then one Get Quote call per contract, in sequence. Every call takes a slot
in Groww's shared Live Data bucket (adapters.groww_budget: 0.2 s apart for every process), so the monitor's LTP and
chain reads are never pushed past Groww's 300 a minute; on top of that the recorder waits SPACING_S between its own
calls, so it uses at most 240 of the 300 calls a minute and leaves the rest to the app's other Live Data reads. A
sweep is 169 calls (~50 s), 676 Groww calls a day. Each sweep stores its rows (fnohist.quotes.store) and its
data-quality checks (fnohist.quotes.checks) in the slot's result.

Fallback (`Deps.quotes_nse_fallback`, off unless switched on): when Groww cannot answer a sweep at all (no session,
no instruments file, no index LTP), NSE's option chain for each underlying and expiry: at most 4 requests a sweep,
16 a day, rows `source` "nse" (bid/ask from buyPrice1/sellPrice1, no quantities or depth). NSE's website terms
restrict automated collection (#362), so this is the owner's decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from finresearch.fnohist.quotes import EXPIRIES, STRIKES_EACH_SIDE

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Contract:
    underlying: str
    expiry: date
    strike: Decimal
    kind: str  # CE | PE
    trading_symbol: str | None  # None: not in Groww's instruments file (counted as a missing row)


def pick(strikes: list[Decimal], spot: Decimal) -> list[Decimal]:
    """The listed strike nearest `spot` (ties: the lower) and STRIKES_EACH_SIDE listed strikes either side."""
    s = sorted(strikes)
    if not s:
        return []
    i = min(range(len(s)), key=lambda j: (abs(s[j] - spot), s[j]))
    return s[max(0, i - STRIKES_EACH_SIDE) : i + STRIKES_EACH_SIDE + 1]


def select_contracts(inst: Any, underlying: str, spot: Decimal, today: date) -> list[Contract]:
    """The sweep's contracts for one underlying from Groww's instruments file (adapters.groww_market.Instruments)."""
    by_exp = inst.option_strikes(underlying)
    out = []
    for exp in sorted(e for e in by_exp if e >= today)[:EXPIRIES]:
        for k in pick(list(by_exp[exp]), spot):
            for kind in ("CE", "PE"):
                out.append(Contract(underlying, exp, k, kind, inst.fno_symbol(underlying, exp, k, kind)))
    return out
