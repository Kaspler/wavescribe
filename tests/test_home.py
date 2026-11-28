# SPDX-License-Identifier: Apache-2.0
"""The home: --home, then WAVESCRIBE_HOME, then the folder a command starts in; the paths given
made absolute before the change into it; and the examples that come with Wavescribe for a home
with no config/."""

import argparse
import os
import subprocess
from pathlib import Path

import pytest

from wavescribe import home
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
