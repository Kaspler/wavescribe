# SPDX-License-Identifier: Apache-2.0
"""Parakeet-TDT on Apple Silicon through parakeet-mlx (Apache-2.0). Mac-only backend.

MLX binds arrays to the thread that created them, so construct and call this from one thread.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wavescribe.asr import Transcript


class ParakeetMlx:
    def __init__(self, model_id: str, model_path: Path) -> None:
        from parakeet_mlx import from_pretrained

        self.model_id = model_id
        self._model = from_pretrained(str(model_path))
        # The first call compiles Metal kernels (~3 s); pay that now, not on live audio.
        self.transcribe(np.zeros(16_000, dtype=np.float32), "und")

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        # The same path as parakeet-mlx's own transcribe(), minus its ffmpeg file loading.
        mel = get_logmel(mx.array(samples, dtype=mx.float32), self._model.preprocessor_config)
        result = self._model.generate(mel)[0]
        return Transcript(text=result.text.strip(), language=language, model=self.model_id)
