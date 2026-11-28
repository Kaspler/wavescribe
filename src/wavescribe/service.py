# SPDX-License-Identifier: Apache-2.0
"""``wavescribe service``: run ``wavescribe serve`` in the background as a launchd LaunchAgent.

A per-user agent (``~/Library/LaunchAgents/<LABEL>.plist``) that starts at login and when it is
installed, and restarts after a crash. What it runs:

    /usr/bin/caffeinate -i <this environment's python> -m wavescribe serve <the serve options>

- **The project's own environment.** The Python that ran ``service install`` (``sys.executable``,
  e.g. ``.venv/bin/python``) runs ``-m wavescribe`` directly, so a restart needs neither uv nor
  the network.
- **It runs in the home** it was installed with (``wavescribe/home.py``), where ``config/`` and
  ``data/`` are: the plist's ``WorkingDirectory``, also named by ``WAVESCRIBE_HOME`` in its
  environment, so the service uses that home whatever launchd's own environment holds.
- **Kept awake.** ``caffeinate -i`` holds an idle-sleep assertion while serve runs. It passes
  SIGTERM on to serve and exits with serve's exit code, so launchd stops it cleanly and sees a
  crash as one. It doesn't stop clamshell sleep: a MacBook with its lid closed still sleeps
  (docs/MEASUREMENTS.md, the 34 h soak), unless ``sudo pmset -a disablesleep 1``.
- **Restarts.** ``KeepAlive`` with ``SuccessfulExit = false``: any exit but 0 starts it again,
  no sooner than ``ThrottleInterval`` after the previous start, so a broken config costs one
  short start every 30 s rather than a busy loop. ``ProcessType = Interactive``: launchd's
  default puts light CPU and I/O limits on agents, and live transcription must keep up.
- **Logs.** launchd sends serve's stdout and stderr (start-up lines, warnings, tracebacks) to
  ``data/logs/service.log``; the full structured log is ``data/logs/wavescribe.jsonl``, rotated
  by serve itself. launchd doesn't rotate service.log, so each install moves a big one aside.

Every ``launchctl`` call goes through the ``Launchd`` interface, so the tests use a fake and a
temporary home directory and never load a real agent.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from wavescribe.home import ENV as HOME_ENV
from wavescribe.invocation import typed

LABEL = "io.github.kaspler.wavescribe"
LAUNCHCTL = "/bin/launchctl"
CAFFEINATE = "/usr/bin/caffeinate"
LOG_DIR = Path("data/logs")
LOG_NAME = "service.log"
LOG_ROTATE_BYTES = 10 * 1024**2  # moved aside to service.log.1 on the next install
THROTTLE_S = 30
EXIT_TIMEOUT_S = 30  # serve closes its workers, receivers and the store on SIGTERM
# Copied into the agent's environment when set at install, so it finds the same models and
# reads the same locale (the band plan's default region comes from it).
KEPT_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "HF_HOME", "HF_HUB_CACHE", "WAVESCRIBE_MODELS_DIR")
SYSTEM_PATH = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
BREW_PATH = ("/opt/homebrew/bin", "/usr/local/bin")


class ServiceError(RuntimeError):
    """The service can't be installed, removed or read."""


class Launchd(Protocol):
    """The few launchctl operations the service needs (macOS only)."""

    def print_service(self, label: str) -> str | None:
        """``launchctl print`` of the loaded job, or None when it isn't loaded."""
        ...

    def enable(self, label: str) -> None: ...

    def bootstrap(self, plist: Path) -> None: ...

    def bootout(self, label: str) -> None: ...


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=60, check=False)


class Launchctl:
    """The real thing: ``launchctl`` in this user's GUI domain (``gui/<uid>``)."""

    def __init__(self, uid: int | None = None, runner: Runner | None = None) -> None:
        self.domain = f"gui/{os.getuid() if uid is None else uid}"
        self._runner = runner or _run

    def _call(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._runner([LAUNCHCTL, *args])

    def _check(self, done: subprocess.CompletedProcess[str], what: str) -> None:
        if done.returncode != 0:
            said = (done.stderr or done.stdout or "").strip()
            raise ServiceError(f"launchctl {what} failed ({done.returncode}): {said}")

    def print_service(self, label: str) -> str | None:
        done = self._call("print", f"{self.domain}/{label}")
        return done.stdout if done.returncode == 0 else None

    def enable(self, label: str) -> None:
        # A job switched off before (launchctl disable, or under Login Items) won't bootstrap.
        self._check(self._call("enable", f"{self.domain}/{label}"), "enable")

    def bootstrap(self, plist: Path) -> None:
        self._check(self._call("bootstrap", self.domain, str(plist)), "bootstrap")

    def bootout(self, label: str) -> None:
        self._check(self._call("bootout", f"{self.domain}/{label}"), "bootout")


def default_launchd() -> Launchd:
    if sys.platform != "darwin":
        raise ServiceError("the background service uses launchd, which is macOS only")
    return Launchctl()


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


# -- the job ---------------------------------------------------------------------------------


def agent_path(which: Callable[[str], str | None] = shutil.which) -> str:
    """PATH for the agent: launchd's default lacks Homebrew, where ffmpeg and the rtl tools
    live. The folders they were found in come first, then Homebrew's and the system's."""
    dirs: list[str] = []
    for tool in ("ffmpeg", "rtl_sdr"):
        found = which(tool)
        if found:
            dirs.append(str(Path(found).parent))
    dirs += [d for d in BREW_PATH if Path(d).is_dir()]
    dirs += SYSTEM_PATH
    return ":".join(dict.fromkeys(dirs))


@dataclass(frozen=True)
class Job:
    """What the agent runs, and where."""

    workdir: Path
    python: str
    serve_args: tuple[str, ...]
    env: dict[str, str]
    keep_awake: bool = True

    @property
    def log_path(self) -> Path:
        return self.workdir / LOG_DIR / LOG_NAME

    @property
    def command(self) -> list[str]:
        awake = [CAFFEINATE, "-i"] if self.keep_awake else []
        return [*awake, self.python, "-m", "wavescribe", "serve", *self.serve_args]


def build_plist(job: Job, label: str = LABEL) -> dict[str, Any]:
    log = str(job.log_path)
    return {
        "Label": label,
        "ProgramArguments": job.command,
        "WorkingDirectory": str(job.workdir),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", **job.env, HOME_ENV: str(job.workdir)},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_S,
        "ExitTimeOut": EXIT_TIMEOUT_S,
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def job_env(environ: dict[str, str] | None = None,
            which: Callable[[str], str | None] = shutil.which) -> dict[str, str]:  # fmt: skip
    environ = dict(os.environ) if environ is None else environ
    env = {"PATH": agent_path(which)}
    env.update({k: environ[k] for k in KEPT_ENV if environ.get(k)})
    return env


def _write_plist(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(plistlib.dumps(data))
    os.chmod(tmp, 0o644)  # launchd refuses an agent others can write to
    os.replace(tmp, path)


def _rotate_log(log: Path) -> None:
    try:
        if log.stat().st_size > LOG_ROTATE_BYTES:
            log.replace(log.with_name(log.name + ".1"))
    except FileNotFoundError:
        pass


def _wait(check: Callable[[], bool], timeout: float, sleep: Callable[[float], None]) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        sleep(0.2)


def install(job: Job, launchd: Launchd, home: Path | None = None, *,
            sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:  # fmt: skip
    """Write the agent's plist and (re)load it; a loaded one is stopped first. Returns status."""
    path = plist_path(home)
    if launchd.print_service(LABEL) is not None:
        launchd.bootout(LABEL)
        if not _wait(lambda: launchd.print_service(LABEL) is None, 40, sleep):
            raise ServiceError("the running service didn't stop within 40 s; try again")
    job.log_path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_log(job.log_path)
    _write_plist(path, build_plist(job))
    launchd.enable(LABEL)
    try:
        launchd.bootstrap(path)
    except ServiceError:
        # launchd sometimes still holds the job it just booted out ("Input/output error").
        sleep(1.0)
        launchd.bootstrap(path)
    _wait(lambda: status(launchd, home)["pid"] is not None, 3, sleep)
    return status(launchd, home)


def uninstall(launchd: Launchd, home: Path | None = None) -> dict[str, bool]:
    """Stop the agent and remove its plist. The logs stay."""
    path = plist_path(home)
    loaded = launchd.print_service(LABEL) is not None
    if loaded:
        launchd.bootout(LABEL)
    existed = path.exists()
    path.unlink(missing_ok=True)
    return {"was_loaded": loaded, "removed": existed}


# -- status ----------------------------------------------------------------------------------


def parse_print(text: str) -> dict[str, Any]:
    """The job's own fields from ``launchctl print``: state, pid, runs and the last exit.

    Only lines indented by exactly one tab belong to the job; nested blocks (endpoints,
    resource coalitions) have their own ``state`` lines.
    """
    fields = dict(re.findall(r"^\t([a-z][a-z ]*?) = (.*)$", text, re.M))
    pid = fields.get("pid", "")
    runs = fields.get("runs", "")
    last = fields.get("last exit code")
    return {
        "state": fields.get("state"),
        "pid": int(pid) if pid.isdigit() else None,
        "runs": int(runs) if runs.isdigit() else None,
        "last_exit": None if last in (None, "(never exited)") else last,
        "last_signal": fields.get("last terminating signal"),
    }


def read_plist(home: Path | None = None) -> dict[str, Any] | None:
    try:
        with plist_path(home).open("rb") as f:
            data = plistlib.load(f)
    except (FileNotFoundError, plistlib.InvalidFileException, ValueError):
        return None
    return data if isinstance(data, dict) else None


def serve_args_of(plist: dict[str, Any] | None) -> list[str] | None:
    """The serve options an installed agent runs with (what follows ``serve``)."""
    args = (plist or {}).get("ProgramArguments") or []
    for i in range(len(args) - 1):
        if args[i] == "wavescribe" and args[i + 1] == "serve":
            return [str(a) for a in args[i + 2 :]]
    return None


def status(launchd: Launchd, home: Path | None = None) -> dict[str, Any]:
    path = plist_path(home)
    plist = read_plist(home)
    printed = launchd.print_service(LABEL)
    job = parse_print(printed) if printed else {}
    workdir = (plist or {}).get("WorkingDirectory")
    log = (plist or {}).get("StandardErrorPath")
    log_bytes = None
    if log:
        with contextlib.suppress(OSError):
            log_bytes = Path(log).stat().st_size
    return {
        "label": LABEL,
        "plist": str(path),
        "installed": path.exists(),
        "loaded": printed is not None,
        "running": job.get("pid") is not None,
        "pid": job.get("pid"),
        "state": job.get("state"),
        "runs": job.get("runs"),
        "last_exit": job.get("last_exit"),
        "last_signal": job.get("last_signal"),
        "workdir": workdir,
        "serve_args": serve_args_of(plist),
        "keep_awake": bool(plist) and (plist.get("ProgramArguments") or [None])[0] == CAFFEINATE,
        "log": log,
        "log_bytes": log_bytes,
        "full_log": str(Path(workdir) / LOG_DIR / "wavescribe.jsonl") if workdir else None,
    }


# -- serve's options -------------------------------------------------------------------------


def serve_argv(serve: argparse.ArgumentParser, ns: argparse.Namespace) -> list[str]:
    """``serve``'s options in ``ns`` back as a command line: only those that differ from their
    defaults, in their long form, positionals last."""
    out: list[str] = []
    positionals: list[str] = []
    for action in serve._actions:
        if isinstance(action, argparse._HelpAction) or action.dest in (argparse.SUPPRESS, "home"):
            continue  # the home goes in the plist by itself (build_plist)
        value = getattr(ns, action.dest, action.default)
        if not action.option_strings:
            positionals += [str(v) for v in value or []]
            continue
        if value == action.default:
            continue
        flag = max(action.option_strings, key=len)
        if action.nargs == 0:  # store_true and store_false
            out.append(flag)
        elif action.nargs is None or action.nargs == "?":
            out += [flag, str(value)]
        else:
            out += [flag, *(str(v) for v in value)]
    return out + positionals


def check_serve_args(args: Sequence[str]) -> argparse.Namespace:
    """Parse a serve command line with serve's own parser: mistakes show at install, not in a
    restart loop."""
    from wavescribe.cli import build_parser

    return build_parser().parse_args(["serve", *args])


def preflight(ns: argparse.Namespace, workdir: Path,
              port_probe: Callable[[str, int], None] | None) -> None:  # fmt: skip
    """What would stop serve straight away, checked before anything is installed."""
    from wavescribe import home
    from wavescribe.alerts import (
        DEFAULT_KEYWORDS_DIR,
        merge_page,
        parse_alerts,
        read_alerts,
        resolve_alerts_path,
    )
    from wavescribe.config import ConfigError, load_stations, resolve_stations_path
    from wavescribe.settings import Settings

    named = home.current().named  # by --home or WAVESCRIBE_HOME: a new home is a home too
    if (
        not named
        and not (workdir / "config").is_dir()
        and not (workdir / "pyproject.toml").is_file()
    ):
        raise ServiceError(f"{workdir} isn't a Wavescribe folder (no config/): run "
                           f"`{typed('service install')}` in the folder you run it in, or give "
                           "that folder with --home")  # fmt: skip
    if ns.parent_pid is not None:
        raise ServiceError("--parent-pid doesn't apply to a background service: it runs until "
                           f"`{typed('service uninstall')}`")  # fmt: skip
    if not isinstance(ns.port, int):
        raise ServiceError(f"--port {ns.port} doesn't apply to a background service: give it a "
                           "fixed --port, so its page keeps one address")  # fmt: skip
    if ns.open:
        raise ServiceError("--open doesn't apply to a background service: open the page "
                           f"yourself at http://localhost:{ns.port}")  # fmt: skip
    try:
        base = load_stations(resolve_stations_path(ns.config))
        settings = Settings(ns.settings)
        for sid in ns.stations:
            base.get(sid)
        path = None if ns.no_alerts else resolve_alerts_path(ns.alerts)
        if path is not None:
            raw, rules, disabled = merge_page(read_alerts(path), settings.get("alerts") or {})
            parse_alerts(raw, path, DEFAULT_KEYWORDS_DIR, rules, disabled)
    except (ConfigError, ValueError) as e:
        raise ServiceError(f"serve would stop at start: {e}") from e
    if port_probe is not None:
        holder = home.running()  # a serve on this home would stop the service at every start
        if holder is not None:
            raise ServiceError(home.in_use(holder, installing=True))
        from wavescribe.doctor import port_taken
        from wavescribe.web.server import PortInUseError

        host = "0.0.0.0" if ns.lan else ns.host
        try:
            port_probe(host, ns.port)
        except PortInUseError as e:
            raise ServiceError(port_taken(ns.port, host, installing=True)) from e


# -- the command -----------------------------------------------------------------------------


def _short(path: str | Path | None, home: Path | None = None) -> str:
    if path is None:
        return "-"
    text, h = str(path), str(home or Path.home())
    return "~" + text[len(h) :] if text.startswith(h + "/") else text


def describe(st: dict[str, Any], home: Path | None = None) -> list[str]:
    """``service status`` for people."""
    lines = [f"Wavescribe service ({st['label']})"]
    if not st["installed"] and not st["loaded"]:
        return [*lines, f"  not installed: `{typed('service install')}` runs `serve` in the "
                "background, starts it at login and restarts it after a crash"]  # fmt: skip
    lines.append(
        f"  plist:     {_short(st['plist'], home)}"
        + ("" if st["installed"] else " (missing, but the job is still loaded)")
    )
    if not st["loaded"]:
        lines.append(f"  loaded:    no (run `{typed('service install')}` to load it again)")
    elif st["running"]:
        runs = f", started {st['runs']} times since it was loaded" if (st["runs"] or 0) > 1 else ""
        lines.append(f"  loaded:    yes, running (pid {st['pid']}){runs}")
    else:
        again = (f"it exited cleanly, so launchd leaves it stopped; `{typed('service install')}` "
                 "starts it again") if st["last_exit"] == "0" else (
                 "launchd tries again within 30 s; the log says why it stopped")  # fmt: skip
        lines.append(f"  loaded:    yes, NOT running: {again}")
    if st["last_exit"] or st["last_signal"]:
        exit_ = st["last_exit"] or "-"
        sig = f", signal {st['last_signal']}" if st["last_signal"] else ""
        lines.append(f"  last exit: {exit_}{sig}")
    if st["serve_args"] is not None:
        lines.append(f"  runs:      serve {' '.join(st['serve_args'])}".rstrip())
        lines.append(f"  home:      {_short(st['workdir'], home)}")
        if st["keep_awake"]:
            lines.append("  awake:     caffeinate -i keeps the Mac from idle sleep while it "
                         "runs (a closed lid still sleeps a MacBook)")  # fmt: skip
    if st["log"]:
        size = f" ({st['log_bytes'] / 1024**2:.1f} MB)" if st["log_bytes"] else ""
        lines.append(f"  logs:      {_short(st['log'], home)}{size}: start-up, warnings, crashes")
        lines.append(f"             {_short(st['full_log'], home)}: everything (JSON lines)")
    return lines


def port_probe(host: str, port: int) -> None:
    """Serve's own check that no other program holds the port (raises PortInUseError)."""
    from wavescribe.web.server import check_port

    check_port(host, port)


def cmd_install(args: argparse.Namespace) -> int:
    from wavescribe import home

    workdir = home.current().path
    serve_args = serve_argv(args.serve_parser, args)
    try:
        ns = check_serve_args(serve_args)
    except SystemExit:  # argparse has printed what's wrong
        return 2
    job = Job(workdir=workdir, python=sys.executable, serve_args=tuple(serve_args),
              env=job_env(), keep_awake=Path(CAFFEINATE).exists())  # fmt: skip
    try:
        if args.dry_run:
            preflight(ns, workdir, None)
            print(f"# would write {plist_path()} and load it", file=sys.stderr)
            sys.stdout.write(plistlib.dumps(build_plist(job)).decode())
            return 0
        launchd = default_launchd()
        # The installed service holds its own port; any other holder would make the new one
        # fail at every start.
        loaded = launchd.print_service(LABEL) is not None
        preflight(ns, workdir, None if loaded else port_probe)
        st = install(job, launchd)
    except ServiceError as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    print(f"Installed and {'started' if st['running'] else 'loaded'} the Wavescribe service. "
          "It starts again at login and after a crash.")  # fmt: skip
    for line in describe(st)[1:]:
        print(line)
    if not st["running"]:
        print(f"It isn't running yet: see {_short(st['log'])}", file=sys.stderr)
    where = "localhost" if not ns.lan else "<this Mac's name>.local"
    print(f"The page: http://{where}:{ns.port}. `{typed('service status')}` shows how it's "
          f"doing; `{typed('doctor')}` checks what it needs.")  # fmt: skip
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    try:
        done = uninstall(default_launchd())
    except ServiceError as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    if not done["was_loaded"] and not done["removed"]:
        print("The Wavescribe service isn't installed: nothing to do.")
        return 0
    what = "Stopped and removed" if done["was_loaded"] else "Removed"
    print(f"{what} the Wavescribe service. Its logs stay in data/logs/.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        st = status(default_launchd())
    except ServiceError as e:
        print(f"wavescribe: {e}", file=sys.stderr)
        return 1
    if args.json:
        from wavescribe.events import to_json

        print(to_json(st))
    else:
        print("\n".join(describe(st)))
    return 0 if st["running"] else 3  # like an init script's status: 3 is "not running"


def add_parser(sub: Any) -> None:
    """``service install|uninstall|status``. ``install`` takes every ``serve`` option, copied
    from serve's parser, which must already be on ``sub``."""
    serve = sub.choices["serve"]
    service = sub.add_parser("service", help="run `serve` in the background (macOS launchd)")
    ssub = service.add_subparsers(dest="service_command", metavar="ACTION", required=True)
    p = ssub.add_parser(
        "install", parents=[serve], conflict_handler="resolve",
        help="start `serve` now and at every login, restarted after a crash; takes serve's "
        "options (run it from the Wavescribe folder, or give its home with --home)",
        description="Install a launchd LaunchAgent that runs `wavescribe serve` with these "
        "options in this home, keeps the Mac from idle sleep while it runs, restarts it "
        "after a crash and starts it at login. Run it again to change the options.",
    )  # fmt: skip
    p.add_argument("--dry-run", action="store_true",
                   help="print the LaunchAgent plist instead of installing it")  # fmt: skip
    p.set_defaults(func=cmd_install, serve_parser=serve)
    p = ssub.add_parser("uninstall", help="stop the background service and remove it")
    p.set_defaults(func=cmd_uninstall)
    from wavescribe.cli import add_json_arg  # cli is loaded: it is building its parser

    p = ssub.add_parser("status", help="installed? running? its pid, last exit and logs")
    add_json_arg(p)
    p.set_defaults(func=cmd_status)
