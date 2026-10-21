"""Encrypted off-machine copies of the database dumps and their freshness (#375; docs/BACKUPS.md).

The destination is a directory the owner chooses (setting `backup_offsite_dir`): a mounted drive, NAS share or a
cloud-synced folder. Nothing leaves this Mac unencrypted: every copy is written by `crypto.encrypt_file` with a
32-byte key kept in the macOS Keychain through `finresearch.secrets` (`finresearch backup init-key` creates it once;
nothing ever replaces it, because the copies already made would become unreadable).

Layout at the destination, one folder per kind (`db` = whole database, `tables` = the forward-only tables, `vault`):

    <dest>/<kind>/finresearch-<kind>-<UTC stamp.µs>-<pid>.enc    the encrypted pg_dump (custom format)
    <dest>/<kind>/finresearch-<kind>-<UTC stamp.µs>-<pid>.json   manifest: time, size, sha256 of the plain dump, key id,
                                                              and row counts taken in the dump's own snapshot
    <dest>/drills.jsonl                                       one line per restore drill (backups.drill)

Manifests hold table names and row counts only, no row data. The destination root must already exist: an unmounted
drive must fail, not quietly create a local folder on the same disk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finresearch.backups.crypto import KEY_LEN, BackupCryptoError, decrypt_file, encrypt_file

# Tables whose rows are recorded forward only and cannot be fetched again later (roadmap P1.13): the option-quote
# recorder (#363), the F&O card ledger (#360), the IPO decision-time subscription snapshots (#244) and the intraday
# 1-minute series (#366: NSE serves only the current session).
FORWARD_ONLY = ("option_quote", "card_log", "subscription_snapshot", "intraday_series")
KEEP = {"db": 7, "tables": 14, "vault": 2}
MAX_AGE = timedelta(days=1)
DRILL_EVERY = timedelta(days=92)  # quarterly
DRILL_LOG = "drills.jsonl"
_READ = object()


class OffsiteError(RuntimeError):
    """An off-machine copy could not be made or read (messages never contain key material)."""


def key_ref() -> str:
    from finresearch import secrets

    return secrets.ref_for("backup", "offsite_key")


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def init_key() -> str:
    """Create the backup key once; refuse when one already exists. Returns the key's reference (not the key)."""
    from finresearch import secrets

    ref = key_ref()
    if secrets.backend().get(ref):
        raise OffsiteError(f"a backup key already exists ({ref}); it is never replaced")
    secrets.backend().set(ref, _b64(os.urandom(KEY_LEN)))
    return ref


def load_key() -> bytes:
    from finresearch import secrets

    raw = secrets.backend().get(key_ref())
    if not raw:
        raise OffsiteError(
            "no backup key: run `finresearch backup init-key` once (and keep a copy, docs/BACKUPS.md)"
        )
    key = base64.b64decode(raw)
    if len(key) != KEY_LEN:
        raise OffsiteError("the stored backup key is not 32 bytes")
    return key


def key_id(key: bytes | None = None) -> str:
    """A short public fingerprint of the key, so a copy made with another key is named as such."""
    return hashlib.sha256(b"finresearch-backup-key-id:" + (key or load_key())).hexdigest()[:16]


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _root(dest: Path | str | None) -> Path:
    if dest is None or str(dest) == "":
        raise OffsiteError("no off-machine destination: set FINRESEARCH_BACKUP_OFFSITE_DIR")
    root = Path(dest).expanduser()
    if not root.is_dir():
        raise OffsiteError(f"the off-machine destination {root} does not exist (not mounted?)")
    return root


def push(src: Path, dest: Path | str | None, kind: str, *, counts: dict[str, int] | None,
         keep: int | None = None, extra: dict[str, Any] | None = None) -> Path:  # fmt: skip
    """Encrypt the plain dump `src` to `<dest>/<kind>/`, read it back to check it decrypts to the same bytes, write
    its manifest last (a copy without a manifest is never counted as fresh), and keep the newest `keep` copies."""
    if kind not in KEEP:
        raise OffsiteError(f"unknown backup kind {kind!r}")
    key = load_key()
    folder = _root(dest) / kind
    folder.mkdir(mode=0o700, exist_ok=True)
    now = datetime.now(UTC)
    name = (
        f"finresearch-{kind}-{now:%Y%m%dT%H%M%S.%fZ}-{os.getpid()}"  # microseconds: two pushes never collide
    )
    enc, man = folder / f"{name}.enc", folder / f"{name}.json"
    sha = _sha256(src)
    encrypt_file(src, enc, key)
    check = src.with_name(src.name + ".verify")
    try:
        decrypt_file(enc, check, key)
        if _sha256(check) != sha:
            raise OffsiteError(f"the copy {enc.name} does not read back to the dump it was made from")
    except (BackupCryptoError, OffsiteError):
        enc.unlink(missing_ok=True)
        raise
    finally:
        check.unlink(missing_ok=True)
    manifest = {"kind": kind, "created_at": now.isoformat(), "file": enc.name, "bytes": src.stat().st_size,
                "sha256": sha, "key_id": key_id(key), "counts": counts, **(extra or {})}  # fmt: skip
    tmp = man.with_suffix(".json.partial")
    tmp.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, man)
    _rotate(folder, kind, keep if keep is not None else KEEP[kind])
    return enc


def _manifests(folder: Path) -> list[tuple[Path, dict[str, Any]]]:
    out = []
    for m in sorted(folder.glob("finresearch-*.json")):
        try:
            out.append((m, json.loads(m.read_text())))
        except (OSError, ValueError):
            continue  # an unreadable manifest is not a copy
    return sorted(out, key=lambda x: x[1].get("created_at", ""))


def _rotate(folder: Path, kind: str, keep: int) -> None:
    if keep < 1:
        raise OffsiteError("keep must be at least 1")
    for m, _ in _manifests(folder)[:-keep]:
        m.with_suffix(".enc").unlink(missing_ok=True)
        m.unlink(missing_ok=True)


def newest(dest: Path | str | None, kind: str) -> Path:
    """The newest copy of `kind` that has a manifest."""
    got = [m for m, _ in _manifests(_root(dest) / kind) if m.with_suffix(".enc").exists()]
    if not got:
        raise OffsiteError(f"no {kind} copy at the destination")
    return got[-1].with_suffix(".enc")


def manifest_of(copy: Path) -> dict[str, Any]:
    return json.loads(copy.with_suffix(".json").read_text())


def fetch(copy: Path, out: Path) -> dict[str, Any]:
    """Decrypt `copy` to `out` and check it against its manifest; returns the manifest."""
    m = manifest_of(copy)
    key = load_key()
    if m.get("key_id") != key_id(key):
        raise OffsiteError(f"{copy.name} was made with a different key ({m.get('key_id')})")
    try:
        decrypt_file(copy, out, key)
    except BackupCryptoError as e:
        raise OffsiteError(f"{copy.name}: {e}") from None
    if _sha256(out) != m.get("sha256"):
        out.unlink(missing_ok=True)
        raise OffsiteError(f"{copy.name} decrypts to a dump that does not match its manifest")
    return m


def last_drill(dest: Path | str | None) -> dict[str, Any] | None:
    """The newest drill that passed, from `<dest>/drills.jsonl` (None when there is none or it cannot be read)."""
    try:
        lines = (_root(dest) / DRILL_LOG).read_text().splitlines()
    except (OSError, OffsiteError):
        return None
    ok = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except ValueError:
            continue
        if d.get("ok") is True and d.get("at"):
            ok.append(d)
    return max(ok, key=lambda d: d["at"]) if ok else None


def status(dest: Path | str | None, *, now: datetime | None = None, drill_at: Any = _READ) -> dict[str, Any]:
    """Per forward-only table: the newest off-machine copy holding it (a `db` or `tables` manifest that counted the
    table) and whether it is at most MAX_AGE old; and whether a drill passed within DRILL_EVERY. Anything that cannot
    be read is reported as not ok with a reason, never as fresh."""
    now = now or datetime.now(UTC)
    reason, seen = None, {}
    try:
        root = _root(dest)
        for kind in ("db", "tables"):
            for _, m in _manifests(root / kind):
                at = _when(m.get("created_at"))
                for t in (m.get("counts") or {}) if at else ():
                    if t in FORWARD_ONLY and (t not in seen or at > seen[t]):
                        seen[t] = at
    except OffsiteError as e:
        reason = str(e)
    tables = []
    for t in FORWARD_ONLY:
        at = seen.get(t)
        row = {"table": t, "copied_at": at.isoformat() if at else None, "ok": False, "reason": None}
        if at is None:
            row["reason"] = reason or "no off-machine copy of this table"
        elif now - at > MAX_AGE:
            row["reason"] = f"newest copy is older than {MAX_AGE}"
        else:
            row["ok"] = True
        tables.append(row)
    if drill_at is _READ:
        d = last_drill(dest)
        drill_at = _when(d["at"]) if d else None
    drill = {"at": drill_at.isoformat() if drill_at else None, "ok": False, "reason": None}
    if drill_at is None:
        drill["reason"] = "no restore drill has passed"
    elif now - drill_at > DRILL_EVERY:
        drill["reason"] = f"last passing drill is older than {DRILL_EVERY.days} days"
    else:
        drill["ok"] = True
    return {"ok": all(r["ok"] for r in tables) and drill["ok"], "tables": tables, "drill": drill}


def _when(v: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None
    return at if at.tzinfo else None  # a naive time is not trusted as fresh


def work_dir():
    """A private temporary folder on this Mac's own disk (the state dir) for plain dumps on their way out or in."""
    import tempfile

    from finresearch.config import get_settings

    base = Path(get_settings().state_dir)
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix="offsite-", dir=base)  # mode 0700


def backup_now(url: str | None, dest: Path | str | None, *, kinds: tuple[str, ...] = ("db", "tables"),
               vault_url: str | None = None) -> dict[str, Path]:  # fmt: skip
    """Dump and push each kind: `db` the whole database at `url` and `tables` its forward-only tables (both with
    row counts from the dump's snapshot), `vault` the vault database (no row counts: counting is a read of sealed
    data, so its drill checks only that the copy restores). The plain dumps are deleted as soon as they are pushed."""
    from finresearch.backups.dump import dump_with_counts
    from finresearch.config import get_settings

    load_key()  # fail before any dump when there is no key
    _root(dest)
    made: dict[str, Path] = {}
    with work_dir() as tmp:
        for kind in kinds:
            src = url or get_settings().database_url
            if kind == "vault":
                src = vault_url or get_settings().vault_database_url
                if not src:
                    raise OffsiteError("no vault database is configured (FINRESEARCH_VAULT_DATABASE_URL)")
            plain = Path(tmp) / f"{kind}.dump"
            counts = dump_with_counts(src, plain, tables=FORWARD_ONLY if kind == "tables" else None,
                                      count=kind != "vault")  # fmt: skip
            made[kind] = push(plain, dest, kind, counts=counts)
            plain.unlink(missing_ok=True)
    return made
