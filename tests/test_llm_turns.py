# SPDX-License-Identifier: Apache-2.0
"""The one model's work takes turns: live translation, alerts on their way, a digest someone
asked for, and the LLM rules' passes over recent speech.

Seen in testing: with three stations and one LLM rule, a digest was refused three times in
five minutes, and made in under three once the rule was switched off. Each of its calls
stopped for the rule's passes, which came about as often as a call lasts.

Measured here on the pipeline's own translation thread in simulated time: the fake model is
the only thing that moves the clock, by what a prompt of that length and a reply of that many
tokens take on a recent Mac, and whatever is said meanwhile is queued as the clock passes it.
No test waits for real time to pass."""

from __future__ import annotations

import itertools
import threading
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest

import wavescribe.pipeline as pipeline_module
from tests.test_digest import add
from tests.test_pipeline import PLAN, EnergyVad, FakeTranscriber, ListSource, station
from wavescribe.asr import Transcript
from wavescribe.digest import (
    BETWEEN_S,
    CHOICE_TOKENS,
    DIGEST_DUE_PRIORITY,
    DIGEST_PRIORITY,
    GIVE_WAY_BELOW,
    GIVE_WAY_TIMES,
    PUT_OFF_S,
    Busy,
    Cancelled,
    Digest,
    Digester,
    busy_words,
    kept_busy,
    model_of,
)
from wavescribe.pipeline import LLM_JOB_PRIORITY, LlmJob, Pipeline, Result, _WorkQueue
from wavescribe.rules import LlmRule
from wavescribe.rules.llm import (
    RULES_DUE_PRIORITY,
    RULES_PRIORITY,
    RULES_PUT_OFF_S,
    VERIFY_PRIORITY,
    LlmRuleChecker,
)
from wavescribe.segment import Utterance
from wavescribe.store import Store

NOW = 1_772_625_600.0  # when the digest's window ends; the simulation starts there
READ_TOKENS_S = 380.0  # Qwen3.5-9B, 4-bit, on an M1 Max: a prompt is read at this rate
WRITE_TOKENS_S = 40.0  # and a reply written at this one while it also translates
CHARS_PER_TOKEN = 4
STATIONS = [station("a"), station("b"), station("c")]
ASKED_AT = 100.0  # seconds into the simulation: the stations have been talking for a while


class World:
    """Simulated time and what happens in it: each station says a line every ``line_every``
    seconds, and every five seconds the rule checker's timer ticks and a digest that waits
    gets to look at the time (``_sync``). ``advance`` is called by the fake model as it works,
    on the translation thread, so everything happens in one order. Time stands still while
    no test waits for an answer (``go``)."""

    def __init__(self, line_every: float = 8.0, line_chars: int = 120) -> None:
        self.t = 0.0
        self.line_every, self.line_chars = line_every, line_chars
        self.run: Run | None = None
        self.checker: LlmRuleChecker | None = None
        self.next_line = {s.id: 1.0 + 2.7 * i for i, s in enumerate(STATIONS)}
        self.next_tick = 5.0
        self.said: list[tuple[str, str, float]] = []  # (how a line begins, station, when)
        self.extra: list[tuple[float, Callable[[], None]]] = []  # what else happens, and when
        self.go = threading.Event()
        self.closed = False
        self.idle_queued = False

    def clock(self) -> float:
        return NOW + self.t

    def at(self, when: float, what: Callable[[], None]) -> None:
        self.extra.append((when, what))
        self.extra.sort(key=lambda e: e[0])

    def advance(self, dt: float) -> None:
        if self.closed:
            return
        self._settle()
        end = self.t + dt
        while not self.closed:
            sid, at = min(self.next_line.items(), key=lambda kv: kv[1])
            then = self.extra[0][0] if self.extra else float("inf")
            first = min(at, self.next_tick, then)
            if first > end:
                break
            self.t = max(self.t, first)
            if first == then:
                self.extra.pop(0)[1]()
            elif first == at:
                self.next_line[sid] = at + self.line_every
                self.say(sid)
            else:
                self.next_tick += 5.0
                if self.checker is not None:
                    self.checker.tick()
                self._sync()
        self.t = max(self.t, end)
        self.keep_idle()

    def say(self, sid: str) -> None:
        st = next(s for s in STATIONS if s.id == sid)
        n = len(self.said) + 1
        filler = "the council talked about the road works again " * 9
        text = f"line {n}: {filler}"[: self.line_chars]
        self.said.append((f"line {n}:", sid, self.t))
        now = time.time()  # the pipeline's own check for lines too late to translate
        u = Utterance(sid, n, np.zeros(1, np.float32), now - 7, now - 1, now)
        r = Result(st, u, Transcript(text, st.language, "fake-asr"))
        assert self.run is not None
        self.run.pipeline.ingest(r)
        if self.checker is not None:
            self.checker.on_transcript(r)

    def keep_idle(self) -> None:
        """With nothing else queued, time passes half a second at a time."""
        assert self.run is not None
        q = self.run.pipeline._mt_q
        if not self.idle_queued and not self.closed and q.qsize() < q.maxsize:
            self.idle_queued = self.run.pipeline.request_llm(self._idle, "idle", float("inf"))

    def _idle(self, _llm) -> None:
        self.idle_queued = False
        self.advance(0.5)

    def _sync(self) -> None:
        """Let whoever waits for a digest look at the time twice: it sees this moment."""
        run = self.run
        assert run is not None
        if run.outstanding:
            seen, deadline = run.digester.polls, time.monotonic() + 60
            while run.digester.polls < seen + 2 and not run.answer_coming():
                assert time.monotonic() < deadline, "nobody waits for the digest"
                time.sleep(0.0005)
        self._settle()

    def _settle(self) -> None:
        """An answer on its way arrives before time goes on, and time goes on only while a
        test waits for one."""
        run = self.run
        assert run is not None
        if run.outstanding and run.answer_coming():
            assert run.answered.wait(60), "the digest's answer never came"
        while not self.go.wait(0.05):
            if self.closed:
                return


class TimedLlm:
    """Takes the time a model takes, in the world's time: a prompt at ``READ_TOKENS_S``, a
    reply at ``WRITE_TOKENS_S`` a token, each passed on as it comes; a line's translation
    ``translate_s``, or by its length. ``calls`` keeps (kind, began, ended, whether it ended
    or was stopped), and ``rule_prompts`` what the rule passes read."""

    model_id = "timed-llm"

    def __init__(self, world: World, translate_s: float | None = None) -> None:
        self.w = world
        self.last_stats: dict[str, float] = {}
        self.translate_s = translate_s
        self.calls: list[tuple[str, float, float, bool]] = []
        self.rule_prompts: list[str] = []
        self.translated: list[float] = []  # when each line's translation was done

    def translate(self, text: str, source: str, target: str) -> str:
        self.w.advance(self.translate_s or 0.4 + len(text) / CHARS_PER_TOKEN / WRITE_TOKENS_S)
        self.translated.append(self.w.t)
        return f"{target}:{text}"

    def complete(self, messages, *, max_tokens=512, on_text=None) -> str:
        asked = messages[-1]["content"]
        prompt = messages[0]["content"] + asked
        if "Rules (id: what to alert about)" in asked:
            kind, tokens, reply = "rules", 8, "[]"
            self.rule_prompts.append(asked)
        elif "A keyword alert rule called" in asked:
            kind, tokens, reply = "check", 30, '{"real": false, "reason": "a figure of speech"}'
        elif max_tokens == CHOICE_TOKENS:
            kind, tokens, reply = "choice", 20, "1, 2, 3, 4, 5"
        else:
            kind, tokens = "notes", 160
            reply = "- The council talked about road works [1].\n- The council talked again [2]."
        began, ended = self.w.t, False
        try:
            self.w.advance(len(prompt) / CHARS_PER_TOKEN / READ_TOKENS_S)
            for _ in range(tokens):
                self.w.advance(1 / WRITE_TOKENS_S)
                if on_text is not None:
                    on_text(".")
            ended = True
        finally:
            self.calls.append((kind, began, self.w.t, ended))
        return reply

    def of(self, *kinds: str, since: float = 0.0) -> list[tuple[str, float, float, bool]]:
        return [c for c in self.calls if c[0] in kinds and c[1] >= since]


def stocked(tmp_path: Path) -> Store:
    """Twenty minutes of three stations, a line every 8 s each, translated: nine parts of
    notes and the choice."""
    store = Store(tmp_path / "w.db")
    n = 0
    for i in range(150):
        for s in STATIONS:
            n += 1
            en = f"item {n}: the council of the town talked about road works and gate {n} " * 2
            t = NOW - 20 * 60 + 8.0 * i + 1
            add(store, s.id, f"linha {n} " + "obras na estrada " * 6, t, en=en[:200])
    return store


class Run:
    """A pipeline in a ``World`` with a store to digest. ``ask`` asks for a digest when the
    world's time reaches ``at`` and gives it back, or what it raised; the world then stands
    still until the next ``ask``."""

    def __init__(
        self, tmp_path: Path, *, rule: bool = True, translate_s: float | None = None
    ) -> None:
        self.w = World()
        self.w.run = self
        self.llm = TimedLlm(self.w, translate_s)
        self.store = stocked(tmp_path)
        self.pipeline = Pipeline(
            [],
            PLAN,
            lambda result: None,
            source_factory=lambda st: ListSource(st.id, np.zeros(1, dtype=np.float32)),
            vad_factory=lambda _path: EnergyVad(),
            transcriber_factory=lambda _e, _p: FakeTranscriber(),
            translator_factory=lambda _e, _p: self.llm,
            clock=self.w.clock,
        )
        self.passes: list[tuple[str, float]] = []  # (station, when its pass was queued)
        if rule:

            def submit(run, name, priority, **kw) -> bool:
                self.passes.append((kw.get("station"), self.w.t))
                return self.pipeline.request_llm(run, name, priority, **kw)

            self.w.checker = LlmRuleChecker(
                lambda: [LlmRule("politics", "a politician is criticised", "low")],
                submit,
                lambda result, hits: None,
                interval_s=45.0,
                max_chars=1500,
                clock=self.w.clock,
                timer=False,
            )
        self.queued = threading.Event()
        self.answered = threading.Event()
        self.outstanding = False  # a digest was asked for and its answer hasn't come
        self.asked = 0.0
        kw = model_of(self.pipeline)
        inner, spent = kw["submit"], kw["spent"]

        def queue(call, given_up) -> bool:
            queued = inner(call, given_up)
            self.queued.set()
            return queued

        def spent_on(seconds: float) -> dict:  # without the world's own idle time
            return {k: v for k, v in spent(seconds).items() if k[0] != "other"}

        self.digester = Digester(
            self.store,
            lambda: STATIONS,
            clock=lambda: NOW,
            timer=lambda: self.w.t,
            poll_s=0.001,
            **{**kw, "submit": queue, "spent": spent_on},
        )
        threading.Thread(target=lambda: self.pipeline.run(until_idle=False), daemon=True).start()

    def answer_coming(self) -> bool:
        """The digest asked for is made or refused, though its answer may not be back yet."""
        line = list(self.digester._line)
        return not line or (line[0].job is not None and line[0].job.done.is_set())

    def ask(self, at: float, *, timeout_s: float = 600.0) -> Digest | Exception:
        box: list[Digest | Exception] = []
        self.digester.timeout_s = timeout_s
        self.answered.clear()

        def one() -> None:
            d = self.digester
            try:
                box.append(d.run(d.request({"minutes": 20, "stations": ["a", "b", "c"]})))
            except Exception as e:
                box.append(e)
            self.w.go.clear()  # time stands still from here
            self.outstanding = False
            self.answered.set()

        def begin() -> None:  # on the translation thread, at ``at``
            self.asked = self.w.t
            self.queued.clear()
            threading.Thread(target=one, daemon=True).start()
            deadline = time.monotonic() + 60
            while not (self.queued.is_set() or self.answered.is_set()):
                assert time.monotonic() < deadline, "the digest was never begun"
                time.sleep(0.0005)
            self.outstanding = not self.answered.is_set()

        self.w.at(at, begin)
        self.w.keep_idle()
        self.w.go.set()
        assert self.answered.wait(300), "the simulation didn't end"
        return box[0]

    def took(self) -> float:
        """From the last digest asked for to the end of its last call."""
        return self.llm.of("notes", "choice", since=self.asked)[-1][2] - self.asked

    def close(self) -> None:
        self.w.closed = True
        self.w.go.set()
        self.pipeline.stop()
        self.store.close()


@pytest.fixture
def world(tmp_path: Path):
    runs: list[Run] = []

    def make(**kw) -> Run:
        runs.append(Run(tmp_path, **kw))
        return runs[-1]

    yield make
    for run in runs:
        run.close()


# -- a digest and an LLM rule -------------------------------------------------------------------


def test_a_digest_is_made_while_an_llm_rule_reads_three_stations(world) -> None:
    quiet = world(rule=False)
    without = quiet.ask(ASKED_AT)
    assert isinstance(without, Digest) and without.calls == 10
    quiet.close()

    run = world(rule=True)
    digest = run.ask(ASKED_AT)
    assert isinstance(digest, Digest), digest
    assert digest.calls == 10 and len(digest.points) == 5
    calls = run.llm.of("notes", "choice", since=run.asked)
    assert len(calls) == 10 and all(ended for *_, ended in calls)  # none was stopped
    # The rule's passes took their turns between its calls, and cost it little: three
    # stations' passes are a few seconds of the model in a minute.
    between = [c for c in run.llm.of("rules", since=run.asked) if c[2] <= calls[-1][2]]
    assert len(between) >= 3
    assert 90 < quiet.took() < 180 and run.took() < 1.25 * quiet.took()
    assert digest.timings["waited_ms"] > 0


def test_a_rule_pass_is_put_off_a_minute_at_most_and_loses_no_line(world) -> None:
    run = world(rule=True)
    assert isinstance(run.ask(ASKED_AT), Digest)
    done = run.llm.of("rules")
    queued = run.passes[: len(done)]  # they are read in the order they were queued
    waits = [began - at for (_sid, at), (_k, began, _e, _ok) in zip(queued, done, strict=True)]
    # A pass waits for a moment nothing else wants the model, a minute at most, and then
    # for the call the model is on.
    longest_call = max(e - b for _k, b, e, _ok in run.llm.of("notes", "choice"))
    assert max(waits) <= RULES_PUT_OFF_S + longest_call + 5
    during = [w for (_sid, at), w in zip(queued, waits, strict=True) if at >= run.asked]
    assert max(during) > 30  # put off behind the digest's calls, which a pass never stops
    # Each station's lines, up to its last pass that was read, are in a pass, each once.
    read = "\n".join(run.llm.rule_prompts)
    for sid in ("a", "b", "c"):
        last = max(at for s, at in queued if s == sid)
        for mark, said_on, t in run.w.said:
            if said_on == sid:
                assert read.count(f"{mark} the council") == (1 if t <= last else 0), mark


# -- a digest and a backlog of translation --------------------------------------------------------


def test_behind_a_backlog_a_digest_waits_two_minutes_then_takes_turns_with_it(world) -> None:
    # Translating takes longer than the stations talk: the queue never empties.
    run = world(rule=True, translate_s=4.0)
    digest = run.ask(ASKED_AT)
    assert isinstance(digest, Digest), digest
    calls = run.llm.of("notes", "choice", since=run.asked)
    assert all(ended for *_, ended in calls)
    first = calls[0][1] - run.asked
    assert PUT_OFF_S <= first <= PUT_OFF_S + 15  # live translation went first for that long
    # From then on each call waits ten seconds at most behind the lines, then for the line
    # being translated and the rule passes that are due.
    gaps = [b[1] - a[2] for a, b in itertools.pairwise(calls)]
    assert BETWEEN_S <= max(gaps) <= BETWEEN_S + 10
    translated = [t for t in run.llm.translated if calls[0][1] < t < calls[-1][1]]
    assert len(translated) >= 2 * len(gaps)  # live translation went on between the calls
    assert run.took() < PUT_OFF_S + 10 * 28
    assert digest.timings["waited_ms"] >= PUT_OFF_S * 1000


def test_a_digest_not_made_in_time_says_what_kept_the_model_busy_and_the_next_is_made(
    world,
) -> None:
    run = world(rule=True, translate_s=4.0)
    late = run.ask(ASKED_AT, timeout_s=90.0)
    assert isinstance(late, Busy)
    assert str(late) == (
        "live translation of a, b and c kept the model busy for 2 minutes, so no digest was "
        "made in time; try again in a minute"
    )
    assert late.reason == str(late).removesuffix("; try again in a minute")
    assert late.retry_after_s == 60 and 90 <= late.waited_s <= 96
    [translation, *others] = late.busy_with
    assert translation["kind"] == "translation" and sorted(translation["names"]) == ["a", "b", "c"]
    assert 60 < translation["seconds"] <= late.waited_s
    assert [b["kind"] for b in others] in ([], ["rules"])
    assert run.llm.of("notes", "choice") == []  # the model never got to it
    # Asked again at the time it named, with the backlog as it was: the digest takes turns
    # with live translation from its first call, and is made.
    made = run.ask(run.w.t + late.retry_after_s)
    assert isinstance(made, Digest), made
    calls = run.llm.of("notes", "choice", since=run.asked)
    assert calls[0][1] - run.asked <= BETWEEN_S + 10
    assert run.took() < 10 * 28
    # The one after it waits behind live translation again, as any digest does.
    third = run.ask(run.w.t + 30.0)
    assert isinstance(third, Digest)
    assert run.llm.of("notes", since=run.asked)[0][1] - run.asked >= PUT_OFF_S


# -- alerts on their way --------------------------------------------------------------------------


def test_a_digest_that_kept_stopping_for_checks_names_the_rule_and_the_next_is_made(world) -> None:
    run = world(rule=False)

    def check() -> None:  # a keyword match to check every four seconds: no call can end
        run.pipeline.request_llm(
            lambda llm: run.w.advance(1.0), "verify", VERIFY_PRIORITY, about=("water",)
        )
        run.w.at(run.w.t + 4.0, check)

    run.w.at(ASKED_AT + 1.0, check)
    refused = run.ask(ASKED_AT)
    assert isinstance(refused, Busy)
    assert str(refused) == (
        "checks of what the alert rule 'water' matched kept the model busy; try again in a minute"
    )
    assert [(b["kind"], b["names"]) for b in refused.busy_with] == [("check", ["water"])]
    stopped = run.llm.of("notes", since=run.asked)
    assert len(stopped) == GIVE_WAY_TIMES + 1 and not any(ended for *_, ended in stopped)
    # Asked again at the time it named, with the matches coming as before: its calls no
    # longer stop for a check, which waits for the call under way instead.
    made = run.ask(run.w.t + refused.retry_after_s)
    assert isinstance(made, Digest), made
    calls = run.llm.of("notes", "choice", since=run.asked)
    assert len(calls) == 10 and all(ended for *_, ended in calls)
    assert run.took() < 10 * 20


# -- where a digest stands while it waits -------------------------------------------------------


def test_the_status_says_where_a_waiting_digest_stands(world) -> None:
    run = world(rule=False, translate_s=4.0)
    seen: list[dict] = []
    run.w.at(ASKED_AT + 30.0, lambda: seen.extend(run.digester.status()))
    run.w.at(ASKED_AT + PUT_OFF_S + 30.0, lambda: seen.extend(run.digester.status()))
    digest = run.ask(ASKED_AT)
    assert isinstance(digest, Digest)
    waiting, later = seen
    assert waiting["state"] == "waiting" and waiting["id"] == digest.request.id
    assert waiting["kind"] == "digest" and waiting["here"] is True
    assert waiting["waited_s"] == 30.0 and waiting["gives_up_in_s"] == 570.0
    assert waiting["behind"] >= 3 and waiting["behind_what"] == ["translation"]
    assert waiting["turn_in_s"] == PUT_OFF_S - 30.0
    assert (waiting["call"], waiting["calls"]) == (1, 10)
    assert later["state"] in ("waiting", "writing") and later["call"] >= 2
    assert later["turn_in_s"] is None or later["turn_in_s"] <= BETWEEN_S
    assert run.digester.status() == []


def until(condition: Callable[[], bool], what: str) -> None:
    """Wait for another thread to get there; a loaded machine is given a minute."""
    deadline = time.monotonic() + 60
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.005)


def test_a_third_digest_waits_in_line_and_a_seventh_is_refused(tmp_path: Path) -> None:
    store = stocked(tmp_path)
    held: list = []  # calls queued for a model that is never free
    reads: list[str] = []  # who read the store
    window = store.window
    store.window = lambda **kw: reads.append(threading.current_thread().name) or window(**kw)
    d = Digester(
        store, lambda: STATIONS, lambda call, given_up: held.append(call) or True,
        clock=lambda: NOW, poll_s=0.001,
    )  # fmt: skip
    answers: dict[str, object] = {}

    def ask(name: str) -> threading.Thread:
        def one() -> None:
            try:
                answers[name] = d.run(d.request({"minutes": 20, "id": name}))
            except Exception as e:
                answers[name] = e

        thread = threading.Thread(target=one, name=name, daemon=True)
        thread.start()
        until(
            lambda: name in answers or any(r["id"] == name for r in d.status()),
            "the digest was never begun",
        )
        return thread

    names = [f"digest-{n}" for n in range(1, 8)]
    threads = [ask(name) for name in names]
    try:
        until(lambda: all(r["calls"] for r in d.status()[:2]), "the first two never began")
        rows = {r["id"]: r for r in d.status()}
        assert [r["state"] for r in rows.values()] == ["waiting"] * 2 + ["in_line"] * 4
        # A digest takes its turn before it reads the store: those in line haven't.
        assert set(reads) == {"digest-1", "digest-2"}
        assert [rows[n]["behind"] for n in names[2:6]] == [1, 2, 3, 4]
        assert rows["digest-3"]["behind_what"] == ["digest"] and rows["digest-3"]["call"] is None
        refused = answers["digest-7"]  # at once, with about when a place comes free
        assert isinstance(refused, Busy) and refused.waited_s is None
        assert str(refused).startswith(
            "6 digests are already being made or waiting for their turn; try again in "
        )
        assert 30 <= refused.retry_after_s <= 600
        # The first is cancelled: the third takes its place, in the order they asked.
        assert d.cancel("digest-1") == "cancelled"
        until(
            lambda: [r["id"] for r in d.status()][:2] == ["digest-2", "digest-3"],
            "the third never got its turn",
        )
        until(lambda: d.status()[1]["calls"] == 10, "the third never read the store")
        assert d.status()[1]["state"] == "waiting" and "digest-3" in reads
        until(lambda: isinstance(answers.get("digest-1"), Cancelled), "the first never ended")
    finally:
        for name in names:
            d.cancel(name)
        for thread in threads:
            thread.join(5)
        store.close()


# -- the queue ------------------------------------------------------------------------------------


def job(name: str, **kw) -> LlmJob:
    return LlmJob(lambda llm: None, name, **kw)


def test_work_that_was_put_off_goes_up_when_its_time_comes() -> None:
    now = [0.0]
    q = _WorkQueue(10, lambda item: False, lambda entry: None, clock=lambda: now[0])
    rules = job("llm-rules", urgent_at=60.0, urgent=RULES_DUE_PRIORITY)
    digest = job("digest", urgent_at=120.0, urgent=DIGEST_DUE_PRIORITY)
    line, question = object(), job("ask")
    q.put_nowait((RULES_PRIORITY, 1, rules))
    q.put_nowait((DIGEST_PRIORITY, 2, digest))
    q.put_nowait((5, 3, line))
    assert [e[2] for e in sorted(q.snapshot())] == [line, digest, rules]
    now[0] = 60.0
    q.put_nowait((LLM_JOB_PRIORITY, 4, question))
    assert [e[2] for e in sorted(q.snapshot())] == [question, rules, line, digest]
    now[0] = 120.0
    assert [q.get()[2] for _ in range(4)] == [question, rules, digest, line]


def test_the_order_of_the_models_work() -> None:
    assert 0 < VERIFY_PRIORITY < LLM_JOB_PRIORITY < GIVE_WAY_BELOW
    assert GIVE_WAY_BELOW < RULES_DUE_PRIORITY < DIGEST_DUE_PRIORITY < 1  # stations start at 1
    assert 10**9 < DIGEST_PRIORITY < RULES_PRIORITY < float("inf")


def test_a_rule_pass_gets_in_ahead_of_a_full_backlog_and_a_digest_does_not(monkeypatch) -> None:
    monkeypatch.setattr(pipeline_module, "MT_QUEUE_MAX", 3)
    pipeline = Pipeline(
        [],
        PLAN,
        lambda result: None,
        source_factory=lambda st: ListSource(st.id, np.zeros(1, dtype=np.float32)),
        vad_factory=lambda _path: EnergyVad(),
        transcriber_factory=lambda _e, _p: FakeTranscriber(),
        translator_factory=lambda _e, _p: None,
    )  # its translation thread waits for speech recognition, which never starts
    a = station("a")
    untranslated: list[Result] = []
    pipeline._on_result = untranslated.append
    try:
        for n in range(3):
            now = time.time()
            u = Utterance("a", n, np.zeros(1, np.float32), now - 2, now - 1, now)
            pipeline.ingest(Result(a, u, Transcript(f"line {n}", "pt-PT", "fake-asr")))
        gone: list[str] = []
        submit = model_of(pipeline)["submit"]

        class Call:
            put_off_s, about = 30.0, ("digest-1",)

        assert not submit(Call(), lambda: gone.append("digest"))  # no room behind the lines
        # A pass's place is behind them too, but it is never refused for a backlog of lines.
        assert pipeline.request_llm(
            lambda llm: None,
            "llm-rules",
            RULES_PRIORITY,
            station="a",
            about=("politics",),
            put_off_s=RULES_PUT_OFF_S,
            urgent=RULES_DUE_PRIORITY,
        )
        assert [r.transcript.text for r in untranslated] == ["line 2"]  # the newest made way
        assert pipeline.status()["dropped"]["rules"] == 0

        class Due(Call):
            put_off_s = 0.0

        assert submit(Due(), lambda: gone.append("digest"))  # it has waited: ahead of the lines
        assert [r.transcript.text for r in untranslated] == ["line 2", "line 1"] and gone == []
    finally:
        pipeline.stop()


# -- in words -----------------------------------------------------------------------------------


def test_what_kept_the_model_busy_is_said_by_name_and_only_here() -> None:
    spent = {
        ("translation", ("North FM",)): 200.0,
        ("translation", ("Harbour Radio",)): 90.0,
        ("rules", ("politics", "roads")): 50.0,
        ("check", ("water",)): 45.0,
        ("digest", ("mine",)): 80.0,  # its own calls
        ("question", ()): 3.0,  # under a tenth
    }
    here = kept_busy(spent, own="mine")
    assert here == [
        {"kind": "translation", "names": ["North FM", "Harbour Radio"], "seconds": 290.0},
        {"kind": "rules", "names": ["politics", "roads"], "seconds": 50.0},
        {"kind": "check", "names": ["water"], "seconds": 45.0},
    ]
    assert [busy_words(b) for b in here] == [
        "live translation of North FM and Harbour Radio",
        "the alert rules 'politics' and 'roads'",
        "checks of what the alert rule 'water' matched",
    ]
    elsewhere = kept_busy(spent, own="mine", named=False)
    assert [b["names"] for b in elsewhere] == [[], [], []]
    assert [busy_words(b) for b in elsewhere] == [
        "live translation",
        "alert rules",
        "checks of keyword matches",
    ]
    assert busy_words({"kind": "digest", "names": ["someone-elses"]}) == "other digests"
    assert kept_busy({}) == []
