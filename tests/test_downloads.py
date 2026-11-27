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


def test_serve_stopping_stops_its_downloads(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    stopped: list[bool] = []
    monkeypatch.setattr(app.downloads, "close", lambda: stopped.append(True))
    app.pipeline.stop()
    app.close()
    app.store.close()
    assert stopped == [True]
