"""Claude Code --mcp-config for the FinResearch MCP server (stdio, launched via uv from this repo)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from finresearch.config import REPO_ROOT, get_settings

SERVER_NAME = "finresearch"
# permission rule that allows every tool of our server (Claude Code names them mcp__<server>__<tool>)
ALLOW_ALL = f"mcp__{SERVER_NAME}"
# the research run one agent task's server serves (#393): option-fact tools take their scope from it, never from an
# argument the agent chooses
RUN_ENV = "FINRESEARCH_MCP_RUN_ID"


def write_mcp_config(path: Path | None = None, run_id: int | None = None) -> Path:
    """The server config; with `run_id`, a config of that run's own (state_dir/mcp/run<id>.json) whose server is
    bound to the run through RUN_ENV."""
    s = get_settings()
    path = path or (
        s.state_dir / "mcp" / f"run{run_id}.json" if run_id is not None else s.state_dir / "mcp.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv") or "uv"
    env = {"FINRESEARCH_DATABASE_URL": s.database_url, "FINRESEARCH_DOCS_DIR": str(s.docs_dir)}
    if run_id is not None:
        env[RUN_ENV] = str(int(run_id))
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
