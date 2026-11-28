# SPDX-License-Identifier: Apache-2.0
"""Model downloads where a program sees them: GET /api/hello and the feed's status event carry
``downloads`` (docs/FEED.md), said at once when one starts, ends or fails, and every few seconds
while one runs. The downloads are stand-in child processes: no network."""

from __future__ import annotations

import http.client
import itertools
import json
import sys
import time
from pathlib import Path

import pytest

from tests.test_app import Running, make_app, wait_for
from wavescribe import downloads, modelfetch
from wavescribe.registry import ModelEntry, ModelRegistry
from wavescribe.web import feed

NONE = {"running": 0, "done_bytes": 0, "total_bytes": 0, "percent": None, "failed": []}
# What `models fetch` ends with when the connection drops, as the page shows it.
WHY = "couldn't download m: no connection to models.example; start the download again"
# A stand-in that writes 250 kB of the model's 1 MB (models.toml says 0.001 GB), then waits.
WRITES = """
import sys, time
from pathlib import Path
folder = Path(sys.argv[1])
folder.mkdir(parents=True, exist_ok=True)
(folder / ("m.bin." + "0" * 16 + ".part")).write_bytes(b"p" * 250_000)
time.sleep(60)
"""
FAILS = "import sys; print('wavescribe: ' + sys.argv[2], file=sys.stderr); sys.exit(1)"


def sized(root: Path) -> ModelRegistry:
    """One model of 1 MB from a URL for every job, not downloaded yet."""
    m = ModelEntry("m", "asr", "fake", "url:https://models.example/m.bin", "u", "MIT", "u",
                   "default", False, ("*",), "", sha256="0" * 64, size_gb=0.001)  # fmt: skip
    return ModelRegistry([m], {"vad": ["m"], "asr": ["m"], "translate": ["m"]},
                         models_dir=root / "models")  # fmt: skip


@pytest.fixture
def node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(modelfetch, "free_bytes", lambda _path: 10**12)
    r = Running(make_app(tmp_path, models=sized))
    yield r
    r.close()


def child(code: str, folder: Path) -> list[str]:
    return [sys.executable, "-c", code, str(folder), WHY]


def hello(node: Running, headers: dict[str, str] | None = None) -> dict:
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=10)
    conn.request("GET", "/api/hello", headers=headers or {})
    body = json.loads(conn.getresponse().read())
    conn.close()
    return body


def test_the_downloads_in_hello_running_then_failed_then_started_again(
    node: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "node" / "models" / "m"
    assert hello(node)["downloads"] == NONE

    monkeypatch.setattr(downloads, "fetch_command", lambda _m: child(WRITES, folder))
    assert node.call("POST", "/api/manage/models/m/fetch")[0] == 200
    wait_for(lambda: hello(node)["downloads"]["done_bytes"] == 250_000)
    assert hello(node)["downloads"] == {
        "running": 1, "done_bytes": 250_000, "total_bytes": 1_000_000, "percent": 25.0,
        "failed": [],
    }  # fmt: skip
    node.call("POST", "/api/manage/models/m/stop")
    assert hello(node)["downloads"] == NONE  # stopped on purpose: not a failure

    monkeypatch.setattr(downloads, "fetch_command", lambda _m: child(FAILS, folder))
    node.call("POST", "/api/manage/models/m/fetch")
    wait_for(lambda: hello(node)["downloads"]["failed"])
    failed = {**NONE, "failed": [{"model": "m", "error": WHY}]}
    assert hello(node)["downloads"] == failed
    # Another machine learns which download failed, not why: the sentence may name folders.
    there = hello(node, {"X-Forwarded-For": "192.0.2.7"})["downloads"]
    assert there["failed"] == [{"model": "m", "error": "the download failed; the Models tab "
                                "on the Wavescribe machine says why"}]  # fmt: skip

    # Started again: no longer a failure, whatever it comes to.
    monkeypatch.setattr(downloads, "fetch_command", lambda _m: child(WRITES, folder))
    node.call("POST", "/api/manage/models/m/fetch")
    assert hello(node)["downloads"]["failed"] == []
    node.call("POST", "/api/manage/models/m/stop")


def test_a_removed_model_is_no_longer_a_failed_download(
    node: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "node" / "models" / "m"
    monkeypatch.setattr(downloads, "fetch_command", lambda _m: child(FAILS, folder))
    node.call("POST", "/api/manage/models/m/fetch")
    wait_for(lambda: hello(node)["downloads"]["failed"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ("m.bin." + "0" * 16 + ".part")).write_bytes(b"p" * 1000)  # what it kept
    assert node.call("POST", "/api/manage/models/m/remove")[0] == 200
    assert hello(node)["downloads"] == NONE


def events(node: Running):
    """The feed's status events, as they come, with when each came."""
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=10)
    conn.request("GET", "/feed/v1/events?events=status")
    resp = conn.getresponse()
    assert resp.status == 200

    def next_event() -> tuple[float, dict]:
        while not (line := resp.readline().decode()).startswith("data:"):
            pass
        return time.monotonic(), json.loads(line[5:])

    return conn, next_event


def test_the_status_event_says_a_download_at_once_and_while_it_runs(
    node: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "node" / "models" / "m"
    monkeypatch.setattr(feed, "DOWNLOADING_STATUS_S", 0.5)
    conn, next_event = events(node)
    try:
        _, first = next_event()
        assert first["downloads"] == NONE

        monkeypatch.setattr(downloads, "fetch_command", lambda _m: child(WRITES, folder))
        asked = time.monotonic()
        node.call("POST", "/api/manage/models/m/fetch")  # said at once, not at the next 10 s
        came, event = next_event()
        assert event["downloads"]["running"] == 1 and came - asked < 5
        # While it runs, every DOWNLOADING_STATUS_S: its bytes come in.
        times = [next_event() for _ in range(3)]
        assert times[-1][1]["downloads"]["done_bytes"] == 250_000
        assert all(b[0] - a[0] < 3 for a, b in itertools.pairwise(times))

        monkeypatch.setattr(downloads, "fetch_command", lambda _m: child(FAILS, folder))
        node.call("POST", "/api/manage/models/m/stop")  # ends: said at once
        while (event := next_event()[1])["downloads"]["running"]:
            pass
        assert event["downloads"] == NONE
        node.call("POST", "/api/manage/models/m/fetch")  # and fails
        while not (event := next_event()[1])["downloads"]["failed"]:
            pass
        assert event["downloads"]["failed"] == [{"model": "m", "error": WHY}]
    finally:
        conn.close()
