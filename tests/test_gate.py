# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import requires_say, say_to_pcm
from wavescribe.gate import decide
from wavescribe.registry import ModelError, ModelRegistry

SR = 16000


def test_music_needs_no_speech_in_any_window_and_mostly_music() -> None:
    song = decide(np.array([0.0, 0.01, 0.0]), np.array([0.9, 0.8, 0.95]))
    assert song.kind == "music"
    # A DJ talking for one window over the song keeps the whole utterance.
    assert decide(np.array([0.0, 0.3, 0.0]), np.array([0.9, 0.6, 0.95])).kind == "speech"
    # Talk over a music bed: music outscores speech, and it's still speech.
    assert decide(np.array([0.18, 0.2]), np.array([0.3, 0.4])).kind == "speech"
    # Neither speech nor music (noise, silence) goes on to ASR, which returns nothing.
    assert decide(np.array([0.02]), np.array([0.05])).kind == "speech"
    assert decide(np.array([]), np.array([])).kind == "speech"


def pluck(freq: float, seconds: float, seed: int) -> np.ndarray:
    """Karplus-Strong plucked string: a noise burst circulating in a damped delay line."""
    rng = np.random.default_rng(seed)
    n = int(SR / freq)
    buf = rng.uniform(-1, 1, n)
    out = np.empty(int(seconds * SR))
    for i in range(len(out)):
        out[i] = buf[i % n]
        buf[i % n] = 0.996 * 0.5 * (buf[i % n] + buf[(i + 1) % n])
    return out


def melody(seconds: float) -> np.ndarray:
    """A synthetic guitar-like tune: no recorded music in the test suite."""
    notes = [196.0, 220.0, 246.9, 293.7, 329.6, 392.0]  # G major pentatonic
    beat = 0.3
    out = np.zeros(int(seconds * SR))
    for k in range(int(seconds / beat)):
        start = int(k * beat * SR)
        note = pluck(notes[(k * 3) % len(notes)], 1.2, seed=k)
        if k % 4 == 0:
            note = note + 0.8 * pluck(notes[k // 4 % 3] / 2, 1.2, seed=100 + k)
        part = note[: len(out) - start]
        out[start : start + len(part)] += part
    return (0.3 * out / np.abs(out).max()).astype(np.float32)


@pytest.mark.models
@requires_say
def test_yamnet_drops_music_and_keeps_speech_even_over_music(tmp_path: Path) -> None:
    from wavescribe.gate.yamnet import YamnetGate

    try:
        _, path = ModelRegistry.load().resolve("gate")
    except ModelError as e:
        pytest.skip(str(e))
    gate = YamnetGate(path)
    speech = say_to_pcm(tmp_path / "pt", "O abastecimento de água foi reposto esta manhã.", "Joana")
    assert gate.classify(melody(8)).kind == "music"
    assert gate.classify(speech).kind == "speech"
    over_music = speech + 0.5 * melody(len(speech) / SR)[: len(speech)]
    assert gate.classify(over_music.astype(np.float32)).kind == "speech"
    assert gate.classify(np.zeros(SR // 2, dtype=np.float32)).kind == "speech"  # short input
