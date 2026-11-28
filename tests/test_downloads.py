# SPDX-License-Identifier: Apache-2.0
"""Model downloads started from the page. The download is a stand-in child process: no network."""

import sys
import time
from pathlib import Path

from tests.test_app import make_app, registry, wait_for
from wavescribe import downloads


def test_a_download_still_running_stops_with_serve(tmp_path: Path, monkeypatch) -> None:
    endless = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(downloads, "fetch_command", lambda _model: endless)
    finished: list[str] = []
    fetching = downloads.Downloads(registry(tmp_path), on_done=finished.append)
    assert fetching.start("m")["state"] == "running"
    wait_for(lambda: "m" in fetching._procs)
    child = fetching._procs["m"]

    started = time.monotonic()
    fetching.close()
    assert child.poll() is not None and time.monotonic() - started < 3
    wait_for(lambda: fetching.status()["m"]["state"] == "failed")
    assert fetching.status()["m"]["error"] == "stopped when Wavescribe stopped"
    assert finished == []
    assert fetching.start("m")  # asked for again while stopping: never starts a child
    wait_for(lambda: fetching.status()["m"]["state"] == "failed")
    assert "stopped before" in fetching.status()["m"]["error"]


def test_a_download_that_fails_shows_the_command_lines_sentence_on_the_page(
    tmp_path: Path, monkeypatch
) -> None:
    import urllib.error

    from wavescribe import modelfetch

    reg = registry(tmp_path)
    refused = urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
    sentence = modelfetch.explain(reg.get("m"), refused)
    assert "run the same command again and it downloads only what is missing" in sentence
    code = "import sys; print('m: MIT'); print(sys.argv[1], file=sys.stderr); sys.exit(1)"
    failing = [sys.executable, "-c", code, f"wavescribe: {sentence}"]
    monkeypatch.setattr(downloads, "fetch_command", lambda _model: failing)
    fetching = downloads.Downloads(reg)
    fetching.start("m")
    wait_for(lambda: fetching.status()["m"]["state"] == "failed")
    error = fetching.status()["m"]["error"]
    # The whole sentence, with what the page offers in place of "run the same command again".
    assert error == sentence.replace("run the same command again", "start the download again")
    assert error.startswith("couldn't download m: no connection to ")
    assert error.endswith("start the download again and it downloads only what is missing.")
    assert "Traceback" not in error and "command" not in error


def test_a_part_file_the_download_continues_counts_as_done_on_the_page(
    tmp_path: Path, monkeypatch
) -> None:
    from wavescribe.registry import ModelEntry, ModelRegistry

    e = ModelEntry("big", "asr", "fake", "url:https://models.example/big.bin", "u", "MIT", "u",
                   "default", False, ("*",), "", sha256="0" * 64, size_gb=0.001)  # fmt: skip
    folder = tmp_path / "models" / "big"
    folder.mkdir(parents=True)
    (folder / f"big.bin.{'0' * 16}.part").write_bytes(b"x" * 400_000)  # a stopped download's
    endless = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(downloads, "fetch_command", lambda _model: endless)
    fetching = downloads.Downloads(ModelRegistry([e], {"asr": ["big"]}, tmp_path / "models"))
    fetching.start("big")
    try:
        job = fetching.status()["big"]
        assert job["bytes"] == 400_000 and job["percent"] == 40.0
    finally:
        fetching.close()


# A stand-in for `models fetch` that writes what a real one writes into the model's folder: a
# part-file of Wavescribe's own, which the next download continues, and one of the library's,
# named after the process, which nothing continues. "deaf" ignores SIGTERM, as a child busy in
# a system call can seem to, so that it has to be killed.
CHILD = """
import os, signal, sys, time
from pathlib import Path
folder = Path(sys.argv[1])
folder.mkdir(parents=True, exist_ok=True)
if sys.argv[2] == "deaf":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
(folder / ("x." + "0" * 16 + ".part")).write_bytes(b"p" * 300_000)
(folder / ("x.%d.incomplete" % os.getpid())).write_bytes(b"i" * 100_000)
time.sleep(60)
"""


def _child(folder: Path, how: str = "") -> list[str]:
    return [sys.executable, "-c", CHILD, str(folder), how]


def _parts(folder: Path, suffix: str) -> list[str]:
    return sorted(p.name for p in folder.glob("*" + suffix))


def test_a_download_stopped_on_the_page_keeps_what_the_next_one_continues(
    tmp_path: Path, monkeypatch
) -> None:
    folder = tmp_path / "models" / "m"
    monkeypatch.setattr(downloads, "fetch_command", lambda _model: _child(folder))
    finished: list[str] = []
    fetching = downloads.Downloads(registry(tmp_path), on_done=finished.append)
    fetching.start("m")
    wait_for(lambda: _parts(folder, ".incomplete"))
    child = fetching._procs["m"]

    started = time.monotonic()
    job = fetching.stop("m")
    assert time.monotonic() - started < 3 and child.poll() is not None
    assert job["state"] == "stopped" and job["error"] is None
    assert job["note"] == (
        "what was downloaded is kept: start the download again and it continues where it stopped"
    )
    assert fetching.status()["m"]["state"] == "stopped" and finished == []
    # SIGTERM, which a real `models fetch` answers by removing the library's part-files: the
    # stand-in doesn't, so the page did. Wavescribe's own stays for the next download.
    assert _parts(folder, ".part") == ["x.0000000000000000.part"]
    assert _parts(folder, ".incomplete") == []

    assert fetching.stop("m")["state"] == "stopped"  # twice: the same answer, nothing breaks
    assert fetching.start("m")["state"] == "running"  # and it can be started again
    wait_for(lambda: _parts(folder, ".incomplete"))
    fetching.close()


def test_a_download_that_ignores_the_stop_is_killed_and_its_leftovers_removed(
    tmp_path: Path, monkeypatch
) -> None:
    fetching = downloads.Downloads(registry(tmp_path))
    folder = tmp_path / "models" / "m"
    (folder / "x.1.incomplete").write_bytes(b"t")  # there before: another download's, perhaps
    monkeypatch.setattr(downloads, "STOP_WAIT_S", 0.5)
    monkeypatch.setattr(downloads, "fetch_command", lambda _model: _child(folder, "deaf"))
    fetching.start("m")
    wait_for(lambda: len(_parts(folder, ".incomplete")) == 2)

    job = fetching.stop("m")
    assert job["state"] == "stopped"
    assert _parts(folder, ".incomplete") == ["x.1.incomplete"]
    assert _parts(folder, ".part") == ["x.0000000000000000.part"]


def test_a_stop_before_the_download_began_starts_nothing(tmp_path: Path, monkeypatch) -> None:
    def never(_model: str) -> list[str]:
        raise AssertionError("no download may start after a stop")

    monkeypatch.setattr(downloads, "fetch_command", never)
    reg = registry(tmp_path)
    fetching = downloads.Downloads(reg)
    job = {"model": "m", "state": "stopping", "error": None, "note": None, "base_bytes": 0}
    fetching._run(reg.get("m"), job)
    assert job["state"] == "stopped"
    assert job["note"].endswith("start the download again and it downloads only what is missing")


def test_nothing_to_stop_is_said_plainly(tmp_path: Path) -> None:
    import pytest

    from wavescribe.registry import ModelError

    with pytest.raises(ModelError, match="m isn't being downloaded: there is nothing to stop"):
        downloads.Downloads(registry(tmp_path)).stop("m")


def test_the_page_stops_a_download_only_from_this_machine(tmp_path: Path, monkeypatch) -> None:
    from tests.test_app import Running

    node = Running(make_app(tmp_path))
    try:
        folder = tmp_path / "node" / "models" / "m"
        monkeypatch.setattr(downloads, "fetch_command", lambda _model: _child(folder))
        assert node.call("POST", "/api/manage/models/m/fetch")[0] == 200
        wait_for(lambda: _parts(folder, ".incomplete"))
        # Another website's form (no header), and a request a proxy forwards: refused.
        stop = "/api/manage/models/m/stop"
        assert node.call("POST", stop, headers={"X-Wavescribe": ""})[0] == 403
        assert node.call("POST", stop, headers={"X-Forwarded-For": "203.0.113.5"})[0] == 403
        assert node.app.downloads.status()["m"]["state"] == "running"

        status, job = node.call("POST", "/api/manage/models/m/stop")
        assert status == 200 and job["state"] == "stopped" and "continues" in job["note"]
        status, models = node.call("GET", "/api/manage/models")
        assert models["downloads"]["m"]["state"] == "stopped"
        status, refused = node.call("POST", "/api/manage/models/other/stop")
        assert status == 400 and "nothing to stop" in refused["error"]
    finally:
        node.close()


def test_serve_stopping_stops_its_downloads(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    stopped: list[bool] = []
    monkeypatch.setattr(app.downloads, "close", lambda: stopped.append(True))
    app.pipeline.stop()
    app.close()
    app.store.close()
    assert stopped == [True]
