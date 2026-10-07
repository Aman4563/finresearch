"""/api/connections: the user's broker connections (read-only sync) and the statement inbox.

Credentials travel only in PUT bodies (never query strings, which access logs record), are stored in the local
database, and are returned masked. Bodies are validated by hand so a validation error never echoes a secret back.
The browser-login callback (`GET .../callback`) checks the one-time `state` it issued before exchanging the code.
Unsafe routes sit behind the app's CSRF/Origin guard like every other route.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse

from finresearch.db import session_scope


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(422, "the body must be JSON") from e
    if not isinstance(body, dict):
        raise HTTPException(422, "the body must be a JSON object")
    return body


LOCAL_APP_DEFAULT = "http://127.0.0.1:3100"


def _local_app_url(url: str) -> str:
    """Where a broker's login callback may send the browser back to: the app on this machine only. The app URL is a
    user setting (also used for links in phone notifications), so a non-loopback value falls back to the default
    instead of turning the callback into an open redirect (CodeQL py/url-redirection)."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1") and not parts.username:
        return f"{parts.scheme}://{parts.netloc}".rstrip("/")
    return LOCAL_APP_DEFAULT


def _known(key: str) -> None:
    from finresearch.portfolio.connectors import CONNECTORS, INBOX_KEY

    if key not in CONNECTORS and key != INBOX_KEY:
        raise HTTPException(404, f"unknown connection {key!r}")


def _broker(key: str) -> None:
    from finresearch.portfolio.connectors import CONNECTORS

    if key not in CONNECTORS:
        raise HTTPException(404, f"unknown broker {key!r}")


def callback_url(request: Request, key: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/api/connections/{key}/callback"


def add_connection_routes(app: FastAPI, clock: Callable[[], datetime] | None = None) -> None:
    def now() -> datetime:
        return clock() if clock else datetime.now(UTC)

    @app.get("/api/connections")
    def connections(request: Request) -> dict[str, Any]:
        """Every broker connector (what it provides, cost, how its login works) with this user's status, masked
        settings, last sync, open positions and funds; plus the statement inbox folder and its waiting files."""
        from finresearch.portfolio.connectors import CONNECTORS
        from finresearch.portfolio.connectors.inbox import inbox_dir, pending_files
        from finresearch.portfolio.connectors.store import public

        with session_scope() as s:
            items = public(s, now())
        for it in items:
            if it["key"] in CONNECTORS:
                it["callback_url"] = callback_url(request, it["key"])
        return {"connections": items, "inbox": {"path": str(inbox_dir()), "pending": pending_files()}}

    @app.put("/api/connections/{key}")
    async def put_connection(key: str, request: Request) -> dict[str, Any]:
        """Body: {"config": {field: value}, "enabled", "auto_sync"}. A secret sent back masked (or null) keeps the
        stored value; {"config": {"clear_<field>": true}} removes it. Changing a credential drops the session."""
        from finresearch.portfolio.connectors.store import public_one, update

        _known(key)
        body = await _body(request)
        with session_scope() as s:
            try:
                update(s, key, body)
            except ValueError as e:
                raise HTTPException(422, str(e)) from None
            out = public_one(s, key, now())
        return out

    @app.delete("/api/connections/{key}")
    def delete_connection(key: str) -> dict[str, Any]:
        """Forget the credentials and session. Imported transactions stay (delete their imports on the portfolio
        page to remove them)."""
        from finresearch.portfolio.connectors.store import disconnect

        _known(key)
        with session_scope() as s:
            return {"deleted": disconnect(s, key)}

    @app.post("/api/connections/{key}/login")
    async def login(key: str) -> dict[str, Any]:
        """A headless login now (TOTP / API secret brokers): stores the day's access token. Browser-login brokers
        answer 409 with the next step (use the login URL)."""
        from finresearch.portfolio.connectors.base import ConnectorError, ReconnectNeeded, redact
        from finresearch.portfolio.connectors.store import build, get_row, public_one, set_token

        _broker(key)
        with session_scope() as s:
            row = get_row(s, key)
            if row is None:
                raise HTTPException(404, "save the connection's settings first")
            conn = build(row)
        try:
            grant = await conn.login()
        except ReconnectNeeded as e:
            raise HTTPException(409, redact(str(e), *conn.secret_values())) from None
        except ConnectorError as e:
            raise HTTPException(502, redact(str(e), *conn.secret_values())) from None
        with session_scope() as s:
            set_token(s, key, grant)
            return public_one(s, key, now())

    @app.get("/api/connections/{key}/login-url")
    def login_url(key: str, request: Request) -> dict[str, Any]:
        """The broker's login page for a browser-login broker, with a one-time state; after you log in the broker
        redirects to this API's callback, which stores the day's token and sends you back to Profile.

        One of the two documented exceptions to "GETs never write" (#247, docs/USAGE.md): it stores the one-time
        OAuth state the callback checks, and the page opens it as a plain link."""
        from finresearch.portfolio.connectors.store import build, get_row

        _broker(key)
        state = secrets.token_urlsafe(16)
        with session_scope() as s:
            row = get_row(s, key)
            if row is None:
                raise HTTPException(404, "save the connection's settings first")
            url = build(row).login_url(callback_url(request, key), state)
            if url is None:
                raise HTTPException(409, "this broker logs in by itself (use Log in now)")
            row.state = {**(row.state or {}), "oauth_state": state, "oauth_at": now().isoformat()}
        return {"url": url, "callback_url": callback_url(request, key)}

    @app.get("/api/connections/{key}/callback")
    async def oauth_callback(key: str, request: Request, code: str | None = Query(None, max_length=2000),
                             request_token: str | None = Query(None, max_length=2000),
                             tokenId: str | None = Query(None, max_length=4000),
                             state: str | None = Query(None, max_length=200)) -> RedirectResponse:  # fmt: skip
        """Where the broker sends the browser after login. Verifies the state, exchanges the code for the day's
        token and redirects to Profile → Connections with ?connected=<key> or ?connect_error=<message>.

        A GET that writes by necessity (#247's documented exception): the broker's OAuth redirect is always a GET."""
        from finresearch.monitor.notify import load
        from finresearch.portfolio.connectors.base import ConnectorError, redact
        from finresearch.portfolio.connectors.store import build, get_row, set_token

        _broker(key)
        with session_scope() as s:
            app_url = _local_app_url(load(s, "general").app_url)
            row = get_row(s, key)
            expected = (row.state or {}).get("oauth_state") if row else None
            conn = build(row) if row else None

        def back(**q: str) -> RedirectResponse:
            qs = "&".join(f"{k}={quote(v)}" for k, v in q.items())
            return RedirectResponse(f"{app_url}/profile?{qs}#connections", status_code=303)

        grant_code = code or request_token or tokenId
        # Zerodha cannot carry a state through its login (redirect_params echo it back as a plain param)
        if (
            conn is None
            or not expected
            or (state or request.query_params.get("finresearch_state")) != expected
        ):
            return back(
                connect_error="the login answer did not match a login started here: try Connect again"
            )
        if not grant_code:
            return back(connect_error="the broker did not return a login code (cancelled?)")
        try:
            grant = await conn.exchange_code(grant_code, callback_url(request, key))
        except ConnectorError as e:
            return back(connect_error=redact(str(e), grant_code, *conn.secret_values())[:200])
        with session_scope() as s:
            set_token(s, key, grant)
            r = get_row(s, key)
            r.state = {k: v for k, v in (r.state or {}).items() if k not in ("oauth_state", "oauth_at")}
        return back(connected=key)

    @app.post("/api/connections/{key}/sync")
    async def sync(key: str) -> dict[str, Any]:
        """Sync now: read holdings, positions, trades (and funds/MF where offered) and merge them. Read-only."""
        from finresearch.portfolio.connectors.sync import sync_now

        _broker(key)
        try:
            return await sync_now(key, trigger="manual", now=now())
        except LookupError as e:
            raise HTTPException(404, str(e)) from None

    @app.get("/api/connections/log")
    def sync_log(key: str | None = Query(None, max_length=20), limit: int = Query(20, ge=1, le=200)) -> list:
        from finresearch.portfolio.connectors.sync import recent_logs

        with session_scope() as s:
            return recent_logs(s, key, limit)

    @app.post("/api/connections/cas_inbox/scan")
    async def scan_inbox() -> dict[str, Any]:
        """Import the files waiting in the statement inbox now."""
        from finresearch.portfolio.connectors.inbox import scan

        return await asyncio.to_thread(scan, now=now())

    @app.get("/api/connections/summary")
    def summary() -> list[dict[str, Any]]:
        """Configured connections with status and last sync (the portfolio page's "last synced" line)."""
        from finresearch.portfolio.connectors.store import public

        with session_scope() as s:
            return [{"key": c["key"], "label": c["label"], "account": c.get("account"), "status": c["status"],
                     "last_sync_at": c["last_sync_at"], "last_error": c["last_error"]}
                    for c in public(s, now()) if c["configured"]]  # fmt: skip
