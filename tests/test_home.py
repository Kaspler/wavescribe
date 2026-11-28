# SPDX-License-Identifier: Apache-2.0
"""The home: --home, then WAVESCRIBE_HOME, then the folder a command starts in; the paths given
made absolute before the change into it; the examples that come with Wavescribe for a home
with no config/; and one station runner per home, under data/wavescribe.lock."""

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from wavescribe import cli, home
from wavescribe.cli import build_parser, main

ALERTS = '[[rule]]\nid = "bridge"\nseverity = "high"\n[rule.terms]\nes = ["puente cerrado"]\n'


@pytest.fixture
def start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The folder a command is started in: not the home in these tests."""
    folder = tmp_path / "start"
    folder.mkdir()
    monkeypatch.chdir(folder)
    return folder


def every_parser(parser: argparse.ArgumentParser) -> list[argparse.ArgumentParser]:
    out = [parser]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for child in action.choices.values():
                out += every_parser(child)
    return out


# -- paths given, and where the home is --------------------------------------------------------


def test_every_path_option_is_made_absolute_against_the_folder_it_was_typed_in(
    start: Path,
) -> None:
    for parser in every_parser(build_parser()):
        for action in parser._actions:  # a new path option must use given_path, not Path
            assert action.type is not Path, f"{parser.prog} {action.option_strings}"
    ns = build_parser().parse_args(["serve", "--config", "mine.toml", "--settings", "ui.toml",
                                    "--alerts", "../rules.toml", "--home", "h"])  # fmt: skip
    assert ns.config == start / "mine.toml" and ns.settings == start / "ui.toml"
    assert ns.alerts == start.parent / "rules.toml" and ns.home == start / "h"
    assert build_parser().parse_args(["serve"]).settings == Path("config/ui.toml")  # the home's
    assert build_parser().parse_args(["bakeoff", "a.wav", "--models", "m", "--language",
                                      "es"]).clips == [start / "a.wav"]  # fmt: skip
    assert build_parser().parse_args(["follow", "--resume", "r"]).resume == start / "r"
    listen = ["listen", "uno", "--source"]
    assert build_parser().parse_args([*listen, "clip.wav"]).source == str(start / "clip.wav")
    for address in ("https://radio.test/a.mp3", "sdr:00000001/99.0?gain=20"):
        assert build_parser().parse_args([*listen, address]).source == address


def test_a_relative_path_given_is_read_from_where_it_was_typed_not_from_the_home(
    start: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (start / "rules.toml").write_text(ALERTS, encoding="utf-8")
    argv = ["alerts", "check", "--alerts", "rules.toml", "--language", "es", "puente cerrado"]
    assert main([*argv, "--home", "../h"]) == 0
    assert "bridge" in capsys.readouterr().out
    assert Path.cwd() == (start.parent / "h").resolve()  # it ran in the home all the same
    assert not (start.parent / "h" / "rules.toml").exists()


def test_the_flag_then_the_environment_then_the_folder_it_started_in(
    start: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag, env = start.parent / "flag", start.parent / "env"
    assert home.choose(flag, {home.ENV: str(env)}, start) == home.Home(flag, "flag")
    assert home.choose(None, {home.ENV: "../env"}, start) == home.Home(start / "../env", "env")
    assert home.choose(None, {home.ENV: ""}, start) == home.Home(start, "folder")

    def where(*argv: str) -> Path:
        main(["search", "anything", *argv])  # a short command: nothing stored yet, exit 1
        here = Path.cwd()
        os.chdir(start)
        return here

    monkeypatch.setenv(home.ENV, "../env")  # relative: from the folder it starts in
    assert where() == env.resolve() and env.is_dir()  # made when missing
    assert os.environ[home.ENV] == str(env.resolve())  # for the programs it starts
    monkeypatch.setenv(home.ENV, "../env")
    assert where("--home", str(flag)) == flag.resolve()
    assert os.environ[home.ENV] == str(flag.resolve())
    monkeypatch.delenv(home.ENV)
    assert where() == start.resolve()
    assert home.current() == home.Home(start.resolve(), "folder")


def test_home_goes_before_or_after_the_command(start: Path) -> None:
    for argv in (["--home", "h", "search", "x"], ["search", "x", "--home", "h"],
                 ["--home", "elsewhere", "search", "--home", "h", "x"]):  # fmt: skip
        assert build_parser().parse_args(argv).home == start / "h"
    assert build_parser().parse_args(["search", "x"]).home is None


def test_relative_paths_in_the_environment_are_made_absolute_before_the_change(
    start: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WAVESCRIBE_MODELS_DIR", "models-here")
    monkeypatch.setenv("HF_HOME", "~/hf")  # the library expands it
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/cert.pem")
    home.enter(start.parent / "h")
    made = Path(os.environ["WAVESCRIBE_MODELS_DIR"])
    assert made.is_absolute() and made.resolve() == (start / "models-here").resolve()
    assert os.environ["HF_HOME"] == "~/hf" and os.environ["SSL_CERT_FILE"] == "/etc/cert.pem"


def test_a_home_that_is_a_file_is_refused(start: Path, capsys) -> None:
    (start / "afile").write_text("")
    assert main(["search", "x", "--home", "afile"]) == 2
    assert capsys.readouterr().err == f"wavescribe: the home {start / 'afile'} isn't a folder\n"


def test_how_a_command_is_typed_does_not_follow_the_folder(start: Path) -> None:
    from wavescribe import invocation

    invocation.invocation.cache_clear()
    before = invocation.invocation()
    home.enter(start.parent / "h")
    invocation.invocation.cache_clear()
    assert invocation.invocation() == before  # read from where the package is, not the folder


# -- the examples that come with Wavescribe ---------------------------------------------------


def test_a_home_without_config_runs_on_the_examples_that_come_with_wavescribe(
    start: Path,
) -> None:
    from wavescribe.alerts import load_alerts, resolve_alerts_path
    from wavescribe.config import load_stations, resolve_stations_path

    home.enter(start.parent / "new")
    assert not Path("config").exists()
    stations = resolve_stations_path()
    assert stations.is_absolute() and stations.name == "stations.example.toml"
    assert load_stations(stations).stations
    alerts = resolve_alerts_path()
    assert alerts is not None and alerts.is_absolute()
    assert "pt" in load_alerts(alerts).rules.packs  # its keyword packs come along

    own = Path("config/stations.example.toml")  # the home's own example wins
    own.parent.mkdir()
    own.write_text('[[station]]\nid = "mine"\nlanguage = "es"\nstream = "https://r.test/a"\n')
    assert resolve_stations_path() == own


def test_an_installed_package_reads_its_examples_from_wavescribe_examples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "site-packages" / "wavescribe"  # as a wheel lays it out
    (package / "examples" / "keywords").mkdir(parents=True)
    (package / "examples" / "stations.example.toml").write_text("")
    (package / "examples" / "keywords" / "pt.example.toml").write_text("")
    monkeypatch.setattr(home, "__file__", str(package / "home.py"))
    monkeypatch.chdir(tmp_path)  # a home with no config/
    assert home.example("stations.example.toml") == package / "examples" / "stations.example.toml"
    assert home.packaged("keywords") == package / "examples" / "keywords"
    assert home.example("alerts.example.toml") is None  # not in this package


def test_the_wheel_carries_every_tracked_example() -> None:
    import tomllib

    shipped = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    mapped = shipped["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    tracked = subprocess.run(["git", "ls-files", "config"], capture_output=True, text=True,
                             check=True).stdout.split()  # fmt: skip
    assert sorted(mapped) == sorted(tracked)
    for source, target in mapped.items():
        assert target == "wavescribe/examples/" + source.removeprefix("config/")


# -- one station runner per home ---------------------------------------------------------------


def lock_file(folder: Path) -> Path:
    return folder / "data" / "wavescribe.lock"


def test_a_second_serve_on_the_same_home_stops_at_once_with_code_3(
    start: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = start.parent / "h"
    h.mkdir()
    os.chdir(h)
    held = home.hold("serve")  # a descriptor of its own: as another process would hold it
    held.note(url="http://localhost:8791/")
    os.chdir(start)
    argv = ["serve", "--home", str(h), "--port", "8792", "--no-store", "--no-alerts"]
    assert main(argv) == 3
    assert capsys.readouterr().err == (
        f"wavescribe: Wavescribe is already running with this home (pid {os.getpid()}, at "
        "http://localhost:8791/); use its page, stop it first, or give this one another --home\n"
    )
    assert not (h / "data" / "logs").exists()  # it stopped before anything else
    del held.info["url"]  # a listen holds it: no page
    held.note(command="listen")
    os.chdir(start)
    assert main(["radio", "tune", "a", "99.0", "--language", "es", "--home", str(h)]) == 3
    assert capsys.readouterr().err == (
        f"wavescribe: Wavescribe is already running with this home (pid {os.getpid()}, "
        "listen); stop it first, or give this one another --home\n"
    )
    held.release()
    assert lock_file(h).read_text() == ""


def test_the_lock_is_held_while_serve_runs_and_let_go_when_it_returns(
    start: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []

    @home.holds("serve")
    def serve(args: argparse.Namespace) -> int:
        home.note_page("http://localhost:8793/")
        seen.append(home.running())
        return 0

    monkeypatch.setattr(cli, "cmd_serve", serve)
    h = start.parent / "h"
    assert main(["serve", "--home", str(h)]) == 0
    assert seen == [{"pid": os.getpid(), "command": "serve", "home": str(h.resolve()),
                     "url": "http://localhost:8793/"}]  # fmt: skip
    assert home.running() is None and lock_file(h).read_text() == ""


def test_short_commands_work_while_the_home_is_held(start: Path, capsys) -> None:
    (start / "config").mkdir()
    (start / "config" / "alerts.toml").write_text(ALERTS, encoding="utf-8")
    held = home.hold("serve")
    try:
        assert main(["alerts", "check", "--language", "es", "el puente cerrado"]) == 0
        assert main(["search", "x"]) == 1  # nothing stored here: not refused
        assert main(["radio", "bands"]) == 0
        err = capsys.readouterr().err
        assert "already running" not in err and "nothing stored yet" in err
    finally:
        held.release()


def test_a_lock_left_by_a_killed_process_blocks_nothing(start: Path, capsys) -> None:
    h = start.parent / "h"
    h.mkdir()
    code = ("import os, sys, time\nfrom wavescribe import home\nos.chdir(sys.argv[1])\n"
            "home.hold('serve').note(url='http://localhost:8794/')\nprint('held', flush=True)\n"
            "time.sleep(60)\n")  # fmt: skip
    child = subprocess.Popen([sys.executable, "-c", code, str(h)], stdout=subprocess.PIPE,
                             text=True)  # fmt: skip
    try:
        assert child.stdout is not None and child.stdout.readline() == "held\n"
        assert main(["serve", "--home", str(h), "--port", "8795"]) == 3
        assert f"(pid {child.pid}, at http://localhost:8794/)" in capsys.readouterr().err
        assert home.running() == {
            "pid": child.pid,
            "command": "serve",
            "home": str(h.resolve()),
            "url": "http://localhost:8794/",
        }
    finally:
        child.send_signal(signal.SIGKILL)  # a crash: no clean-up runs
        child.wait()
    assert json.loads(lock_file(h).read_text())["pid"] == child.pid  # the file is still there
    assert home.running() is None
    home.hold("serve").release()  # and the next one takes it
