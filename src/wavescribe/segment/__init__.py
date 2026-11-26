# SPDX-License-Identifier: Apache-2.0
"""Segmenters turn a station's audio into utterances: short stretches of speech to transcribe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from wavescribe.audio import SAMPLE_RATE, AudioChunk


@dataclass(frozen=True)
class Utterance:
    station_id: str
    seq: int  # per-station counter
    samples: np.ndarray  # float32 mono at SAMPLE_RATE
    t_start: float  # wall-clock epoch seconds
    t_end: float  # when the last kept sample arrived
    ready_at: float  # when the segmenter emitted it (t_end + the end-of-speech wait)
    freq_hz: int | None = None  # a watch's: the frequency it was caught on

    @property
    def duration(self) -> float:
        return len(self.samples) / SAMPLE_RATE


class Segmenter(Protocol):
    def feed(self, chunk: AudioChunk) -> list[Utterance]:
        """Consume a chunk; return any utterances that ended inside it."""
        ...

    def flush(self) -> list[Utterance]:
        """Emit whatever speech is buffered (at shutdown or before a discontinuity)."""
        ...
