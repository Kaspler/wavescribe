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


def test_serve_stopping_stops_its_downloads(tmp_path: Path, monkeypatch) -> None:
    app = make_app(tmp_path)
    stopped: list[bool] = []
    monkeypatch.setattr(app.downloads, "close", lambda: stopped.append(True))
    app.pipeline.stop()
    app.close()
    app.store.close()
    assert stopped == [True]
