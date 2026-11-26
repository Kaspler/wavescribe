# SPDX-License-Identifier: Apache-2.0
"""A watch's source: every channel of a dongle's window watched at once, and each
transmission demodulated, from just before it was noticed, into a catch of its own.

``Catcher`` does the work on the dongle's thread, beside the stations and the live receiver
that share its ``rtl_sdr`` pipe (``sources/sdr.py``):

    each block of IQ ─► the history (the last 2 s, as the dongle's 8-bit samples)
                     ─► the activity detector (dsp/activity.py): starts and ends per channel
    a start ─► an NFM or AM demodulator (dsp/channel.py) for that channel, fed from the
               history 0.3 s before the transmission was first above the noise, then live
    an end  ─► a catch: its audio up to 0.3 s after the last moment above the noise

The detector takes about two frames (100 ms) to be sure, and the history holds two seconds,
so the first syllable is never lost to the time it takes to notice. Up to four transmissions
are demodulated at once; one more is counted as missed rather than delayed or crashed.

``WatchSource`` hands the catches to the pipeline as a station's audio, one after another,
each its own stretch (a discontinuity first, so the VAD starts afresh) with its frequency on
every chunk. Two catches at once are transcribed one after the other, never mixed.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

import numpy as np

from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.config import SdrTuning
from wavescribe.dsp.activity import CARRIER_S, Activity, ActivityDetector, IqHistory
from wavescribe.dsp.channel import NarrowbandDemodulator
from wavescribe.log import event
from wavescribe.radio.bandplan import POLICIES, lookup
from wavescribe.radio.watch import Catch, Watch, publish
from wavescribe.sources import SourceHealth
from wavescribe.sources.sdr import RtlSdrDevice, shared_device

log = logging.getLogger(__name__)

PREROLL_S = 0.3  # audio kept from before the transmission was noticed: its first syllable
TAIL_S = 0.3  # and after its last moment above the noise: the end of its last word
HISTORY_S = 2.0
MAX_ACTIVE = 4  # demodulators at once (about 2 % of a core each at 2.4 MS/s)
CHUNK = SAMPLE_RATE // 4  # a catch reaches the pipeline in quarter seconds, like live audio
# Silence after each catch: more than the VAD's end-of-speech wait (0.6 s), so the catch's last
# utterance ends with it instead of running into the next catch.
CLOSING_S = 0.8
QUEUE_CATCHES = 16  # waiting for the VAD and the gate; beyond it, dropped and counted
SAY_DEAF_S = 3.0  # interference across the window this long is logged and shown, not a click


@dataclass
class _Recording:
    activity: Activity
    demod: NarrowbandDemodulator
    start: int  # the sample index its audio starts at
    audio: list[np.ndarray] = field(default_factory=list)


class Catcher:
    """Turns a dongle's IQ into catches: ``feed`` each block, get the catches that ended in
    it. Not thread-safe: the dongle's thread is its only caller."""

    def __init__(
        self,
        tuning: SdrTuning,
        *,
        spurs: list[int] | tuple[int, ...] = (),
        max_active: int = MAX_ACTIVE,
        carrier_s: float = CARRIER_S,
        station_id: str = "",
        privacy: str = "private",
    ) -> None:
        watch = tuning.watch
        if watch is None:
            raise ValueError("a catcher needs a watch: the channels of a window")
        self.tuning = tuning
        self.watch: Watch = watch
        self.station_id = station_id
        self.privacy = privacy
        d = tuning.device
        self._rate, self._centre = d.sample_rate, d.center_hz
        self._decim = d.sample_rate // SAMPLE_RATE
        self._channel_hz = tuning.channel_hz
        self.detector = ActivityDetector(
            d.sample_rate,
            d.center_hz,
            watch.channels,
            float(tuning.channel_hz or 12_500),
            step_hz=watch.step_hz,
            threshold_db=watch.threshold_db,
            spurs=spurs,
            carrier_s=carrier_s,
        )
        self._history = IqHistory(d.sample_rate, HISTORY_S)
        self._max_active = max_active
        self._recordings: dict[int, _Recording] = {}
        # Where the audio of each channel's last catch ended: the next catch's pre-roll starts
        # no earlier, so two catches never share a moment of the same channel.
        self._audio_end: dict[int, int] = {}
        self._anchor = (0, 0.0)  # the newest sample's index and wall-clock time
        self.missed = 0  # transmissions no demodulator was free for
        self.short = 0  # shorter than a catch: data bursts, key clicks
        self._deaf: tuple[float, int] | None = None  # once said: how long, and relearnt then

    @property
    def deaf_s(self) -> float | None:
        return self.detector.deaf_s

    @property
    def warning(self) -> str | None:
        """What keeps the watch from catching anything right now, for the station's health."""
        if (self.deaf_s or 0) < SAY_DEAF_S:
            return None
        return "deaf: noise across the whole window; learnt as the new floor if it stays"

    @property
    def active(self) -> list[int]:
        """The frequencies being caught right now."""
        return [r.activity.freq_hz for r in self._recordings.values()]

    def where(self, **fields: object) -> dict[str, object]:
        """Fields for the log that say where and when something was heard: none for a class
        that keeps no text, whose log must not keep what its catches don't."""
        return fields if POLICIES[self.privacy].text_days != 0 else {}

    def feed(self, iq: np.ndarray, t_end: float) -> list[Catch]:
        self._history.append(iq)
        self._anchor = (self._history.end, t_end)
        for rec in self._recordings.values():
            rec.audio.append(rec.demod(iq))
        out = []
        # The recordings started here are fed from the history, this block included.
        for a in self.detector.feed(iq):
            if a.kind == "start":
                self._begin(a)
                continue
            rec = self._recordings.pop(a.channel, None)
            if rec is None:  # it was missed: no demodulator was free when it started
                continue
            if a.kind == "short":
                self.short += 1
                continue
            out.append(self._catch(rec, a))
        self._say_deaf()
        return out

    def _say_deaf(self) -> None:
        """One line in the log when interference across the window has made the watch deaf,
        and one when it hears again."""
        deaf, said = self.deaf_s, self._deaf
        if deaf is not None and said is not None:
            self._deaf = (max(said[0], deaf), said[1])
        elif deaf is not None and deaf >= SAY_DEAF_S:
            self._deaf = (deaf, self.detector.relearnt)
            event(log, "watch_deaf", level=logging.WARNING, station=self.station_id)
        elif deaf is None and said is not None:
            self._deaf = None
            learnt = self.detector.relearnt > said[1]  # the floors, afresh: the noise stayed
            event(
                log,
                "watch_hearing",
                station=self.station_id,
                deaf_s=round(said[0], 1),
                learnt=learnt,
            )

    def stop(self) -> list[Catch]:
        """The catches still running, cut where the IQ ends (the dongle stopped)."""
        out = []
        for a in self.detector.flush():
            rec = self._recordings.pop(a.channel, None)
            if rec is not None and a.kind == "end":
                out.append(self._catch(rec, a))
        return out

    def _begin(self, a: Activity) -> None:
        if len(self._recordings) >= self._max_active:
            self.missed += 1
            where = self.where(mhz=a.freq_hz / 1e6, active=[f / 1e6 for f in self.active])
            event(log, "watch_missed", level=logging.WARNING, missed=self.missed, **where)
            return
        start = max(
            a.start - round(PREROLL_S * self._rate),
            self._history.start,
            self._audio_end.get(a.channel, 0),
        )
        demod = NarrowbandDemodulator(
            self._rate,
            a.freq_hz - self._centre,
            self.tuning.mode,
            self._channel_hz,
            self.tuning.squelch_db,
        )
        rec = _Recording(a, demod, start)
        rec.audio.append(demod(self._history.since(start)))
        self._recordings[a.channel] = rec

    def _time(self, index: int) -> float:
        newest, t = self._anchor
        return t - (newest - index) / self._rate

    def _catch(self, rec: _Recording, a: Activity) -> Catch:
        end = (a.end if a.end is not None else self._anchor[0]) + round(TAIL_S * self._rate)
        keep = max(0, (end - rec.start) // self._decim)
        audio = np.concatenate(rec.audio)[:keep] if rec.audio else np.zeros(0, np.float32)
        self._audio_end[a.channel] = rec.start + len(audio) * self._decim
        return Catch(
            freq_hz=a.freq_hz,
            mode=self.tuning.mode,
            t_start=self._time(a.start),
            t_end=self._time(a.end if a.end is not None else self._anchor[0]),
            audio_from=self._time(rec.start),
            audio=audio.astype(np.float32, copy=False),
            snr_db=round(max(rec.activity.snr_db, a.snr_db), 1),
            peak_db=round(max(rec.activity.power_db, a.power_db), 1),
            reason=a.reason,
        )


class WatchSource:
    """A watch as a station's ``AudioSource``: its catches, one after another, as audio."""

    def __init__(
        self,
        station_id: str,
        tuning: SdrTuning,
        *,
        device: RtlSdrDevice | None = None,
        spurs: list[int] | tuple[int, ...] = (),
        privacy: str = "private",
        queue_catches: int = QUEUE_CATCHES,
    ) -> None:
        """``privacy`` is the watch's class, kept with each catch."""
        self.station_id = station_id
        self.tuning = tuning
        self._device = device or shared_device(tuning.device)
        self._spurs = tuple(spurs)
        self._privacy = privacy
        self._catcher = self._new_catcher()
        self._q: queue.Queue[Catch | None] = queue.Queue(maxsize=queue_catches)
        self._closed = threading.Event()
        self._catching = threading.Lock()  # the catcher: fed on the dongle's thread, flushed here
        self._error: Exception | None = None  # what catching raised, on the device thread
        self._health = SourceHealth()
        self._heard: Catch | None = None  # the catch the pipeline is reading
        self.dropped = 0  # catches the pipeline was too far behind for

    @property
    def health(self) -> SourceHealth:
        return self._health

    def _new_catcher(self) -> Catcher:
        return Catcher(
            self.tuning, spurs=self._spurs, station_id=self.station_id, privacy=self._privacy
        )

    @property
    def catcher(self) -> Catcher:
        return self._catcher

    def __iter__(self) -> Iterator[AudioChunk]:
        if not self._device.available:
            raise RuntimeError("'rtl_sdr' not found; install it with: brew install librtlsdr")
        if self._error is not None:  # iterated again after a failure: start afresh
            self._error = None
            self._catcher = self._new_catcher()
        self._device.attach(self)
        try:
            while not self._closed.is_set() and self._error is None:
                try:
                    catch = self._q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if catch is None:
                    break
                yield from self._audio(catch)
                self._heard = None
                publish(catch)
            if self._error is not None and not self._closed.is_set():
                raise RuntimeError(f"catching failed: {self._error}") from self._error
        finally:
            self._device.detach(self)
            self._health.connected = False
            self._health.squelch_open = self._health.warning = None
            # Caught but not yet heard by the VAD, or still being caught: still caught, so
            # still logged, as far as the IQ went.
            if self._heard is not None:
                self._unchecked(self._heard)
            with contextlib.suppress(queue.Empty):
                while (catch := self._q.get_nowait()) is not None:
                    self._unchecked(catch)
            if self._error is None:
                with self._catching:
                    running = self._catcher.stop()
                for catch in running:
                    self._label(catch)
                    self._unchecked(catch)

    def _audio(self, catch: Catch) -> Iterator[AudioChunk]:
        self._heard = catch
        catch.voice = None  # until the pipeline says it looked for speech in it
        closing = np.zeros(round(CLOSING_S * SAMPLE_RATE), dtype=np.float32)
        audio = np.concatenate([catch.audio, closing])
        for i in range(0, len(audio), CHUNK):
            part = audio[i : i + CHUNK]
            t_end = catch.audio_from + (i + len(part)) / SAMPLE_RATE
            yield AudioChunk(part, t_end, discontinuity=i == 0, freq_hz=catch.freq_hz)

    def checked(self, chunk: AudioChunk) -> None:
        """The pipeline's word that a chunk of this source went through the VAD: a catch whose
        audio did, and none of whose utterances passed the gate, had nobody speaking in it."""
        heard = self._heard
        if heard is not None and heard.voice is None and chunk.freq_hz == heard.freq_hz:
            heard.voice = False

    def speech(self, utterance: object) -> None:
        """The pipeline's word that an utterance of this source passed the VAD and the gate."""
        heard = self._heard
        if heard is not None and getattr(utterance, "freq_hz", None) == heard.freq_hz:
            heard.voice = True

    def _unchecked(self, catch: Catch) -> None:
        if catch.voice is False:  # not heard to the end: whether anyone spoke is unknown
            catch.voice = None
        publish(catch)

    def close(self) -> None:
        self._closed.set()
        with contextlib.suppress(queue.Full):
            self._q.put_nowait(None)

    # Called on the device thread.

    def _feed(self, iq: np.ndarray, t_end: float, first: bool) -> None:
        if self._closed.is_set() or self._error is not None:
            return
        try:
            with self._catching:
                caught = []
                if first and self._catcher.detector.samples:
                    # After a gap the history no longer runs on: what was being caught ends at
                    # the gap, and the floors are learnt again (half a second).
                    caught = self._catcher.stop()
                    self._catcher = self._new_catcher()
                caught += self._catcher.feed(iq, t_end)
        except Exception as e:
            # Raised again in the station's own thread, so the watch is restarted like any
            # failed source while the device and its other stations go on.
            self._error = e
            with contextlib.suppress(queue.Full):
                self._q.put_nowait(None)
            return
        h = self._health
        h.connected = True
        h.last_data_at = t_end
        h.samples_total += len(iq) // (self.tuning.device.sample_rate // SAMPLE_RATE)
        h.signal_db = round(float(self._catcher.detector.power_db.max()), 1)
        h.clipped = self._device.clipped
        h.squelch_open = bool(self._catcher.active)
        h.warning = self._catcher.warning
        for catch in caught:
            self._queue(catch)

    def _label(self, catch: Catch) -> None:
        band = lookup(catch.freq_hz, self._catcher.watch.region)
        catch.station_id, catch.privacy = self.station_id, self._privacy
        catch.band, catch.label = (band.id, band.label(catch.freq_hz)) if band else (None, "")

    def _queue(self, catch: Catch) -> None:
        self._label(catch)
        try:
            self._q.put_nowait(catch)
        except queue.Full:
            # The pipeline is far behind (a busy speech model). Keeping the catch would mean
            # writing it to the store here, on the thread that feeds every station on this
            # dongle: it is counted instead, so that thread never waits.
            self.dropped += 1
            where = self._catcher.where(mhz=catch.freq_hz / 1e6)
            event(
                log,
                "watch_behind",
                level=logging.WARNING,
                station=self.station_id,
                dropped=self.dropped,
                **where,
            )

    def _disconnected(self, reconnects: int) -> None:
        self._health.connected = False
        self._health.reconnects = reconnects
