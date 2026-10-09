"""Where secrets live: the macOS Keychain. Database rows keep only a reference and a masked hint (issue #245).

A secret (broker API secret, TOTP seed, access token, PIN, the opt-in CAS password, ntfy topic/token, Telegram bot
token, the local API token) is written to a backend and the row stores `{"secret_ref": "<ref>", "hint": "••••"}` in a
JSON column, or `secret_ref:<ref>` in a text column. Reads resolve the reference through the backend, so a database
dump (scripts/backup.sh) carries no secret.

Backends (setting `secrets_backend`, env FINRESEARCH_SECRETS_BACKEND):
- `keychain`: the user's login Keychain through `/usr/bin/security`. The value goes to `security -i` on stdin, never
  on the command line (where `ps` would show it to every local user). Items are generic passwords with service
  `finresearch/<database>` and account `<scope>/<field>`, so a second database never overwrites the live one's.
  Created by /usr/bin/security, they are readable by it without a Keychain prompt.
- `file`: a 0600 JSON file under the state dir, for Linux/CI and throw-away test servers.
- `memory`: per process, for tests.
- `auto` (default): `keychain` on macOS for a database whose name does not end in `_test`, else `file`.
The keychain backend refuses to run under pytest or against a `*_test` database: tests never touch the user's
Keychain.

Values are never logged or printed; `finresearch secrets migrate` and `check` print counts and field names only.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

REF_KEY = "secret_ref"
TEXT_PREFIX = "secret_ref:"  # a reference in a text column (broker_connection.token)
SECURITY_BIN = "/usr/bin/security"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}(/[A-Za-z0-9._-]{1,80})*$")
# a value read from the Keychain is reused for a minute (one `security` call each, not one per alert)
_CACHE_S = 60.0


class SecretStoreError(RuntimeError):
    """The secret store could not be read or written (the message never contains a value)."""


class Backend(Protocol):
    name: str

    def get(self, ref: str) -> str | None: ...
    def set(self, ref: str, value: str) -> None: ...
    def delete(self, ref: str) -> None: ...


def _split(ref: str) -> tuple[str, str]:
    """Split a reference finresearch/<db>/<scope>/<field> into (service, account)."""
    parts = ref.split("/")
    if len(parts) < 4 or not _NAME.match(ref):
        raise SecretStoreError(f"not a secret reference: {ref[:80]!r}")
    return "/".join(parts[:2]), "/".join(parts[2:])


class MemoryBackend:
    name = "memory"

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.data.get(ref)

    def set(self, ref: str, value: str) -> None:
        _split(ref)
        self.data[ref] = value

    def delete(self, ref: str) -> None:
        self.data.pop(ref, None)


class FileBackend:
    """A JSON object in a 0600 file, rewritten atomically under an exclusive lock."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _write(self, data: dict[str, str]) -> None:
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, ref: str) -> str | None:
        with self._locked():
            return self._read().get(ref)

    def set(self, ref: str, value: str) -> None:
        _split(ref)
        with self._locked():
            data = self._read()
            data[ref] = value
            self._write(data)

    def delete(self, ref: str) -> None:
        with self._locked():
            data = self._read()
            if data.pop(ref, None) is not None:
                self._write(data)


class KeychainBackend:
    """Generic passwords in the login Keychain, through /usr/bin/security. Values are stored as `b64:<base64>` so
    any text round-trips (security prints non-printable passwords as hex)."""

    name = "keychain"

    def __init__(self, run: Any = subprocess.run) -> None:
        if "PYTEST_CURRENT_TEST" in os.environ and run is subprocess.run:
            raise SecretStoreError("the Keychain backend is never used by tests (use the memory backend)")
        self._run = run

    def get(self, ref: str) -> str | None:
        service, account = _split(ref)
        p = self._run([SECURITY_BIN, "find-generic-password", "-s", service, "-a", account, "-w"],
                      capture_output=True, text=True, timeout=20)  # fmt: skip
        if p.returncode == 44:  # errSecItemNotFound
            return None
        if p.returncode != 0:
            raise SecretStoreError(f"Keychain read failed for {ref} (security exit {p.returncode})")
        out = p.stdout.rstrip("\n")
        if out.startswith("b64:"):
            return base64.b64decode(out[4:]).decode("utf-8")
        return out

    def set(self, ref: str, value: str) -> None:
        service, account = _split(ref)
        enc = "b64:" + base64.b64encode(value.encode("utf-8")).decode("ascii")
        # `security -i` reads the command from stdin: the value never appears in any process's argv
        cmd = f"add-generic-password -U -s {service} -a {account} -l {service}/{account} -w {enc}\n"
        p = self._run([SECURITY_BIN, "-i"], input=cmd, capture_output=True, text=True, timeout=20)
        if p.returncode != 0 or self.get(ref) != value:  # `security -i` can exit 0 on a failed command
            raise SecretStoreError(f"Keychain write failed for {ref}")

    def delete(self, ref: str) -> None:
        service, account = _split(ref)
        p = self._run([SECURITY_BIN, "delete-generic-password", "-s", service, "-a", account],
                      capture_output=True, text=True, timeout=20)  # fmt: skip
        if p.returncode not in (0, 44):
            raise SecretStoreError(f"Keychain delete failed for {ref} (security exit {p.returncode})")


class _Cached:
    """A short read cache in front of a backend (writes and deletes go straight through and update it)."""

    def __init__(self, inner: Backend, ttl: float = _CACHE_S) -> None:
        self.inner, self.name, self.ttl = inner, inner.name, ttl
        self._lock = threading.Lock()
        self._hits: dict[str, tuple[float, str | None]] = {}

    def get(self, ref: str) -> str | None:
        with self._lock:
            hit = self._hits.get(ref)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1]
        v = self.inner.get(ref)
        with self._lock:
            self._hits[ref] = (time.monotonic(), v)
        return v

    def set(self, ref: str, value: str) -> None:
        self.inner.set(ref, value)
        with self._lock:
            self._hits[ref] = (time.monotonic(), value)

    def delete(self, ref: str) -> None:
        self.inner.delete(ref)
        with self._lock:
            self._hits.pop(ref, None)


# --------------------------------------------------------------------------- the process's store
_lock = threading.Lock()
_backend: tuple[tuple[str, str, str], Backend] | None = None
_MEMORY = MemoryBackend()


def _db_name() -> str:
    from sqlalchemy.engine import make_url

    from finresearch.config import get_settings

    return make_url(get_settings().database_url).database or "finresearch"


def backend() -> Backend:
    """The configured backend (rebuilt when the setting, database or state dir changes)."""
    global _backend
    from finresearch.config import get_settings

    s = get_settings()
    db = _db_name()
    key = (s.secrets_backend, db, str(s.state_dir))
    with _lock:
        if _backend is not None and _backend[0] == key:
            return _backend[1]
        kind = s.secrets_backend
        if kind == "auto":
            kind = "keychain" if sys.platform == "darwin" and not db.endswith("_test") else "file"
        if kind == "keychain":
            if db.endswith("_test"):
                raise SecretStoreError("refusing the Keychain for a *_test database (use the file backend)")
            if not Path(SECURITY_BIN).exists():
                raise SecretStoreError(f"{SECURITY_BIN} not found: the Keychain backend needs macOS")
            b: Backend = _Cached(KeychainBackend())
        elif kind == "file":
            b = FileBackend(Path(s.state_dir) / "secrets.json")
        elif kind == "memory":
            b = _MEMORY
        else:
            raise SecretStoreError(f"unknown secrets backend {kind!r}")
        _backend = (key, b)
        return b


def reset_memory() -> None:
    """Test helper: empty the memory backend and forget the cached choice."""
    global _backend
    with _lock:
        _MEMORY.data.clear()
        _backend = None


def ref_for(scope: str, field: str) -> str:
    return f"finresearch/{_db_name()}/{scope}/{field}"


def is_ref(v: Any) -> bool:
    return (isinstance(v, dict) and isinstance(v.get(REF_KEY), str)) or (
        isinstance(v, str) and v.startswith(TEXT_PREFIX)
    )


def ref_of(v: Any) -> str | None:
    if isinstance(v, dict) and isinstance(v.get(REF_KEY), str):
        return v[REF_KEY]
    if isinstance(v, str) and v.startswith(TEXT_PREFIX):
        return v[len(TEXT_PREFIX) :]
    return None


def put(scope: str, field: str, value: str, hint: str = "") -> dict[str, str]:
    """Store `value` and return the JSON reference a row keeps instead."""
    ref = ref_for(scope, field)
    _no_logs(value)
    backend().set(ref, value)
    return {REF_KEY: ref, "hint": hint}


def put_text(scope: str, field: str, value: str) -> str:
    """Store `value` and return the text reference a text column keeps instead."""
    return TEXT_PREFIX + put(scope, field, value)[REF_KEY]


def resolve(v: Any) -> str:
    """The plaintext for a stored value: a reference is read from the store, a legacy plain string passes through
    (until `finresearch secrets migrate --apply`), and empty stays empty. A reference whose item is gone reads as ""
    (the field then shows as not set, so the user is asked for it again) and is logged by reference only."""
    ref = ref_of(v)
    if ref is None:
        return "" if v is None else str(v)
    got = backend().get(ref)
    if got is None:
        log.warning("secret %s is missing from the %s store", ref, backend().name)
        return ""
    _no_logs(got)
    return got


def _no_logs(value: str) -> None:
    """Every secret this process reads or stores is redacted from its log lines from then on (logredact, #269)."""
    from finresearch import logredact

    logredact.register(value)


def drop(v: Any) -> None:
    """Delete the stored secret behind a reference (a plain value or empty does nothing)."""
    if (ref := ref_of(v)) is not None:
        backend().delete(ref)


def keep(scope: str, field: str, new: str, old: Any, hint: str = "••••") -> dict[str, str] | str:
    """What a row stores for a secret field after a save: "" when cleared (the old item is deleted), the old reference
    when the value is unchanged, else a new reference (also when the old value was legacy plaintext)."""
    if not new:
        drop(old)
        return ""
    if is_ref(old) and resolve(old) == new:
        return old
    return put(scope, field, new, hint)


def keep_text(scope: str, field: str, new: str | None, old: Any) -> str | None:
    """`keep` for a text column: None when cleared."""
    if not new:
        drop(old)
        return None
    if is_ref(old) and resolve(old) == new:
        return old
    return put_text(scope, field, new)
