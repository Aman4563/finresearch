"""The small interface portfolio alert metrics are computed through (roadmap items 8-9 build the portfolio).

Contract for `finresearch.portfolio` (the portfolio package implements it; until it exists, or while its tables are
missing, every portfolio metric is "unknown" with a reason and no rule fires):

    def alert_metrics(session) -> dict[str, tuple[Decimal | None, str]]
        # {"allocation_drift_pp": (value, source), "ltcg_headroom_inr": (...), "drawdown_pct": (...)}
        # value None = not computable (say why in the source string); keys it does not know are simply absent

Tests (and a coordinator wiring things up) can set `SOURCE` to any callable with that signature.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from decimal import Decimal
from typing import Any

PortfolioSource = Callable[[Any], dict[str, tuple[Decimal | None, str]]]
SOURCE: PortfolioSource | None = None

NOT_SET_UP = "the portfolio is not set up yet (import holdings on /portfolio)"


def _default_source() -> PortfolioSource | None:
    try:
        mod = importlib.import_module("finresearch.portfolio")
    except ModuleNotFoundError:
        return None
    fn = getattr(mod, "alert_metrics", None)
    return fn if callable(fn) else None


def portfolio_metrics(session: Any) -> tuple[dict[str, tuple[Decimal | None, str]], str | None]:
    """(metrics, reason). `reason` says why nothing is available (no portfolio module, no tables yet)."""
    from sqlalchemy.exc import SQLAlchemyError

    fn = SOURCE or _default_source()
    if fn is None:
        return {}, NOT_SET_UP
    try:
        with session.begin_nested():  # a missing table must not poison the caller's transaction
            return dict(fn(session) or {}), None
    except SQLAlchemyError as e:
        return {}, f"{NOT_SET_UP} ({type(e).__name__})"
