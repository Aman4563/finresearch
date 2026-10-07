"""#246b: agents can never fetch this machine or the local network (WebFetch rules + hook; MCP URL tools)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from finresearch import fetch_guard
from finresearch.fetch_guard import DENY_RULES, blocked_reason

REPO = Path(__file__).resolve().parents[1]
DNS = {"localtest.me": ["127.0.0.1"], "rebind.example.com": ["93.184.216.34", "192.168.1.10"],
       "www.sebi.gov.in": ["1.2.3.4"], "v6.example.com": ["::ffff:127.0.0.1"]}  # fmt: skip


def fake_resolve(host: str) -> list[str] | None:
    return DNS.get(host)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8710/api/export/all.zip", "http://localhost:3100/", "http://[::1]:8710/api/portfolio",
    "http://10.1.2.3/", "http://172.16.0.5/", "http://192.168.1.1/admin", "http://169.254.169.254/latest/meta-data",
    "http://100.64.0.1/", "http://0.0.0.0:8710/", "http://[::ffff:127.0.0.1]/", "http://[fd00::1]/",
    "http://api.localhost/", "http://printer.local/", "http://intranet/", "http://2130706433/",
    "http://localtest.me:8710/api/export/all.zip", "https://rebind.example.com/", "http://v6.example.com/",
    "https://does-not-resolve.example/", "file:///etc/passwd", "ftp://www.sebi.gov.in/x", "not a url",
])  # fmt: skip
def test_local_and_private_targets_are_blocked(url):
    assert blocked_reason(url, resolve=fake_resolve), url


@pytest.mark.parametrize(
    "url", ["https://www.sebi.gov.in/filings/x.html", "https://1.1.1.1/", "https://8.8.8.8/x"]
)
def test_public_targets_pass(url):
    assert blocked_reason(url, resolve=fake_resolve) is None


def _hook(event: dict, command: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command or fetch_guard.hook_command(), shell=True, input=json.dumps(event),
                          capture_output=True, text=True, timeout=60)  # fmt: skip


def test_the_hook_command_blocks_with_exit_2_and_lets_public_ips_through():
    # run exactly the command written into the sandbox settings (the interpreter path has spaces in this repo)
    command = fetch_guard.sandbox_settings()["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    bad = _hook({"tool_name": "WebFetch", "tool_input": {"url": "http://127.0.0.1:8710/api/export/all.zip",
                                                         "prompt": "x"}}, command)  # fmt: skip
    assert bad.returncode == 2 and "127.0.0.1" in bad.stderr
    ok = _hook({"tool_name": "WebFetch", "tool_input": {"url": "https://1.1.1.1/", "prompt": "x"}}, command)
    assert ok.returncode == 0, ok.stderr
    junk = subprocess.run(command, shell=True, input="not json", capture_output=True, text=True, timeout=60)
    assert junk.returncode == 2  # unreadable input is refused, not waved through


def test_sandbox_settings_and_command_line_deny_local_fetches(tmp_path):
    path = fetch_guard.write_sandbox_settings(tmp_path / "ws")
    data = json.loads(path.read_text())
    assert path == tmp_path / "ws" / ".claude" / "settings.json"
    assert data["permissions"]["deny"] == list(DENY_RULES)
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "WebFetch"


def test_repository_settings_deny_local_fetches():
    data = json.loads((REPO / ".claude" / "settings.json").read_text())
    assert set(DENY_RULES) <= set(data["permissions"]["deny"])


def test_bridge_passes_the_deny_rules_and_writes_the_sandbox_settings(tmp_path):
    from finresearch.bridge.claude_code import ClaudeCodeEngine
    from finresearch.bridge.types import AgentTask, ModelClass, Tier

    eng = ClaudeCodeEngine(Tier.CLAUDE_MAX, claude_bin="/nonexistent/claude", runs_dir=tmp_path / "runs",
                           models={m: "haiku" for m in ModelClass})  # fmt: skip
    web = eng.build_command(AgentTask(name="t", prompt="p", allowed_tools=["WebSearch", "WebFetch"]))
    assert web[web.index("--disallowedTools") + 1] == ",".join(DENY_RULES)
    plain = eng.build_command(AgentTask(name="t", prompt="p", allowed_tools=[]))
    assert "--disallowedTools" not in plain


async def test_mcp_url_tools_refuse_this_machine():
    from finresearch.mcp_server import server

    for tool, arg in ((server.nse_results_facts, "http://127.0.0.1:8710/api/export/all.zip"),
                      (server.sebi_resolve_pdf, "https://127.0.0.1/sebi"),
                      (server.sebi_resolve_pdf, "https://www.sebi.gov.in:8710/x"),
                      (server.nse_results_facts, "https://localhost/x.xml")):  # fmt: skip
        out = json.loads(await tool(arg))
        assert "error" in out, (tool, arg)
