"""Find and move plaintext secrets left in the database (issue #245): `finresearch secrets check | migrate`.

Before #245 broker credentials, access tokens, the opt-in CAS password and notification tokens were stored as plain
text in `broker_connection.config` / `.token` and `notification_setting.value`. `migrate` moves each one into the
secret store (the Keychain) and leaves a reference; it is a dry run unless `apply=True`, idempotent (references are
skipped) and reports counts and field names only, never a value. `check` lists what is still plaintext, so a backup
can refuse to run while any is left.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from finresearch import secrets as secret_store
from finresearch.db.models import BrokerConnection, NotificationSetting

log = logging.getLogger(__name__)

MASK = "••••"


@dataclass(frozen=True)
class Plain:
    """One plaintext secret: where it is, never what it is."""

    table: str
    key: str
    field: str


@dataclass
class Report:
    found: list[Plain] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)  # broker rows whose connector is unknown (cannot classify)
    moved: int = 0

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for p in self.found:
            out[p.table] = out.get(p.table, 0) + 1
        return out


def _broker_secret_fields(key: str) -> set[str] | None:
    from finresearch.portfolio.connectors.store import secret_names

    try:
        return secret_names(key)
    except Exception:  # a connector this version no longer knows: its fields cannot be classified
        return None


def _plain(v: object) -> bool:
    return isinstance(v, str) and v != "" and not secret_store.is_ref(v)


def scan(s: Session) -> Report:
    """Every secret field still held as plain text."""
    from finresearch.monitor.notify import SECRETS

    rep = Report()
    for row in s.scalars(select(BrokerConnection).order_by(BrokerConnection.key)):
        names = _broker_secret_fields(row.key)
        if names is None:
            rep.unknown.append(row.key)
            continue
        cfg = row.config or {}
        rep.found += [Plain("broker_connection", row.key, n) for n in sorted(names) if _plain(cfg.get(n))]
        if _plain(row.token):
            rep.found.append(Plain("broker_connection", row.key, "token"))
    for row in s.scalars(select(NotificationSetting).order_by(NotificationSetting.key)):
        value = row.value or {}
        rep.found += [Plain("notification_setting", row.key, f) for f in SECRETS.get(row.key, ()) if _plain(value.get(f))]
    return rep


def migrate(s: Session, *, apply: bool = False) -> Report:
    """Move every plaintext secret into the secret store and keep a reference. Dry run unless `apply`."""
    rep = scan(s)
    if not apply or not rep.found:
        return rep
    for p in rep.found:
        if p.table == "broker_connection":
            row = s.get(BrokerConnection, p.key)
            if p.field == "token":
                row.token = secret_store.put_text(f"broker.{p.key}", "token", row.token)
            else:
                cfg = dict(row.config or {})
                cfg[p.field] = secret_store.put(f"broker.{p.key}", p.field, cfg[p.field], MASK)
                row.config = cfg
        else:
            row = s.get(NotificationSetting, p.key)
            value = dict(row.value or {})
            value[p.field] = secret_store.put(f"notify.{p.key}", p.field, value[p.field], MASK)
            row.value = value
        rep.moved += 1
    s.flush()
    return rep


def vacuum(engine) -> None:
    """Rewrite the two tables so the old plaintext row versions are gone from the data files too (VACUUM FULL cannot
    run inside a transaction)."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("VACUUM FULL broker_connection, notification_setting"))
