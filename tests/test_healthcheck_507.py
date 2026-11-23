"""scripts/healthcheck.py (#507): the decisions as pure functions over injected probe results. Nothing here asks the
real Postgres, the running app, launchd or the process list: probes and commands are fakes, folders are tmp."""

from __future__ import annotations

import ast
import importlib.util
import socket
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "healthcheck.py"
spec = importlib.util.spec_from_file_location("finresearch_healthcheck", SCRIPT)
hc = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hc  # dataclasses resolve their annotations through sys.modules
spec.loader.exec_module(hc)

CFG = hc.Config(pgdata=Path("/pg/data"), state=Path("/nowhere/state.json"), domain="gui/501")
OPTED_IN = hc.Config(
    pgdata=Path("/pg/data"), state=Path("/nowhere/state.json"), domain="gui/501", move_stale_pid=True
)
OK = hc.Probes(pg=hc.READY, api_ok=True, web_ok=True, vector_ok=True)
# 12-Nov-2026: after the reboot PID 709 belonged to a macOS program and no postgres process ran
STALE = hc.Probes(pg=hc.DOWN, pid_file=True, pid=709, pid_command="/usr/libexec/rapportd", postgres_running=False,
                  web_ok=True)  # fmt: skip


def _with(p, **kw):
    return hc.Probes(**{**p.__dict__, **kw})


def keys(d) -> list[str]:
    return [p.key for p in d.problems]


# ---------------------------------------------------------------------------------------------- decide
def test_a_healthy_machine_has_nothing_to_report():
    d = hc.decide(OK, OPTED_IN)
    assert d.problems == () and d.action is None


def test_the_stale_lock_is_reported_with_the_exact_fix_and_nothing_is_touched_by_default():
    d = hc.decide(STALE, CFG)
    assert keys(d) == ["postgres_stale_lock"] and d.action is None
    msg = d.problems[0].message
    assert "names PID 709, which is now '/usr/libexec/rapportd' (not Postgres)" in msg
    assert (
        "mv '/pg/data/postmaster.pid' '/pg/data/postmaster.pid.stale' && brew services restart postgresql@17"
        in msg
    )


def test_the_opt_in_allows_the_move_only_for_exactly_the_stale_lock():
    d = hc.decide(STALE, OPTED_IN)
    assert (d.action, d.pid) == (hc.MOVE, 709)


@pytest.mark.parametrize("change", [
    {"postgres_running": True},  # a postgres process exists: do nothing
    {"postgres_running": True, "pid_command": "/opt/homebrew/opt/postgresql@17/bin/postgres"},
    {"pid_command": "/opt/homebrew/opt/postgresql@17/bin/postgres"},  # the PID is Postgres itself
    {"pid_command": "postgres"},
    {"pid_command": ""},  # no such process: Postgres clears that file itself
    {"pid_command": None},  # ps could not be asked
    {"postgres_running": None},  # pgrep could not be asked
    {"pid": None, "pid_command": None},  # the file's first line is not a PID
    {"pid_file": False, "pid": None, "pid_command": None},  # no lock file at all
    {"pid_file": None, "pid": None, "pid_command": None},  # the data folder could not be read
    {"pg": "starting"},  # pg_isready: rejecting connections (starting up), not "no response"
    {"pg": "unknown"},  # pg_isready gave no verdict
    {"pg": "ready"},
])  # fmt: skip
def test_anything_else_never_moves_the_file_even_when_opted_in(change):
    d = hc.decide(_with(STALE, **change), OPTED_IN)
    assert d.action is None and "postgres_stale_lock" not in keys(d)
    assert hc.stale_lock(_with(STALE, **change)) is False


def test_a_running_postgres_that_does_not_answer_is_reported_and_left_alone():
    d = hc.decide(_with(STALE, postgres_running=True), OPTED_IN)
    assert keys(d) == ["postgres"] and "A postgres process is running" in d.problems[0].message
    assert "Nothing was changed" in d.problems[0].message


def test_a_dead_pid_and_no_lock_file_say_how_to_start_postgres():
    dead = hc.decide(_with(STALE, pid_command=""), CFG).problems[0]
    assert dead.title == "Postgres is not running" and "PID 709, which no longer exists" in dead.message
    none = hc.decide(_with(STALE, pid_file=False, pid=None, pid_command=None), CFG).problems[0]
    assert "no Postgres process and no postmaster.pid" in none.message
    assert "brew services restart postgresql@17" in dead.message and "brew services restart" in none.message


def test_unknown_probes_are_said_to_be_unknown():
    d = hc.decide(_with(STALE, postgres_running=None), CFG)
    assert keys(d) == ["postgres"] and "could not tell why" in d.problems[0].message
    assert keys(hc.decide(_with(OK, pg="unknown"), CFG)) == ["postgres_probe"]
    assert keys(hc.decide(_with(OK, pg="starting"), CFG)) == ["postgres"]


def test_while_postgres_is_down_only_the_cause_is_reported():
    # the API cannot answer without its database: its state and pgvector's are unknown, not two more alarms
    d = hc.decide(_with(STALE, api_ok=False, vector_ok=False), CFG)
    assert keys(d) == ["postgres_stale_lock"]
    assert keys(hc.decide(_with(STALE, web_ok=False), CFG)) == ["postgres_stale_lock", "web"]


def test_api_web_and_pgvector_problems_when_postgres_is_up():
    api = hc.decide(_with(OK, api_ok=False, vector_ok=None), CFG)
    assert (
        keys(api) == ["api"]
        and "launchctl kickstart -k gui/501/in.finresearch.api" in api.problems[0].message
    )
    web = hc.decide(_with(OK, web_ok=False), CFG)
    assert (
        keys(web) == ["web"]
        and "launchctl kickstart -k gui/501/in.finresearch.web" in web.problems[0].message
    )
    vec = hc.decide(_with(OK, vector_ok=False), CFG)
    assert keys(vec) == ["vector"] and '"$libdir/vector"' in vec.problems[0].message
    assert hc.decide(_with(OK, vector_ok=None), CFG).problems == ()  # not checked is not a failure
    assert hc.decide(_with(OK, api_ok=None, web_ok=None), CFG).problems == ()


# ---------------------------------------------------------------------------------------------- plan (rate limit)
def test_a_problem_is_notified_on_its_second_run_then_every_six_hours_and_recovery_resets_it():
    pr = hc.decide(_with(OK, api_ok=False), CFG).problems
    t0 = 1_000_000.0
    due, st, rec = hc.plan({}, pr, t0)
    assert (due, rec, st["api"]["count"], st["api"]["notified"]) == (
        [],
        [],
        1,
        None,
    )  # a restart in progress?
    due, st, _ = hc.plan(st, pr, t0 + 300)
    assert [p.key for p in due] == ["api"] and st["api"]["notified"] == t0 + 300 and st["api"]["first"] == t0
    for n in range(2, 72):  # every 5 minutes up to 5 h 55 min after the notification: silence
        due, st, _ = hc.plan(st, pr, t0 + 300 * n)
        assert due == []
    due, st, _ = hc.plan(st, pr, t0 + 300 + 6 * 3600)
    assert [p.key for p in due] == ["api"]
    due, st, rec = hc.plan(st, (), t0 + 300 + 6 * 3600 + 300)
    assert (due, st, rec) == ([], {}, ["api"])
    due, st, _ = hc.plan(st, pr, t0 + 8 * 3600)
    assert due == [] and st["api"]["count"] == 1  # a recurrence starts over


def test_a_garbled_state_file_is_treated_as_empty(tmp_path):
    f = tmp_path / "s.json"
    f.write_text("{not json")
    assert hc.load_state(f) == {} and hc.load_state(tmp_path / "missing.json") == {}
    f.write_text("[1, 2]")
    assert hc.load_state(f) == {}
    pr = hc.decide(_with(OK, web_ok=False), CFG).problems
    assert hc.plan({"web": "nonsense"}, pr, 5.0)[1]["web"]["count"] == 1
    hc.save_state(tmp_path / "new" / "s.json", {"a": 1})
    assert hc.load_state(tmp_path / "new" / "s.json") == {"a": 1}


# ---------------------------------------------------------------------------------------------- run_once
class Fakes:
    def __init__(self, *probes):
        self.probes, self.notes, self.moves, self.lines = list(probes), [], [], []

    def probe(self):
        return self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]

    def kw(self):
        def move(pgdata, now):
            self.moves.append((pgdata, now))
            return pgdata / "postmaster.pid.stale-x"

        return {"probe": self.probe, "notify": lambda t, m: self.notes.append((t, m)), "move": move,
                "log": self.lines.append}  # fmt: skip


def test_without_the_opt_in_the_file_is_never_moved_and_the_notification_says_what_to_do():
    f, state = Fakes(STALE), {}
    for n in range(3):
        state = hc.run_once(CFG, state, 1000.0 + 300 * n, **f.kw())
    assert f.moves == [] and state["ok"] is False and "last_move" not in state
    assert len(f.notes) == 1 and f.notes[0][0] == "Postgres cannot start: stale postmaster.pid"
    assert "mv '/pg/data/postmaster.pid'" in f.notes[0][1]
    assert sum(ln.startswith("PROBLEM postgres_stale_lock") for ln in f.lines) == 3  # a log line every run


def test_with_the_opt_in_the_move_waits_for_a_second_run_and_a_fresh_look():
    f = Fakes(STALE)
    state = hc.run_once(OPTED_IN, {}, 1000.0, **f.kw())
    assert f.moves == [] and f.notes == [] and any("ACTION waiting" in ln for ln in f.lines)
    state = hc.run_once(OPTED_IN, state, 1300.0, **f.kw())
    assert f.moves == [(Path("/pg/data"), 1300.0)]
    assert state["last_move"] == {"at": 1300.0, "to": "/pg/data/postmaster.pid.stale-x", "pid": 709}
    assert any(ln.startswith("ACTION moved the stale /pg/data/postmaster.pid (PID 709") for ln in f.lines)
    assert (
        f.notes == [("Moved a stale postmaster.pid aside", f.notes[0][1])]
        and "nothing was deleted" in f.notes[0][1]
    )


def test_the_move_is_called_off_when_the_second_look_differs():
    # run 2: the first probe still shows the stale lock, the look just before acting finds a postgres process
    f = Fakes(STALE, STALE, _with(STALE, postgres_running=True))
    state = hc.run_once(OPTED_IN, {}, 1000.0, **f.kw())
    state = hc.run_once(OPTED_IN, state, 1300.0, **f.kw())
    assert f.moves == [] and any("ACTION skipped" in ln for ln in f.lines) and "last_move" not in state
    # and when the PID in the file changed between the two looks
    f = Fakes(STALE, STALE, _with(STALE, pid=4242))
    state = hc.run_once(OPTED_IN, hc.run_once(OPTED_IN, {}, 1000.0, **f.kw()), 1300.0, **f.kw())
    assert f.moves == []


def test_a_failed_move_is_logged_and_recovery_is_logged():
    f = Fakes(STALE)
    kw = f.kw()

    def refuse(_pgdata, _now):
        raise PermissionError("read-only")

    kw["move"] = refuse
    state = hc.run_once(OPTED_IN, hc.run_once(OPTED_IN, {}, 1000.0, **kw), 1300.0, **kw)
    assert any(ln.startswith("ACTION FAILED could not move") for ln in f.lines) and "last_move" not in state
    g = Fakes(OK)
    state = hc.run_once(OPTED_IN, state, 1600.0, **g.kw())
    assert g.lines == ["RECOVERED postgres_stale_lock"] and state["ok"] is True and state["problems"] == {}


def test_notifications_can_be_turned_off():
    quiet = hc.Config(pgdata=Path("/pg/data"), notify=False, domain="gui/501")
    f, state = Fakes(_with(OK, api_ok=False)), {}
    for n in range(3):
        state = hc.run_once(quiet, state, 1000.0 + 300 * n, **f.kw())
    assert f.notes == [] and len(f.lines) == 3


# ---------------------------------------------------------------------------------------------- the thin probes
def runner(table):
    """A fake command runner: {program: (exit code, stdout)}; anything else is 'not installed'."""
    calls = []

    def run(argv):
        calls.append(argv)
        return table.get(argv[0], (None, ""))

    run.calls = calls
    return run


@pytest.mark.parametrize(
    "rc,want", [(0, "ready"), (1, "starting"), (2, "down"), (3, "unknown"), (9, "unknown")]
)
def test_pg_isready_exit_codes(rc, want):
    assert hc.probe_postgres(CFG, runner({"pg_isready": (rc, "")})) == want


def test_without_pg_isready_only_a_refused_connection_is_down():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    cfg = hc.Config(pghost="127.0.0.1", pgport=port, domain="gui/501")
    s.listen(1)
    assert hc.probe_postgres(cfg, runner({})) == "unknown"  # something listens: starting or ready, no verdict
    s.close()
    assert hc.probe_postgres(cfg, runner({})) == "down"  # this test's own closed port


def test_reading_the_lock_file(tmp_path):
    assert hc.read_pid_file(tmp_path / "missing") == (None, None)
    assert hc.read_pid_file(tmp_path) == (False, None)
    (tmp_path / "postmaster.pid").write_text("709\n/opt/homebrew/var/postgresql@17\n1762900000\n5432\n")
    assert hc.read_pid_file(tmp_path) == (True, 709)
    for bad in ("", "\n", "not-a-pid\n", "0\n", "-5\n"):
        (tmp_path / "postmaster.pid").write_text(bad)
        assert hc.read_pid_file(tmp_path) == (True, None)


def test_process_probes():
    assert hc.pid_command(709, runner({"ps": (0, "/usr/libexec/rapportd\n")})) == "/usr/libexec/rapportd"
    assert hc.pid_command(709, runner({"ps": (1, "")})) == ""  # no such process
    assert hc.pid_command(709, runner({})) is None and hc.pid_command(709, runner({"ps": (2, "")})) is None
    assert hc.any_postgres(runner({"pgrep": (1, "")})) is False
    assert hc.any_postgres(runner({"pgrep": (0, "431\n")})) is True
    assert hc.any_postgres(runner({"pgrep": (3, "")})) is None and hc.any_postgres(runner({})) is None
    assert hc.is_postgres("/opt/homebrew/opt/postgresql@17/bin/postgres") and hc.is_postgres("postgres")
    assert not hc.is_postgres("/usr/libexec/rapportd") and not hc.is_postgres("postgres_exporter")


def test_gather_reads_the_lock_only_when_postgres_is_down_and_pgvector_from_the_api(tmp_path):
    (tmp_path / "postmaster.pid").write_text("709\n")
    cfg = hc.Config(pgdata=tmp_path, domain="gui/501")
    answers = {cfg.api: (200, {"ok": True, "version": "x", "vector": False}), cfg.web: (200, None)}
    up = hc.gather(cfg, runner({"pg_isready": (0, "")}), answers.get)
    assert up == hc.Probes(pg="ready", api_ok=True, web_ok=True, vector_ok=False)
    run = runner({"pg_isready": (2, ""), "ps": (0, "/usr/libexec/rapportd\n"), "pgrep": (1, "")})
    down = hc.gather(cfg, run, lambda url: (None, None))
    assert down == hc.Probes(pg="down", pid_file=True, pid=709, pid_command="/usr/libexec/rapportd",
                             postgres_running=False, api_ok=None, web_ok=False, vector_ok=None)  # fmt: skip
    assert hc.stale_lock(down)
    # an API that answers 500, and one from before #507 with no "vector" field
    old = hc.gather(
        cfg, runner({"pg_isready": (0, "")}), {cfg.api: (200, {"ok": True}), cfg.web: (404, None)}.get
    )
    assert (old.api_ok, old.vector_ok, old.web_ok) == (True, None, True)
    bad = hc.gather(cfg, runner({"pg_isready": (0, "")}), {cfg.api: (500, None), cfg.web: (502, None)}.get)
    assert (bad.api_ok, bad.vector_ok, bad.web_ok) == (False, None, False)


def test_the_move_renames_next_to_itself_and_never_deletes_or_replaces(tmp_path):
    (tmp_path / "postmaster.pid").write_text("709\nrest\n")
    (tmp_path / "PG_VERSION").write_text("17\n")
    dst = hc.move_stale_pid(tmp_path, 1_795_000_000.0)
    assert dst.parent == tmp_path and dst.name.startswith("postmaster.pid.stale-20")
    assert dst.read_text() == "709\nrest\n" and not (tmp_path / "postmaster.pid").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["PG_VERSION", dst.name]
    (tmp_path / "postmaster.pid").write_text(
        "811\n"
    )  # the same second again: the earlier copy is not replaced
    with pytest.raises(FileExistsError):
        hc.move_stale_pid(tmp_path, 1_795_000_000.0)
    assert (tmp_path / "postmaster.pid").read_text() == "811\n" and dst.read_text() == "709\nrest\n"


def test_the_notification_is_one_osascript_call_with_quotes_escaped():
    run = runner({"osascript": (0, "")})
    hc.notify_macos('Postgres "down"', "path C:\\x\nsecond line", run)
    (argv,) = run.calls
    assert argv[:2] == ["osascript", "-e"]
    assert argv[2] == ('display notification "path C:\\\\x second line" with title "FinResearch" '
                       'subtitle "Postgres \\"down\\""')  # fmt: skip


def test_only_the_exact_opt_in_value_opts_in():
    on = hc.Config.from_env({"FINRESEARCH_HEALTH_MOVE_STALE_PID": "1"})
    assert on.move_stale_pid is True
    for v in ("", "0", "true", "yes", "on", " 1", "2"):
        assert hc.Config.from_env({"FINRESEARCH_HEALTH_MOVE_STALE_PID": v}).move_stale_pid is False
    cfg = hc.Config.from_env({"FINRESEARCH_HEALTH_PGDATA": "/x/pg", "FINRESEARCH_HEALTH_PGPORT": "5433",
                              "FINRESEARCH_HEALTH_NOTIFY": "0", "FINRESEARCH_HEALTH_UID_DOMAIN": "gui/7"})  # fmt: skip
    assert (cfg.pgdata, cfg.pgport, cfg.notify, cfg.domain, cfg.move_stale_pid) == (Path("/x/pg"), 5433, False,
                                                                                     "gui/7", False)  # fmt: skip
    assert hc.Config.from_env({}).pgdata == Path("/opt/homebrew/var/postgresql@17")


def test_the_script_is_standard_library_only_and_reads_no_secrets():
    src = SCRIPT.read_text()
    mods = {
        a.name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import) for a in n.names
    }
    mods |= {
        n.module.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)
    code = src.split('"""', 2)[2]  # past the module docstring, which says what is never read
    for never in (".pgpass", '.env"', "/.env", "dotenv", "find-generic-password", "security ", "api_token",
                  "PGPASSWORD", "psql"):  # fmt: skip
        assert never not in code, never
