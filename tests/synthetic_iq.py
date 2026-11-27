# SPDX-License-Identifier: Apache-2.0
"""Synthetic IQ for the watch's tests: a receiver's noise, transmissions with a made-up voice,
lines and clicks, rounded to 8 bits as a dongle sends them. Nothing here was ever on the air.

``Scene`` fixes the sample rate and the FFT size together, so a bin is 586 Hz and an FFT
1.7 ms at every rate (what a dongle at 2.4 MS/s has with 4,096 points)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.signal import butter, lfilter, resample_poly, sosfilt

from wavescribe.dsp.activity import Activity, ActivityDetector
from wavescribe.dsp.wfm import iq_from_u8

AUDIO = 16_000
MOD = 240_000  # modulating signals are made at this rate and read between its samples
NOISE = 0.006  # per component, full scale 1: about three quarters of a converter's step
STEP = 1 / 128


@lru_cache(maxsize=8)
def voice(seconds: float, seed: int = 0) -> np.ndarray:
    """Something shaped like speech, at 16 kHz with its peak at 1: words of a few syllables
    with pauses between them, each syllable a pitch that glides through three resonances
    that differ from one syllable to the next, or a hiss."""
    rng = np.random.default_rng(seed)
    out = np.zeros(round(seconds * AUDIO) + AUDIO)
    at = 0
    while at < len(out) - AUDIO // 2:
        for _ in range(int(rng.integers(2, 6))):  # one word
            n = int(rng.uniform(0.12, 0.26) * AUDIO)
            syllable = _hiss(n, rng) if rng.random() < 0.15 else _vowel(n, rng)
            syllable *= np.sin(np.pi * np.arange(n) / n) ** 0.5 * rng.uniform(0.5, 1.0)
            end = min(len(out), at + n)
            out[at:end] += syllable[: end - at]
            at = end
        at += int(rng.uniform(0.06, 0.3) * AUDIO)
    out = out[: round(seconds * AUDIO)]
    return out / np.abs(out).max()


def _vowel(n: int, rng: np.random.Generator) -> np.ndarray:
    pitch = np.linspace(rng.uniform(95, 190), rng.uniform(95, 190), n)
    phase = np.cumsum(pitch) / AUDIO
    pulses = np.diff(np.floor(phase), prepend=0.0)  # one pulse each time the pitch comes round
    y = np.zeros(n)
    for centre, width in ((rng.uniform(300, 850), 90), (rng.uniform(900, 2300), 120),
                          (rng.uniform(2400, 3000), 160)):  # fmt: skip
        r = np.exp(-np.pi * width / AUDIO)
        a = [1.0, -2 * r * np.cos(2 * np.pi * centre / AUDIO), r * r]
        y += lfilter([1.0], a, pulses)
    return y / max(float(np.abs(y).max()), 1e-9)


def _hiss(n: int, rng: np.random.Generator) -> np.ndarray:
    y = sosfilt(butter(2, 1_800, "highpass", fs=AUDIO, output="sos"), rng.standard_normal(n))
    return 0.4 * y / max(float(np.abs(y).max()), 1e-9)


@lru_cache(maxsize=32)
def radio_audio(seconds: float, seed: int = 0, limit: float = 99.0) -> np.ndarray:
    """What a narrowband FM radio feeds its modulator, at ``MOD``, full deviation at ±1: the
    voice through 300 to 3,000 Hz, pre-emphasis and a limiter. The limiter clips the loudest
    1 % of a normal talker (``limit`` 99) and far more of a loud one (60)."""
    a = sosfilt(butter(4, [300, 3_000], "bandpass", fs=AUDIO, output="sos"), voice(seconds, seed))
    a = lfilter([1.0, -0.95], [1.0], a)
    a = np.clip(a / np.percentile(np.abs(a), limit), -1.0, 1.0)
    a = sosfilt(butter(6, 3_000, "lowpass", fs=AUDIO, output="sos"), a)
    a = sosfilt(butter(2, 300, "highpass", fs=AUDIO, output="sos"), a)  # no standing offset
    return np.clip(resample_poly(a, MOD // AUDIO, 1), -1.0, 1.0)


def _span(t: np.ndarray, start_s: float, stop_s: float) -> slice:
    """The part of a block of sample times that is from ``start_s`` to before ``stop_s``."""
    return slice(int(np.searchsorted(t, start_s)), int(np.searchsorted(t, stop_s)))


def _turning(phase: np.ndarray) -> np.ndarray:
    """A carrier of amplitude 1 with this phase, sample by sample."""
    p = np.mod(phase, 2 * np.pi).astype(np.float32)
    out = np.empty(len(p), dtype=np.complex64)
    out.real, out.imag = np.cos(p), np.sin(p)
    return out


@lru_cache(maxsize=64)
def _one_block(offset_hz: float, n: int, rate: int) -> np.ndarray:
    return _turning(2 * np.pi * offset_hz * np.arange(n) / rate)


def _carrier(offset_hz: float, t: np.ndarray) -> np.ndarray:
    """A steady carrier over a whole block: one block of it, turned to where this one begins."""
    rate = round((len(t) - 1) / (t[-1] - t[0]))
    return np.complex64(np.exp(2j * np.pi * offset_hz * t[0])) * _one_block(offset_hz, len(t), rate)


@dataclass
class Tx:
    """A transmission ``offset_hz`` from the centre, keyed from ``start_s`` to ``stop_s``:
    narrowband FM (``mode`` nfm), AM (am) or broadcast FM with its pilot (wfm). It says
    nothing for its first ``silent_s`` (a bare carrier), and nothing at all with ``speaks``
    off."""

    offset_hz: float
    start_s: float
    stop_s: float
    amp: float
    mode: str = "nfm"
    deviation_hz: float = 2_500.0
    seed: int = 0
    silent_s: float = 0.0
    speaks: bool = True
    limit: float = 99.0

    def __post_init__(self) -> None:
        spoken = max(0.0, self.stop_s - self.start_s - self.silent_s) if self.speaks else 0.0
        if spoken <= 0:
            m = np.zeros(0)
        elif self.mode == "wfm":
            a = resample_poly(voice(round(spoken, 3), self.seed), MOD // AUDIO, 1)
            m = 0.85 * a + 0.1 * np.sin(2 * np.pi * 19_000 * np.arange(len(a)) / MOD)
        else:
            m = radio_audio(round(spoken, 3), self.seed, self.limit)
        m = np.concatenate([np.zeros(round(self.silent_s * MOD)), m, np.zeros(MOD // 10)])
        self._mod = m
        self._phase = 2 * np.pi * self.deviation_hz * np.cumsum(m) / MOD

    def add(self, x: np.ndarray, t: np.ndarray) -> None:
        on = _span(t, self.start_s, self.stop_s)
        if on.start == on.stop:
            return
        quiet = not self.speaks or t[-1] < self.start_s + self.silent_s
        if quiet and on == slice(0, len(t)) and self.mode != "am":  # a bare carrier: cheap
            x += np.float32(self.amp) * _carrier(self.offset_hz, t)
            return
        k = np.minimum((t[on] - self.start_s) * MOD, len(self._mod) - 1.001)
        i = k.astype(np.intp)
        carrier = 2 * np.pi * self.offset_hz * t[on]
        if self.mode == "am":
            level = 1.0 + 0.8 * (self._mod[i] + (k - i) * (self._mod[i + 1] - self._mod[i]))
            x[on] += self.amp * level.astype(np.float32) * _turning(carrier)
        else:
            phase = self._phase[i] + (k - i) * (self._phase[i + 1] - self._phase[i])
            x[on] += np.float32(self.amp) * _turning(carrier + phase)


@dataclass
class Line:
    """A line a receiver makes by itself, ``offset_hz`` from the centre: there from
    ``start_s`` to ``stop_s``, drifting ``drift_hz_s``, and with ``period_s`` on for only
    ``on_s`` of every period."""

    offset_hz: float
    amp: float
    start_s: float = 0.0
    stop_s: float = 1e9
    drift_hz_s: float = 0.0
    period_s: float = 0.0
    on_s: float = 0.0

    def add(self, x: np.ndarray, t: np.ndarray) -> None:
        on = _span(t, self.start_s, self.stop_s)
        if on.start == on.stop:
            return
        if on == slice(0, len(t)) and not self.drift_hz_s and not self.period_s:
            x += np.float32(self.amp) * _carrier(self.offset_hz, t)
            return
        wave = _turning(2 * np.pi * (self.offset_hz + 0.5 * self.drift_hz_s * t[on]) * t[on])
        if self.period_s:
            wave *= ((t[on] - self.start_s) % self.period_s) < self.on_s
        x[on] += np.float32(self.amp) * wave


@dataclass
class Clicks:
    """Broadband clicks, each up to 2 ms long, about ``per_s`` of them a second."""

    per_s: float
    amp: float
    seed: int = 0
    _rng: np.random.Generator = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = np.random.default_rng(1_000 + self.seed)

    def add(self, x: np.ndarray, t: np.ndarray) -> None:
        n = len(t)
        rate = n / (t[-1] - t[0] + 1e-12)
        for _ in range(self._rng.poisson(self.per_s * n / rate)):
            at = int(self._rng.integers(0, n))
            m = min(n - at, int(self._rng.uniform(0.05, 2.0) * 1e-3 * rate))
            burst = self._rng.standard_normal(m) + 1j * self._rng.standard_normal(m)
            x[at : at + m] += (self.amp * burst).astype(np.complex64)


@dataclass(frozen=True)
class Scene:
    """A dongle tuned to ``centre`` at ``rate``, with the FFT size that makes a bin 586 Hz."""

    rate: int = 1_200_000
    centre: int = 446_000_000
    noise: float = NOISE

    @property
    def fft(self) -> int:
        return 4096 * self.rate // 2_400_000

    @property
    def noise_power(self) -> float:
        """The noise per sample as the detector has it: the receiver's and the rounding's."""
        return 2 * self.noise**2 + 2 * STEP**2 / 12

    def amp(self, snr_db: float, bandwidth_hz: float = 12_500.0) -> float:
        """The amplitude of a signal ``snr_db`` over the noise in ``bandwidth_hz``."""
        return float(np.sqrt(10 ** (snr_db / 10) * self.noise_power * bandwidth_hz / self.rate))

    def line(self, over_db: float) -> float:
        """The amplitude of a line ``over_db`` over the noise in one bin."""
        return self.amp(over_db, 1.5 * self.rate / self.fft)

    def air(self, seconds: float, things: list, seed: int = 0) -> Iterator[np.ndarray]:
        """Blocks of IQ, a quarter of a second each: noise and whatever ``things`` add."""
        rng = np.random.default_rng(seed)
        block = self.rate // 4
        for k in range(round(seconds * self.rate) // block):
            t = (k * block + np.arange(block)) / self.rate
            x = (self.noise * rng.standard_normal(2 * block, dtype=np.float32)).view(np.complex64)
            for thing in things:
                thing.add(x, t)
            raw = np.clip(np.rint(x.view(np.float32) * 128.0 + 127.4), 0, 255).astype(np.uint8)
            yield iq_from_u8(raw)

    def detector(self, channels: tuple[int, ...] = (), **kw: object) -> ActivityDetector:
        """A detector on this scene, its open range the whole window unless given."""
        kw.setdefault("open_range", (self.centre - self.rate / 2, self.centre + self.rate / 2))
        return ActivityDetector(self.rate, self.centre, channels, 12_500.0, fft=self.fft, **kw)

    def hz(self, offset_hz: float) -> float:
        return self.centre + offset_hz

    def seconds(self, samples: int | None) -> float:
        return (samples or 0) / self.rate


def watch(detector: ActivityDetector, blocks: Iterator[np.ndarray]) -> list[Activity]:
    """Everything the detector reports over ``blocks``, and at the end."""
    out: list[Activity] = []
    for iq in blocks:
        out += detector.feed(iq)
    return out + detector.flush()
