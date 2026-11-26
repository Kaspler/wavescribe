# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import requires_ffmpeg, requires_say, say_to_pcm
from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.registry import ModelRegistry
from wavescribe.segment.vad import FRAME, SileroVad, VadSegmenter


class EnergyVad:
    """Deterministic stand-in for Silero: 'speech' is any frame louder than -40 dBFS."""

    def __call__(self, frame: np.ndarray) -> float:
        return 1.0 if float(np.sqrt(np.mean(frame**2))) > 0.01 else 0.0

    def reset(self) -> None:
        pass


def tone(seconds: float) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (0.3 * np.sin(2 * np.pi * 300 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def run(seg: VadSegmenter, audio: np.ndarray, chunk: float = 0.5, t0: float = 1000.0):
    out, step = [], int(chunk * SAMPLE_RATE)
    for i in range(0, len(audio), step):
        part = audio[i : i + step]
        t_end = t0 + (i + len(part)) / SAMPLE_RATE
        out += seg.feed(AudioChunk(part, t_end=t_end, discontinuity=(i == 0)))
    return out + seg.flush()


def test_splits_on_silence_with_padding() -> None:
    seg = VadSegmenter("s", EnergyVad(), min_silence=0.5, pad=0.2)
    audio = np.concatenate([silence(1), tone(2), silence(1), tone(3), silence(1)])
    utts = run(seg, audio)

    assert [u.seq for u in utts] == [1, 2]
    # Each utterance is the tone plus up to ~0.2 s of padding on each side.
    assert 2.0 <= utts[0].duration <= 2.5
    assert 3.0 <= utts[1].duration <= 3.5
    assert utts[0].t_end < utts[1].t_start
    assert utts[0].t_start == pytest.approx(utts[0].t_end - utts[0].duration)


def test_short_pauses_do_not_split() -> None:
    seg = VadSegmenter("s", EnergyVad(), min_silence=0.6)
    audio = np.concatenate([tone(1.5), silence(0.3), tone(1.5), silence(1)])
    assert len(run(seg, audio)) == 1


def test_drops_blips_shorter_than_min_speech() -> None:
    seg = VadSegmenter("s", EnergyVad(), min_speech=0.5, pad=0.0)
    audio = np.concatenate([silence(1), tone(0.2), silence(1)])
    assert run(seg, audio) == []


def test_long_speech_is_split_at_a_pause() -> None:
    seg = VadSegmenter("s", EnergyVad(), max_utterance=10, soft_max=10, min_silence=1.0, pad=0.0)
    # 7 s speech, 0.3 s pause (too short to end the utterance), 7 s speech.
    audio = np.concatenate([tone(7), silence(0.3), tone(7), silence(2)])
    utts = run(seg, audio)
    assert len(utts) == 2
    assert 7.0 <= utts[0].duration <= 7.3  # cut inside the pause, not at the 10 s limit
    assert all(u.duration <= 10.0 for u in utts)


def test_soft_split_at_first_pause_after_soft_max() -> None:
    seg = VadSegmenter("s", EnergyVad(), soft_max=4, soft_pause=0.2, min_silence=1.0, pad=0.0)
    # Pauses of 0.3 s: the one at 3 s is too early, the one at 6 s ends the first utterance.
    audio = np.concatenate([tone(3), silence(0.3), tone(3), silence(0.3), tone(3), silence(2)])
    utts = run(seg, audio)
    assert len(utts) == 2
    assert 6.2 <= utts[0].duration <= 6.6


def test_continuous_speech_is_hard_cut_at_max() -> None:
    seg = VadSegmenter("s", EnergyVad(), max_utterance=5, soft_max=5, pad=0.0)
    utts = run(seg, np.concatenate([tone(12), silence(1)]))
    assert [round(u.duration) for u in utts] == [5, 5, 2]


def test_discontinuity_flushes_buffered_speech() -> None:
    seg = VadSegmenter("s", EnergyVad())
    out = seg.feed(AudioChunk(tone(2), t_end=10.0, discontinuity=True))
    out += seg.feed(AudioChunk(tone(2), t_end=20.0, discontinuity=True))  # reconnect
    out += seg.flush()
    assert len(out) == 2
    assert out[0].t_end == 10.0


def test_frames_split_across_chunks_are_kept() -> None:
    seg = VadSegmenter("s", EnergyVad(), pad=0.0)
    audio = np.concatenate([tone(2), silence(1)])
    utts = run(seg, audio, chunk=0.0123)  # chunks much smaller than, and unaligned to, FRAME
    assert len(utts) == 1
    assert abs(utts[0].duration - 2.0) <= 2 * FRAME / SAMPLE_RATE


@pytest.mark.models
@requires_say
@requires_ffmpeg
def test_silero_finds_tts_speech(tmp_path: Path) -> None:
    registry = ModelRegistry.load()
    path = registry.local_path(registry.get("silero-vad-v6"))
    if path is None:
        pytest.skip("run: wavescribe models fetch silero-vad-v6")
    speech = say_to_pcm(tmp_path / "a", "The water supply will be restored tomorrow morning.")
    audio = np.concatenate([silence(1.5), speech, silence(2.0), speech, silence(1.5)])
    utts = run(VadSegmenter("s", SileroVad(path)), audio)
    assert len(utts) == 2
    for u in utts:
        assert abs(u.duration - len(speech) / SAMPLE_RATE) < 1.0
