#!/usr/bin/env python3
"""FinResearch health check (#507): is Postgres accepting connections, do the API and the web app answer, and can
Postgres load pgvector? launchd runs it every few minutes (scripts/launchd/, docs/USAGE.md). Each problem gets a
log line on every run and a macOS notification, rate-limited through a small state file.

Why it exists: on 2-Nov-2026 the Mac restarted and the app stayed down for three weeks; from 12-Nov Postgres itself
refused to start because `postmaster.pid` named a PID that, after the reboot, belonged to another program. Nobody
was told.

It works without the app and without the database: standard library only, no `finresearch` import, and it never
reads `.env`, `~/.pgpass` or the Keychain (no database login: Postgres is asked with `pg_isready`, which needs none,
and pgvector's state comes from the API's unauthenticated /api/health).

The decisions are pure functions over probe results (`decide`, `plan`), tested in tests/test_healthcheck_507.py
with fakes; `main` only gathers the probes and carries the decision out.

The stale lock. Postgres refuses to start while postmaster.pid names a live process. That is detected exactly: no
answer from Postgres, the file exists, its PID is alive and is NOT a postgres process, and no postgres process
exists at all. By default the notification says what to do and nothing is touched. Only with the explicit opt-in
FINRESEARCH_HEALTH_MOVE_STALE_PID=1, and only when two runs in a row saw exactly that and a fresh probe just before
acting still does, the file is MOVED aside (renamed next to itself, never deleted) and the move is logged. Whenever
any postgres process exists, or any probe could not answer, nothing is done.

Settings (environment; the installer writes them into the launchd job):
  FINRESEARCH_HEALTH_PGDATA           Postgres data directory (default /opt/homebrew/var/postgresql@17)
  FINRESEARCH_HEALTH_PGHOST / _PGPORT default localhost / 5432
  FINRESEARCH_HEALTH_API / _WEB       default http://127.0.0.1:8710/api/health and http://127.0.0.1:3100/
  FINRESEARCH_HEALTH_STATE            state file (default <repo>/data/state/healthcheck.json)
  FINRESEARCH_HEALTH_MOVE_STALE_PID   "1" opts in to moving a stale postmaster.pid aside (default: off)
  FINRESEARCH_HEALTH_NOTIFY           "0" turns the notifications off (the log lines stay)
  FINRESEARCH_HEALTH_UID_DOMAIN       launchd domain named in the advice (default gui/<uid>)
  FINRESEARCH_HEALTH_LABEL_API / _WEB launchd labels named in the advice (default in.finresearch.api / .web)

Run by hand: `uv run --no-project python scripts/healthcheck.py --dry-run` prints what it sees and would do, and
changes nothing (no notification, no state, no move).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
CONFIRM_RUNS = 2  # a problem is notified, and a stale lock moved, only when two runs in a row saw it
NOTIFY_EVERY_S = 6 * 3600  # one notification per problem per six hours while it lasts
POSTGRES_NAMES = ("postgres", "postmaster")
READY, STARTING, DOWN, UNKNOWN = "ready", "starting", "down", "unknown"
MOVE = "move_stale_pid"


@dataclass(frozen=True)
class Probes:
    """What one run saw. None always means "could not be determined", never yes or no."""

    pg: str = UNKNOWN  # READY | STARTING (rejecting connections) | DOWN (no response) | UNKNOWN
    pid_file: bool | None = None  # postmaster.pid exists
    pid: int | None = None  # its first line
    pid_command: str | None = None  # that PID's command; "" when no such process
    postgres_running: bool | None = None  # any process named postgres, of any user
    api_ok: bool | None = None  # GET /api/health answered 200
    web_ok: bool | None = None  # the web app answered below 500
    vector_ok: bool | None = None  # /api/health's "vector"


@dataclass(frozen=True)
class Problem:
    key: str
    title: str
    message: str


@dataclass(frozen=True)
class Decision:
    problems: tuple[Problem, ...] = ()
    action: str | None = None  # MOVE or None
    pid: int | None = None  # the stale PID the action is about


@dataclass(frozen=True)
class Config:
    pgdata: Path = Path("/opt/homebrew/var/postgresql@17")
    pghost: str = "localhost"
    pgport: int = 5432
    api: str = "http://127.0.0.1:8710/api/health"
    web: str = "http://127.0.0.1:3100/"
    state: Path = REPO / "data" / "state" / "healthcheck.json"
    move_stale_pid: bool = False
    notify: bool = True
    domain: str = field(default_factory=lambda: f"gui/{os.getuid()}")
    label_api: str = "in.finresearch.api"
    label_web: str = "in.finresearch.web"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = os.environ if env is None else env
        d = cls()
        return cls(
            pgdata=Path(e.get("FINRESEARCH_HEALTH_PGDATA") or d.pgdata),
            pghost=e.get("FINRESEARCH_HEALTH_PGHOST") or d.pghost,
            pgport=int(e.get("FINRESEARCH_HEALTH_PGPORT") or d.pgport),
            api=e.get("FINRESEARCH_HEALTH_API") or d.api,
            web=e.get("FINRESEARCH_HEALTH_WEB") or d.web,
            state=Path(e.get("FINRESEARCH_HEALTH_STATE") or d.state),
            move_stale_pid=e.get("FINRESEARCH_HEALTH_MOVE_STALE_PID", "")
            == "1",  # exactly "1": nothing else opts in
            notify=e.get("FINRESEARCH_HEALTH_NOTIFY", "1") != "0",
            domain=e.get("FINRESEARCH_HEALTH_UID_DOMAIN") or d.domain,
            label_api=e.get("FINRESEARCH_HEALTH_LABEL_API") or d.label_api,
            label_web=e.get("FINRESEARCH_HEALTH_LABEL_WEB") or d.label_web,
        )


# ---------------------------------------------------------------------------------------------- pure decisions
def is_postgres(command: str) -> bool:
    return os.path.basename(command.strip().split(" ")[0]).lstrip("-") in POSTGRES_NAMES


def stale_lock(p: Probes) -> bool:
    """Exactly the case that blocks every start: Postgres gives no answer, postmaster.pid exists, the PID in it is
    alive and is not a postgres process, and no postgres process exists at all. Any unknown makes it False."""
    return (
        p.pg == DOWN
        and p.pid_file is True
        and p.pid is not None
        and bool(p.pid_command)  # known and alive ("" = no such process, None = could not ask)
        and not is_postgres(p.pid_command or "")
        and p.postgres_running is False
    )


def _postgres_down(p: Probes, cfg: Config) -> tuple[Problem, bool]:
    """The problem to report while Postgres gives no answer, and whether it is the stale lock."""
    where, pidfile = f"{cfg.pghost}:{cfg.pgport}", cfg.pgdata / "postmaster.pid"
    start = "brew services restart postgresql@17"
    if stale_lock(p):
        fix = f"mv '{pidfile}' '{pidfile}.stale' && {start}"
        return Problem(
            "postgres_stale_lock",
            "Postgres cannot start: stale postmaster.pid",
            f"{pidfile} names PID {p.pid}, which is now '{p.pid_command}' (not Postgres), and no Postgres process is "
            f"running, so Postgres refuses every start. To fix it, run: {fix}",
        ), True
    if p.postgres_running is True:
        odd = ""
        if p.pid_file and p.pid is not None and p.pid_command and not is_postgres(p.pid_command):
            # e.g. another cluster runs while this one's lock is stale: said, never acted on
            odd = f" {pidfile} names PID {p.pid}, which is '{p.pid_command}', not Postgres."
        return Problem(
            "postgres",
            "Postgres is not accepting connections",
            f"A postgres process is running but nothing answers on {where} (still starting, shutting down, or "
            f"another cluster).{odd} Nothing was changed, because a postgres process exists. Look at: brew "
            "services list, and Postgres's log.",
        ), False
    if (
        p.postgres_running is None
        or p.pid_file is None
        or (p.pid_file and (p.pid is None or p.pid_command is None))
    ):
        return Problem(
            "postgres",
            "Postgres is not accepting connections",
            f"Nothing answers on {where}, and the check could not tell why (the process list or {pidfile} could not "
            "be read). Nothing was changed. Look at: brew services list.",
        ), False
    if p.pid_file and p.pid_command == "":
        return Problem(
            "postgres",
            "Postgres is not running",
            f"Nothing answers on {where}. {pidfile} names PID {p.pid}, which no longer exists: Postgres clears "
            f"such a file itself when it starts. Start it: {start}",
        ), False
    if p.pid_file:  # the PID is a postgres process although none is listed: a race; look again next run
        return Problem(
            "postgres",
            "Postgres is not accepting connections",
            f"Nothing answers on {where} and the process list changed while it was read. Nothing was changed.",
        ), False
    return Problem(
        "postgres",
        "Postgres is not running",
        f"Nothing answers on {where}; there is no Postgres process and no postmaster.pid. Start it: {start}",
    ), False


def decide(p: Probes, cfg: Config) -> Decision:
    """The problems to report and the one action that may follow. While Postgres is not ready the API's and
    pgvector's states are unknown (the API needs the database), so only the cause is reported."""
    out: list[Problem] = []
    action = pid = None
    if p.pg == DOWN:
        problem, stale = _postgres_down(p, cfg)
        out.append(problem)
        if stale and cfg.move_stale_pid:
            action, pid = MOVE, p.pid
    elif p.pg == STARTING:
        out.append(Problem("postgres", "Postgres is not accepting connections yet",
                           f"Postgres on {cfg.pghost}:{cfg.pgport} is starting up or recovering and rejects "
                           "connections. Nothing was changed; this run only reports it."))  # fmt: skip
    elif p.pg != READY:
        out.append(Problem("postgres_probe", "Cannot check Postgres",
                           "pg_isready could not run or gave no verdict, so whether Postgres accepts connections is "
                           "unknown. Is pg_isready on the job's PATH?"))  # fmt: skip
    else:
        if p.api_ok is False:
            out.append(Problem("api", "FinResearch API is not answering",
                               f"{cfg.api} does not answer (the API and the monitor are down). Restart: launchctl "
                               f"kickstart -k {cfg.domain}/{cfg.label_api}; log: data/logs/serve.log"))  # fmt: skip
        elif p.api_ok is True and p.vector_ok is False:
            out.append(Problem("vector", "Postgres cannot load pgvector",
                               'The vector extension does not load (could not access file "$libdir/vector" or not '
                               "installed), so document search fails. Rebuild it for this Postgres: see "
                               "docs/dev/ENGINEERING_HANDOFF.md, 'pgvector on Postgres 17'."))  # fmt: skip
    if p.web_ok is False:  # the web app serves pages without the database: reported on its own
        out.append(Problem("web", "FinResearch web app is not answering",
                           f"{cfg.web} does not answer. Restart: launchctl kickstart -k {cfg.domain}/"
                           f"{cfg.label_web}; log: data/logs/web.launchd.log"))  # fmt: skip
    return Decision(tuple(out), action, pid)


def plan(state: dict[str, Any], problems: tuple[Problem, ...], now: float, *, confirm: int = CONFIRM_RUNS,
         every_s: float = NOTIFY_EVERY_S) -> tuple[list[Problem], dict[str, Any], list[str]]:  # fmt: skip
    """Rate limiting. `state` is the last run's {key: {first, count, notified}}. Returns the problems to notify now
    (seen `confirm` runs in a row, and not notified in the last `every_s`), the new state, and the keys that
    recovered since the last run (dropped, so a recurrence is new)."""
    new: dict[str, Any] = {}
    due: list[Problem] = []
    for pr in problems:
        old = state.get(pr.key) if isinstance(state.get(pr.key), dict) else {}
        e = {
            "first": old.get("first", now),
            "count": int(old.get("count", 0)) + 1,
            "notified": old.get("notified"),
        }
        if e["count"] >= confirm and (e["notified"] is None or now - float(e["notified"]) >= every_s):
            due.append(pr)
            e["notified"] = now
        new[pr.key] = e
    return due, new, sorted(k for k in state if k not in new)


def confirmed(decision: Decision, problems_state: dict[str, Any], *, confirm: int = CONFIRM_RUNS) -> bool:
    """The stale-lock action only once the same finding has been seen `confirm` runs in a row."""
    seen = problems_state.get("postgres_stale_lock") or {}
    return decision.action == MOVE and int(seen.get("count", 0)) >= confirm


# ---------------------------------------------------------------------------------------------- probes (thin)
Run = Callable[[list], tuple]  # argv -> (exit code or None, stdout); spelled for any Python 3.9+


def run_command(argv: list[str]) -> tuple[int | None, str]:
    """(exit code, stdout); (None, "") when the program is missing or did not finish in 10 s."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return r.returncode, r.stdout


def probe_postgres(cfg: Config, run: Run = run_command) -> str:
    """pg_isready's verdict (it needs no login): 0 accepting, 1 rejecting (starting up), 2 no response; 3 or no
    verdict is unknown. Without pg_isready a refused TCP connection is DOWN and anything else is unknown."""
    rc, _ = run(["pg_isready", "-q", "-h", cfg.pghost, "-p", str(cfg.pgport), "-t", "3"])
    if rc is not None:
        return {0: READY, 1: STARTING, 2: DOWN}.get(rc, UNKNOWN)
    try:
        socket.create_connection((cfg.pghost, cfg.pgport), timeout=3).close()
    except ConnectionRefusedError:
        return DOWN
    except OSError:
        pass
    return UNKNOWN  # a port that accepts TCP may still be starting up: not a verdict


def read_pid_file(pgdata: Path) -> tuple[bool | None, int | None]:
    """(postmaster.pid exists, the PID on its first line). (None, None) when the folder cannot be read."""
    f = pgdata / "postmaster.pid"
    try:
        if not pgdata.is_dir():
            return None, None
        if not f.is_file():
            return False, None
        lines = f.read_text(errors="replace").splitlines()
    except OSError:
        return None, None
    first = lines[0].strip() if lines else ""
    return True, int(first) if first.isdigit() and int(first) > 0 else None


def pid_command(pid: int, run: Run = run_command) -> str | None:
    """The command of `pid`: "" when no such process, None when ps could not be asked."""
    rc, out = run(["ps", "-p", str(pid), "-o", "comm="])
    if rc is None or rc not in (0, 1):
        return None
    return out.strip() if rc == 0 and out.strip() else ""


def any_postgres(run: Run = run_command) -> bool | None:
    """Is any process named postgres running (any user)? None when pgrep could not be asked."""
    seen: bool | None = False
    for name in POSTGRES_NAMES:
        rc, _ = run(["pgrep", "-x", name])
        if rc == 0:
            return True
        if rc != 1:  # 1 = no match; anything else is no verdict
            seen = None
    return seen


def http_get(url: str, timeout: float = 5.0) -> tuple[int | None, Any]:
    """(status, JSON body or None) without any proxy; (None, None) when nothing answered."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            status, body = r.status, r.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return None, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, None


def gather(
    cfg: Config, run: Run = run_command, get: Callable[[str], tuple[int | None, Any]] = http_get
) -> Probes:
    pg = probe_postgres(cfg, run)
    pid_file, pid = read_pid_file(cfg.pgdata) if pg == DOWN else (None, None)
    api_ok = vector_ok = None
    if pg == READY:
        status, body = get(cfg.api)
        api_ok = status == 200
        vector = body.get("vector") if api_ok and isinstance(body, dict) else None
        vector_ok = vector if isinstance(vector, bool) else None
    web_status, _ = get(cfg.web)
    return Probes(
        pg=pg,
        pid_file=pid_file,
        pid=pid,
        pid_command=pid_command(pid, run) if pid is not None else None,
        postgres_running=any_postgres(run) if pg == DOWN else None,
        api_ok=api_ok,
        web_ok=web_status is not None and web_status < 500,
        vector_ok=vector_ok,
    )


# ---------------------------------------------------------------------------------------------- acting (thin)
def move_stale_pid(pgdata: Path, now: float) -> Path:
    """Rename postmaster.pid next to itself (same folder, so it is one atomic rename) and return the new path. The
    file is never deleted and an existing file is never replaced."""
    src = pgdata / "postmaster.pid"
    dst = pgdata / f"postmaster.pid.stale-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now))}"
    if dst.exists():
        raise FileExistsError(f"{dst} already exists")
    os.rename(src, dst)
    return dst


def notify_macos(title: str, message: str, run: Run = run_command) -> bool:
    """A Notification Centre banner through osascript (the text is passed as AppleScript strings, quotes escaped).
    False when osascript is missing or failed: the caller logs it, so a banner that never showed is not silent."""

    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") + '"'

    script = f"display notification {q(message[:240])} with title {q('FinResearch')} subtitle {q(title)}"
    rc, _ = run(["osascript", "-e", script])
    return rc == 0


def load_state(path: Path) -> dict[str, Any]:
    try:
        got = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def run_once(cfg: Config, state: dict[str, Any], now: float, *, probe: Callable[[], Probes],
             notify: Callable[[str, str], Any], move: Callable[[Path, float], Path],
             log: Callable[[str], None]) -> dict[str, Any]:  # fmt: skip
    """One check: probe, decide, log every problem, notify the due ones, and (opt-in, confirmed, re-probed) move a
    stale postmaster.pid aside. Returns the new state."""
    seen = probe()
    decision = decide(seen, cfg)
    old = state.get("problems") if isinstance(state.get("problems"), dict) else {}
    due, problems, recovered = plan(old, decision.problems, now)
    for key in recovered:
        log(f"RECOVERED {key}")
    for pr in decision.problems:
        log(f"PROBLEM {pr.key}: {pr.title}. {pr.message}")
    moved: str | None = None
    if confirmed(decision, problems):
        again = decide(probe(), cfg)  # look once more just before touching anything
        if again.action == MOVE and again.pid == decision.pid:
            try:
                moved = str(move(cfg.pgdata, now))
                log(f"ACTION moved the stale {cfg.pgdata / 'postmaster.pid'} (PID {decision.pid}, not a postgres "
                    f"process; no postgres process running) to {moved}. Nothing was deleted. Postgres's own "
                    "launchd job starts it at its next retry, or run: brew services restart postgresql@17")  # fmt: skip
            except OSError as e:
                log(f"ACTION FAILED could not move the stale postmaster.pid: {type(e).__name__}: {e}")
        else:
            log("ACTION skipped: the stale lock was not confirmed by a second look; nothing was changed")
    elif decision.action == MOVE:
        log("ACTION waiting: a stale postmaster.pid is moved only when two runs in a row see it")
    if cfg.notify:
        for pr in due:
            if moved and pr.key == "postgres_stale_lock":
                shown = notify("Moved a stale postmaster.pid aside",
                               f"It named PID {decision.pid}, which is not Postgres. It is now {moved}; nothing "
                               "was deleted. Postgres should start at its next retry.")  # fmt: skip
            else:
                shown = notify(pr.title, pr.message)
            if shown is False:  # osascript missing or refused: this log line is then the only notice
                log(f"NOTIFY FAILED {pr.key}: the macOS notification could not be shown (osascript)")
    return {"checked_at": now, "ok": not decision.problems, "problems": problems,
            **({"last_move": {"at": now, "to": moved, "pid": decision.pid}} if moved else
               {"last_move": state["last_move"]} if "last_move" in state else {})}  # fmt: skip


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cfg = Config.from_env()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    def log(line: str) -> None:
        print(f"{stamp} {line}", flush=True)

    if "--dry-run" in args:
        seen = gather(cfg)
        decision = decide(seen, cfg)
        log(f"DRY RUN probes: {seen}")
        for pr in decision.problems:
            log(f"DRY RUN would report {pr.key}: {pr.title}. {pr.message}")
        log(f"DRY RUN action: {decision.action or 'none'} (opt-in {'on' if cfg.move_stale_pid else 'off'}); "
            "nothing was notified, saved or moved")  # fmt: skip
        return 0
    state = run_once(cfg, load_state(cfg.state), time.time(), probe=lambda: gather(cfg), notify=notify_macos,
                     move=move_stale_pid, log=log)  # fmt: skip
    try:
        save_state(cfg.state, state)
    except OSError as e:
        log(f"could not write {cfg.state}: {type(e).__name__}: {e}")
    return 0  # a found problem is a result, not a failed run: launchd must not see a crash


if __name__ == "__main__":
    raise SystemExit(main())
