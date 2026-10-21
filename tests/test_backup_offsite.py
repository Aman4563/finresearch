"""finresearch.backups.offsite: key handling, encrypted copies at a user-chosen destination, rotation and the
freshness status (#375). The destination is always a tmp directory here; the key lives in the memory backend."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from finresearch.backups import offsite


@pytest.fixture
def mem(monkeypatch, tmp_path):
    from finresearch import config, secrets

    monkeypatch.setenv("FINRESEARCH_SECRETS_BACKEND", "memory")
    monkeypatch.setenv("FINRESEARCH_DATABASE_URL", "postgresql+psycopg://localhost/finresearch_unit_test")
    monkeypatch.setenv("FINRESEARCH_STATE_DIR", str(tmp_path / "state"))
    config.get_settings.cache_clear()
    secrets.reset_memory()
    yield
    secrets.reset_memory()
    config.get_settings.cache_clear()


def _plain(tmp_path, body: bytes = b"PGDMP synthetic rows for Fake Co"):
    p = tmp_path / "x.dump"
    p.write_bytes(body)
    return p


def test_init_key_stores_a_32_byte_key_once(mem):
    offsite.init_key()
    key = offsite.load_key()
    assert len(key) == 32
    with pytest.raises(offsite.OffsiteError, match="already exists"):
        offsite.init_key()
    assert offsite.load_key() == key  # never silently replaced: old copies would become unreadable


def test_push_without_a_key_refuses_and_writes_nothing(mem, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    with pytest.raises(offsite.OffsiteError, match="init-key"):
        offsite.push(_plain(tmp_path), dest, "tables", counts={"card_log": 1})
    assert list(dest.rglob("*")) == []


def test_push_refuses_a_destination_that_does_not_exist(mem, tmp_path):
    """An unmounted drive or synced folder must fail loudly, not quietly create a local folder on this Mac."""
    offsite.init_key()
    with pytest.raises(offsite.OffsiteError, match="does not exist"):
        offsite.push(_plain(tmp_path), tmp_path / "not-mounted", "tables", counts={})
    assert not (tmp_path / "not-mounted").exists()


def test_push_writes_an_encrypted_copy_and_a_manifest(mem, tmp_path):
    offsite.init_key()
    dest = tmp_path / "dest"
    dest.mkdir()
    src = _plain(tmp_path)
    copy = offsite.push(src, dest, "tables", counts={"card_log": 3, "option_quote": 0})
    assert copy.parent == dest / "tables" and copy.suffix == ".enc"
    assert b"Fake Co" not in copy.read_bytes()
    m = json.loads(copy.with_suffix(".json").read_text())
    assert m["kind"] == "tables" and m["counts"] == {"card_log": 3, "option_quote": 0}
    assert m["bytes"] == src.stat().st_size and len(m["sha256"]) == 64 and m["key_id"] == offsite.key_id()
    assert datetime.fromisoformat(m["created_at"]).tzinfo is not None
    assert oct(copy.stat().st_mode & 0o777) == "0o600"
    out = tmp_path / "back.dump"
    offsite.fetch(copy, out)
    assert out.read_bytes() == src.read_bytes()


def test_push_keeps_the_newest_n_copies_per_kind(mem, tmp_path):
    offsite.init_key()
    dest = tmp_path / "dest"
    dest.mkdir()
    for i in range(4):
        offsite.push(_plain(tmp_path, b"v%d" % i), dest, "vault", counts=None, keep=2)
    offsite.push(_plain(tmp_path), dest, "tables", counts={}, keep=2)
    assert len(list((dest / "vault").glob("*.enc"))) == 2 and len(list((dest / "vault").glob("*.json"))) == 2
    newest = offsite.newest(dest, "vault")
    out = tmp_path / "o"
    offsite.fetch(newest, out)
    assert out.read_bytes() == b"v3"
    assert len(list((dest / "tables").glob("*.enc"))) == 1


def test_fetch_refuses_a_copy_made_with_another_key(mem, tmp_path):
    from finresearch import secrets

    offsite.init_key()
    dest = tmp_path / "dest"
    dest.mkdir()
    copy = offsite.push(_plain(tmp_path), dest, "db", counts={})
    secrets.backend().set(offsite.key_ref(), offsite._b64(os.urandom(32)))
    with pytest.raises(offsite.OffsiteError, match="different key"):
        offsite.fetch(copy, tmp_path / "o")


NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)


def _manifest(dest, kind, at, counts):
    d = dest / kind
    d.mkdir(parents=True, exist_ok=True)
    name = f"finresearch-{kind}-{at:%Y%m%dT%H%M%SZ}-1"
    (d / f"{name}.enc").write_bytes(b"x")
    (d / f"{name}.json").write_text(
        json.dumps({"kind": kind, "created_at": at.isoformat(), "counts": counts})
    )


def test_status_is_unknown_not_ok_when_the_destination_is_missing_or_empty(tmp_path):
    for dest in (None, tmp_path / "gone", tmp_path):
        st = offsite.status(dest, now=NOW)
        assert st["ok"] is False
        assert {t["table"] for t in st["tables"]} == set(offsite.FORWARD_ONLY)
        assert all(t["ok"] is False and t["copied_at"] is None and t["reason"] for t in st["tables"])


def test_status_needs_every_forward_only_table_copied_within_a_day(tmp_path):
    every = dict.fromkeys(offsite.FORWARD_ONLY, 5)
    _manifest(tmp_path, "tables", NOW - timedelta(hours=23), every)
    st = offsite.status(tmp_path, now=NOW, drill_at=NOW - timedelta(days=10))
    assert st["ok"] is True and all(t["ok"] for t in st["tables"])

    _manifest(tmp_path, "tables", NOW - timedelta(hours=1), {"card_log": 5})  # newer copy missing two tables
    st = offsite.status(tmp_path, now=NOW, drill_at=NOW - timedelta(days=10))
    by = {t["table"]: t for t in st["tables"]}
    assert by["card_log"]["ok"] is True
    assert by["option_quote"]["ok"] is True  # still covered by the 23-hour-old copy

    st = offsite.status(tmp_path, now=NOW + timedelta(hours=2), drill_at=NOW - timedelta(days=10))
    by = {t["table"]: t for t in st["tables"]}
    assert by["option_quote"]["ok"] is False and "older than" in by["option_quote"]["reason"]
    assert st["ok"] is False


def test_status_flags_a_missing_or_overdue_drill(tmp_path):
    _manifest(tmp_path, "tables", NOW - timedelta(hours=1), dict.fromkeys(offsite.FORWARD_ONLY, 1))
    assert offsite.status(tmp_path, now=NOW, drill_at=None)["drill"]["ok"] is False
    assert offsite.status(tmp_path, now=NOW, drill_at=NOW - timedelta(days=93))["drill"]["ok"] is False
    assert offsite.status(tmp_path, now=NOW, drill_at=NOW - timedelta(days=91))["drill"]["ok"] is True
