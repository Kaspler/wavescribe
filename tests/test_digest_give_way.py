# SPDX-License-Identifier: Apache-2.0
"""A digest's call to the model gives way to an alert, measured with a fake model that takes
its time over each token, on the pipeline's own translation thread; and a digest's place in a
queue that sheds work when it is full."""

import functools
import threading
import time
from pathlib import Path

import numpy as np
import pytest

import wavescribe.pipeline as pipeline_module
from tests.test_digest import NOW, STATIONS, FakeLlm, stocked
from tests.test_pipeline import PLAN, EnergyVad, FakeTranscriber, ListSource
from wavescribe.digest import DIGEST_PRIORITY, GIVE_WAY_BELOW, GIVE_WAY_TIMES, Busy, Digester
from wavescribe.pipeline import Pipeline

TOKEN_S = 0.02
TOKENS = 100  # two seconds a reply: long enough to see who waits for it


class SlowLlm(FakeLlm):
    """Takes ``TOKEN_S`` over each token of its reply, passing each on as it comes; translates
    at once."""

    def __init__(self, *replies: str) -> None:
        super().__init__(*replies)
        self.generating = threading.Event()
        self.mid = False  # in the middle of a reply
        self.stopped = 0

    def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
        reply = super().complete(messages, max_tokens=max_tokens)
        try:
            for _ in range(TOKENS):
                self.mid = True
                self.generating.set()
                time.sleep(TOKEN_S)
                if on_text is not None:
                    on_text(".")
        except BaseException:
            self.stopped += 1
            raise
        finally:
            self.mid = False
        return reply

    def translate(self, text: str, source: str, target: str) -> str:
        return f"{target}:{text}"


def pipeline_with(llm: SlowLlm) -> Pipeline:
    """A pipeline with no station, running, whose translator is ``llm``."""
    pipeline = Pipeline(
        [],
        PLAN,
        lambda result: None,
        source_factory=lambda st: ListSource(st.id, np.zeros(1, dtype=np.float32)),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: llm,
    )
    threading.Thread(target=lambda: pipeline.run(until_idle=False), daemon=True).start()
    return pipeline


def digester_on(store, pipeline: Pipeline) -> Digester:
    def submit(run, given_up) -> bool:
        return pipeline.request_llm(run, "digest", DIGEST_PRIORITY, given_up=given_up)

    return Digester(
        store,
        lambda: STATIONS,
        submit,
        clock=lambda: NOW,
        ahead=lambda: pipeline.llm_waiting(GIVE_WAY_BELOW),
    )


def test_an_alerts_translation_waits_for_a_token_not_for_a_digest(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    llm = SlowLlm("- Water is cut tomorrow [1].")
    pipeline = pipeline_with(llm)
    d = digester_on(store, pipeline)
    made: list = []
    worker = threading.Thread(
        target=lambda: made.append(d.run(d.request({"minutes": 60}), timeout=20)), daemon=True
    )
    worker.start()
    try:
        assert llm.generating.wait(10)
        translated = threading.Event()
        t0 = time.monotonic()
        pipeline.request_translation(
            "Evacuem a zona ribeirinha", "pt-PT", ["en"], lambda out, model: translated.set()
        )
        assert translated.wait(10)
        waited = time.monotonic() - t0
        # A token and the translation, with room for a busy machine; not the whole reply.
        assert waited < TOKENS * TOKEN_S / 4, f"the alert waited {waited:.2f} s"
        worker.join(30)
        # The digest's call began again from the start once the alert was translated.
        [digest] = made
        assert llm.stopped == 1 and len(llm.prompts) == 2
        assert [p.text for p in digest.points] == ["Water is cut tomorrow."]
    finally:
        pipeline.stop()
        store.close()


def test_a_digest_gives_up_when_alerts_keep_coming(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    llm = SlowLlm("- Water is cut tomorrow [1].")

    def submit(run, given_up) -> bool:
        threading.Thread(target=run, args=(llm,), daemon=True).start()
        return True

    # Something that goes first is always waiting once the model has started a reply.
    d = Digester(store, lambda: STATIONS, submit, clock=lambda: NOW, ahead=lambda: llm.mid)
    with pytest.raises(Busy, match="alerts and questions kept the model busy"):
        d.run(d.request({"minutes": 60}), timeout=10)
    assert llm.stopped == GIVE_WAY_TIMES + 1
    store.close()


# -- in a full queue ----------------------------------------------------------------------------


def stopped_pipeline(monkeypatch: pytest.MonkeyPatch, room: int) -> Pipeline:
    """A pipeline whose translation queue holds ``room`` entries and is never worked through:
    its thread waits for speech recognition, which never starts."""
    monkeypatch.setattr(pipeline_module, "MT_QUEUE_MAX", room)
    return Pipeline(
        [],
        PLAN,
        lambda result: None,
        source_factory=lambda st: ListSource(st.id, np.zeros(1, dtype=np.float32)),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: SlowLlm(),
    )


def test_a_digest_makes_way_first_and_never_takes_anyones_place(monkeypatch) -> None:
    pipeline = stopped_pipeline(monkeypatch, 3)
    gone: list[str] = []
    try:
        question = functools.partial(pipeline.request_llm, lambda llm: None, "ask")
        assert question() and question()
        assert pipeline.request_llm(
            lambda llm: None, "digest", DIGEST_PRIORITY, given_up=lambda: gone.append("first")
        )
        # Full: an alert's translation takes the digest's place, not a question's.
        translated: list[dict] = []
        pipeline.request_translation("Fogo", "pt-PT", ["en"], lambda out, m: translated.append(out))
        assert gone == ["first"] and translated == []  # queued, not turned away
        # Full of other work: a digest is turned away and takes nobody's place.
        assert not pipeline.request_llm(
            lambda llm: None, "digest", DIGEST_PRIORITY, given_up=lambda: gone.append("second")
        )
        assert gone == ["first"] and pipeline.status()["mt_queue"] == 3
        status = pipeline.status()
        assert status["dropped"]["mt"] == 0 and status["load"]["state"] == "ok"
    finally:
        pipeline.stop()


def test_a_digest_waiting_long_is_no_station_behind(monkeypatch) -> None:
    pipeline = stopped_pipeline(monkeypatch, 10)
    try:
        assert pipeline.request_llm(lambda llm: None, "digest", DIGEST_PRIORITY)
        later = time.time() + 3600
        with monkeypatch.context() as m:
            m.setattr(pipeline_module.time, "time", lambda: later)
            status = pipeline.status()
        assert status["load"] == {"state": "ok", "reason": None}
        assert status["dropped"] == {"asr": 0, "mt": 0, "rules": 0, "filter": 0}
    finally:
        pipeline.stop()


def test_a_digest_given_up_for_live_work_says_so(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    taken: list = []

    def submit(run, given_up) -> bool:
        taken.append(given_up)
        threading.Timer(0.1, given_up).start()  # station lines filled the queue meanwhile
        return True

    d = Digester(store, lambda: STATIONS, submit, clock=lambda: NOW)
    with pytest.raises(Busy, match="live translation needed the model's queue"):
        d.run(d.request({"minutes": 60}), timeout=10)
    assert len(taken) == 1
    store.close()
