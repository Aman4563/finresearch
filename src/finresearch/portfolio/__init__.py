"""Personal portfolio: imports (CAS, tradebooks, manual), FIFO lots, valuation, capital-gains tax (roadmap 8-9).

Personal data: local database and data/portfolio/ only; never sent to an LLM.
"""

from __future__ import annotations

from typing import Any


def alert_metrics(session: Any):  # the finresearch.alerts.portfolio contract
    from finresearch.portfolio.metrics import alert_metrics as _metrics

    return _metrics(session)
