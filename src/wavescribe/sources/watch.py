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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from wavescribe.audio import SAMPLE_RATE
from wavescribe.config import SdrTuning
from wavescribe.dsp.activity import Activity, ActivityDetector, IqHistory
from wavescribe.dsp.channel import NarrowbandDemodulator
from wavescribe.log import event
from wavescribe.radio.watch import Catch, Watch

log = logging.getLogger(__name__)

PREROLL_S = 0.3  # audio kept from before the transmission was noticed: its first syllable
TAIL_S = 0.3  # and after its last moment above the noise: the end of its last word
HISTORY_S = 2.0
MAX_ACTIVE = 4  # demodulators at once (about 2 % of a core each at 2.4 MS/s)


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
        **detector: Any,
    ) -> None:
        watch = tuning.watch
        if watch is None:
            raise ValueError("a catcher needs a watch: the channels of a window")
        self.tuning = tuning
        self.watch: Watch = watch
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
            **detector,
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

    @property
    def active(self) -> list[int]:
        """The frequencies being caught right now."""
        return [r.activity.freq_hz for r in self._recordings.values()]

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
        return out

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
            event(
                log,
                "watch_missed",
                level=logging.WARNING,
                mhz=a.freq_hz / 1e6,
                active=[f / 1e6 for f in self.active],
                missed=self.missed,
            )
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
