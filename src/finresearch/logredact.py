"""Secrets never reach a log line (issue #269).

Two layers, installed once per process when `finresearch` is imported:

1. Noisy HTTP loggers are kept at WARNING: httpcore at DEBUG writes raw request header bytes (an `Authorization:
   Bearer ...` line), httpx at INFO writes every URL, and the vendor SDKs some users install (growwapi, kiteconnect,
   requests/urllib3) log requests too. Nothing in FinResearch needs those lines.
2. Every log record is redacted as it is created (`logging.setLogRecordFactory`, so it covers handlers added later,
   such as uvicorn's dictConfig and the rotated serve.log): the formatted message and any exception text lose every
   secret value this process has read or stored (`register`, fed by `finresearch.secrets` on each resolve/put) and
   anything shaped like a bearer token or a token/secret/password assignment.

Values shorter than MIN_LEN are not registered (a 6-digit TOTP code or a 4-digit PIN would blank unrelated numbers);
they never travel outside the request that uses them.
"""

from __future__ import annotations

import logging
import re
import threading

MIN_LEN = 8
MASK = "[redacted]"
QUIET = ("httpx", "httpcore", "hpack", "urllib3", "requests", "growwapi", "kiteconnect", "upstox_client", "dhanhq",
         "nats", "websockets")  # fmt: skip
_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)((?:access[_-]?token|api[_-]?key|api[_-]?secret|totp[_-]?secret|password|secret|token)"
        r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]{6,}"
    ),
)
_lock = threading.Lock()
_values: set[str] = set()
_installed = False


def register(*values: str | None) -> None:
    """Remember secret values so no later log line can carry them (process memory only, never written anywhere)."""
    new = {v for v in values if isinstance(v, str) and len(v) >= MIN_LEN}
    if new - _values:
        with _lock:
            _values.update(new)


def redact(text: str) -> str:
    if not text:
        return text
    for v in sorted(
        _values, key=len, reverse=True
    ):  # longest first: a token that contains another stays whole
        if v in text:
            text = text.replace(v, MASK)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + MASK, text)
    return text


def _scrub(record: logging.LogRecord) -> logging.LogRecord:
    try:
        msg = record.getMessage()
    except Exception:  # a broken format string: leave it to the handler's own error path
        return record
    clean = redact(msg)
    if clean != msg:
        record.msg, record.args = clean, ()
    if record.exc_info and not record.exc_text:
        record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
    elif record.exc_text:
        record.exc_text = redact(record.exc_text)
    return record


def install() -> None:
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
        for name in QUIET:
            logging.getLogger(name).setLevel(logging.WARNING)
        base = logging.getLogRecordFactory()

        def factory(*args, **kwargs) -> logging.LogRecord:
            return _scrub(base(*args, **kwargs))

        logging.setLogRecordFactory(factory)
