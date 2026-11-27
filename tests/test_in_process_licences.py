"""Libraries that are only installed, never loaded: soundfile (libsndfile) and soxr (libsoxr)
come with librosa, which parakeet-mlx uses for its mel filterbank alone. A fresh interpreter
that imports Wavescribe's modules and runs that filterbank must not have either loaded."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

NEVER_LOADED = ("soundfile", "_soundfile", "soxr")

PROBE = f"""
import importlib, json, sys
for name in ("wavescribe.cli", "wavescribe.app", "wavescribe.pipeline", "wavescribe.web.server",
             "wavescribe.asr.parakeet", "wavescribe.asr.mlx_audio", "wavescribe.gate.yamnet",
             "wavescribe.segment.vad", "wavescribe.translate.llm", "wavescribe.sources.sdr",
             "wavescribe.sources.stream"):
    importlib.import_module(name)
try:
    import parakeet_mlx.audio  # noqa: F401
    import librosa
    librosa.filters.mel(sr=16000, n_fft=512, n_mels=128)
    ran = True
except ImportError:
    ran = False
print(json.dumps({{"ran": ran, "loaded": [m for m in {NEVER_LOADED!r} if m in sys.modules]}}))
"""


def test_the_speech_path_never_loads_soundfile_or_soxr() -> None:
    out = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=300, check=True
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    if not result["ran"]:
        pytest.skip("parakeet-mlx or librosa isn't installed here")
    assert result["loaded"] == []
