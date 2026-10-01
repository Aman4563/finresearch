"""Deliver alerts to the phone (and the Mac): ntfy, a Telegram bot, macOS notifications (roadmap item 10).

Research and sources: the PR's alerts-research notes. In short:
- ntfy: one HTTP POST (JSON publishing to the server root, so ₹ and other non-Latin text are safe). On ntfy.sh "the
  topic is essentially a password" (https://docs.ntfy.sh/publish/): the app suggests a long random topic. Priority
  1-5, a click URL. Default server limits: a burst of 60 requests, then one per 5 s (https://docs.ntfy.sh/config/).
  A self-hosted server with access control takes a bearer token (`tk_...`).
- Telegram: BotFather token + your chat id; `sendMessage` with `disable_notification` for low priorities. About one
  message a second to one chat; a 429 carries `retry_after` (https://core.telegram.org/bots/faq).
- macOS: terminal-notifier when installed (a click opens the app page), else `osascript` `display notification`
  with the text passed as arguments (never spliced into the script). Only shows on this Mac.

Secrets (ntfy topic and token, Telegram bot token) are stored in the local database table `notification_setting`,
never in the repo or the investor profile (the profile reaches the model); the API returns them masked, and every
error written to the delivery log is redacted.

Delivery: `queue` adds one `alert_delivery` row per chosen, configured channel; `deliver_due` (every monitor tick)
sends due rows, at most one message a second per channel. A failure is retried after 30 s, 2 min and 8 min (or the
server's retry-after), then marked failed with the error. During the profile's quiet hours a delivery is held until
they end, except `urgent` ones; the in-app alert is recorded at once either way.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import secrets
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from finresearch.db.models import Alert, AlertDelivery, NotificationSetting

log = logging.getLogger(__name__)

CHANNELS = ("ntfy", "telegram", "macos")
NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
BACKOFF = (timedelta(seconds=30), timedelta(minutes=2), timedelta(minutes=8))
MAX_ATTEMPTS = len(BACKOFF) + 1
SEND_TIMEOUT_S = 15.0
MASK = "••••"
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CHAT = re.compile(r"^(-?\d{1,20}|@[A-Za-z0-9_]{4,64})$")


# --------------------------------------------------------------------------- settings
class NtfySettings(BaseModel):
    enabled: bool = False
    server: str = "https://ntfy.sh"
    topic: str = ""  # secret: anyone who knows it can read and post (on a server without access control)
    token: str = ""  # secret: bearer token for a self-hosted server with access control

    @field_validator("server")
    @classmethod
    def _server(cls, v: str) -> str:
        v = (v or "https://ntfy.sh").strip().rstrip("/")
        if not re.match(r"^https?://[^\s/]+", v):
            raise ValueError("the ntfy server must be an http(s) URL")
        return v

    @field_validator("topic")
    @classmethod
    def _topic(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not _TOPIC.match(v):
            raise ValueError("an ntfy topic is 1-64 letters, digits, '-' or '_'")
        return v


class TelegramSettings(BaseModel):
    enabled: bool = False
    bot_token: str = ""  # secret: controls the bot
    chat_id: str = ""

    @field_validator("bot_token")
    @classmethod
    def _token(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not re.match(r"^\d{5,15}:[A-Za-z0-9_-]{20,80}$", v):
            raise ValueError("a Telegram bot token looks like 123456789:AA... (from @BotFather)")
        return v

    @field_validator("chat_id")
    @classmethod
    def _chat(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not _CHAT.match(v):
            raise ValueError("a Telegram chat id is a number (from getUpdates) or @channelname")
        return v


class MacosSettings(BaseModel):
    enabled: bool = False
    sound: bool = True  # a sound for high and urgent alerts


class GeneralSettings(BaseModel):
    # where the app's pages open from the phone (a LAN or Tailscale address works on the same network)
    app_url: str = "http://127.0.0.1:3100"
    # also send the monitor's own alerts (listing, allotment, rule changes, big moves) at this level or above
    forward_level: Literal["off", "action", "warn"] = "off"
    forward_channels: list[Literal["ntfy", "telegram", "macos"]] = Field(default_factory=list)

    @field_validator("app_url")
    @classmethod
    def _url(cls, v: str) -> str:
        v = (v or "").strip().rstrip("/")
        if not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("the app URL must be an http(s) URL")
        return v


MODELS: dict[str, type[BaseModel]] = {"ntfy": NtfySettings, "telegram": TelegramSettings, "macos": MacosSettings,
                                      "general": GeneralSettings}  # fmt: skip
SECRETS = {"ntfy": ("topic", "token"), "telegram": ("bot_token",)}


def load(session: Session, key: str) -> Any:
    row = session.get(NotificationSetting, key)
    return MODELS[key].model_validate(row.value if row else {})


def _store(session: Session, key: str, model: BaseModel, extra: dict[str, Any] | None = None) -> None:
    from datetime import UTC

    row = session.get(NotificationSetting, key)
    value = {**model.model_dump(mode="json"), **(extra or {})}
    if row is None:
        session.add(NotificationSetting(key=key, value=value))
    else:
        row.value, row.updated_at = value, datetime.now(UTC)


def mask(v: str) -> str:
    if not v:
        return ""
    return MASK + (v[-4:] if len(v) >= 12 else "")


def configured(channel: str, cfg: Any) -> str | None:
    """None when the channel can send, else why not."""
    if not cfg.enabled:
        return "turned off"
    if channel == "ntfy" and not cfg.topic:
        return "no topic set"
    if channel == "telegram" and not (cfg.bot_token and cfg.chat_id):
        return "bot token or chat id missing"
    if channel == "macos" and sys.platform != "darwin":
        return "not running on macOS"
    return None


def public(session: Session) -> dict[str, Any]:
    """All settings with secrets masked (what the API returns)."""
    out: dict[str, Any] = {}
    for key in MODELS:
        cfg = load(session, key)
        d = cfg.model_dump(mode="json")
        for f in SECRETS.get(key, ()):
            d[f"{f}_set"] = bool(d[f])
            d[f] = mask(d[f])
        if key in CHANNELS:
            d["status"] = configured(key, cfg) or "ready"
        out[key] = d
    out["channels"] = list(CHANNELS)
    return out


def update(session: Session, body: dict[str, Any]) -> dict[str, Any]:
    """Apply a settings change. A secret field left out, empty-masked or sent back masked keeps the stored value;
    send "" explicitly with `clear_<field>: true` to remove it."""
    for key, patch in body.items():
        if key not in MODELS or not isinstance(patch, dict):
            continue
        cur = load(session, key).model_dump(mode="json")
        extra: dict[str, Any] = {}
        for f, v in patch.items():
            if f.startswith("clear_") and v is True and f[6:] in SECRETS.get(key, ()):
                cur[f[6:]] = ""
                continue
            if f not in cur or f.endswith("_set") or f == "status":
                continue
            if f in SECRETS.get(key, ()) and (v is None or str(v).startswith(MASK)):
                continue  # the masked value came back: keep the secret
            cur[f] = v
        new = MODELS[key].model_validate(cur)
        if key == "general":
            old = load(session, "general")
            prev = session.get(NotificationSetting, "general")
            cursor = (prev.value or {}).get("forward_cursor") if prev else None
            if (old.forward_level == "off" and new.forward_level != "off") or cursor is None:
                cursor = (
                    session.scalar(select(func.max(Alert.id))) or 0
                )  # forward from now on, not the history
            extra["forward_cursor"] = cursor
        _store(session, key, new, extra)
    return public(session)


def random_topic() -> str:
    """A hard-to-guess ntfy topic (about 128 bits)."""
    return "finresearch-" + secrets.token_urlsafe(16).replace("_", "x").replace("-", "y")


# --------------------------------------------------------------------------- queueing
def queue(session: Session, alert: Alert, channels: list[str], priority: str, *, title: str, message: str,
          path: str | None, now: datetime) -> list[str]:  # fmt: skip
    """One pending delivery per chosen channel that is configured; returns the channels queued."""
    from sqlalchemy.dialects.postgresql import insert

    if not channels:
        return []
    general = load(session, "general")
    click = f"{general.app_url}{path}" if path else general.app_url
    queued = []
    for ch in dict.fromkeys(channels):
        if ch not in CHANNELS or configured(ch, load(session, ch)):
            continue
        session.execute(insert(AlertDelivery).values(alert_id=alert.id, channel=ch, priority=priority,
                                                     title=title[:200], message=message, click=click[:500],
                                                     status="pending", attempts=0, next_try_at=now, test=False)
                        .on_conflict_do_nothing(index_elements=["alert_id", "channel"]))  # fmt: skip
        queued.append(ch)
    return queued


def forward_monitor_alerts(session: Session, now: datetime) -> int:
    """Queue the monitor's own alerts (not rule alerts) at the chosen level for the forward channels."""
    row = session.get(NotificationSetting, "general")
    general = load(session, "general")
    if general.forward_level == "off" or not general.forward_channels or row is None:
        return 0
    cursor = int((row.value or {}).get("forward_cursor") or 0)
    levels = ("action",) if general.forward_level == "action" else ("action", "warn")
    rows = session.scalars(select(Alert).where(Alert.id > cursor).order_by(Alert.id).limit(100)).all()
    n = 0
    for a in rows:
        if a.kind != "rule_alert" and a.level in levels:
            path = (a.data or {}).get("path") or (f"/monitor/{a.watch_id}" if a.watch_id else "/monitor")
            n += bool(queue(session, a, general.forward_channels, "high" if a.level == "action" else "default",
                            title=f"FinResearch · {a.kind.replace('_', ' ')}", message=a.message, path=path, now=now))  # fmt: skip
    if rows:
        row.value = {**(row.value or {}), "forward_cursor": rows[-1].id}
    return n


# --------------------------------------------------------------------------- sending
class SendError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after, self.permanent = retry_after, permanent


@dataclass
class Message:
    title: str
    body: str
    priority: str = "default"
    click: str | None = None


# test seams: an httpx transport (MockTransport) and a subprocess runner for macOS
TRANSPORT: httpx.AsyncBaseTransport | None = None
RUN: Callable[[list[str]], Awaitable[tuple[int, str]]] | None = None


def redact(text: str, *secret_values: str) -> str:
    for s in secret_values:
        if s:
            text = text.replace(s, "[redacted]")
    return re.sub(r"bot\d{5,15}:[A-Za-z0-9_-]{20,80}", "bot[redacted]", text)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=SEND_TIMEOUT_S, transport=TRANSPORT, follow_redirects=False)


def _check(r: httpx.Response, what: str) -> None:
    if r.status_code < 300:
        return
    retry = None
    try:
        body = r.json()
        retry = (body.get("parameters") or {}).get("retry_after")
        desc = body.get("description") or body.get("error") or r.text
    except Exception:
        desc = r.text
    if retry is None and r.headers.get("retry-after", "").isdigit():
        retry = float(r.headers["retry-after"])
    permanent = r.status_code in (400, 401, 403, 404) and r.status_code != 429
    raise SendError(f"{what} answered HTTP {r.status_code}: {str(desc)[:200]}", retry, permanent)


async def send_ntfy(cfg: NtfySettings, m: Message) -> None:
    payload: dict[str, Any] = {"topic": cfg.topic, "title": m.title, "message": m.body,
                               "priority": NTFY_PRIORITY.get(m.priority, 3), "tags": ["chart_with_upwards_trend"]}  # fmt: skip
    if m.click:
        payload["click"] = m.click
        payload["actions"] = [{"action": "view", "label": "Open in FinResearch", "url": m.click}]
    headers = {"Authorization": f"Bearer {cfg.token}"} if cfg.token else {}
    async with _client() as c:
        r = await c.post(cfg.server + "/", json=payload, headers=headers)
    _check(r, "ntfy")


async def send_telegram(cfg: TelegramSettings, m: Message) -> None:
    text = f"<b>{html.escape(m.title)}</b>\n{html.escape(m.body)}"
    if m.click:
        text += f"\n{html.escape(m.click)}"
    payload = {"chat_id": cfg.chat_id, "text": text[:4000], "parse_mode": "HTML",
               "disable_notification": m.priority in ("min", "low"),
               "link_preview_options": {"is_disabled": True}}  # fmt: skip
    async with _client() as c:
        r = await c.post(f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage", json=payload)
    _check(r, "Telegram")


async def _run(argv: list[str]) -> tuple[int, str]:
    if RUN is not None:
        return await RUN(argv)
    p = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=10)
    except TimeoutError:
        p.kill()
        return 1, "timed out"
    return p.returncode or 0, (err or out or b"").decode(errors="replace")[:300]


APPLESCRIPT = ("on run argv\n display notification (item 2 of argv) with title (item 1 of argv)"
               " sound name (item 3 of argv)\nend run")  # fmt: skip
APPLESCRIPT_SILENT = (
    "on run argv\n display notification (item 2 of argv) with title (item 1 of argv)\nend run"
)


async def send_macos(cfg: MacosSettings, m: Message) -> None:
    loud = cfg.sound and m.priority in ("high", "urgent")
    tn = shutil.which("terminal-notifier") if RUN is None else None
    if tn:
        argv = [tn, "-title", m.title, "-message", m.body, "-group", "finresearch"]
        if m.click:
            argv += ["-open", m.click]
        if loud:
            argv += ["-sound", "Glass"]
    else:  # the text travels as arguments, never inside the script
        argv = ["osascript", "-e", APPLESCRIPT if loud else APPLESCRIPT_SILENT, m.title, m.body[:250],
                *(["Glass"] if loud else [])]  # fmt: skip
    code, out = await _run(argv)
    if code != 0:
        raise SendError(f"macOS notification failed ({code}): {out}")


async def send(session: Session, channel: str, m: Message) -> None:
    cfg = load(session, channel)
    why = configured(channel, cfg)
    if why:
        raise SendError(f"{channel} is not ready: {why}", permanent=True)
    secret_values = [getattr(cfg, f) for f in SECRETS.get(channel, ())]
    try:
        if channel == "ntfy":
            await send_ntfy(cfg, m)
        elif channel == "telegram":
            await send_telegram(cfg, m)
        else:
            await send_macos(cfg, m)
    except SendError as e:
        raise SendError(redact(str(e), *secret_values), e.retry_after, e.permanent) from None
    except Exception as e:  # network errors: the URL may carry the Telegram token
        raise SendError(redact(f"{type(e).__name__}: {e}", *secret_values)) from None


# --------------------------------------------------------------------------- quiet hours and the delivery loop
def quiet_until(now: datetime, quiet_start: str | None, quiet_end: str | None) -> datetime | None:
    """When the current quiet period ends (IST), or None when `now` is outside quiet hours."""
    from finresearch.fincalc.dates import ist_datetime, to_ist

    if not quiet_start or not quiet_end:
        return None
    ist = to_ist(now)
    hm = ist.hour * 60 + ist.minute
    s = int(quiet_start[:2]) * 60 + int(quiet_start[3:])
    e = int(quiet_end[:2]) * 60 + int(quiet_end[3:])
    inside = s <= hm < e if s < e else (hm >= s or hm < e)
    if not inside:
        return None
    end_day = ist.date() + timedelta(days=1) if (s > e and hm >= s) else ist.date()
    return ist_datetime(end_day, e // 60, e % 60)


def _quiet_window(session: Session) -> tuple[str | None, str | None]:
    from finresearch.suggest.advisor import load_profile

    try:
        ww = load_profile(session).preferences.watch
        return ww.quiet_start, ww.quiet_end
    except Exception:
        return None, None


async def attempt(session: Session, d: AlertDelivery, now: datetime) -> str:
    """Send one delivery now; update its row. Returns its new status."""
    d.attempts += 1
    try:
        await send(session, d.channel, Message(d.title, d.message, d.priority, d.click))
    except SendError as e:
        d.last_error = str(e)[:500]
        if e.permanent or d.attempts >= MAX_ATTEMPTS:
            d.status = "failed"
        else:
            wait = BACKOFF[min(d.attempts - 1, len(BACKOFF) - 1)]
            if e.retry_after:
                wait = max(wait, timedelta(seconds=float(e.retry_after)))
            d.next_try_at = now + wait
        return d.status
    d.status, d.sent_at, d.last_error, d.held = "sent", now, None, None
    return "sent"


async def deliver_due(now: datetime, *, spacing_s: float = 1.0, limit: int = 20) -> dict[str, int]:
    """Send every due delivery (the monitor calls this each tick). Quiet hours hold non-urgent ones."""
    from finresearch.db import session_scope

    out = {"sent": 0, "failed": 0, "retry": 0, "held": 0, "forwarded": 0}
    with session_scope() as s:
        out["forwarded"] = forward_monitor_alerts(s, now)
    with session_scope() as s:
        rows = s.scalars(select(AlertDelivery).where(AlertDelivery.status == "pending", AlertDelivery.next_try_at <= now)
                         .order_by(AlertDelivery.id).limit(limit).with_for_update(skip_locked=True)).all()  # fmt: skip
        if not rows:
            return out
        qs, qe = _quiet_window(s)
        until = quiet_until(now, qs, qe)
        last_sent: dict[str, bool] = {}
        for d in rows:
            if until is not None and d.priority != "urgent" and not d.test:
                d.next_try_at, d.held = until, f"quiet hours until {qe}"
                out["held"] += 1
                continue
            if last_sent.get(d.channel) and spacing_s:
                await asyncio.sleep(spacing_s)  # at most one message a second per channel
            st = await attempt(s, d, now)
            last_sent[d.channel] = True
            out["sent" if st == "sent" else "failed" if st == "failed" else "retry"] += 1
    return out


async def send_test(channel: str, now: datetime) -> dict[str, Any]:
    """Send a test notification at once (quiet hours ignored) and log it like any delivery."""
    from finresearch.db import session_scope

    if channel not in CHANNELS:
        raise ValueError(f"unknown channel {channel!r}")
    with session_scope() as s:
        general = load(s, "general")
        d = AlertDelivery(alert_id=None, channel=channel, priority="default", title="FinResearch test",
                          message=f"Test notification from FinResearch ({now:%d-%b %H:%M} UTC). If you see this, "
                          f"{channel} alerts work.", click=general.app_url + "/rules", status="pending",
                          attempts=0, next_try_at=now, test=True)  # fmt: skip
        s.add(d)
        s.flush()
        st = await attempt(s, d, now)
        if st == "pending":  # a test is not retried: show the error now
            d.status = "failed"
        return delivery_json(d)


def delivery_json(d: AlertDelivery) -> dict[str, Any]:
    def iso(x: datetime | None) -> str | None:
        return x.isoformat() if x else None

    return {"id": d.id, "alert_id": d.alert_id, "channel": d.channel, "priority": d.priority, "title": d.title,
            "message": d.message, "status": d.status, "attempts": d.attempts, "next_try_at": iso(d.next_try_at),
            "last_error": d.last_error, "held": d.held, "test": d.test, "created_at": iso(d.created_at),
            "sent_at": iso(d.sent_at)}  # fmt: skip
