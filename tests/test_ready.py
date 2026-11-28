# SPDX-License-Identifier: Apache-2.0
"""What a program that starts ``wavescribe serve`` relies on: the ready file, written whole once
the page listens and removed whenever serve stops; ``--parent-pid``, which stops serve with exit
code 0 when that process ends; and serve's exit codes.

Pages listen on fixed ports of 8790 to 8799 here: ``--port auto`` counts a port as taken while
connections an earlier test made to it are still closing, and could then move past 8799. It has
tests of its own (test_ports.py)."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from wavescribe import __version__, ready
from wavescribe.cli import main

PORT = "8796"


# -- the ready file ------------------------------------------------------------------------


def test_the_ready_file_has_its_fields_in_order_and_a_loopback_url() -> None:
    home = Path(".")
    got = ready.fields(host="0.0.0.0", port=8791, started=1_800_000_000.7, home=home, pid=42)
    assert list(got) == ["app", "version", "pid", "host", "port", "url", "home", "started"]
    assert got == {
        "app": "wavescribe",
        "version": __version__,
        "pid": 42,
        "host": "0.0.0.0",
        "port": 8791,
        "url": "http://127.0.0.1:8791/",  # under --lan too: this machine's address
        "home": str(home.resolve()),
        "started": "2027-01-15T08:00:00Z",
    }
    assert ready.fields(host="127.0.0.1", port=8792, started=0, home=home)["pid"] == os.getpid()
    assert ready.fields(host="::1", port=8792, started=0, home=home)["url"] == "http://[::1]:8792/"


def test_the_ready_file_is_written_whole_and_for_its_owner_only(tmp_path: Path,
                                                                monkeypatch) -> None:  # fmt: skip
    path = tmp_path / "data" / "serve.json"
    old = os.umask(0)  # whatever the umask
    try:
        ready.write(path, {"pid": 1, "port": 8790})
    finally:
        os.umask(old)
    assert json.loads(path.read_text()) == {"pid": 1, "port": 8790}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ready.write(path, {"pid": 2})
    assert json.loads(path.read_text()) == {"pid": 2}

    def interrupted(src: Any, dst: Any) -> None:  # the rename never happens
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", interrupted)
    with pytest.raises(OSError, match="disk full"):
        ready.write(path, {"pid": 3})
    assert json.loads(path.read_text()) == {"pid": 2}  # the old one, whole
    assert sorted(p.name for p in path.parent.iterdir()) == ["serve.json"]  # no temporary left


def test_the_ready_file_is_removed_only_while_it_is_this_process(tmp_path: Path) -> None:
    path = tmp_path / "serve.json"
    ready.write(path, {"pid": os.getpid() + 1})  # another serve's, given the same path since
    ready.remove(path)
    assert path.exists()
    ready.write(path, {"pid": os.getpid()})
    ready.remove(path)
    assert not path.exists()
    ready.remove(path)  # gone already: nothing to do


# -- following a process -------------------------------------------------------------------


def test_alive_is_false_for_a_process_that_ended_even_before_it_is_reaped() -> None:
    assert ready.alive(os.getpid())
    child = subprocess.Popen(["sleep", "30"])
    try:
        assert ready.alive(child.pid)
    finally:
        child.kill()
    deadline = time.monotonic() + 5
    while ready.alive(child.pid):  # a zombie: ended, not yet reaped
        assert time.monotonic() < deadline
        time.sleep(0.02)
    child.wait()
    assert not ready.alive(child.pid)


@pytest.mark.parametrize("kqueue", [True, False], ids=["kqueue", "polling"])
def test_the_parent_watch_calls_back_once_its_process_ends(kqueue: bool, monkeypatch) -> None:
    if kqueue and not ready.has_kqueue():
        pytest.skip("no kqueue here")
    if not kqueue:
        monkeypatch.setattr(ready, "has_kqueue", lambda: False)
    ended = threading.Event()
    child = subprocess.Popen(["sleep", "30"])
    try:
        ready.watch_parent(child.pid, ended.set, poll_s=0.1)
        assert not ended.wait(0.3)
        t0 = time.monotonic()
        child.kill()
        if not kqueue:  # serve's parent isn't serve's child: its own parent reaps it
            child.wait()
        assert ended.wait(2) and time.monotonic() - t0 < 2
    finally:
        child.kill()
        child.wait()
    again = threading.Event()
    ready.watch_parent(child.pid, again.set, poll_s=0.1)  # ended already: at once
    assert again.wait(1)


# -- serve ---------------------------------------------------------------------------------


def serve_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "WAVESCRIBE_HOME"}


def start_serve(tmp_path: Path, *extra: str, port: int = 8797) -> tuple[subprocess.Popen, Path]:
    log = (tmp_path / "serve.log").open("w")
    p = subprocess.Popen(
        [sys.executable, "-m", "wavescribe", "serve", "--home", str(tmp_path / "h"),
         "--port", str(port), "--no-store", "--no-alerts", *extra],
        stdout=log, stderr=subprocess.STDOUT, env=serve_env(),
    )  # fmt: skip
    return p, tmp_path / "serve.log"


def wait_for(path: Path, p: subprocess.Popen, log: Path, timeout: float = 60) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert p.poll() is None, log.read_text()
        assert time.monotonic() < deadline, log.read_text()
        time.sleep(0.05)
    return json.loads(path.read_text())


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_serve_writes_the_ready_file_once_it_listens_and_removes_it_when_it_stops(
    tmp_path: Path, sig: int, code: int
) -> None:
    path = tmp_path / "ready.json"
    p, log = start_serve(tmp_path, "--ready-file", str(path))
    try:
        got = wait_for(path, p, log)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert got["pid"] == p.pid and got["app"] == "wavescribe" and got["host"] == "127.0.0.1"
        assert got["port"] == 8797 and got["url"] == "http://127.0.0.1:8797/"
        assert got["home"] == str((tmp_path / "h").resolve())
        import urllib.request

        with urllib.request.urlopen(got["url"] + "healthz", timeout=5) as r:  # it listens
            assert json.loads(r.read())["version"] == __version__
    finally:
        p.send_signal(sig)
        assert p.wait(30) == code, log.read_text()
    assert not path.exists()


def test_serve_writes_the_ready_file_in_the_home_by_default(tmp_path: Path) -> None:
    path = tmp_path / "h" / "data" / "serve.json"
    p, log = start_serve(tmp_path)
    try:
        assert wait_for(path, p, log)["pid"] == p.pid
    finally:
        p.send_signal(signal.SIGTERM)
        assert p.wait(30) == 143
    assert not path.exists()


def test_serve_stops_with_code_0_within_2_s_once_its_parent_ends(tmp_path: Path) -> None:
    parent = subprocess.Popen(["sleep", "300"])
    path = tmp_path / "ready.json"
    p, log = start_serve(tmp_path, "--ready-file", str(path), "--parent-pid", str(parent.pid))
    try:
        wait_for(path, p, log)
        t0 = time.monotonic()
        parent.kill()
        assert p.wait(10) == 0, log.read_text()
        assert time.monotonic() - t0 < 2
    finally:
        parent.kill()
        parent.wait()
        if p.poll() is None:
            p.kill()
    assert not path.exists()


@pytest.mark.parametrize("stop", ["parent", "sigterm"])
def test_serve_stops_cleanly_when_nobody_reads_its_output_any_more(
    tmp_path: Path, stop: str
) -> None:
    """The program that started serve read its output through pipes; once it is gone they are
    broken, and a write to them fails. Serve still stops as it should, with nothing lost."""
    parent = subprocess.Popen(["sleep", "300"])
    path = tmp_path / "ready.json"
    p = subprocess.Popen(
        [sys.executable, "-m", "wavescribe", "serve", "--home", str(tmp_path / "h"), "--port",
         "8797", "--no-store", "--no-alerts", "-v", "--ready-file", str(path),
         "--parent-pid", str(parent.pid)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=serve_env(),
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while not path.exists():
            assert p.poll() is None and time.monotonic() < deadline, p.stderr.read().decode()
            time.sleep(0.05)
        p.stdout.close()  # the pipes' reading ends go with the program that held them
        p.stderr.close()
        t0 = time.monotonic()
        if stop == "parent":
            parent.kill()
        else:
            p.send_signal(signal.SIGTERM)
        assert p.wait(10) == (0 if stop == "parent" else 143)
        assert time.monotonic() - t0 < 2
    finally:
        parent.kill()
        parent.wait()
        if p.poll() is None:
            p.kill()
            p.wait()
    assert not path.exists()
    logged = (tmp_path / "h" / "data" / "logs" / "wavescribe.jsonl").read_text()
    assert "Traceback" not in logged and "BrokenPipeError" not in logged


def test_serve_whose_parent_has_ended_exits_0_before_loading_anything(
    tmp_path: Path, capsys
) -> None:
    gone = subprocess.Popen(["true"])
    gone.wait()
    h = tmp_path / "h"
    assert main(["serve", "--home", str(h), "--port", PORT, "--parent-pid", str(gone.pid)]) == 0
    assert f"process {gone.pid} (--parent-pid) has ended; not starting" in capsys.readouterr().err
    assert not h.exists()


@pytest.mark.parametrize(
    ("file", "text", "extra", "said"),
    [
        ("stations.toml", '[[station]]\nid = "uno"\nlanguage = [', [], "stations.toml"),
        ("ui.toml", "[stations\n", [], "ui.toml"),
        ("alerts.toml", "[[rule]]\nid = 3\n", [], "alerts.toml"),
        (None, None, ["nope"], "unknown station 'nope'"),
    ],
)  # fmt: skip
def test_serve_exits_2_when_a_file_or_a_station_named_cant_be_used(
    tmp_path: Path, monkeypatch, capsys, file, text, extra, said
) -> None:
    monkeypatch.setattr("wavescribe.log.setup_logging", lambda *_a, **_k: None)
    h = tmp_path / "h"
    (h / "config").mkdir(parents=True)
    if file:
        (h / "config" / file).write_text(text, encoding="utf-8")
    argv = ["serve", "--home", str(h), "--port", PORT, "--no-store", *extra]
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert err.startswith("wavescribe: ") and said in err
    assert not (h / "data" / "serve.json").exists()


def test_serve_exits_1_when_it_cant_write_its_ready_file(tmp_path: Path) -> None:
    folder = tmp_path / "ready.json"
    folder.mkdir()  # a folder where the file would go
    p, log = start_serve(tmp_path, "--ready-file", str(folder))
    assert p.wait(60) == 1
    assert f"wavescribe: can't write the ready file {folder}" in log.read_text()
