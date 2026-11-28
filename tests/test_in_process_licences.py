"""Libraries that are only installed, never loaded (docs/LICENSING.md):

- soundfile (libsndfile) and soxr (libsoxr) come with librosa, which parakeet-mlx uses for its
  mel filterbank alone;
- sounddevice (PortAudio, and Windows DLLs built with the ASIO SDK) comes with mlx-audio, which
  imports it only for speech-to-speech;
- scipy.odr is the one part of scipy that loads the GCC runtime libraries scipy's macOS wheel
  carries; Wavescribe uses scipy.signal and scipy.ndimage.

A fresh interpreter that imports Wavescribe's modules, runs that filterbank and designs a
demodulator's filters must not have any of them loaded.

A Whisper model is the exception, as docs/LICENSING.md says: mlx-audio loads Whisper's processor
from transformers, which imports soundfile and soxr. That path may load those two, never the
others."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

NEVER_LOADED = ("soundfile", "_soundfile", "soxr", "sounddevice", "scipy.odr")
WHISPER_MAY_LOAD = ("soundfile", "_soundfile", "soxr")

PROBE = f"""
import importlib, json, sys
for name in ("wavescribe.cli", "wavescribe.app", "wavescribe.pipeline", "wavescribe.web.server",
             "wavescribe.asr.parakeet", "wavescribe.asr.mlx_audio", "wavescribe.gate.yamnet",
             "wavescribe.segment.vad", "wavescribe.translate.llm", "wavescribe.sources.sdr",
             "wavescribe.sources.stream", "wavescribe.sources.watch", "wavescribe.dsp.wfm",
             "wavescribe.dsp.channel", "wavescribe.dsp.rds", "wavescribe.dsp.activity",
             "wavescribe.radio.live"):
    importlib.import_module(name)
from scipy import signal
signal.firwin(31, 0.2)
try:
    import parakeet_mlx.audio  # noqa: F401
    import librosa
    librosa.filters.mel(sr=16000, n_fft=512, n_mels=128)
    ran = True
except ImportError:
    ran = False
print(json.dumps({{"ran": ran, "loaded": [m for m in {NEVER_LOADED!r} if m in sys.modules]}}))
"""


def test_the_speech_and_radio_paths_never_load_the_installed_only_libraries() -> None:
    out = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, timeout=300, check=True
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    if not result["ran"]:
        pytest.skip("parakeet-mlx or librosa isn't installed here")
    assert result["loaded"] == []


WHISPER_PROBE = f"""
import json, sys
try:
    from transformers import WhisperProcessor  # what mlx-audio's Whisper loads after the weights
    ran = True
except ImportError:
    ran = False
print(json.dumps({{"ran": ran, "loaded": [m for m in {NEVER_LOADED!r} if m in sys.modules]}}))
"""


def test_a_whisper_model_loads_only_the_libraries_the_licence_table_names() -> None:
    out = subprocess.run(
        [sys.executable, "-c", WHISPER_PROBE],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    if not result["ran"]:
        pytest.skip("transformers isn't installed here")
    assert set(result["loaded"]) <= set(WHISPER_MAY_LOAD)
