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
from wavescribe.digest import (
    DIGEST_PRIORITY,
    GIVE_WAY_TIMES,
    KEPT,
    Busy,
    Cancelled,
    Digester,
    RequestError,
    model_of,
)
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
    return Digester(store, lambda: STATIONS, clock=lambda: NOW, **model_of(pipeline))


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


def test_a_call_that_made_way_in_a_full_queue_is_queued_again(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    llm = FakeLlm("- Water is cut tomorrow [1].")
    taken: list = []

    def submit(run, given_up) -> bool:
        taken.append(run)
        if len(taken) == 1:
            given_up()  # station lines filled the queue meanwhile: its call made way for them
        else:
            run(llm)
        return True

    d = Digester(store, lambda: STATIONS, submit, clock=lambda: NOW, poll_s=0.001)
    digest = d.run(d.request({"minutes": 60}), timeout=60)
    assert [p.text for p in digest.points] == ["Water is cut tomorrow."]
    assert len(taken) == 2 and len(llm.prompts) == 1
    taken[0](llm)  # the call that made way, should it turn up after all: it isn't made twice
    assert len(llm.prompts) == 1
    store.close()


# -- cancelled by its id ------------------------------------------------------------------------


def running(d: Digester, request) -> tuple[threading.Thread, list]:
    """``d.run(request)`` on a thread of its own; the list gets what it returned or raised."""
    out: list = []

    def work() -> None:
        try:
            out.append(d.run(request, timeout=20))
        except Exception as e:
            out.append(e)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, out


def until(condition, what: str) -> None:
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


def test_a_digest_cancelled_while_queued_never_reaches_the_model(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    llm = SlowLlm("- Water is cut tomorrow [1].")
    queued: list = []

    def submit(run, given_up) -> bool:
        queued.append(run)  # behind a backlog that isn't worked through here
        return True

    d = Digester(store, lambda: STATIONS, submit, clock=lambda: NOW)
    worker, out = running(d, d.request({"minutes": 60, "id": "cancel-me-1"}))
    until(lambda: queued, "the digest's call was never queued")
    assert d.cancel("cancel-me-1") == "cancelled"
    worker.join(5)
    [cancelled] = out  # answered at once, while still queued
    assert isinstance(cancelled, Cancelled)
    queued[0](llm)  # its turn comes: the model isn't spent on it, and nothing more is queued
    assert llm.prompts == [] and len(queued) == 1
    assert d.cancel("cancel-me-1") is None and d.kept("cancel-me-1") is None
    store.close()


def test_a_digest_cancelled_while_written_stops_at_the_next_token(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    llm = SlowLlm("- Water is cut tomorrow [1].")
    pipeline = pipeline_with(llm)
    d = digester_on(store, pipeline)
    worker, out = running(d, d.request({"minutes": 60, "id": "cancel-me-2"}))
    try:
        assert llm.generating.wait(10)
        t0 = time.monotonic()
        assert d.cancel("cancel-me-2") == "cancelled"
        worker.join(5)
        waited = time.monotonic() - t0
        assert isinstance(out[0], Cancelled) and waited < TOKENS * TOKEN_S / 2, waited
        # The model is free at once, and the call isn't queued again.
        translated = threading.Event()
        pipeline.request_translation("Fogo", "pt-PT", ["en"], lambda out, m: translated.set())
        assert translated.wait(5)
        assert llm.stopped == 1 and len(llm.prompts) == 1 and not llm.mid
    finally:
        pipeline.stop()
        store.close()


def test_a_digest_is_kept_by_its_id_and_an_id_is_one_digests(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    go = threading.Event()
    llm = FakeLlm("- Water is cut tomorrow [1].")

    def submit(run, given_up) -> bool:
        threading.Thread(target=lambda: (go.wait(5), run(llm)), daemon=True).start()
        return True

    d = Digester(store, lambda: STATIONS, submit, clock=lambda: NOW)
    worker, out = running(d, d.request({"minutes": 60, "id": "same-id-123"}))
    until(lambda: "same-id-123" in d._live, "the digest was never begun")
    with pytest.raises(RequestError, match="already being made") as refused:
        d.run(d.request({"minutes": 60, "id": "same-id-123"}))
    assert refused.value.code == "id_in_use"
    go.set()
    worker.join(5)
    assert out[0].request.id == "same-id-123"
    assert d.kept("same-id-123") is out[0] and d.cancel("same-id-123") == "finished"
    # Another machine reaches only what other machines asked for.
    assert d.cancel("same-id-123", here=False) is None
    for n in range(KEPT):
        d.run(d.request({"minutes": 60, "id": f"later-{n:04d}"}))
    assert d.kept("same-id-123") is None and d.kept(f"later-{KEPT - 1:04d}") is not None
    store.close()
