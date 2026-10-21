#!/usr/bin/env python3
"""Claude Code hooks that enforce the repo's standing rules (AGENT_RULES, ENGINEERING_HANDOFF) on every tool call.

One script, one mode per hook event (wired in .claude/settings.json):
  pre-bash     PreToolUse/Bash: exit 2 with the reason on stderr blocks the command
  pre-edit     PreToolUse/Edit|Write|NotebookEdit: no edits to the main working tree while a research run is live
  post-edit    PostToolUse/Edit|Write: report ruff errors in the edited .py file (never rewrites it: a rewrite would
               make Claude's next Edit of that file fail as "modified since read")
  stop         Stop: ruff-format the changed .py files; refuse to stop while Alembic has more than one head
  session      SessionStart: say whether a research run is live and the last known 5-hour window use

Research agents run `claude -p` in sandboxes under data/runs/ with CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 (#333): every mode
is a no-op there, so the pipeline pays nothing for these hooks. Stdlib only and Python 3.9-compatible, because it runs
on the system python3 (a fresh worktree has no .venv yet). Tests: tests/test_claude_hooks.py.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

# top-level data/ folders whose loss is unrecoverable or personal
DATA_DIRS = ("portfolio", "backups", "state", "devtools", "archive", "reports", "runs", "evals", "docs")
SECRET_FILE = re.compile(r"(^|[\s/'\"=])(\.pgpass|\.env(?!\.example)(\.[\w-]+)?)(?=$|[\s'\";|&)])")
DESTRUCTIVE_SQL = re.compile(r"\b(drop\s+(table|database|schema|extension)|truncate|delete\s+from)\b", re.I)
GIT_TREE_VERBS = ("checkout", "switch", "pull", "merge", "reset", "restore", "clean", "cherry-pick", "revert")
CONFIG_READS = {"--get", "--get-all", "--list", "-l", "--get-regexp", "--show-origin"}
WRAPPERS = ("export", "env", "time", "timeout", "nohup", "caffeinate", "sudo", "exec", "command")


def in_agent_sandbox(event: dict) -> bool:
    cwd = str(event.get("cwd") or os.getcwd())
    return os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1" or "/data/runs/" in cwd + "/"


def live_run_active() -> bool:
    """A research run is live when a sandboxed `claude -p` with the bridge's exact flags is running."""
    if os.environ.get("FINRESEARCH_HOOK_LIVE_RUN") in ("0", "1"):  # tests
        return os.environ["FINRESEARCH_HOOK_LIVE_RUN"] == "1"
    try:
        out = subprocess.run(["ps", "-Ao", "command"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(
        ln.startswith("claude -p") and "--strict-mcp-config" in ln and "--setting-sources project,local" in ln
        for ln in out.splitlines()
    )


def main_tree(cwd: str) -> Path | None:
    """The main working tree's root (the parent of the shared .git directory), or None outside a git repo."""
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                                capture_output=True, text=True, timeout=5).stdout.strip()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(common).parent if common.endswith("/.git") else None


def in_main_checkout(path: str, root: Path | None) -> bool:
    """Inside the main working tree, not in a Claude Code agent worktree (those live under .claude/worktrees/)."""
    return bool(root) and is_inside(path, root) and not is_inside(path, root / ".claude" / "worktrees")


def is_inside(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------- command parsing
def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies (file contents and commit messages are text, not commands)."""
    out, lines, i = [], command.split("\n"), 0
    while i < len(lines):
        out.append(lines[i])
        tags = re.findall(r"<<-?\s*['\"]?([A-Za-z_][\w]*)['\"]?", lines[i])
        i += 1
        for tag in tags:
            while i < len(lines) and lines[i].strip() != tag:
                i += 1
            i += 1
    return "\n".join(out)


def split_commands(command: str) -> list[str]:
    """Split on ; & | newlines and subshell/substitution boundaries that sit outside quotes."""
    parts, cur, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                cur.append(c)
                i += 1
                c = command[i]
            cur.append(c)
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c == "\\" and i + 1 < len(command):
            cur.append(command[i : i + 2])
            i += 1
        elif c in ";&|\n()`" or command.startswith("$(", i):
            parts.append("".join(cur))
            cur = []
            if command.startswith("$(", i):
                i += 1
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def words_of(segment: str) -> list[str]:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]) or words[0] in WRAPPERS):
        words = words[1:]  # VAR=value prefixes and wrappers such as `timeout 600` or `caffeinate -i`
        while words and (words[0].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smh]?", words[0])):
            words = words[1:]
    return words


def assignments(command: str) -> dict[str, str]:
    """VAR=value assignments anywhere in the command (inline, `export`, or a separate statement)."""
    out = {}
    for m in re.finditer(r"(?:^|[\s;&|(])([A-Z_][A-Z0-9_]*)=(\"[^\"]*\"|'[^']*'|[^\s;&|)]*)", command):
        out[m.group(1)] = m.group(2).strip("'\"")
    for k, v in list(out.items()):  # one level of indirection: T=...; FINRESEARCH_TEST_DATABASE_URL=$T
        ref = re.fullmatch(r"\$\{?([A-Z_][A-Z0-9_]*)\}?", v)
        if ref and ref.group(1) in out:
            out[k] = out[ref.group(1)]
    return out


def db_name(url: str) -> str:
    return url.rsplit("/", 1)[-1].split("?", 1)[0] if url else ""


def psql_target(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a in ("-d", "--dbname") and i + 1 < len(args):
            return db_name(args[i + 1])
        if a.startswith("--dbname="):
            return db_name(a.split("=", 1)[1])
        if a.startswith("postgres"):
            return db_name(a)
    return os.environ.get("PGDATABASE", "")


def git_verb(args: list[str]) -> tuple[str, list[str]]:
    while args[:1] and args[0] in ("-c", "-C"):  # git -c user.name=... / git -C <dir>
        args = args[2:]
    args = [a for a in args if a not in ("--no-pager", "-P")]
    return (args[0], args[1:]) if args else ("", [])


# ---------------------------------------------------------------------------------------------------- bash rules
def check_bash(command: str, cwd: str, live: bool = False) -> str | None:
    """The reason to block `command`, or None to allow it."""
    command = strip_heredocs(command)
    env = assignments(command)
    segments = split_commands(command)
    for seg in segments:
        w = words_of(seg)
        if not w:
            continue
        prog, args = Path(w[0]).name, w[1:]
        joined = " ".join(w)

        if prog == "gh":
            if args[:1] == ["auth"] and len(args) > 1 and args[1] not in ("status", "token"):
                return f"never run `gh auth {args[1]}`: the machine's global gh identity must stay as it is"
            if args[:1] != ["auth"]:
                return f"use `git gh {' '.join(args)}` (the repo-local alias that authenticates as Aman4563)"
        if prog == "git":
            verb, rest = git_verb(args)
            if verb == "config" and {"--global", "--system"} & set(rest) and not CONFIG_READS & set(rest):
                return (
                    "never change global/system git config: the Aman4563 identity lives in .git/config only"
                )
            if verb == "push":
                if any(re.fullmatch(r"\+?((HEAD|[\w./-]+):)?(refs/heads/)?(main|master)", a) for a in rest):
                    return "never push to main: issue -> branch -> PR -> merge commit"
                if any(a in ("--mirror", "--all") for a in rest):
                    return "never push --mirror/--all: push only your branch"
            if verb == "stash" and (not rest or rest[0] not in ("list", "show")):
                return "never use git stash: the stash is shared across worktrees; commit WIP to your branch"
            merged = [
                a for a in rest if not a.startswith("-") and a not in ("origin/main", "main", "FETCH_HEAD")
            ]
            if verb == "merge" and merged and not {"--abort", "--continue", "--quit"} & set(rest):
                root = main_tree(cwd)
                if in_main_checkout(cwd, root):
                    return ("never merge a branch into the main checkout (it is the live app's tree, e.g. superpowers' "
                            "'merge locally' option): push the branch, open a PR and deploy with /ship")  # fmt: skip
            if verb == "rebase" and any(re.fullmatch(r"(origin/)?(main|master)", a) for a in rest):
                return (
                    "never rebase onto a newer main (that once reverted #270-#272): `git merge origin/main`"
                )
            if verb == "reset" and "--soft" in rest and any(re.fullmatch(r"(origin/)?main", a) for a in rest):
                return "never soft-reset onto main and recommit an old tree: `git merge origin/main`"
            if live and verb in GIT_TREE_VERBS:
                root = main_tree(cwd)
                if in_main_checkout(cwd, root):
                    return ("a research run is live: never change the main working tree's files or branch; work in "
                            "a worktree (`git worktree add ../wt-<name> -b <branch> origin/main`)")  # fmt: skip
        ln_flags = args[0] if prog == "ln" and args and re.fullmatch(r"-[a-zA-Z]*", args[0]) else ""
        if (
            "s" in ln_flags
            and "f" in ln_flags
            and "n" not in ln_flags
            and any("node_modules" in a for a in args)
        ):
            return ("`ln -sf` onto an existing node_modules link nests a link inside the main web/node_modules: "
                    "copy it (`cp -cR`) instead")  # fmt: skip
        if prog == "dropdb" and any(not a.endswith("_test") for a in args if not a.startswith("-")):
            return "dropdb only on scratch databases named *_test (never the live `finresearch`)"
        if prog == "psql" and DESTRUCTIVE_SQL.search(joined) and not psql_target(args).endswith("_test"):
            return f"destructive SQL only on *_test databases (target here: {psql_target(args) or 'the default'})"
        if prog == "pg_restore" and not psql_target(args).endswith("_test"):
            return "restoring over a non-test database is a manual operator action (docs/BACKUPS.md)"
        if prog == "rm" and any(
            re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*", a) or a == "--recursive" for a in args
        ):
            for a in (a for a in args if not a.startswith("-")):
                p = a.rstrip("/")
                if re.fullmatch(r"(.*/)?data", p) or re.fullmatch(
                    r"(.*/)?data/(" + "|".join(DATA_DIRS) + ")", p
                ):
                    return f"never delete {a}: data/ holds the live runs, state, backups and portfolio"
        if prog in ("kill", "pkill", "killall") and re.search(
            r"(finresearch|next start|next-server|claude_code_bridge|\bclaude\b|uvicorn)", joined
        ):
            return "never stop the live app or Claude processes: merge_deploy.sh restarts the live servers"
        if prog in ("cat", "head", "tail", "less", "more", "grep", "rg", "sed", "awk", "cp", "base64", "strings") and \
                SECRET_FILE.search(" " + " ".join(args)):  # fmt: skip
            return "never read .env files or ~/.pgpass (libpq and the app read them themselves)"
        if prog == "security" and args and re.fullmatch(r"find-(generic|internet)-password", args[0]):
            return "never read Keychain passwords"
        if prog == "pytest" or (prog in ("uv", "python", "python3") and "pytest" in args):
            if "--collect-only" in args or "--co" in args:
                continue
            if not env.get("FINRESEARCH_TEST_DATABASE_URL"):
                return ("pass your own test database inline: T=postgresql+psycopg://localhost/finresearch_<you>_test; "
                        "FINRESEARCH_TEST_DATABASE_URL=$T FINRESEARCH_DATABASE_URL=$T uv run pytest ... "
                        "(the default finresearch_test is shared and reset by every session)")  # fmt: skip
            for var in ("FINRESEARCH_TEST_DATABASE_URL", "FINRESEARCH_DATABASE_URL"):
                if env.get(var) and not db_name(env[var]).endswith("_test"):
                    return f"{var} must name a database ending in _test when running pytest"
        if prog == "alembic" or (prog == "uv" and "alembic" in args):
            verb = next((a for a in args if a in ("upgrade", "downgrade", "stamp", "revision")), None)
            url = env.get("FINRESEARCH_DATABASE_URL") or os.environ.get("FINRESEARCH_DATABASE_URL", "")
            if verb and not db_name(url).endswith("_test"):
                return (f"`alembic {verb}` needs FINRESEARCH_DATABASE_URL pointing at a scratch *_test database in "
                        "the same command; the live database is migrated only by merge_deploy.sh, after a backup")  # fmt: skip
    killers = any(prog_in(segments, k) for k in ("kill", "pkill", "killall")) or re.search(r"xargs[^;&|]*\bkill\b",
                                                                                           command)  # fmt: skip
    if killers and re.search(
        r"(pgrep|lsof)[^;&|]*(claude|finresearch|uvicorn|next|tcp:(8710|3100)\b)", command
    ):
        return "never stop the live app (:8710/:3100): merge_deploy.sh restarts the live servers"
    if re.search(r"merge_deploy\.sh[^\n;&]*\|", command):
        return "never pipe merge_deploy.sh: the pipe hides its exit code; run it bare and check `$?`"
    return None


def prog_in(segments: list[str], name: str) -> bool:
    return any((w := words_of(s)) and Path(w[0]).name == name for s in segments)


# ---------------------------------------------------------------------------------------------------------- modes
def project_dir(event: dict) -> str:
    return os.environ.get("CLAUDE_PROJECT_DIR") or str(event.get("cwd") or os.getcwd())


def pre_bash(event: dict) -> int:
    command = str((event.get("tool_input") or {}).get("command") or "")
    cwd = str(event.get("cwd") or os.getcwd())
    git_tree_change = re.search(r"\bgit\b[^;&|\n]*\b(" + "|".join(GIT_TREE_VERBS) + r")\b", command)
    reason = check_bash(command, cwd, live=bool(git_tree_change) and live_run_active())
    if reason:
        print(f"FinResearch guard (.claude/hooks/guard.py) blocked this: {reason}.", file=sys.stderr)
        return 2
    wrapped = filter_test_output(command)
    if wrapped:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                      "permissionDecisionReason": "test output filtered (.claude/hooks/test_filter.py)",
                                      "updatedInput": {**(event.get("tool_input") or {}), "command": wrapped}}}  # fmt: skip
        print(json.dumps(out))
    return 0


def runs_tests(command: str) -> bool:
    for seg in split_commands(command):
        w = words_of(seg)
        if not w or "--collect-only" in w or "--co" in w or "--list" in w:
            continue
        prog, args = Path(w[0]).name, w[1:]
        if prog == "pytest" or (prog in ("uv", "python", "python3") and "pytest" in args):
            return True
        if (prog == "pnpm" and args[:1] == ["e2e"]) or ("playwright" in w and "test" in w):
            return True
    return False


def filter_test_output(command: str) -> str | None:
    """pytest/Playwright commands piped through test_filter.py (pipefail keeps their exit code); None to leave as is.

    Commands that already pipe or redirect their output, or carry a heredoc, are left alone."""
    if (
        not runs_tests(command)
        or "<<" in command
        or re.search(r"(?<![|&>])\|(?!\|)|(?<![<&2])>(?!&)", command)
    ):
        return None
    flt = Path(__file__).resolve().with_name("test_filter.py")
    if not flt.exists():
        return None
    return f'set -o pipefail; {{ {command}\n}} 2>&1 | python3 "{flt}"'


def pre_edit(event: dict) -> int:
    ti = event.get("tool_input") or {}
    path = str(ti.get("file_path") or ti.get("notebook_path") or "")
    root = main_tree(str(event.get("cwd") or os.getcwd()))
    if path and in_main_checkout(path, root) and not is_inside(path, root / "data") and live_run_active():
        print("FinResearch guard blocked this edit: a research run is live, so the main working tree must not change. "
              "Work in a worktree: git worktree add ../wt-<name> -b <branch> origin/main", file=sys.stderr)  # fmt: skip
        return 2
    return 0


def ruff_cmd(root: str) -> list[str] | None:
    local = Path(root) / ".venv" / "bin" / "ruff"
    if local.exists():
        return [str(local)]
    if shutil.which("ruff"):
        return ["ruff"]
    return ["uv", "run", "--no-sync", "ruff"] if (Path(root) / "pyproject.toml").exists() else None


def post_edit(event: dict) -> int:
    path = str((event.get("tool_input") or {}).get("file_path") or "")
    root = project_dir(event)
    if re.search(r"\.(tsx?|jsx?|mjs)$", path):
        return eslint_edited(path, root)
    ruff = ruff_cmd(root)
    if not path.endswith(".py") or not Path(path).exists() or not ruff:
        return 0
    try:
        res = subprocess.run([*ruff, "check", "--quiet", "--output-format", "concise", path], cwd=root,
                             capture_output=True, text=True, timeout=30)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return 0
    if res.returncode == 1 and res.stdout.strip():
        lines = res.stdout.strip().splitlines()
        print("ruff found problems in the file just edited (CI fails on them):\n" + "\n".join(lines[:20]),
              file=sys.stderr)  # fmt: skip
        return 2
    return 0


def eslint_edited(path: str, root: str) -> int:
    """Report ESLint errors in an edited web/ file (report only, like ruff above; web/ has no formatter)."""
    web = Path(root) / "web"
    eslint = web / "node_modules" / ".bin" / "eslint"
    if not eslint.exists() or not Path(path).exists() or not is_inside(path, web):
        return 0
    try:
        res = subprocess.run([str(eslint), "--no-warn-ignored", "--format", "unix", path], cwd=web,
                             capture_output=True, text=True, timeout=90)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return 0
    if res.returncode == 1 and res.stdout.strip():
        lines = res.stdout.strip().splitlines()
        print("eslint found problems in the file just edited (CI's `pnpm lint` fails on errors):\n"
              + "\n".join(lines[:20]), file=sys.stderr)  # fmt: skip
        return 2
    return 0


def stop(event: dict) -> int:
    if event.get("stop_hook_active"):
        return 0
    root = project_dir(event)
    main = main_tree(root)
    if main and main.resolve() == Path(root).resolve() and live_run_active():
        return 0  # never rewrite files in the main tree during a live run
    try:
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=root,
                                capture_output=True, text=True, timeout=10).stdout.splitlines()  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return 0
    files = [ln[3:].split(" -> ")[-1].strip('"') for ln in status if "D" not in ln[:2]]
    ruff = ruff_cmd(root)
    py = [f for f in files if f.endswith(".py") and Path(root, f).exists()]
    if py and ruff:
        subprocess.run([*ruff, "format", "--quiet", *py], cwd=root, capture_output=True, timeout=60)
    if any(f.startswith("migrations/versions/") for f in files):
        alembic = Path(root) / ".venv" / "bin" / "alembic"
        cmd = [str(alembic)] if alembic.exists() else ["uv", "run", "--no-sync", "alembic"]
        try:
            heads = subprocess.run(
                [*cmd, "heads"], cwd=root, capture_output=True, text=True, timeout=60
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return 0
        n = sum("(head)" in ln for ln in heads.splitlines())
        if n > 1:
            print(f"`alembic heads` shows {n} heads, so CI's Alembic round trip fails: merge origin/main and re-point "
                  "your migration's down_revision so there is exactly one head.", file=sys.stderr)  # fmt: skip
            return 2
    return 0


def session(event: dict) -> int:
    notes = []
    if live_run_active():
        notes.append("A FinResearch research run is LIVE (sandboxed claude -p agents are running): do not edit, switch "
                     "branches in or deploy the main working tree, and do not restart :8710/:3100. Use a worktree.")  # fmt: skip
    main = main_tree(project_dir(event))
    if main:
        try:
            snap = json.loads((main / "data" / "state" / "tier_state.json").read_text())["claude_max"][
                "snapshot"
            ]
            used, resets = snap.get("five_hour_utilization"), snap.get("five_hour_resets_at")
            if used is not None and resets and resets > time.time():
                at = time.strftime("%H:%M", time.localtime(resets))
                notes.append(
                    f"Claude Max 5-hour window: {used:.0%} used at the last research step, resets {at}."
                )
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if notes:
        out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(notes)}}
        print(json.dumps(out))
    return 0


MODES = {"pre-bash": pre_bash, "pre-edit": pre_edit, "post-edit": post_edit, "stop": stop, "session": session}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if mode not in MODES or not isinstance(event, dict) or in_agent_sandbox(event):
        return 0
    return MODES[mode](event)


if __name__ == "__main__":
    sys.exit(main())
