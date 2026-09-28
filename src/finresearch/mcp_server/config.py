"""Claude Code --mcp-config for the FinResearch MCP server (stdio, launched via uv from this repo)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from finresearch.config import REPO_ROOT, get_settings

SERVER_NAME = "finresearch"
# permission rule that allows every tool of our server (Claude Code names them mcp__<server>__<tool>)
ALLOW_ALL = f"mcp__{SERVER_NAME}"


def write_mcp_config(path: Path | None = None) -> Path:
    s = get_settings()
    path = path or s.state_dir / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv") or "uv"
    env = {"FINRESEARCH_DATABASE_URL": s.database_url, "FINRESEARCH_DOCS_DIR": str(s.docs_dir)}
    cfg = {
        "mcpServers": {
            SERVER_NAME: {
                "type": "stdio",
                "command": uv,
                "args": [
                    "run",
                    "--quiet",
                    "--directory",
                    str(REPO_ROOT),
                    "python",
                    "-m",
                    "finresearch.mcp_server",
                ],
                "env": env,
            }
        }
    }
    path.write_text(json.dumps(cfg, indent=2))
    return path
