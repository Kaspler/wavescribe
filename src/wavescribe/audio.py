# SPDX-License-Identifier: Apache-2.0
"""Audio types shared by every pipeline stage."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16_000  # every source delivers 16 kHz mono float32 PCM in [-1, 1]


@dataclass(frozen=True)
class AudioChunk:
    samples: np.ndarray  # float32, mono, SAMPLE_RATE
    t_end: float  # wall-clock time (epoch seconds) when the last sample arrived (a live stream:
    # when it was due at the pace it was spoken, sources/stream.py)
    discontinuity: bool = False  # first chunk after a (re)connect: drop any partial state

    @property
    def duration(self) -> float:
        return len(self.samples) / SAMPLE_RATE
