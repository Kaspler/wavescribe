# SPDX-License-Identifier: Apache-2.0
"""Whisper, Voxtral, Qwen3-ASR and Nemotron on Apple Silicon through mlx-audio (MIT). Mac-only.

Each model family takes the language differently: Whisper wants an ISO 639-1 code, Nemotron a
locale key such as ``pt-PT``, and Qwen3-ASR and Voxtral detect it themselves. Like
``ParakeetMlx``, construct and call this from one thread.
"""

from __future__ import annotations

import json
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

    @staticmethod
    def tells_languages(model_path: Path) -> bool:
        """Whether the model at ``model_path`` can tell languages (``identify_language``), from
        its config and without loading it: a Whisper that knows more than English (an
        English-only one has a vocabulary of 51,864 tokens, a multilingual one more)."""
        folder = model_path if model_path.is_dir() else model_path.parent
        try:
            config = json.loads((folder / "config.json").read_text())
        except (OSError, ValueError):
            return False
        vocab = config.get("n_vocab") or config.get("vocab_size") or 0
        return config.get("model_type") == "whisper" and vocab >= 51_865

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

    def identify_language(self, samples: np.ndarray) -> dict[str, float] | None:
        """Whisper's language identification without transcribing: one encoder pass over the
        first 30 s and one decoder step. Probabilities by language subtag (BCP-47 where
        Whisper's code differs); None if this model family can't tell."""
        import mlx.core as mx
        from mlx_audio.stt.models.whisper.audio import N_FRAMES, pad_or_trim

        if self._family != "whisper":
            return None
        mel, _ = self._model._prepare_audio(mx.array(samples, dtype=mx.float32))
        segment = pad_or_trim(mel, N_FRAMES, axis=-2).astype(self._model.dtype)
        _, probs = self._model.detect_language(segment)
        back = {v: k for k, v in _WHISPER_CODES.items()}
        return {back.get(code, code): float(p) for code, p in probs.items()}

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
