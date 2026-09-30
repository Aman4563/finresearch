"""TOTP (RFC 6238, HMAC-SHA1, 30-second step, 6 digits): the one-time code an authenticator app shows.

Brokers that allow a headless daily login (Groww's TOTP flow, Angel One SmartAPI) accept the code generated from the
base32 seed the user copies when enabling TOTP. The seed is a secret: it stays in the local database and is only
read here. Golden tests use the RFC 6238 Appendix B vectors (https://www.rfc-editor.org/rfc/rfc6238#appendix-B).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time


def _key(seed: str) -> bytes:
    s = seed.replace(" ", "").replace("-", "").upper()
    if not s:
        raise ValueError("empty TOTP secret")
    try:
        return base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception as e:
        raise ValueError("the TOTP secret must be the base32 key (letters A-Z and digits 2-7)") from e


def hotp(key: bytes, counter: int, digits: int = 6, digest=hashlib.sha1) -> str:
    """RFC 4226 HOTP with dynamic truncation."""
    mac = hmac.new(key, struct.pack(">Q", counter), digest).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off : off + 4])[0] & 0x7FFFFFFF) % (10**digits)
    return str(code).zfill(digits)


def totp(seed: str, at: float | None = None, *, step: int = 30, digits: int = 6) -> str:
    """The code for base32 `seed` at unix time `at` (now by default)."""
    t = time.time() if at is None else at
    return hotp(_key(seed), int(t // step), digits)


def valid_seed(seed: str) -> bool:
    try:
        _key(seed)
        return True
    except ValueError:
        return False
