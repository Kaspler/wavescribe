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
overlapping ones starts. Many channels rising together, with the converter not clipping, is
interference (USB noise, a charger), not traffic: while it lasts nothing starts and the floors
hold.
Ten seconds of it without a break is no passing interference but the noise itself risen (a
charger switched on nearby), so the floors are learnt afresh, as at the start, rather than the
watch staying deaf for good.

Unless one channel towers over the rest: a walkie-talkie keyed a few metres from the antenna
overloads the dongle (its converter clips, its tuner mixes), and the whole window rises with the
products, tens of dB under the transmission itself. That channel is caught, marked ``overload``,
and nothing else starts while it lasts: the rest is its products. The same holds while the
converter clips hard with no rise, where the products land on a few channels of their own. The
floors hold, and the time doesn't count towards learning them afresh: the rise is the
transmission's, and ends with it.

Unless the transmission close by is on none of the channels (a walkie-talkie keyed on another
frequency in the window, or at the receiver's centre): then the channels hear only its products,
and the channel on top is one of them. While the receiver is overloaded, the strongest signal in
the whole window is found in the FFT the frame already has, in the bins standing the busy
threshold over their floors: the dongle's own spike at its centre, always there, is part of the
floor and no signal, though a transmission on top of it is. When it is on none of the channels
and stands over them and over everything else in the window, nothing starts, and the overload
is said with that frequency.

The converter clipping hard with no channel on top is an overload as well, not interference:
by a transmitter off the channels that isn't clear of the rest, or beyond the window, where it
isn't seen. Nothing starts, and it is said as off the channels, with no frequency. Where the
strongest signal is on a channel but stands over nothing (two walkie-talkies close by at once,
or one not yet clear of its copies), it is an overload with no transmitter known, and that
channel may still start if it comes to stand over the rest. An overload nothing is caught from
is learnt as the floor after ten seconds, as interference is, so a transmitter that stays
doesn't leave the watch deaf; while the converter still clips it stays noted as an overload.

One overload is one transmitter: its channel (or its frequency, off the channels) is fixed once
it is sure, and no other channel starts until the overload is over.

Clipping is counted per FFT, so each frame has its own share.

Times are counted in samples since the first block, so the caller can find a transmission's
beginning in its own history of the IQ and demodulate it from there.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import fft as sp_fft

from wavescribe.dsp import NARROW_MARGIN_HZ

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
# The strongest channel this far over every channel beyond its own splatter, while the window
# rises (or the converter clips hard): a transmission close by overloading the receiver, not
# interference. Measured on synthetic IQ: a walkie-talkie overloading the receiver stands 26 to
# 48 dB over the rest where the converter clips, 17.5 dB in the worst case without clipping (the
# rest lifted 40 dB); noise, slopes, combs, USB clicks and a carrier clipping from outside the
# window never more than 12 dB in any frame, under 5 dB as a rule.
OVERLOAD_DB = 15.0
# The strongest signal in the window this far over the channel on top, and on none of the
# channels: the transmission close by is off the channels, and the channel on top is one of its
# products, which are always weaker than it. With no channel on top, it must stand
# ``OVERLOAD_DB`` over every channel beyond its own reach, as a channel on top does.
OFF_CHANNEL_DB = 6.0
OFF_CHANNELS = -1  # an overload's transmitter, when it is on none of the channels
CLIPPING = 0.01  # this share of the 8-bit samples at the converter's limits: clipping hard
CLIP_LEVEL = 0.992  # |I| or |Q| this high is a sample at 0 or 255 (``iq_from_u8``)


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
    overload: bool = False  # the receiver was overloaded while it lasted, so far


@dataclass
class _Channel:
    state: str = "quiet"  # quiet, rising, busy
    frames: int = 0  # frames above in a row, while rising
    first: int = 0  # sample index of the first frame above
    last: int = 0  # end of the last frame above, while busy
    snr_db: float = 0.0
    power_db: float = -200.0
    overload: bool = False


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
        self._offsets = offsets
        self._half_bins = max(1, int(half / bin_hz))  # a channel's measured half, in bins
        # A signal this close to a channel is on it (a radio a little off frequency included).
        self._on_hz = channel_hz / 4
        self._busy = 10 ** (threshold_db / 10)  # a bin this far over its floor has a signal
        self._usable_hz = rate / 2 - NARROW_MARGIN_HZ  # beyond: the filter's edges, and aliases
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
        # Where an overloaded receiver makes copies of a signal itself: its mirror about the
        # centre, and the third-order product of its I and Q clipping at three times its offset
        # the other way (folded back into the band), only 10 to 15 dB under it. Channels that
        # near either are no measure of how far it stands over the rest.
        offsets_hz = np.array(self.freqs, dtype=float) - center_hz
        self._channel_offsets = offsets_hz
        self._reach = reach = SHADOW_CHANNELS * max(step, channel_hz)
        self._copies = np.zeros((len(self.freqs), len(self.freqs)), dtype=bool)
        for k in (-1, -3):
            at = (k * offsets_hz + rate / 2) % rate - rate / 2
            self._copies |= np.abs(at[:, None] - offsets_hz[None, :]) <= reach

        self._floor: np.ndarray | None = None
        self._first_frames: list[np.ndarray] = []
        self._pending = np.zeros(0, dtype=np.complex64)
        self._acc = np.zeros(fft, dtype=np.float64)
        self._acc_count = 0
        self._acc_clips = 0  # samples (I and Q apart) at the converter's limits, this frame
        self._frame_clipped = 0.0  # their share, in the frame being looked at
        self._samples = 0  # samples fed so far
        self._frame_start = 0
        self._channels = [_Channel() for _ in self.freqs]
        self._deaf_since: int | None = None  # where interference across the window began
        self._blind_since: int | None = None  # and where it last began with no channel on top
        self._interfered = 0  # the end of the last frame with interference
        # While deaf: "overload" (a transmitter on a channel or off them, or the converter
        # clipping hard with no channel on top) or "interference".
        self.deaf_reason: str | None = None
        self._culprit: int | None = None  # the overload's channel, or OFF_CHANNELS, once sure
        self._overload_seen = False  # this deafness is an overload: a culprit, or several
        self._off_frames = 0  # frames in a row overloaded with no channel on top
        self._off_hz: int | None = None  # where off the channels, the last of them that knew
        # An overload learnt as the floor while the converter still clips: heard through, and
        # still noted, until the clipping has stopped for ``CLEAR_S``.
        self._clip_learnt = False
        self._clipped_at = 0  # the end of the last frame the converter clipped in
        self.overloaded_at: int | None = None  # the end of the last frame overloaded
        # The last overload's transmitter, one for the whole overload: the channel it was caught
        # on (``overload_on_channel`` True), or where it was off the channels (False), to the kHz
        # (None where that isn't clear: at the window's edges, where signals from beyond it fold
        # in, or with the converter clipping and nothing clear of the rest). Both None when the
        # strongest signal was on a channel standing over nothing: not known which.
        self.overload_hz: int | None = None
        self.overload_on_channel: bool | None = None
        self.clipped = 0.0  # share of the last block's samples at the converter's limits (the log)
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
        """How long interference or overload across the window has kept anything (but the
        channel on top, overloaded) from starting, or None; ``deaf_reason`` says which."""
        if self._deaf_since is None:
            return None
        return (self._frame_start - self._deaf_since) / self.rate

    def feed(self, iq: np.ndarray) -> list[Activity]:
        x = np.concatenate([self._pending, iq]) if len(self._pending) else iq
        whole = len(x) // self._n
        self._pending = x[whole * self._n :].copy()
        out: list[Activity] = []
        if whole == 0:
            self._samples += len(iq)
            return out
        rows = x[: whole * self._n].reshape(whole, self._n)
        # Clipping is counted per FFT, so each frame has its own: a dongle's block is a quarter
        # of a second, and the frames before a key-up in it didn't clip.
        parts = (
            rows.view(np.float32)
            if rows.dtype == np.complex64
            else np.concatenate([rows.real, rows.imag], axis=1)
        )
        at_limits = np.abs(parts) > CLIP_LEVEL
        total = np.count_nonzero(at_limits)
        self.clipped = float(total) / (2 * whole * self._n)
        clips = np.count_nonzero(at_limits, axis=1) if total else np.zeros(whole, dtype=np.int64)
        power = np.abs(sp_fft.fft(rows * self._window, axis=1, workers=1)) ** 2
        k = 0
        while k < whole:
            take = min(whole - k, self._per_frame - self._acc_count)
            self._acc += power[k : k + take].sum(axis=0)
            self._acc_clips += int(clips[k : k + take].sum())
            self._acc_count += take
            k += take
            if self._acc_count == self._per_frame:
                frame = np.fft.fftshift(self._acc / (self._per_frame * self._norm))
                self._frame_clipped = self._acc_clips / (2 * self._frame_samples)
                end = self._frame_start + self._frame_samples
                out += self._frame(frame, self._frame_start, end)
                self._frame_start = end
                self._acc[:] = 0.0
                self._acc_count = self._acc_clips = 0
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
        floor_signal = self._weights @ floor
        self.snr_db = 10 * np.log10((signal + 1e-20) / (floor_signal + 1e-20))
        self.power_db = 10 * np.log10(signal + 1e-20)
        mirror_db = 10 * np.log10((self._mirror @ power + 1e-20) / (self._mirror @ floor + 1e-20))
        above = (self.snr_db >= self.threshold_db) & self._measured
        holding = self.snr_db >= self.threshold_db - STAY_DB
        states = [c.state for c in self._channels]
        rising_now = sum(1 for i in np.flatnonzero(above) if states[i] != "busy")
        risen = rising_now >= max(4, BROADBAND * len(self.freqs))
        # The channel on top of an overload: it may start, and nothing else.
        clipping = self._frame_clipped >= CLIPPING
        if clipping:
            self._clipped_at = end
        elif self._clip_learnt and end - self._clipped_at >= self._clear:
            self._clip_learnt = False
        overloading = risen or clipping
        top = self._on_top(above) if overloading else None
        where, off_hz = (
            self._strongest(power, floor, signal - floor_signal, top)
            if overloading
            else (None, None)
        )
        off = where == "off"
        several = False  # the converter clipping, and no channel standing over the rest
        if clipping and top is None and not off and not self._clip_learnt:
            if where == "on":
                # Its strongest signal on a channel, standing over nothing: several close by
                # at once, or one not yet clear of its copies. Overloaded; nothing is fixed
                # yet, so that channel starts if it comes to stand over the rest.
                several = True
            else:
                # Overloaded all the same, by a signal on none of the channels that isn't
                # clear of the rest, or beyond the window, where it isn't seen.
                off = True
        self._off_hz = off_hz if off_hz is not None else self._off_hz
        overloaded = off or several or top is not None  # a transmission close by, this frame
        culprit = self._culprit
        if off or several:
            top = None  # the channel on top is one of its products
            self._off_frames += 1
            if culprit is None and self._off_frames >= START_FRAMES:
                if off:
                    culprit = self._culprit = OFF_CHANNELS
                    self.overload_hz, self.overload_on_channel = self._off_hz, False
                elif not self._overload_seen:
                    self.overload_hz = self.overload_on_channel = None  # not known where
                self._overload_seen = True
        else:
            self._off_frames, self._off_hz = 0, None
        if culprit is not None and top != culprit:
            top = None  # one transmitter per overload: no other channel starts while it lasts
        if overloaded or risen:
            self._interfered = end
            self._deaf_since = start if self._deaf_since is None else self._deaf_since
            self.deaf_reason = "overload" if self._overload_seen else "interference"
        if (self._overload_seen and overloaded) or self._clip_learnt:
            self.overloaded_at = end  # sure of it: no passing peak
        if culprit is not None and culprit != OFF_CHANNELS and overloaded:
            # Caught on its channel: the rise is its own, and ends with it.
            self._blind_since = None
        elif risen or overloaded:
            # Interference, or an overload that nothing is caught from: this long without a
            # break, it is learnt as the floor, so a signal that stays (and clips for good)
            # doesn't leave the watch deaf.
            if self._blind_since is None:
                self._blind_since = start
            elif end - self._blind_since >= self._blind:
                return self._relearn(clipping)
        elif self._deaf_since is not None and end - self._interfered >= self._clear:
            self._hearing()
        deaf = self._deaf_since is not None

        out: list[Activity] = []
        for i in np.flatnonzero(above | np.array([s != "quiet" for s in states])):
            c = self._channels[i]
            if c.state == "busy":
                c.overload |= overloaded
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
            if not above[i] or (deaf and i != top):
                c.state, c.frames, c.overload = "quiet", 0, False
                continue
            if c.state == "quiet":
                c.state, c.frames, c.first = "rising", 0, start
                c.snr_db, c.power_db = -200.0, -200.0
            c.overload |= overloaded
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
            if overloaded and self._culprit is None:
                self._culprit, self._overload_seen = int(i), True
                self.overload_hz, self.overload_on_channel = self.freqs[i], True
            out.append(
                Activity(
                    "start",
                    int(i),
                    self.freqs[i],
                    c.first,
                    snr_db=c.snr_db,
                    power_db=c.power_db,
                    overload=c.overload,
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

    def _relearn(self, clipping: bool = False) -> list[Activity]:
        """The floors learnt afresh from the next frames, as at the start: the noise rose and
        stayed. Nothing rising goes on; what is being caught ends by itself if it was noise.
        Learnt while the converter clips (``clipping``), the overload is still noted while it
        does, and no longer keeps the watch deaf."""
        self._hearing()
        self._clip_learnt = clipping
        self.relearnt += 1
        self._floor = None
        for c in self._channels:
            if c.state == "rising":
                c.state, c.frames = "quiet", 0
        return []

    def _hearing(self) -> None:
        """Deaf no more: the interference or the overload is over."""
        self._deaf_since = self._blind_since = self.deaf_reason = None
        self._culprit, self._off_frames, self._off_hz = None, 0, None
        self._overload_seen = False

    def _strongest(
        self, power: np.ndarray, floor: np.ndarray, excess: np.ndarray, top: int | None
    ) -> tuple[str | None, int | None]:
        """Where the strongest signal in the whole window is while the receiver is overloaded:
        "on" a channel, "off" the channels (a transmission close by elsewhere, whose products
        are all the channels hear), or None when it is off them but not clear of the rest.
        Measured over its floor, in the bins standing the busy threshold over theirs (so the
        dongle's own spike at the centre, and a signal learnt as the floor, are no signal) and
        over a channel's width, like a channel. Off the channels, it must stand
        ``OFF_CHANNEL_DB`` over every other signal in the window beyond its reach (its splatter,
        and its copies as in ``_copies``), the channel on top (``top``) included: one transmitter,
        not a comb of lines. With no channel on top beyond its reach, it must also stand
        ``OVERLOAD_DB`` over every channel beyond its reach, as a channel on top does. Also
        where it is off them, to the kHz (its power-weighted middle), or None at the window's
        edges. ``excess`` is each channel's power over its floor."""
        over = np.maximum(power - floor * self._busy, 0.0)  # bins a signal is in, as a channel
        width = 2 * self._half_bins + 1
        sums = np.cumsum(np.concatenate([[0.0], over]))
        windows = sums[width:] - sums[:-width]  # over a channel's width, about each bin
        p = int(np.argmax(windows))
        total = float(windows[p])
        if total <= 0:
            return None, None
        near_peak = over[p : p + width]
        offset = float(near_peak @ self._offsets[p : p + width]) / total
        if np.abs(self._channel_offsets - offset).min() <= self._on_hz:
            return "on", None  # whether it stands over the rest is _on_top's
        copies = [(k * offset + self.rate / 2) % self.rate - self.rate / 2 for k in (1, -1, -3)]
        centres = self._offsets[self._half_bins : self._half_bins + len(windows)]
        mine = np.zeros(len(windows), dtype=bool)
        near = np.zeros(len(self.freqs), dtype=bool)
        for at in copies:
            mine |= np.abs(centres - at) <= self._reach
            near |= np.abs(self._channel_offsets - at) <= self._reach
        others = float(windows[~mine].max()) if (~mine).any() else 0.0
        if 10 * np.log10(total / max(others, 1e-20)) < OFF_CHANNEL_DB:
            return None, None
        rest = self._measured & ~near
        if not rest.any():
            rest = self._measured
        if not rest.any():
            return None, None
        if (top is None or not rest[top]) and (
            10 * np.log10(total / max(float(excess[rest].max()), 1e-20)) < OVERLOAD_DB
        ):
            return None, None
        if abs(offset) > self._usable_hz:
            return "off", None
        return "off", round((self.center_hz + offset) / 1e3) * 1_000

    def _on_top(self, above: np.ndarray) -> int | None:
        """The channel a transmission close by overloads the receiver from: the strongest, when
        it stands ``OVERLOAD_DB`` over every channel beyond its own splatter (three channels
        either side, and those overlapping it) and its copies (``_copies``), wherever they are
        in the window. None when no channel does: interference, or traffic on several channels."""
        snr = np.where(self._measured, self.snr_db, -np.inf)
        i = int(np.argmax(snr))
        if not above[i]:
            return None
        rest = self._measured & ~self._overlapping[i]
        rest[i] = False
        if (rest & ~self._nearby[i] & ~self._copies[i]).any():  # a window wider than those
            rest &= ~self._nearby[i] & ~self._copies[i]
        if not rest.any():
            return None
        return i if snr[i] - snr[rest].max() >= OVERLOAD_DB else None

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
        out = Activity(
            kind, i, self.freqs[i], c.first, c.last, c.snr_db, c.power_db, reason, c.overload
        )
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
