"""/api/portfolio: the personal portfolio (imports, holdings, lots, P&L, allocation) and its capital-gains tax view.

Privacy: this is the user's own financial data. It stays in the local database and under settings.portfolio_dir
(data/portfolio/, gitignored) and is never sent to an LLM. Uploads are JSON bodies with the file base64-encoded;
the CAS password travels only in the body of that one request (never a query string, which access logs record), is
handed to casparser and dropped: it is not stored, logged or echoed in any response, including validation errors
(the body is validated by hand, because FastAPI's default 422 echoes the offending input).

The POST/PUT/DELETE routes sit behind the app's CSRF/Origin guard like every other unsafe route.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from finresearch.config import get_settings
from finresearch.db import session_scope

log = logging.getLogger(__name__)
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
_MISS = object()
PRICE_TTL_S = 600
# The whole live price fetch of one request (stream or page) at most; rows still unpriced then show their reason.
PRICE_BUDGET_S = 45.0
# A quote that just failed is answered with the same error for this long: the page's stream is followed at once by the
# full valuation, which would otherwise re-fetch every failed instrument (doubling a slow first load). An error is
# never stored as a price, and it expires in seconds (no "no data" kept for the 10-minute price TTL).
FAILED_TTL_S = 30.0


class RebalanceParams(BaseModel):
    new_money: float = Field(0, ge=0, le=1e11)  # ₹ to invest now (a SIP instalment or a lump sum)
    # None = the profile's band (portfolio.limits.bands: 5 pp / 25 % unless the user set their own), as /wealth uses
    band_abs_pp: float | None = Field(None, ge=0.5, le=50)  # absolute band, percentage points
    band_rel_pct: float | None = Field(
        None, ge=0, le=100
    )  # relative band, % of the target (0 = absolute only)


class ManualTxn(BaseModel):
    holding_id: int | None = None
    asset_type: Literal["stock", "mf", "other"] = "stock"
    name: str = Field("", max_length=300)
    account: str = Field("Manual", max_length=80)
    isin: str | None = Field(None, max_length=12)
    nse_symbol: str | None = Field(None, max_length=30)
    bse_code: str | None = Field(None, max_length=20)
    scheme_code: str | None = Field(None, max_length=20)
    day: date
    kind: Literal["buy", "sell", "dividend", "bonus", "split", "opening"]
    quantity: Decimal | None = Field(None, gt=0)
    price: Decimal | None = Field(None, ge=0)
    amount: Decimal | None = Field(None, ge=0)
    charges: Decimal = Field(Decimal(0), ge=0)
    stt_paid: bool = True
    note: str | None = Field(None, max_length=2000)
    meta: dict[str, Any] = Field(default_factory=dict)
    allow_duplicate: bool = False  # add it although another source has the same trade (a second, real trade)

    @field_validator("meta")
    @classmethod
    def _meta_keys(cls, v: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "a",
            "b",
            "from",
            "to",
            "acquired",
            "rbi_redemption",
            "held_to_maturity",
            "resolves_action",
        }
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"unsupported meta keys: {sorted(bad)}")
        return v


class ResolveAction(BaseModel):
    key: str = Field(max_length=80)  # "<type>:<ex-date>", as GET /api/portfolio/holdings/{id} lists it
    note: str = Field(max_length=2000)


class TxnUpdate(BaseModel):
    day: date | None = None
    kind: Literal["buy", "sell", "dividend", "bonus", "split", "opening"] | None = None
    quantity: Decimal | None = Field(None, gt=0)
    price: Decimal | None = Field(None, ge=0)
    amount: Decimal | None = Field(None, ge=0)
    charges: Decimal | None = Field(None, ge=0)
    stt_paid: bool | None = None
    note: str | None = Field(None, max_length=2000)
    meta: dict[str, Any] | None = None


class HoldingUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=300)
    nse_symbol: str | None = Field(None, max_length=30)
    bse_code: str | None = Field(None, max_length=20)
    scheme_code: str | None = Field(None, max_length=20)
    isin: str | None = Field(None, max_length=12)
    sector: str | None = Field(None, max_length=120)
    tax_class: Literal["equity", "debt_mf", "other_mf", "sgb", "other"] | None = None
    fmv_2018: Decimal | None = Field(None, ge=0)
    flags: dict[str, bool] | None = None


def _decode(content_b64: Any) -> bytes:
    if not isinstance(content_b64, str) or not content_b64:
        raise HTTPException(422, "content_b64: the file, base64-encoded, is required")
    if len(content_b64) > MAX_UPLOAD_BYTES * 4 // 3 + 8:
        raise HTTPException(413, f"file too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
    try:
        return base64.b64decode(content_b64.split(",", 1)[-1] if content_b64.startswith("data:") else content_b64,
                                validate=True)  # fmt: skip
    except (binascii.Error, ValueError) as e:
        raise HTTPException(422, "content_b64 is not valid base64") from e


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(422, "the body must be JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(422, "the body must be a JSON object")
    return body


def add_portfolio_routes(app: FastAPI, *, scheme_rows: Callable[[], Awaitable[list]]) -> None:
    from finresearch.api.markets import MarketSources, TtlCache

    cache = TtlCache()
    # quote key -> (expiry, the error), see FAILED_TTL_S
    failed: dict[tuple, tuple[float, BaseException]] = {}

    def src() -> MarketSources:
        s = getattr(app.state, "markets", None)
        if s is None:
            s = app.state.markets = MarketSources(listings=getattr(app.state, "listings", None))
        return s

    def _cached(key: tuple) -> Any:
        hit = cache.entries.get(key)
        return hit[1] if hit and hit[0] > time.time() else _MISS

    async def _prices(
        holdings: list[Any], *, cached_only: bool = False, on_price=None
    ) -> tuple[dict[int, Any], set[int]]:
        """(prices, ids still pending). Live: one shared NSE/BSE session for the whole valuation (QuoteBatch) behind
        the 10-minute quote cache and the 6-hour NAVAll cache. `cached_only`: no network at all; holdings without a
        cached price are returned as pending (the page shows cost basis at once, then streams the prices)."""
        from finresearch.portfolio.valuation import QuoteBatch, fetch_prices

        sources = src()
        pending: set[tuple] = set()

        async with QuoteBatch() as batch:

            async def live(sym: str, exch: str) -> Any:
                if (exch == "NSE" and sources.quote is not None) or (
                    exch == "BSE" and sources.bse_quote is not None
                ):
                    return await sources.get_quote(sym, exch)  # test seams / injected fakes
                return await batch.quote(sym, exch)

            async def quote(sym: str, exch: str) -> Any:
                key = ("quote", exch, sym)
                recent = failed.get(key)
                if recent is not None and recent[0] > time.time():
                    raise recent[1]
                if cached_only:
                    got = _cached(key)
                    if got is _MISS:
                        pending.add(key)
                        raise LookupError("price not cached yet")
                    return got
                try:
                    return await cache.get(key, PRICE_TTL_S, lambda: live(sym, exch))
                except Exception as e:
                    failed[key] = (time.time() + FAILED_TTL_S, e)
                    raise

            async def rows() -> list:
                if cached_only:
                    got = _cached(("navall",))
                    if got is _MISS:
                        pending.add(("navall",))
                        raise LookupError("NAVs not cached yet")
                    return got
                return await cache.get(("navall",), 6 * 3600, scheme_rows)

            from finresearch.adapters.groww_market import Prefetch

            # Groww first when connected (#267); the instant first paint reads only what Groww data is cached
            prices = await fetch_prices(holdings, quote=quote, scheme_rows=rows, listings=sources.listings,
                                        on_price=on_price, budget_s=None if cached_only else PRICE_BUDGET_S,
                                        prefetch=Prefetch(cached_only=cached_only))  # fmt: skip
        waiting: set[int] = set()
        if pending:
            from finresearch.portfolio.valuation import instrument_of

            for h in holdings:
                if h.asset_type == "mf" and ("navall",) in pending:
                    waiting.add(h.id)
                elif h.asset_type == "stock":
                    sym, exch, _ = instrument_of(h)
                    if ("quote", exch, sym) in pending:
                        waiting.add(h.id)
        return prices, waiting

    def _detached_holdings() -> list[Any]:
        from finresearch.db.models import PortfolioHolding

        with session_scope() as s:
            hs = list(s.scalars(select(PortfolioHolding)))
            s.expunge_all()
            return hs

    def _slab() -> Decimal:
        from finresearch.suggest.advisor import load_profile

        with session_scope() as s:
            return Decimal(str(load_profile(s).tax_slab_pct)) / 100

    # ------------------------------------------------------------------ read
    @app.get("/api/portfolio")
    async def portfolio(prices: Literal["live", "cached"] = "live") -> dict[str, Any]:
        """Holdings with live valuation, P&L and XIRR; allocation; invested/realised over time; dividends.

        `prices=cached` answers at once from the 10-minute price cache without any network call: holdings whose price
        is not cached come back with `pending: true` (cost basis only) and `pending` counts them; the page then reads
        /api/portfolio/prices/stream and finally this route again (by then every price is cached).

        Read-only (#247): the day's value history is recorded by the monitor's daily pass and by
        POST /api/portfolio/snapshot, which the page calls once every price is in."""
        from finresearch.portfolio.metrics import drift, get_targets
        from finresearch.portfolio.report import snapshot

        cached_only = prices == "cached"
        got, waiting = await _prices(_detached_holdings(), cached_only=cached_only)
        today = src().today()
        with session_scope() as s:
            snap = snapshot(s, got, today)
            for row in snap["holdings"]:
                row["pending"] = row["id"] in waiting
                if row["pending"]:
                    row["price_error"] = None
            snap["pending"] = len(waiting)
            by_asset = {r["label"]: r["value"] for r in snap["allocation"]["asset"]}
            targets = get_targets(s)
            snap["targets"] = targets
            snap["drift"] = drift(by_asset, targets)
        snap["lookthrough_coverage"] = _coverage(snap["holdings"], today)
        return snap

    @app.post("/api/portfolio/snapshot")
    async def portfolio_snapshot() -> dict[str, Any]:
        """Record today's value in the history behind the drawdown and drift alerts (the latest valuation of a day
        wins). Uses cached prices only (no network): nothing is recorded while any holding has no cached price, as
        GET /api/portfolio once did on every load (#247 moved the write here)."""
        from finresearch.portfolio.metrics import record_snapshot, valuation_gaps
        from finresearch.portfolio.report import snapshot

        got, waiting = await _prices(_detached_holdings(), cached_only=True)
        today = src().today()
        with session_scope() as s:
            snap = snapshot(s, got, today)
            if waiting:
                return {"recorded": False, "reason": f"{len(waiting)} holding(s) have no current price yet"}
            if not snap["summary"]["value"]:
                return {"recorded": False, "reason": "no holding has a value"}
            by_asset = {r["label"]: r["value"] for r in snap["allocation"]["asset"]}
            record_snapshot(s, today, snap["summary"]["value"], snap["invested"], by_asset, snap["complete"],
                            valuation_gaps(s, snap))  # fmt: skip
            return {"recorded": True, "day": today.isoformat(), "complete": snap["complete"]}

    def _coverage(rows: list[dict[str, Any]], today: Any) -> dict[str, Any] | None:
        """The share of the portfolio in funds not looked through (#214), for the sector and cap cards; None when the
        look-through store cannot be read (the card then says coverage is unknown)."""
        from finresearch.portfolio.lookthrough import PortfolioStore, coverage_from_rows

        try:
            return coverage_from_rows(rows, PortfolioStore(), today)
        except Exception:
            log.warning("look-through coverage failed", exc_info=True)
            return None

    @app.get("/api/portfolio/targets")
    def targets() -> dict[str, Any]:
        from finresearch.portfolio.metrics import ASSET_CLASSES, get_targets

        with session_scope() as s:
            return {"targets": get_targets(s), "classes": list(ASSET_CLASSES)}

    @app.get("/api/portfolio/prices/stream")
    async def price_stream() -> StreamingResponse:
        """Newline-delimited JSON: one {"type": "price", "id", "price", "as_of", "source", "error", "note"} line per holding as
        its price arrives (concurrent, rate-limited fetches that fill the 10-minute cache), then {"type": "done"}."""
        import asyncio
        import json

        holdings = _detached_holdings()
        queue: asyncio.Queue = asyncio.Queue()

        def on_price(hid: int, p: Any) -> None:
            queue.put_nowait({"type": "price", "id": hid, "price": None if p.price is None else float(p.price),
                              "as_of": p.as_of, "source": p.source, "error": p.error,
                              "note": p.note})  # fmt: skip

        async def run() -> None:
            try:
                await _prices(holdings, on_price=on_price)
            finally:
                queue.put_nowait({"type": "done", "count": len(holdings)})

        async def body():
            task = asyncio.create_task(run())
            try:
                while True:
                    item = await queue.get()
                    yield json.dumps(item) + "\n"
                    if item["type"] == "done":
                        break
            finally:
                await task

        return StreamingResponse(
            body(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"}
        )

    @app.put("/api/portfolio/targets")
    def put_targets(body: dict[str, float | None]) -> dict[str, Any]:
        """Target allocation, % by asset class (must add up to 100; an empty body clears them). Drives the drift
        alert metric."""
        from finresearch.portfolio.metrics import ASSET_CLASSES, set_targets

        with session_scope() as s:
            return {"targets": set_targets(s, body), "classes": list(ASSET_CLASSES)}

    def _elss_view(s: Any, h: Any, lots: list[Any]) -> dict[str, Any] | None:
        """The ELSS lock-in of a fund holding (portfolio.elss), or None. Category and NAV come from the NAVAll rows
        already in the price cache (no network here), else from the monitor's daily valuation."""
        from finresearch.portfolio import cache as pf_cache
        from finresearch.portfolio import elss
        from finresearch.portfolio.valuation import fund_prices

        if h.asset_type != "mf":
            return None
        category, price = None, None
        rows = _cached(("navall",))
        if rows is not _MISS:
            p = fund_prices([h], rows, None)[h.id]
            if p.source == "AMFI NAVAll":
                category, price = p.category, p.price
        v = pf_cache.read(s, pf_cache.VALUATION)
        if price is None:
            got = ((v.get("holdings") or {}).get(str(h.id)) or {}).get("price")
            price = Decimal(str(got)) if got is not None else None
        det = elss.detect(h.asset_type, h.name, elss.category_of(h, v, category))
        return None if det is None else elss.lockin(lots, src().today(), price, det)

    @app.get("/api/portfolio/holdings/{holding_id}")
    def holding(holding_id: int) -> dict[str, Any]:
        from finresearch.db.models import PortfolioDisposal, PortfolioHolding, PortfolioLot, PortfolioTxn
        from finresearch.portfolio.service import actions_of

        with session_scope() as s:
            h = s.get(PortfolioHolding, holding_id)
            if h is None:
                raise HTTPException(404, f"unknown holding {holding_id}")
            txns = s.scalars(select(PortfolioTxn).where(PortfolioTxn.holding_id == h.id)
                             .order_by(PortfolioTxn.day, PortfolioTxn.id)).all()  # fmt: skip
            lots = s.scalars(
                select(PortfolioLot).where(PortfolioLot.holding_id == h.id).order_by(PortfolioLot.id)
            ).all()
            disp = s.scalars(select(PortfolioDisposal).where(PortfolioDisposal.holding_id == h.id)
                             .order_by(PortfolioDisposal.sold, PortfolioDisposal.id)).all()  # fmt: skip
            return {"elss": _elss_view(s, h, lots),
                "id": h.id, "name": h.name, "account": h.account, "asset_type": h.asset_type,
                "transactions": [{"id": t.id, "day": t.day.isoformat(), "kind": t.kind, "quantity": _n(t.quantity),
                                  "price": _n(t.price), "amount": _n(t.amount), "charges": _n(t.charges),
                                  "stt_paid": t.stt_paid, "source": t.source, "import_id": t.import_id,
                                  "note": t.note, "meta": {k: v for k, v in (t.meta or {}).items()
                                                           if k in ("a", "b", "from", "to", "acquired", "reinvest",
                                                                    "subject", "description", "cas_type_txn",
                                                                    "exchange", "rbi_redemption", "note")}}
                                 for t in txns],
                "lots": [{"id": x.id, "acquired": x.acquired.isoformat() if x.acquired else None, "origin": x.origin,
                          "quantity": _n(x.quantity), "open_quantity": _n(x.open_quantity),
                          "cost_per_unit": _n(x.cost_per_unit), "stt_paid": x.stt_paid} for x in lots],
                "disposals": [{"id": d.id, "acquired": d.acquired.isoformat() if d.acquired else None,
                               "sold": d.sold.isoformat(), "quantity": _n(d.quantity), "cost": _n(d.cost),
                               "proceeds": _n(d.proceeds), "origin": d.origin} for d in disp],
                "warnings": (h.meta or {}).get("lot_warnings") or [],
                "pending_actions": actions_of(h.meta, open_only=False),
            }  # fmt: skip

    @app.get("/api/portfolio/imports")
    def imports() -> list[dict[str, Any]]:
        from finresearch.db.models import PortfolioImport

        with session_scope() as s:
            return [{"id": i.id, "kind": i.kind, "source": i.source, "filename": i.filename,
                     "created_at": i.created_at.isoformat() if i.created_at else None, "summary": i.summary or {}}
                    for i in s.scalars(select(PortfolioImport).order_by(PortfolioImport.id.desc()))]  # fmt: skip

    @app.get("/api/portfolio/tax")
    async def tax() -> dict[str, Any]:
        """Realised gains per financial year (STCG/LTCG, set-off, exemption, estimated tax), every disposal with its
        rule, and harvesting ideas for the current year. A personal estimate: verify with a CA."""
        from finresearch.portfolio.report import tax_view

        prices, _ = await _prices(_detached_holdings())
        slab = _slab()
        with session_scope() as s:
            return tax_view(s, prices, src().today(), slab)

    @app.get("/api/portfolio/tax.csv")
    def tax_csv(fy: int | None = Query(None, ge=2000, le=2100)) -> Response:
        from finresearch.portfolio.report import export_rows
        from finresearch.portfolio.tax import export_csv

        with session_scope() as s:
            text = export_csv(export_rows(s), fy)
        name = f"capital-gains-FY{fy - 1}-{fy % 100:02d}.csv" if fy else "capital-gains.csv"
        return Response(text, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"',
                                 "Cache-Control": "no-store"})  # fmt: skip

    async def _rebalance(body: RebalanceParams) -> dict[str, Any]:
        from finresearch.portfolio.limits import bands
        from finresearch.portfolio.rebalance import rebalance_view
        from finresearch.suggest.advisor import load_profile

        prices, _ = await _prices(_detached_holdings())
        slab = _slab()
        with session_scope() as s:
            abs_pp, rel_pct = bands(load_profile(s))
            if body.band_abs_pp is not None:
                abs_pp = body.band_abs_pp
            if body.band_rel_pct is not None:
                rel_pct = body.band_rel_pct
            return rebalance_view(s, prices, src().today(), slab, new_money=Decimal(str(body.new_money)),
                                  abs_pp=abs_pp, rel_pct=rel_pct)  # fmt: skip

    @app.get("/api/portfolio/rebalance")
    async def rebalance(
        new_money: float = Query(0, ge=0, le=1e11),
        band_abs_pp: float | None = Query(None, ge=0.5, le=50),
        band_rel_pct: float | None = Query(None, ge=0, le=100),
    ) -> dict[str, Any]:
        """Illustrative, tax-aware rebalancing steps (portfolio.rebalance): new money to the underweight classes,
        then FIFO sales (losses and the LTCG exemption first) only when a class is outside its band. Not advice."""
        return await _rebalance(RebalanceParams(new_money=new_money, band_abs_pp=band_abs_pp,
                                                band_rel_pct=band_rel_pct))  # fmt: skip

    @app.post("/api/portfolio/rebalance")
    async def rebalance_post(body: RebalanceParams) -> dict[str, Any]:
        """As GET, with the parameters in a JSON body. Read-only: nothing is stored or traded."""
        return await _rebalance(body)

    # ------------------------------------------------------------------ imports
    @app.post("/api/portfolio/import/cas")
    async def import_cas(request: Request) -> dict[str, Any]:
        """Body: {"filename", "content_b64", "password", "dry_run": true|false}. With dry_run (the default) nothing is
        written or saved: the response previews the rows and the reconciliation. The password is never stored."""
        from finresearch.portfolio.importers import StatementError, parse_cas
        from finresearch.portfolio.service import apply, preview, save_upload

        body = await _body(request)
        password = body.pop("password", None)
        if not isinstance(password, str) or not password:
            raise HTTPException(
                422, "password: the CAS PDF password is required (usually your PAN in capitals)"
            )
        content = _decode(body.get("content_b64"))
        filename = str(body.get("filename") or "cas.pdf")[:300]
        dry = body.get("dry_run", True) is not False
        if not content.startswith(b"%PDF"):
            raise HTTPException(422, "not a PDF file")
        try:
            res = parse_cas(content, password)
        except StatementError as e:
            raise HTTPException(422, str(e)) from None
        del password  # the only reference: gone as soon as casparser is done
        sha = hashlib.sha256(content).hexdigest()
        with session_scope() as s:
            if dry or res.holdings_only:
                out = preview(s, res)
                out["already_imported"] = _existing(s, sha)
                return {"dry_run": True, **out}
            if (prev := _existing(s, sha)) is not None:
                raise HTTPException(409, f"this file was already imported (import #{prev})")
            return {
                "dry_run": False,
                **apply(s, res, filename=filename, sha256=sha, saved_path=save_upload(content, sha, ".pdf")),
            }

    @app.post("/api/portfolio/import/tradebook")
    async def import_tradebook(request: Request) -> dict[str, Any]:
        """Body: {"filename", "content_b64", "broker": "zerodha"|"groww"|"upstox"|null (auto), "dry_run"}."""
        from finresearch.portfolio.importers import StatementError, parse_tradebook
        from finresearch.portfolio.service import apply, preview, save_upload

        body = await _body(request)
        content = _decode(body.get("content_b64"))
        filename = str(body.get("filename") or "tradebook.csv")[:300]
        broker = body.get("broker") or None
        if broker not in (None, "zerodha", "groww", "upstox"):
            raise HTTPException(422, "broker must be zerodha, groww, upstox or empty (auto-detect)")
        sha = hashlib.sha256(content).hexdigest()
        dry = body.get("dry_run", True) is not False
        try:
            res = parse_tradebook(content, filename, broker)
        except StatementError as e:
            from finresearch.portfolio.importers import parse_holdings_statement

            try:  # not a tradebook: maybe a holdings statement (baseline + reconciliation, connectors.merge)
                hs = parse_holdings_statement(content, filename, broker)
            except StatementError:
                from finresearch.portfolio.importers import realised_report_hint

                raise HTTPException(422, realised_report_hint(content, filename) or str(e)) from None
            return _holdings_statement(hs, sha, filename, dry)
        with session_scope() as s:
            if dry:
                return {"dry_run": True, **preview(s, res), "already_imported": _existing(s, sha)}
            if (prev := _existing(s, sha)) is not None:
                raise HTTPException(409, f"this file was already imported (import #{prev})")
            ext = (
                Path(filename).suffix.lower()
                if Path(filename).suffix.lower() in (".csv", ".xlsx")
                else ".csv"
            )
            return {
                "dry_run": False,
                **apply(s, res, filename=filename, sha256=sha, saved_path=save_upload(content, sha, ext)),
            }

    # ------------------------------------------------------------------ AIS check
    @app.get("/api/portfolio/ais")
    def ais_list() -> dict[str, Any]:
        """The imported AIS years (no identifiers are stored) and the years the app has dividends or trades in."""
        from finresearch.portfolio.ais_store import statements

        with session_scope() as s:
            return statements(s)

    @app.get("/api/portfolio/ais/{fy}")
    def ais_check(fy: int) -> dict[str, Any]:
        """The AIS check for one financial year (named by its end year: 2026 = FY 2025-26)."""
        from finresearch.portfolio.ais_store import check

        if not 2000 <= fy <= 2100:
            raise HTTPException(422, "fy must be a year such as 2026 (FY 2025-26)")
        with session_scope() as s:
            try:
                return check(s, fy)
            except LookupError as e:
                raise HTTPException(404, str(e)) from None

    @app.post("/api/portfolio/ais/import")
    async def ais_import(request: Request) -> dict[str, Any]:
        """Body: {"filename", "content_b64", "password" (PDF only), "fy" (when the file does not say), "dry_run"}.
        The AIS is reduced to the rows the check needs and the file is not saved; the password is never stored,
        logged or echoed (same handling as the CAS import)."""
        from finresearch.portfolio.ais_store import check, known_sha, parse_file, save
        from finresearch.portfolio.importers import StatementError

        body = await _body(request)
        password = body.pop("password", None)
        if password is not None and not isinstance(password, str):
            raise HTTPException(422, "password must be a string")
        content = _decode(body.get("content_b64"))
        dry = body.get("dry_run", True) is not False
        try:
            st = parse_file(content, password or None)
        except StatementError as e:
            raise HTTPException(422, str(e)) from None
        finally:
            del password
        fy_in = body.get("fy")
        if fy_in is not None and (not isinstance(fy_in, int) or not 2000 <= fy_in <= 2100):
            raise HTTPException(422, "fy must be a year such as 2026 (FY 2025-26)")
        if st.fy is not None and fy_in is not None and st.fy != fy_in:
            raise HTTPException(422, f"this AIS is for FY {st.fy - 1}-{st.fy % 100:02d}, not the year chosen")
        fy = st.fy or fy_in
        if fy is None:
            raise HTTPException(422, "fy: the financial year could not be read from the file; choose it")
        sha = hashlib.sha256(content).hexdigest()
        summary = {"fy": fy, "format": st.format, "rows": len(st.items), "ignored": st.ignored,
                   "warnings": st.warnings}  # fmt: skip
        with session_scope() as s:
            prev = known_sha(s, sha)
            if not dry:
                save(s, st, fy, sha)
            return {"dry_run": dry, "already_imported": prev, **summary, "check": check(s, fy, st.items)}

    @app.delete("/api/portfolio/ais/{fy}")
    def ais_delete(fy: int) -> dict[str, Any]:
        """Forget the AIS stored for one financial year."""
        from finresearch.db.models import PortfolioAis

        with session_scope() as s:
            row = s.get(PortfolioAis, fy)
            if row is None:
                raise HTTPException(404, "no AIS imported for that year")
            s.delete(row)
        return {"deleted": fy}

    @app.delete("/api/portfolio/imports/{import_id}")
    def delete_import(import_id: int) -> dict[str, Any]:
        """Remove an import and every transaction it added (lots are rebuilt). The saved file is deleted too."""
        from finresearch.db.models import PortfolioImport
        from finresearch.portfolio.service import delete_import as _delete

        with session_scope() as s:
            imp = s.get(PortfolioImport, import_id)
            path = Path(imp.saved_path) if imp and imp.saved_path else None
            try:
                n = _delete(s, import_id)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
        if path is not None and path.is_file() and _within(path, get_settings().portfolio_dir):
            path.unlink()
        return {"deleted": import_id, "holdings_rebuilt": n}

    # ------------------------------------------------------------------ manual edits
    @app.post("/api/portfolio/transactions", status_code=201)
    def add_txn(body: ManualTxn) -> dict[str, Any]:
        from finresearch.portfolio.service import DuplicateEntry, manual_txn

        if body.holding_id is None and not body.name.strip():
            raise HTTPException(422, "name: say what you bought (or pick an existing holding)")
        if body.kind in ("buy", "sell", "opening") and body.quantity is None:
            raise HTTPException(422, "quantity is required for a buy, sell or opening balance")
        if body.kind in ("buy", "sell") and body.price is None and body.amount is None:
            raise HTTPException(422, "price or amount is required for a buy or sell")
        if body.kind == "dividend" and not body.amount:
            raise HTTPException(422, "amount is required for a dividend")
        if body.kind == "split" and not ({"from", "to"} <= set(body.meta)):
            raise HTTPException(422, "a split needs meta.from and meta.to (face value before and after)")
        if body.kind == "bonus" and not ({"a", "b"} <= set(body.meta)):
            raise HTTPException(422, "a bonus needs meta.a and meta.b (a new shares for every b held)")
        with session_scope() as s:
            try:
                t = manual_txn(s, body.model_dump())
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            except DuplicateEntry as e:
                raise HTTPException(409, str(e)) from e
            return {"id": t.id, "holding_id": t.holding_id}

    @app.put("/api/portfolio/transactions/{txn_id}")
    def put_txn(txn_id: int, body: TxnUpdate) -> dict[str, Any]:
        from finresearch.portfolio.service import update_txn

        with session_scope() as s:
            try:
                t = update_txn(s, txn_id, body.model_dump(exclude_unset=True))
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            return {"id": t.id, "holding_id": t.holding_id}

    @app.delete("/api/portfolio/transactions/{txn_id}")
    def del_txn(txn_id: int) -> dict[str, Any]:
        from finresearch.portfolio.service import delete_txn

        with session_scope() as s:
            try:
                delete_txn(s, txn_id)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
        return {"deleted": txn_id}

    @app.put("/api/portfolio/holdings/{holding_id}")
    def put_holding(holding_id: int, body: HoldingUpdate) -> dict[str, Any]:
        from finresearch.portfolio.service import update_holding

        data = body.model_dump(exclude_unset=True)
        for k in ("nse_symbol", "isin"):
            if data.get(k):
                data[k] = data[k].strip().upper()
        with session_scope() as s:
            try:
                h = update_holding(s, holding_id, data)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            return {"id": h.id}

    @app.post("/api/portfolio/actions/sync")
    async def sync_actions() -> dict[str, Any]:
        """Fetch NSE corporate actions for every stock holding with an NSE symbol and add splits and bonuses (after
        the first purchase, each once). BSE-only holdings: BSE's corporate-action feed. Any other action that changes
        the cost or the units of a stock held on its ex-date (demerger, rights, merger, ISIN change, buyback, capital
        reduction, consolidation) is recorded as `pending` on the holding, which makes its cost and later tax years
        incomplete until it is resolved (#237)."""
        from finresearch.adapters.bse_equity import bse_source_url
        from finresearch.adapters.nse_equity import nse_source_url
        from finresearch.db.models import PortfolioHolding
        from finresearch.portfolio.service import apply_actions, record_unsupported

        with session_scope() as s:
            targets = [(h.id, h.nse_symbol, h.bse_code) for h in s.scalars(select(PortfolioHolding))
                       if h.asset_type == "stock" and (h.nse_symbol or h.bse_code)]  # fmt: skip
        fetched: dict[tuple[str, str], list[tuple[date | None, str]]] = {}
        errors: list[str] = []
        failed: set[tuple[str, str]] = set()
        for _, sym, code in targets:
            key = ("NSE", sym) if sym else ("BSE", code)
            if key in fetched:
                continue
            try:
                async with src().open_equity(key[0]) as eq:
                    acts = await eq.corporate_actions(key[1])
                fetched[key] = [(a.ex_date, a.subject) for a in acts]
            except Exception as e:
                errors.append(f"{key[0]} {key[1]}: {type(e).__name__}")
                failed.add(key)
                fetched[key] = []
        added: dict[str, list[str]] = {}
        pending: dict[str, list[str]] = {}
        synced_on = src().today().isoformat()
        with session_scope() as s:
            for hid, sym, code in targets:
                h = s.get(PortfolioHolding, hid)
                acts = fetched[("NSE", sym) if sym else ("BSE", code)]
                got = apply_actions(s, h, acts)
                if got:
                    added[h.name] = got
                url = (
                    nse_source_url("corporate_actions", sym)
                    if sym
                    else bse_source_url("corporate_actions", code)
                )
                rec = record_unsupported(
                    s, h, acts, source_url=url, source="nse_actions" if sym else "bse_actions"
                )
                if rec:
                    pending[h.name] = rec
                # when this stock's actions were last read (a failed fetch is not a sync): the data-health row reads
                # a stock never synced, or synced too long ago, as unknown, never as "no corporate action" (#264)
                if (("NSE", sym) if sym else ("BSE", code)) not in failed:
                    h.meta = {**(h.meta or {}), "actions_synced": synced_on}
        return {"checked": len(targets), "added": added, "pending": pending, "errors": errors}

    @app.post("/api/portfolio/holdings/{holding_id}/actions/resolve")
    def resolve_action(holding_id: int, body: ResolveAction) -> dict[str, Any]:
        """Mark a recorded corporate action resolved with a note (the cost allocation entered by hand, or why none
        is needed). Its holding's cost and later tax years count as complete again."""
        from finresearch.portfolio.service import actions_of
        from finresearch.portfolio.service import resolve_action as resolve

        with session_scope() as s:
            try:
                h = resolve(s, holding_id, body.key, body.note)
            except LookupError as e:
                raise HTTPException(404, str(e)) from e
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            return {"id": h.id, "pending_actions": actions_of(h.meta, open_only=False)}


def _holdings_statement(hs, sha: str, filename: str, dry: bool) -> dict[str, Any]:
    """A broker holdings statement: the broker-baseline merge rules (connectors.merge). Dry run = the same merge in a
    savepoint that is rolled back."""
    from datetime import UTC, datetime

    from finresearch.portfolio.connectors.inbox import _record_sha
    from finresearch.portfolio.connectors.merge import merge_sync, remember_statement_prices, statement_day

    broker, holdings = hs
    acc = {"zerodha": "Zerodha", "groww": "Groww", "upstox": "Upstox"}[broker]
    now = datetime.now(UTC)
    as_of = statement_day(filename, now)
    with session_scope() as s:
        prev = _existing(s, sha)
        if not dry and prev is not None:
            raise HTTPException(409, f"this file was already imported (import #{prev})")
        sp = s.begin_nested()
        mr = merge_sync(s, account=acc, source=f"{broker}_holdings", label=f"{acc} holdings file", holdings=holdings,
                        trades=[], today=as_of, now=now)  # fmt: skip
        remember_statement_prices(s, account=acc, holdings=holdings, day=as_of, label=f"{acc}")
        out = mr.as_dict()
        if dry:
            sp.rollback()
        else:
            sp.commit()
            _record_sha(s, mr.import_ids, sha, filename, f"{broker}_holdings")
    return {"dry_run": dry, "kind": "holdings", "source": broker, "rows": len(holdings),
            "new_rows": len(out["baselines"]), "duplicates": 0, "holdings_only": True, "already_imported": prev,
            "warnings": ["A holdings statement has no dates: holdings without history in this account get a "
                         "baseline at the broker's average cost (purchase date unknown); the rest are only "
                         "compared. Import the tradebook too for exact lots and tax."], **out}  # fmt: skip


def _existing(s, sha: str) -> int | None:
    from finresearch.db.models import PortfolioImport

    return s.scalar(select(PortfolioImport.id).where(PortfolioImport.sha256 == sha))


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _n(v: Decimal | None) -> str | None:
    if v is None:
        return None
    return format(v.normalize(), "f")
