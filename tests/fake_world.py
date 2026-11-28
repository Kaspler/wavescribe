# SPDX-License-Identifier: Apache-2.0
"""A made-up sky for the stand-in rtl_sdr (``FAKE_RTL_WORLD``): transmitters at absolute
frequencies, so a receiver tuned anywhere hears what lies in its window, at the right offsets,
and a receiver moved elsewhere hears something else.

A world is JSON, a file or the text itself::

    {"clock": "start",                  # "start": every start is time 0; "wall": the wall clock
     "epoch": 0,                        # with "wall": the world's time 0, in Unix seconds
     "pace": false,                     # true: no faster than a dongle (for a page to watch)
     "noise": 0.01,                     # the noise floor: its standard deviation in I and in Q
     "ppm": 0, "dc": 0, "rolloff_db": 0,   # how a dongle lies; see below
     "receivers": {"<serial>": {"ppm": 40, ...}},   # per dongle: any of the four above
     "transmitters": [
       {"kind": "wfm", "mhz": 92.4, "ps": "VALLEY", "pi": "0x1234", "rt": "Hello"},
       {"kind": "wfm", "mhz": 93.1, "audio": "speech.f32", "gap_s": 1.0, "level": 0.1},
       {"kind": "nfm", "mhz": 446.00625, "tone_hz": 1000, "period_s": 4.096, "on_s": 0.768},
       {"kind": "am", "mhz": 120.5, "audio": "speech.f32", "depth": 0.6},
       {"kind": "line", "mhz": 433.92, "level": 0.05},
       {"kind": "block", "mhz": 211.648, "width_khz": 1536, "level": 0.03}]}

Every transmitter takes ``level`` (amplitude, 0.2 by default; full scale is 1; a block's is
what it would have filling the whole window), ``from_s`` and ``until_s`` (on the air between
those world times), and ``period_s``/``on_s``/``start_s``: keyed for the last ``on_s`` of
every ``period_s`` counted from ``start_s``, as a walkie-talkie is. A sound is ``tone_hz``
(default 1 kHz), ``audio`` (a float32 16 kHz mono file) or ``voice`` (a made-up voice from
tests/synthetic_iq.py, from that seed, ``voice_s`` long; on ``nfm`` through a walkie-talkie's
filters and limiter), sent again ``gap_s`` after it ends. ``wfm`` is broadcast FM: 75 kHz
deviation, 50 µs pre-emphasis on audio, a 19 kHz pilot, and RDS when it has ``ps`` or ``rt``.
``nfm`` takes ``deviation_hz`` (2.5 kHz), ``am`` ``depth`` (0.5). ``line`` is a bare carrier
and ``block`` a flat-topped spread of noise with steep sides, as a digital multiplex looks.

The dongle's own untruths, all off by default:

- ``ppm``: its clock is off by that much, less what rtl_sdr's ``-p`` corrects, so everything
  appears shifted (about 22 kHz at 446 MHz for 50 ppm);
- ``dc``: the spike at the centre of every window, a constant of that amplitude;
- ``rolloff_db``: the tuner's filter gives way towards the window's edges, by that many dB at
  ±rate/2 and very little within ±0.8 of it, so a carrier near ±1.1 MHz at 2.4 MS/s comes
  out weaker rather than cut clean;
- ``noise``: a floor raised above the default 0.01.

A transmitter that would straddle the window's edge is left out whole: sampled as complex IQ,
whatever lies past ±rate/2 would come back in at the other edge, where a real tuner's filter
removes it.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

BLOCK_S = 0.256
MPX_RATE = 240_000  # the sounds and the multiplex are made at about this rate, then held
RDS_BPS = 1187.5
HALF_WIDTH_HZ = {"wfm": 100_000, "nfm": 6_250, "am": 5_000, "line": 0}
ROLLOFF_ORDER = 8  # Butterworth order of the made-up tuner filter


@dataclass(frozen=True)
class Untruths:
    """How one dongle departs from a perfect receiver."""

    ppm: float = 0.0
    dc: float = 0.0
    rolloff_db: float = 0.0
    noise: float = 0.01

    def gain(self, offset_hz: np.ndarray | float, rate: float) -> np.ndarray | float:
        """The amplitude the tuner's filter leaves at ``offset_hz`` from the centre."""
        if self.rolloff_db <= 0:
            return np.ones_like(offset_hz) if isinstance(offset_hz, np.ndarray) else 1.0
        fc = (rate / 2) / (10 ** (self.rolloff_db / 10) - 1) ** (1 / (2 * ROLLOFF_ORDER))
        return 1 / np.sqrt(1 + (np.abs(offset_hz) / fc) ** (2 * ROLLOFF_ORDER))


class World:
    def __init__(self, spec: dict[str, Any], base: Path | None = None) -> None:
        self.spec = spec
        self.base = base or Path.cwd()

    @classmethod
    def load(cls, text_or_path: str) -> World:
        if text_or_path.lstrip().startswith("{"):
            return cls(json.loads(text_or_path))
        path = Path(text_or_path)
        return cls(json.loads(path.read_text()), path.parent)

    def untruths(self, serial: str | None) -> Untruths:
        keys = ("ppm", "dc", "rolloff_db", "noise")
        values = {k: self.spec[k] for k in keys if k in self.spec}
        values |= {
            k: v
            for k, v in self.spec.get("receivers", {}).get(serial or "", {}).items()
            if k in keys
        }
        return Untruths(**values)

    def tune(self, center_hz: float, rate: int, serial: str | None = None,
             ppm_corrected: float = 0.0) -> Tuned:  # fmt: skip
        """What a dongle tuned to ``center_hz`` hears, one block at a time."""
        if self.spec.get("clock", "start") == "wall":
            t0 = time.time() - float(self.spec.get("epoch", 0))
        else:
            t0 = 0.0
        return Tuned(self, center_hz, rate, self.untruths(serial), ppm_corrected, t0)


class Tuned:
    """One start of the stand-in: the world as heard from one place, block after block."""

    def __init__(self, world: World, center_hz: float, rate: int, untruths: Untruths,
                 ppm_corrected: float, t0: float) -> None:  # fmt: skip
        self.rate = rate
        self.up = max(1, round(rate / MPX_RATE))
        self.mpx_rate = rate / self.up
        # Whole multiplex samples per block, so the held samples line up with the block.
        self.n = self.up * round(rate * BLOCK_S / self.up)
        self.t0 = t0
        self.k = 0  # samples given so far
        self.untruths = untruths
        self.pace = bool(world.spec.get("pace")) or bool(os.environ.get("FAKE_RTL_PACE"))
        self._started = time.monotonic()
        self._rng = np.random.default_rng(int(center_hz) % (2**32))
        # The dongle's clock runs fast by ppm: tuned to f it sits at f(1 + ppm·1e-6).
        lo = center_hz * (1 + (untruths.ppm - ppm_corrected) * 1e-6)
        self.transmitters: list[_Transmitter] = []
        for spec in world.spec.get("transmitters", []):
            tx = _make(spec, world.base, self, float(spec["mhz"]) * 1e6 - lo)
            if tx is not None:
                self.transmitters.append(tx)

    def seconds(self) -> float:
        return self.k / self.rate

    def block(self) -> np.ndarray:
        """The next ``n`` complex samples, before the converter."""
        n = self.n
        u = self.untruths
        noise = self._rng.standard_normal(2 * n, dtype=np.float32) * u.noise
        x = noise.view(np.complex64).astype(np.complex128)
        t_world = self.t0 + (self.k + np.arange(n)) / self.rate
        for tx in self.transmitters:
            if (s := tx.block(self.k, t_world)) is not None:
                x += s
        if u.dc:
            x += u.dc * np.exp(0.7j)
        self.k += n
        if self.pace:
            ahead = self._started + self.seconds() - time.monotonic()
            if ahead > 0:
                time.sleep(ahead)
        return x

    def u8(self) -> bytes:
        return u8(self.block())


def u8(x: np.ndarray) -> bytes:
    """Complex samples as rtl_sdr writes them: I and Q interleaved, 8 bits each, offset 127.4."""
    iq = np.empty(2 * len(x))
    iq[0::2], iq[1::2] = x.real, x.imag
    return np.clip(np.round(iq * 128 + 127.4), 0, 255).astype(np.uint8).tobytes()


# The transmitters.


class _Transmitter:
    def __init__(self, spec: dict[str, Any], tuned: Tuned, offset_hz: float) -> None:
        self.spec = spec
        self.tuned = tuned
        self.offset = offset_hz
        self.level = float(spec.get("level", 0.2)) * float(
            tuned.untruths.gain(offset_hz, tuned.rate)
        )
        self._phase = 0.0  # of the carrier at the offset, at the start of the next block

    def keyed(self, t: np.ndarray) -> np.ndarray | bool:
        """Whether it is on the air at each world time ``t``."""
        s = self.spec
        on: np.ndarray | bool = True
        if "from_s" in s:
            on = t >= float(s["from_s"])
        if "until_s" in s:
            on = on & (t < float(s["until_s"]))
        if "period_s" in s:
            period, length = float(s["period_s"]), float(s.get("on_s", 0))
            since = (t - float(s.get("start_s", 0))) % period
            on = on & (since >= period - length)
        return on

    def carrier(self, n: int) -> np.ndarray:
        phase = self._phase + 2 * np.pi * self.offset * np.arange(n) / self.tuned.rate
        self._phase = float((self._phase + 2 * np.pi * self.offset * n / self.tuned.rate)
                            % (2 * np.pi))  # fmt: skip
        return phase

    def block(self, k: int, t: np.ndarray) -> np.ndarray | None:
        on = self.keyed(t)
        phase = self.carrier(len(t))
        if on is False or (isinstance(on, np.ndarray) and not on.any()):
            return None  # its sound goes on all the same: it is read by sample number
        out = self.signal(k, phase)
        return out if on is True else out * on

    def signal(self, k: int, phase: np.ndarray) -> np.ndarray:
        raise NotImplementedError


class _Sound:
    """A tone or a recording at the multiplex rate, peak 1, sample ``k`` onwards."""

    def __init__(self, spec: dict[str, Any], base: Path, rate: float, preemphasis: bool) -> None:
        self.rate = rate
        self.tone = float(spec.get("tone_hz", 1000))
        self.loop: np.ndarray | None = None
        if "voice" in spec and not preemphasis:  # as a walkie-talkie sends it
            from scipy.signal import resample_poly

            from tests import synthetic_iq

            x = synthetic_iq.radio_audio(float(spec.get("voice_s", 8.0)), int(spec["voice"]))
            ratio = Fraction(rate).limit_denominator(1000) / synthetic_iq.MOD
            x = resample_poly(x, ratio.numerator, ratio.denominator) if ratio != 1 else x
            gap = np.zeros(round(float(spec.get("gap_s", 1.0)) * rate))
            self.loop = np.concatenate([x, gap])
        elif "voice" in spec or spec.get("audio"):
            from scipy.signal import lfilter, resample_poly

            if "voice" in spec:
                from tests import synthetic_iq

                seconds = float(spec.get("voice_s", 8.0))
                x = synthetic_iq.voice(seconds, int(spec["voice"])).astype(np.float64)
            else:
                x = np.fromfile(base / spec["audio"], dtype=np.float32).astype(np.float64)
            if preemphasis:  # the inverse of the receiver's 50 µs de-emphasis
                a = np.exp(-1 / (16_000 * 50e-6))
                x = lfilter([1, -a], [1 - a], x)
            ratio = Fraction(rate).limit_denominator(1000) / 16_000
            x = resample_poly(x, ratio.numerator, ratio.denominator)
            x = x / (np.max(np.abs(x)) or 1)
            gap = np.zeros(round(float(spec.get("gap_s", 1.0)) * rate))
            self.loop = np.concatenate([x, gap])

    def at(self, k: int, m: int) -> np.ndarray:
        if self.loop is None:
            return np.sin(2 * np.pi * self.tone * (k + np.arange(m)) / self.rate)
        return self.loop[(k + np.arange(m)) % len(self.loop)]


class _Fm(_Transmitter):
    def __init__(self, spec: dict[str, Any], base: Path, tuned: Tuned, offset: float,
                 deviation: float, preemphasis: bool) -> None:  # fmt: skip
        super().__init__(spec, tuned, offset)
        self.sound = _Sound(spec, base, tuned.mpx_rate, preemphasis)
        self.deviation = deviation
        self._swing = 0.0  # the modulation's phase, carried from block to block

    def mpx(self, k_mpx: int, m: int) -> np.ndarray:
        return self.sound.at(k_mpx, m)

    def signal(self, k: int, phase: np.ndarray) -> np.ndarray:
        up = self.tuned.up
        m = len(phase) // up
        held = np.repeat(self.mpx(k // up, m), up)  # the channel filter smooths the steps
        swing = self._swing + 2 * np.pi * self.deviation * np.cumsum(held) / self.tuned.rate
        self._swing = float(swing[-1] % (2 * np.pi))
        return self.level * np.exp(1j * (phase + swing))


class _Wfm(_Fm):
    def __init__(self, spec: dict[str, Any], base: Path, tuned: Tuned, offset: float) -> None:
        super().__init__(spec, base, tuned, offset, 75_000, preemphasis=True)
        self.rds = _Rds(spec, tuned.mpx_rate) if ("ps" in spec or "rt" in spec) else None

    def mpx(self, k_mpx: int, m: int) -> np.ndarray:
        t = (k_mpx + np.arange(m)) / self.tuned.mpx_rate
        pilot = 2 * np.pi * 19_000 * t
        x = 0.8 * self.sound.at(k_mpx, m) + 0.09 * np.cos(pilot)
        if self.rds is not None:
            x += 0.04 * self.rds.at(k_mpx, m) * np.cos(3 * pilot)
        return x


class _Rds:
    """The station's groups, again and again, as shaped biphase symbols (before the 57 kHz
    subcarrier)."""

    def __init__(self, spec: dict[str, Any], rate: float) -> None:
        from scipy.signal import firwin

        from tests.rds_groups import bits_of, name_groups, text_groups

        pi = int(str(spec.get("pi", "0x1234")), 0)
        sent = name_groups(pi, str(spec.get("ps", ""))[:8]) * 4
        if rt := spec.get("rt"):
            sent += text_groups(pi, str(rt)[:64] + "\r")
        bits = np.array(bits_of(sent) * 2)  # twice: the differential code ends where it began
        self.sent = np.cumsum(bits) % 2  # differential coding
        self.rate = rate
        self.taps = firwin(151, 2_400, fs=rate)
        self._tail = np.zeros(len(self.taps) - 1)

    def at(self, k: int, m: int) -> np.ndarray:
        from scipy.signal import fftconvolve

        pos = (k + np.arange(m)) / self.rate * RDS_BPS
        bit = self.sent[np.floor(pos).astype(np.int64) % len(self.sent)]
        first_half = (pos - np.floor(pos)) < 0.5
        sym = np.where(bit == 1, 1.0, -1.0) * np.where(first_half, 1.0, -1.0)
        x = np.concatenate([self._tail, sym])
        self._tail = x[-(len(self.taps) - 1) :]
        return fftconvolve(x, self.taps, mode="valid")


class _Am(_Transmitter):
    def __init__(self, spec: dict[str, Any], base: Path, tuned: Tuned, offset: float) -> None:
        super().__init__(spec, tuned, offset)
        self.sound = _Sound(spec, base, tuned.mpx_rate, preemphasis=False)
        self.depth = float(spec.get("depth", 0.5))

    def signal(self, k: int, phase: np.ndarray) -> np.ndarray:
        up = self.tuned.up
        held = np.repeat(self.sound.at(k // up, len(phase) // up), up)
        return self.level * (1 + self.depth * held) * np.exp(1j * phase)


class _Line(_Transmitter):
    def signal(self, k: int, phase: np.ndarray) -> np.ndarray:
        return self.level * np.exp(1j * phase)


class _Block(_Transmitter):
    """A flat-topped spread of noise: the same block of it again and again."""

    def __init__(self, spec: dict[str, Any], tuned: Tuned, offset: float) -> None:
        super().__init__(spec, tuned, offset)
        n, rate = tuned.n, tuned.rate
        f = np.fft.fftfreq(n, 1 / rate)
        width = float(spec.get("width_khz", 1536)) * 1e3
        inside = (np.abs(f - offset) <= width / 2) & (np.abs(f) < rate / 2 * 0.999)
        rng = np.random.default_rng(int(abs(offset)) % (2**32))
        # Unit bins scaled so that a block filling the whole window has an RMS of 1: its
        # density is the same however much of it the window holds.
        spectrum = np.where(inside, np.exp(2j * np.pi * rng.random(n)), 0) * math.sqrt(n)
        self.loop = np.fft.ifft(spectrum * tuned.untruths.gain(f, rate))
        self.level = float(spec.get("level", 0.2))  # its sides carry the roll-off already

    def signal(self, k: int, phase: np.ndarray) -> np.ndarray:
        return self.level * self.loop[: len(phase)]


def _make(spec: dict[str, Any], base: Path, tuned: Tuned, offset: float) -> _Transmitter | None:
    kind = spec.get("kind", "wfm")
    if kind == "block":
        width = float(spec.get("width_khz", 1536)) * 1e3
        if abs(offset) - width / 2 >= tuned.rate / 2:
            return None  # nothing of it in the window
        return _Block(spec, tuned, offset)
    if abs(offset) + HALF_WIDTH_HZ[kind] >= tuned.rate / 2:
        return None
    if kind == "wfm":
        return _Wfm(spec, base, tuned, offset)
    if kind == "nfm":
        deviation = float(spec.get("deviation_hz", 2_500))
        return _Fm(spec, base, tuned, offset, deviation, preemphasis=False)
    if kind == "am":
        return _Am(spec, base, tuned, offset)
    if kind == "line":
        return _Line(spec, tuned, offset)
    raise ValueError(f"unknown transmitter kind {kind!r}")
