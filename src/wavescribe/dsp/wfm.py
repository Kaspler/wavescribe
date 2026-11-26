# SPDX-License-Identifier: Apache-2.0
"""Broadcast FM (WFM) demodulation of one station from an IQ stream, in numpy/scipy.

    IQ at the device rate (e.g. 2.4 MS/s)
      ─► mix the station down to 0 Hz
      ─► low-pass ±100 kHz and decimate to 240 kHz (in one or two stages)
      ─► FM discriminator: the phase step between samples is the audio
      ─► low-pass and decimate to 48 kHz ─► de-emphasis ─► low-pass 7 kHz, decimate to 16 kHz

An FM station swings its carrier up to ±75 kHz with the audio. Mono audio (up to 15 kHz) sits at
the bottom of the demodulated signal; the stereo difference (around 38 kHz) and RDS (57 kHz) sit
above it and are filtered out here, which is what speech recognition wants. Transmitters boost
the treble before sending ("pre-emphasis"); the receiver undoes it with a one-pole low-pass of
time constant 50 µs (Europe and most of the world) or 75 µs (the Americas, South Korea).

Every stage keeps its state between calls, so the output doesn't depend on how the input is
chunked. Each FIR is a Kaiser-window design that keeps the frequencies a decimation would fold
into the band we keep at least 60 dB down.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from scipy.signal import firwin, kaiserord, lfilter, upfirdn

from wavescribe.dsp import CHANNEL_STOP_HZ, IF_RATE, in_band, supported_rate

MID_RATE = 48_000  # Hz, where de-emphasis runs
AUDIO_RATE = 16_000  # Hz, the pipeline's rate
DEVIATION_HZ = 75_000  # broadcast FM's peak deviation: full scale out
CHANNEL_PASS_HZ = 90_000  # kept flat either side of the station (75 kHz deviation + audio)
AUDIO_HZ = 7_000  # audio bandwidth kept (speech needs far less)
ATTENUATION_DB = 60.0


def iq_from_u8(raw: bytes | np.ndarray) -> np.ndarray:
    """RTL-SDR samples (interleaved unsigned 8-bit I, Q) to complex64 in about [-1, 1]."""
    u8 = np.frombuffer(raw, dtype=np.uint8) if isinstance(raw, bytes | bytearray) else raw
    x = (u8.astype(np.float32) - 127.4) / 128.0
    return x[: len(x) - len(x) % 2].view(np.complex64)


def lowpass(rate: float, passband: float, stopband: float) -> np.ndarray:
    """Odd-length FIR low-pass: flat to ``passband``, ATTENUATION_DB down at ``stopband``."""
    numtaps, beta = kaiserord(ATTENUATION_DB, (stopband - passband) / (0.5 * rate))
    numtaps |= 1
    return firwin(numtaps, (passband + stopband) / 2, window=("kaiser", beta), fs=rate)


class Decimator:
    """Stateful low-pass FIR plus decimation by an integer factor (polyphase, via upfirdn).

    Input of any length is accepted; leftovers that don't fill a whole output sample wait for
    the next call, and the last taps' worth of input is kept as history.
    """

    def __init__(self, taps: np.ndarray, factor: int, dtype: type = np.float32) -> None:
        self.factor = factor
        # Complex taps (a filter shifted to a channel, see dsp/channel.py) stay complex.
        self._taps = np.asarray(taps, dtype=np.complex64 if np.iscomplexobj(taps) else np.float32)
        self._hist_len = -(-(len(taps) - 1) // factor) * factor  # rounded up to whole outputs
        self._hist = np.zeros(self._hist_len, dtype=dtype)
        self._pending = np.zeros(0, dtype=dtype)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if len(self._pending):
            x = np.concatenate([self._pending, x])
        n = len(x) - len(x) % self.factor
        self._pending = x[n:].copy()
        if n == 0:
            return np.zeros(0, dtype=x.dtype)
        buf = np.concatenate([self._hist, x[:n]])
        y = upfirdn(self._taps, buf, 1, self.factor)
        start = self._hist_len // self.factor
        if self._hist_len:
            self._hist = buf[-self._hist_len :].copy()
        return y[start : start + n // self.factor].astype(x.dtype, copy=False)


def _channel_filter(rate: int) -> list[Decimator]:
    """Stages from ``rate`` down to IF_RATE: a cheap wide first stage, then a sharp one.

    After decimating to ``out``, whatever was above ``out - CHANNEL_STOP_HZ`` folds into the band
    the next stage keeps, so each stage is ``ATTENUATION_DB`` down from there. The last stage's
    stopband is therefore ``CHANNEL_STOP_HZ``: an adjacent station can't fold onto ours.
    """
    total = rate // IF_RATE
    factors = [total] if total < 4 or total % 2 else [total // 2, 2]
    stages, r = [], rate
    for f in factors:
        out = r // f
        if f > 1:
            taps = lowpass(r, CHANNEL_PASS_HZ, out - CHANNEL_STOP_HZ)
            stages.append(Decimator(taps, f, np.complex64))
        r = out
    return stages


class WfmDemodulator:
    """One station: complex IQ at ``rate`` in, 16 kHz float32 mono audio out."""

    def __init__(self, rate: int, offset_hz: float, deemphasis_us: float = 50.0) -> None:
        if not supported_rate(rate):
            raise ValueError(f"sample rate {rate} is not a multiple of {IF_RATE}")
        if not in_band(offset_hz, 0, rate):
            raise ValueError(
                f"station is {offset_hz / 1e3:.0f} kHz from the centre: outside the band"
            )
        self.rate = rate
        self.offset_hz = offset_hz
        self._step = -2.0 * np.pi * offset_hz / rate
        self._phase = 0.0
        self._channel = _channel_filter(rate)
        self._last = np.complex64(0)
        self._to_mid = Decimator(
            lowpass(IF_RATE, AUDIO_HZ, MID_RATE - AUDIO_HZ), IF_RATE // MID_RATE
        )
        a = float(np.exp(-1.0 / (MID_RATE * deemphasis_us * 1e-6)))
        self._deemph = ([1.0 - a], [1.0, -a])
        self._deemph_state = np.zeros(1)
        stop = AUDIO_RATE - AUDIO_HZ  # 9 kHz: folds to 7 kHz and above
        self._to_audio = Decimator(lowpass(MID_RATE, AUDIO_HZ, stop), MID_RATE // AUDIO_RATE)
        self.power_db: float | None = None  # the station's power in the last block, dBFS
        self.on_mpx: Callable[[np.ndarray], None] | None = None  # e.g. an RDS decoder

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        audio = self.mpx(iq)
        if self.on_mpx is not None and len(audio):
            self.on_mpx(audio)
        mid = self._to_mid(audio)
        if len(mid) == 0:  # lfilter turns its state into NaN on empty input
            return np.zeros(0, dtype=np.float32)
        mid, self._deemph_state = lfilter(*self._deemph, mid, zi=self._deemph_state)
        return self._to_audio(mid.astype(np.float32))

    def mpx(self, iq: np.ndarray) -> np.ndarray:
        """The station's multiplex at IF_RATE: mono audio, the 19 kHz pilot, stereo and RDS
        (57 kHz), scaled so the full ±75 kHz deviation is ±1."""
        n = len(iq)
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        # Mix down: the phase runs on in float64 so it doesn't drift over hours.
        phase = self._phase + self._step * np.arange(n, dtype=np.float64)
        self._phase = float((self._phase + self._step * n) % (2.0 * np.pi))
        x = iq * np.exp(1j * phase).astype(np.complex64)
        for stage in self._channel:
            x = stage(x)
        if len(x) == 0:
            return np.zeros(0, dtype=np.float32)
        p = float(np.mean(np.abs(x) ** 2))
        self.power_db = 10.0 * np.log10(p) if p > 0 else -200.0
        # Discriminator: the angle of each sample times the conjugate of the one before it.
        prev = np.concatenate([[self._last], x[:-1]])
        self._last = x[-1]
        audio = np.angle(x * np.conj(prev)).astype(np.float32)
        audio *= IF_RATE / (2.0 * np.pi * DEVIATION_HZ)
        return audio
