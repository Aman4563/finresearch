"""Exchange disclosures (finresearch.disclosures): surveillance and pledge red flags, insider / SAST / bulk and block
deals, credit-rating actions and SEBI orders, per stock, per bond and for everything you hold or watch.

A stock page's GET reads NSE for that stock when the database has no good read from the last 20 hours (`refresh=0`
serves only what is stored; `retry=1` skips the 30-second wait after a failure). The tracked overview and the brief
read the database only: the monitor refreshes it after each close and before the open. Every part says "ok" (with
its source URL and as-of date) or "unavailable" (with the reason) — an unread source never shows as "none".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException

from finresearch.db import session_scope

ISIN_RE = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")


def _isin(v: str | None) -> str | None:
    if v is None or v == "":
        return None
    v = v.strip().upper()
    if not ISIN_RE.match(v):
        raise HTTPException(422, f"{v!r} is not an Indian ISIN")
    return v


def add_disclosure_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    @app.get("/api/stocks/{symbol}/disclosures")
    async def stock_disclosures(symbol: str, isin: str | None = None, refresh: bool = True,
                                retry: bool = False) -> dict[str, Any]:  # fmt: skip
        """Red flags (ASM / GSM stage, F&O ban, promoter pledge and its change), insider trades with the 90-day net,
        SAST Regulation 29 disclosures, bulk/block deals, rating actions on the issuer and SEBI orders that may name
        it. Public market data."""
        from finresearch.disclosures import refresh as R
        from finresearch.disclosures import views
        from finresearch.signals.stock import instrument_key

        try:
            key = instrument_key(symbol)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        isin = _isin(isin)
        t = now()
        refreshed: dict[str, Any] = {}
        if refresh and not key.startswith("BSE:"):
            refreshed = await R.ensure_fresh(key, t, isin=isin, retry=retry)
        with session_scope() as s:
            out = views.stock(s, key, t, isin=isin)
        out["refreshed"] = {k: sorted(v) for k, v in refreshed.items()}
        return out

    @app.get("/api/bonds/{isin}/rating-actions")
    async def bond_ratings(isin: str, refresh: bool = True, retry: bool = False) -> dict[str, Any]:
        """Credit-rating filings for this bond and its issuer's other instruments (agency, rating, action, outlook),
        and SEBI orders that may name the issuer."""
        from finresearch.disclosures import refresh as R
        from finresearch.disclosures import views

        code = _isin(isin)
        assert code is not None
        t = now()
        if refresh:
            await R.ensure_fresh(None, t, retry=retry)
        with session_scope() as s:
            return views.bond(s, code, t)

    @app.get("/api/disclosures/tracked")
    def tracked() -> dict[str, Any]:
        """Flags for every held or watched NSE stock and every tracked bond, from the database (no network)."""
        from finresearch.disclosures import views

        with session_scope() as s:
            return views.tracked_overview(s, now())

    @app.get("/api/disclosures/status")
    def status() -> dict[str, Any]:
        """Each market-wide feed's state: last good read, as-of, last error."""
        from finresearch.disclosures import store, views

        t = now()
        with session_scope() as s:
            return {ds: views.feed_state(store.feed(s, ds), t, ds) for ds in
                    ("asm", "gsm", "fno_ban", "credit_ratings", "sebi_orders")}  # fmt: skip
