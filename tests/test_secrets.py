"""#245: secrets live in the secret store (the Keychain on the Mac); database rows keep only references.

All values here are synthetic. The tests run on the memory backend (conftest); the Keychain backend is exercised
through a fake `security` runner, never the real Keychain.
"""

from __future__ import annotations

import base64
import subprocess
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from finresearch import secrets as secret_store

SEED = "JBSWY3DPEHPK3PXP"  # the RFC 4226 example-style base32 seed, not anyone's
# synthetic values, split so secret scanners don't read the fixtures as credentials
API_KEY = "synthetic-gro" + "ww-key-7f3a9c"
API_SECRET = "synthetic-kite" + "-secret-b81d2e"
ACCESS = "synthetic-acce" + "ss-token-0c4d55"
CAS_PW = "ABCDE1234F"  # a made-up PAN
NTFY_TOPIC = "finresearch-synthetictopic123"
NTFY_TOKEN = "tk_syntheticntfytoken99"
TG_TOKEN = "123456789:AAsyntheticTelegramBotToken_abcdefgh"
ALL = (SEED, API_KEY, API_SECRET, ACCESS, CAS_PW, NTFY_TOPIC, NTFY_TOKEN, TG_TOKEN)


@pytest.fixture
def db(env):
    from finresearch.db import session_scope

    def wipe():
        with session_scope() as s:
            s.execute(text("TRUNCATE broker_connection, notification_setting CASCADE"))

    wipe()
    yield session_scope
    wipe()


def _raw_rows(session_scope) -> str:
    """Everything the two tables hold, as pg_dump would see it."""
    with session_scope() as s:
        a = s.execute(text("SELECT coalesce(string_agg(config::text || coalesce(token, ''), '|'), '') "
                           "FROM broker_connection")).scalar()  # fmt: skip
        b = s.execute(
            text("SELECT coalesce(string_agg(value::text, '|'), '') FROM notification_setting")
        ).scalar()
    return a + "|" + b


def test_saved_broker_and_notification_secrets_never_reach_the_database(db):
    from finresearch.db.models import BrokerConnection
    from finresearch.monitor import notify
    from finresearch.portfolio.connectors.base import TokenGrant
    from finresearch.portfolio.connectors.store import INBOX_KEY, build, public_one, set_token, update

    with db() as s:
        update(s, "groww", {"config": {"api_key": API_KEY, "totp_secret": SEED}})
        update(s, "zerodha", {"config": {"api_key": "kite-public-key", "api_secret": API_SECRET}})
        update(s, INBOX_KEY, {"config": {"password": CAS_PW}})
        set_token(s, "zerodha", TokenGrant(ACCESS, datetime.now(UTC) + timedelta(hours=8)))
        notify.update(s, {"ntfy": {"enabled": True, "topic": NTFY_TOPIC, "token": NTFY_TOKEN},
                          "telegram": {"enabled": True, "bot_token": TG_TOKEN, "chat_id": "12345"}})  # fmt: skip
    raw = _raw_rows(db)
    assert not [v for v in ALL if v in raw], "a secret value reached a database row"
    assert "secret_ref" in raw and "kite-public-key" in raw  # non-secret settings stay readable
    with db() as s:
        groww = build(s.get(BrokerConnection, "groww"))
        assert (groww.config["api_key"], groww.config["totp_secret"]) == (API_KEY, SEED)
        kite = build(s.get(BrokerConnection, "zerodha"))
        assert kite.token == ACCESS and kite.config["api_secret"] == API_SECRET
        assert notify.load(s, "ntfy").token == NTFY_TOKEN and notify.load(s, "telegram").bot_token == TG_TOKEN
        shown = public_one(s, "groww")["config"]
        assert shown["api_key"] == notify.MASK and shown["api_key_set"] is True
        pub = str(notify.public(s))
        assert not [v for v in ALL if v in pub]


def test_clearing_and_disconnecting_delete_the_stored_secrets(db):
    from finresearch.monitor import notify
    from finresearch.portfolio.connectors.base import TokenGrant
    from finresearch.portfolio.connectors.store import disconnect, set_token, update

    mem = secret_store.backend()
    with db() as s:
        update(s, "zerodha", {"config": {"api_key": "k", "api_secret": API_SECRET}})
        set_token(s, "zerodha", TokenGrant(ACCESS, None))
        notify.update(s, {"ntfy": {"topic": NTFY_TOPIC, "token": NTFY_TOKEN}})
    assert set(mem.data.values()) == {API_SECRET, ACCESS, NTFY_TOPIC, NTFY_TOKEN}
    with db() as s:
        # a new credential drops the old token (it belonged to the old credentials), and its stored copy
        update(s, "zerodha", {"config": {"api_secret": API_SECRET + "-new"}})
        notify.update(s, {"ntfy": {"clear_token": True}})
    assert set(mem.data.values()) == {API_SECRET + "-new", NTFY_TOPIC}
    with db() as s:
        assert disconnect(s, "zerodha")
    assert set(mem.data.values()) == {NTFY_TOPIC}


def _plant_plaintext(db) -> None:
    """Rows as a version before #245 wrote them."""
    from finresearch.db.models import BrokerConnection, NotificationSetting

    with db() as s:
        s.add(BrokerConnection(key="groww", config={"api_key": API_KEY, "totp_secret": SEED}, token=ACCESS,
                               state={}, status="connected"))  # fmt: skip
        s.add(BrokerConnection(key="cas_inbox", config={"password": CAS_PW, "password_rev": "r1"}, state={}))
        s.add(NotificationSetting(key="ntfy", value={"enabled": True, "server": "https://ntfy.sh",
                                                     "topic": NTFY_TOPIC, "token": NTFY_TOKEN}))  # fmt: skip
        s.add(
            NotificationSetting(
                key="telegram", value={"enabled": True, "bot_token": TG_TOKEN, "chat_id": "1"}
            )
        )
        s.add(NotificationSetting(key="general", value={"app_url": "http://127.0.0.1:3100"}))


def test_migrate_moves_plaintext_once_and_check_passes_after(db):
    from finresearch.db.models import BrokerConnection
    from finresearch.monitor import notify
    from finresearch.portfolio.connectors.store import build
    from finresearch.secrets_migrate import migrate, scan

    _plant_plaintext(db)
    with db() as s:
        rep = scan(s)
    assert rep.counts() == {"broker_connection": 4, "notification_setting": 3}
    with db() as s:
        dry = migrate(s, apply=False)
    assert dry.moved == 0 and all(
        v in _raw_rows(db) for v in (API_KEY, SEED, ACCESS, CAS_PW, NTFY_TOKEN, TG_TOKEN)
    )
    with db() as s:
        assert migrate(s, apply=True).moved == 7
    raw = _raw_rows(db)
    assert not [v for v in ALL if v in raw]
    with db() as s:
        assert scan(s).found == [] and migrate(s, apply=True).moved == 0  # idempotent
        g = build(s.get(BrokerConnection, "groww"))
        assert (g.config["api_key"], g.config["totp_secret"], g.token) == (API_KEY, SEED, ACCESS)
        assert notify.load(s, "ntfy").topic == NTFY_TOPIC and notify.load(s, "telegram").bot_token == TG_TOKEN
        assert s.get(BrokerConnection, "cas_inbox").config["password_rev"] == "r1"


def test_secrets_cli_check_fails_on_plaintext_and_never_prints_values(db):
    from finresearch.cli import app

    _plant_plaintext(db)
    runner = CliRunner()
    bad = runner.invoke(app, ["secrets", "check"])
    assert (
        bad.exit_code == 1
        and "broker_connection: 4" in bad.output
        and "notification_setting: 3" in bad.output
    )
    dry = runner.invoke(app, ["secrets", "migrate"])
    assert dry.exit_code == 0 and "dry run" in dry.output
    assert runner.invoke(app, ["secrets", "check"]).exit_code == 1
    done = runner.invoke(app, ["secrets", "migrate", "--apply"])
    assert done.exit_code == 0 and "moved 7" in done.output, done.output
    ok = runner.invoke(app, ["secrets", "check"])
    assert ok.exit_code == 0, ok.output
    printed = bad.output + dry.output + done.output + ok.output
    assert not [v for v in ALL if v in printed]


class FakeSecurity:
    """Records `security` invocations; keeps items in a dict like the Keychain would."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.argv: list[list[str]] = []

    def __call__(self, argv, input=None, **kw):
        self.argv.append(list(argv))
        out, rc = "", 0
        if argv[1] == "-i":
            words = input.split()
            opts = {w: words[i + 1] for i, w in enumerate(words) if w in ("-s", "-a", "-w", "-l")}
            self.items[(opts["-s"], opts["-a"])] = opts["-w"]
        elif argv[1] == "find-generic-password":
            k = (argv[argv.index("-s") + 1], argv[argv.index("-a") + 1])
            out, rc = (self.items[k] + "\n", 0) if k in self.items else ("", 44)
        elif argv[1] == "delete-generic-password":
            k = (argv[argv.index("-s") + 1], argv[argv.index("-a") + 1])
            rc = 0 if self.items.pop(k, None) is not None else 44
        return subprocess.CompletedProcess(argv, rc, out, "")


def test_keychain_backend_passes_values_on_stdin_never_argv():
    fake = FakeSecurity()
    kc = secret_store.KeychainBackend(run=fake)
    ref = "finresearch/finresearch/broker.groww/api_key"
    value = 'synthetic secret with spaces ₹ and "quotes"'
    kc.set(ref, value)
    assert kc.get(ref) == value
    assert not any(value in " ".join(a) or "b64:" in " ".join(a) for a in fake.argv)
    stored = fake.items[("finresearch/finresearch", "broker.groww/api_key")]
    assert base64.b64decode(stored[4:]).decode() == value
    kc.delete(ref)
    assert kc.get(ref) is None
    kc.delete(ref)  # deleting a missing item is fine
    with pytest.raises(secret_store.SecretStoreError):
        kc.get("not a ref")


def test_tests_and_test_databases_never_use_the_keychain(monkeypatch):
    from finresearch import config

    with pytest.raises(secret_store.SecretStoreError):
        secret_store.KeychainBackend()  # the real `security` runner under pytest
    monkeypatch.setenv("FINRESEARCH_DATABASE_URL", "postgresql+psycopg://localhost/finresearch_x_test")
    monkeypatch.setenv("FINRESEARCH_SECRETS_BACKEND", "auto")
    config.get_settings.cache_clear()
    assert secret_store.backend().name == "file"
    monkeypatch.setenv("FINRESEARCH_SECRETS_BACKEND", "keychain")
    config.get_settings.cache_clear()
    with pytest.raises(secret_store.SecretStoreError):
        secret_store.backend()


def test_file_backend_is_private(tmp_path):
    fb = secret_store.FileBackend(tmp_path / "state" / "secrets.json")
    fb.set("finresearch/db/notify.ntfy/token", NTFY_TOKEN)
    assert fb.get("finresearch/db/notify.ntfy/token") == NTFY_TOKEN
    assert (tmp_path / "state" / "secrets.json").stat().st_mode & 0o777 == 0o600
    fb.delete("finresearch/db/notify.ntfy/token")
    assert fb.get("finresearch/db/notify.ntfy/token") is None
