# SPDX-License-Identifier: Apache-2.0
"""`wavescribe service`: the LaunchAgent plist and install, status and uninstall, against a
fake launchd and a temporary home. No test loads a real LaunchAgent or runs launchctl."""

import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_agent import SERVICE_KEYS
from wavescribe import doctor, service
from wavescribe.cli import build_parser, main
from wavescribe.web.server import PortInUseError

PY = "/opt/ws/.venv/bin/python"


class FakeLaunchd:
    """launchd as far as the service sees it: a job that starts when bootstrapped."""

    def __init__(self, *, starts: bool = True) -> None:
        self.calls: list[tuple[str, object]] = []
        self.loaded: Path | None = None
        self.pid: int | None = None
        self.runs = 0
        self.last_exit: str | None = None
        self.starts = starts

    def print_service(self, label: str) -> str | None:
        self.calls.append(("print", label))
        if self.loaded is None:
            return None
        lines = [f"gui/501/{label} = {{", f"\tpath = {self.loaded}", "\ttype = LaunchAgent",
                 f"\tstate = {'running' if self.pid else 'not running'}", "",
                 "\tprogram = /usr/bin/caffeinate", f"\truns = {self.runs}"]  # fmt: skip
        if self.pid:
            lines.append(f"\tpid = {self.pid}")
        lines.append(f"\tlast exit code = {self.last_exit or '(never exited)'}")
        lines += ["\tresource coalition = {", "\t\tstate = active", "\t}", "}"]
        return "\n".join(lines) + "\n"

    def enable(self, label: str) -> None:
        self.calls.append(("enable", label))

    def bootstrap(self, plist: Path) -> None:
        self.calls.append(("bootstrap", plist))
        self.loaded = plist
        if self.starts:
            self.runs += 1
            self.pid = 4242

    def bootout(self, label: str) -> None:
        self.calls.append(("bootout", label))
        self.loaded = self.pid = None

    def names(self) -> list[str]:
        return [c[0] for c in self.calls if c[0] != "print"]


@pytest.fixture(autouse=True)
def no_real_launchd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Any real launchctl call fails the test, and the home directory is a temporary one. No
    real port is looked up either: nothing listens on any (a test says what does)."""

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a test tried to reach the real launchd")

    monkeypatch.setattr(service, "_run", refuse)
    monkeypatch.setattr(service, "default_launchd", refuse)
    monkeypatch.setattr(doctor, "port_holder", lambda *_a, **_k: None)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def job(workdir: Path, *args: str) -> service.Job:
    return service.Job(workdir=workdir, python=PY, serve_args=args, env={"PATH": "/usr/bin"})


def test_the_plist_runs_serve_from_the_project_environment_and_keeps_it_going(
    tmp_path: Path,
) -> None:
    p = service.build_plist(job(tmp_path, "--lan", "--port", "8780"))
    assert p["Label"] == service.LABEL == "io.github.kaspler.wavescribe"
    assert p["ProgramArguments"] == ["/usr/bin/caffeinate", "-i", PY, "-m", "wavescribe",
                                     "serve", "--lan", "--port", "8780"]  # fmt: skip
    assert p["WorkingDirectory"] == str(tmp_path)
    assert p["RunAtLoad"] is True
    assert p["KeepAlive"] == {"SuccessfulExit": False}  # restarted after any failure
    assert p["ThrottleInterval"] >= 10 and p["ExitTimeOut"] >= 20
    assert p["ProcessType"] == "Interactive"  # no background CPU and I/O limits
    log = str(tmp_path / "data" / "logs" / "service.log")
    assert p["StandardOutPath"] == p["StandardErrorPath"] == log
    assert p["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1", "PATH": "/usr/bin",
                                         "WAVESCRIBE_HOME": str(tmp_path)}  # fmt: skip
    assert plistlib.loads(plistlib.dumps(p)) == p
    no_caffeinate = service.Job(tmp_path, PY, (), {}, keep_awake=False)
    assert service.build_plist(no_caffeinate)["ProgramArguments"][0] == PY


def test_the_agent_finds_homebrew_tools_and_keeps_locale_and_model_folders() -> None:
    path = service.agent_path(lambda tool: {"ffmpeg": "/tools/bin/ffmpeg"}.get(tool))
    dirs = path.split(":")
    assert dirs[0] == "/tools/bin" and "/usr/bin" in dirs and "/usr/sbin" in dirs
    assert len(dirs) == len(set(dirs))
    env = service.job_env({"LANG": "es_ES.UTF-8", "HF_HOME": "/models", "API_KEY": "x"},
                          which=lambda _t: None)  # fmt: skip
    assert env["LANG"] == "es_ES.UTF-8" and env["HF_HOME"] == "/models"
    assert "API_KEY" not in env and "PATH" in env


def test_install_status_and_uninstall(tmp_path: Path, no_real_launchd: Path) -> None:
    home, launchd = no_real_launchd, FakeLaunchd()
    work = tmp_path / "ws"
    st = service.install(job(work, "--port", "8780"), launchd, home, sleep=lambda _s: None)
    plist = home / "Library" / "LaunchAgents" / "io.github.kaspler.wavescribe.plist"
    assert plist.exists() and oct(plist.stat().st_mode & 0o777) == "0o644"
    assert launchd.names() == ["enable", "bootstrap"]
    assert st["installed"] and st["loaded"] and st["running"] and st["pid"] == 4242
    assert st["serve_args"] == ["--port", "8780"] and st["workdir"] == str(work)
    assert st["log"] == str(work / "data" / "logs" / "service.log")
    assert (work / "data" / "logs").is_dir()
    text = "\n".join(service.describe(st))
    assert "running (pid 4242)" in text and "serve --port 8780" in text
    assert "caffeinate -i" in text and "wavescribe.jsonl" in text

    assert service.uninstall(launchd, home) == {"was_loaded": True, "removed": True}
    assert not plist.exists() and launchd.loaded is None
    st = service.status(launchd, home)
    assert not st["installed"] and not st["loaded"] and not st["running"]
    assert "not installed" in "\n".join(service.describe(st))
    assert service.uninstall(launchd, home) == {"was_loaded": False, "removed": False}


def test_reinstall_stops_the_running_one_first_and_moves_a_big_log_aside(
    tmp_path: Path, no_real_launchd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launchd = FakeLaunchd()
    service.install(job(tmp_path), launchd, no_real_launchd, sleep=lambda _s: None)
    log = tmp_path / "data" / "logs" / "service.log"
    log.write_text("a long story\n" * 10)
    monkeypatch.setattr(service, "LOG_ROTATE_BYTES", 50)
    launchd.calls.clear()
    st = service.install(job(tmp_path, "--lan"), launchd, no_real_launchd, sleep=lambda _s: None)
    assert launchd.names() == ["bootout", "enable", "bootstrap"]
    assert (tmp_path / "data" / "logs" / "service.log.1").read_text().startswith("a long")
    assert not log.exists() and st["serve_args"] == ["--lan"] and st["runs"] == 2


def test_status_reads_launchctl_print_and_reports_a_crash_loop(
    tmp_path: Path, no_real_launchd: Path
) -> None:
    text = (
        "gui/501/io.github.kaspler.wavescribe = {\n\tactive count = 0\n\tstate = not running\n"
        "\truns = 7\n\tlast exit code = 1: Operation not permitted\n"
        "\tlast terminating signal = Terminated: 15\n\tendpoints = {\n\t\tstate = active\n\t}\n}\n"
    )
    assert service.parse_print(text) == {"state": "not running", "pid": None, "runs": 7,
                                         "last_exit": "1: Operation not permitted",
                                         "last_signal": "Terminated: 15"}  # fmt: skip
    launchd = FakeLaunchd(starts=False)
    service.install(job(tmp_path), launchd, no_real_launchd, sleep=lambda _s: None)
    launchd.runs, launchd.last_exit = 5, "1"
    st = service.status(launchd, no_real_launchd)
    assert st["loaded"] and not st["running"] and st["last_exit"] == "1"
    shown = "\n".join(service.describe(st))
    assert "NOT running" in shown and "last exit: 1" in shown and "tries again" in shown


def test_launchctl_is_called_in_the_users_gui_domain() -> None:
    calls: list[list[str]] = []
    answers = {"print": (113, "", "Could not find service"), "bootout": (5, "", "Boot-out failed")}

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        code, out, err = answers.get(args[1], (0, "", ""))
        return subprocess.CompletedProcess(args, code, out, err)

    lc = service.Launchctl(uid=501, runner=runner)
    assert lc.print_service("x.y") is None  # not loaded
    lc.enable("x.y")
    lc.bootstrap(Path("/h/Library/LaunchAgents/x.y.plist"))
    with pytest.raises(service.ServiceError, match="Boot-out failed"):
        lc.bootout("x.y")
    assert calls == [
        ["/bin/launchctl", "print", "gui/501/x.y"],
        ["/bin/launchctl", "enable", "gui/501/x.y"],
        ["/bin/launchctl", "bootstrap", "gui/501", "/h/Library/LaunchAgents/x.y.plist"],
        ["/bin/launchctl", "bootout", "gui/501/x.y"],
    ]


def test_install_takes_serves_options_and_gives_them_back_as_serves_command_line() -> None:
    ns = build_parser().parse_args(["service", "install", "--lan", "--port", "8780",
                                    "--audio-days", "3", "--no-translate", "--config",
                                    "config/x.toml", "uno", "dos"])  # fmt: skip
    argv = service.serve_argv(ns.serve_parser, ns)
    assert argv[-2:] == ["uno", "dos"]
    back = service.check_serve_args(argv)
    for key in ("lan", "port", "audio_days", "no_translate", "config", "stations", "host",
                "settings", "text_days"):  # fmt: skip
        assert getattr(back, key) == getattr(ns, key), key
    defaults = build_parser().parse_args(["service", "install"])
    assert service.serve_argv(defaults.serve_parser, defaults) == []


def write_folder(root: Path, stations: str | None = None) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "stations.toml").write_text(stations or (
        '[defaults]\nlanguage = "es-ES"\n\n'
        '[[station]]\nid = "uno"\nstream = "http://radio.test/uno"\n'))  # fmt: skip
    return root


def test_the_command_installs_reports_and_removes(
    tmp_path: Path, no_real_launchd: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    work = write_folder(tmp_path / "ws")
    monkeypatch.chdir(work)
    launchd, probed = FakeLaunchd(), []
    monkeypatch.setattr(service, "default_launchd", lambda: launchd)
    monkeypatch.setattr(service, "port_probe", lambda h, p: probed.append((h, p)))
    assert main(["service", "install", "--port", "8780", "--no-alerts"]) == 0
    out = capsys.readouterr().out
    assert "Installed and started" in out and "http://localhost:8780" in out
    plist = no_real_launchd / "Library" / "LaunchAgents" / f"{service.LABEL}.plist"
    args = plistlib.loads(plist.read_bytes())["ProgramArguments"]
    assert args[-4:] == ["serve", "--port", "8780", "--no-alerts"]
    assert args[2] == sys.executable and probed == [("127.0.0.1", 8780)]

    assert main(["service", "status"]) == 0
    assert "running (pid 4242)" in capsys.readouterr().out
    assert main(["service", "status", "--json"]) == 0
    st = json.loads(capsys.readouterr().out)
    assert set(st) == SERVICE_KEYS
    assert st["pid"] == 4242 and st["serve_args"] == ["--port", "8780", "--no-alerts"]

    # Installing again replaces the running one; its port isn't checked, as it holds it.
    assert main(["service", "install", "--lan"]) == 0
    assert probed == [("127.0.0.1", 8780)] and launchd.names()[-3:] == [
        "bootout", "enable", "bootstrap"]  # fmt: skip
    capsys.readouterr()
    assert main(["service", "uninstall"]) == 0
    assert "Stopped and removed" in capsys.readouterr().out and not plist.exists()
    assert main(["service", "status"]) == 3
    assert main(["service", "uninstall"]) == 0
    assert "nothing to do" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "stations", "expect"),
    [
        (["--open"], None, "--open doesn't apply"),
        ([], '[[station]]\nid = "uno"\nlanguage = [', "serve would stop at start"),
        (["nope"], None, "unknown station 'nope'"),
        (["--alerts", "config/missing.toml"], None, "missing.toml: not found"),
        ([], None, "port 8765 is already in use"),
    ],
)
def test_install_refuses_what_would_stop_serve_at_every_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, args, stations, expect
) -> None:
    monkeypatch.chdir(write_folder(tmp_path / "ws", stations))
    launchd = FakeLaunchd()
    monkeypatch.setattr(service, "default_launchd", lambda: launchd)

    def busy(host: str, port: int) -> None:
        raise PortInUseError(f"port {port} is already in use on {host} by another program")

    monkeypatch.setattr(service, "port_probe", busy)
    assert main(["service", "install", *args]) == 1
    assert expect in capsys.readouterr().err
    assert "bootstrap" not in launchd.names()


def test_install_says_when_the_port_is_a_wavescribe_already_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(write_folder(tmp_path / "ws"))
    launchd = FakeLaunchd()
    monkeypatch.setattr(service, "default_launchd", lambda: launchd)

    def busy(host: str, port: int) -> None:
        raise PortInUseError(f"port {port} is already in use on {host}")

    monkeypatch.setattr(service, "port_probe", busy)
    serve = doctor.PortHolder(4000, 3999, "/ws/.venv/bin/python /ws/.venv/bin/wavescribe serve "
                              "--port 8790", "127.0.0.1:8790")  # fmt: skip
    monkeypatch.setattr(doctor, "port_holder", lambda *_a, **_k: serve)
    assert main(["service", "install", "--port", "8790"]) == 1
    assert capsys.readouterr().err == (
        "wavescribe: Wavescribe is already running at http://localhost:8790/ (pid 4000); stop it "
        "first (Ctrl-C in its Terminal), or install with another --port\n"
    )
    assert "bootstrap" not in launchd.names()


def test_the_service_runs_in_the_home_it_was_installed_with(
    tmp_path: Path, no_real_launchd: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    start, h = tmp_path / "start", tmp_path / "radio home"
    start.mkdir()
    monkeypatch.chdir(start)  # no config/ here, nor in the new home: --home names it anyway
    monkeypatch.setattr(service, "default_launchd", lambda: pytest.fail("reached launchd"))
    argv = ["service", "install", "--port", "8796", "--dry-run", "--config", "s.toml"]
    (start / "s.toml").write_text('[[station]]\nid = "uno"\nlanguage = "es"\n'
                                  'stream = "https://radio.test/uno"\n')  # fmt: skip
    assert main([*argv, "--home", str(h)]) == 0
    printed = plistlib.loads(capsys.readouterr().out.encode())
    assert printed["WorkingDirectory"] == str(h.resolve())
    assert printed["EnvironmentVariables"]["WAVESCRIBE_HOME"] == str(h.resolve())
    assert printed["StandardErrorPath"] == str(h.resolve() / "data" / "logs" / "service.log")
    assert printed["ProgramArguments"][-5:] == [  # the stations file absolute, from start/
        "serve", "--config", str(start / "s.toml"), "--port", "8796"]  # fmt: skip
    assert "--home" not in printed["ProgramArguments"]
    assert not (no_real_launchd / "Library").exists()


def test_install_needs_the_wavescribe_folder_and_a_dry_run_loads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)  # no config/ here
    monkeypatch.setattr(service, "default_launchd", FakeLaunchd)
    assert main(["service", "install"]) == 1
    assert "isn't a Wavescribe folder" in capsys.readouterr().err
    monkeypatch.chdir(write_folder(tmp_path / "ws"))
    # default_launchd refuses (the autouse fixture): a dry run must not reach launchd.
    monkeypatch.setattr(service, "default_launchd", lambda: pytest.fail("reached launchd"))
    assert main(["service", "install", "--lan", "--dry-run"]) == 0
    printed = plistlib.loads(capsys.readouterr().out.encode())
    assert printed["ProgramArguments"][-2:] == ["serve", "--lan"]
    assert not (tmp_path / "home" / "Library").exists()
