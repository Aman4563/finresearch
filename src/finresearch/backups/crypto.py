"""Chunked AES-256-GCM file encryption for the off-machine backup copies (#375).

A dump can be hundreds of MB and the Mac also runs the live app, so the file is encrypted in frames rather than read
into memory whole. Layout:

    header  = MAGIC (6 bytes) | version (1) | frame size (4, big-endian) | nonce prefix (7, random per file)
    frame   = ciphertext length (4, big-endian) | AES-GCM ciphertext + 16-byte tag

Frame i uses the nonce `prefix | i (4 bytes) | final flag (1 byte)` and the header as associated data, so a frame
cannot be reordered, moved to another file or edited, and dropping whole trailing frames is detected because the
last frame must carry the final flag (an empty file is one empty final frame). Decryption writes `<dst>.partial`
and renames it only after every frame has authenticated: a wrong key or damaged copy leaves no output.
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"FRBKP\x00"
VERSION = 1
KEY_LEN = 32
FRAME = 16 * 1024 * 1024
_MAX_FRAME = 256 * 1024 * 1024  # a header asking for more is damaged, not read into memory
_TAG = 16
HEADER_LEN = len(MAGIC) + 1 + 4 + 7


class BackupCryptoError(RuntimeError):
    """The copy could not be encrypted or authenticated (wrong key, damaged, truncated or not a backup copy)."""


def _aes(key: bytes) -> AESGCM:
    if not isinstance(key, bytes) or len(key) != KEY_LEN:
        raise BackupCryptoError("the backup key must be 32 bytes")
    return AESGCM(key)


def _nonce(prefix: bytes, i: int, final: bool) -> bytes:
    if i >= 2**32:
        raise BackupCryptoError("file too large for one backup copy")
    return prefix + struct.pack(">IB", i, 1 if final else 0)


def encrypt_file(src: Path, dst: Path, key: bytes, *, frame: int = FRAME) -> None:
    """Encrypt `src` into `dst` (written as `<dst>.partial`, then renamed; mode 0600)."""
    aes = _aes(key)
    prefix = os.urandom(7)
    header = MAGIC + struct.pack(">BI", VERSION, frame) + prefix
    tmp = Path(str(dst) + ".partial")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            fout.write(header)
            i, chunk = 0, fin.read(frame)
            while True:
                nxt = fin.read(frame) if len(chunk) == frame else b""
                final = nxt == b""
                ct = aes.encrypt(_nonce(prefix, i, final), chunk, header)
                fout.write(struct.pack(">I", len(ct)) + ct)
                if final:
                    break
                i, chunk = i + 1, nxt
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


def decrypt_file(src: Path, dst: Path, key: bytes) -> None:
    """Decrypt `src` into `dst`; raise BackupCryptoError (and write nothing) unless every frame authenticates."""
    aes = _aes(key)
    tmp = Path(str(dst) + ".partial")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with open(src, "rb") as fin, os.fdopen(fd, "wb") as fout:
            header = fin.read(HEADER_LEN)
            if len(header) != HEADER_LEN or not header.startswith(MAGIC):
                raise BackupCryptoError("not a FinResearch backup copy")
            version, frame = struct.unpack(">BI", header[len(MAGIC) : len(MAGIC) + 5])
            if version != VERSION:
                raise BackupCryptoError(f"unsupported backup copy version {version}")
            if not 0 < frame <= _MAX_FRAME:
                raise BackupCryptoError("backup copy is damaged (bad frame size)")
            prefix, i = header[-7:], 0
            while True:
                raw = fin.read(4)
                if len(raw) != 4:
                    raise BackupCryptoError("backup copy is truncated (no final frame)")
                (n,) = struct.unpack(">I", raw)
                if n < _TAG or n > frame + _TAG:
                    raise BackupCryptoError("backup copy is damaged (bad frame length)")
                ct = fin.read(n)
                if len(ct) != n:
                    raise BackupCryptoError("backup copy is truncated")
                final = fin.peek(1)[:1] == b""  # nothing after this frame
                try:
                    fout.write(aes.decrypt(_nonce(prefix, i, final), ct, header))
                except InvalidTag:
                    raise BackupCryptoError(
                        "backup copy failed authentication (wrong key or damaged)"
                    ) from None
                if final:
                    break
                i += 1
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
