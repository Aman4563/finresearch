"""scripts/launchd/ (#507): the plist templates, the installer and the launcher. Nothing here talks to launchd or to
a running service: the scripts are copied into a tmp checkout (whose name has a space and an "&", like the real
path's space), `launchctl` is a recording fake, the LaunchAgents folder is a tmp folder, and `uv`, `pnpm` and
`caffeinate` are fakes that only write down how they were called."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import socket
import stat
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHD = ROOT / "scripts" / "launchd"
TEMPLATES = sorted(LAUNCHD.glob("*.plist.template"))
PLACEHOLDERS = {"__LABEL__", "__LABEL_API__", "__LABEL_WEB__", "__REPO__", "__HOME__", "__TOOL_DIRS__", "__PGDATA__",
                "__UID_DOMAIN__", "__MOVE_STALE_PID__"}  # fmt: skip
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:__HOME__/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def _exe(p: Path, body: str) -> Path:
    p.write_text(body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


# ---------------------------------------------------------------------------------------------- the templates
def test_there_are_three_templates_and_they_parse_as_committed():
    assert [t.name for t in TEMPLATES] == ["in.finresearch.api.plist.template",
                                           "in.finresearch.healthcheck.plist.template",
                                           "in.finresearch.web.plist.template"]  # fmt: skip
    for t in TEMPLATES:
        plistlib.loads(t.read_bytes())  # CI has no plutil: the parser is the check everywhere
        if shutil.which("plutil"):
            assert subprocess.run(["plutil", "-lint", str(t)], capture_output=True).returncode == 0


def test_nothing_user_specific_is_committed():
    for f in [*LAUNCHD.iterdir(), ROOT / "scripts" / "healthcheck.py"]:
        text = f.read_text()
        assert "/Users/" not in text and "/home/" not in text, f.name
    for t in TEMPLATES:
        assert set(re.findall(r"__[A-Z_]+__", t.read_text())) <= PLACEHOLDERS, t.name
        d = plistlib.loads(t.read_bytes())
        assert d["Label"] == "__LABEL__" and d["WorkingDirectory"].startswith("__REPO__")
        assert d["ProgramArguments"][:2] == ["/bin/bash", "__REPO__/scripts/launchd/run-service.sh"]
        assert (
            d["StandardOutPath"].startswith("__REPO__/data/logs/")
            and d["StandardErrorPath"] == d["StandardOutPath"]
        )


def test_the_service_jobs_restart_with_a_throttle_and_run_in_the_gui_session():
    by = {t.name.split(".")[2]: plistlib.loads(t.read_bytes()) for t in TEMPLATES}
    for name in ("api", "web"):
        d = by[name]
        assert d["ProgramArguments"][2] == name and d["RunAtLoad"] is True
        assert d["KeepAlive"] is True and d["ThrottleInterval"] == 30  # a crash loop: one start per 30 s
        assert (
            d["LimitLoadToSessionType"] == "Aqua"
        )  # the login Keychain is reachable only from the GUI session
        assert d["EnvironmentVariables"]["PATH"] == "__TOOL_DIRS__" + BASE_PATH
    assert (
        by["api"]["StandardOutPath"] == "__REPO__/data/logs/serve.launchd.log"
    )  # serve.log is the app's, rotated
    assert by["web"]["WorkingDirectory"] == "__REPO__/web" and by["web"]["StandardOutPath"].endswith(
        "/web.log"
    )
    h = by["healthcheck"]
    assert h["StartInterval"] == 300 and "KeepAlive" not in h and "RunAtLoad" not in h
    assert h["EnvironmentVariables"]["FINRESEARCH_HEALTH_MOVE_STALE_PID"] == "__MOVE_STALE_PID__"


# ---------------------------------------------------------------------------------------------- a tmp checkout
class Box:
    """A copy of scripts/ in a tmp 'checkout', a tmp HOME and LaunchAgents folder, and a recording fake launchctl."""

    def __init__(self, tmp: Path):
        self.repo = tmp / "check out & co"
        shutil.copytree(ROOT / "scripts" / "launchd", self.repo / "scripts" / "launchd")
        shutil.copy(ROOT / "scripts" / "healthcheck.py", self.repo / "scripts" / "healthcheck.py")
        (self.repo / "web").mkdir()
        self.home, self.dest, self.bin = tmp / "home", tmp / "home" / "Library" / "LaunchAgents", tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.calls, self.loaded = tmp / "launchctl.calls", tmp / "launchctl.loaded"
        self.loaded.write_text("")
        # print: 0 when the label is loaded, else launchctl's "could not find service" (113)
        self.launchctl = _exe(
            self.bin / "launchctl",
            f"""#!/bin/bash
echo "$*" >> "{self.calls}"
case "$1" in
  print) grep -qx "${{2##*/}}" "{self.loaded}" || exit 113 ;;
  bootstrap) basename "$3" .plist >> "{self.loaded}" ;;
  bootout) grep -vx "${{2##*/}}" "{self.loaded}" > "{self.loaded}.tmp"; mv "{self.loaded}.tmp" "{self.loaded}" ;;
esac
exit 0
""",
        )
        assert Path.home() not in self.dest.parents and tmp in self.launchctl.parents  # never the real ones

    def env(self, **extra: str) -> dict[str, str]:
        return {**os.environ, "HOME": str(self.home), "LAUNCH_AGENTS_DIR": str(self.dest),
                "LAUNCHCTL": str(self.launchctl), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", **extra}  # fmt: skip

    def run(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.repo / "scripts" / "launchd" / script), *args], env=self.env(),
                              capture_output=True, text=True, timeout=60)  # fmt: skip

    def launchctl_calls(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def plist(self, svc: str) -> dict:
        return plistlib.loads((self.dest / f"in.finresearch.{svc}.plist").read_bytes())


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


DOMAIN = f"gui/{os.getuid()}"


def test_print_renders_valid_plists_and_changes_nothing(box):
    r = box.run("install.sh", "--print")
    assert r.returncode == 0, r.stderr
    parts = re.split(r"^# ---- would write (.+)$", r.stdout, flags=re.M)[1:]
    assert [Path(p).name for p in parts[0::2]] == [
        f"in.finresearch.{s}.plist" for s in ("api", "web", "healthcheck")
    ]
    api, web, health = (plistlib.loads(x.strip().encode()) for x in parts[1::2])
    assert "__" not in "".join(parts[1::2])
    # the path's space and "&" survive the XML exactly
    assert api["ProgramArguments"] == ["/bin/bash", f"{box.repo}/scripts/launchd/run-service.sh", "api"]
    assert api["Label"] == "in.finresearch.api" and api["WorkingDirectory"] == str(box.repo)
    assert (
        web["WorkingDirectory"] == f"{box.repo}/web"
        and web["StandardOutPath"] == f"{box.repo}/data/logs/web.log"
    )
    path = api["EnvironmentVariables"]["PATH"].split(":")
    assert path[-7:] == ["/opt/homebrew/bin", "/usr/local/bin", f"{box.home}/.local/bin", "/usr/bin", "/bin",
                         "/usr/sbin", "/sbin"]  # fmt: skip
    env = health["EnvironmentVariables"]
    assert env["FINRESEARCH_HEALTH_MOVE_STALE_PID"] == "0"  # off unless asked for
    assert env["FINRESEARCH_HEALTH_PGDATA"] == "/opt/homebrew/var/postgresql@17"
    assert (
        env["FINRESEARCH_HEALTH_UID_DOMAIN"] == DOMAIN
        and env["FINRESEARCH_HEALTH_LABEL_API"] == "in.finresearch.api"
    )
    assert not box.dest.exists() and box.launchctl_calls() == [] and not (box.repo / "data").exists()


def test_install_loads_each_job_once_and_a_second_run_changes_nothing(box):
    r = box.run("install.sh")
    assert r.returncode == 0, r.stderr
    labels = [f"in.finresearch.{s}" for s in ("api", "web", "healthcheck")]
    assert sorted(p.name for p in box.dest.iterdir()) == sorted(f"{x}.plist" for x in labels)
    assert box.launchctl_calls() == [
        *[c for x in labels for c in (f"print {DOMAIN}/{x}", f"bootstrap {DOMAIN} {box.dest}/{x}.plist")]]  # fmt: skip
    assert box.plist("api")["KeepAlive"] is True and (box.repo / "data" / "logs").is_dir()
    assert "launchctl kickstart -k" in r.stdout  # the one restart command per service is printed
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in box.dest.iterdir()}
    box.calls.unlink()
    r = box.run("install.sh")
    assert (
        r.returncode == 0 and r.stdout.count("is up to date") == 3 and r.stdout.count("already loaded") == 3
    )
    assert box.launchctl_calls() == [f"print {DOMAIN}/{x}" for x in labels]  # looked, started nothing
    assert {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in box.dest.iterdir()} == before


def test_install_never_stops_or_restarts_a_loaded_job_even_when_its_plist_changed(box):
    assert box.run("install.sh").returncode == 0
    box.calls.unlink()
    r = box.run("install.sh", "--allow-move-stale-postmaster-pid", "--pgdata", "/tmp/pg data")
    assert r.returncode == 0, r.stderr
    env = box.plist("healthcheck")["EnvironmentVariables"]
    assert (
        env["FINRESEARCH_HEALTH_MOVE_STALE_PID"] == "1" and env["FINRESEARCH_HEALTH_PGDATA"] == "/tmp/pg data"
    )
    assert all(
        c.startswith("print ") for c in box.launchctl_calls()
    )  # no bootout, kickstart, kill or bootstrap
    assert "in.finresearch.healthcheck is loaded with its OLD settings and was left running" in r.stdout
    assert (
        f"launchctl bootout {DOMAIN}/in.finresearch.healthcheck && launchctl bootstrap {DOMAIN}" in r.stdout
    )
    assert r.stdout.count("already loaded: left as it is") == 2  # api and web: unchanged, untouched


def test_no_load_and_bad_options(box):
    r = box.run("install.sh", "--no-load", "--label-prefix", "dev.fin")
    assert r.returncode == 0 and box.launchctl_calls() == []
    assert plistlib.loads((box.dest / "dev.fin.api.plist").read_bytes())["Label"] == "dev.fin.api"
    assert box.run("install.sh", "--frobnicate").returncode == 64
    assert box.run("install.sh", "--label-prefix", "a b").returncode == 64
    assert box.run("uninstall.sh", "--label-prefix", "x/../y").returncode == 64
    assert "write the three plists" in box.run("install.sh", "--help").stdout


def test_uninstall_unloads_what_is_loaded_and_deletes_the_plists(box):
    assert box.run("install.sh").returncode == 0
    box.calls.unlink()
    shown = box.run("uninstall.sh", "--print")
    assert shown.returncode == 0 and box.launchctl_calls() == [] and len(list(box.dest.iterdir())) == 3
    r = box.run("uninstall.sh")
    assert r.returncode == 0, r.stderr
    labels = [f"in.finresearch.{s}" for s in ("healthcheck", "web", "api")]
    assert box.launchctl_calls() == [
        c for x in labels for c in (f"print {DOMAIN}/{x}", f"bootout {DOMAIN}/{x}")
    ]
    assert list(box.dest.iterdir()) == [] and "its service is stopped" in r.stdout
    box.calls.unlink()
    again = box.run("uninstall.sh")  # nothing left: says so, removes nothing, fails nothing
    assert again.returncode == 0 and again.stdout.count("is not loaded") == 3
    assert box.launchctl_calls() == [f"print {DOMAIN}/{x}" for x in labels]


def test_the_scripts_hold_no_kill_and_read_no_secrets():
    for name in ("install.sh", "uninstall.sh", "run-service.sh"):
        code = "\n".join(
            ln for ln in (LAUNCHD / name).read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        assert not re.search(r"\b(kill|pkill|killall)\b", code), name
        for never in (".pgpass", ".env", "security ", "find-generic-password", "api_token"):
            assert never not in code, (name, never)
    code = (LAUNCHD / "install.sh").read_text()
    assert "kickstart" not in code.split("cat <<EOF")[0]  # only named in the closing advice, never run


# ---------------------------------------------------------------------------------------------- the launcher
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _fake_tools(box: Box) -> Path:
    """uv, pnpm and caffeinate that write their name, folder and arguments to one file and exit."""
    out = box.bin.parent / "tool.calls"
    for tool in ("uv", "pnpm", "caffeinate"):
        _exe(box.bin / tool, f'#!/bin/bash\necho "{tool}|$PWD|$*" >> "{out}"\n')
    return out


def _launch(box: Box, service: str, **env: str) -> subprocess.Popen:
    return subprocess.Popen(["bash", str(box.repo / "scripts" / "launchd" / "run-service.sh"), service],
                            env=box.env(**env), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)  # fmt: skip


def test_the_launcher_starts_each_service_as_it_is_started_by_hand(box):
    calls = _fake_tools(box)
    api, web = _free_port(), _free_port()
    for svc in ("api", "web", "healthcheck"):
        p = _launch(box, svc, FINRESEARCH_API_PORT=str(api), FINRESEARCH_WEB_PORT=str(web))
        assert p.wait(timeout=30) == 0, p.stdout.read()
    repo = os.path.realpath(box.repo)
    got = [ln.split("|") for ln in calls.read_text().splitlines()]
    assert [(tool, os.path.realpath(cwd), args) for tool, cwd, args in got] == [
        (
            "caffeinate",
            repo,
            f"-i uv run finresearch serve --port {api} --log-file {repo}/data/logs/serve.log",
        ),
        ("pnpm", f"{repo}/web", f"start -H 127.0.0.1 -p {web}"),
        ("uv", repo, f"run --no-project --quiet python {repo}/scripts/healthcheck.py"),
    ]
    bad = _launch(box, "everything")
    assert bad.wait(timeout=30) == 64


def test_the_launcher_never_starts_a_second_copy_while_the_port_answers(box):
    calls = _fake_tools(box)
    with socket.socket() as held:  # the copy started by hand: this test's own listener
        held.bind(("127.0.0.1", 0))
        held.listen(8)
        port = held.getsockname()[1]
        p = _launch(box, "api", FINRESEARCH_API_PORT=str(port), FINRESEARCH_PORT_WAIT_S="0.2")
        time.sleep(1.5)  # several looks at the port
        assert p.poll() is None and not calls.exists()  # still waiting, nothing started
    try:  # the hand-started copy stopped: the job takes over
        assert p.wait(timeout=30) == 0
    finally:
        if p.poll() is None:
            p.kill()  # this test's own child
    out = p.stdout.read()
    assert f"port {port} already answers" in out and "not starting a second one" in out
    assert f"port {port} is free: starting" in out
    assert calls.read_text().startswith("caffeinate|") and calls.read_text().count("\n") == 1
