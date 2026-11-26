# SPDX-License-Identifier: Apache-2.0
"""Speech recognition: utterance audio -> source-language text."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from wavescribe.registry import ModelEntry


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str  # BCP-47 tag the station declared (backends may not report their own guess)
    model: str
    confidence: float | None = None


class Transcriber(Protocol):
    model_id: str

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        """Transcribe 16 kHz mono float32 audio spoken in ``language``."""
        ...


def load_transcriber(entry: ModelEntry, path: Path) -> Transcriber:
    """Instantiate the backend for a registry entry. GPU backends: call from the worker thread."""
    if entry.backend == "parakeet-mlx":
        from wavescribe.asr.parakeet import ParakeetMlx

        return ParakeetMlx(entry.id, path)
    if entry.backend == "mlx-audio":
        from wavescribe.asr.mlx_audio import MlxAudioTranscriber

        return MlxAudioTranscriber(entry.id, path)
    raise ValueError(f"{entry.id}: no transcriber backend {entry.backend!r} on this platform")
