# SPDX-License-Identifier: Apache-2.0
"""Narrowband voice channels from an IQ stream, in numpy/scipy: NFM (walkie-talkies, amateur
repeaters, marine radio) and AM (airband, CB; medium wave is below what the tuner reaches).

    IQ at the device rate (e.g. 2.4 MS/s)
      ─► one frequency-translating FIR: low-pass taps shifted up to the channel, decimating to
         48 kHz, so nothing runs at the device rate except the filter's own outputs
      ─► channel filter at 48 kHz (± about 45 % of the channel spacing)
      ─► squelch (silence when closed): NFM by the discriminator's noise above the channel, AM
         by the carrier's spectral line, either by a fixed level when one is set
      ─► NFM: FM discriminator, de-emphasis (6 dB per octave), then a sharp 300 Hz high-pass
         that removes sub-audible CTCSS tones
         AM: the envelope divided by its carrier level (a natural AGC), DC removed
      ─► voice low-pass (about 3.8 kHz) and decimation to 16 kHz

Why shift the taps instead of mixing first: mixing a channel down at 2.4 MS/s costs one complex
exponential and one multiply per input sample, for every channel. Filtering with taps
``h[k]·e^{jωk}`` and rotating the (50 times fewer) outputs by ``e^{-jωn}`` gives the same result.
A channel costs about 2 % of an M1 Max core at 2.4 MS/s, two thirds of it in that first filter,
so a dongle can watch all 16 PMR446 channels at once.

Narrowband FM swings ±2.5 kHz (12.5 kHz channels) or ±5 kHz (25 kHz channels). AM puts the
voice on the carrier's amplitude; an airband channel is 8.33 or 25 kHz wide.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np
from scipy.signal import butter, ellip, lfilter, lfilter_zi, sosfilt

from wavescribe.dsp import DEFAULT_CHANNEL_HZ, MAX_CHANNEL_HZ, MIN_CHANNEL_HZ, NARROW_IF_RATE
from wavescribe.dsp.wfm import Decimator, lowpass

AUDIO_RATE = 16_000
VOICE_HZ = 3_800  # what a narrowband voice channel carries
SUB_BLOCK = 480  # 10 ms at 48 kHz: the squelch's resolution
FADE = SUB_BLOCK // 2  # 5 ms: how fast the audio comes in and goes out with the squelch

# Peak FM deviation by channel spacing: what a full-scale tone swings the carrier.
DEVIATION_HZ = {12_500: 2_500, 25_000: 5_000}

# Voice radios (land mobile, amateur, marine, weather radio) boost the audio by 6 dB per octave
# from 300 to 3000 Hz before the modulator, which makes their FM a phase modulation in effect;
# the receiver takes it back with the opposite slope (TIA-603, EN 300 086). One pole at 212 Hz
# (750 µs) follows that slope within 1.6 dB over the whole voice band. Without it the audio is
# thin and the discriminator's noise, which also rises with frequency, is left in full.
# The filter is scaled to unity at 300 Hz, the bottom of the voice band, so it only ever cuts:
# no audio a transmitter can send is boosted into clipping. A tone at full deviation comes out
# at 0.8 of full scale at 300 Hz and 0.29 at 1 kHz.
DEEMPHASIS_S = 750e-6
VOICE_LOW_HZ = 300
# CTCSS ("sub-audible") tones sit at 67 to 254.1 Hz, some 17 dB under full deviation, and the
# de-emphasis lifts them 10 to 13 dB against 1 kHz. A sixth-order elliptic high-pass (0.5 dB
# ripple from 300 Hz up, 40 dB down from 255 Hz) leaves them 46 dB or more under a 1 kHz tone
# at full deviation; a fourth-order Butterworth would leave the 254.1 Hz tone only 14 dB under.
CTCSS_FILTER = ellip(6, 0.5, 40, VOICE_LOW_HZ, "highpass", fs=NARROW_IF_RATE, output="sos")
AM_HUM_FILTER = butter(2, 100, "highpass", fs=NARROW_IF_RATE, output="sos")  # and the envelope's DC


class XlatingDecimator:
    """Low-pass + decimate one channel at ``offset_hz`` from the centre: complex IQ in, complex
    baseband out at ``rate / factor``, the channel at 0 Hz. Keeps its state between calls."""

    def __init__(self, rate: int, offset_hz: float, factor: int, passband: float, stopband: float):
        taps = lowpass(rate, passband, stopband)
        w = 2.0 * np.pi * offset_hz / rate
        shifted = taps * np.exp(1j * w * np.arange(len(taps)))
        self._dec = Decimator(shifted.astype(np.complex64), factor, np.complex64)
        self._step = -w * factor  # the output's rotation per output sample
        self._n = 0  # outputs produced so far (their phase runs on in float64)

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        y = self._dec(iq)
        if len(y) == 0:
            return y
        phase = self._step * (self._n + np.arange(len(y), dtype=np.float64))
        self._n += len(y)
        return y * np.exp(1j * (phase % (2.0 * np.pi))).astype(np.complex64)


class Gate:
    """Squelch decisions per block, with hysteresis and a hang time.

    ``open_when`` opens it and ``stay_when`` (a lower bar) holds it. A block that is neither
    still passes for up to ``hang`` blocks, so a weak signal dipping into the noise isn't
    chopped. A block that is ``gone_when`` (plainly no signal at all) is muted at once, and so
    are those after it until one opens or holds it again. For the hang time after the last
    block that held it, ``stay_when`` is enough to reopen it: a signal coming out of a deep
    fade is back within a block."""

    def __init__(self, hang: int) -> None:
        self._hang = max(1, hang)
        self._quiet = 0  # blocks since the last one that opened or held it
        self._armed = False  # within the hang time
        self.open = False  # the last block passed

    def __call__(
        self, open_when: np.ndarray, stay_when: np.ndarray, gone_when: np.ndarray
    ) -> np.ndarray:
        out = np.zeros(len(open_when), dtype=bool)
        flags = zip(open_when, stay_when, gone_when, strict=True)
        for i, (on, stay, gone) in enumerate(flags):
            if on or (self._armed and stay):
                self._armed, self.open, self._quiet = True, True, 0
            elif self._armed:
                self._quiet += 1
                self._armed = self._quiet <= self._hang
                self.open = self.open and self._armed and not gone
            out[i] = self.open
        return out


# NFM noise squelch. With no carrier the discriminator puts out loud noise far beyond the
# channel's own bandwidth; a carrier captures the discriminator and that noise collapses. What is
# measured is the discriminator's output above NOISE_CORNER of the channel width (9 kHz for a
# 12.5 kHz channel, 18 kHz for 25 kHz: clear of the audio and of the noise the channel filter
# passes around a carrier), in dB against the same measurement on noise alone.
# The ratio follows the signal-to-noise ratio in the channel and nothing else: not the gain, not
# the signal's strength. The noise-alone level it is taken against does depend on the channel
# filter (-11 dB re 1 rad² at 6.25 kHz, -7.5 dB at 12.5 kHz, -6.8 dB at 25 kHz), so
# ``no_carrier_db`` measures it per width. Measured with the signal-to-noise ratio taken in
# the channel's width: the squelch opens (noise 11 dB down) on a bare carrier at about 4 dB and
# on a tone at full deviation at about 5 dB, for 12.5, 25 and 30 kHz channels alike (6.25 kHz:
# 1 to 2 dB earlier), and holds (6 dB down) to about 2 dB.
# A block within 3 dB of the noise-alone level counts as gone: no carrier, or one too deep in
# the noise to carry anything (around 0 dB SNR). Noise alone reads that high in all but 0.01 %
# of 10 ms blocks at 12.5 kHz (1.3 % at 6.25 kHz, where a block holds fewer independent
# samples). So the full-scale noise that follows a carrier is muted from its first block, and
# the hang time is left to signals that hover between holding the squelch and gone.
NOISE_CORNER = 0.72
NFM_OPEN_DB, NFM_STAY_DB, NFM_GONE_DB = -11.0, -6.0, -3.0
# A fixed level: held to 3 dB under it, gone 6 dB under it.
LEVEL_STAY_DB, LEVEL_GONE_DB = -3.0, -6.0
# AM carrier squelch: a carrier is a spectral line. The strongest bin within ±3 kHz against the
# channel's median bin is about 10 dB for noise alone, 30 dB for a carrier at 0 dB SNR.
AM_OPEN_DB, AM_STAY_DB = 16.0, 13.0
AM_BLOCK = 4_800  # 100 ms at 48 kHz: 10 Hz bins


def _channel_filter(width: float) -> np.ndarray:
    """The channel's low-pass at 48 kHz: flat to 45 % of the spacing, 60 dB down at 60 %."""
    return lowpass(NARROW_IF_RATE, 0.45 * width, 0.6 * width)


def _noise_filter(width: float) -> tuple[np.ndarray, np.ndarray]:
    """The high-pass that leaves only the discriminator's noise above the channel."""
    return butter(4, NOISE_CORNER * width, "highpass", fs=NARROW_IF_RATE)


@lru_cache(maxsize=64)
def no_carrier_db(width: float) -> float:
    """What the NFM squelch measures with no signal at all, in dB re 1 rad² per sample: two
    seconds of white noise through the channel filter, the discriminator and the noise filter.
    It is a property of the filters alone, so each width is measured once (about 10 ms)."""
    rng = np.random.default_rng(0)
    n = 2 * NARROW_IF_RATE
    x = np.convolve(rng.normal(size=n) + 1j * rng.normal(size=n), _channel_filter(width), "valid")
    hf = lfilter(*_noise_filter(width), np.angle(x[1:] * np.conj(x[:-1])))
    return float(10.0 * np.log10(np.mean(hf[NARROW_IF_RATE // 10 :] ** 2)))


class NarrowbandDemodulator:
    """One NFM or AM channel: complex IQ at ``rate`` in, 16 kHz float32 mono audio out
    (silence while the squelch is closed)."""

    def __init__(
        self,
        rate: int,
        offset_hz: float,
        mode: str = "nfm",
        channel_hz: float | None = None,
        squelch_db: float | None = None,
        hang_s: float = 0.6,
    ) -> None:
        if mode not in DEFAULT_CHANNEL_HZ:
            raise ValueError(f"mode {mode!r} is not narrowband (nfm or am)")
        if rate % NARROW_IF_RATE:
            raise ValueError(f"sample rate {rate} is not a multiple of {NARROW_IF_RATE}")
        self.mode = mode
        self.rate = rate
        self.offset_hz = offset_hz
        width = float(channel_hz or DEFAULT_CHANNEL_HZ[mode])
        if not MIN_CHANNEL_HZ <= width <= MAX_CHANNEL_HZ:
            raise ValueError(f"a channel is {MIN_CHANNEL_HZ / 1e3:g} to "
                             f"{MAX_CHANNEL_HZ / 1e3:g} kHz wide, not {width / 1e3:g}")  # fmt: skip
        self.channel_hz = width
        passband = 0.45 * width
        # Stage 1 only has to keep whatever would fold onto the channel out: wide transition.
        self._front = XlatingDecimator(
            rate, offset_hz, rate // NARROW_IF_RATE, passband, NARROW_IF_RATE - passband
        )
        self._channel = Decimator(_channel_filter(width), 1, np.complex64)
        # A fixed level (dBFS) replaces the automatic squelch when given.
        self.squelch_db = squelch_db
        self._block = SUB_BLOCK if mode == "nfm" or squelch_db is not None else AM_BLOCK
        self._gate = Gate(round(hang_s * NARROW_IF_RATE / self._block))
        # The audio runs two squelch blocks (20 ms) behind the decisions, so that it can end
        # before the noise does (see _shape). AM's automatic squelch works in 100 ms blocks and
        # gates them as they are.
        self._delayed = self._block == SUB_BLOCK
        self._held = np.zeros(2 * SUB_BLOCK, dtype=np.float32)  # the blocks not yet put out
        self._held_open = [False, False]  # whether the squelch passed each of them
        self._was_open = False  # whether the block put out last was heard
        self._fade = (0.5 - 0.5 * np.cos(np.pi * (np.arange(FADE) + 0.5) / FADE)).astype(np.float32)
        self._hf = _noise_filter(width)
        self._hf_zi = lfilter_zi(*self._hf) * 0.0
        self._no_carrier_db = no_carrier_db(width) if mode == "nfm" else 0.0
        self._window = np.hanning(AM_BLOCK).astype(np.float32)
        bins = np.fft.fftfreq(AM_BLOCK, 1 / NARROW_IF_RATE)
        self._carrier_bins = np.abs(bins) <= 3_000
        self._channel_bins = np.abs(bins) <= passband
        self.detect_db: float | None = None  # the squelch's last measurement
        dev = DEVIATION_HZ.get(round(width), width * 0.2)
        self._fm_gain = NARROW_IF_RATE / (2.0 * np.pi * dev) * 0.8
        self._last = np.complex64(0)
        self._carrier = 0.0  # AM: slow average of the envelope
        self._carrier_a = float(np.exp(-1.0 / (NARROW_IF_RATE * 0.05)))  # 50 ms
        a = float(np.exp(-1.0 / (NARROW_IF_RATE * DEEMPHASIS_S)))
        at_low = abs(1.0 - a * np.exp(-2j * np.pi * VOICE_LOW_HZ / NARROW_IF_RATE))
        self._deemph = ([at_low], [1.0, -a])  # unity at VOICE_LOW_HZ
        self._deemph_state = np.zeros(1)
        self._hp = CTCSS_FILTER if mode == "nfm" else AM_HUM_FILTER
        self._hp_state = np.zeros((len(self._hp), 2))
        to_audio = lowpass(NARROW_IF_RATE, VOICE_HZ, AUDIO_RATE - VOICE_HZ)
        self._to_audio = Decimator(to_audio, NARROW_IF_RATE // AUDIO_RATE)
        self._pending = np.zeros(0, dtype=np.complex64)  # samples short of a whole squelch block
        self.power_db: float | None = None  # the channel's power in the last call, dBFS

    @property
    def open(self) -> bool:
        return self._gate.open

    def __call__(self, iq: np.ndarray) -> np.ndarray:
        x = self._channel(self._front(iq))
        if len(self._pending):
            x = np.concatenate([self._pending, x])
        blk = self._block
        n = len(x) - len(x) % blk
        self._pending = x[n:].copy()
        x = x[:n]
        if n == 0:
            return np.zeros(0, dtype=np.float32)
        power = np.abs(x) ** 2
        p = float(power.mean())
        self.power_db = 10.0 * np.log10(p) if p > 0 else -200.0

        phase_step = None
        if self.mode == "nfm":
            prev = np.concatenate([[self._last], x[:-1]])
            phase_step = np.angle(x * np.conj(prev)).astype(np.float32)
        if self.squelch_db is not None:
            level = 10.0 * np.log10(power.reshape(-1, blk).mean(axis=1) + 1e-20)
            on, stay = level >= self.squelch_db, level >= self.squelch_db + LEVEL_STAY_DB
            gone = level < self.squelch_db + LEVEL_GONE_DB
            self.detect_db = float(level[-1])
        elif phase_step is not None:
            hf, self._hf_zi = lfilter(*self._hf, phase_step, zi=self._hf_zi)
            noise = 10.0 * np.log10((hf**2).reshape(-1, blk).mean(axis=1) + 1e-20)
            noise -= self._no_carrier_db
            on, stay, gone = noise <= NFM_OPEN_DB, noise <= NFM_STAY_DB, noise >= NFM_GONE_DB
            self.detect_db = float(noise[-1])
        else:
            spec = np.abs(np.fft.fft(x.reshape(-1, blk) * self._window, axis=1)) ** 2
            carrier = spec[:, self._carrier_bins].max(axis=1)
            floor = np.median(spec[:, self._channel_bins], axis=1) + 1e-30
            ratio = 10.0 * np.log10(carrier / floor)
            on, stay, gone = ratio >= AM_OPEN_DB, ratio >= AM_STAY_DB, np.zeros(len(ratio), bool)
            self.detect_db = float(ratio[-1])
        passing = self._gate(on, stay, gone)

        if phase_step is not None:
            audio, self._deemph_state = lfilter(
                *self._deemph, phase_step * self._fm_gain, zi=self._deemph_state
            )
        else:
            env = np.abs(x).astype(np.float64)
            carrier = lfilter([1.0 - self._carrier_a], [1.0, -self._carrier_a], env,
                              zi=[self._carrier * self._carrier_a])[0]  # fmt: skip
            self._carrier = float(carrier[-1])
            audio = ((env - carrier) / np.maximum(carrier, 1e-6) * 0.6).astype(np.float32)
        self._last = x[-1]
        audio, self._hp_state = sosfilt(self._hp, audio, zi=self._hp_state)
        audio = audio.astype(np.float32)
        if self._delayed:
            audio = self._shape(audio, passing)
        else:
            audio = np.where(np.repeat(passing, blk), audio, np.float32(0.0))
        return self._to_audio(np.clip(audio, -1.0, 1.0))

    def _shape(self, audio: np.ndarray, passing: np.ndarray) -> np.ndarray:
        """The audio two blocks late: a block is heard only if the squelch passed it and the
        one after it, fading in over the first 5 ms and out over the last.

        The block in which a carrier drops can still measure as signal, and what follows the
        drop within it is the full-scale noise the squelch is there to keep out. Muting the block
        before every closed one drops that noise (and at most the transmission's last 15 ms),
        and a lone noise block that happens to measure as signal with it."""
        m = len(passing)
        audio = np.concatenate([self._held, audio])
        passed = np.concatenate([self._held_open, passing])
        heard = passed[:-1] & passed[1:]  # the m blocks put out here, and the one after
        now, after = heard[:m], heard[1:]
        before = np.concatenate([[self._was_open], now[:-1]])
        gain = np.repeat(now, SUB_BLOCK).astype(np.float32).reshape(m, SUB_BLOCK)
        gain[now & ~before, :FADE] *= self._fade
        gain[now & ~after, -FADE:] *= self._fade[::-1]
        self._held = audio[m * SUB_BLOCK :].copy()
        self._held_open = [bool(passed[-2]), bool(passed[-1])]
        self._was_open = bool(now[-1])
        return audio[: m * SUB_BLOCK] * gain.reshape(-1)
