# SPDX-License-Identifier: Apache-2.0
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import requires_ffmpeg, requires_say, say_to_pcm
from tests.test_segmenter import EnergyVad, silence, tone
from wavescribe.asr import Transcript
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.config import Station
from wavescribe.pipeline import Pipeline, Plan, Result, plan_models
from wavescribe.registry import ModelEntry, ModelError, ModelRegistry
from wavescribe.segment import Utterance
from wavescribe.sources import SourceHealth


def station(
    sid: str, priority: int = 5, translate_to: tuple[str, ...] = ("en",), language: str = "pt-PT"
) -> Station:
    return Station(sid, sid, language, translate_to, (), priority=priority)


class ListSource:
    def __init__(self, station_id: str, audio: np.ndarray) -> None:
        self.station_id = station_id
        self._audio = audio
        self.health = SourceHealth()

    def __iter__(self) -> Iterator[AudioChunk]:
        step = SAMPLE_RATE // 2
        for i in range(0, len(self._audio), step):
            yield AudioChunk(self._audio[i : i + step], t_end=1000.0 + i / SAMPLE_RATE,
                             discontinuity=i == 0)  # fmt: skip

    def close(self) -> None:
        pass


class FakeTranscriber:
    model_id = "fake-asr"

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        return Transcript(f"{len(samples) / SAMPLE_RATE:.0f}s", language, self.model_id)


class FakeTranslator:
    model_id = "fake-mt"
    calls = 0

    def translate(self, text: str, source: str, target: str) -> str:
        FakeTranslator.calls += 1
        return f"{target}:{text}"


ENTRY = ModelEntry("m", "x", "fake", "url:x", "u", "MIT", "u", "default", False, ("*",), "")
PLAN = Plan(vad=(ENTRY, Path()), asr={"a": (ENTRY, Path()), "b": (ENTRY, Path())},
            mt=(ENTRY, Path()))  # fmt: skip


def run_pipeline(stations: list[Station], audio: np.ndarray, plan: Plan = PLAN) -> list[Result]:
    results: list[Result] = []
    pipeline = Pipeline(
        stations,
        plan,
        results.append,
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: FakeTranslator(),
    )
    pipeline.run(duration=30)
    return results


def test_transcribes_and_translates_every_utterance() -> None:
    audio = np.concatenate([silence(1), tone(2), silence(1), tone(3), silence(1)])
    results = run_pipeline([station("a")], audio)
    assert [r.transcript.text for r in results] == ["2s", "3s"]
    assert [r.translations for r in results] == [{"en": "en:2s"}, {"en": "en:3s"}]
    for r in results:
        assert {"asr_ms", "asr_rtf", "mt_ms", "total_ms"} <= r.timings.keys()


def test_skips_translation_into_the_source_language() -> None:
    audio = np.concatenate([tone(2), silence(1)])
    results = run_pipeline([station("a", translate_to=("pt", "en"))], audio)
    assert results[0].translations == {"en": "en:2s"}


def test_station_already_in_the_target_language_skips_the_translator() -> None:
    FakeTranslator.calls = 0
    audio = np.concatenate([tone(2), silence(1)])
    results = run_pipeline([station("a", translate_to=("en",), language="en-GB")], audio)
    assert len(results) == 1
    assert results[0].translations == {}
    assert "mt_ms" not in results[0].timings  # never queued for translation
    assert FakeTranslator.calls == 0


def installed_registry(tmp_path: Path) -> ModelRegistry:
    registry = ModelRegistry(
        [ENTRY], {"vad": ["m"], "asr": ["m"], "translate": ["m"]}, models_dir=tmp_path
    )
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "x").write_bytes(b"")
    return registry


def test_the_translator_loads_only_when_something_needs_it() -> None:
    import threading

    loads: list[str] = []

    def translator(_e: ModelEntry, _p: Path) -> FakeTranslator:
        loads.append("mt")
        return FakeTranslator()

    english = station("a", language="en-US")
    results: list[Result] = []
    pipeline = Pipeline(
        [english], PLAN, results.append,
        source_factory=lambda st: ListSource(st.id, np.concatenate([tone(2), silence(1)])),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=translator,
    )  # fmt: skip
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    deadline = time.monotonic() + 10
    while not results and time.monotonic() < deadline:
        time.sleep(0.05)
    assert results and results[0].translations == {}
    assert loads == [] and pipeline.status()["models"]["mt"] is None  # English into English

    answered = threading.Event()
    assert pipeline.request_llm(lambda t: answered.set())  # a question loads it
    assert answered.wait(10)
    pipeline.stop()
    assert loads == ["mt"] and pipeline.status()["models"]["mt"] == "fake-mt"


def test_plan_resolves_a_stations_own_model_choice(tmp_path: Path) -> None:
    import dataclasses

    other = dataclasses.replace(ENTRY, id="other")
    registry = ModelRegistry(
        [ENTRY, other], {"vad": ["m"], "asr": ["m", "other"], "translate": ["m"]},
        models_dir=tmp_path,
    )  # fmt: skip
    for mid in ("m", "other"):
        (tmp_path / mid).mkdir()
        (tmp_path / mid / "x").write_bytes(b"")
    chosen = dataclasses.replace(station("b"), asr_model="other")
    far = dataclasses.replace(station("r"), remote="http://n:8766", remote_station="x")
    plan = plan_models(registry, [station("a"), chosen, far], translate=True)
    assert plan.asr["a"][0].id == "m" and plan.asr["b"][0].id == "other"
    assert "r" not in plan.asr  # another machine transcribes it


def test_translator_loads_only_after_the_asr_models() -> None:
    # Loading two backends at once races on shared imports (transformers), so loads are serial.
    loads: list[str] = []

    def slow_transcriber(_e: ModelEntry, _p: Path) -> FakeTranscriber:
        time.sleep(0.3)
        loads.append("asr")
        return FakeTranscriber()

    def translator(_e: ModelEntry, _p: Path) -> FakeTranslator:
        loads.append("mt")
        return FakeTranslator()

    Pipeline(
        [station("a")],
        PLAN,
        lambda _r: None,
        source_factory=lambda st: ListSource(st.id, np.concatenate([tone(2), silence(1)])),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=slow_transcriber,
        translator_factory=translator,
    ).run(duration=30)
    assert loads == ["asr", "mt"]


def test_without_translation() -> None:
    plan = Plan(vad=PLAN.vad, asr=PLAN.asr, mt=None)
    results = run_pipeline([station("a")], np.concatenate([tone(2), silence(1)]), plan)
    assert len(results) == 1
    assert results[0].translations == {}


class LongIsMusicGate:
    """Fake gate: utterances longer than 2.5 s are songs."""

    model_id = "fake-gate"

    def classify(self, samples: np.ndarray):
        from wavescribe.gate import decide

        song = len(samples) > 2.5 * SAMPLE_RATE
        return decide(np.array([0.0 if song else 0.9]), np.array([0.9 if song else 0.0]))


def test_gate_keeps_songs_away_from_asr() -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1), tone(3), silence(1)])
    plan = Plan(vad=PLAN.vad, asr=PLAN.asr, mt=PLAN.mt, gate=(ENTRY, Path()))
    results: list[Result] = []
    pipeline = Pipeline(
        [station("a")],
        plan,
        results.append,
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=lambda _path: EnergyVad(),
        gate_factory=lambda _path: LongIsMusicGate(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: FakeTranslator(),
    )
    pipeline.run(duration=30)
    assert [r.transcript.text for r in results] == ["2s"]
    assert pipeline.status()["music_skipped_s"] == 3


class ScriptedTranscriber:
    """Fake ASR: a loop for the 3 s utterance, Spanish for the 2 s one."""

    model_id = "fake-asr"

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        if len(samples) > 2.5 * SAMPLE_RATE:
            return Transcript("la la la la la la la la", language, self.model_id)
        return Transcript("No quiero saber nada de lo que pasó ayer.", language, self.model_id)


def test_asr_checks_drop_loops_and_flag_other_languages() -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1), tone(3), silence(1)])
    results: list[Result] = []
    filtered: list[tuple[str, str]] = []
    pipeline = Pipeline(
        [station("a")],
        PLAN,
        results.append,
        on_filtered=lambda r, reason: filtered.append((r.transcript.text, reason)),
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: ScriptedTranscriber(),
        translator_factory=lambda _e, _p: FakeTranslator(),
    )
    pipeline.run(duration=30)
    assert [r.flags for r in results] == [("lang:es",)]
    assert pipeline.status()["dropped"]["filter"] == 1
    # The loop reaches no transcript or result observer, only the one for filtered text.
    assert filtered == [("la la la la la la la la", "repetition")]


def test_two_stations() -> None:
    audio = np.concatenate([tone(2), silence(1), tone(2), silence(1)])
    results = run_pipeline([station("a"), station("b")], audio)
    assert sorted(r.station.id for r in results) == ["a", "a", "b", "b"]


def test_plan_fails_clearly_for_an_unserved_language(tmp_path: Path) -> None:
    registry = ModelRegistry([ENTRY], {"vad": ["m"], "asr": []}, models_dir=tmp_path)
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "x").write_bytes(b"")  # the VAD model is installed; no ASR serves pt
    with pytest.raises(ModelError, match="no asr model"):
        plan_models(registry, [station("a")], translate=False)


@pytest.mark.models
@requires_say
@requires_ffmpeg
def test_real_models_on_tts_portuguese(tmp_path: Path) -> None:
    registry = ModelRegistry.load()
    try:
        plan = plan_models(registry, [station("a")], translate=True)
    except ModelError as e:
        pytest.skip(str(e))
    speech = say_to_pcm(tmp_path / "pt", "O abastecimento de água na cidade foi reposto.", "Joana")
    audio = np.concatenate([silence(1), speech, silence(2)])
    results: list[Result] = []
    Pipeline(
        [station("a")], plan, results.append, source_factory=lambda st: ListSource(st.id, audio)
    ).run(duration=120)
    assert len(results) == 1
    assert "água" in results[0].transcript.text.lower()
    assert "water" in results[0].translations["en"].lower()


def test_alert_translations_jump_the_queue_and_skip_unsafe_targets() -> None:
    import dataclasses
    import threading

    unsafe = dataclasses.replace(ENTRY, unsafe_targets=("uk",))
    plan = dataclasses.replace(PLAN, mt=(unsafe, Path()))
    pipeline = Pipeline(
        [], plan, lambda r: None,
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: FakeTranslator(),
    )  # fmt: skip
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    got: list = []
    done = threading.Event()

    def callback(translations: dict, model: str | None) -> None:
        got.append((translations, model))
        done.set()

    pipeline.request_translation("Olá", "pt-PT", ["en", "uk", "pt"], callback)
    assert done.wait(10)
    pipeline.stop()
    # uk is dropped: this model was measured inventing content in Ukrainian.
    assert got == [({"en": "en:Olá"}, "fake-mt")]


def test_an_alert_is_translated_when_no_station_is() -> None:
    import threading

    # No translator at the start and no station that translates: one chosen later (on the
    # page) must still serve alerts, or each alert waits out its translation timeout.
    pipeline = Pipeline(
        [], Plan(vad=PLAN.vad, asr={}, mt=None), lambda r: None,
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: FakeTranslator(),
    )  # fmt: skip
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    assert wait_for(lambda: pipeline.ready)
    pipeline.set_translator((ENTRY, Path()))
    got: list = []
    done = threading.Event()
    pipeline.request_translation("Olá", "pt-PT", ["en"], lambda t, m: (got.append(t), done.set()))
    answered = done.wait(5)
    pipeline.stop()
    assert answered and got == [{"en": "en:Olá"}]


def wait_for(cond, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_stations_added_later_load_their_model_then_and_change_in_place() -> None:
    import dataclasses
    import threading

    whisper = dataclasses.replace(ENTRY, id="whisper")
    loads: list[str] = []

    def transcriber(entry: ModelEntry, _p: Path) -> FakeTranscriber:
        loads.append(entry.id)
        return FakeTranscriber()

    plan = Plan(vad=PLAN.vad, asr={"a": (ENTRY, Path())}, mt=PLAN.mt)
    results: list[Result] = []
    audio = np.concatenate([tone(2), silence(1)] * 3)
    pipeline = Pipeline(
        [station("a")], plan, results.append,
        source_factory=lambda st: ListSource(st.id, audio),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=transcriber,
        translator_factory=lambda _e, _p: FakeTranslator(),
    )  # fmt: skip
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    assert wait_for(lambda: len(results) == 3)
    assert loads == ["m"]  # only what the starting station needs

    with pytest.raises(ValueError):
        pipeline.add_station(station("he", language="he"))  # nothing planned for it
    pipeline.add_station(station("he", language="he", translate_to=("en", "uk")),
                         asr=(whisper, Path()))  # fmt: skip
    assert wait_for(lambda: sum(r.station.id == "he" for r in results) == 3)
    assert loads == ["m", "whisper"]
    assert results[-1].translations == {"en": "en:2s", "uk": "uk:2s"}
    status = pipeline.status()
    assert status["models"]["asr"] == ["m", "whisper"]
    assert status["lag"]["he"]["n"] == 3 and status["lag"]["he"]["text_p50"] is not None

    pipeline.update_station(station("a", translate_to=()))  # same source: changes in place
    pipeline.ingest(Result(station("a", translate_to=("uk",)),
                           Utterance("a", 1, np.zeros(1, np.float32), 1000.0, 1001.0, 1001.0),
                           Transcript("Olá", "pt-PT", "remote-asr"),
                           translations={"en": "Hello"}))  # fmt: skip
    assert wait_for(lambda: results[-1].transcript.text == "Olá")
    assert results[-1].translations == {"en": "Hello", "uk": "uk:Olá"}  # only uk added here
    pipeline.stop()


class FailsOnLongTranscriber(FakeTranscriber):
    """Fake ASR whose model raises on every utterance longer than 2.5 s."""

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        if len(samples) > 2.5 * SAMPLE_RATE:
            raise RuntimeError("the decoder ran out of memory")
        return super().transcribe(samples, language)


class NoTokenTranslator(FakeTranslator):
    """Fake MADLAD: it has no target token for a language it was never trained on."""

    def translate(self, text: str, source: str, target: str) -> str:
        if target == "xx":
            raise ValueError("MADLAD-400 has no target token for 'xx'")
        return super().translate(text, source, target)


class BrokenGate:
    model_id = "fake-gate"

    def classify(self, samples: np.ndarray):
        raise RuntimeError("interpreter failed")


def fake_pipeline(stations: list[Station], plan: Plan, on_result, source_factory, **fakes):
    factories = {
        "vad_factory": lambda _path: EnergyVad(),
        "transcriber_factory": lambda _e, _p: FakeTranscriber(),
        "translator_factory": lambda _e, _p: FakeTranslator(),
        **fakes,
    }
    return Pipeline(stations, plan, on_result, source_factory=source_factory, **factories)


def run_with(audio: np.ndarray, stations: list[Station], plan: Plan = PLAN, **fakes: object):
    results: list[Result] = []
    pipeline = fake_pipeline(
        stations, plan, results.append, lambda st: ListSource(st.id, audio), **fakes
    )
    pipeline.run(duration=30)
    return results, pipeline.status()


def test_an_utterance_the_asr_model_fails_on_costs_only_that_utterance() -> None:
    audio = np.concatenate([silence(0.5), tone(3), silence(1), tone(2), silence(1)])
    results, status = run_with(
        audio, [station("a")], transcriber_factory=lambda _e, _p: FailsOnLongTranscriber()
    )
    assert [r.transcript.text for r in results] == ["2s"]
    assert status["dropped"]["asr"] == 1 and status["models"]["errors"] == {}


def test_a_target_the_translator_cannot_do_leaves_the_other_translations() -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1), tone(3), silence(1)])
    results, status = run_with(
        audio, [station("a", translate_to=("xx", "en"))],
        translator_factory=lambda _e, _p: NoTokenTranslator(),
    )  # fmt: skip
    assert [r.translations for r in results] == [{"en": "en:2s"}, {"en": "en:3s"}]
    assert status["dropped"]["mt"] == 2


def test_a_model_that_keeps_failing_is_reported_until_it_works_again(monkeypatch) -> None:
    monkeypatch.setattr("wavescribe.pipeline.FAILURES_BEFORE_ERROR", 2)
    failing = np.concatenate([silence(0.5), tone(3), silence(1), tone(3), silence(1)])
    results, status = run_with(
        failing, [station("a"), station("b")],
        transcriber_factory=lambda _e, _p: FailsOnLongTranscriber(),
        translator_factory=lambda _e, _p: NoTokenTranslator(),
    )  # fmt: skip
    assert results == [] and status["dropped"]["asr"] == 4
    assert status["models"]["errors"]["m"].startswith("failed 2 times in a row (station ")
    assert "the decoder ran out of memory" in status["models"]["errors"]["m"]

    recovering = np.concatenate([failing, tone(2), silence(1)])
    results, status = run_with(
        recovering, [station("a", translate_to=("xx", "en"))],
        transcriber_factory=lambda _e, _p: FailsOnLongTranscriber(),
        translator_factory=lambda _e, _p: NoTokenTranslator(),
    )  # fmt: skip
    assert [r.translations for r in results] == [{"en": "en:2s"}]
    assert status["models"]["errors"] == {}  # the next utterance went through


def test_a_gate_that_fails_lets_the_utterance_through() -> None:
    audio = np.concatenate([silence(0.5), tone(2), silence(1)])
    plan = Plan(vad=PLAN.vad, asr=PLAN.asr, mt=PLAN.mt, gate=(ENTRY, Path()))
    results, status = run_with(audio, [station("a")], plan, gate_factory=lambda _p: BrokenGate())
    assert [r.transcript.text for r in results] == ["2s"]
    assert status["music_skipped_s"] == 0


def test_a_target_madlad_does_not_know_is_reported_when_planning(tmp_path: Path, caplog) -> None:
    import dataclasses
    import io

    import sentencepiece

    vocabulary = io.BytesIO()
    sentencepiece.SentencePieceTrainer.train(
        sentence_iterator=iter(["corte de água amanhã", "water cut tomorrow"] * 20),
        model_writer=vocabulary, vocab_size=40, hard_vocab_limit=False,
        user_defined_symbols=["<2en>", "<2pt>", "<2zh_Hant>"],
    )  # fmt: skip
    folder = tmp_path / "madlad"
    folder.mkdir()
    (folder / "spiece.model").write_bytes(vocabulary.getvalue())
    madlad = dataclasses.replace(
        ENTRY, id="madlad", backend="ctranslate2-madlad", source=f"local:{folder}"
    )

    from wavescribe.translate import unsupported_targets

    assert unsupported_targets(madlad, folder, ["en-GB", "zh-Hant", "xx", "tlh"]) == ["xx", "tlh"]
    (tmp_path / "m").mkdir()
    (tmp_path / "m" / "x").write_bytes(b"")
    registry = ModelRegistry(
        [ENTRY, madlad], {"vad": ["m"], "asr": ["m"], "translate": ["madlad"]}, models_dir=tmp_path
    )
    with caplog.at_level("WARNING", logger="wavescribe.pipeline"):
        plan_models(registry, [station("a", translate_to=("en", "xx"))], translate=True)
    assert "station a: madlad can't translate into xx" in caplog.text


def run_in_background(pipeline: Pipeline) -> list[BaseException]:
    """Run until stopped; returns the list that gets what ended the run, if anything does."""
    crashed: list[BaseException] = []

    def run() -> None:
        try:
            pipeline.run(until_idle=False)
        except Exception as e:
            crashed.append(e)

    threading.Thread(target=run, daemon=True).start()
    return crashed


class FailingSource(ListSource):
    """A stream whose decoder raises as soon as it is opened, every time."""

    def __init__(self, station_id: str) -> None:
        super().__init__(station_id, silence(1))
        self.opened = 0

    def __iter__(self) -> Iterator[AudioChunk]:
        self.opened += 1
        raise RuntimeError("invalid data found when processing input")


class HeldSource(ListSource):
    """Plays its audio, then stays connected and silent until it is closed."""

    def __init__(self, station_id: str, audio: np.ndarray) -> None:
        super().__init__(station_id, audio)
        self._closed = threading.Event()

    def __iter__(self) -> Iterator[AudioChunk]:
        yield from super().__iter__()
        self._closed.wait()

    def close(self) -> None:
        self._closed.set()


def test_a_station_that_keeps_failing_is_retried_while_the_others_go_on(monkeypatch) -> None:
    monkeypatch.setattr("wavescribe.pipeline.RESTART_INITIAL_S", 0.01)
    failing = FailingSource("b")

    class AfterEachRestart(ListSource):
        """Station a speaks once each time b has been opened again."""

        def __iter__(self) -> Iterator[AudioChunk]:
            for opened in (2, 3, 4):
                wait_for(lambda want=opened: failing.opened >= want)
                yield from super().__iter__()

    sources = {"a": AfterEachRestart("a", np.concatenate([tone(2), silence(1)])), "b": failing}
    results: list[Result] = []
    pipeline = fake_pipeline(
        [station("a"), station("b")], PLAN, results.append, lambda st: sources[st.id]
    )
    crashed = run_in_background(pipeline)
    assert wait_for(lambda: len(results) == 3)
    b = pipeline.status()["stations"]["b"]
    pipeline.stop()
    assert crashed == [] and [r.station.id for r in results] == ["a", "a", "a"]
    assert b["error"] == "invalid data found when processing input" and b["retry_in_s"] >= 0
    assert not b["connected"] and b["reconnects"] >= 3


def test_a_source_that_retries_on_its_own_says_why_it_is_down() -> None:
    class Retrying(HeldSource):
        """Connects to nothing: a stream retrying in its own loop, as StreamSource does."""

        def __iter__(self) -> Iterator[AudioChunk]:
            self.health.error = "Connection refused; check the address"
            self.health.retry_at = time.monotonic() + 30
            self._closed.wait()
            yield from ()

    pipeline = fake_pipeline([station("a")], PLAN, lambda _r: None,
                             lambda st: Retrying(st.id, silence(1)))  # fmt: skip
    crashed = run_in_background(pipeline)
    assert wait_for(lambda: "error" in pipeline.status()["stations"].get("a", {}))
    a = pipeline.status()["stations"]["a"]
    pipeline.stop()
    assert crashed == [] and not a["connected"]
    assert a["error"] == "Connection refused; check the address" and 25 < a["retry_in_s"] <= 30


class BrokenVad(EnergyVad):
    def __call__(self, frame: np.ndarray) -> float:
        raise IndexError("index 512 is out of bounds for axis 0 with size 448")


def test_a_vad_that_raises_restarts_its_station_with_a_new_one(monkeypatch, caplog) -> None:
    monkeypatch.setattr("wavescribe.pipeline.RESTART_INITIAL_S", 0.01)
    made: list[EnergyVad] = []

    def vad(_path: Path) -> EnergyVad:
        made.append(BrokenVad() if len(made) < 3 else EnergyVad())
        return made[-1]

    with caplog.at_level("ERROR", logger="wavescribe.pipeline"):
        results, _ = run_with(
            np.concatenate([tone(2), silence(1)]), [station("a")], vad_factory=vad
        )
    assert [r.transcript.text for r in results] == ["2s"]  # heard by the fourth VAD
    failed = [r for r in caplog.records if r.getMessage() == "station_failed"]
    logged = [(r.fields["in_a_row"], bool(r.exc_info)) for r in failed]
    assert logged == [(1, True), (2, False), (3, False)]  # one traceback, then one line each


def test_a_station_added_later_whose_gate_will_not_load_is_retried(monkeypatch) -> None:
    monkeypatch.setattr("wavescribe.pipeline.RESTART_INITIAL_S", 0.01)
    monkeypatch.setattr("wavescribe.pipeline.RESTART_MAX_S", 0.05)
    truncated = threading.Event()
    truncated.set()

    def gate(_path: Path) -> LongIsMusicGate:
        if truncated.is_set():
            raise RuntimeError("the gate's model file is truncated")
        return LongIsMusicGate()

    results: list[Result] = []
    pipeline = fake_pipeline(
        [],
        Plan(vad=PLAN.vad, asr={}, mt=PLAN.mt, gate=(ENTRY, Path())),
        results.append,
        lambda st: HeldSource(st.id, np.concatenate([tone(2), silence(1)])),
        gate_factory=gate,
    )
    crashed = run_in_background(pipeline)
    assert wait_for(lambda: pipeline.ready)
    pipeline.add_station(station("a"), asr=(ENTRY, Path()))
    assert wait_for(lambda: "error" in pipeline.status()["stations"].get("a", {}))
    assert pipeline.status()["stations"]["a"]["error"] == "the gate's model file is truncated"
    truncated.clear()
    assert wait_for(lambda: len(results) == 1)
    a = pipeline.status()["stations"]["a"]
    pipeline.stop()
    assert crashed == [] and results[0].transcript.text == "2s"
    assert "error" not in a and a["reconnects"] >= 1  # it hears audio again


def test_a_station_waiting_to_restart_stops_at_once(monkeypatch) -> None:
    monkeypatch.setattr("wavescribe.pipeline.RESTART_INITIAL_S", 60.0)
    sources = {"a": FailingSource("a"), "b": FailingSource("b")}
    pipeline = fake_pipeline(
        [station("a"), station("b")], PLAN, lambda _r: None, lambda st: sources[st.id]
    )
    pipeline.start()
    assert wait_for(lambda: all("error" in pipeline.status()["stations"].get(s, {}) for s in "ab"))
    t0 = time.monotonic()
    pipeline.remove_station("a", wait=5)
    assert time.monotonic() - t0 < 2 and list(pipeline.status()["stations"]) == ["b"]
    t0 = time.monotonic()
    pipeline.stop(drain=True)  # joins every worker without a timeout
    assert time.monotonic() - t0 < 2 and sources["b"].opened == 1


class SlowToClose(HeldSource):
    """Like a stream whose ffmpeg takes a moment to exit once it is told to."""

    def __init__(self, station_id: str, linger_s: float) -> None:
        super().__init__(station_id, silence(1))
        self.linger_s = linger_s

    def __iter__(self) -> Iterator[AudioChunk]:
        yield from super().__iter__()
        time.sleep(self.linger_s)


def test_a_station_started_again_while_it_stops_runs_once_the_stop_is_done() -> None:
    opened: list[SlowToClose] = []

    def source(st: Station) -> SlowToClose:
        opened.append(SlowToClose(st.id, 0.4))
        return opened[-1]

    pipeline = fake_pipeline([station("a")], PLAN, lambda _r: None, source)
    pipeline.start()
    assert wait_for(lambda: len(opened) == 1 and "a" in pipeline.status()["stations"])
    pipeline.remove_station("a")  # as the page's Stop does: it doesn't wait
    pipeline.add_station(station("a"))  # Start, at once
    time.sleep(0.6)  # the first source has let go by now
    a = pipeline.status()["stations"].get("a")
    pipeline.stop()
    assert len(opened) == 2 and a is not None and not a["stopping"]


def test_a_station_that_takes_too_long_to_stop_says_so_when_started(monkeypatch) -> None:
    from wavescribe.pipeline import StationStopping

    monkeypatch.setattr("wavescribe.pipeline.STOP_WAIT_S", 0.1)
    pipeline = fake_pipeline(
        [station("a")], PLAN, lambda _r: None, lambda st: SlowToClose(st.id, 2.0)
    )
    pipeline.start()
    assert wait_for(lambda: "a" in pipeline.status()["stations"])
    pipeline.remove_station("a")
    with pytest.raises(StationStopping, match="a is still stopping: try again in a few seconds"):
        pipeline.add_station(station("a"))
    pipeline.stop()


def test_a_source_that_exits_still_ends_the_run() -> None:
    class Exits(ListSource):
        def __iter__(self) -> Iterator[AudioChunk]:
            raise SystemExit(2)

    pipeline = fake_pipeline(
        [station("a")], PLAN, lambda _r: None, lambda st: Exits(st.id, silence(1))
    )
    with pytest.raises(RuntimeError, match="a pipeline worker failed"):
        pipeline.run(duration=10, until_idle=False)


def test_stop_from_another_thread_ends_the_run() -> None:
    # A server runs the pipeline with until_idle=False; stopping it must end run() too, not
    # leave its loop polling forever.
    pipeline = fake_pipeline(
        [station("a")], PLAN, lambda _r: None, lambda st: ListSource(st.id, silence(1))
    )
    runner = threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True)
    runner.start()
    assert wait_for(lambda: pipeline.ready)
    pipeline.stop()
    runner.join(timeout=5)
    assert not runner.is_alive()


def test_a_drained_stop_ends_even_when_the_asr_worker_crashed(monkeypatch) -> None:
    """The ASR worker tells the translation worker to finish however it ends: a drained stop
    joins every worker without a timeout, so a crash must not leave one waiting forever."""
    calls = []
    real = Pipeline._worked

    def crash_on_the_second_line(self, *args, **kwargs):
        if threading.current_thread().name == "asr":
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("a bug in the ASR worker")
        return real(self, *args, **kwargs)

    def load_after_the_crash(_e, _p) -> FakeTranslator:
        # The first line still waits for translation when the ASR worker has ended: it must
        # be translated and kept all the same, not lost behind an immediate stop marker.
        wait_for(lambda: len(calls) == 2 and not pipeline._alive("asr"))
        return FakeTranslator()

    monkeypatch.setattr(Pipeline, "_worked", crash_on_the_second_line)
    results: list[Result] = []
    audio = np.concatenate([tone(2), silence(1), tone(3), silence(1)])
    pipeline = fake_pipeline(
        [station("a")], PLAN, results.append, lambda st: HeldSource(st.id, audio),
        translator_factory=load_after_the_crash,
    )  # fmt: skip
    pipeline.start()
    assert wait_for(lambda: len(calls) == 2)  # the first line went to translation, then a crash
    done = threading.Event()
    threading.Thread(target=lambda: (pipeline.stop(drain=True), done.set()), daemon=True).start()
    assert done.wait(15), "stop(drain=True) waited forever on the translation worker"
    assert [r.transcript.text for r in results] == ["2s"]


# -- overload: what falls behind, what is dropped, and what is checked first --------------------


class GatedTranslator(FakeTranslator):
    """Translates once ``go`` is set, so the translation worker stays busy until then."""

    def __init__(self, go: threading.Event, done: list) -> None:
        self.go, self.done = go, done

    def translate(self, text: str, source: str, target: str) -> str:
        self.go.wait(10)
        self.done.append(text)
        return f"{target}:{text}"


def line(st: Station, text: str, waited_s: float = 0.0) -> Result:
    """A transcript as ``Pipeline.ingest`` takes it, its text ready ``waited_s`` ago."""
    now = time.time()
    u = Utterance(st.id, 1, np.zeros(1, np.float32), now - 2, now - 1, now - 1 - waited_s)
    r = Result(st, u, Transcript(text, st.language, "remote-asr"))
    r.asr_done_at = now - waited_s
    return r


def busy(monkeypatch, size: int, results: list, *stations: Station):
    """A running pipeline with room for ``size`` jobs and a translator that waits for ``go``."""
    monkeypatch.setattr("wavescribe.pipeline.MT_QUEUE_MAX", size)
    go, done = threading.Event(), []
    pipeline = fake_pipeline(
        list(stations), PLAN, results.append, lambda st: HeldSource(st.id, silence(0.5)),
        translator_factory=lambda _e, _p: GatedTranslator(go, done),
    )  # fmt: skip
    run_in_background(pipeline)
    assert wait_for(lambda: pipeline.ready and set(pipeline.status()["stations"]) >= {
        s.id for s in stations})  # fmt: skip
    return pipeline, go, done


def test_rule_checks_and_alert_translations_get_in_ahead_of_a_full_backlog(monkeypatch) -> None:
    # Seen in testing: with five stations translated on a loaded machine the queue stayed full
    # of station lines, and LLM rule checks were turned away: possibly missed alerts.
    a = station("a")
    results: list[Result] = []
    pipeline, go, done = busy(monkeypatch, 4, results, a)
    pipeline.ingest(line(a, "first"))
    assert wait_for(lambda: pipeline.status()["mt_queue"] == 0)  # being translated, held
    for i in range(5):
        pipeline.ingest(line(a, f"queued {i}"))  # the fifth finds no room
    order: list[str] = []
    assert pipeline.request_llm(lambda _t: order.append("rules"), "llm-rules", 0.3, station="a")
    got = threading.Event()
    pipeline.request_translation("Olá", "pt-PT", ["en"],
                                 lambda t, _m: (order.append(t["en"]), got.set()))  # fmt: skip
    status = pipeline.status()
    # Two station lines made room; with the one turned away, three went untranslated.
    assert status["stations"]["a"]["dropped"] == {"asr": 0, "mt": 3, "rules": 0}
    assert status["dropped"]["mt"] == 3 and status["dropped"]["rules"] == 0
    assert status["load"]["state"] == "behind"
    assert "untranslated" in status["load"]["reason"]
    untranslated = [r.transcript.text for r in results if not r.translations]
    assert untranslated == ["queued 4", "queued 3", "queued 2"]  # the newest go first
    go.set()
    assert got.wait(10) and wait_for(lambda: len(results) == 6)
    pipeline.stop()
    assert order == ["en:Olá", "rules"]  # the alert's translation first, then the rule check
    assert done[:2] == ["first", "Olá"] and done[2:] == ["queued 0", "queued 1"]


def test_rule_checks_that_cannot_run_are_counted_and_logged_once(monkeypatch, caplog) -> None:
    a = station("a")
    pipeline, go, _ = busy(monkeypatch, 2, [], a)
    pipeline.ingest(line(a, "first"))
    assert wait_for(lambda: pipeline.status()["mt_queue"] == 0)
    with caplog.at_level("WARNING", logger="wavescribe.pipeline"):
        queued = [pipeline.request_llm(lambda _t: None, "llm-rules", 0.3, station="a")
                  for _ in range(12)]  # fmt: skip
    assert queued == [True, True] + [False] * 10  # other rule checks fill the queue: no room
    status = pipeline.status()
    assert status["stations"]["a"]["dropped"]["rules"] == 10 and status["dropped"]["rules"] == 10
    assert status["load"]["state"] == "overloaded"
    assert "10 LLM rule checks" in status["load"]["reason"]
    assert "alerts may be missed" in status["load"]["reason"]
    # The first is logged at once, the next ones a minute later, all in one line. (Another
    # test's pipeline may still be logging its own drops as it ends.)
    drops = [r.fields for r in caplog.records
             if r.getMessage() == "drop" and r.fields["stage"] == "rules"]  # fmt: skip
    assert len(drops) == 1 and drops[0]["n"] == 1 and drops[0]["stations"] == "a:1"
    go.set()
    pipeline.stop()


def test_translations_too_late_to_be_useful_are_skipped_and_counted(monkeypatch) -> None:
    a, b = station("a", priority=1), station("b", priority=9)
    results: list[Result] = []
    pipeline, go, done = busy(monkeypatch, 10, results, a, b)
    pipeline.ingest(line(a, "busy"))
    assert wait_for(lambda: pipeline.status()["mt_queue"] == 0)
    pipeline.ingest(line(b, "six minutes old", waited_s=360))
    pipeline.ingest(line(b, "two minutes old", waited_s=130))
    status = pipeline.status()
    assert status["stations"]["b"]["behind_s"]["translation"] >= 360
    assert status["stations"]["b"]["behind_s"]["text"] is None  # no speech waits for text
    assert status["stations"]["a"]["behind_s"]["translation"] < 5  # the one being translated
    assert status["load"]["state"] == "overloaded"
    assert status["load"]["reason"].startswith("Too many stations for this ")
    assert "translation is 6 minutes behind" in status["load"]["reason"]
    go.set()
    assert wait_for(lambda: len(results) == 3)
    pipeline.stop()
    assert "six minutes old" not in done and "two minutes old" in done
    stale = next(r for r in results if r.transcript.text == "six minutes old")
    assert stale.translations == {}  # stored and shown untranslated
    assert pipeline.status()["dropped"]["mt"] == 1


def test_text_that_falls_behind_shows_in_the_status(monkeypatch) -> None:
    monkeypatch.setattr("wavescribe.pipeline.TEXT_BEHIND_S", 0.2)
    go = threading.Event()

    class Held(FakeTranscriber):
        def transcribe(self, samples, language):
            go.wait(10)
            return super().transcribe(samples, language)

    audio = np.concatenate([tone(2), silence(1), tone(2), silence(1)])
    pipeline = fake_pipeline([station("a")], PLAN, lambda _r: None,
                             lambda st: HeldSource(st.id, audio),
                             transcriber_factory=lambda _e, _p: Held())  # fmt: skip
    run_in_background(pipeline)
    assert wait_for(lambda: pipeline.status()["asr_queue"] == 1)  # one held, one waiting
    time.sleep(0.3)
    status = pipeline.status()
    assert status["stations"]["a"]["behind_s"]["text"] >= 0.3
    assert status["load"] == {"state": "behind", "reason": status["load"]["reason"]}
    assert status["load"]["reason"].startswith("Text is ") and "behind on a" in status["load"][
        "reason"]  # fmt: skip
    go.set()
    assert wait_for(lambda: pipeline.status()["load"]["state"] == "ok")
    assert pipeline.status()["stations"]["a"]["behind_s"] == {"text": None, "translation": None}
    pipeline.stop()


def test_lines_waiting_for_translation_follow_a_change_of_languages(monkeypatch) -> None:
    # Lines queued before a translation language is removed are translated into what the
    # station still wants: not into the removed one, and not left without any.
    import dataclasses

    a, b = station("a", translate_to=("en", "fr")), station("b", translate_to=("en", "de"))
    results: list[Result] = []
    pipeline, go, done = busy(monkeypatch, 10, results, a, b)
    pipeline.ingest(line(a, "busy"))
    assert wait_for(lambda: pipeline.status()["mt_queue"] == 0)
    pipeline.ingest(line(a, "queued a"))
    pipeline.ingest(line(b, "queued b"))
    pipeline.update_station(dataclasses.replace(a, translate_to=("en",)))
    pipeline.update_station(dataclasses.replace(b, translate=False))  # text only, from now on
    go.set()
    assert wait_for(lambda: len(results) == 3)
    pipeline.stop()
    got = {r.transcript.text: r.translations for r in results}
    assert got["busy"] == {"en": "en:busy", "fr": "fr:busy"}  # already being translated
    assert got["queued a"] == {"en": "en:queued a"}
    assert got["queued b"] == {}
    assert done == ["busy", "busy", "queued a"]
    assert pipeline.status()["dropped"]["mt"] == 0  # nothing was dropped: nothing was wanted


def test_a_line_of_a_station_that_hears_any_language_keeps_the_language_heard(monkeypatch):
    # Such a station's lines carry the language each was heard in; translated as the station
    # is now, a line already in a wanted language must not be translated into it again.
    import dataclasses

    auto = station("a", language="auto", translate_to=("en", "fr"))
    results: list[Result] = []
    pipeline, go, _ = busy(monkeypatch, 10, results, auto)
    pipeline.ingest(line(dataclasses.replace(auto, language="es"), "busy"))
    assert wait_for(lambda: pipeline.status()["mt_queue"] == 0)
    pipeline.ingest(line(dataclasses.replace(auto, language="en"), "already English"))
    go.set()
    assert wait_for(lambda: len(results) == 2)
    pipeline.stop()
    english = next(r for r in results if r.transcript.text == "already English")
    assert english.translations == {"fr": "fr:already English"}
