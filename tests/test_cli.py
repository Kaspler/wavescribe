# SPDX-License-Identifier: Apache-2.0
import pytest

from wavescribe import __version__
from wavescribe.cli import main


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: wavescribe" in capsys.readouterr().out


def test_signals_stop_the_process_even_when_sigint_was_inherited_ignored() -> None:
    import signal
    import subprocess
    import sys
    import time

    code = (
        "import signal, sys, time\n"
        "from wavescribe.cli import install_stop_signals\n"
        "install_stop_signals()\n"
        "print('ready', flush=True)\n"
        "try:\n"
        "    time.sleep(30)\n"
        "except KeyboardInterrupt:\n"
        "    print('interrupted', flush=True)\n"
    )
    for sig, expect in ((signal.SIGINT, "interrupted"), (signal.SIGTERM, None)):
        # Start it the way a background job from a non-interactive shell starts: SIGINT ignored.
        def ignore_sigint() -> None:
            signal.signal(signal.SIGINT, signal.SIG_IGN)

        cmd = [sys.executable, "-c", code]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, preexec_fn=ignore_sigint)
        assert p.stdout.readline().strip() == "ready"
        t0 = time.monotonic()
        p.send_signal(sig)
        out, _ = p.communicate(timeout=10)
        assert time.monotonic() - t0 < 5
        if expect:
            assert expect in out and p.returncode == 0
        else:
            assert p.returncode == 143


def test_listen_explains_stations_it_cannot_transcribe(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)  # settings and data are looked up from the working directory
    monkeypatch.setattr("wavescribe.log.setup_logging", lambda *_a, **_k: None)
    stations = tmp_path / "stations.toml"
    stations.write_text(
        '[[station]]\nid = "far"\nlanguage = "pt-PT"\n'
        'remote = "http://attic.local:8765"\nremote_station = "antena1"\n'
        '[[station]]\nid = "music"\nlanguage = "pt-PT"\n'
        'stream = "https://example.org/a.mp3"\ntranscribe = false\n'
    )
    for station, why in (("far", "another Wavescribe"), ("music", "transcribe = false")):
        assert main(["listen", station, "--config", str(stations), "--no-store"]) == 1
        err = capsys.readouterr().err
        assert err.startswith(f"wavescribe: station '{station}'") and why in err
        assert "Traceback" not in err and "KeyError" not in err


@pytest.fixture
def receiver(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """A stand-in `rtl_sdr` first on the PATH and a stations file with receiver "a"; returns
    the arguments for it and the file each start of rtl_sdr is logged to."""
    import stat
    import sys
    from pathlib import Path

    exe = tmp_path / "bin" / "rtl_sdr"
    exe.parent.mkdir()
    fake = Path(__file__).with_name("fake_rtl_sdr.py")
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(exe.parent))
    monkeypatch.setenv("FAKE_RTL_SPAWNS", str(tmp_path / "spawns.txt"))
    monkeypatch.chdir(tmp_path)  # no page settings from wherever the tests run
    stations = tmp_path / "stations.toml"
    stations.write_text(
        '[defaults]\nlanguage = "pt-PT"\n\n[[device]]\nid = "a"\nserial = "00001234"\n'
        "center_mhz = 99.0\ngain_db = 20\nppm = 52\n\n"
        '[[station]]\nid = "one"\nfm_mhz = 99.0\ndevice = "a"\n'
    )
    return ["a", "99.0", "--seconds", "1", "--config", str(stations)], tmp_path / "spawns.txt"


def test_radio_rds_starts_rtl_sdr_with_the_receivers_settings(
    receiver, capsys: pytest.CaptureFixture[str]
) -> None:
    args, spawns = receiver
    assert main(["radio", "rds", *args]) == 0
    assert spawns.read_text().splitlines() == [
        "-d 00001234 -f 98700000 -s 2400000 -g 20 -p 52 -n 2400000 -"
    ]
    assert "# 0 groups" in capsys.readouterr().err  # the stand-in's station sends no RDS
    assert main(["radio", "rds", *args, "--ppm", "-3"]) == 0
    assert "-g 20 -p -3 -n" in spawns.read_text().splitlines()[-1]


@pytest.mark.parametrize(
    ("trouble", "message"),
    [
        ("FAKE_RTL_BUSY", "dongle 00001234 is busy: another program has it open"),
        ("FAKE_RTL_MISSING", "rtl_sdr gave no samples: No matching devices found."),
        ("PATH", "rtl_sdr isn't installed"),
    ],
)
def test_radio_rds_says_why_it_heard_nothing(
    receiver, trouble: str, message: str, tmp_path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    monkeypatch.setenv(trouble, str(tmp_path))  # as PATH: a folder with no rtl_sdr in it
    assert main(["radio", "rds", *receiver[0]]) == 1
    err = capsys.readouterr().err
    assert message in err and "groups" not in err


def test_listen_says_which_tool_is_missing_before_loading_models(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wavescribe.log.setup_logging", lambda *_a, **_k: None)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # neither ffmpeg nor rtl_sdr
    monkeypatch.setattr("wavescribe.pipeline.plan_models", lambda *_a, **_k: pytest.fail("loaded"))
    stations = tmp_path / "stations.toml"
    stations.write_text('[[station]]\nid = "news"\nlanguage = "es-ES"\n'
                        'stream = "https://example.org/a.mp3"\n')  # fmt: skip
    assert main(["listen", "news", "--config", str(stations), "--no-store"]) == 1
    err = capsys.readouterr().err
    assert "ffmpeg isn't installed" in err and "brew install ffmpeg" in err


def test_listen_keeps_the_store_pruned_while_nothing_is_said(tmp_path, monkeypatch) -> None:
    """Pruning on writes alone keeps nothing in check on a quiet station or overnight."""
    from pathlib import Path

    from tests.test_pipeline import ENTRY
    from wavescribe.pipeline import Plan
    from wavescribe.store import Store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("wavescribe.log.setup_logging", lambda *_a, **_k: None)
    monkeypatch.setattr("shutil.which", lambda tool: f"/usr/bin/{tool}")
    plan = Plan(vad=(ENTRY, Path()), asr={"news": (ENTRY, Path())}, mt=None)
    monkeypatch.setattr("wavescribe.pipeline.plan_models", lambda *_a, **_k: plan)

    class Pipeline:
        def __init__(self, *_a, **_k) -> None:
            pass

        def run(self, duration=None) -> None:
            pass

    monkeypatch.setattr("wavescribe.pipeline.Pipeline", Pipeline)
    pruning: list[Store] = []
    monkeypatch.setattr(Store, "keep_pruned", lambda store, *_a, **_k: pruning.append(store))
    stations = tmp_path / "stations.toml"
    stations.write_text('[[station]]\nid = "news"\nlanguage = "es-ES"\n'
                        'stream = "https://example.org/a.mp3"\n')  # fmt: skip
    args = ["listen", "news", "--config", str(stations), "--no-alerts", "--audio-days", "0"]
    assert main(args) == 0
    assert len(pruning) == 1
