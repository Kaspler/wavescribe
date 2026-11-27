# SPDX-License-Identifier: Apache-2.0
"""Noticing transmissions anywhere in what a dongle hears, for a watch (``sources/watch.py``).

    IQ at the device rate (e.g. 2.4 MS/s)
      ─► 4096-point FFTs (586 Hz apart at 2.4 MS/s), averaged over about 50 ms: one frame
      ─► each channel of the band's raster: its power against its own noise floor
      ─► a start after two frames above the threshold, an end after a hang time below it

Every FFT bin has a noise floor of its own. It follows the bin only while the bin's channel is
quiet: down within seconds, up over tens of seconds. Whatever is always there (the dongle's
spike at its centre, its learnt spurs, a beacon, a neighbour's computer) becomes part of the
floor and never counts as traffic. The bins right at the centre and at each learnt spur are left
out of the measurement as well, since such lines wander a little with temperature and gain.
A channel busy for minutes is taken into its floor too, so a stuck carrier ends as one long
catch instead of holding a demodulator for good.

A strong signal also shows where it isn't: on its neighbours (its own splatter and the
transmitter's noise) and, weaker, mirrored about the receiver's centre. A start 20 dB or more
under a signal close by, or under its own mirror image, is taken for that signal's shadow.
Where channels overlap (a 25 kHz channel on a 5 kHz raster), only the strongest of the
overlapping ones starts. Many channels rising together is interference or overload (USB noise,
a strong signal just outside the window), not traffic: while it lasts nothing starts and the
floors hold. Ten seconds of it without a break is no passing interference but the noise itself
risen (a charger switched on nearby), so the floors are learnt afresh, as at the start, rather
than the watch staying deaf for good.

Times are counted in samples since the first block, so the caller can find a transmission's
beginning in its own history of the IQ and demodulate it from there.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import fft as sp_fft

FFT = 4096
FRAME_S = 0.05
THRESHOLD_DB = 8.0  # a channel this far above its floor is busy (the scan's own margin)
STAY_DB = 3.0  # once busy, it stays busy down to this much under the threshold
START_FRAMES = 2  # frames above the threshold before it counts: one frame can be a click
HANG_S = 1.2  # a pause inside one over (a breath, "over" and the reply) stays in one catch
MIN_S = 0.3  # shorter is a data burst or a key click, counted and not kept
CARRIER_S = 120.0  # busy for longer: a stuck carrier or a beacon, learnt into the floor
INIT_S = 0.5  # the first floors come from the median of this much of the band
FLOOR_DOWN_S, FLOOR_UP_S = 2.0, 20.0  # how fast a quiet bin's floor follows it
GUARD_HZ = 1_500  # left out around the centre (DC) and around each learnt spur
MEASURED = 0.8  # the middle of each channel is measured, clear of its neighbours' edges
SHADOW_DB = 20.0  # this much under a signal nearby or at the mirror: its shadow, not traffic
SHADOW_CHANNELS = 3  # how far "nearby" reaches, in channels
BROADBAND = 0.5  # this share of the channels rising at once (and at least four): interference
CLEAR_S = 0.5  # interference is over once this long has passed without it
BLIND_S = 10.0  # interference this long without a break: the noise rose, learnt afresh


@dataclass(frozen=True)
class Activity:
    """Something that happened on one channel. ``start`` and ``end`` are sample indices:
    the transmission's first frame above the threshold, and the end of its last."""

    kind: str  # "start", "end" or "short" (ended before MIN_S: not a catch)
    channel: int  # index into the detector's channels
    freq_hz: int
    start: int
    end: int | None = None
    snr_db: float = 0.0  # the strongest it was over its noise floor
    power_db: float = 0.0  # the channel's strongest power, dBFS
    reason: str | None = None  # an end that wasn't the signal going: "carrier"


@dataclass
class _Channel:
    state: str = "quiet"  # quiet, rising, busy
    frames: int = 0  # frames above in a row, while rising
    first: int = 0  # sample index of the first frame above
    last: int = 0  # end of the last frame above, while busy
    snr_db: float = 0.0
    power_db: float = -200.0


class ActivityDetector:
    """Watches ``channels`` (Hz) of a dongle tuned to ``center_hz`` at ``rate`` and reports
    transmissions on them as they start and end. ``feed`` takes IQ blocks of any length.
    ``spurs`` are the dongle's own lines, as learnt at bins ``spur_bin_hz`` wide."""

    def __init__(
        self,
        rate: int,
        center_hz: int,
        channels: list[int] | tuple[int, ...],
        channel_hz: float,
        *,
        step_hz: float | None = None,
        threshold_db: float = THRESHOLD_DB,
        spurs: list[int] | tuple[int, ...] = (),
        spur_bin_hz: float = 0.0,
        fft: int = FFT,
        hang_s: float = HANG_S,
        min_s: float = MIN_S,
        carrier_s: float = CARRIER_S,
    ) -> None:
        self.rate = rate
        self.center_hz = center_hz
        self.freqs = [int(f) for f in channels]
        self.threshold_db = threshold_db
        self._n = fft
        self._per_frame = max(1, round(FRAME_S * rate / fft))
        self._frame_samples = self._per_frame * fft
        frame_s = self._frame_samples / rate
        self._hang = round(hang_s * rate)
        self._min = round(min_s * rate)
        self._carrier = round(carrier_s * rate)
        self._init_frames = max(3, round(INIT_S / frame_s))
        self._clear = round(CLEAR_S * rate)
        self._blind = round(BLIND_S * rate)
        self._down = 1.0 - np.exp(-frame_s / FLOOR_DOWN_S)
        self._up = 1.0 - np.exp(-frame_s / FLOOR_UP_S)
        self._window = np.hanning(fft).astype(np.float32)
        self._norm = float(self._window.sum()) ** 2  # a full-scale tone reads 1 (0 dBFS)

        step = float(step_hz or channel_hz)
        half = MEASURED * min(step, channel_hz) / 2
        bin_hz = rate / fft
        offsets = (np.arange(fft) - fft // 2) * bin_hz  # each bin's offset from the centre
        excluded = np.abs(offsets) <= GUARD_HZ
        # A learnt spur is only known to the bin it was learnt at: it may be anywhere in it.
        spur_guard = GUARD_HZ + spur_bin_hz / 2
        for s in spurs:
            excluded |= np.abs(offsets - (s - center_hz)) <= spur_guard
        self._weights = np.zeros((len(self.freqs), fft), dtype=np.float64)
        self._mirror = np.zeros_like(self._weights)
        self._owner = np.full(fft, -1)  # the channel each bin is measured for
        for i, f in enumerate(self.freqs):
            near = (np.abs(offsets - (f - center_hz)) <= half) & ~excluded
            self._weights[i, near] = 1.0
            self._owner[near] = i
            # The same bins mirrored about the centre: where the receiver's mirror of this
            # channel lies, and where the signal this channel would be a mirror of is.
            mirrored = np.abs(offsets + (f - center_hz)) <= half
            self._mirror[i, mirrored & ~excluded] = 1.0
        self._measured = self._weights.sum(axis=1) > 0
        # Channels close enough for one to be the other's splatter, and those that overlap.
        apart = np.abs(np.subtract.outer(self.freqs, self.freqs)).astype(float)
        self._nearby = (apart > 0) & (apart <= SHADOW_CHANNELS * max(step, channel_hz))
        self._overlapping = (apart > 0) & (apart < channel_hz)

        self._floor: np.ndarray | None = None
        self._first_frames: list[np.ndarray] = []
        self._pending = np.zeros(0, dtype=np.complex64)
        self._acc = np.zeros(fft, dtype=np.float64)
        self._acc_count = 0
        self._samples = 0  # samples fed so far
        self._frame_start = 0
        self._channels = [_Channel() for _ in self.freqs]
        self._deaf_since: int | None = None  # where interference across the window began
        self._interfered = 0  # the end of the last frame with interference
        self.relearnt = 0  # times the floors were learnt afresh after the noise rose
        self.snr_db = np.zeros(len(self.freqs))  # each channel over its floor, last frame
        self.power_db = np.full(len(self.freqs), -200.0)  # each channel's power, last frame

    @property
    def busy(self) -> list[int]:
        return [i for i, c in enumerate(self._channels) if c.state == "busy"]

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def deaf_s(self) -> float | None:
        """How long interference across the window has kept anything from starting, or None."""
        if self._deaf_since is None:
            return None
        return (self._frame_start - self._deaf_since) / self.rate

    def feed(self, iq: np.ndarray) -> list[Activity]:
        x = np.concatenate([self._pending, iq]) if len(self._pending) else iq
        whole = len(x) // self._n
        self._pending = x[whole * self._n :].copy()
        out: list[Activity] = []
        if whole == 0:
            return out
        blocks = x[: whole * self._n].reshape(whole, self._n) * self._window
        power = np.abs(sp_fft.fft(blocks, axis=1, workers=1)) ** 2
        k = 0
        while k < whole:
            take = min(whole - k, self._per_frame - self._acc_count)
            self._acc += power[k : k + take].sum(axis=0)
            self._acc_count += take
            k += take
            if self._acc_count == self._per_frame:
                frame = np.fft.fftshift(self._acc / (self._per_frame * self._norm))
                end = self._frame_start + self._frame_samples
                out += self._frame(frame, self._frame_start, end)
                self._frame_start = end
                self._acc[:] = 0.0
                self._acc_count = 0
        self._samples += len(iq)
        return out

    def _frame(self, power: np.ndarray, start: int, end: int) -> list[Activity]:
        if self._floor is None:
            self._first_frames.append(power)
            if len(self._first_frames) >= self._init_frames:
                self._floor = np.median(np.array(self._first_frames), axis=0) + 1e-20
                self._first_frames = []
            return []
        floor = self._floor
        signal = self._weights @ power
        self.snr_db = 10 * np.log10((signal + 1e-20) / (self._weights @ floor + 1e-20))
        self.power_db = 10 * np.log10(signal + 1e-20)
        mirror_db = 10 * np.log10((self._mirror @ power + 1e-20) / (self._mirror @ floor + 1e-20))
        above = (self.snr_db >= self.threshold_db) & self._measured
        holding = self.snr_db >= self.threshold_db - STAY_DB
        states = [c.state for c in self._channels]
        rising_now = sum(1 for i in np.flatnonzero(above) if states[i] != "busy")
        if rising_now >= max(4, BROADBAND * len(self.freqs)):
            self._interfered = end
            if self._deaf_since is None:
                self._deaf_since = start
            elif end - self._deaf_since >= self._blind:
                self._deaf_since = None
                self.relearnt += 1
                self._floor = None  # from the next frames, as at the start
                for c in self._channels:
                    if c.state == "rising":
                        c.state, c.frames = "quiet", 0
                return []
        elif self._deaf_since is not None and end - self._interfered >= self._clear:
            self._deaf_since = None
        deaf = self._deaf_since is not None

        out: list[Activity] = []
        for i in np.flatnonzero(above | np.array([s != "quiet" for s in states])):
            c = self._channels[i]
            if c.state == "busy":
                if holding[i]:
                    c.last = end
                    c.snr_db = max(c.snr_db, float(self.snr_db[i]))
                    c.power_db = max(c.power_db, float(self.power_db[i]))
                    if end - c.first >= self._carrier:
                        out.append(self._end(i, "carrier"))
                        floor[self._weights[i] > 0] = power[self._weights[i] > 0]
                elif end - c.last >= self._hang:
                    out.append(self._end(i))
                continue
            if not above[i] or deaf:
                c.state, c.frames = "quiet", 0
                continue
            if c.state == "quiet":
                c.state, c.frames, c.first = "rising", 0, start
                c.snr_db, c.power_db = -200.0, -200.0
            c.frames += 1
            c.snr_db = max(c.snr_db, float(self.snr_db[i]))
            c.power_db = max(c.power_db, float(self.power_db[i]))
            if c.frames < START_FRAMES:
                continue
            if self._shadowed(i, above, mirror_db):
                c.frames = 0  # tried again next frame, from where it is then
                c.first = end
                continue
            c.state, c.last = "busy", end
            out.append(
                Activity(
                    "start", int(i), self.freqs[i], c.first, snr_db=c.snr_db, power_db=c.power_db
                )
            )

        if not deaf:
            # Only bins whose channel is quiet learn: a transmission must not lift its own floor.
            hot = np.array([False] + [c.state != "quiet" for c in self._channels])
            learn = ~hot[self._owner + 1]
            rate = np.where(power < floor, self._down, self._up)
            floor[learn] += rate[learn] * (power[learn] - floor[learn])
            np.maximum(floor, 1e-20, out=floor)
        return out

    def _shadowed(self, i: int, above: np.ndarray, mirror_db: np.ndarray) -> bool:
        """Whether channel ``i`` rising is the shadow of a stronger signal: its own splatter
        on a channel nearby, the receiver's mirror of it, or the same signal measured on an
        overlapping channel."""
        snr = self.snr_db[i]
        if mirror_db[i] - snr >= SHADOW_DB:
            return True
        live = above | np.array([c.state == "busy" for c in self._channels])
        rivals = np.flatnonzero(live & (self._nearby[i] | self._overlapping[i]))
        for j in rivals:
            if self.snr_db[j] - snr >= SHADOW_DB:
                return True
            if self._overlapping[i, j] and self.snr_db[j] > snr:
                return True
        return False

    def _end(self, i: int, reason: str | None = None) -> Activity:
        c = self._channels[i]
        kind = "short" if c.last - c.first < self._min and reason != "carrier" else "end"
        out = Activity(kind, i, self.freqs[i], c.first, c.last, c.snr_db, c.power_db, reason)
        self._channels[i] = _Channel()
        return out

    def flush(self) -> list[Activity]:
        """End every transmission still running (the dongle stopped, or the watch did)."""
        return [self._end(i, "stopped") for i in self.busy]


class IqHistory:
    """The last ``seconds`` of a dongle's IQ, kept as the 8-bit samples the dongle sent (a
    quarter of the memory of complex floats), so a transmission noticed a moment after it began
    can be demodulated from its first syllable."""

    def __init__(self, rate: int, seconds: float = 2.0) -> None:
        self._cap = int(rate * seconds)
        self._buf = np.zeros(2 * self._cap, dtype=np.uint8)
        self.end = 0  # samples appended so far

    @property
    def start(self) -> int:
        """The oldest sample index still held."""
        return max(0, self.end - self._cap)

    def append(self, iq: np.ndarray) -> None:
        # The dongle's own bytes again: ``iq_from_u8`` scaled them as (u - 127.4) / 128.
        raw = np.clip(np.rint(iq.view(np.float32) * 128.0 + 127.4), 0, 255).astype(np.uint8)
        n = len(raw) // 2
        if n >= self._cap:
            raw, skipped = raw[-2 * self._cap :], n - self._cap
            n = self._cap
        else:
            skipped = 0
        at = 2 * ((self.end + skipped) % self._cap)
        first = min(len(raw), len(self._buf) - at)
        self._buf[at : at + first] = raw[:first]
        self._buf[: len(raw) - first] = raw[first:]
        self.end += n + skipped

    def since(self, index: int) -> np.ndarray:
        """The IQ from sample ``index`` (or the oldest held) to the newest, as complex64."""
        from wavescribe.dsp.wfm import iq_from_u8

        index = max(index, self.start)
        n = self.end - index
        if n <= 0:
            return np.zeros(0, dtype=np.complex64)
        at = 2 * (index % self._cap)
        raw = self._buf[at : at + 2 * n]
        if len(raw) < 2 * n:
            raw = np.concatenate([raw, self._buf[: 2 * n - len(raw)]])
        return iq_from_u8(raw)
