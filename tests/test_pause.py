# SPDX-License-Identifier: Apache-2.0
"""Pausing all listening and resuming it (POST /api/pause, /api/resume), and what a program
reads of how things stand: GET /api/hello, /api/state's ``paused`` and the feed's status event.

The made-up receivers of tests/page_fixture.py show that pause lets go of every receiver and
resume opens them again; that page listens on port 8798."""

from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import threading
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pytest

from tests.test_app import Running, make_app, wait_for
from wavescribe import __version__
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.sources import SourceHealth
from wavescribe.web.server import trouble

STATIONS = {
    "defaults": {"language": "es", "translate_to": ["en"]},
    "station": [
        {"id": "a", "name": "Radio A", "stream": "http://example.test/a"},
        {"id": "b", "name": "Radio B", "stream": "http://example.test/b"},
        {"id": "c", "name": "Radio C", "stream": "http://example.test/c"},
    ],
}


class EndlessSource:
    """A stream that plays silence until it is closed, as a radio does."""

    opened: ClassVar[list[str]] = []
    closed: ClassVar[list[str]] = []

    def __init__(self, station_id: str) -> None:
        self.station_id = station_id
        self.health = SourceHealth()
        self._stop = threading.Event()
        EndlessSource.opened.append(station_id)

    def __iter__(self):
        self.health.connected = True
        while not self._stop.wait(0.1):
            yield AudioChunk(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), t_end=time.time())

    def close(self) -> None:
        self._stop.set()
        EndlessSource.closed.append(self.station_id)


@pytest.fixture
def node(tmp_path: Path):
    EndlessSource.opened, EndlessSource.closed = [], []
    app = make_app(tmp_path, stations=STATIONS, start=["a", "b"],
                   page={"run": {"stations": ["a", "b"]}})  # fmt: skip
    app.pipeline._source_factory = lambda st: EndlessSource(st.id)
    r = Running(app)
    wait_for(lambda: set(app.pipeline.status()["stations"]) == {"a", "b"})
    yield r
    r.close()


def saved_run(tmp_path: Path) -> list[str]:
    return tomllib.loads((tmp_path / "node" / "ui.toml").read_text())["run"]["stations"]


def test_pause_stops_every_station_keeps_what_is_saved_and_resume_starts_them_again(
    node: Running, tmp_path: Path
) -> None:
    app = node.app
    status, body = node.call("POST", "/api/pause", {"ignored": True})
    assert status == 200 and body["paused"] is True and body["since"] > 0
    assert app.pipeline.status()["stations"] == {}
    assert sorted(EndlessSource.closed) == ["a", "b"]
    assert app.pipeline.keep_models
    assert saved_run(tmp_path) == ["a", "b"]  # the next serve starts them: pause isn't kept
    since = body["since"]
    assert node.call("POST", "/api/pause") == (200, {"paused": True, "since": since})

    state = node.call("GET", "/api/state")[1]
    assert state["paused"] is True and state["paused_since"] == since
    assert not any(s["running"] for s in state["stations"])

    # Changes while paused are saved, and take effect on resume.
    assert node.call("POST", "/api/stations/c/start")[0] == 200
    assert node.call("POST", "/api/stations/b/stop")[0] == 200
    assert app.pipeline.status()["stations"] == {}
    assert saved_run(tmp_path) == ["a", "c"]

    assert node.call("POST", "/api/resume", "x") == (200, {"paused": False, "since": None})
    wait_for(lambda: set(app.pipeline.status()["stations"]) == {"a", "c"})
    assert not app.pipeline.keep_models
    assert node.call("POST", "/api/resume") == (200, {"paused": False, "since": None})
    state = node.call("GET", "/api/state")[1]
    assert state["paused"] is False and state["paused_since"] is None


def test_models_stay_loaded_while_paused(node: Running, monkeypatch) -> None:
    import wavescribe.pipeline as pipeline

    monkeypatch.setattr(pipeline, "ASR_IDLE_UNLOAD_S", 0)
    loaded = {"m": object()}
    node.app.pipeline._asr_models["m"] = 0.0
    node.call("POST", "/api/pause")
    node.app.pipeline._unload_idle(loaded)
    assert loaded and "m" in node.app.pipeline._asr_models
    node.call("POST", "/api/resume")
    node.app.pipeline._unload_idle(loaded)  # running again: only what no station uses goes
    assert set(loaded) <= {"m"}


def test_the_page_keeps_working_while_paused(node: Running) -> None:
    node.call("POST", "/api/pause")
    assert node.call("GET", "/api/alerts")[0] == 200
    assert node.call("GET", "/api/history?station=a")[0] == 200
    assert node.call("GET", "/api/search?q=agua")[0] == 200
    # A station's settings still change while paused.
    status, _ = node.call("POST", "/api/manage/stations/a", {"translate_to": ["fr"]})
    assert status == 200
    node.call("POST", "/api/resume")


def test_pause_and_resume_are_page_changes(node: Running) -> None:
    for path in ("/api/pause", "/api/resume"):
        status, _ = node.call("POST", path, headers={"X-Wavescribe": ""})
        assert status == 403  # without the header a cross-site form could send
        status, _ = node.call("POST", path, headers={"X-Forwarded-For": "192.0.2.7"})
        assert status == 403  # through a proxy: another machine's
    assert node.app.paused_since is None


def get(node: Running, path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=10)
    conn.request("GET", path, headers=headers or {})  # no X-Wavescribe, no token
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def test_hello_from_this_machine_and_from_another(node: Running, tmp_path: Path) -> None:
    from wavescribe import home

    status, here = get(node, "/api/hello")
    port = node.server.server_address[1]
    assert status == 200 and list(here) == [
        "app", "version", "pid", "host", "port", "url", "home", "started",
        "paused", "listening", "ready", "state", "problem",
    ]  # fmt: skip
    assert here["app"] == "wavescribe" and here["version"] == __version__
    assert here["pid"] == os.getpid() and here["home"] == str(home.current().path)
    assert here["port"] == port and here["url"] == f"http://127.0.0.1:{port}/"
    assert here["paused"] is False and here["listening"] == 2 and here["ready"] is True
    assert (here["state"], here["problem"]) == ("listening", None)

    status, there = get(node, "/api/hello", {"X-Forwarded-For": "192.0.2.7"})
    assert status == 200 and "pid" not in there and "home" not in there
    assert {k: v for k, v in here.items() if k not in ("pid", "home")} == there

    node.call("POST", "/api/pause")
    _, paused = get(node, "/api/hello")
    assert paused["paused"] is True and paused["listening"] == 0
    assert (paused["state"], paused["problem"]) == ("paused", None)
    node.call("POST", "/api/resume")


def test_the_feed_status_event_says_how_things_stand(node: Running, monkeypatch) -> None:
    import wavescribe.web.feed as feed

    monkeypatch.setattr(feed, "KEEPALIVE_S", 0.5)
    node.call("POST", "/api/pause")
    conn = http.client.HTTPConnection(*node.server.server_address, timeout=10)
    conn.request("GET", "/feed/v1/events?events=status")
    resp = conn.getresponse()
    assert resp.status == 200
    while not (line := resp.readline().decode()).startswith("data:"):
        pass
    event = json.loads(line[5:])
    assert event["type"] == "status" and event["paused"] is True
    assert (event["state"], event["problem"]) == ("paused", None)
    t0 = time.monotonic()
    node.call("POST", "/api/resume")  # said at once, not at the next 10 s
    while not (line := resp.readline().decode()).startswith("data:"):
        pass
    conn.close()
    event = json.loads(line[5:])
    assert event["paused"] is False and event["state"] != "paused"
    assert time.monotonic() - t0 < 5


def test_state_is_loading_then_problem_from_what_the_health_tab_shows(node: Running) -> None:
    hub = node.app.hub
    base = {"ready": True, "paused": False, "stations": {}}
    assert hub.condition(status={**base, "ready": False}) == ("loading", None)
    assert hub.condition(status={**base, "paused": True, "ready": False}) == ("paused", None)
    down = {"connected": False, "reconnects": 2, "error": "HTTP 503"}
    stations = {"a": down, "b": {"connected": False, "reconnects": 0, "error": None},
                "c": {"connected": False, "reconnects": 4}}  # fmt: skip
    assert hub.condition(status={**base, "stations": stations}) == (
        "problem",
        "Radio A: down: HTTP 503 (and 1 more)",
    )
    assert hub.condition(status={**base, "stations": {"b": stations["b"]}}) == ("listening", None)


@pytest.mark.parametrize(
    ("st", "said"),
    [
        ({"connected": True}, None),
        ({"connected": False, "reconnects": 0, "error": None}, None),  # connecting, first time
        ({"connected": False, "stopping": True, "error": "x"}, None),
        ({"connected": False, "reconnects": 1}, "reconnecting"),
        ({"connected": False, "reconnects": 1, "error": "no dongle"}, "down: no dongle"),
        ({"connected": False, "reconnects": 3, "remote": "http://x", "error": "refused"},
         "reconnecting: refused"),
        ({"connected": False, "pushed": True, "reconnects": 0}, None),
    ],
)  # fmt: skip
def test_trouble_is_the_health_tabs_rule(st: dict[str, Any], said: str | None) -> None:
    assert trouble(st) == said


# -- the made-up receivers -----------------------------------------------------------------


def rtl_sdr_children(pid: int) -> list[str]:
    """The stand-in rtl_sdr processes serve runs: one per receiver it holds open."""
    out = subprocess.run(["ps", "-A", "-o", "ppid=,command="], capture_output=True, text=True)
    return sorted(
        m[1]
        for line in out.stdout.splitlines()
        if line.split(None, 1)[0] == str(pid)
        and "fake_rtl_sdr.py" in line
        and (m := re.search(r" -d (\S+)", line))
    )


def post(url: str, path: str, body: Any = None) -> Any:
    req = urllib.request.Request(url + path.lstrip("/"), method="POST",
                                 data=json.dumps(body or {}).encode(),
                                 headers={"X-Wavescribe": "1",
                                          "Content-Type": "application/json"})  # fmt: skip
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def read(url: str, path: str) -> Any:
    req = urllib.request.Request(url + path.lstrip("/"), headers={"X-Wavescribe": "1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_pause_lets_go_of_every_receiver_and_resume_opens_them_again() -> None:
    from tests.page_fixture import SERIALS, running

    with running(8798) as f:
        pid = f.process.pid
        # Receiver A runs two stations, B watches its spectrum, C is tuned by hand.
        post(f.url, "/api/manage/receivers/b/monitor", {"on": True})
        post(f.url, "/api/manage/live/c", {"mhz": 120.5, "mode": "am", "acknowledge": True})
        wait_for(lambda: rtl_sdr_children(pid) == sorted(SERIALS), 20)

        assert post(f.url, "/api/pause")["paused"] is True
        wait_for(lambda: rtl_sdr_children(pid) == [], 10)
        state = read(f.url, "/api/state")
        assert state["paused"] and not any(s["running"] for s in state["stations"])
        assert not read(f.url, "/api/manage/live/c")["open"]
        with pytest.raises(urllib.error.HTTPError) as e:
            post(f.url, "/api/manage/live/c", {"mhz": 120.5, "mode": "am"})
        assert "Listening is paused: resume it to listen here." in e.value.read().decode()
        with pytest.raises(urllib.error.HTTPError) as e:
            post(f.url, "/api/manage/radio/scan", {"device": "b", "band": "all"})
        assert "Listening is paused: resume it to scan." in e.value.read().decode()
        time.sleep(1.0)
        assert rtl_sdr_children(pid) == []  # nothing opens them again meanwhile

        assert post(f.url, "/api/resume")["paused"] is False
        wait_for(lambda: rtl_sdr_children(pid) == sorted(SERIALS), 20)
        state = read(f.url, "/api/state")
        assert {s["id"] for s in state["stations"] if s["running"]} == {"valley", "harbour"}
        live = read(f.url, "/api/manage/live/c")
        assert live["open"] and live["mhz"] == 120.5 and live["mode"] == "am"
        assert read(f.url, "/api/manage/receivers")["devices"][1]["monitoring"] is True
