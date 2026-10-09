"""Error text the API may show: the exception's class and message, with secrets and local paths removed.

The app is local-only, but an exception message can still carry things that should not reach a page or a screenshot:
credentials in a database or proxy URL, API/bot tokens in a query string or URL path, and absolute file paths. The full
exception (with traceback) goes to the server log; responses get `public_error(e)` (CodeQL py/stack-trace-exposure).
"""

from __future__ import annotations

import re

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@"), r"\1***@"),  # user:password@ in any URL
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "***"),  # Telegram bot token
    (re.compile(r"(?i)\b(bot)\d{6,12}:[A-Za-z0-9_-]+"), r"\1***"),  # .../bot<token>/sendMessage
    (
        re.compile(
            r"(?i)\b(token|access_token|api_key|apikey|key|secret|password|passwd|pwd|auth|sig|signature)"
            r"=[^\s&\"']+"
        ),
        r"\1=***",
    ),
    (re.compile(r"(?i)\b(bearer|token)\s+[A-Za-z0-9._~+/=-]{16,}"), r"\1 ***"),
    (re.compile(r"(?:/Users|/home|/private|/var|/tmp|[A-Za-z]:\\Users)[^\s\"':,;)]*"), "<path>"),
)


_QUERY = re.compile(r"(?i)\b(https?://[^\s?#\"'<>]+)\?[^\s#\"'<>]*")


def redact(text: str) -> str:
    """`text` with credentials, tokens and absolute local paths replaced."""
    for rx, sub in _RULES:
        text = rx.sub(sub, text)
    return text


def public_error(e: BaseException, limit: int = 300, *, drop_query: bool = False) -> str:
    """'<ExceptionClass>: <redacted message>' cut to `limit` characters: enough to say what failed, nothing private.
    `drop_query`: also cut every http(s) URL's query string ("?..." becomes "?<query>"), for messages that may quote an
    upstream request URL whose parameters carry a session, token or personal identifier under any name (#261)."""
    msg = redact(str(e))
    if drop_query:
        msg = _QUERY.sub(r"\1?<query>", msg)
    return (f"{type(e).__name__}: {msg}" if msg else type(e).__name__)[:limit]
