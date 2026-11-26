# SPDX-License-Identifier: Apache-2.0
"""Per-station switches: listen only (no transcription), no translation, and a station's own
speech and translation models."""

import dataclasses
import threading
from pathlib import Path

import numpy as np
import pytest

from tests.test_app import NowSource, Running, make_app, wait_for
from tests.test_pipeline import ENTRY, PLAN, FakeTranscriber, FakeTranslator, ListSource, station
from tests.test_segmenter import EnergyVad, silence, tone
from wavescribe.app import ChangeError
from wavescribe.config import ConfigError, parse_stations
from wavescribe.pipeline import NO_VAD, Pipeline, Plan, Result, plan_available
from wavescribe.registry import ModelRegistry
from wavescribe.settings import effective_config


def test_switches_parse_and_switch_translation_off(tmp_path: Path) -> None:
    cfg = parse_stations({
        "defaults": {"language": "de-DE", "translate_to": ["en"]},
        "station": [
            {"id": "a", "stream": "http://x.test/a", "transcribe": False},
            {"id": "b", "stream": "http://x.test/b", "translate": False},
            {"id": "c", "stream": "http://x.test/c"},
        ],
    }, tmp_path / "s.toml")  # fmt: skip
    a, b, c = cfg.stations
    assert not a.transcribe and a.translation_targets == ()
    assert b.transcribe and not b.translate and b.translation_targets == ()
    assert b.translate_to == ("en",)  # kept, for when translation is switched back on
    assert c.transcribe and c.translate and c.translation_targets == ("en",)
    with pytest.raises(ConfigError, match="channel_khz"):
        dongle = {"id": "d", "serial": "SN0042", "center_mhz": 446.0, "gain_db": 30}
        parse_stations({"device": [dongle],
                        "station": [{"id": "x", "language": "es", "device": "d", "mhz": 446.1,
                                     "acknowledge": True, "channel_khz": 60}]},
                       tmp_path / "s.toml")  # fmt: skip
    with pytest.raises(ConfigError, match="true or false"):
        parse_stations({"station": [{"id": "x", "language": "es", "stream": "http://x.test",
                                     "transcribe": "no"}]}, tmp_path / "s.toml")  # fmt: skip


def test_page_stations_keep_their_model_choices(tmp_path: Path) -> None:
    base = parse_stations({"defaults": {"language": "es", "translate_to": ["en"]},
                           "station": [{"id": "f", "stream": "http://x.test/f"}]},
                          tmp_path / "s.toml")  # fmt: skip
    config, errors = effective_config(base, {
        "station": [{"id": "p", "stream": "http://x.test/p", "asr_model": "whisper",
                     "mt_model": "madlad", "transcribe": False}],
        "overrides": {"f": {"mt_model": "madlad", "translate": False}},
    })  # fmt: skip
    assert not errors
    p, f = config.get("p"), config.get("f")
    assert (p.asr_model, p.mt_model, p.transcribe) == ("whisper", "madlad", False)
    assert (f.mt_model, f.translate) == ("madlad", False)


def registry_without_vad(tmp_path: Path) -> ModelRegistry:
    return ModelRegistry([ENTRY], {"vad": ["m"], "asr": ["m"], "translate": ["m"]},
                         models_dir=tmp_path)  # fmt: skip


def test_listening_needs_no_model_at_all(tmp_path: Path) -> None:
    listen = dataclasses.replace(station("listen"), transcribe=False)
    plan, unavailable = plan_available(registry_without_vad(tmp_path),
                                       [listen, station("talk")], translate=True)  # fmt: skip
    assert plan.vad is None and plan.asr == {}
    assert unavailable == {"talk": NO_VAD}  # listen isn't held back by the missing models

    heard: list[int] = []
    results: list[Result] = []
    audio = np.concatenate([tone(2), silence(1)] * 2)

    def no_models(*_a):
        raise AssertionError("a listen-only station loaded a model")

    pipeline = Pipeline(
        [listen], plan, results.append,
        on_audio=lambda sid, chunk: heard.append(len(chunk.samples)),
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=no_models, transcriber_factory=no_models, translator_factory=no_models,
    )  # fmt: skip
    pipeline.run(duration=10)
    assert sum(heard) == len(audio)  # every sample reached the page's audio
    assert results == []


class SlowSource(ListSource):
    """Half a second of audio per half second, so switches land mid-stream."""

    def __iter__(self):
        import time

        for chunk in super().__iter__():
            time.sleep(0.05)
            yield chunk


def test_transcription_switches_on_and_off_without_restarting_the_audio() -> None:
    listen = dataclasses.replace(station("a"), transcribe=False)
    sources: list[ListSource] = []
    results: list[Result] = []
    audio = np.concatenate([tone(1), silence(1)] * 40)

    def source(st):
        sources.append(SlowSource(st.id, audio))
        return sources[-1]

    pipeline = Pipeline(
        [listen], dataclasses.replace(PLAN, asr={"a": (ENTRY, Path())}), results.append,
        source_factory=source, vad_factory=lambda _p: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: FakeTranslator(),
    )  # fmt: skip
    threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True).start()
    assert wait_for_true(lambda: pipeline.status()["stations"].get("a"))
    pipeline.update_station(station("a"))  # transcribe on, in place
    assert wait_for_true(lambda: len(results) >= 2)
    assert results[0].translations == {"en": "en:1s"}
    pipeline.update_station(dataclasses.replace(station("a"), translate=False))
    n = len(results)
    assert wait_for_true(lambda: len(results) >= n + 2)
    assert results[-1].translations == {}
    pipeline.update_station(dataclasses.replace(station("a"), transcribe=False))
    import time

    time.sleep(0.6)  # what was being said is finished, then nothing more
    n = len(results)
    time.sleep(0.6)
    assert len(results) == n
    assert len(sources) == 1  # one source the whole time: the audio never stopped
    pipeline.stop()


class SlowNowSource(NowSource):
    def __iter__(self):
        import time

        for chunk in super().__iter__():
            time.sleep(0.05)
            yield chunk


def wait_for_true(cond, timeout: float = 10.0) -> bool:
    try:
        wait_for(cond, timeout)
    except AssertionError:
        return False
    return True


def test_a_station_can_have_its_own_translator() -> None:
    madlad = dataclasses.replace(ENTRY, id="madlad", capability="translate")
    loads: list[str] = []

    class Named(FakeTranslator):
        def __init__(self, name: str) -> None:
            self.model_id = name

        def translate(self, text: str, source: str, target: str) -> str:
            return f"{self.model_id}:{text}"

    def translator(entry, _p):
        loads.append(entry.id)
        return Named(entry.id)

    plan = Plan(vad=PLAN.vad, asr={"a": (ENTRY, Path()), "b": (ENTRY, Path())},
                mt=(ENTRY, Path()), mt_station={"b": (madlad, Path())})  # fmt: skip
    results: list[Result] = []
    audio = np.concatenate([tone(2), silence(1)])
    pipeline = Pipeline(
        [station("a"), station("b")], plan, results.append,
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=lambda _p: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=translator,
    )  # fmt: skip
    pipeline.run(duration=20)
    by = {r.station.id: r for r in results}
    assert by["a"].translations == {"en": "m:2s"} and by["a"].translations_model == "m"
    assert by["b"].translations == {"en": "madlad:2s"} and by["b"].translations_model == "madlad"
    assert sorted(loads) == ["m", "madlad"]


def test_the_page_switches_a_running_station_to_listen_only(tmp_path: Path) -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1)] * 30)
    app = make_app(tmp_path, audio=audio, start=["a"])
    app.pipeline._source_factory = lambda st: SlowNowSource(st.id, audio)  # keeps running
    node = Running(app)
    try:
        app = node.app
        wait_for(lambda: app.store.recent("a"))
        status, view = node.call("POST", "/api/manage/stations/a", {"transcribe": False})
        assert status == 200 and view["transcribe"] is False and view["running"]
        assert view["asr_model"] is None and "a" not in app.unavailable
        assert "a" in app.running()  # still playing, just not written down

        status, view = node.call("POST", "/api/manage/stations/a",
                                 {"transcribe": True, "translate": False})  # fmt: skip
        assert status == 200 and view["transcribe"] and not view["translate"]
        assert view["asr_model"] == "m" and view["mt_model"] == "m"

        assert node.call("POST", "/api/manage/stations/a", {"asr_model": "nope"})[0] == 400
        assert node.call("POST", "/api/manage/stations/a", {"mt_model": "nope"})[0] == 400
    finally:
        node.close()


def test_without_models_stations_play_and_get_text_once_downloaded(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    audio = np.concatenate([silence(0.5), tone(2), silence(1)] * 30)
    app.pipeline._source_factory = lambda st: SlowNowSource(st.id, audio)  # keeps running
    models = tmp_path / "node" / "models" / "m"
    for f in models.iterdir():
        f.unlink()
    models.rmdir()  # as on a new install: nothing downloaded
    app.plan.vad, app.plan.asr, app.plan.mt = None, {}, None
    app._reload()
    for sid in ("a", "b"):
        app.plan.asr.pop(sid, None)
        app._replan(app.config.get(sid), None)
    try:
        # The stations want text but can't have it yet: they play meanwhile, and say why.
        assert "a" not in app.unavailable and app._startable("a")
        view = next(v for v in app.stations_view() if v["id"] == "a")
        assert view["available"] and view["transcribe"] and view["listen_only"]
        assert "models fetch" in view["reason"]
        app.start_station("a")
        assert app.pipeline._live["a"].station.transcribe is False

        view = app.update_station("b", {"transcribe": False})  # listened to by choice
        assert view["available"] and not view["listen_only"] and view["reason"] is None
        with pytest.raises(ChangeError, match="speech detector"):
            app.update_station("b", {"transcribe": True})  # by hand: refused, says what to fetch

        models.mkdir()
        (models / "x").write_bytes(b"")
        app.models_changed("m")  # what a finished download from the page calls
        assert app.plan.vad is not None and app.plan.mt is not None
        assert app.plan.asr["a"][0].id == "m" and not app.listen_only
        assert app.pipeline._live["a"].station.transcribe is True  # same audio, now with text
        view = app.update_station("b", {"transcribe": True})
        assert view["available"] and view["asr_model"] == "m"
    finally:
        app.pipeline.stop()
        app.store.close()


def test_a_translation_language_the_translator_lacks_is_named_and_the_station_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """MADLAD has a fixed list of languages. A station asking for one it lacks still runs, is
    written down and alerts on its own words; it is translated into the rest, and its reason
    names what is missing, from startup on and after every change on the page."""
    monkeypatch.setattr("wavescribe.app.unsupported_targets",
                        lambda _e, _p, targets: [t for t in targets if t == "uk"])  # fmt: skip
    page = {"overrides": {"b": {"translate_to": ["en", "uk"]}}}
    app = make_app(tmp_path, page=page, start=["b"])
    try:
        b = next(v for v in app.stations_view() if v["id"] == "b")
        assert b["available"] and not b["listen_only"] and b["translate_to_missing"] == ["uk"]
        assert b["reason"].startswith("Not translated into Ukrainian: m can't translate into it")
        assert app._startable("b") and "b" not in app.unavailable

        with caplog.at_level("WARNING", logger="wavescribe.app"):
            view = app.add_station({"name": "Nova", "stream": "http://example.test/nova",
                                    "translate_to": ["en", "uk"]}, start=True)  # fmt: skip
        assert view["available"] and view["translate_to_missing"] == ["uk"] and view["reason"]
        assert "station nova: Not translated into Ukrainian" in caplog.text
        view = app.update_station("nova", {"translate_to": ["en"]})
        assert view["translate_to_missing"] == [] and view["reason"] is None
        view = app.update_station("b", {"translate": False})  # not translated: nothing to lack
        assert view["translate_to_missing"] == [] and view["reason"] is None
    finally:
        app.pipeline.stop()
        app.store.close()
