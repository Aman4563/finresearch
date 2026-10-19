"""#331: the API spawns workers as `python -m finresearch.cli ...` (api/workers.py, stratlab/jobs.py). Under -m the
`if __name__ == "__main__": app()` guard runs app() at that point of the module, so every command registered below
it was missing ("No such command 'fno'") and Strategy Lab runs started from the app failed."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import finresearch.cli as cli

CLI_FILE = Path(cli.__file__)


def _run_module(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "finresearch.cli", *args], capture_output=True, text=True,
                          timeout=120)  # fmt: skip


def test_the_main_guard_is_the_last_statement_of_the_cli_module():
    body = ast.parse(CLI_FILE.read_text()).body
    last = body[-1]
    assert isinstance(last, ast.If) and "__main__" in ast.unparse(last.test)
    assert sum(isinstance(n, ast.If) and "__main__" in ast.unparse(n.test) for n in body) == 1


def test_the_module_form_sees_every_command_group():
    groups = {g.name or g.typer_instance.info.name for g in cli.app.registered_groups}
    assert {"fno", "portfolio", "inbox", "forecasts"} <= groups
    out = _run_module("--help")
    assert out.returncode == 0, out.stderr
    missing = sorted(g for g in groups if g and g not in out.stdout)
    assert not missing, f"python -m finresearch.cli lacks {missing}"


def test_the_strategy_lab_worker_command_runs_in_the_module_form():
    # stratlab.jobs.launch spawns exactly this argv (with the run id instead of --help)
    out = _run_module("fno", "lab", "run-job", "--help")
    assert out.returncode == 0 and "No such command" not in out.stdout + out.stderr, out.stdout + out.stderr
