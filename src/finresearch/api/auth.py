"""The local API token (issue #246c): every /api route except /api/health needs it.

`finresearch serve` creates one random token per install (per database) on first start, keeps it in the Keychain
(finresearch.secrets) and writes it to `<state_dir>/api_token` (mode 0600) for the Next.js server to read. Callers
send it as `Authorization: Bearer <token>` (the CLI and scripts: `client_headers()`), or as the httpOnly,
SameSite=Strict cookie `finresearch_token_<api port>` that the web app's server sets for 127.0.0.1 (cookies are
scoped to the host, not the port, so the app on :3100 sets the cookie the browser then sends to the API on :8710; the
port in the name keeps a second API on another port from overwriting it).

Exempt: /api/health (the app's status dot) and the brokers' OAuth callback, which the browser reaches by a
cross-site redirect from the broker (a SameSite=Strict cookie is not sent there); the callback is already bound to a
one-time state started from the app.

What it stops: a local program that can make HTTP requests but cannot read this user's files (for example a research
agent's WebFetch, which is also denied loopback and private addresses). What it does not stop: malware running as
this user, which can read the token file.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
import secrets
from pathlib import Path

from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from finresearch import secrets as secret_store
from finresearch.config import get_settings

log = logging.getLogger(__name__)

COOKIE_PREFIX = "finresearch_token_"
EXEMPT = frozenset({"/api/health"})
_CALLBACK = re.compile(r"^/api/connections/[a-z_]{1,20}/callback$")


def token_file() -> Path:
    return Path(get_settings().state_dir) / "api_token"


def _write_file(token: str) -> None:
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_file() -> str | None:
    try:
        tok = token_file().read_text().strip()
    except FileNotFoundError:
        return None
    return tok or None


def current() -> str | None:
    """The token this install uses: the FINRESEARCH_API_TOKEN setting, else the state file."""
    s = get_settings()
    if s.api_token is not None and s.api_token.get_secret_value():
        return s.api_token.get_secret_value()
    return _read_file()


def bootstrap() -> str:
    """Make sure a token exists in the Keychain and in the 0600 state file; return it. The Keychain copy wins; a
    state file from an earlier start is adopted when the Keychain has none (or cannot be reached, which is logged)."""
    s = get_settings()
    if s.api_token is not None and s.api_token.get_secret_value():
        tok = s.api_token.get_secret_value()
        if _read_file() != tok:
            _write_file(tok)
        return tok
    ref = secret_store.ref_for("api", "token")
    stored: str | None = None
    try:
        stored = secret_store.backend().get(ref)
    except secret_store.SecretStoreError as e:
        log.warning("API token: the secret store is unavailable (%s); using the state file only", e)
    tok = stored or _read_file() or secrets.token_urlsafe(32)
    if not stored:
        try:
            secret_store.backend().set(ref, tok)
        except secret_store.SecretStoreError as e:
            log.warning("API token: could not save it in the secret store (%s)", e)
    if _read_file() != tok:
        _write_file(tok)
    return tok


def client_headers() -> dict[str, str]:
    """Headers for a local client of the API (CLI, scripts)."""
    tok = current()
    if not tok:
        raise RuntimeError("no API token yet: start the API once with `uv run finresearch serve`")
    return {"Authorization": f"Bearer {tok}"}


def exempt(method: str, path: str) -> bool:
    return method == "OPTIONS" or path in EXEMPT or (method in ("GET", "HEAD") and bool(_CALLBACK.match(path)))


class ApiTokenGuard:
    """401 unless the request carries the token as a bearer header or the app's cookie."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        if not token:
            raise ValueError("ApiTokenGuard needs a token")
        self.app, self._token = app, token.encode()

    def _ok(self, conn: HTTPConnection) -> bool:
        auth = conn.headers.get("authorization", "")
        if auth[:7].lower() == "bearer " and hmac.compare_digest(auth[7:].strip().encode(), self._token):
            return True
        port = (conn.scope.get("server") or (None, None))[1]
        cookie = conn.cookies.get(f"{COOKIE_PREFIX}{port}")
        return cookie is not None and hmac.compare_digest(cookie.encode(), self._token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            conn = HTTPConnection(scope)
            if not exempt(scope.get("method", "GET"), scope["path"]) and not self._ok(conn):
                resp = JSONResponse({"detail": "unauthorized: the local API token is missing or wrong (reload the "
                                               "app; the CLI reads it from data/state/api_token)"},
                                    status_code=401, headers={"WWW-Authenticate": "Bearer"})  # fmt: skip
                return await resp(scope, receive, send)
        await self.app(scope, receive, send)
