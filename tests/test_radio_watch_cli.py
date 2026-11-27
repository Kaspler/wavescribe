# SPDX-License-Identifier: Apache-2.0
"""`wavescribe radio watch` and its receiver: refused at once when another program holds it,
never "0 caught" when it never listened, a line when the receiver stops sending, and no serial
on the terminal. A stand-in rtl_sdr (tests/fake_rtl_sdr.py) is the only one on the PATH; no
real receiver is opened, and no model is loaded."""

from __future__ import annotations

import logging
import re
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from wavescribe import cli
from wavescribe.cli import main
from wavescribe.radio import holders, watch_cli
from wavescribe.sources.sdr import forget_device

SERIAL = "00001234"
STATIONS = (
    f'[[device]]\nid = "a"\nserial = "{SERIAL}"\ncenter_mhz = 446.0\ngain_db = 30\n\n'
    '[[station]]\nid = "news"\nlanguage = "es-ES"\nstream = "http://example.test/news"\n'
)
SERVE_HOLDS = {"pid": 4001, "program": "rtl_sdr", "other_wavescribe": True,
               "wavescribe_pid": 4000, "serve": True}  # fmt: skip
HALF_A_SECOND = 2 * 2_400_000 // 2  # bytes of IQ
CHECK_MODELS = watch_cli.check_models  # the fixture puts a stand-in in its place


def listen(args) -> int:
    """What `listen` does with a watch, without the models: the console as it sets it up, and
    the watch's source read until it ends or the time is up."""
    from wavescribe.log import setup_logging
    from wavescribe.sources.watch import WatchSource

    setup_logging(None)
    [station] = args.tuned_stations
    source = WatchSource(station.id, station.sdr, privacy=station.privacy)
    reading = threading.Thread(target=lambda: [None for _ in source], daemon=True)
    reading.start()
    reading.join(args.duration or 10)
    source.close()
    reading.join(5)
    return 0


@pytest.fixture
def watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The arguments of a PMR446 watch on receiver "a", the stand-in rtl_sdr and the file
    each of its starts is logged to."""
    exe = tmp_path / "bin" / "rtl_sdr"
    exe.parent.mkdir()
    fake = Path(__file__).with_name("fake_rtl_sdr.py")
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(exe.parent))  # no other rtl tool can be reached
    monkeypatch.setenv("FAKE_RTL_SPAWNS", str(tmp_path / "spawns.txt"))
    monkeypatch.setenv("FAKE_RTL_STATIONS", "-900000:1000")  # a carrier outside the window
    monkeypatch.chdir(tmp_path)
    (tmp_path / "stations.toml").write_text(STATIONS)
    monkeypatch.setattr(holders, "held_elsewhere", dict)  # the registry tells nothing
    monkeypatch.setattr(watch_cli, "check_models", lambda *_a: None)
    monkeypatch.setattr(cli, "cmd_listen", listen)
    root = logging.getLogger("wavescribe")
    kept = root.handlers[:], root.propagate, root.level
    yield [
        "radio", "watch", "a", "pmr446", "--acknowledge", "--region", "cept",
        "--config", str(tmp_path / "stations.toml"), "--no-store", "--no-alerts",
    ], tmp_path / "spawns.txt"  # fmt: skip
    root.handlers[:], root.propagate = kept[0], kept[1]
    root.setLevel(kept[2])
    forget_device(SERIAL)


def starts(spawns: Path) -> int:
    return len(spawns.read_text().splitlines()) if spawns.exists() else 0


def test_a_receiver_another_program_holds_is_refused_before_anything_starts(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, spawns = watch
    monkeypatch.setattr(holders, "held_elsewhere", lambda: {SERIAL: SERVE_HOLDS})
    monkeypatch.setattr(cli, "cmd_listen", lambda _a: pytest.fail("it went on to listen"))
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "wavescribe: receiver A is busy: Wavescribe itself has it open (`wavescribe serve`, "
        "process 4000). Stop serve, or use another receiver"
    ]
    assert starts(spawns) == 0


def test_a_receiver_rtl_sdr_finds_busy_ends_the_watch_at_once_with_no_count(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, spawns = watch
    monkeypatch.setenv("FAKE_RTL_BUSY", "1")
    t0 = time.monotonic()
    assert main([*argv, "--minutes", "1"]) == 1
    assert time.monotonic() - t0 < 10  # not the minute asked for
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        "wavescribe: receiver A is busy: another program has it open (`wavescribe serve`, an "
        "SDR program, or another rtl_ tool). Close it, or use another receiver; if it is "
        "serve, stop it"
    )
    assert "with speech" not in err and SERIAL not in err
    assert starts(spawns) == 1  # tried once, not again and again


def test_a_receiver_that_gives_nothing_says_what_rtl_sdr_said(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, spawns = watch
    monkeypatch.setenv("FAKE_RTL_MISSING", "1")
    assert main([*argv, "--minutes", "1"]) == 1
    err = capsys.readouterr().err
    assert err.splitlines()[-1] == (
        "wavescribe: receiver A sent nothing, so nothing was watched: No matching devices found."
    )
    assert "with speech" not in err and starts(spawns) == 1


def test_a_receiver_that_stops_sending_and_doesnt_come_back_is_said_and_fails(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, spawns = watch
    monkeypatch.setenv("FAKE_RTL_MAX_BYTES", str(HALF_A_SECOND))
    monkeypatch.setenv("FAKE_RTL_THEN", "missing")
    assert main([*argv, "--minutes", "0.06"]) == 1
    err = capsys.readouterr().err
    assert "] receiver A stopped sending; trying again" in err
    last = err.splitlines()[-1]
    assert last.startswith("# 0 caught, 0 with speech; receiver A stopped sending at ")
    assert last.endswith(" and wasn't heard after that")
    assert SERIAL not in err and starts(spawns) >= 2


def test_a_receiver_that_stops_sending_for_a_while_is_said_and_fails(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, _ = watch
    monkeypatch.setenv("FAKE_RTL_MAX_BYTES", str(HALF_A_SECOND))
    monkeypatch.setenv("FAKE_RTL_THEN", "stream")
    assert main([*argv, "--minutes", "0.06"]) == 1
    err = capsys.readouterr().err
    assert "] receiver A stopped sending; trying again" in err
    # A second or two: how long the gap is seen as depends on how busy the machine is.
    again = re.search(r"\] receiver A is sending again, after ([1-3]) s", err)
    assert again, err
    assert err.splitlines()[-1] == (
        f"# 0 caught, 0 with speech; receiver A wasn't heard for {again[1]} s"
    )


@pytest.mark.parametrize("value", ["-3", "nan", "inf", "soon"])
def test_minutes_are_checked_before_anything_else(
    value: str, watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, spawns = watch
    monkeypatch.setattr(holders, "held_elsewhere", lambda: pytest.fail("looked at the receiver"))
    with pytest.raises(SystemExit) as e:
        main([*argv, "--minutes", value])
    assert e.value.code == 2
    err = capsys.readouterr().err
    assert f"argument --minutes: give minutes from 0 up (0: until Ctrl-C), not '{value}'" in err
    assert "# watching" not in err and starts(spawns) == 0


def test_zero_minutes_is_until_ctrl_c_as_the_help_says(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.test_radio_watch_log import heard

    argv, _ = watch
    asked: list[float | None] = []
    monkeypatch.setattr(cli, "cmd_listen", lambda a: asked.append(a.duration) or heard(a))
    assert main([*argv, "--minutes", "0"]) == 0
    assert asked == [None]  # no deadline
    with pytest.raises(SystemExit):
        main(["radio", "watch", "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "0: until Ctrl-C" in shown
    assert "Weak, very short or overlapping transmissions can be missed" in shown  # honest


class NoSpeechModel:
    """A registry with a speech detector and no speech model for the language asked for."""

    def resolve(self, capability: str, language: str | None = None) -> None:
        from wavescribe.registry import ModelError

        if capability == "asr":
            raise ModelError(f"no asr model in the registry supports {language!r}")


def test_a_language_no_model_knows_is_refused_before_the_receiver_is_looked_at(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from wavescribe.registry import ModelRegistry

    argv, spawns = watch
    monkeypatch.setattr(watch_cli, "check_models", CHECK_MODELS)  # the real one
    monkeypatch.setattr(ModelRegistry, "load", classmethod(lambda *_a, **_k: NoSpeechModel()))
    monkeypatch.setattr(holders, "held_elsewhere", lambda: pytest.fail("looked at the receiver"))
    assert main([*argv, "--language", "xx-notalang"]) == 1
    assert capsys.readouterr().err.splitlines() == [
        "wavescribe: no asr model in the registry supports 'xx-notalang'"
    ]
    assert starts(spawns) == 0


def test_a_missing_speech_detector_is_said_before_the_watch_starts(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, spawns = watch
    monkeypatch.setattr(watch_cli, "check_models", CHECK_MODELS)  # no models in this folder
    assert main([*argv, "--language", "es-ES"]) == 1
    [line] = capsys.readouterr().err.splitlines()
    assert line.startswith("wavescribe: transcribing needs the speech detector (Silero VAD)")
    assert starts(spawns) == 0


def test_the_terminal_names_a_receiver_by_its_id_never_by_its_serial() -> None:
    """Serials stay in the log file, `doctor` and Settings → Receivers (docs/RADIO.md)."""
    from wavescribe.log import ConsoleFormatter, JsonFormatter

    def record(msg: str, **fields: object) -> logging.LogRecord:
        r = logging.LogRecord("wavescribe.sources.sdr", logging.WARNING, "", 0, msg, (), None)
        r.fields = fields
        return r

    said = record("rtl_sdr", device="a", serial=SERIAL, stderr=f"0:  Blog V4, SN: {SERIAL}")
    shown = ConsoleFormatter().format(said)
    assert "device=a" in shown and SERIAL not in shown and "SN: …1234" in shown
    alone = ConsoleFormatter().format(record("sdr_serial_shared", serial=SERIAL))
    assert "serial=…1234" in alone and SERIAL not in alone  # no receiver to name it by
    assert SERIAL in JsonFormatter().format(said)  # the log keeps it


def test_a_receiver_that_sends_all_along_counts_what_was_caught(
    watch, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv, _ = watch
    assert main([*argv, "--minutes", "0.02"]) == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("# watching PMR446")
    assert err.splitlines()[-1] == "# 0 caught, 0 with speech"
