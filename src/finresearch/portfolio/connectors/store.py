"""Connection settings in the local database: masked for the API, secrets kept when the masked value comes back.

Secrets (fields declared `secret=True`, and the access token) live in the macOS Keychain (finresearch.secrets): the
row's `config` keeps `{"secret_ref": ..., "hint": "••••"}` for each and `token` keeps `secret_ref:<ref>`. Read them
only through `config_of` / `token_of` / `build`.

Mirrors monitor.notify's secret handling (same mask, same "send the masked value back to keep it" rule). A secret is
never returned by the API, never logged, never put in the investor profile (which reaches the model) and never
committed (the database and data/ are local). "Disconnect" deletes the row: credentials and token are gone, the
imported transactions stay (delete their imports on the Portfolio page if you want them gone too).
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from finresearch import secrets as secret_store
from finresearch.db.models import BrokerConnection
from finresearch.monitor.notify import MASK
from finresearch.portfolio.connectors import CONNECTORS, INBOX_KEY, connector_class
from finresearch.portfolio.connectors.base import BrokerConnector, FieldSpec, TokenGrant

INBOX_FIELDS = (
    FieldSpec("password", "CAS PDF password (optional)", secret=True, required=False,
              help="Saved only if you type it here. CAMS/KFintech/NSDL/CDSL statements are usually locked with your "
                   "PAN in capitals. Without it, PDFs wait in the inbox until you import them by hand."),
)  # fmt: skip


def fields_of(key: str) -> tuple[FieldSpec, ...]:
    return INBOX_FIELDS if key == INBOX_KEY else connector_class(key).fields


def get_row(s: Session, key: str) -> BrokerConnection | None:
    return s.get(BrokerConnection, key)


def secret_names(key: str) -> set[str]:
    return {f.name for f in fields_of(key) if f.secret}


def config_of(row: BrokerConnection | None) -> dict[str, Any]:
    """The row's settings with its secrets read from the secret store."""
    if row is None:
        return {}
    return _resolved(row.key, row.config)


def _resolved(key: str, config: dict[str, Any] | None) -> dict[str, Any]:
    cfg = dict(config or {})
    for name in secret_names(key) & set(cfg):
        cfg[name] = secret_store.resolve(cfg[name])
    return cfg


def token_of(row: BrokerConnection) -> str | None:
    return secret_store.resolve(row.token) or None if row.token else None


def clear_token(row: BrokerConnection) -> None:
    """Forget the access token: its Keychain item too."""
    secret_store.drop(row.token)
    row.token, row.token_expires_at = None, None
    row.state = {k: v for k, v in (row.state or {}).items() if k != "token_rev"}


def build(row: BrokerConnection) -> BrokerConnector:
    return connector_class(row.key)(config_of(row), token_of(row))


def build_from(key: str, config: dict[str, Any] | None, token: str | None) -> BrokerConnector:
    """`build` from a row's plain column values (no session): the secret-store reads (a Keychain subprocess each)
    can then run in a worker thread, off the event loop (sync.sync_now)."""
    tok = (secret_store.resolve(token) or None) if token else None
    return connector_class(key)(_resolved(key, config), tok)


# a token about to expire is treated as expired: a sync that starts a minute before the broker ends the session
# (Groww, Zerodha 06:00 IST) would otherwise fail half-way and lose the day's scheduled attempt (#266)
TOKEN_MARGIN = timedelta(minutes=5)


def token_valid(row: BrokerConnection, now: datetime) -> bool:
    return bool(row.token) and (row.token_expires_at is None or row.token_expires_at - TOKEN_MARGIN > now)


def missing_fields(key: str, config: dict[str, Any]) -> list[str]:
    return [f.label for f in fields_of(key) if f.required and not str(config.get(f.name) or "").strip()]


def status_of(row: BrokerConnection | None, key: str, now: datetime) -> tuple[str, str]:
    """(status, what to do next)."""
    if row is None:
        return "not_connected", "Enter your API details to connect."
    if key == INBOX_KEY:
        return ("connected" if row.enabled else "off"), ("Drop statements into the inbox folder." if row.enabled
                                                          else "Turn on to import files from the inbox folder.")  # fmt: skip
    if not row.enabled:
        return "off", "Turned off: nothing is synced."
    if miss := missing_fields(key, row.config or {}):
        return "incomplete", "Missing: " + ", ".join(miss)
    cls = connector_class(key)
    if token_valid(row, now):
        if row.status == "error":
            return "error", row.last_error or "the last sync failed"
        return "connected", "Syncs after the close each trading day, or Sync now."
    if row.status in ("reconnect", "error") and row.last_error:
        return "error", row.last_error
    if cls.auth_kind in ("totp", "password_totp", "key_secret"):
        return "ready", "Logs in by itself (TOTP) at the next sync."
    ended = bool(row.token or row.last_sync_at or row.status == "reconnect")
    return "reconnect", ("Log in to the broker again (Reconnect): its daily session ended." if ended
                         else "Log in to the broker once (Connect) to start syncing.")  # fmt: skip


def public_one(s: Session, key: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    row = get_row(s, key)
    cfg = (
        dict(row.config or {}) if row else {}
    )  # secrets stay references here: only whether one is set is shown
    fields = fields_of(key)
    shown: dict[str, Any] = {}
    for f in fields:
        v = "set" if secret_store.is_ref(cfg.get(f.name)) else str(cfg.get(f.name) or "")
        shown[f.name] = (
            (MASK if v else "") if f.secret else v
        )  # no characters of a secret, not even the last four
        if f.secret:
            shown[f"{f.name}_set"] = bool(v)
    st, nxt = status_of(row, key, now)
    base = ({"key": INBOX_KEY, "label": "Statement inbox", "auth_kind": "none", "capabilities": ["cas", "tradebook",
             "holdings_statement"], "fields": [{"name": f.name, "label": f.label, "secret": f.secret,
                                                 "required": f.required, "help": f.help} for f in fields]}
            if key == INBOX_KEY else connector_class(key)({}).describe())  # fmt: skip
    state = dict(row.state or {}) if row else {}
    return {**base, "configured": row is not None, "enabled": bool(row.enabled) if row else False,
            "auto_sync": bool(row.auto_sync) if row else True, "config": shown, "status": st, "next_step": nxt,
            "token_set": bool(row and row.token), "connected_as": state.get("connected_as"),
            "token_expires_at": _iso(row.token_expires_at) if row else None,
            "last_sync_at": _iso(row.last_sync_at) if row else None, "last_error": row.last_error if row else None,
            "positions": state.get("positions") or [], "funds": state.get("funds") or None,
            "last_summary": state.get("last_summary") or None,
            # Groww (#282, #284): what the syncs learned about T1 shares, and the last in-session order-list read
            "t1_semantics": state.get("t1_semantics") or None, "orders_poll": state.get("orders_poll") or None}  # fmt: skip


def public(s: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    return [public_one(s, k, now) for k in [*CONNECTORS, INBOX_KEY]]


def update(s: Session, key: str, body: dict[str, Any]) -> BrokerConnection:
    """Apply settings. A secret left out, sent back masked or null keeps the stored value; `clear_<field>: true`
    removes it. Changing a credential drops the current token (it belonged to the old credentials)."""
    fields = {f.name: f for f in fields_of(key)}
    row = get_row(s, key)
    if row is None:
        row = BrokerConnection(
            key=key, enabled=True, auto_sync=True, config={}, state={}, status="not_connected"
        )
        s.add(row)
    stored = dict(row.config or {})
    before = config_of(row)  # secrets resolved, so "changed?" compares values, not references
    cfg = dict(before)
    changed_credential = False
    for name, v in (body.get("config") or {}).items():
        if name.startswith("clear_") and v is True and name[6:] in fields and fields[name[6:]].secret:
            cfg.pop(name[6:], None)
            changed_credential = True
            continue
        f = fields.get(name)
        if f is None:
            continue
        if f.secret and (v is None or str(v).strip() == "" or str(v).startswith(MASK)):
            continue  # left empty or sent back masked: keep the saved secret (clear_<field> removes it)
        v = str(v or "").strip()
        if len(v) > 2000:
            raise ValueError(f"{f.label}: too long")
        if cfg.get(name, "") != v:
            changed_credential = True
        if v:
            cfg[name] = v
        else:
            cfg.pop(name, None)
    if key != INBOX_KEY:
        _validate(key, cfg)
    elif cfg.get("password") != before.get("password"):
        # the inbox remembers which files failed with which saved password by a random revision id, never by a hash
        # of the password: a CAS password is usually PAN-derived (low entropy), so even a truncated hash stored in
        # the database could be brute-forced back (CodeQL py/weak-sensitive-data-hashing)
        if cfg.get("password"):
            cfg["password_rev"] = secrets.token_hex(8)
        else:
            cfg.pop("password_rev", None)
    for name in secret_names(key):  # the row keeps references; the values go to the Keychain
        if name in cfg or name in stored:
            ref = secret_store.keep(f"broker.{key}", name, str(cfg.get(name) or ""), stored.get(name))
            if ref:
                cfg[name] = ref
            else:
                cfg.pop(name, None)
    row.config = cfg
    for flag in ("enabled", "auto_sync"):
        if isinstance(body.get(flag), bool):
            setattr(row, flag, body[flag])
    if changed_credential and key != INBOX_KEY:
        clear_token(row)
        row.last_error = None
        row.status = "not_connected"
    row.updated_at = datetime.now(UTC)
    s.flush()
    return row


def _validate(key: str, cfg: dict[str, Any]) -> None:
    from finresearch.portfolio.connectors.totp import valid_seed

    for f in fields_of(key):
        v = cfg.get(f.name)
        if v and f.name.endswith("totp_secret") and not valid_seed(v):
            raise ValueError(f"{f.label}: the TOTP secret is the base32 key shown when you enabled TOTP "
                             "(letters A-Z and digits 2-7), not the 6-digit code")  # fmt: skip


def set_token(s: Session, key: str, grant: TokenGrant) -> str:
    """Store a fresh access token; returns its revision (`state.token_rev`)."""
    row = get_row(s, key)
    if row is None:
        raise LookupError(key)
    row.token = secret_store.keep_text(f"broker.{key}", "token", grant.token, row.token)
    row.token_expires_at = grant.expires_at
    row.status, row.last_error = "connected", None
    # which login this token came from: the Keychain reference stays the same across logins, so a sync that finds
    # its token refused clears it only while it is still the one it used (sync._finish), never a newer one
    row.state = {**(row.state or {}), **{k: v for k, v in (grant.extra or {}).items() if k in ("connected_as",)},
                 "token_rev": (rev := secrets.token_hex(8))}  # fmt: skip
    row.updated_at = datetime.now(UTC)
    return rev


def disconnect(s: Session, key: str) -> bool:
    row = get_row(s, key)
    if row is None:
        return False
    for name in secret_names(key):
        secret_store.drop((row.config or {}).get(name))
    secret_store.drop(row.token)
    s.delete(row)
    return True


def _iso(d: datetime | None) -> str | None:
    return d.isoformat() if d else None


__all__ = ["CONNECTORS", "INBOX_KEY", "build", "disconnect", "public", "public_one", "set_token", "update"]
