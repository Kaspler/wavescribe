# SPDX-License-Identifier: Apache-2.0
"""A second opinion from Whisper on lines heard in another language than the station's.

The ASR checks flag real-looking text in another language (``asr/checks.py``): a song, a real
interview in that language, or the speech model translating instead of transcribing. Parakeet
picks the language itself and can't be told which one to expect, and now and then it renders a
station's own speech in English. Whisper can be told the language, and it can also say which
language it hears. So when a line is flagged and a Whisper model that covers the station's
language is installed, Whisper listens to the same audio again:

- It hears the station's language: the first model mis-rendered it. Whisper transcribes it in
  that language, and if its text passes the checks it replaces the flagged text, with Whisper as
  the model and the ``rechecked`` flag.
- It hears another language: the line really is in it, and stays as it was, flagged, with
  ``rechecked`` added.

It runs on the ASR thread, behind every live utterance, so it never delays a station's text;
the flagged line is shown and stored at once, and updated when the verdict is in. The rules,
the translation and the finished result wait for the verdict (``Pipeline._rechecked``), so
each line is judged and translated once, on its final text. Waiting re-checks are bounded per
station and in total; a line that finds no room stays flagged, and the log says so.

Whisper loads with the stations it re-checks, before their audio is read, and stays loaded
while one of them runs (about 1.6 GB for large-v3-turbo). A station transcribed by Whisper
itself has nothing to gain: Whisper was told its language.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from wavescribe.asr.checks import check_transcript
from wavescribe.invocation import typed
from wavescribe.languages import same_language
from wavescribe.log import event
from wavescribe.radio.bandplan import logged_text
from wavescribe.registry import ModelEntry, ModelRegistry

log = logging.getLogger(__name__)

MAX_PER_STATION = 3  # re-checks waiting for one station; a song's leaked lyrics come in runs
MAX_TOTAL = 8  # re-checks waiting in all (each holds its utterance's audio, ≤ 1.3 MB)
MAX_WAIT_S = 60.0  # one that waited longer finds the ASR far behind: it is skipped
# Queue priority, added to the station's: after every live utterance (station priorities are
# small numbers), before the pipeline's "stop when drained" marker (infinity).
PRIORITY = 1_000_000.0
FLAG = "rechecked"
MISSING_TTL_S = 300.0  # how long "no Whisper for this language" is believed (a download ends it)

Model = tuple[ModelEntry, Path]
GetModel = Callable[[ModelEntry, Path], Any]  # loads (if needed) and returns a transcriber
Submit = Callable[[Callable[[GetModel], None], float], bool]  # (job, priority) on the ASR thread


def is_whisper(entry: ModelEntry) -> bool:
    return "whisper" in entry.id.lower()


def other_language(flags: Iterable[str]) -> str | None:
    """The language the ASR checks flagged a line as, if they did."""
    return next((f[5:] for f in flags if f.startswith("lang:")), None)


class Rechecker:
    """Chooses the Whisper for each station, bounds the waiting re-checks and runs them."""

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        max_per_station: int = MAX_PER_STATION,
        max_total: int = MAX_TOTAL,
        max_wait_s: float = MAX_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._registry = registry
        self.max_per_station = max_per_station
        self.max_total = max_total
        self.max_wait_s = max_wait_s
        self._clock = clock
        self._lock = threading.Lock()
        self._waiting: dict[str, int] = {}  # station id -> re-checks queued or running
        self._found: dict[str, tuple[float, Model | None]] = {}  # language -> (when, model)
        self._broken: set[str] = set()  # models that failed to load: not tried again
        self.counts = {"replaced": 0, "kept": 0, "skipped": 0}

    @classmethod
    def plan(
        cls, registry: ModelRegistry, stations: Iterable[Any], asr: dict[str, Model]
    ) -> Rechecker:
        """The re-checker for a run. When no Whisper covers any station that could use one, say
        once which model would."""
        rechecker = cls(registry)
        wanting = [s for s in stations if s.id in asr and not is_whisper(asr[s.id][0])]
        if wanting and not any(rechecker.model_for(s, asr[s.id][0]) for s in wanting):
            known = next((e for s in wanting for e in rechecker._candidates(s.language)), None)
            if known is not None:
                log.info("lines heard in another language than their station's aren't checked "
                         "again: that needs %s (%s)", known.id,
                         typed("models fetch " + known.id))  # fmt: skip
        return rechecker

    def _candidates(self, language: str) -> list[ModelEntry]:
        """Registered Whisper models for ``language``: the ASR chain's order, then the rest."""
        chain = self._registry.chain("asr")
        rest = [e for e in self._registry.entries() if e.capability == "asr" and e not in chain]
        return [e for e in [*chain, *rest] if is_whisper(e) and e.supports(language)]

    def model_for(self, station: Any, asr: ModelEntry) -> Model | None:
        """The installed Whisper that re-checks ``station`` (transcribed by ``asr``), or None.
        Never downloads."""
        if is_whisper(asr):
            return None  # Whisper was told the station's language already
        language = station.language.lower()
        with self._lock:
            found = self._found.get(language)
        if found is not None:
            model = found[1]
            if model is not None and model[0].id not in self._broken:
                return model
            if model is None and self._clock() - found[0] < MISSING_TTL_S:
                return None
        model = None  # not looked up yet, looked up a while ago, or that one failed to load
        for entry in self._candidates(language):
            if entry.id in self._broken or not self._registry.licenses.accepted(entry):
                continue
            path = self._registry.local_path(entry)
            if path is not None:
                model = (entry, path)
                break
        with self._lock:
            self._found[language] = (self._clock(), model)
        return model

    def preload(self, stations: Iterable[Any], asr: dict[str, Model], load: GetModel) -> None:
        """Load the Whisper these stations need now (call on the ASR thread before their audio
        is read). One that fails to load only leaves their lines flagged."""
        needed: dict[str, Model] = {}
        for s in stations:
            model = self.model_for(s, asr[s.id][0]) if s.id in asr else None
            if model is not None:
                needed[model[0].id] = model
        for model in needed.values():
            try:
                load(*model)
            except Exception:
                log.exception("loading %s to re-check lines failed; they stay flagged",
                              model[0].id)  # fmt: skip
                self._broken.add(model[0].id)

    def offer(
        self, result: Any, asr: ModelEntry, submit: Submit, done: Callable[[Any], None]
    ) -> bool:
        """Queue a re-check of a flagged line, if it needs one and there is room. True when
        queued: ``done(result)`` is then called on the ASR thread once the verdict is in (and
        the text replaced, if it was). Never raises: the ASR thread calls it."""
        station = result.station
        if not result.transcript.text or other_language(result.flags) is None:
            return False
        if FLAG in result.flags:
            return False
        try:
            model = self.model_for(station, asr)
        except Exception:
            log.exception("choosing a model to re-check %s failed", station.id)
            return False
        if model is None:
            return False
        with self._lock:
            waiting = self._waiting.get(station.id, 0)
            total = sum(self._waiting.values())
            room = waiting < self.max_per_station and total < self.max_total
            if room:
                self._waiting[station.id] = waiting + 1
        if not room:
            self._skipped(result, "full", waiting=waiting, total=total)
            return False
        queued_at = self._clock()

        def run(get: GetModel) -> None:
            try:
                self._run(result, model, get, self._clock() - queued_at)
            except Exception:
                log.exception("re-checking a line from %s failed; it stays flagged", station.id)
            finally:
                self._release(station.id)
                done(result)

        try:
            queued = submit(run, PRIORITY + station.priority)
        except Exception:
            log.exception("queueing a re-check for %s failed", station.id)
            queued = False
        if not queued:
            self._release(station.id)
            self._skipped(result, "asr_queue_full", waiting=waiting, total=total)
        return queued

    def _release(self, station_id: str) -> None:
        with self._lock:
            left = self._waiting.get(station_id, 1) - 1
            if left > 0:
                self._waiting[station_id] = left
            else:
                self._waiting.pop(station_id, None)

    def status(self) -> dict[str, int]:
        """Re-checks waiting now, and since the start: lines replaced, kept, skipped."""
        with self._lock:
            return {"waiting": sum(self._waiting.values()), **self.counts}

    def _skipped(self, result: Any, reason: str, **fields: Any) -> None:
        self.counts["skipped"] += 1
        event(log, "recheck_skipped", level=logging.WARNING, station=result.station.id,
              reason=reason, flags=",".join(result.flags), **fields)  # fmt: skip

    def _run(self, result: Any, model: Model, get: GetModel, waited_s: float) -> None:
        station, utt = result.station, result.utterance
        result.timings["recheck_wait_ms"] = waited_s * 1000
        if waited_s > self.max_wait_s:
            self._skipped(result, "stale", waited_s=round(waited_s, 1))
            return
        entry = model[0]
        t0 = time.perf_counter()
        try:
            whisper = get(*model)
        except Exception:
            self._broken.add(entry.id)
            raise
        identify = getattr(whisper, "identify_language", None)
        probs = identify(utt.samples) if identify is not None else None
        if not probs:
            self._skipped(result, "no_language_id", model=entry.id)
            return
        heard = max(probs, key=probs.__getitem__)
        fields: dict[str, Any] = {"station": station.id, "model": entry.id, "heard": heard,
                                  "p": round(probs[heard], 3),
                                  "flagged": other_language(result.flags)}  # fmt: skip
        verdict = "kept"
        if same_language(heard, station.language):
            redo = whisper.transcribe(utt.samples, station.language)
            check = check_transcript(redo.text, utt.duration, station.language)
            if redo.text and check.drop is None and other_language(check.flags) is None:
                fields["was"] = logged_text(station, result.transcript.text, 80)
                result.transcript = redo
                result.flags = (*check.flags, FLAG)
                verdict = "replaced"
            else:
                verdict = "kept"
                fields["whisper"] = check.drop or ",".join(check.flags) or "empty"
                result.flags = (*result.flags, FLAG)
        else:
            result.flags = (*result.flags, FLAG)
        ms = (time.perf_counter() - t0) * 1000
        result.timings["recheck_ms"] = ms
        self.counts[verdict] += 1
        event(log, "recheck", verdict=verdict, ms=round(ms, 1), s=round(utt.duration, 1),
              text=logged_text(station, result.transcript.text, 80), **fields)  # fmt: skip
