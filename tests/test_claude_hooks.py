"""The Claude Code guard hooks (.claude/hooks/guard.py, #336): what they block, what they allow, and that research-agent
sandboxes never pay for them."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / ".claude" / "hooks" / "guard.py"
_spec = importlib.util.spec_from_file_location("claude_guard", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

T = "T=postgresql+psycopg://localhost/finresearch_me_test; "

BLOCKED = [
    "gh auth switch --user Aman4563",
    "gh auth login",
    "gh pr list",
    "cd x && gh issue view 3",
    "git config --global user.email a@b.c",
    "git config --system core.editor vim",
    "git push origin main",
    "git push -u origin HEAD:main",
    "git push origin +main",
    "git push --mirror",
    "git stash",
    "git stash pop",
    "git -C ../wt-x stash push -m wip",
    "git rebase origin/main",
    "git reset --soft origin/main",
    "ln -sf ../finresearch/web/node_modules web/node_modules",
    "dropdb finresearch",
    "dropdb finresearch_dev",
    "psql -d finresearch -c 'drop table agent_step'",
    "psql finresearch -c 'truncate research_run'",
    "pg_restore -d finresearch data/backups/x.dump",
    "rm -rf data",
    "rm -rf ./data/",
    "rm -r data/portfolio",
    'rm -rf "/Users/me/finresearch/data/backups"',
    "pkill -f 'finresearch serve'",
    "lsof -ti tcp:8710 | xargs kill",
    "kill $(pgrep -f claude_code_bridge)",
    "cat .env",
    "grep TOKEN ~/.pgpass",
    "security find-generic-password -s finresearch-postgres -w",
    "uv run pytest -q",
    "timeout 600 uv run pytest tests/test_x.py",
    "FINRESEARCH_TEST_DATABASE_URL=postgresql+psycopg://localhost/finresearch uv run pytest",
    T
    + "FINRESEARCH_TEST_DATABASE_URL=$T FINRESEARCH_DATABASE_URL=postgresql+psycopg://localhost/finresearch "
    "uv run pytest",
    "uv run alembic upgrade head",
    "FINRESEARCH_DATABASE_URL=postgresql+psycopg://localhost/finresearch uv run alembic downgrade -1",
    "data/devtools/merge_deploy.sh 340 wt-x feat/x | tail -5 && git gh release create v1",
]

ALLOWED = [
    "git gh pr list",
    "git gh pr merge 12 --merge",
    "gh auth status",
    "gh auth token --user Aman4563 >/dev/null",
    "git config --global --get user.name",
    "git config --local user.name Aman4563",
    "git -c user.name=Aman4563 -c user.email=x@users.noreply.github.com commit -m 'Fix gh auth login docs; main'",
    "git push -u origin chore/claude-guard-hooks",
    "git stash list",
    "git merge origin/main",
    "git rebase -i HEAD~2",
    "ln -sfn ../finresearch/web/node_modules web/node_modules",
    "cp -cR ../finresearch/web/node_modules web/node_modules",
    "dropdb finresearch_me_test",
    "psql -d finresearch_me_test -c 'drop schema public cascade'",
    "psql -d finresearch -Atc 'select id, status from research_run order by id desc limit 3'",
    "rm -rf data/runs/_smoke_isolation/probe",
    "rm -rf web/.next",
    "kill 12345",
    "cat .env.example",
    "uv run pytest --collect-only -q",
    T + "FINRESEARCH_TEST_DATABASE_URL=$T FINRESEARCH_DATABASE_URL=$T uv run pytest -q -rs",
    "M=postgresql+psycopg://localhost/finresearch_me_mig_test; export FINRESEARCH_DATABASE_URL=$M; "
    "uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade -1",
    "uv run alembic heads",
    "data/devtools/merge_deploy.sh 340 wt-x feat/x; echo exit=$?",
    "cat > notes.md <<'EOF'\ngit push origin main\ngh auth login\nEOF",
    "uv run ruff check . && uv run ruff format --check .",
]


@pytest.mark.parametrize("command", BLOCKED)
def test_blocks(command):
    assert guard.check_bash(command, str(REPO)) is not None


@pytest.mark.parametrize("command", ALLOWED)
def test_allows(command):
    assert guard.check_bash(command, str(REPO)) is None


def test_live_run_blocks_tree_changes_only_in_the_main_tree(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    main = str(tmp_path / "repo")
    assert guard.check_bash("git checkout -b x", main, live=True) is not None
    assert guard.check_bash("git pull --ff-only", main, live=True) is not None
    assert guard.check_bash("git checkout -b x", main, live=False) is None
    assert guard.check_bash("git status", main, live=True) is None


def _run(mode: str, event: dict, **env) -> subprocess.CompletedProcess:
    full = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}
    full.update({"FINRESEARCH_HOOK_LIVE_RUN": "0", "CLAUDE_PROJECT_DIR": str(REPO), **env})
    return subprocess.run([sys.executable, str(GUARD), mode], input=json.dumps(event), capture_output=True,
                          text=True, env=full, timeout=60)  # fmt: skip


def test_pre_bash_exit_codes():
    blocked = _run("pre-bash", {"cwd": str(REPO), "tool_input": {"command": "git push origin main"}})
    assert blocked.returncode == 2 and "never push to main" in blocked.stderr
    assert _run("pre-bash", {"cwd": str(REPO), "tool_input": {"command": "git status"}}).returncode == 0


def test_research_agent_sandboxes_are_never_guarded():
    """The bridge's agents run with CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 in data/runs/<run>/<role>/ (#333)."""
    ev = {"tool_input": {"command": "gh pr list"}}
    assert _run("pre-bash", {**ev, "cwd": str(REPO / "data" / "runs" / "17" / "stream")}).returncode == 0
    assert _run("pre-bash", {**ev, "cwd": "/tmp"}, CLAUDE_CODE_DISABLE_CLAUDE_MDS="1").returncode == 0
    assert _run("pre-bash", {**ev, "cwd": "/tmp"}).returncode == 2


def test_pre_edit_blocks_the_main_tree_only_while_a_run_is_live(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ev = {"cwd": str(tmp_path), "tool_input": {"file_path": str(tmp_path / "src" / "x.py")}}
    assert _run("pre-edit", ev, FINRESEARCH_HOOK_LIVE_RUN="1").returncode == 2
    assert _run("pre-edit", ev, FINRESEARCH_HOOK_LIVE_RUN="0").returncode == 0
    outside = {"cwd": str(tmp_path), "tool_input": {"file_path": "/elsewhere/x.py"}}
    assert _run("pre-edit", outside, FINRESEARCH_HOOK_LIVE_RUN="1").returncode == 0


def test_post_edit_reports_ruff_errors_without_rewriting(tmp_path):
    bad = tmp_path / "bad.py"
    source = "import os\n"
    bad.write_text(source)
    res = _run("post-edit", {"cwd": str(REPO), "tool_input": {"file_path": str(bad)}})
    assert res.returncode == 2 and "F401" in res.stderr
    assert bad.read_text() == source
    good = tmp_path / "good.py"
    good.write_text("X = 1\n")
    assert _run("post-edit", {"cwd": str(REPO), "tool_input": {"file_path": str(good)}}).returncode == 0
    assert (
        _run("post-edit", {"cwd": str(REPO), "tool_input": {"file_path": str(tmp_path / "a.md")}}).returncode
        == 0
    )


def test_stop_formats_changed_python_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 110\n")
    f = tmp_path / "x.py"
    f.write_text("x = {'a':1}\n")
    res = _run("stop", {"cwd": str(tmp_path)}, CLAUDE_PROJECT_DIR=str(tmp_path),
               PATH=f"{REPO / '.venv' / 'bin'}:{os.environ['PATH']}")  # fmt: skip
    assert res.returncode == 0
    assert f.read_text() == 'x = {"a": 1}\n'
    assert _run("stop", {"cwd": str(tmp_path), "stop_hook_active": True}).returncode == 0


def test_session_start_reports_a_live_run():
    res = _run("session", {"cwd": str(REPO)}, FINRESEARCH_HOOK_LIVE_RUN="1")
    ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert res.returncode == 0 and "LIVE" in ctx


def _hook_commands() -> list[str]:
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    return [h["command"] for groups in settings["hooks"].values() for g in groups for h in g["hooks"]]


def test_hook_commands_fail_open_when_the_script_is_missing_or_in_a_sandbox(tmp_path):
    """A missing script must never become exit 2 (python3's "can't open file"), which would block every tool call."""
    cmds = _hook_commands()
    assert len(cmds) == 5
    event = json.dumps({"cwd": str(tmp_path), "tool_input": {"command": "gh pr list"}})
    for cmd in cmds:
        base = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_DISABLE_CLAUDE_MDS"}
        for env in ({**base, "CLAUDE_PROJECT_DIR": str(tmp_path)},
                    {**base, "CLAUDE_PROJECT_DIR": str(REPO), "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}):  # fmt: skip
            res = subprocess.run(
                ["sh", "-c", cmd], input=event, capture_output=True, text=True, env=env, timeout=60
            )
            assert res.returncode == 0, (cmd, res.stderr)
    pre_bash = next(c for c in cmds if c.endswith("pre-bash"))
    res = subprocess.run(["sh", "-c", pre_bash], input=event, capture_output=True, text=True, timeout=60,
                         env={**os.environ, "CLAUDE_PROJECT_DIR": str(REPO), "FINRESEARCH_HOOK_LIVE_RUN": "0"})  # fmt: skip
    assert res.returncode == 2  # and it still blocks when the script is there


def test_branch_merges_are_blocked_in_the_main_checkout_only(tmp_path):
    main, wt = tmp_path / "repo", tmp_path / "wt"
    run = lambda *a: subprocess.run(["git", *a], cwd=main, check=True, capture_output=True)  # noqa: E731
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    run(
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    run("worktree", "add", "-q", str(wt), "-b", "feat/x")
    assert guard.check_bash("git merge feat/x", str(main)) is not None
    assert guard.check_bash("git merge --no-ff feat/x", str(main)) is not None
    assert guard.check_bash("git merge origin/main", str(main)) is None
    assert guard.check_bash("git merge --abort", str(main)) is None
    assert guard.check_bash("git merge origin/main", str(wt)) is None  # updating a feature branch is the rule
    assert guard.check_bash("git merge feat/y", str(wt)) is None
