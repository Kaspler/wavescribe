# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def make_tone(path: Path, seconds: float, *, rate: int = 44_100, channels: int = 2) -> Path:
    """A synthetic test signal (sine tone) rendered by ffmpeg; never real broadcast audio."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
         "-ac", str(channels), str(path)],
        check=True,
    )  # fmt: skip
    return path


def say_to_pcm(path: Path, text: str, voice: str | None = None) -> np.ndarray:
    """Synthetic speech from macOS `say`, as 16 kHz mono float32. Only for local tests."""
    aiff = path.with_suffix(".aiff")
    cmd = ["say", "-o", str(aiff)] + (["-v", voice] if voice else []) + [text]
    subprocess.run(cmd, check=True)
    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(aiff),
         "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        check=True, capture_output=True,
    ).stdout  # fmt: skip
    return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0


requires_say = pytest.mark.skipif(shutil.which("say") is None, reason="needs macOS `say`")
