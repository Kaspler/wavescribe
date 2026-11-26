# SPDX-License-Identifier: Apache-2.0
"""Whisper, Voxtral, Qwen3-ASR and Nemotron on Apple Silicon through mlx-audio (MIT). Mac-only.

Each model family takes the language differently: Whisper wants an ISO 639-1 code, Nemotron a
locale key such as ``pt-PT``, and Qwen3-ASR and Voxtral detect it themselves. Like
``ParakeetMlx``, construct and call this from one thread.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from wavescribe.asr import Transcript

# Whisper's own codes where they differ from BCP-47.
_WHISPER_CODES = {"jv": "jw"}


class MlxAudioTranscriber:
    def __init__(self, model_id: str, model_path: Path) -> None:
        from mlx_audio.stt.utils import load_model

        self.model_id = model_id
        self._model = load_model(str(model_path))
        self._family = type(self._model).__module__.split(".")[-2]  # e.g. "whisper"
        self.transcribe(np.zeros(16_000, dtype=np.float32), "en")  # compile Metal kernels now

    def _language_kwargs(self, language: str) -> dict[str, str]:
        if self._family == "whisper":
            primary = language.split("-")[0].lower()
            return {"language": _WHISPER_CODES.get(primary, primary)}
        if self._family == "nemotron_asr":
            return {"language": language}
        return {}  # qwen3_asr, voxtral_realtime: built-in language identification

    def transcribe(self, samples: np.ndarray, language: str) -> Transcript:
        import mlx.core as mx

        out = self._model.generate(
            mx.array(samples, dtype=mx.float32), **self._language_kwargs(language)
        )
        return Transcript(text=out.text.strip(), language=language, model=self.model_id)

    def detect_language(self, samples: np.ndarray) -> Transcript | None:
        """Transcribe with the model's own language identification (Whisper only); the
        transcript's language is the one it heard, as Whisper's code. None if this model
        family can't tell."""
        import mlx.core as mx

        if self._family != "whisper":
            return None
        out = self._model.generate(mx.array(samples, dtype=mx.float32))
        lang = getattr(out, "language", None)
        if not lang:
            return None
        lang = {v: k for k, v in _WHISPER_CODES.items()}.get(lang, lang)
        return Transcript(text=out.text.strip(), language=lang, model=self.model_id)
