"""finresearch.backups.crypto: chunked AES-256-GCM for the off-machine backup copies (#375)."""

from __future__ import annotations

import os

import pytest

from finresearch.backups.crypto import BackupCryptoError, decrypt_file, encrypt_file


def _key() -> bytes:
    return os.urandom(32)  # built at runtime: never a literal key in the repo


@pytest.mark.parametrize("size", [0, 1, 1000, 4096, 4097, 3 * 4096 + 5])
def test_round_trip_any_size_across_frames(tmp_path, size):
    src, enc, out = tmp_path / "a.dump", tmp_path / "a.enc", tmp_path / "b.dump"
    data = os.urandom(size)
    src.write_bytes(data)
    key = _key()
    encrypt_file(src, enc, key, frame=4096)
    assert data == b"" or data not in enc.read_bytes()  # the plaintext never appears in the copy
    decrypt_file(enc, out, key)
    assert out.read_bytes() == data


def test_wrong_key_is_refused_and_leaves_no_output(tmp_path):
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "b"
    src.write_bytes(b"personal rows")
    encrypt_file(src, enc, _key())
    with pytest.raises(BackupCryptoError):
        decrypt_file(enc, out, _key())
    assert not out.exists() and not list(tmp_path.glob("*.partial"))


def test_a_flipped_byte_is_refused(tmp_path):
    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "b"
    src.write_bytes(os.urandom(10_000))
    key = _key()
    encrypt_file(src, enc, key, frame=4096)
    blob = bytearray(enc.read_bytes())
    blob[len(blob) // 2] ^= 1
    enc.write_bytes(bytes(blob))
    with pytest.raises(BackupCryptoError):
        decrypt_file(enc, out, key)
    assert not out.exists()


def test_a_copy_cut_at_a_frame_boundary_is_refused(tmp_path):
    """Dropping whole trailing frames must not decrypt to a shorter, valid-looking dump."""
    from finresearch.backups import crypto

    src, enc, out = tmp_path / "a", tmp_path / "a.enc", tmp_path / "b"
    src.write_bytes(os.urandom(3 * 4096))
    key = _key()
    encrypt_file(src, enc, key, frame=4096)
    blob = enc.read_bytes()
    one_frame = 4 + 4096 + 16  # length prefix + ciphertext + GCM tag
    enc.write_bytes(blob[: crypto.HEADER_LEN + 2 * one_frame])
    with pytest.raises(BackupCryptoError):
        decrypt_file(enc, out, key)


def test_not_a_backup_file_is_refused(tmp_path):
    p = tmp_path / "x.enc"
    p.write_bytes(b"PGDMP plain dump")
    with pytest.raises(BackupCryptoError):
        decrypt_file(p, tmp_path / "y", _key())


def test_a_short_key_is_refused(tmp_path):
    (tmp_path / "a").write_bytes(b"x")
    with pytest.raises(BackupCryptoError):
        encrypt_file(tmp_path / "a", tmp_path / "a.enc", b"short")
