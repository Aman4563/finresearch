"""/api/lookthrough: mutual-fund look-through from the AMCs' monthly portfolio files (see adapters.amc_portfolio).

Reads the personal portfolio (read-only) and the file store under data/portfolio/lookthrough/. Downloads happen
only on an explicit POST (fetch from an AMC page or an allow-listed file URL) and for AMFI's market-cap list; uploads
are JSON bodies with the file base64-encoded. Nothing here is sent to an LLM. Every response carries the personal,
non-advisory disclaimer; the numbers are arithmetic on published portfolios, not advice.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from sqlalchemy import select

from finresearch.db import session_scope

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
PRICE_TTL_S = 600
QUOTE_TIMEOUT_S = 25
NAV_TTL_S = 6 * 3600


def add_lookthrough_routes(app: FastAPI, *, scheme_rows: Callable[[], Awaitable[list]], store_root: Path | None = None,
                           http: Callable[[], Any] | None = None) -> None:  # fmt: skip
    """`http()` returns a PoliteClient-like object (tests pass one with a mock transport); default: a live client."""
    from finresearch.adapters.amc_portfolio import AMC_SOURCES, AmcPortfolioError, source_for_amc
    from finresearch.api.markets import MarketSources, TtlCache
    from finresearch.portfolio import lookthrough as lt

    cache = TtlCache()

    def store() -> lt.PortfolioStore:
        return lt.PortfolioStore(store_root)

    def client() -> Any:
        factory = http or getattr(
            app.state, "lookthrough_http", None
        )  # test seam: a client with a mock transport
        return factory() if factory is not None else lt._client()

    def src() -> MarketSources:
        s = getattr(app.state, "markets", None)
        if s is None:
            s = app.state.markets = MarketSources(listings=getattr(app.state, "listings", None))
        return s

    def today() -> date:
        return src().today()

    async def navs() -> list:
        return await cache.get(("navall",), NAV_TTL_S, scheme_rows)

    async def amfi_by_code() -> dict[str, Any]:
        try:
            return {r.code: r for r in await navs()}
        except Exception:
            return {}

    async def ensure_cap_list(st: lt.PortfolioStore) -> None:
        c = client()
        try:
            await lt.refresh_cap_list(st, client=c, today=today())
        except Exception:
            pass  # the cap mix then reads "Unclassified"; the page says the list is missing
        finally:
            await c.aclose()

    async def prices(holdings: list[Any]) -> dict[int, Any]:
        from finresearch.portfolio.valuation import QuoteBatch, fetch_prices

        sources = src()
        async with QuoteBatch() as batch:

            async def live(sym: str, exch: str) -> Any:
                if (exch == "NSE" and sources.quote is not None) or (
                    exch == "BSE" and sources.bse_quote is not None
                ):
                    return await sources.get_quote(sym, exch)
                return await batch.quote(sym, exch)

            async def quote(sym: str, exch: str) -> Any:
                # a quote that hangs (an exchange refusing the session) must not hang the page: the holding is then
                # "without a price" and left out, and the page says how many were left out
                return await asyncio.wait_for(
                    cache.get(("quote", exch, sym), PRICE_TTL_S, lambda: live(sym, exch)), QUOTE_TIMEOUT_S
                )

            return await fetch_prices(holdings, quote=quote, scheme_rows=navs, listings=sources.listings)

    def detached_holdings() -> list[Any]:
        from finresearch.db.models import PortfolioHolding

        with session_scope() as s:
            hs = list(s.scalars(select(PortfolioHolding)))
            s.expunge_all()
            return hs

    def held_mf_codes() -> list[tuple[str | None, str]]:
        """(scheme code, name) of mutual funds with open units: no prices needed for overlap."""
        from finresearch.db.models import PortfolioHolding, PortfolioLot

        with session_scope() as s:
            open_ids = {
                h for (h,) in s.execute(select(PortfolioLot.holding_id).where(PortfolioLot.open_quantity > 0))
            }
            return [(h.scheme_code, h.name) for h in s.scalars(select(PortfolioHolding).where(
                PortfolioHolding.asset_type == "mf")) if h.id in open_ids]  # fmt: skip

    def err(e: Exception, status: int = 422) -> HTTPException:
        return HTTPException(status, str(e))

    # ------------------------------------------------------------------ read
    @app.get("/api/lookthrough")
    async def portfolio_lookthrough(top: int = Query(25, ge=5, le=200)) -> dict[str, Any]:
        """The whole portfolio through the funds' latest month-end portfolios: true top stocks (direct + via funds),
        sector and market-cap mix of the equity, asset buckets that reconcile to the portfolio value, redundancy, and
        the overlap between every pair of held funds."""
        st = store()
        await ensure_cap_list(st)
        holdings = detached_holdings()
        got = await prices(holdings)
        amfi = await amfi_by_code()
        with session_scope() as s:
            return lt.lookthrough_exposure(s, got, today(), store=st, amfi=amfi, top=top)

    @app.get("/api/lookthrough/sources")
    def sources() -> dict[str, Any]:
        st = store()
        idx = st.index()
        return {
            "sources": [{"key": k, "amc": v.amc, "page": v.page, "mode": v.mode, "note": v.note, "hosts": list(v.hosts)}
                        for k, v in AMC_SOURCES.items()],
            "files": [{"sha": sha, **{k: m.get(k) for k in ("filename", "url", "amc", "fetched_at", "size")},
                       "schemes": len(m.get("schemes", [])),
                       "months": sorted({(x.get("as_of") or "")[:7] for x in m.get("schemes", []) if x.get("as_of")})}
                      for sha, m in sorted(idx["files"].items(), key=lambda kv: kv[1].get("fetched_at") or "",
                                           reverse=True)],
            "schemes": st.schemes(), "aliases": idx["aliases"], "cap_list": lt.cap_lookup(st)[1],
            "disclaimer": lt.DISCLAIMER,
        }  # fmt: skip

    @app.get("/api/lookthrough/funds/{code}")
    async def fund(code: str, bench: str | None = Query(None, max_length=200),
                   with_code: str | None = Query(None, alias="with", max_length=20)) -> dict[str, Any]:  # fmt: skip
        """One scheme: its latest stored portfolio, style over the stored months, active share against an index
        fund/ETF proxy of its benchmark, and its overlap with the user's other funds (and with `with`)."""
        st = store()
        amfi = await amfi_by_code()
        row = amfi.get(code)
        name = row.name if row is not None else None
        held = held_mf_codes()
        if name is None:
            name = next((n for c, n in held if c == code), None)
        key = st.key_for(code, name)
        out = (
            lt.fund_detail(st, key, today(), bench_key=bench)
            if key
            else {"key": None, "file": {"available": False}}
        )
        src_key = source_for_amc(getattr(row, "amc", None), key or "")
        out.update({"code": code, "name": name, "amc": getattr(row, "amc", None), "source": src_key,
                    "auto_fetch": bool(src_key and AMC_SOURCES[src_key].mode == "auto"),
                "url_mode": bool(src_key and AMC_SOURCES[src_key].mode == "url"),
                    "source_page": AMC_SOURCES[src_key].page if src_key else None, "disclaimer": lt.DISCLAIMER})  # fmt: skip
        me = st.latest(key)
        others = []
        seen = {key}
        for c, n in held:
            k = st.key_for(c, getattr(amfi.get(c or ""), "name", None) or n)
            if k in seen:
                continue
            seen.add(k)
            f = st.latest(k)
            item: dict[str, Any] = {"code": c, "name": getattr(amfi.get(c or ""), "name", None) or n, "key": k,
                                    "available": f is not None}  # fmt: skip
            if me is not None and f is not None:
                item.update(lt.overlap_json(me, f))
            others.append(item)
        out["held"] = others
        out["is_held"] = any(
            st.key_for(c, getattr(amfi.get(c or ""), "name", None) or n) == key for c, n in held
        )
        if with_code:
            wr = amfi.get(with_code)
            wk = st.key_for(with_code, wr.name if wr is not None else None)
            wf = st.latest(wk)
            mode = (
                AMC_SOURCES[s2].mode if (s2 := source_for_amc(getattr(wr, "amc", None), wk or "")) else None
            )
            out["with"] = {"code": with_code, "name": wr.name if wr is not None else None, "key": wk,
                           "available": wf is not None, "auto_fetch": mode == "auto", "url_mode": mode == "url",
                           **(lt.overlap_json(me, wf) if me is not None and wf is not None else {})}  # fmt: skip
        return out

    @app.get("/api/lookthrough/overlap")
    async def overlap_matrix(codes: str = Query(..., max_length=400)) -> dict[str, Any]:
        """Pairwise overlap (SEBI Annexure 1A: Σ min weight over common ISINs, equity holdings) of 2-12 schemes."""
        wanted = [c.strip() for c in codes.split(",") if c.strip()][:12]
        if len(wanted) < 2:
            raise HTTPException(422, "give at least two scheme codes")
        st = store()
        amfi = await amfi_by_code()
        found = []
        for c in wanted:
            r = amfi.get(c)
            k = st.key_for(c, r.name if r is not None else None)
            found.append((c, r.name if r is not None else c, st.latest(k)))
        pairs = [{"a": a[0], "b": b[0], **lt.overlap_json(a[2], b[2], top=5)} for i, a in enumerate(found)
                 for b in found[i + 1 :] if a[2] is not None and b[2] is not None]  # fmt: skip
        return {"funds": [{"code": c, "name": n, "file": lt.file_status(f, today())} for c, n, f in found],
                "pairs": pairs, "disclaimer": lt.DISCLAIMER}  # fmt: skip

    # ------------------------------------------------------------------ write (behind the app's CSRF/Origin guard)
    async def body_of(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(422, "the body must be JSON") from e
        if not isinstance(body, dict):
            raise HTTPException(422, "the body must be a JSON object")
        return body

    def match_codes(portfolios: list[Any], rows: list) -> list[dict[str, Any]]:
        from finresearch.adapters.amc_portfolio import scheme_key

        by_key: dict[str, list[Any]] = {}
        for r in rows:
            by_key.setdefault(scheme_key(r.name), []).append(r)
        return [{"sheet": p.sheet, "name": p.scheme_name, "key": p.key, "as_of": p.as_of.isoformat() if p.as_of else None,
                 "holdings": len(p.holdings), "benchmark": p.benchmark, "warnings": p.warnings,
                 "codes": [r.code for r in by_key.get(p.key, [])][:12]} for p in portfolios]  # fmt: skip

    @app.post("/api/lookthrough/fetch")
    async def fetch(request: Request) -> dict[str, Any]:
        """{"scheme_code", "months"?}: find the scheme's monthly files on its AMC's page (PPFAS, Nippon, DSP), or
        {"url"}: download one allow-listed AMC file (Axis and others: paste the file link)."""
        body = await body_of(request)
        st = store()
        c = client()
        try:
            if body.get("url"):
                sha, ps = await lt.fetch_url(st, str(body["url"]), client=c)
                rows = await navs()
                return {"sha": sha, "schemes": match_codes(ps, rows)}
            code = str(body.get("scheme_code") or "").strip()
            if not code:
                raise HTTPException(422, "scheme_code or url is required")
            months = max(1, min(12, int(body.get("months") or 1)))
            amfi = await amfi_by_code()
            r = amfi.get(code)
            name = r.name if r is not None else next((n for cc, n in held_mf_codes() if cc == code), None)
            key = st.key_for(code, name)
            if not key:
                raise HTTPException(404, f"unknown scheme {code}")
            return await lt.fetch_scheme(st, key, getattr(r, "amc", None), months=months, client=c)
        except AmcPortfolioError as e:
            raise err(e) from e
        finally:
            await c.aclose()

    @app.post("/api/lookthrough/upload")
    async def upload(request: Request) -> dict[str, Any]:
        """{"filename", "content_b64"}: any AMC's monthly portfolio file (xlsx, or a zip of xlsx). Returns the schemes
        found and the AMFI codes each one was linked to by name."""
        body = await body_of(request)
        raw = body.get("content_b64")
        if not isinstance(raw, str) or not raw:
            raise HTTPException(422, "content_b64: the file, base64-encoded, is required")
        if len(raw) > MAX_UPLOAD_BYTES * 4 // 3 + 8:
            raise HTTPException(413, f"file too large (limit {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)")
        try:
            data = base64.b64decode(raw.split(",", 1)[-1] if raw.startswith("data:") else raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(422, "content_b64 is not valid base64") from e
        name = str(body.get("filename") or "upload.xlsx")[:200]
        try:
            sha, ps = store().add_file(data, name, amc=None)
        except AmcPortfolioError as e:
            raise err(e) from e
        rows: list = []
        with contextlib.suppress(Exception):
            rows = await navs()
        return {"sha": sha, "schemes": match_codes(ps, rows)}

    @app.put("/api/lookthrough/links")
    async def link(request: Request) -> dict[str, Any]:
        """{"scheme_code", "key"}: use the stored portfolio `key` for this AMFI scheme (when the names differ); a
        null key removes the manual link."""
        body = await body_of(request)
        code = str(body.get("scheme_code") or "").strip()
        key = body.get("key")
        if not code:
            raise HTTPException(422, "scheme_code is required")
        st = store()
        if key is not None and not any(s["key"] == key for s in st.schemes()):
            raise HTTPException(404, "no stored portfolio has that key")
        st.set_alias(code, str(key) if key else None)
        return {"scheme_code": code, "key": key}

    @app.delete("/api/lookthrough/files/{sha}")
    def delete_file(sha: str) -> dict[str, Any]:
        if not store().delete_file(sha):
            raise HTTPException(404, "unknown file")
        return {"deleted": sha}
