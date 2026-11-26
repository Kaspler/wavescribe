# SPDX-License-Identifier: Apache-2.0
"""VAD-driven segmentation, with Silero VAD (MIT) run through onnxruntime.

``SileroVad`` feeds the ONNX model the inputs it documents (audio with 64 samples of context,
its recurrent state, the sample rate), as snakers4/silero-vad's own wrapper (MIT) does, so
Wavescribe doesn't need that pip package, which pulls in torch. ``VadSegmenter`` is a state
machine over per-frame speech probabilities, with hysteresis and Silero's own threshold
conventions.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import numpy as np

from wavescribe.audio import SAMPLE_RATE, AudioChunk
from wavescribe.segment import Utterance

FRAME = 512  # samples per VAD frame at 16 kHz (32 ms), fixed by the Silero model


class FrameVad(Protocol):
    def __call__(self, frame: np.ndarray) -> float:
        """Speech probability for one FRAME-sample frame."""
        ...

    def reset(self) -> None: ...


class SileroVad:
    _CONTEXT = 64  # samples of the previous frame the v5+ model expects in front of each frame

    def __init__(self, model_path: Path) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset()

    def reset(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self._CONTEXT), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> float:
        x = np.concatenate([self._context, frame.reshape(1, FRAME)], axis=1)
        out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = x[:, -self._CONTEXT :]
        return float(out[0, 0])


class VadSegmenter:
    """Cuts a station's audio into utterances, each ``min_speech`` to ``max_utterance`` s long.

    Speech starts when the probability reaches ``threshold`` and ends after ``min_silence``
    seconds below it. Once an utterance is longer than ``soft_max``, the next pause of at least
    ``soft_pause`` ends it (presenters rarely stop for ``min_silence``). At ``max_utterance`` it
    is split at the latest short pause, or hard-cut if there is none.
    """

    def __init__(
        self,
        station_id: str,
        vad: FrameVad,
        *,
        threshold: float = 0.5,
        min_silence: float = 0.6,
        min_speech: float = 0.5,
        max_utterance: float = 20.0,
        soft_max: float = 10.0,
        soft_pause: float = 0.2,
        pad: float = 0.2,
        pause: float = 0.1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.station_id = station_id
        self._vad = vad
        self._thr = threshold
        self._neg_thr = max(threshold - 0.15, 0.01)  # Silero's hysteresis convention
        self._min_silence = _frames(min_silence)
        self._min_speech = _frames(min_speech)
        self._max = _frames(max_utterance)
        self._soft_max = _frames(soft_max)
        self._soft_pause = max(1, _frames(soft_pause))
        self._pad = _frames(pad)
        self._pause = max(1, _frames(pause))
        self._clock = clock
        self._seq = 0
        self._pending = np.zeros(0, dtype=np.float32)
        self._preroll: deque[tuple[np.ndarray, float]] = deque(maxlen=self._pad)
        self._reset_utterance()

    def _reset_utterance(self) -> None:
        self._in_speech = False
        self._frames: list[np.ndarray] = []
        self._times: list[float] = []  # arrival time of each frame
        self._silence = 0  # trailing frames since the probability last reached the threshold
        self._last_pause: int | None = None  # frame index of the latest usable split point

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def feed(self, chunk: AudioChunk) -> list[Utterance]:
        out: list[Utterance] = []
        if chunk.discontinuity:
            out += self.flush()
            self._vad.reset()
            self._pending = np.zeros(0, dtype=np.float32)
            self._preroll.clear()
        audio = np.concatenate([self._pending, chunk.samples])
        n = len(audio) // FRAME
        for i in range(n):
            frame = audio[i * FRAME : (i + 1) * FRAME]
            if (u := self._step(frame, self._vad(frame), chunk.t_end)) is not None:
                out.append(u)
        self._pending = audio[n * FRAME :]
        return out

    def flush(self) -> list[Utterance]:
        out: list[Utterance] = []
        if self._in_speech:
            keep = len(self._frames) - max(0, self._silence - self._pad)
            if (u := self._emit(keep)) is not None:
                out.append(u)
        self._reset_utterance()
        return out

    def _step(self, frame: np.ndarray, prob: float, t: float) -> Utterance | None:
        if not self._in_speech:
            self._preroll.append((frame, t))
            if prob >= self._thr:
                self._in_speech = True
                self._frames = [f for f, _ in self._preroll]
                self._times = [ft for _, ft in self._preroll]
                self._preroll.clear()
            return None

        self._frames.append(frame)
        self._times.append(t)
        if prob >= self._thr:
            self._silence = 0
        elif prob < self._neg_thr or self._silence:
            self._silence += 1
            if self._silence >= self._pause:
                # Split point: the middle of the current pause.
                self._last_pause = len(self._frames) - self._silence // 2

        long_enough = len(self._frames) - self._silence >= self._soft_max
        if self._silence >= self._min_silence or (
            long_enough and self._silence >= self._soft_pause
        ):
            keep = len(self._frames) - self._silence + min(self._pad, self._silence)
            tail = list(zip(self._frames[keep:], self._times[keep:], strict=True))
            utterance = self._emit(keep)
            self._reset_utterance()
            self._preroll.extend(tail[-self._pad :] if self._pad else [])
            return utterance

        if len(self._frames) >= self._max:
            cut = self._last_pause
            if cut is None or cut < self._max // 4:
                cut = len(self._frames)
            rest_f, rest_t = self._frames[cut:], self._times[cut:]
            utterance = self._emit(cut)
            self._frames, self._times = rest_f, rest_t
            self._last_pause = None
            self._silence = min(self._silence, len(rest_f))
            return utterance
        return None

    def _emit(self, keep: int) -> Utterance | None:
        keep = max(0, min(keep, len(self._frames)))
        if keep < self._min_speech:
            return None
        samples = np.concatenate(self._frames[:keep])
        t_end = self._times[keep - 1]
        self._seq += 1
        return Utterance(
            station_id=self.station_id,
            seq=self._seq,
            samples=samples,
            t_start=t_end - len(samples) / SAMPLE_RATE,
            t_end=t_end,
            ready_at=self._clock(),
        )


def _frames(seconds: float) -> int:
    return round(seconds * SAMPLE_RATE / FRAME)
