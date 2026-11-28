# SPDX-License-Identifier: Apache-2.0
"""Removing a downloaded model while Wavescribe runs: on the page, and with `models remove` while
a `serve` holds the home. A model a running station uses right now is refused, naming the
station; any other goes, with its own files only, and nothing plans to use it any more.

Every model here is a made-up file in the app's own temporary folder: nothing real is touched.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tests.test_app import NowSource, Running, make_app, wait_for
from tests.test_segmenter import silence, tone
from wavescribe import downloads, home
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.cli import main
from wavescribe.registry import ModelEntry, ModelRegistry

SIZES = {"vad": 2_000, "speech": 3_000_000, "speech-2": 2_000_000, "translator": 1_000_000}
JOBS = {"vad": "vad", "speech": "asr", "speech-2": "asr", "translator": "translate"}


def four_models(root: Path) -> ModelRegistry:
    """A speech detector, two speech models and a translator, each from a URL and installed:
    one file in models/<id>/."""
    entries = []
    for mid, size in SIZES.items():
        entries.append(ModelEntry(mid, JOBS[mid], "fake", f"url:https://models.example/{mid}.bin",
                                  "u", "MIT", "u", "default", False, ("*",), "",
                                  size_gb=size / 1e9))  # fmt: skip
        (root / "models" / mid).mkdir(parents=True)
        (root / "models" / mid / f"{mid}.bin").write_bytes(b"x" * size)
    chains = {"vad": ["vad"], "asr": ["speech", "speech-2"], "translate": ["translator"]}
    return ModelRegistry(entries, chains, models_dir=root / "models")


class OnAir(NowSource):
    """A station that says one thing, then plays silence until it is stopped."""

    def __init__(self, station_id: str) -> None:
        super().__init__(station_id, np.concatenate([silence(0.5), tone(2), silence(1)]))
        self._stop = threading.Event()

    def __iter__(self):
        yield from super().__iter__()
        while not self._stop.wait(0.1):
            yield AudioChunk(np.zeros(SAMPLE_RATE // 10, dtype=np.float32), t_end=time.time())

    def close(self) -> None:
        self._stop.set()


@pytest.fixture
def node(tmp_path: Path):
    app = make_app(tmp_path, start=["a"], models=four_models)
    app.pipeline._source_factory = lambda st: OnAir(st.id)
    r = Running(app)
    wait_for(lambda: "a" in app.running())
    wait_for(lambda: app.pipeline.status()["models"]["mt"] is not None)  # it has translated
    yield r
    r.close()


def test_a_model_a_running_station_uses_is_refused_and_named(node: Running, tmp_path: Path):
    models = tmp_path / "node" / "models"
    # Rádio B plays, listened to only: it uses no model.
    assert node.call("POST", "/api/manage/stations/b/start", {"listen_only": True})[0] == 200
    wait_for(lambda: "b" in node.app.running())

    status, view = node.call("GET", "/api/manage/models/speech/remove")
    assert status == 200 and view["on_disk"] and view["size"] == 3_000_000
    assert view["places"] == [str(models / "speech")]  # as `models remove` lists it
    assert view["code"] == "in_use" and view["refusal"] == (
        "speech can't be removed while it is in use: Rádio A uses it right now, so stop that "
        "station first"
    )
    status, refused = node.call("POST", "/api/manage/models/speech/remove")
    assert status == 400 and refused["code"] == "in_use" and refused["stations"] == ["Rádio A"]
    assert (models / "speech" / "speech.bin").exists()
    # The speech detector too: every station written down uses it.
    assert node.call("POST", "/api/manage/models/vad/remove")[1]["stations"] == ["Rádio A"]
    # The translator, which also stays loaded for questions and rules.
    refused = node.call("POST", "/api/manage/models/translator/remove")[1]
    assert refused["error"] == (
        "translator can't be removed while it is in use: Rádio A uses it right now, so stop "
        "that station first; the translator has it loaded, so put another model first under "
        "Translation, or stop Wavescribe first"
    )
    assert refused["translator"] is True

    # A model no running station uses goes, and only its own files.
    status, done = node.call("POST", "/api/manage/models/speech-2/remove")
    assert status == 200 and done == {"model": "speech-2", "freed": 2_000_000}
    assert sorted(p.name for p in models.iterdir()) == ["speech", "translator", "vad"]
    status, again = node.call("POST", "/api/manage/models/speech-2/remove")
    assert status == 400 and again["code"] == "not_installed"
    assert again["error"] == "speech-2 isn't installed: nothing of it is on this disk"


def test_once_its_station_stops_a_model_goes_and_nothing_plans_to_use_it(node: Running):
    assert node.call("POST", "/api/manage/stations/a/stop")[0] == 200
    wait_for(lambda: "a" not in node.app.running())
    assert node.call("POST", "/api/manage/models/speech-2/remove")[0] == 200
    status, done = node.call("POST", "/api/manage/models/speech/remove")
    assert status == 200 and done["freed"] == 3_000_000

    app = node.app
    assert app.plan.asr == {}
    stations = {s["id"]: s for s in node.call("GET", "/api/manage/stations")[1]["stations"]}
    assert stations["a"]["listen_only"]
    assert "until its speech model is downloaded" in stations["a"]["reason"]
    models = {m["id"]: m for m in node.call("GET", "/api/manage/models")[1]["models"]}
    assert not models["speech"]["installed"] and not models["speech"]["loaded"]
    assert models["speech"]["removable"]

    # The speech detector: stations wait for it now; and the translator is still loaded.
    assert node.call("POST", "/api/manage/models/vad/remove")[0] == 200
    assert app.plan.vad is None
    refused = node.call("POST", "/api/manage/models/translator/remove")[1]
    assert refused["code"] == "in_use" and refused["stations"] == []


def test_a_model_being_downloaded_is_refused_until_the_download_stops(
    node: Running, monkeypatch: pytest.MonkeyPatch
) -> None:
    endless = [sys.executable, "-c", "import time; time.sleep(60)"]
    monkeypatch.setattr(downloads, "fetch_command", lambda _model: endless)
    assert node.call("POST", "/api/manage/models/speech-2/fetch")[0] == 200
    status, refused = node.call("POST", "/api/manage/models/speech-2/remove")
    assert status == 400 and refused["code"] == "downloading"
    assert refused["error"] == (
        "speech-2 is being downloaded: stop the download first, then remove it"
    )
    assert node.call("POST", "/api/manage/models/speech-2/stop")[1]["state"] == "stopped"
    assert node.call("POST", "/api/manage/models/speech-2/remove")[0] == 200


def test_only_this_machine_removes_a_model(node: Running, tmp_path: Path) -> None:
    remove = "/api/manage/models/speech-2/remove"
    assert node.call("POST", remove, headers={"X-Wavescribe": ""})[0] == 403
    assert node.call("POST", remove, headers={"X-Forwarded-For": "203.0.113.5"})[0] == 403
    assert node.call("GET", remove, headers={"X-Forwarded-For": "203.0.113.5"})[0] == 403
    assert (tmp_path / "node" / "models" / "speech-2" / "speech-2.bin").exists()


def test_models_remove_asks_the_serve_that_holds_the_home(
    node: Running, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    there = tmp_path / "home"
    there.mkdir()
    monkeypatch.chdir(there)
    monkeypatch.setenv("WAVESCRIBE_HOME", str(there))
    held = home.hold("serve")
    try:
        held.note(url=node.url + "/")
        assert main(["models", "remove", "speech", "--yes"]) == 1
        assert capsys.readouterr().err == (
            "wavescribe: speech can't be removed while it is in use: Rádio A uses it right now, "
            "so stop that station first; nothing was removed\n"
        )
        assert main(["models", "remove", "speech-2", "--yes"]) == 0
        out = capsys.readouterr().out.splitlines()
        assert out[0].split() == ["speech-2", "2", "MB", str(tmp_path / "node/models/speech-2")]
        assert out[1].startswith("Removed speech-2: 2 MB freed. To download it again: ")
        assert not (tmp_path / "node" / "models" / "speech-2").exists()
        assert "speech-2" not in {e.id for e, _ in node.app.plan.asr.values()}
    finally:
        held.release()

    held = home.hold("listen")  # no page to ask: it may be using any of them
    try:
        assert main(["models", "remove", "vad", "--yes"]) == 1
        err = capsys.readouterr().err
        assert "listen` is running in this home (pid " in err
        assert err.endswith("may be using these models: stop it first, then remove them\n")
    finally:
        held.release()
    assert (tmp_path / "node" / "models" / "vad" / "vad.bin").exists()


def test_a_models_folder_that_holds_the_hugging_face_cache_is_never_removed_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wavescribe import modelremove

    cache = tmp_path / "models" / "hf"  # where the Mac app keeps it
    (cache / "models--org--speech" / "blobs").mkdir(parents=True)
    (cache / "models--org--speech" / "blobs" / "abc").write_bytes(b"w")
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    odd = ModelEntry("hf", "asr", "fake", "url:https://models.example/hf.bin", "u", "MIT", "u",
                     "default", False, ("*",), "")  # fmt: skip
    reg = ModelRegistry([odd], {"asr": ["hf"]}, models_dir=tmp_path / "models")
    assert modelremove.plan(reg, odd) is None  # nothing of its own there: refused, not removed
    assert (cache / "models--org--speech" / "blobs" / "abc").exists()
