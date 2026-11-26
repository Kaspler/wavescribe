# SPDX-License-Identifier: Apache-2.0
"""Speech/music gate: decides whether an utterance from the VAD is worth transcribing.

Silero VAD treats singing as speech, so without a gate every song reaches ASR and comes back as
lyrics or invented text. The gate looks at each finished utterance and drops the ones that are
music with no speech in them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np


@dataclass(frozen=True)
class Verdict:
    kind: Literal["speech", "music"]
    speech: float  # the highest speech score over the utterance's windows, 0..1
    music: float  # the mean music score, 0..1


class Gate(Protocol):
    model_id: str

    def classify(self, samples: np.ndarray) -> Verdict:
        """Classify one utterance of 16 kHz mono float32 audio."""
        ...


def decide(
    speech: np.ndarray, music: np.ndarray, *, speech_min: float = 0.1, music_min: float = 0.3
) -> Verdict:
    """Music only when **no** window has speech and the utterance is mostly music.

    Measured on Portuguese radio (docs/MODELS.md): inside songs no window scored Speech above
    0.02; talk, even over a music bed, always had some window above 0.17. Taking the maximum
    speech score means a DJ talking for one second over a song keeps the whole utterance: the
    gate errs towards transcribing, because a missed announcement costs more than a transcribed
    chorus.
    """
    top_speech = float(speech.max()) if len(speech) else 0.0
    mean_music = float(music.mean()) if len(music) else 0.0
    kind = "music" if top_speech < speech_min and mean_music >= music_min else "speech"
    return Verdict(kind, round(top_speech, 3), round(mean_music, 3))
