# SPDX-License-Identifier: Apache-2.0
"""Whisper's second opinion on lines heard in another language (asr/recheck.py), with fakes."""

import logging
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tests.test_alerts import CONFIG
from tests.test_pipeline import FakeTranslator, ListSource, station
from tests.test_rules import pack
from tests.test_segmenter import EnergyVad, silence, tone
from wavescribe.alerts import Alerter, parse_alerts
from wavescribe.asr import Transcript
from wavescribe.asr.recheck import FLAG, Rechecker
from wavescribe.audio import SAMPLE_RATE
from wavescribe.pipeline import Pipeline, Plan, Result, fan_out, plan_available
from wavescribe.registry import ModelEntry, ModelRegistry
from wavescribe.store import Store

# What the first model wrote for each utterance, by its length in seconds. The 2 s one is the
# failure this is for: the station speaks Portuguese, the model answered in English (and the
# Portuguese pack's "sabotagem" stems like "sabotage").
HEARD = {
    2: "They said there was nuclear sabotage at the plant near the river yesterday.",
    3: "O governo aprovou hoje o orçamento para o próximo ano.",
    4: "Corte de água amanhã de manhã em vários bairros da cidade.",
}
# What Whisper writes when told the language is Portuguese.
WHISPER_PT = {2: "Corte de água em Vila Nova amanhã de manhã, disse a câmara."}


def entry(model_id: str, languages: tuple[str, ...] = ("*",)) -> ModelEntry:
    return ModelEntry(
        model_id,
        "asr",
        "fake",
        f"url:https://x/{model_id}.bin",
        "u",
        "MIT",
        "u",
        "default",
        False,
        languages,
        "",
    )


VAD = ModelEntry(
    "vad", "vad", "fake", "url:https://x/vad.bin", "u", "MIT", "u", "default", False, ("*",), ""
)
FIRST = entry("first-asr", ("pt", "en"))
WHISPER = entry("whisper-fake", ("pt", "en", "uk"))


def registry(tmp_path: Path, *, whisper: bool = True) -> ModelRegistry:
    """A registry with a first ASR model and a Whisper, installed (or the Whisper not)."""
    reg = ModelRegistry(
        [VAD, FIRST, WHISPER],
        {"vad": ["vad"], "asr": ["first-asr", "whisper-fake"]},
        models_dir=tmp_path / "models",
    )
    for e in (VAD, FIRST, WHISPER) if whisper else (VAD, FIRST):
        (tmp_path / "models" / e.id).mkdir(parents=True)
        (tmp_path / "models" / e.id / f"{e.id}.bin").write_bytes(b"")
    return reg


def seconds(samples: np.ndarray) -> int:
    return round(len(samples) / SAMPLE_RATE)


class Calls:
    def __init__(self) -> None:
        self.log: list[str] = []
        self.lock = threading.Lock()

    def add(self, what: str) -> None:
        with self.lock:
            self.log.append(what)


class FirstModel:
    """The station's model: picks the language itself, so it can't be told Portuguese."""

    model_id = FIRST.id

    def __init__(self, calls: Calls, delay: float = 0.0) -> None:
        self.calls, self.delay = calls, delay

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        n = seconds(samples)
        if n and self.delay:
            time.sleep(self.delay)  # let the source queue every utterance behind the first
            self.delay = 0.0
        self.calls.add(f"asr:{n}")
        return Transcript(HEARD.get(n, ""), language, self.model_id)


class FakeWhisper:
    model_id = WHISPER.id

    def __init__(self, calls: Calls, heard: str = "pt", text: dict[int, str] | None = None):
        self.calls, self.heard = calls, heard
        self.text = WHISPER_PT if text is None else text

    def identify_language(self, samples: np.ndarray) -> dict[str, float]:
        self.calls.add(f"lid:{seconds(samples)}")
        return {self.heard: 0.97, "es": 0.02, "en" if self.heard != "en" else "pt": 0.01}

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        n = seconds(samples)
        self.calls.add(f"whisper:{n}:{language}")
        return Transcript(self.text.get(n, ""), language, self.model_id)


class Run:
    """A pipeline over synthetic utterances with the fakes above, and what it reported."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        lengths: tuple[int, ...] = (2, 3),
        whisper: FakeWhisper | None = None,
        rechecker: Rechecker | None = None,
        installed: bool = True,
        delay: float = 0.0,
        alerts: bool = False,
        with_store: bool = False,
        translate_to: tuple[str, ...] = ("en",),
    ) -> None:
        self.calls = Calls()
        self.transcripts: list[tuple[str, tuple[str, ...], bool]] = []
        self.rechecked: list[tuple[str, tuple[str, ...], str]] = []
        self.results: list[Result] = []
        reg = registry(tmp_path, whisper=installed)
        st = station("a", translate_to=translate_to)
        plan, _ = plan_available(reg, [st], translate=False)
        if rechecker is not None:
            rechecker._registry = reg
            plan.recheck = rechecker
        plan.mt = (FIRST, Path())  # any entry: the translator is a fake
        self.store = Store(tmp_path / "w.db") if with_store else None
        self.alerter = None
        if alerts:
            config = parse_alerts(CONFIG, None, pack(tmp_path))
            self.alerter = Alerter(config, actions=_NoActions(), store=self.store)
        whisper = whisper or FakeWhisper(self.calls)
        whisper.calls = self.calls
        models = {FIRST.id: FirstModel(self.calls, delay), WHISPER.id: whisper}
        audio = np.concatenate([np.concatenate([tone(n), silence(1)]) for n in lengths])
        self.loaded: list[str] = []

        def load(e: ModelEntry, _p: Path):
            self.loaded.append(e.id)
            return models[e.id]

        pipeline = Pipeline(
            [st],
            plan,
            fan_out(self.store and self.store.add_translations, self.results.append),
            on_transcript=fan_out(
                self.store and self.store.add_transcript,
                self.alerter and self.alerter.on_transcript,
                self._transcript,
            ),
            on_rechecked=fan_out(
                self.store and self.store.update_transcript,
                self.alerter and self.alerter.on_transcript,
                self._rechecked,
            ),
            source_factory=lambda s: ListSource(s.id, audio),
            vad_factory=lambda _p: EnergyVad(),
            transcriber_factory=load,
            translator_factory=lambda _e, _p: FakeTranslator(),
        )
        pipeline.run(duration=30)
        self.status = pipeline.status()
        if self.alerter is not None:
            self.alerter.drain()

    def _transcript(self, r: Result) -> None:
        self.transcripts.append((r.transcript.text, r.flags, r.rechecking))

    def _rechecked(self, r: Result) -> None:
        self.rechecked.append((r.transcript.text, r.flags, r.transcript.model))

    def final(self, n: int) -> Result:
        [r] = [r for r in self.results if round(r.utterance.duration) == n]
        return r


class _NoActions:
    def notify(self, title: str, body: str) -> None:
        pass

    def speak(self, voice: str, text: str) -> None:
        pass

    def voices(self) -> set[str]:
        return {"Lesya"}


def test_a_line_whisper_hears_in_the_stations_language_is_replaced(tmp_path: Path) -> None:
    run = Run(tmp_path)
    # Shown at once as heard, dimmed, while Whisper listens again...
    assert run.transcripts[0] == (HEARD[2], ("lang:en",), True)
    # ...then replaced by Whisper's Portuguese, which passed the checks.
    assert run.rechecked == [(WHISPER_PT[2], (FLAG,), WHISPER.id)]
    final = run.final(2)
    assert (final.transcript.text, final.flags, final.transcript.model) == (
        WHISPER_PT[2],
        (FLAG,),
        WHISPER.id,
    )
    # Translated once, from the final text.
    assert final.translations == {"en": f"en:{WHISPER_PT[2]}"}
    assert "whisper:2:pt-PT" in run.calls.log and "recheck_ms" in final.timings
    # The line in the station's language needed no second opinion.
    assert run.final(3).flags == () and "lid:3" not in run.calls.log
    assert run.status["rechecks"] == {"waiting": 0, "replaced": 1, "kept": 0, "skipped": 0}


def test_a_line_whisper_hears_in_another_language_is_kept_as_it_was(tmp_path: Path) -> None:
    run = Run(tmp_path, whisper=FakeWhisper(Calls(), heard="en"))
    final = run.final(2)
    assert (final.transcript.text, final.transcript.model) == (HEARD[2], FIRST.id)
    assert final.flags == ("lang:en", FLAG)  # still dimmed, and known to be checked
    assert final.translations == {"en": f"en:{HEARD[2]}"}
    assert not any(c.startswith("whisper:") for c in run.calls.log)  # identified, no more
    assert run.status["rechecks"]["kept"] == 1


@pytest.mark.parametrize(
    "text",
    ["", "Thanks for watching!", "They said there was a problem at the plant near the river."],
)
def test_whisper_text_that_fails_the_checks_keeps_the_original(tmp_path: Path, text: str) -> None:
    run = Run(tmp_path, whisper=FakeWhisper(Calls(), text={2: text}))
    final = run.final(2)
    assert (final.transcript.text, final.flags) == (HEARD[2], ("lang:en", FLAG))
    assert "whisper:2:pt-PT" in run.calls.log


def test_rechecks_wait_behind_every_live_utterance(tmp_path: Path) -> None:
    # The first model is slow on the first utterance, so the others are queued by then.
    run = Run(tmp_path, lengths=(2, 3, 4, 3), delay=0.5)
    log = [c for c in run.calls.log if not c.endswith(":1") and c != "asr:0"]
    assert log == ["asr:2", "asr:3", "asr:4", "asr:3", "lid:2", "whisper:2:pt-PT"]


def test_waiting_rechecks_are_bounded_and_overflow_stays_flagged(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="wavescribe.asr.recheck")
    run = Run(tmp_path, lengths=(2, 2, 2), delay=0.5, rechecker=Rechecker(None, max_per_station=1))  # type: ignore[arg-type]
    flags = sorted(r.flags for r in run.results)
    assert flags == [("lang:en",), ("lang:en",), (FLAG,)]  # one re-checked, two kept flagged
    skipped = [r for r in caplog.records if r.getMessage() == "recheck_skipped"]
    assert [r.fields["reason"] for r in skipped] == ["full", "full"]  # type: ignore[attr-defined]
    assert run.status["rechecks"]["skipped"] == 2 and run.status["rechecks"]["waiting"] == 0


def test_a_recheck_that_waited_too_long_is_skipped(tmp_path: Path) -> None:
    run = Run(tmp_path, rechecker=Rechecker(None, max_wait_s=-1))  # type: ignore[arg-type]
    assert run.final(2).flags == ("lang:en",)
    assert not any(c.startswith(("lid:", "whisper:")) for c in run.calls.log)


def test_without_whisper_nothing_changes_but_one_line_at_start(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wavescribe.asr.recheck")
    run = Run(tmp_path, installed=False)
    assert run.final(2).flags == ("lang:en",) and run.rechecked == []
    assert run.transcripts[0] == (HEARD[2], ("lang:en",), False)
    assert run.loaded == [FIRST.id]
    [line] = [r.getMessage() for r in caplog.records if r.name == "wavescribe.asr.recheck"]
    assert "whisper-fake" in line and "models fetch whisper-fake" in line


def test_whisper_loads_with_the_station_before_its_audio(tmp_path: Path) -> None:
    run = Run(tmp_path, lengths=(3,))
    assert run.loaded == [FIRST.id, WHISPER.id]  # at the start, never while text waits


def test_a_station_transcribed_by_whisper_is_not_rechecked(tmp_path: Path) -> None:
    rechecker = Rechecker(registry(tmp_path))
    assert rechecker.model_for(station("a"), WHISPER) is None
    assert rechecker.model_for(station("a"), FIRST) == (
        WHISPER,
        tmp_path / "models" / WHISPER.id / "whisper-fake.bin",
    )
    assert rechecker.model_for(station("b", language="ja"), FIRST) is None  # not covered


def test_rules_read_a_rechecked_line_once_on_its_final_text(tmp_path: Path) -> None:
    run = Run(tmp_path, alerts=True, with_store=True)
    alerts = run.store.alerts(limit=10)
    # Whisper's Portuguese names a water cut in a local place: announced at full severity.
    # The English rendering's "sabotage" never became an alert of its own.
    assert [(a["rule"], a["severity"], a["mentions"]) for a in alerts] == [("water", "critical", 1)]
    [row] = [u for u in run.store.recent("a") if u["text"] == WHISPER_PT[2]]
    assert row["flags"] == [FLAG]
    assert run.store.search("Vila Nova")  # the index follows the new text


def test_rules_read_a_kept_line_once_and_keep_it_low(tmp_path: Path) -> None:
    run = Run(tmp_path, whisper=FakeWhisper(Calls(), heard="en"), alerts=True, with_store=True)
    alerts = run.store.alerts(limit=10)
    assert [(a["rule"], a["severity"], a["mentions"]) for a in alerts] == [("conflict", "low", 1)]
    [row] = [u for u in run.store.recent("a") if u["text"] == HEARD[2]]
    assert row["flags"] == ["lang:en", FLAG]


def test_a_pipeline_whose_owner_ignores_rechecks_gets_none(tmp_path: Path) -> None:
    reg = registry(tmp_path)
    plan, _ = plan_available(reg, [station("a")], translate=False)
    results: list[Result] = []
    calls = Calls()
    audio = np.concatenate([tone(2), silence(1)])
    Pipeline(
        [station("a", translate_to=())],
        plan,
        results.append,
        source_factory=lambda s: ListSource(s.id, audio),
        vad_factory=lambda _p: EnergyVad(),
        transcriber_factory=lambda e, _p: (
            FirstModel(calls) if e.id == FIRST.id else FakeWhisper(calls)
        ),
    ).run(duration=30)
    assert [r.flags for r in results] == [("lang:en",)]
    assert not any(c.startswith("lid:") for c in calls.log)


def test_plan_without_a_registry_whisper_says_nothing(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wavescribe.asr.recheck")
    reg = ModelRegistry([VAD, FIRST], {"vad": ["vad"], "asr": ["first-asr"]}, models_dir=tmp_path)
    assert isinstance(Rechecker.plan(reg, [station("a")], {"a": (FIRST, Path())}), Rechecker)
    # No Whisper registered for the language: nothing to fetch, nothing said.
    assert [r for r in caplog.records if r.name == "wavescribe.asr.recheck"] == []
    plan = Plan(vad=None, asr={}, mt=None)  # built by hand (tests): no re-checks
    assert plan.recheck is None


def test_the_feed_streams_a_line_that_finished_after_a_later_one(tmp_path: Path) -> None:
    """A re-checked line reaches on_result after lines that came later; the live feed must
    still send it, and its event id must not move a resuming client backwards."""
    import http.client
    import json

    from tests.test_app import Running, make_app

    r = Running(make_app(tmp_path))
    try:
        conn = http.client.HTTPConnection(*r.server.server_address, timeout=15)
        conn.request("GET", "/feed/v1/events?events=utterance")
        resp = conn.getresponse()
        assert resp.readline().startswith(b"retry:")
        base = {
            "station": "a",
            "privacy": "broadcast",
            "final": True,
            "t_start": 1000.0,
            "duration": 2.0,
            "language": "pt-PT",
            "translations": {},
            "hits": [],
        }
        r.app.hub.publish("utterance", {**base, "id": 12, "text": "Mais tarde.", "flags": []})
        r.app.hub.publish("utterance", {**base, "id": 10, "text": WHISPER_PT[2], "flags": [FLAG]})
        events: list[tuple[str, dict]] = []
        while len(events) < 2:
            line = resp.readline().decode()
            if line.startswith("id:"):
                event_id = line.split(":", 1)[1].strip()
                assert resp.readline().startswith(b"event: utterance")
                data = json.loads(resp.readline().decode().split(":", 1)[1])
                events.append((event_id, data))
        conn.close()
        assert [(i, d["id"], d["flags"]) for i, d in events] == [("12", 12, []), ("12", 10, [FLAG])]
    finally:
        r.close()
