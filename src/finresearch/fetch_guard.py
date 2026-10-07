"""Keep the research agents' WebFetch off this machine and the local network (issue #246b).

An agent reads untrusted web pages. Without this, a prompt-injected page could make it fetch
http://127.0.0.1:8710/api/export/all.zip (personal data) or a router admin page. Three layers, because the agents run
`claude -p` in a sandbox folder (data/runs/<run>/<role>/) whose own `.claude/settings.json` is the project setting
they load, not the repository's:

1. `--disallowedTools` deny rules on the command line (bridge.claude_code), for the names a rule can express;
2. the same rules plus a PreToolUse hook in `<sandbox>/.claude/settings.json` (`write_sandbox_settings`). The hook
   (`python -m finresearch.fetch_guard`, stdlib only) resolves the URL's host and blocks (exit 2) loopback, private,
   link-local, carrier-grade NAT, reserved and multicast addresses, `localhost` and `.local`/`.internal` names, a
   host that does not resolve, and non-http(s) schemes. Domain rules cannot express IP ranges; the hook can.
3. The MCP tools fetch through `adapters.http.PoliteClient`, which refuses such URLs on every request and redirect.

The repository's own `.claude/settings.json` carries the deny rules for interactive sessions.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import shlex
import socket
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# what permission rules can name (wildcards do not cross dots, so IP ranges are left to the hook)
DENY_RULES = (
    "WebFetch(domain:localhost)",
    "WebFetch(domain:*.localhost)",
    "WebFetch(domain:127.0.0.1)",
    "WebFetch(domain:0.0.0.0)",
    "WebFetch(domain:*.local)",
    "WebFetch(domain:*.internal)",
)
RESOLVE_TIMEOUT_S = 5.0


def _bad_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global or ip.is_multicast


def _resolve(host: str) -> list[str] | None:
    """The host's addresses, or None if it does not resolve within the timeout."""
    out: list[Any] = []

    def run() -> None:
        with contextlib.suppress(OSError):
            out.extend(socket.getaddrinfo(host, None))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(RESOLVE_TIMEOUT_S)
    return [str(a[4][0]).split("%")[0] for a in out] or None


def blocked_reason(url: str, *, resolve=_resolve) -> str | None:
    """Why `url` must not be fetched by an agent, or None when it may."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return "unparseable URL"
    if parts.scheme not in ("http", "https"):
        return f"scheme {parts.scheme or '(none)'!r} is not http(s)"
    if not host:
        return "no host"
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return f"{host} is on this machine or the local network"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return f"{ip} is a private, loopback or reserved address" if _bad_ip(ip) else None
    if "." not in host:
        return f"{host} is a local network name"
    addrs = resolve(host)
    if not addrs:
        return f"{host} does not resolve"
    for a in addrs:
        try:
            if _bad_ip(ipaddress.ip_address(a)):
                return f"{host} resolves to {a}, a private, loopback or reserved address"
        except ValueError:
            return f"{host} resolves to an unreadable address"
    return None


def hook_command() -> str:
    return f"{shlex.quote(sys.executable)} -m finresearch.fetch_guard"


def sandbox_settings() -> dict[str, Any]:
    return {
        "permissions": {"deny": list(DENY_RULES)},
        "hooks": {"PreToolUse": [{"matcher": "WebFetch", "hooks": [{"type": "command", "command": hook_command(),
                                                                     "timeout": 15}]}]},
    }  # fmt: skip


def write_sandbox_settings(workspace: Path) -> Path:
    """Write `<workspace>/.claude/settings.json` (the project settings a sandboxed `claude -p` loads)."""
    path = Path(workspace) / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sandbox_settings(), indent=2) + "\n")
    return path


def main() -> int:
    """PreToolUse hook: the tool call arrives as JSON on stdin; exit 2 (reason on stderr) blocks it."""
    try:
        event = json.load(sys.stdin)
        if event.get("tool_name") not in (None, "WebFetch"):
            return 0
        url = str((event.get("tool_input") or {}).get("url") or "")
        reason = blocked_reason(url)
    except Exception as e:  # unreadable input: refuse rather than let an unchecked fetch through
        reason = f"could not check the URL ({type(e).__name__})"
    if reason:
        print(f"FinResearch blocks this fetch: {reason}. Agents may only fetch public internet pages.",
              file=sys.stderr)  # fmt: skip
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
