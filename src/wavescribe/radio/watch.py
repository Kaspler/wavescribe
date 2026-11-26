# SPDX-License-Identifier: Apache-2.0
"""Watches: a station that listens to every channel of one window of spectrum and keeps
whatever is said on any of them (docs/RADIO.md, "Catching radio that only talks now and then").

A watch is one dongle's window at a time (about 2.2 MHz at 2.4 MS/s): all of PMR446, the 2 m
amateur band, or a slice of anything else. ``dsp/activity.py`` notices transmissions in it and
``sources/watch.py`` demodulates each one, from just before it was noticed, into a catch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from wavescribe.dsp.activity import THRESHOLD_DB
from wavescribe.radio.bandplan import AUTO


@dataclass(frozen=True)
class Watch:
    """What a watch listens to (``SdrTuning.watch``): the channels of its window."""

    spec: str | tuple[float, float]  # as the user gave it: a band id, or (start, stop) in MHz
    channels: tuple[int, ...]  # Hz, ascending
    step_hz: float  # the raster the channels are on (a channel list: their smallest spacing)
    threshold_db: float = THRESHOLD_DB
    region: str = AUTO  # the band plan region, for naming what is caught
    label: str = ""  # what the window is called: the band's name, or its range

    @property
    def lo_hz(self) -> int:
        return self.channels[0]

    @property
    def hi_hz(self) -> int:
        return self.channels[-1]


@dataclass
class Catch:
    """One transmission caught by a watch: when, where, how strong, and its audio."""

    freq_hz: int
    mode: str
    t_start: float  # the transmission's first moment above the noise (epoch seconds)
    t_end: float  # its last
    audio_from: float  # when ``audio`` starts: a moment before t_start, for the first syllable
    audio: np.ndarray = field(repr=False)  # 16 kHz mono float32
    snr_db: float = 0.0  # the strongest it was over the channel's noise floor
    peak_db: float = 0.0  # the channel's strongest power, dBFS
    reason: str | None = None  # "carrier": on for minutes, learnt into the noise floor
    station_id: str = ""
    privacy: str = "private"  # the watch's class: what is kept of it (radio/bandplan.py)
    band: str | None = None  # the band plan entry the frequency is in
    label: str = ""  # the band's name, with the channel number where it has one
    voice: bool | None = None  # speech passed the VAD and the gate; None: not checked
    id: int | None = None  # its row in the store, once kept

    @property
    def duration(self) -> float:
        return max(0.0, self.t_end - self.t_start)

    @property
    def audio_to(self) -> float:
        return self.audio_from + len(self.audio) / 16_000
