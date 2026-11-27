# SPDX-License-Identifier: Apache-2.0
"""The listen pipeline: sources -> VAD -> one ASR worker -> one translation worker -> output.

Threads, not processes: ffmpeg runs in its own process already, and onnxruntime, MLX and
CTranslate2 all release the GIL while they compute. Queues are bounded, so memory stays bounded
when a stage falls behind; overflow is dropped and counted rather than blocking the sources.

Overload is shown, not hidden. When a queue is full, more urgent work (an LLM rule check, an
alert's translation, a more important station's speech) takes the place of the least urgent
station work instead of being turned away; a station translation that waited longer than
``MT_STALE_S`` is skipped, since it would only push everything else later. Every drop is counted
per station over ten minutes and logged once a minute, and ``status()`` says how far behind each
station's text and translation are and whether the machine keeps up (``load``).
"""

from __future__ import annotations

import contextlib
import gc
import heapq
import itertools
import logging
import queue
import resource
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from wavescribe.asr import Transcriber, Transcript, load_transcriber
from wavescribe.asr.checks import check_transcript
from wavescribe.asr.language import AUTO, plan_detecting, transcribe_any
from wavescribe.asr.recheck import Rechecker
from wavescribe.audio import AudioChunk
from wavescribe.config import Station
from wavescribe.gate import Gate, Verdict
from wavescribe.gate.yamnet import YamnetGate
from wavescribe.languages import same_language
from wavescribe.log import event
from wavescribe.radio.bandplan import logged_text
from wavescribe.registry import ModelEntry, ModelError, ModelRegistry
from wavescribe.segment import Utterance
from wavescribe.segment.vad import FrameVad, SileroVad, VadSegmenter
from wavescribe.sources import AudioSource, open_source
from wavescribe.translate import Translator, load_translator, unsupported_targets

log = logging.getLogger(__name__)

ASR_QUEUE_MAX = 200  # ~1 h of speech for one station; beyond this we are hopelessly behind
MT_QUEUE_MAX = 200
HEALTH_INTERVAL_S = 60.0
LAG_WINDOW_S = 600.0  # lag percentiles cover the last ten minutes
# Keeping up (``status()["load"]``). Text is behind when speech has waited this long for the ASR,
# translation when a transcript has waited this long for the translator. A station translation
# that waited MT_STALE_S is skipped: shown untranslated, counted, never translated late.
TEXT_BEHIND_S = 30.0
TEXT_OVERLOAD_S = 120.0
MT_BEHIND_S = 120.0
MT_STALE_S = 300.0
RULES_OVERLOAD_S = 120.0  # an LLM rule check waiting this long: alerts come late
DROP_WINDOW_S = 600.0  # drops are counted per station over the last ten minutes
DROP_LOG_S = 60.0  # and logged at most this often, with how many
# Why work was dropped: "full" (no room in its queue, or displaced by more urgent work) and
# "stale" (a translation too late to be useful) are the machine not keeping up; "failed" is a
# model that raised (its own log line says so), "no_llm" an LLM rule with no LLM to check it.
CAPACITY = ("full", "stale")
ASR_IDLE_UNLOAD_S = 300.0  # an ASR model no running station uses is unloaded after this
# A model call that raises costs one utterance. After this many in a row for the same station or
# target language the status reports the model as failing, next to the load errors.
FAILURES_BEFORE_ERROR = 20
# A station whose capture raises (its source, its VAD, loading the VAD or the gate) is restarted
# after a delay that doubles with each failure in a row, as the stream sources back off, so one
# broken station neither ends the others nor spins.
RESTART_INITIAL_S = 1.0
RESTART_MAX_S = 60.0
RESTART_STEADY_S = 30.0  # a run this long without failing starts the delay afresh
STOP_WAIT_S = 10.0  # a station started while it stops waits this long for the stop to finish
# Sentinel priorities: STOP_NOW sorts before every queued job, STOP_DRAINED after all of them.
_STOP_NOW = float("-inf")
_STOP_DRAINED = float("inf")
_CONTROL = -1.0  # model loads and swaps: ahead of every utterance, behind STOP_NOW


@dataclass(frozen=True)
class _LoadAsr:
    entry: ModelEntry
    path: Path


@dataclass(frozen=True)
class _AsrJob:
    run: Callable[[Callable[[ModelEntry, Path], Transcriber]], None]


@dataclass(frozen=True)
class _SwapMt:
    mt: tuple[ModelEntry, Path] | None


@dataclass
class Result:
    """Everything known about one utterance once it has been through the pipeline."""

    station: Station
    utterance: Utterance
    transcript: Transcript
    translations: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)  # milliseconds per stage
    hits: list = field(default_factory=list)  # keyword rule hits (rules.Hit), set by the Alerter
    asr_done_at: float = 0.0
    translations_model: str | None = None
    segment_id: int | None = None  # set by the store when it keeps the utterance
    flags: tuple[str, ...] = ()  # from the ASR checks, e.g. ("lang:es",)
    # Whisper is to listen again (asr/recheck.py): what judges the words waits for on_rechecked.
    rechecking: bool = False


def fan_out(*callbacks: Callable[..., object] | None) -> Callable[..., None]:
    """One observer that calls several in order; one failing never stops the others."""
    active = [c for c in callbacks if c is not None]

    def call(*args: object) -> None:
        for callback in active:
            try:
                callback(*args)
            except Exception:
                log.exception("observer %r failed", callback)

    return call


@dataclass(frozen=True)
class TranslationJob:
    """An ad-hoc translation (an alert's text), served ahead of every station's backlog."""

    text: str
    source: str
    targets: tuple[str, ...]
    callback: Callable[[dict[str, str], str | None], None]  # (translations, model id)


@dataclass(frozen=True)
class LlmJob:
    """Work for the translation model itself (answering a question about the transcripts). It
    runs on the translation thread, after alert translations and ahead of station backlogs."""

    run: Callable[[Translator], None]
    name: str = "llm"
    station: str | None = None  # whose text it reads (LLM rule checks), for the drop counts
    queued_at: float = field(default_factory=time.time)


class _WorkQueue:
    """A bounded priority queue of ``(priority, tiebreak, item)`` entries, lowest first.

    When it is full, a more urgent entry takes the place of the least urgent one that
    ``evictable`` allows (station work; never jobs, model loads or stop markers) instead of
    being turned away, and ``on_evict`` is told, outside the lock. Otherwise it is refused
    (``queue.Full``), as is an entry no more urgent than everything there: among equals the
    newest goes. An LLM rule check or an alert's translation must not be refused because the
    station backlog filled the queue."""

    def __init__(
        self,
        maxsize: int,
        evictable: Callable[[object], bool],
        on_evict: Callable[[tuple], None],
    ) -> None:
        self.maxsize = maxsize
        self._heap: list[tuple] = []
        self._cond = threading.Condition()
        self._evictable = evictable
        self._on_evict = on_evict

    def qsize(self) -> int:
        with self._cond:
            return len(self._heap)

    def put_nowait(self, entry: tuple) -> None:
        evicted = None
        with self._cond:
            if len(self._heap) >= self.maxsize:
                least = max((e for e in self._heap if self._evictable(e[2])),
                            key=lambda e: e[:2], default=None)  # fmt: skip
                if least is None or entry[:2] >= least[:2]:
                    raise queue.Full
                self._heap.remove(least)
                heapq.heapify(self._heap)
                evicted = least
            heapq.heappush(self._heap, entry)
            self._cond.notify_all()
        if evicted is not None:
            self._on_evict(evicted)

    def put(self, entry: tuple, give_up: Callable[[], bool] = lambda: False) -> None:
        """Wait for room (the drained stop marker: the worker is consuming), unless
        ``give_up`` says to stop waiting (``queue.Full``)."""
        with self._cond:
            while len(self._heap) >= self.maxsize:
                if give_up():
                    raise queue.Full
                self._cond.wait(0.2)
            heapq.heappush(self._heap, entry)
            self._cond.notify_all()

    def get(self, timeout: float | None = None) -> tuple:
        with self._cond:
            end = None if timeout is None else time.monotonic() + timeout
            while not self._heap:
                left = None if end is None else end - time.monotonic()
                if left is not None and left <= 0:
                    raise queue.Empty
                self._cond.wait(left)
            entry = heapq.heappop(self._heap)
            self._cond.notify_all()  # room for a waiting put
            return entry

    def snapshot(self) -> list[tuple]:
        with self._cond:
            return list(self._heap)

    def take(self, which: Callable[[tuple], bool]) -> list[tuple]:
        """Remove and return the entries ``which`` picks."""
        with self._cond:
            out = [e for e in self._heap if which(e)]
            if out:
                self._heap = [e for e in self._heap if not which(e)]
                heapq.heapify(self._heap)
                self._cond.notify_all()
            return out


class _Recent:
    """Counts per key over the last ``window_s``, in one-minute buckets: bounded memory."""

    def __init__(self, window_s: float = DROP_WINDOW_S, bucket_s: float = 60.0) -> None:
        self.window_s, self.bucket_s = window_s, bucket_s
        self._counts: dict[tuple, deque[list[float]]] = {}
        self._lock = threading.Lock()

    def add(self, key: tuple, now: float, n: int = 1) -> None:
        bucket = now // self.bucket_s
        keep = int(self.window_s // self.bucket_s) + 1
        with self._lock:
            rows = self._counts.setdefault(key, deque(maxlen=keep))
            if rows and rows[-1][0] == bucket:
                rows[-1][1] += n
            else:
                rows.append([bucket, n])

    def totals(self, now: float) -> dict[tuple, int]:
        oldest = (now - self.window_s) // self.bucket_s
        out: dict[tuple, int] = {}
        with self._lock:
            for key, rows in list(self._counts.items()):
                n = sum(int(c) for b, c in rows if b > oldest)
                if n:
                    out[key] = n
                elif not rows or rows[-1][0] <= oldest:
                    del self._counts[key]  # nothing recent: forget it
        return out


LLM_JOB_PRIORITY = 0.5  # alert translations are 0, stations start at 1


@dataclass
class Plan:
    """Which model serves each station, resolved from the registry. Models load when a running
    station first needs them, so planning every configured station costs nothing."""

    vad: tuple[ModelEntry, Path] | None  # None: not downloaded; only listening works
    asr: dict[str, tuple[ModelEntry, Path]]  # station id -> (entry, path)
    mt: tuple[ModelEntry, Path] | None  # the shared translator
    gate: tuple[ModelEntry, Path] | None = None  # speech/music; None transcribes songs too
    # Stations that chose their own translator on the page: station id -> (entry, path).
    mt_station: dict[str, tuple[ModelEntry, Path]] = field(default_factory=dict)
    recheck: Rechecker | None = None  # Whisper's second opinion on lines in another language

    def translator_for(self, station: Station) -> tuple[ModelEntry, Path] | None:
        return self.mt_station.get(station.id) or self.mt

    def noncommercial(self) -> list[str]:
        """Restricted-tier (non-commercial) models in this plan, which the app must announce."""
        used = [self.vad, self.mt, self.gate, *self.asr.values(), *self.mt_station.values()]
        return sorted({e.id for e, _ in filter(None, used) if e.tier == "restricted"})


def plan_available(
    registry: ModelRegistry,
    stations: list[Station],
    translate: bool,
    *,
    asr_model: str | None = None,
    mt_model: str | None = None,
    use_gate: bool = True,
) -> tuple[Plan, dict[str, str]]:
    """Resolve models per station; stations whose language has no installed model are returned
    separately with the reason, instead of failing the whole plan. Stations that are only
    listened to need no model at all, so they work before any model is downloaded."""
    try:
        vad: tuple[ModelEntry, Path] | None = registry.resolve("vad")
    except ModelError as e:
        vad = None
        log.info("no speech detector yet, so stations can only be listened to: %s", e)
    asr: dict[str, tuple[ModelEntry, Path]] = {}
    mt_station: dict[str, tuple[ModelEntry, Path]] = {}
    unavailable: dict[str, str] = {}
    for s in stations:
        if s.remote or not s.transcribe:  # transcribed elsewhere, or only listened to
            continue
        if vad is None:
            unavailable[s.id] = NO_VAD
            continue
        try:
            asr[s.id] = plan_station(registry, s, asr_model)
        except ModelError as e:
            unavailable[s.id] = str(e)
            continue
        if s.mt_model and translate and not mt_model:
            try:
                mt_station[s.id] = registry.installed(s.mt_model)
            except ModelError as e:
                log.warning("station %s: its translator isn't usable, the shared one is: %s",
                            s.id, e)  # fmt: skip
    mt = None
    # The translator loads only when a running station needs it, or for a question or an LLM
    # rule; resolving it here costs nothing.
    if translate:
        try:
            mt = registry.installed(mt_model) if mt_model else registry.resolve("translate")
        except ModelError as e:
            if any(s.translation_targets for s in stations if s.id in asr):
                raise
            log.info("no translator: %s", e)
    for s in stations:
        chosen = mt_station.get(s.id) or mt
        if s.id not in asr or chosen is None:
            continue
        missing = unsupported_targets(*chosen, s.translation_targets)
        if missing:
            log.warning("station %s: %s can't translate into %s, so those translations will be "
                        "missing; choose another translator for it or correct translate_to",
                        s.id, chosen[0].id, ", ".join(missing))  # fmt: skip
    gate = None
    if use_gate:
        try:
            gate = registry.resolve("gate")
        except ModelError as e:
            log.warning("no speech/music gate, so songs will be transcribed too: %s", e)
    recheck = Rechecker.plan(registry, stations, asr)
    plan = Plan(vad=vad, asr=asr, mt=mt, gate=gate, mt_station=mt_station, recheck=recheck)
    return plan, unavailable


MT_IDLE_UNLOAD_S = 900.0  # a station's own translator is dropped after 15 min unused

NO_VAD = ("transcribing needs the speech detector (Silero VAD): download it under Settings → "
          "Languages and models (or wavescribe models fetch); listening works without "
          "it")  # fmt: skip


def plan_station(
    registry: ModelRegistry, station: Station, asr_model: str | None = None
) -> tuple[ModelEntry, Path]:
    """The ASR model for one station: the one named on the command line, else the station's
    own choice (made on the page), else the first installed model of the chain for its
    language."""
    if station.language == AUTO:
        return plan_detecting(registry, station, asr_model)
    chosen = asr_model or station.asr_model
    if chosen:
        return registry.installed(chosen, station.language)
    return registry.resolve("asr", station.language)


def plan_models(
    registry: ModelRegistry,
    stations: list[Station],
    translate: bool,
    *,
    asr_model: str | None = None,
    mt_model: str | None = None,
    use_gate: bool = True,
) -> Plan:
    """Resolve models from the registry chains, or use the ones named explicitly."""
    plan, unavailable = plan_available(
        registry, stations, translate, asr_model=asr_model, mt_model=mt_model, use_gate=use_gate
    )
    if unavailable:
        raise ModelError(next(iter(unavailable.values())))
    return plan


class StationStopping(RuntimeError):
    """A station was started again while its last run is still letting go of its source."""


@dataclass
class _Live:
    """A running station: its source, its segmenter and the latest audio level."""

    station: Station
    source: AudioSource
    segmenter: VadSegmenter | None = None
    level: float = 0.0  # RMS of the latest chunk, 0..1
    stopping: bool = False
    started_at: float = field(default_factory=time.time)
    last_speech_at: float | None = None  # when the VAD last heard speech (epoch seconds)
    last_talk_at: float | None = None  # when an utterance last passed the gate
    last_music_at: float | None = None  # when the gate last dropped an utterance as music
    # After its capture raised: why, until it hears audio again; when it is tried again
    # (monotonic); failures in a row, for the delay; restarts in all, shown as reconnects.
    error: str | None = None
    retry_at: float = 0.0
    failures: int = 0
    restarts: int = 0


class Pipeline:
    def __init__(
        self,
        stations: list[Station],
        plan: Plan,
        on_result: Callable[[Result], None],
        *,
        on_transcript: Callable[[Result], None] | None = None,
        on_filtered: Callable[[Result, str], None] | None = None,
        on_rechecked: Callable[[Result], None] | None = None,
        on_audio: Callable[[str, AudioChunk], None] | None = None,
        source_factory: Callable[[Station], AudioSource] | None = None,
        vad_factory: Callable[[Path], FrameVad] = SileroVad,
        gate_factory: Callable[[Path], Gate] = YamnetGate,
        transcriber_factory: Callable[[ModelEntry, Path], Transcriber] = load_transcriber,
        translator_factory: Callable[[ModelEntry, Path], Translator] | None = None,
        mt_threads: int = 4,
    ) -> None:
        """
        Args:
            on_result: called once per utterance when it is fully processed.
            on_transcript: called as soon as the source-language text exists, before
                translation (for live views). The same ``Result`` later reaches ``on_result``.
            on_filtered: called with an utterance the ASR checks dropped and the reason
                (``Check.drop``). It reaches neither of the above: this is for the alert rules,
                which must not lose a warning to a mistake of the filter.
            on_rechecked: called with a line that reached ``on_transcript`` with ``rechecking``
                set, once Whisper has listened again (asr/recheck.py): its text may be new.
                Then it goes on to translation and ``on_result``.
            on_audio: called with every audio chunk, from the station's source thread.
        """
        self._stations = stations
        self._plan = plan
        self._on_result = on_result
        self._on_transcript = on_transcript
        self._on_filtered = on_filtered
        self._on_rechecked = on_rechecked
        self._on_audio = on_audio
        self._source_factory = source_factory or open_source
        self._vad_factory = vad_factory
        self._gate_factory = gate_factory
        self._music_s = 0.0  # seconds of music the gate kept away from ASR
        self._transcriber_factory = transcriber_factory
        self._translator_factory = translator_factory or (
            lambda entry, path: load_translator(entry, path, threads=mt_threads)
        )
        self._stop = threading.Event()
        # A station's speech, or its transcript waiting for translation, can make way for more
        # urgent work; jobs (rule checks, alert translations, questions, re-checks), model
        # loads and the stop markers never do.
        self._asr_q = _WorkQueue(
            ASR_QUEUE_MAX,
            lambda item: isinstance(item, tuple),  # (station, utterance)
            lambda e: self._drop("asr", e[2][0].id, "full"),
        )
        self._mt_q = _WorkQueue(MT_QUEUE_MAX, lambda item: isinstance(item, Result),
                                lambda e: self._untranslated(e[2], "full"))  # fmt: skip
        self._tiebreak = itertools.count()
        self._lock = threading.Lock()
        self._live: dict[str, _Live] = {}
        self._threads: list[threading.Thread] = []
        self._dropped = {"asr": 0, "mt": 0, "rules": 0, "filter": 0}  # since the start
        self._recent_drops = _Recent()  # (stage, why, station) over DROP_WINDOW_S
        self._drop_lock = threading.Lock()
        self._unlogged: Counter[tuple[str, str, str]] = Counter()  # drops since the last log
        self._drops_logged_at = float("-inf")
        self._asr_doing: tuple[str, float] | None = None  # (station, ready_at) being heard
        self._mt_doing: tuple[str, float] | None = None  # (station, asr_done_at) translating
        self._mt_swept_at = 0.0
        self._busy: dict[str, deque[tuple[float, float]]] = {
            "asr": deque(maxlen=2000),
            "mt": deque(maxlen=2000),
        }
        self._asr_ready = threading.Event()
        self._drain = False
        self._errors: list[BaseException] = []
        # One model load at a time: two backends importing at once from two threads race on
        # half-initialised modules (mlx-audio and mlx-lm both import transformers).
        self._load_lock = threading.Lock()
        self._asr_models: dict[str, float] = {}  # loaded ASR model id -> last used (epoch s)
        self._model_errors: dict[str, str] = {}  # model id -> why it failed to load or run
        self._failures: dict[tuple[str, str], int] = {}  # (model id, work) -> failures in a row
        self._loading: str | None = None  # the model being loaded right now
        self._mt_model: str | None = None  # the translator's id once it is loaded
        self._mt_started = False
        self._lag: dict[str, deque[tuple[float, float, float | None]]] = {}

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        self._spawn("asr", self._asr_worker)
        if self._plan.mt is not None:
            self._start_mt()
        for station in self._stations:
            self.add_station(station)

    def _start_mt(self) -> None:
        with self._lock:
            if self._mt_started:
                return
            self._mt_started = True
        self._spawn("mt", self._mt_worker)

    def add_station(self, station: Station, asr: tuple[ModelEntry, Path] | None = None) -> None:
        """Start listening to a station (no-op if it is already running). ``asr`` plans its
        model, for a station added after the start; its model loads before its first
        utterance."""
        if asr is not None:
            self._plan.asr[station.id] = asr
        planned = self._plan.asr.get(station.id)
        if planned is None and station.transcribe:
            raise ValueError(f"no ASR model was planned for station {station.id!r}")
        # Stopped a moment ago (Stop, then Start): its source may still be letting go. Started
        # now, it would count as running and then end with the old run.
        self._join_source(station.id, STOP_WAIT_S, only_stopping=True)
        with self._lock:
            old = self._live.get(station.id)
            if old is not None and old.stopping:
                raise StationStopping(
                    f"{station.name} is still stopping: try again in a few seconds"
                )
            if old is not None:
                return
            live = _Live(station, self._source_factory(station))
            self._live[station.id] = live
            self._threads = [t for t in self._threads if t.is_alive()]
        if planned and station.transcribe and self._asr_ready.is_set():  # not if listened to
            for model in self._models_for(station):
                if model[0].id not in self._asr_models:
                    self._control(self._asr_q, _LoadAsr(*model))
        if station.translation_targets and self._plan.translator_for(station) is not None:
            self._start_mt()
        self._spawn(f"source-{station.id}", self._source_worker, live)

    def remove_station(self, station_id: str, *, wait: float = 0.0) -> None:
        """Stop a station; utterances already queued are still transcribed. ``wait`` seconds
        for its source to close (before restarting it)."""
        with self._lock:
            live = self._live.get(station_id)
        if live is None:
            return
        live.stopping = True
        live.source.close()
        if wait:
            self._join_source(station_id, wait)

    def _join_source(self, station_id: str, wait: float, *, only_stopping: bool = False) -> None:
        """Wait up to ``wait`` seconds for the station's source worker to end (with
        ``only_stopping``, only when the station is being stopped)."""
        with self._lock:
            live = self._live.get(station_id)
        if live is None or (only_stopping and not live.stopping):
            return
        deadline = time.monotonic() + wait
        for t in list(self._threads):
            if t.name == f"source-{station_id}":
                t.join(timeout=max(0.0, deadline - time.monotonic()))

    def update_station(self, station: Station, asr: tuple[ModelEntry, Path] | None = None) -> None:
        """Apply a changed station. Name, language, priority and translation languages change
        in place for the next utterance; a new source (URL, device, frequency) restarts it."""
        if asr is not None:
            self._plan.asr[station.id] = asr
        with self._lock:
            live = self._live.get(station.id)
        if live is None:
            return
        if _source_key(live.station) != _source_key(station):
            self.remove_station(station.id, wait=10)
            self.add_station(station)
            return
        live.station = station  # transcribe/translate switches apply from the next chunk
        if station.transcribe and self._asr_ready.is_set():
            for model in self._models_for(station):
                if model[0].id not in self._asr_models:
                    self._control(self._asr_q, _LoadAsr(*model))
        if station.translation_targets and self._plan.translator_for(station) is not None:
            self._start_mt()

    def forget_station(self, station_id: str) -> None:
        """Stop a station and drop its plan (it was deleted)."""
        self.remove_station(station_id)
        self._plan.asr.pop(station_id, None)

    def set_translator(self, mt: tuple[ModelEntry, Path] | None) -> None:
        """Use another translator from the next job on (None switches translation off)."""
        self._plan.mt = mt
        if self._mt_started:
            self._control(self._mt_q, _SwapMt(mt))
        elif mt is not None and (
            any(live.station.translation_targets for live in list(self._live.values()))
        ):
            self._start_mt()

    def ingest(self, result: Result) -> None:
        """A transcript made elsewhere (another Wavescribe's feed): store, rules and page see it
        like our own; it is translated here only into languages it doesn't already carry."""
        result.asr_done_at = result.asr_done_at or time.time()
        self._notify(self._on_transcript, result)
        missing = [t for t in result.station.translation_targets if t not in result.translations]
        if missing and self._plan.translator_for(result.station) and result.transcript.text:
            self._start_mt()
            try:
                self._mt_q.put_nowait((result.station.priority, next(self._tiebreak), result))
            except queue.Full:
                self._untranslated(result, "full")
            return
        self._finish(result)

    def request_asr(
        self,
        run: Callable[[Callable[[ModelEntry, Path], Transcriber]], None],
        priority: float = _CONTROL,
    ) -> bool:
        """Run ``run(get)`` on the ASR thread, ahead of utterances (or at ``priority``, which
        sorts with station priorities); ``get(entry, path)`` gives a loaded transcriber
        (loading it if needed). For one-off work such as detecting the language of a stream
        someone is adding."""
        try:
            self._asr_q.put_nowait((priority, next(self._tiebreak), _AsrJob(run)))
        except queue.Full:
            return False
        return True

    def _control(self, q: _WorkQueue, item: object) -> None:
        with contextlib.suppress(queue.Full):
            q.put_nowait((_CONTROL, next(self._tiebreak), item))

    @property
    def ready(self) -> bool:
        """The models the starting stations need are loaded."""
        return self._asr_ready.is_set()

    def run(self, duration: float | None = None, *, until_idle: bool = True) -> None:
        """Run until Ctrl-C, ``duration`` seconds, or (with ``until_idle``) every source ends."""
        self.start()
        deadline = None if duration is None else time.monotonic() + duration
        next_health = time.monotonic() + HEALTH_INTERVAL_S
        drain = False  # on Ctrl-C or a crash, stop at once; otherwise finish queued work
        try:
            while True:
                if self._errors:
                    raise RuntimeError("a pipeline worker failed") from self._errors[0]
                if self._stop.is_set():  # stop() was called from another thread
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    drain = True
                    break
                if until_idle and not self._live:
                    drain = True
                    break
                self._log_drops()
                if time.monotonic() >= next_health:
                    self._log_health()
                    next_health += HEALTH_INTERVAL_S
                time.sleep(0.2)
        finally:
            self.stop(drain=drain)
            self._log_drops(force=True)

    def stop(self, *, drain: bool = False) -> None:
        """Stop the sources; with ``drain``, finish the queued utterances first."""
        self._drain = drain
        if not drain:
            self._stop.set()
        for live in list(self._live.values()):
            live.stopping = True  # also ends a station's wait to restart after a failure
            live.source.close()
        for t in list(self._threads):
            if t.name.startswith("source-"):
                t.join(timeout=10)
        self._put_sentinel(self._asr_q, wait=self._alive("asr"))  # a dead one's never empties
        for t in list(self._threads):
            t.join(timeout=None if drain else 10)
        self._stop.set()

    def status(self) -> dict:
        """A snapshot for health views: per-station state, queue depths, how busy each stage is,
        how far behind each station is and what was dropped (``behind_s``, ``dropped``), and
        whether the machine keeps up (``load``)."""
        now = time.time()
        text_since, mt_since, rules_since = self._waiting()
        drops = self._recent_drops.totals(now)
        stations = {}
        for sid, live in list(self._live.items()):
            h = live.source.health
            # Why its capture failed, else why its source is down and retrying on its own.
            error = live.error or getattr(h, "error", None)
            in_speech = bool(live.segmenter and live.segmenter.in_speech)
            music = (
                live.last_music_at is not None
                and live.last_music_at > (live.last_talk_at or 0)
                and (in_speech or now - live.last_music_at < 60)
            )
            stations[sid] = {
                # Down after a failure until it hears audio again, whatever its source last said.
                "connected": h.connected and error is None,
                "reconnects": h.reconnects + live.restarts,
                "last_data_age": round(now - h.last_data_at, 1) if h.last_data_at else None,
                "in_speech": in_speech,
                # The last utterance was a song (and it's still playing or just ended).
                "music": music,
                # Seconds since the last utterance worth transcribing.
                "quiet_s": round(now - (live.last_talk_at or live.started_at)),
                "level": round(live.level, 4),
                "stopping": live.stopping,
                # How long its oldest unfinished speech has waited for text, and its oldest
                # transcript for translation (None: nothing waits).
                "behind_s": {
                    "text": _age(now, text_since.get(sid)),
                    "translation": _age(now, mt_since.get(sid)),
                },
                # Work of this station dropped in the last ten minutes: speech never written
                # down, lines left untranslated, LLM rule checks that couldn't run.
                "dropped": {
                    stage: sum(n for (st, _, s), n in drops.items() if st == stage and s == sid)
                    for stage in ("asr", "mt", "rules")
                },
            }
            if error is not None:  # why its capture failed, and when it is tried again
                stations[sid]["error"] = error
                retry_at = live.retry_at if live.error is not None else h.retry_at or 0.0
                stations[sid]["retry_in_s"] = round(max(0.0, retry_at - time.monotonic()), 1)
            if h.signal_db is not None:  # SDR sources: signal strength and overload
                stations[sid]["signal_db"] = round(h.signal_db, 1)
                stations[sid]["clipped"] = h.clipped
            if h.rds is not None:
                stations[sid]["rds"] = h.rds
            if h.squelch_open is not None:
                stations[sid]["squelch_open"] = h.squelch_open
            if h.warning is not None:
                stations[sid]["warning"] = h.warning
        return {
            "ready": self.ready,
            "stations": stations,
            "asr_queue": self._asr_q.qsize(),
            "mt_queue": self._mt_q.qsize(),
            "dropped": dict(self._dropped),
            "rechecks": self._plan.recheck.status() if self._plan.recheck else None,
            "music_skipped_s": round(self._music_s),
            "asr_busy": round(self._busy_fraction("asr", now), 3),
            "mt_busy": round(self._busy_fraction("mt", now), 3),
            "models": {
                "asr": sorted(self._asr_models),
                "mt": self._mt_model,
                "loading": self._loading,
                "errors": dict(self._model_errors),
            },
            "lag": self.lag(now),
            "load": self._load(now, text_since, mt_since, rules_since, drops),
        }

    def _waiting(self) -> tuple[dict[str, float], dict[str, float], float | None]:
        """Since when the oldest unfinished work of each station has waited: speech for text
        (from when it was cut), transcripts for translation (from when their text was ready),
        and the oldest LLM rule check waiting for the LLM."""
        text: dict[str, float] = {}
        mt: dict[str, float] = {}
        rules: float | None = None

        def oldest(into: dict[str, float], sid: str, t: float) -> None:
            into[sid] = min(t, into.get(sid, t))

        for _, _, item in self._asr_q.snapshot():
            if isinstance(item, tuple):
                oldest(text, item[0].id, item[1].ready_at)
        for _, _, item in self._mt_q.snapshot():
            if isinstance(item, Result):
                oldest(mt, item.station.id, item.asr_done_at)
            elif isinstance(item, LlmJob) and item.name == "llm-rules":
                rules = item.queued_at if rules is None else min(rules, item.queued_at)
        for into, doing in ((text, self._asr_doing), (mt, self._mt_doing)):
            if doing is not None:
                oldest(into, *doing)
        return text, mt, rules

    def _load(
        self,
        now: float,
        text_since: dict[str, float],
        mt_since: dict[str, float],
        rules_since: float | None,
        drops: dict[tuple[str, str, str], int],
    ) -> dict[str, str | None]:
        """Whether the machine keeps up: ``ok``, ``behind`` (text or translation late) or
        ``overloaded`` (speech or rule checks lost, text or rule checks minutes late, or
        translations too late to be made), with a plain sentence saying the worst of it."""

        def lost(stage: str, whys: tuple[str, ...] = CAPACITY) -> int:
            return sum(n for (st, why, _), n in drops.items() if st == stage and why in whys)

        def worst(since: dict[str, float]) -> tuple[float, str]:
            if not since:
                return 0.0, ""
            sid = min(since, key=since.__getitem__)
            live = self._live.get(sid)
            return now - since[sid], live.station.name if live else sid

        text_s, text_where = worst(text_since)
        mt_s, _ = worst(mt_since)
        rules_s = now - rules_since if rules_since is not None else 0.0
        machine = "this Mac" if sys.platform == "darwin" else "this computer"
        many = f"Too many stations for {machine}: "
        if n := lost("rules"):
            return _state("overloaded", f"{many}{_count(n, 'LLM rule check')} couldn't run in "
                          "the last 10 minutes, so alerts may be missed")  # fmt: skip
        if n := lost("asr"):
            return _state("overloaded", f"{many}{_count(n, 'utterance')} went unwritten in the "
                          "last 10 minutes")  # fmt: skip
        if text_s >= TEXT_OVERLOAD_S:
            return _state("overloaded", f"{many}text is {_span(text_s)} behind on {text_where}")
        if rules_s >= RULES_OVERLOAD_S:
            return _state("overloaded", f"{many}LLM rule checks are {_span(rules_s)} behind, so "
                          "alerts come late")  # fmt: skip
        if mt_s >= MT_STALE_S:
            return _state("overloaded", f"{many}translation is {_span(mt_s)} behind")
        if n := lost("mt", ("stale",)):
            return _state("overloaded", f"{many}{_count(n, 'line')} waited over "
                          f"{_span(MT_STALE_S)} for translation in the last 10 minutes and "
                          "stayed untranslated")  # fmt: skip
        if text_s >= TEXT_BEHIND_S:
            return _state("behind", f"Text is {_span(text_s)} behind on {text_where}")
        if mt_s >= MT_BEHIND_S:
            return _state("behind", f"Translation is {_span(mt_s)} behind")
        if n := lost("mt", ("full",)):
            return _state("behind", f"{_count(n, 'line')} stayed untranslated in the last 10 "
                          "minutes: the translation queue was full")  # fmt: skip
        return _state("ok", None)

    # -- drops ------------------------------------------------------------------------------

    def _drop(self, stage: str, station_id: str | None, why: str) -> None:
        """Count work that was dropped: in the totals, per station over the last ten minutes,
        and in the log once a minute (``_log_drops``), never once per item."""
        now = time.time()
        with self._drop_lock:
            self._dropped[stage] = self._dropped.get(stage, 0) + 1
            if why != "failed":  # a model that raised has logged it already
                self._unlogged[stage, why, station_id or "-"] += 1
        if station_id is not None:
            self._recent_drops.add((stage, why, station_id), now)
        self._log_drops()

    def _untranslated(self, result: Result, why: str) -> None:
        """A transcript that won't be translated (no room, or too late): counted, and finished
        with the text alone, so it is stored and shown all the same."""
        self._drop("mt", result.station.id, why)
        self._finish(result)

    def _current(self, station: Station) -> Station:
        """The station as it is now (its translation languages, its translator), for work
        queued before a change; as it was, once it has stopped. The language stays the one the
        line was heard in (a station that hears any language, or one changed meanwhile)."""
        live = self._live.get(station.id)
        if live is None:
            return station
        return replace(live.station, language=station.language)

    def _skip_stale(self) -> None:
        """Take every station transcript that waited ``MT_STALE_S`` for translation out of the
        queue, at most every few seconds (translation thread): it is shown untranslated now
        rather than translated late, after more important stations' lines."""
        now = time.time()
        if now - self._mt_swept_at < 5:
            return
        self._mt_swept_at = now
        stale = self._mt_q.take(
            lambda e: isinstance(e[2], Result) and now - e[2].asr_done_at >= MT_STALE_S
        )
        for _, _, result in stale:
            self._untranslated(result, "stale")

    def _log_drops(self, force: bool = False) -> None:
        """One warning per stage and reason for the drops since the last one, at most every
        ``DROP_LOG_S``: the first drop after a quiet spell is logged at once."""
        now = time.monotonic()
        with self._drop_lock:
            if not self._unlogged or (not force and now - self._drops_logged_at < DROP_LOG_S):
                return
            pending, self._unlogged = self._unlogged, Counter()
            self._drops_logged_at = now
        grouped: dict[tuple[str, str], Counter[str]] = {}
        for (stage, why, sid), n in pending.items():
            grouped.setdefault((stage, why), Counter())[sid] += n
        for (stage, why), per in grouped.items():
            event(log, "drop", level=logging.WARNING, stage=stage, why=why,
                  n=sum(per.values()), stations=",".join(f"{s}:{n}" for s, n in per.items()),
                  next_log_in_s=DROP_LOG_S)  # fmt: skip

    def child_pids(self) -> dict[str, int]:
        """The helper processes our sources run (ffmpeg, rtl_sdr), by station."""
        out = {}
        for sid, live in list(self._live.items()):
            proc = getattr(live.source, "_proc", None) or getattr(
                getattr(live.source, "_device", None), "_proc", None
            )
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int):
                out[sid] = pid
        return out

    def lag(self, now: float | None = None) -> dict[str, dict[str, float | int | None]]:
        """Per station over the last ten minutes: seconds from the end of an utterance to its
        text (``text_*``) and to its translations (``translated_*``), median and 95th
        percentile. Streams also lag the broadcast by their own buffering, which no one
        can measure from here."""
        now = time.time() if now is None else now
        out: dict[str, dict[str, float | int | None]] = {}
        for sid, rows in list(self._lag.items()):
            recent = [r for r in list(rows) if r[0] > now - LAG_WINDOW_S]
            text = sorted(r[1] for r in recent)
            done = sorted(r[2] for r in recent if r[2] is not None)
            out[sid] = {
                "n": len(recent),
                "text_p50": _pct(text, 0.5),
                "text_p95": _pct(text, 0.95),
                "translated_p50": _pct(done, 0.5),
                "translated_p95": _pct(done, 0.95),
            }
        return out

    def request_translation(
        self,
        text: str,
        source: str,
        targets: list[str] | tuple[str, ...],
        callback: Callable[[dict[str, str], str | None], None],
    ) -> None:
        """Translate ``text`` ahead of the queue and call back from the translation thread.

        For alerts: detection never waits for this; delivery waits a bounded time. Calls back
        with {} when there is no translator or the queue is full.
        """
        unsafe = self._plan.mt[0].unsafe_targets if self._plan.mt else ()
        wanted = tuple(
            t
            for t in targets
            if not same_language(t, source) and not any(same_language(t, u) for u in unsafe)
        )
        if self._plan.mt is None or not wanted:
            callback({}, None)
            return
        self._start_mt()  # no station may translate, and an alert still needs the worker
        try:  # priority 0: ahead of every station (station priorities start at 1)
            self._mt_q.put_nowait(
                (0, next(self._tiebreak), TranslationJob(text, source, wanted, callback))
            )
        except queue.Full:  # only jobs in the queue, none of them station lines
            self._drop("mt", None, "full")
            callback({}, None)

    def request_llm(
        self,
        run: Callable[[Translator], None],
        name: str = "llm",
        priority: float = LLM_JOB_PRIORITY,
        station: str | None = None,
    ) -> bool:
        """Queue ``run(translator)`` on the translation thread. False if there is no translator
        or its queue is full of other jobs (station lines make way for it). An LLM rule check
        (``name`` "llm-rules") that can't be queued is counted against its ``station``: it is
        a possibly missed alert."""
        rules = name == "llm-rules"
        if self._plan.mt is None:
            if rules:
                self._drop("rules", station, "no_llm")
            return False
        self._start_mt()
        try:
            self._mt_q.put_nowait((priority, next(self._tiebreak), LlmJob(run, name, station)))
        except queue.Full:
            if rules:
                self._drop("rules", station, "full")
            else:
                event(log, "drop", level=logging.WARNING, stage="mt", station=name)
            return False
        return True

    def llm_waiting(self, below: float) -> bool:
        """Whether a job of priority under ``below`` waits for the translation thread: what a
        long job of lower priority gives way to (a digest's call)."""
        with self._mt_q.mutex:
            return bool(self._mt_q.queue) and self._mt_q.queue[0][0] < below

    def _busy_fraction(self, stage: str, now: float, window: float = 60.0) -> float:
        return min(1.0, sum(d for t, d in list(self._busy[stage]) if t > now - window) / window)

    def _alive(self, name: str) -> bool:
        return any(t.name == name and t.is_alive() for t in list(self._threads))

    def _put_sentinel(self, q: _WorkQueue, wait: bool = True, drained: bool | None = None) -> None:
        """Tell a worker to finish: after its queued work (``drained``, by default when the
        stop drains) or at once."""
        if (self._drain if drained is None else drained) and wait:
            # The worker is consuming, so room will appear; unless a stop that doesn't drain
            # comes meanwhile and the worker ends without taking it.
            with contextlib.suppress(queue.Full):
                q.put((_STOP_DRAINED, 0, None), give_up=self._stop.is_set)
        else:
            with contextlib.suppress(queue.Full):  # a busy worker sees _stop after this item
                q.put_nowait((_STOP_NOW, 0, None))

    def _spawn(self, name: str, target: Callable, *args: object) -> None:
        def guarded() -> None:
            try:
                target(*args)
            except BaseException as e:
                log.exception("worker %s crashed", name)
                self._errors.append(e)

        t = threading.Thread(target=guarded, name=name, daemon=True)
        with self._lock:  # add_station replaces the list while it drops finished threads
            self._threads.append(t)
        t.start()

    def _notify(self, callback: Callable | None, *args: object) -> None:
        # Observers (views) must never take a worker down with them.
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            log.exception("observer %r failed", callback)

    def _failed(self, stage: str, model_id: str, work: str, error: Exception) -> None:
        """A model call raised (call this from the ``except`` block). It costs the one item,
        never the worker: a single utterance a model chokes on must not end the listening.

        ``work`` says what was being done ("station tsf", "into uk"). Failures are counted in a
        row per model and work, so a target language that never works isn't hidden by the
        ones that do.
        """
        n = self._failures[model_id, work] = self._failures.get((model_id, work), 0) + 1
        # The traceback once per run of failures, one line for each failure after it.
        fields = {"stage": stage, "model": model_id, "work": work, "in_a_row": n,
                  "error": str(error)[:200]}  # fmt: skip
        log.error("model_call_failed", exc_info=n == 1, extra={"fields": fields})
        if n >= FAILURES_BEFORE_ERROR:
            self._model_errors[model_id] = f"failed {n} times in a row ({work}): {error}"[:300]

    def _worked(self, model_id: str, work: str) -> None:
        if self._failures.pop((model_id, work), 0) >= FAILURES_BEFORE_ERROR:
            self._model_errors.pop(model_id, None)

    # -- workers ------------------------------------------------------------------------------

    def _source_worker(self, live: _Live) -> None:
        station = live.station
        try:
            # Don't queue audio while the models are still loading (a station that is only
            # listened to needs none, and starts at once).
            while live.station.transcribe and not self.ready:
                if self._stop.wait(0.2) or self._errors or live.stopping:
                    return
            while not (live.stopping or self._stop.is_set()):
                started = time.monotonic()
                try:
                    self._capture(live)
                    return  # the source ended: it was closed, or a file was played to its end
                except Exception as e:
                    self._station_failed(live, e, time.monotonic() - started)
                # In short steps, so that stopping the station or the pipeline stays prompt.
                while (left := live.retry_at - time.monotonic()) > 0 and not live.stopping:
                    if self._stop.wait(min(left, 0.2)):
                        break
        finally:
            with self._lock:
                if self._live.get(station.id) is live:
                    del self._live[station.id]

    def _station_failed(self, live: _Live, error: Exception, ran_s: float) -> None:
        """The station's capture raised (call this from the ``except`` block). Like a model call
        in ``_failed``, it is counted in a row and logged with its traceback once per run of
        failures; the station is tried again after a delay that doubles each time."""
        if ran_s >= RESTART_STEADY_S:
            live.failures = 0
        live.failures += 1
        live.restarts += 1
        delay = min(RESTART_MAX_S, RESTART_INITIAL_S * 2 ** min(live.failures - 1, 16))
        live.retry_at = time.monotonic() + delay
        live.error = (str(error) or type(error).__name__)[:300]
        fields = {
            "station": live.station.id,
            "in_a_row": live.failures,
            "retry_in_s": round(delay, 1),
            "error": live.error[:200],
        }
        log.error("station_failed", exc_info=live.failures == 1, extra={"fields": fields})

    def _capture(self, live: _Live) -> None:
        """Read the station's source until it ends, cutting its audio into utterances. Each run
        makes its own VAD and gate: after a failure, the old ones may be what raised."""
        station = live.station
        gate: Gate | None = None
        live.segmenter = None
        chunks = iter(live.source)
        try:
            for chunk in chunks:
                if self._stop.is_set():
                    break
                live.error = None  # it hears audio, so it is up
                live.level = float(np.sqrt(np.mean(chunk.samples**2))) if len(chunk.samples) else 0
                self._notify(self._on_audio, station.id, chunk)
                if not live.station.transcribe or self._plan.vad is None:
                    if live.segmenter is not None:  # switched off: finish what was being said
                        for utt in live.segmenter.flush():
                            self._gate_and_enqueue(live, gate, utt)
                        live.segmenter = None
                    continue
                if live.segmenter is None:
                    live.segmenter = VadSegmenter(station.id, self._vad_factory(self._plan.vad[1]))
                    # One gate per station: LiteRT interpreters aren't thread-safe (~4 MB each).
                    if gate is None and self._plan.gate:
                        gate = self._gate_factory(self._plan.gate[1])
                if (checked := getattr(live.source, "checked", None)) is not None:
                    checked(chunk)  # a watch tells a catch nobody spoke in from one unchecked
                for utt in live.segmenter.feed(chunk):
                    self._gate_and_enqueue(live, gate, utt)
                if live.segmenter.in_speech:
                    live.last_speech_at = time.time()
            if live.segmenter is not None:
                for utt in live.segmenter.flush():
                    self._gate_and_enqueue(live, gate, utt)
        finally:
            # A source holds its ffmpeg or its dongle until its iterator is closed, and the
            # traceback of a failure would keep this frame, and so the iterator, alive.
            close = getattr(chunks, "close", None)
            if close is not None:
                close()

    def _gate_and_enqueue(self, live: _Live, gate: Gate | None, utt: Utterance) -> None:
        if gate is not None:
            t0 = time.perf_counter()
            try:
                verdict = gate.classify(utt.samples)
            except Exception as e:
                # No verdict: transcribe it. A song in the transcript costs less than lost speech.
                self._failed("gate", gate.model_id, f"station {live.station.id}", e)
                verdict = Verdict("speech", 0.0, 0.0)
            else:
                self._worked(gate.model_id, f"station {live.station.id}")
            if verdict.kind == "music":
                live.last_music_at = time.time()
                self._music_s += utt.duration
                event(log, "gated", station=live.station.id, kind="music",
                      s=round(utt.duration, 1), speech=verdict.speech, music=verdict.music,
                      ms=_ms(t0))  # fmt: skip
                return
            # Passes too, at debug level: what tuning the thresholds needs.
            event(log, "gated", level=logging.DEBUG, station=live.station.id, kind="speech",
                  s=round(utt.duration, 1), speech=verdict.speech, music=verdict.music,
                  ms=_ms(t0))  # fmt: skip
        live.last_talk_at = time.time()
        speech = getattr(live.source, "speech", None)  # a watch notes which catches had speech
        if speech is not None:
            speech(utt)
        self._enqueue_asr(live.station, utt)

    def _enqueue_asr(self, station: Station, utt: Utterance) -> None:
        try:
            self._asr_q.put_nowait((station.priority, next(self._tiebreak), (station, utt)))
        except queue.Full:
            self._drop("asr", station.id, "full")

    def _models_for(self, station: Station) -> list[tuple[ModelEntry, Path]]:
        """The speech models a station needs loaded: its own, and the Whisper that re-checks
        its lines heard in another language (asr/recheck.py), if one is installed."""
        planned = self._plan.asr.get(station.id)
        if planned is None:
            return []
        recheck = self._plan.recheck if self._on_rechecked is not None else None
        second = recheck.model_for(station, planned[0]) if recheck is not None else None
        return [planned] if second is None else [planned, second]

    def _load_asr(self, loaded: dict[str, Transcriber], entry: ModelEntry, path: Path) -> None:
        if entry.id in loaded:
            self._asr_models[entry.id] = time.time()  # in use again: not idle
            return
        with self._load_lock:
            self._loading = entry.id
            try:
                t0 = time.perf_counter()
                loaded[entry.id] = self._transcriber_factory(entry, path)
                event(log, "model_loaded", model=entry.id, ms=_ms(t0))
            finally:
                self._loading = None
        self._asr_models[entry.id] = time.time()
        self._model_errors.pop(entry.id, None)

    def _unload_idle(self, loaded: dict[str, Transcriber]) -> None:
        """Free ASR models no running station uses (Whisper alone holds ~1.6 GB)."""
        with self._lock:
            running = [live.station for live in self._live.values()]
        wanted = {entry.id for s in running for entry, _ in self._models_for(s)}
        now = time.time()
        idle = [m for m in loaded if m not in wanted
                and now - self._asr_models.get(m, 0) > ASR_IDLE_UNLOAD_S]  # fmt: skip
        for model_id in idle:
            del loaded[model_id]
            self._asr_models.pop(model_id, None)
            event(log, "model_unloaded", model=model_id)
        if idle:
            gc.collect()
            mx = sys.modules.get("mlx.core")  # only if MLX is already in use
            if mx is not None and hasattr(mx, "clear_cache"):
                mx.clear_cache()

    def _asr_worker(self) -> None:
        try:
            self._asr_work()
        finally:
            # However it ended, a crash included: the translation worker is told to finish,
            # and nothing waits for this one to be ready. A drained stop joins every worker
            # without a timeout, so it would otherwise wait forever. Unless the run is stopping
            # at once, it finishes the lines already waiting for translation first: a crash
            # here must not lose what was heard before it.
            self._asr_ready.set()
            self._put_sentinel(self._mt_q, wait=self._alive("mt"),
                               drained=not self._stop.is_set())  # fmt: skip

    def _asr_work(self) -> None:
        # Models load in this thread (MLX arrays are thread-bound): the starting stations'
        # models now, any other when its station first needs it.
        # A station only listened to this run loads nothing, even with a model planned.
        loaded: dict[str, Transcriber] = {}
        writing = [s for s in self._stations if s.transcribe]
        for station in writing:
            if station.id in self._plan.asr:
                self._load_asr(loaded, *self._plan.asr[station.id])
        if self._plan.recheck is not None and self._on_rechecked is not None:
            self._plan.recheck.preload(writing, self._plan.asr,
                                       lambda e, p: self._load_asr(loaded, e, p))  # fmt: skip
        self._asr_ready.set()
        last_sweep = time.monotonic()

        while True:
            if time.monotonic() - last_sweep > 60:
                self._unload_idle(loaded)
                last_sweep = time.monotonic()
            try:
                _, _, item = self._asr_q.get(timeout=60)
            except queue.Empty:
                continue
            if item is None or self._stop.is_set():
                break
            if isinstance(item, _AsrJob):
                try:
                    item.run(lambda e, p: (self._load_asr(loaded, e, p), loaded[e.id])[1])
                except Exception:
                    log.exception("ASR job failed")
                continue
            if isinstance(item, _LoadAsr):
                try:
                    self._load_asr(loaded, item.entry, item.path)
                except Exception as e:
                    log.exception("loading %s failed", item.entry.id)
                    self._model_errors[item.entry.id] = str(e)[:300]
                continue
            station, utt = item
            planned = self._plan.asr.get(station.id)
            if planned is None:  # the station was deleted while this waited
                continue
            self._asr_doing = (station.id, utt.ready_at)
            try:
                self._hear(loaded, station, utt, planned)
            finally:
                self._asr_doing = None

    def _hear(
        self,
        loaded: dict[str, Transcriber],
        station: Station,
        utt: Utterance,
        planned: tuple[ModelEntry, Path],
    ) -> None:
        """Transcribe one utterance and hand it on (ASR thread)."""
        if planned[0].id not in loaded:
            try:
                self._load_asr(loaded, *planned)
            except Exception as e:
                log.exception("loading %s failed", planned[0].id)
                self._model_errors[planned[0].id] = str(e)[:300]
                self._drop("asr", station.id, "failed")
                return
        self._asr_models[planned[0].id] = time.time()
        started = time.time()
        t0 = time.perf_counter()
        transcriber = loaded[planned[0].id]
        try:
            if station.language == AUTO:
                transcript = transcribe_any(transcriber, utt.samples, station.languages)
                # From here on, the utterance is one of a station in the language heard.
                station = replace(station, language=transcript.language)
            else:
                transcript = transcriber.transcribe(utt.samples, station.language)
            asr_ms = _ms(t0)
            check = check_transcript(transcript.text, utt.duration, station.language)
        except Exception as e:
            self._drop("asr", station.id, "failed")
            self._failed("asr", planned[0].id, f"station {station.id}", e)
            return
        self._worked(planned[0].id, f"station {station.id}")
        if check.drop:
            self._dropped["filter"] += 1
            event(log, "filtered", station=station.id, reason=check.drop,
                  chars=len(transcript.text), s=round(utt.duration, 1),
                  text=logged_text(station, transcript.text, 80))  # fmt: skip
            self._notify(self._on_filtered, Result(station, utt, transcript), check.drop)
            return
        result = Result(station, utt, transcript, flags=check.flags)
        result.timings.update(
            segment_wait_ms=(utt.ready_at - utt.t_end) * 1000,
            asr_queue_ms=(started - utt.ready_at) * 1000,
            asr_ms=asr_ms,
            asr_rtf=asr_ms / (utt.duration * 1000),
        )
        result.asr_done_at = time.time()
        self._busy["asr"].append((result.asr_done_at, asr_ms / 1000))
        if check.flags and self._plan.recheck and self._on_rechecked:
            result.rechecking = self._plan.recheck.offer(
                result, planned[0], self.request_asr, self._rechecked
            )
        if transcript.text:
            self._notify(self._on_transcript, result)
        if not result.rechecking:
            self._translate_or_finish(result)

    def _rechecked(self, result: Result) -> None:
        """Whisper has listened to a flagged line again (asr/recheck.py), on the ASR thread:
        its text is final, so it is judged and translated now."""
        result.rechecking = False
        self._busy["asr"].append((time.time(), result.timings.get("recheck_ms", 0.0) / 1000))
        self._notify(self._on_rechecked, result)
        self._translate_or_finish(result)

    def _translate_or_finish(self, result: Result) -> None:
        station = result.station
        translate = station.translation_targets and self._plan.translator_for(station)
        if translate and result.transcript.text:
            self._start_mt()
            try:
                self._mt_q.put_nowait((station.priority, next(self._tiebreak), result))
            except queue.Full:
                self._untranslated(result, "full")
        else:
            self._finish(result)

    def _load_mt(self, mt: tuple[ModelEntry, Path] | None = None) -> Translator | None:
        mt = mt or self._plan.mt
        if mt is None:
            return None
        entry, path = mt
        with self._load_lock:
            self._loading = entry.id
            try:
                t0 = time.perf_counter()
                translator = self._translator_factory(entry, path)
                event(log, "model_loaded", model=entry.id, ms=_ms(t0))
            except Exception as e:
                log.exception("loading %s failed", entry.id)
                self._model_errors[entry.id] = str(e)[:300]
                return None
            finally:
                self._loading = None
        self._model_errors.pop(entry.id, None)
        if self._plan.mt is not None and entry.id == self._plan.mt[0].id:
            self._mt_model = translator.model_id
        return translator

    def _mt_worker(self) -> None:
        # Load after the starting stations' ASR models, never alongside them (see _load_lock).
        while not self._asr_ready.wait(0.2):
            if self._stop.is_set() or self._errors:
                return
        # The shared translator, plus any a station chose on the page; those load when first
        # needed and are dropped after MT_IDLE_UNLOAD_S unused (each can take gigabytes).
        loaded: dict[str, Translator] = {}
        last_used: dict[str, float] = {}

        def get(mt: tuple[ModelEntry, Path] | None) -> Translator | None:
            if mt is None:
                return None
            t = loaded.get(mt[0].id)
            if t is None:
                t = self._load_mt(mt)
                if t is None:
                    return None
                loaded[mt[0].id] = t
            last_used[mt[0].id] = time.time()
            return t

        def unload_idle() -> None:
            shared = self._plan.mt[0].id if self._plan.mt else None
            for mid in [m for m, t in last_used.items() if m != shared]:
                if time.time() - last_used[mid] > MT_IDLE_UNLOAD_S:
                    loaded.pop(mid, None)
                    last_used.pop(mid, None)
                    gc.collect()
                    event(log, "model_unloaded", model=mid)

        if any(s.translation_targets for s in self._stations):
            get(self._plan.mt)

        while True:
            try:
                _, _, result = self._mt_q.get(timeout=60)
            except queue.Empty:
                unload_idle()
                continue
            if result is None or self._stop.is_set():
                break
            self._skip_stale()
            if isinstance(result, Result) and time.time() - result.asr_done_at >= MT_STALE_S:
                self._untranslated(result, "stale")
                continue
            if isinstance(result, _SwapMt):
                loaded.clear()
                last_used.clear()
                self._mt_model = None
                gc.collect()
                continue
            # A station line is translated as its station is now: languages removed while it
            # waited are left out, and with none left it goes on with its text alone.
            current = self._current(result.station) if isinstance(result, Result) else None
            if current is not None and not [
                t for t in current.translation_targets if t not in result.translations
            ]:
                self._finish(result)
                continue
            if current is None:
                translator = get(self._plan.mt)
            else:
                translator = get(self._plan.translator_for(current))
            if translator is None:  # no translator, or it failed to load: pass work through
                if isinstance(result, TranslationJob):
                    self._notify(result.callback, {}, None)
                elif isinstance(result, Result):
                    self._finish(result)
                continue
            if isinstance(result, TranslationJob):
                self._run_job(translator, result)
                continue
            if isinstance(result, LlmJob):
                t0 = time.perf_counter()
                try:
                    result.run(translator)
                except Exception:
                    log.exception("%s job failed", result.name)
                self._busy["mt"].append((time.time(), _ms(t0) / 1000))
                continue
            queued_ms = (time.time() - result.asr_done_at) * 1000
            t0 = time.perf_counter()
            self._mt_doing = (result.station.id, result.asr_done_at)
            try:
                for target in current.translation_targets:
                    if target in result.translations:  # it came with this one (another node's)
                        continue
                    text = self._translate(
                        translator, result.transcript.text, result.station.language, target
                    )
                    if text is not None:
                        result.translations[target] = text
            finally:
                self._mt_doing = None
            mt_ms = _ms(t0)
            result.translations_model = translator.model_id
            result.timings.update(mt_queue_ms=queued_ms, mt_ms=mt_ms)
            self._busy["mt"].append((time.time(), mt_ms / 1000))
            self._finish(result)
            unload_idle()

    def _translate(self, translator: Translator, text: str, source: str, target: str) -> str | None:
        """One translation, or None when the model raised: the utterance or alert then goes
        on without this language."""
        try:
            out = translator.translate(text, source, target)
        except Exception as e:
            with self._drop_lock:
                self._dropped["mt"] += 1
            self._failed("mt", translator.model_id, f"into {target}", e)
            return None
        self._worked(translator.model_id, f"into {target}")
        return out

    def _run_job(self, translator: Translator, job: TranslationJob) -> None:
        t0 = time.perf_counter()
        out: dict[str, str] = {}
        for target in job.targets:
            text = self._translate(translator, job.text, job.source, target)
            if text is not None:
                out[target] = text
        self._busy["mt"].append((time.time(), _ms(t0) / 1000))
        event(log, "translation_job", targets=",".join(job.targets), ms=_ms(t0))
        self._notify(job.callback, out, translator.model_id)

    def _finish(self, result: Result) -> None:
        u = result.utterance
        now = time.time()
        result.timings["total_ms"] = (now - u.t_end) * 1000
        lag = self._lag.setdefault(result.station.id, deque(maxlen=500))
        text_s = (result.asr_done_at or now) - u.t_end
        lag.append((now, text_s, (now - u.t_end) if result.translations else None))
        event(
            log,
            "utterance",
            station=result.station.id,
            seq=u.seq,
            t_start=round(u.t_start, 3),
            audio_s=round(u.duration, 2),
            asr_model=result.transcript.model,
            chars=len(result.transcript.text),
            **{k: round(v, 3 if k.endswith("rtf") else 1) for k, v in result.timings.items()},
        )
        self._notify(self._on_result, result)

    def _log_health(self) -> None:
        sources = {
            sid: {
                "connected": live.source.health.connected,
                "reconnects": live.source.health.reconnects,
                **(
                    {"signal_db": round(live.source.health.signal_db, 1)}
                    if live.source.health.signal_db is not None
                    else {}
                ),
            }
            for sid, live in list(self._live.items())
        }
        event(
            log,
            "health",
            peak_rss_mb=round(_peak_rss_bytes() / 2**20),
            asr_queue=self._asr_q.qsize(),
            mt_queue=self._mt_q.qsize(),
            dropped=dict(self._dropped),
            sources=sources,
        )


def _source_key(s: Station) -> tuple:
    """What a station listens to: when it changes, its source restarts."""
    return (s.stream, s.stream_hls, s.sdr, s.remote, s.remote_station)


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return round(values[min(len(values) - 1, int(q * len(values)))], 1)


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # Linux reports KiB


def _ms(t0: float) -> float:
    return (time.perf_counter() - t0) * 1000


def _age(now: float, since: float | None) -> float | None:
    return None if since is None else round(max(0.0, now - since), 1)


def _state(state: str, reason: str | None) -> dict[str, str | None]:
    return {"state": state, "reason": reason}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}{'' if n == 1 else 's'}"


def _span(seconds: float) -> str:
    """A wait in words: "40 s", "6 minutes", "1.5 hours"."""
    if seconds < 90:
        return f"{max(1, round(seconds))} s"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{seconds / 3600:.1f} hours"
