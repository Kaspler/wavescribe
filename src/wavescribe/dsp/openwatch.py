# SPDX-License-Identifier: Apache-2.0
"""Noticing a transmission anywhere in a range of what a dongle hears, not only on the
channels of a watch (``dsp/activity.py`` runs this beside its channels, on the same frames).

    the frame's power, bin by bin, against a noise floor per bin
      ─► over a channel's measured width about every bin at once (two running sums)
      ─► runs of bins over the threshold that no channel and no track has: candidates, each
         placed at the power-weighted middle of what stands over the floor there
      ─► a candidate that lasts two frames and moves is a transmission: a track, from its
         start to a hang time after it has gone
      ─► what is too wide for a narrowband transmission is noted, and never a track

Everything a receiver makes by itself is a line: one frequency, no modulation. A voice moves
an FM carrier, so its strongest bin is somewhere else from one FFT to the next (there are
about thirty in a frame); a spur, a clock harmonic or a silent carrier stays put. A candidate
that does not move is pending: never a transmission, and after ``LINE_S`` a known line, left
out of the measurement, followed as it drifts and forgotten once it has been gone for
``LINE_GONE_S``. Lines that are there when the floors are learnt are known from the first
frame. Two lines close together are no voice either: the strongest bin jumps between two
places without passing through the middle, or the power about them beats from one FFT to the
next.

A known line is still listened to. One that was still and starts to move as a voice does was
a carrier all along (a key pressed before the first word, a repeater idling) or has a
transmission on top of it now: a track from that moment. It is a line again once it has been
still for ``LINE_S``. A line that was there when the floors were learnt needn't have been
still first: one that moves as a voice does from the start is a transmission that was already
on the air when the window landed.

Where a carrier is the signal (``OpenBand.carrier``: an AM band), nothing has to move, and no
line is kept: a candidate that lasts two frames is a transmission, as on a channel.

A wide signal (``dsp/widesignal.py`` tells which) is held from its first bin to its last
while it lasts: nothing starts inside it, and what had started there before it showed itself
is taken back (``absorbed``). It is reported once it has been there half a second (``noted``)
and once more when it has gone (``gone``).

The channels of the watch are not touched by any of this. Within a quarter of a channel's
width of a channel's centre only the channel's own rule applies, and what a channel is
measuring is the channel's. This side has floors of its own, so nothing it holds or learns
changes what a channel reads.

Times are sample indices, as in ``dsp/activity.py``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy.ndimage import percentile_filter

from wavescribe.dsp import NARROW_MARGIN_HZ, widesignal
from wavescribe.dsp.activity import (
    BLIND_S,
    BROADBAND,
    CLEAR_S,
    FLOOR_DOWN_S,
    FLOOR_UP_S,
    GUARD_HZ,
    INIT_S,
    OVERLOAD_DB,
    SHADOW_CHANNELS,
    SHADOW_DB,
    START_FRAMES,
    STAY_DB,
    Activity,
    estimate,
    runs,
)
from wavescribe.dsp.widesignal import NARROW_HZ, RANGED_OF, WIDE_HZ, Wide, ranging

# How far a carrier's strongest bin must move between a frame's FFTs to be spoken into: half
# the spread between the 16th and 84th percentiles of its place (interpolated between bins).
# Measured on synthetic IQ at 586 Hz a bin: a line 0 to 90 Hz at 5 dB and up, drifting
# 500 Hz/s 90 Hz, wobbling ±100 Hz 110 Hz, ±300 Hz at 100 Hz 210 Hz; a carrier with only a
# sub-audible tone 260 Hz; speech at 2.5 kHz deviation 200 to 230 Hz in the middle frame and
# over 290 Hz in a third of them.
WANDER_HZ = 293.0
WANDER_REACH_HZ = 4_700.0  # either side of it: where its strongest bin is looked for
WANDER_NEAR_HZ = 2_350.0  # and again this close, where another signal beside it is not
ENVELOPE = 0.25  # the power about it may step this much (of its mean) between FFTs, no more
HELD = 0.35  # of the power about it: what no FFT of the frame finds gone from its bin
WANDER_MIDDLE = 0.15  # of a frame's FFTs between the extremes (a sine has 0.29, noise 0.38)
PEAK_BINS = 2  # the strongest bin's place is the middle of the power this many bins about it
MOVED_OF = (2, 4)  # moving in two of the last four frames: no line, and no click
LINE_S = 5.0  # a carrier this long that never moved: a known line
LINE_GONE_S = 30.0  # a known line not seen this long is forgotten
LINE_GUARD_HZ = 1_760.0  # left out either side of a known line
LINE_OVER = 4.0  # a known line is there while its bin stands this far over the floor about it
MAX_LINES = 64
MAX_TRACKS = 128  # candidates and transmissions at once; more are not looked at
# A known line is listened to for a voice only after it has been still: a second of frames,
# net of those in which it was neither still nor moving as a voice does (two lines close
# together are like that all the time).
CALM_S = 1.0
CALM_MOST_S = 5.0  # the most stillness it is credited with
STILL = 0.5  # of ``WANDER_HZ``: under this, with steady power, it is still
# A known line that moves is a voice only while the middle of where it moves stays where the
# line rests: within this much (Hz), and this share of how far it moves more.
REST_HZ = 150.0
REST_OF = 0.3
REST_FOLLOWS = 0.2  # how fast the resting place follows a line that drifts, a frame
APERTURE_HZ = 25_000.0  # either side of a candidate: where its centre and width are measured
CLOCK_PPM = 5.0  # how far off its nominal frequency another receiver's clock may be
# A click lifts the whole window at once, unevenly from bin to bin: a frame in which half the
# window stands this far over its floors says nothing of the shape or the width of a signal.
RISEN = 1.25


@dataclass(frozen=True)
class OpenBand:
    """A part of an open range with rules of its own. ``carrier``: a bare carrier is the
    signal there (an AM band). ``narrow_hz``: the most a narrowband transmission occupies
    there, and twice as far as its carrier goes from its centre; what is wider is a wide
    signal."""

    lo_hz: float
    hi_hz: float
    carrier: bool = False
    narrow_hz: float = NARROW_HZ


@dataclass(eq=False)
class _Track:
    id: int
    pos: float  # where it is, in bins
    anchor: int  # the bin its profile is centred on
    profile: np.ndarray = field(repr=False)  # its power over the floor, summed over frames
    state: str = "rising"  # rising, pending, busy
    first: int = 0
    last: int = 0
    frames: int = 0
    snr_db: float = -200.0
    power_db: float = -200.0
    overload: bool = False
    moved: deque = field(default_factory=lambda: deque(maxlen=MOVED_OF[1]))
    calm: int = 0  # frames it was still, while pending
    freq_hz: int = 0  # fixed at its start
    line: bool = False  # it was a known line: a line again once it stops moving
    spoke: int = 0  # the end of the last frame it moved in
    # this frame: how far it moved, whether its power was steady, the middle of its places,
    # and whether its carrier ranged as only a wide signal's does
    move: tuple[float, bool, float, bool] = (0.0, False, 0.0, False)
    ranged: deque = field(default_factory=lambda: deque(maxlen=RANGED_OF[1]))


@dataclass(eq=False)
class _Line:
    pos: int  # its bin
    last: int  # the end of the last frame it was seen in
    calm: int = 0  # frames it has been still, net
    moved: deque = field(default_factory=lambda: deque(maxlen=MOVED_OF[1]))
    since: int = 0  # the start of the first frame of those it moved in
    fine: float | None = None  # where it is between bins, from the frames it was still in
    rest: float | None = None  # where the middle of its FFTs' strongest bins is when still
    # this frame, as a track's; None: not listened to
    move: tuple[float, bool, float, bool] | None = None
    ranged: deque = field(default_factory=lambda: deque(maxlen=RANGED_OF[1]))
    landed: bool = False  # there when the floors were learnt: maybe a voice already talking


class OpenWatch:
    """The part of ``ActivityDetector`` that watches ``open_range`` (Hz) between and beyond
    the channels. ``frame`` takes each frame's power and its FFTs and gives what started and
    ended; ``ids`` numbers the tracks."""

    def __init__(
        self,
        rate: int,
        center_hz: int,
        fft: int,
        open_range: tuple[float, float],
        *,
        channels: Sequence[int],
        channel_hz: float,
        half_bins: int,
        threshold_db: float,
        excluded: np.ndarray,
        frame_samples: int,
        hang: int,
        least: int,
        carrier: int,
        ids: Callable[[], int],
        norm: float = 1.0,
        bands: Sequence[OpenBand] = (),
        clocks: Sequence[float] = (),
        step_hz: float | None = None,
    ) -> None:
        self.rate, self.center_hz, self._n = rate, center_hz, fft
        self._ids, self._norm = ids, norm
        self._frame_samples = frame_samples
        frame_s = frame_samples / rate
        self._hang, self._min, self._carrier = hang, least, carrier
        self._line, self._line_gone = round(LINE_S * rate), round(LINE_GONE_S * rate)
        self._init_frames = max(3, round(INIT_S / frame_s))
        self._clear, self._blind = round(CLEAR_S * rate), round(BLIND_S * rate)
        self._down = 1.0 - np.exp(-frame_s / FLOOR_DOWN_S)
        self._up = 1.0 - np.exp(-frame_s / FLOOR_UP_S)
        self._calm, self._calm_most = round(CALM_S / frame_s), round(CALM_MOST_S / frame_s)
        self._busy = 10 ** (threshold_db / 10)
        self._hold = 10 ** ((threshold_db - STAY_DB) / 10)

        self.bin_hz = bin_hz = rate / fft
        offsets = (np.arange(fft) - fft // 2) * bin_hz
        self._offsets = offsets
        self._h = half_bins
        self._half = max(2, round(channel_hz / 2 / bin_hz))  # a signal's half, in bins
        self._ap = round(APERTURE_HZ / bin_hz)
        self._guard = max(1, round(LINE_GUARD_HZ / bin_hz))
        self._wander, self._rest = WANDER_HZ / bin_hz, REST_HZ / bin_hz
        self._look = max(2, round(WANDER_REACH_HZ / bin_hz))
        self._look_near = min(self._look, max(2, round(WANDER_NEAR_HZ / bin_hz)))
        self._reach = round(
            SHADOW_CHANNELS * max(float(step_hz or channel_hz), channel_hz) / bin_hz
        )
        self._usable = np.abs(offsets) <= rate / 2 - NARROW_MARGIN_HZ
        lo_hz, hi_hz = open_range
        at = offsets + center_hz
        self._in_range = (at >= lo_hz) & (at <= hi_hz) & self._usable
        self._static = excluded | _clock_lines(at, clocks)
        self._mask = self._static.copy()
        self._line_at = np.full(fft, -1, dtype=np.intp)  # the known line each bin is left to
        # Within a quarter of a channel's width of a channel only the channel's rule applies.
        self._ch_bins = np.array(
            [round((c - center_hz) / bin_hz) + fft // 2 for c in channels], dtype=np.intp
        )
        self._ch_ok = (self._ch_bins >= 0) & (self._ch_bins < fft)
        self._channel_zone = np.zeros(fft, dtype=bool)
        for c in channels:
            self._channel_zone |= np.abs(offsets - (c - center_hz)) <= channel_hz / 4
        self._carrier_ok = np.zeros(fft, dtype=bool)  # where a bare carrier is the signal
        self._narrow = np.full(fft, NARROW_HZ)  # the most a narrowband transmission occupies
        for band in bands:
            inside = (at >= band.lo_hz) & (at <= band.hi_hz)
            self._carrier_ok[inside] = band.carrier
            self._narrow[inside] = band.narrow_hz
        self._bound = np.maximum(1, np.round(self._narrow / 2 / bin_hz)).astype(np.intp)
        self._wide_bins = round(WIDE_HZ / bin_hz)
        self._said = max(1, round(widesignal.SAID_S / frame_s))
        self._wide_gone = round(widesignal.GONE_S * rate)

        self._floor: np.ndarray | None = None
        self._first_frames: list[np.ndarray] = []
        self._rows: list[np.ndarray] = []
        self._tracks: list[_Track] = []
        self._lines: list[_Line] = []
        self._wides: list[Wide] = []
        self._deaf_since: int | None = None
        self._blind_since: int | None = None
        self._interfered = 0
        self._frame_start = 0
        self._rise = 1.0
        self._culprit: float | None = None  # the bin of the overload's transmitter
        self._top_frames = 0
        self.deaf_reason: str | None = None  # while deaf: "overload" or "interference"
        self.relearnt = 0  # times the floors were learnt afresh after the noise rose
        self.lines_seen = 0  # carriers that came and never moved

    @property
    def steady(self) -> int:
        """How many known lines are being left out."""
        return len(self._lines)

    @property
    def noted(self) -> list[tuple[int, float]]:
        """The wide signals there now: each one's middle (Hz) and how wide it is (Hz)."""
        return [(w.freq_hz, w.width_hz) for w in self._wides if w.said]

    @property
    def deaf_s(self) -> float | None:
        """How long interference or an overload across the window has kept anything from
        starting off the channels, or None."""
        if self._deaf_since is None:
            return None
        return (self._frame_start - self._deaf_since) / self.rate

    # -- one frame ----------------------------------------------------------------------------

    def frame(
        self,
        power: np.ndarray,
        rows: list[np.ndarray],
        start: int,
        end: int,
        clipping: bool,
        live: np.ndarray,
        busy: np.ndarray,
        snr_db: np.ndarray,
    ) -> list[Activity]:
        """What started and ended off the channels in the frame from ``start`` to ``end``.
        ``power`` is the frame, bin by bin; ``rows`` its FFTs, as the FFT gives them;
        ``live`` and ``busy`` say which channels are measuring a signal and which are in a
        transmission, and ``snr_db`` how far each stands over its floor."""
        self._frame_start = end
        if self._floor is None:
            self._first_frames.append(power)
            if len(self._first_frames) >= self._init_frames:
                self._floor = np.median(np.array(self._first_frames), axis=0) + 1e-20
                self._first_frames = []
                self._first_lines(start, end)
            return []
        floor, self._rows = self._floor, rows
        measuring = self._ch_bins[live & self._ch_ok]
        taken = self._on_their_channel(measuring, power, floor)
        self._moves()
        out = self._listen_to_lines(power, floor, taken, start, end)
        ratio = self._sliding(power, floor)
        hot = ratio >= self._busy
        self._rise = self._risen(ratio)
        excess = np.where(self._mask, 0.0, power - floor)
        out += self._wide_signals(power, floor, hot, end)
        sig, claimed = self._claims(taken)
        deaf, top, overloading = self._hearing(ratio, hot, claimed, clipping, busy, start, end)
        if self._floor is None:  # the noise rose and stayed: learnt afresh
            return out
        rivals = [
            (float(b), float(s)) for b, s in zip(self._ch_bins[busy], snr_db[busy], strict=True)
        ]
        for t in list(self._tracks):
            out += self._update(t, power, floor, excess, ratio, taken, rivals, start, end, deaf,
                                top, overloading)  # fmt: skip
        if deaf and top is not None:
            self._overloading_track(top, taken, excess, ratio, power, start, end)
        elif not deaf:
            sig, claimed = self._claims(taken)
            for a, b in runs(hot & self._in_range & ~claimed):
                if b - a + 1 - 2 * self._h >= self._wide_bins:
                    if self._rise < RISEN:
                        self._wide_at(a + self._h, b - self._h, start, end)
                    continue
                pos = self._candidate(a, b, excess, sig)
                if pos is not None:
                    self._new(pos, excess, ratio, power, start, end, overloading)
            self._learn(power, floor, measuring)
        self._rows = []
        return out

    def _risen(self, ratio: np.ndarray) -> float:
        """How far the window as a whole stands over its floors in this frame, known lines
        and wide signals left out: the middle one of every fourth place."""
        free = self._usable & ~self._mask
        for w in self._wides:
            free[w.lo : w.hi + 1] = False
        some = ratio[free][::4]
        return max(1.0, float(np.median(some))) if len(some) else 1.0

    def _sliding(self, power: np.ndarray, floor: np.ndarray) -> np.ndarray:
        """The power over a channel's measured width about every bin, over the floors of the
        same bins: what a channel centred there would read. Known lines count for nothing."""
        keep = ~self._mask
        power_sums = np.concatenate([[0.0], np.cumsum(np.where(keep, power, 0.0))])
        floor_sums = np.concatenate([[0.0], np.cumsum(np.where(keep, floor, 0.0))])
        at = np.arange(self._n)
        first, last = np.maximum(at - self._h, 0), np.minimum(at + self._h + 1, self._n)
        den = floor_sums[last] - floor_sums[first]
        num = power_sums[last] - power_sums[first]
        return np.where(den > 0, (num + 1e-20) / (den + 1e-20), 1.0)

    def _on_their_channel(
        self, measuring: np.ndarray, power: np.ndarray, floor: np.ndarray
    ) -> np.ndarray:
        """Those of the channels measuring a signal (their bins) whose signal is theirs: the
        middle of what stands over the floor in the channel is within its measured width. A
        channel that only hears the edge of a signal beside it has no claim on that signal."""
        mine = []
        for c in measuring.tolist():
            lo = max(0, c - self._half)
            over = np.maximum(power[lo : c + self._half + 1] - floor[lo : c + self._half + 1], 0.0)
            middle = lo + float((np.arange(len(over)) * over).sum() / max(over.sum(), 1e-30))
            if abs(middle - c) <= self._h:
                mine.append(c)
        return np.array(mine, dtype=np.intp)

    def _claims(self, taken: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The bins a track's, a wide signal's or a measuring channel's signal is in, and
        those where the sliding measurement is of that signal: no candidate is taken from
        them."""
        sig = np.zeros(self._n, dtype=bool)
        claimed = np.zeros(self._n, dtype=bool)
        reach = self._h + 2
        centres = [round(t.pos) for t in self._tracks] + taken.tolist()
        held = [(c - self._half, c + self._half) for c in centres]
        for lo, hi in held + [(w.lo, w.hi) for w in self._wides]:
            sig[max(0, lo) : hi + 1] = True
            claimed[max(0, lo - reach) : hi + reach + 1] = True
        return sig, claimed

    def _learn(self, power: np.ndarray, floor: np.ndarray, taken: np.ndarray) -> None:
        """The floors follow the bins nothing is in: a transmission must not lift its own
        floor, and a known line's stays what it was before the line."""
        hold = np.zeros(self._n, dtype=bool)
        for c in [round(t.pos) for t in self._tracks] + taken.tolist():
            hold[max(0, c - self._half - 2) : c + self._half + 3] = True
        for w in self._wides:
            hold[max(0, w.lo - 2) : w.hi + 3] = True
        for ln in self._lines:
            hold[max(0, ln.pos - self._guard - 2) : ln.pos + self._guard + 3] = True
        learn = ~hold
        rate = np.where(power < floor, self._down, self._up)
        floor[learn] += rate[learn] * (power[learn] - floor[learn])
        np.maximum(floor, 1e-20, out=floor)

    # -- interference and overload, over bins ---------------------------------------------------

    def _hearing(
        self,
        ratio: np.ndarray,
        hot: np.ndarray,
        claimed: np.ndarray,
        clipping: bool,
        busy: np.ndarray,
        start: int,
        end: int,
    ) -> tuple[bool, float | None, bool]:
        """Whether this side is deaf in this frame (half of what the dongle hears rose at
        once, or its converter clips), the bin of the one signal that stands over everything
        else if there is one (a transmitter close by: it may start, and nothing else), and
        whether the receiver is overloaded or interfered with at all."""
        free = self._usable & ~claimed & ~self._mask
        risen = np.count_nonzero(hot & free) >= BROADBAND * max(1, np.count_nonzero(free))
        overloading = bool(risen or clipping)
        top = self._on_top(ratio) if overloading else None
        if overloading:
            if top is not None:
                self._top_frames += 1
                if self._culprit is None and self._top_frames >= START_FRAMES:
                    self._culprit = top
            else:
                self._top_frames = 0
            if self._culprit is not None and (top is None or abs(top - self._culprit) > self._h):
                top = None  # one transmitter per overload
            self._interfered = end
            self._deaf_since = start if self._deaf_since is None else self._deaf_since
            sure = self._culprit is not None or clipping
            self.deaf_reason = "overload" if sure else "interference"
            near = 2 * self._h
            caught = self._culprit is not None and (
                any(t.state == "busy" and abs(t.pos - self._culprit) <= near for t in self._tracks)
                or bool((np.abs(self._ch_bins[busy] - self._culprit) <= near).any())
            )
            if caught:
                self._blind_since = None  # the rise is its own, and ends with it
            elif self._blind_since is None:
                self._blind_since = start
            elif end - self._blind_since >= self._blind:
                self._relearn()
        elif self._deaf_since is not None and end - self._interfered >= self._clear:
            self._hear_again()
        return self._deaf_since is not None, top, overloading

    def _on_top(self, ratio: np.ndarray) -> float | None:
        """The bin of the strongest signal in the window, when it stands ``OVERLOAD_DB`` over
        everything beyond its own reach and its copies (its mirror about the centre, and three
        times its offset the other way) and is in the range: the transmitter close by."""
        s = np.where(self._usable & ~self._mask, ratio, 0.0)
        p = int(np.argmax(s))
        if s[p] < self._busy:
            return None
        mine = np.zeros(self._n, dtype=bool)
        for k in (1, -1, -3):
            at = (k * self._offsets[p] + self.rate / 2) % self.rate - self.rate / 2
            mine |= np.abs(self._offsets - at) <= self._reach * self.bin_hz
        rest = self._usable & ~mine & ~self._mask
        if not rest.any():
            return None
        if s[p] < float(s[rest].max()) * 10 ** (OVERLOAD_DB / 10):
            return None
        return float(p) if self._in_range[p] else None

    def _relearn(self) -> None:
        self._hear_again()
        self.relearnt += 1
        self._floor = None
        self._tracks = [t for t in self._tracks if t.state == "busy"]

    def _hear_again(self) -> None:
        self._deaf_since = self._blind_since = self.deaf_reason = None
        self._culprit, self._top_frames = None, 0

    # -- candidates and tracks ------------------------------------------------------------------

    def _candidate(self, a: int, b: int, excess: np.ndarray, sig: np.ndarray) -> float | None:
        """Where the signal in a run of bins over the threshold is: the power-weighted middle
        of what stands over the floor about the strongest of it (of two side by side, the
        stronger). None within a quarter of a channel's width of a channel: that is the
        channel's."""
        lo, hi = max(0, a - self._h), min(self._n - 1, b + self._h)
        e = np.where(sig[lo : hi + 1], 0.0, np.maximum(excess[lo : hi + 1], 0.0))
        if float(e.sum()) <= 0:
            return None
        pos, _ = estimate(e, self.bin_hz, lo)
        return None if self._channel_zone[round(pos)] else pos

    def _new(
        self,
        pos: float,
        excess: np.ndarray,
        ratio: np.ndarray,
        power: np.ndarray,
        start: int,
        end: int,
        overloading: bool,
    ) -> _Track | None:
        anchor = round(pos)
        if anchor - self._ap < 0 or anchor + self._ap >= self._n:
            return None
        if len(self._tracks) >= MAX_TRACKS or any(
            abs(t.pos - pos) <= self._h for t in self._tracks
        ):
            return None
        t = _Track(self._ids(), pos, anchor, np.zeros(2 * self._ap + 1), first=start, last=end)
        t.overload, t.frames = overloading, 1
        self._tracks.append(t)
        self._accumulate(t, excess, ratio, power)
        return t

    def _overloading_track(
        self,
        top: float,
        taken: np.ndarray,
        excess: np.ndarray,
        ratio: np.ndarray,
        power: np.ndarray,
        start: int,
        end: int,
    ) -> None:
        """The whole window is over its floors, so there are no runs to read: the transmitter
        close by is where the strongest stands over everything else. On a channel it is the
        channel's; off them it is a track at that frequency."""
        at = round(top)
        if self._channel_zone[at] or (len(taken) and np.abs(taken - top).min() <= self._half):
            return
        self._new(top, excess, ratio, power, start, end, True)

    def _accumulate(
        self, t: _Track, excess: np.ndarray, ratio: np.ndarray, power: np.ndarray
    ) -> None:
        first = t.anchor - self._ap
        mine = excess[first : t.anchor + self._ap + 1].copy()
        for o in self._tracks:  # what stands there is another's, not part of this one
            if o is not t:
                a, b = round(o.pos) - self._half - first, round(o.pos) + self._half - first
                if b >= 0 and a < len(mine):
                    mine[max(0, a) : b + 1] = 0.0
        if self._rise < RISEN:
            t.profile += mine
        c = round(t.pos)
        t.snr_db = max(t.snr_db, self._here_db(ratio, c))
        mine_now = float(power[max(0, c - self._h) : c + self._h + 1].sum())
        t.power_db = max(t.power_db, 10 * np.log10(mine_now + 1e-20))

    @staticmethod
    def _here_db(ratio: np.ndarray, c: int) -> float:
        return 10 * float(np.log10(ratio[max(0, c - 2) : c + 3].max()))

    def _freq(self, pos: float) -> int:
        return round(self.center_hz + (pos - self._n // 2) * self.bin_hz)

    def _update(
        self,
        t: _Track,
        power: np.ndarray,
        floor: np.ndarray,
        excess: np.ndarray,
        ratio: np.ndarray,
        taken: np.ndarray,
        rivals: list[tuple[float, float]],
        start: int,
        end: int,
        deaf: bool,
        top: float | None,
        overloading: bool,
    ) -> list[Activity]:
        c = round(t.pos)
        here = float(ratio[max(0, c - 2) : c + 3].max())
        above, holding = here >= self._busy, here >= self._hold
        allowed = not deaf or (top is not None and abs(t.pos - top) <= 2 * self._h)
        wander, steady, _, ranged = t.move
        moved = wander >= self._wander
        if t.state != "busy" and len(taken) and np.abs(taken - t.pos).min() <= self._half:
            self._tracks.remove(t)  # a channel is measuring it: the channel's
            return []
        t.ranged.append(ranged)
        if sum(t.ranged) >= RANGED_OF[0] or self._too_wide(t, c):
            return self._widen(t, end)
        if t.state == "rising":
            if not above or not allowed:
                self._tracks.remove(t)
                return []
            t.frames += 1
            t.last = end
            t.overload |= overloading
            self._accumulate(t, excess, ratio, power)
            t.moved.append(moved)
            t.calm += steady and wander < STILL * self._wander
            if t.frames < START_FRAMES:
                return []
            if self._shadowed(t, ratio, rivals):
                t.frames, t.first = 0, end  # tried again next frame, from where it is then
                t.profile[:] = 0.0
                t.moved.clear()
                return []
            if (self._carrier_ok[c] or sum(t.moved) >= MOVED_OF[0]) and not any(t.ranged):
                return [self._start(t)]
            t.state = "pending"
            return []
        if t.state == "pending":
            return self._pending(t, c, excess, ratio, power, moved, steady and not moved
                                 and wander < STILL * self._wander, above, holding, allowed,
                                 overloading, end)  # fmt: skip
        t.overload |= overloading
        if moved:
            t.spoke = end
        if t.line and end - t.spoke >= self._line and holding:
            # A carrier again, as it was before it was spoken into: a known line.
            self._tracks.remove(t)
            t.last = min(t.spoke + self._hang, end)
            self._known(c, excess, end, self._calm_most)
            return [self._end(t)]
        if holding:
            t.last = end
            self._accumulate(t, excess, ratio, power)
            if end - t.first >= self._carrier:
                self._tracks.remove(t)
                if self._carrier_ok[c]:
                    lo, hi = max(0, c - self._half), c + self._half + 1
                    floor[lo:hi] = power[lo:hi]
                else:
                    self._known(c, excess, end, 0)
                return [self._end(t, "carrier")]
        elif end - t.last >= self._hang:
            self._tracks.remove(t)
            return [self._end(t)]
        return []

    def _pending(
        self,
        t: _Track,
        c: int,
        excess: np.ndarray,
        ratio: np.ndarray,
        power: np.ndarray,
        moved: bool,
        still: bool,
        above: bool,
        holding: bool,
        allowed: bool,
        overloading: bool,
        end: int,
    ) -> list[Activity]:
        """A carrier that has not moved yet: a transmission the moment it does, a line when
        it goes or has stayed ``LINE_S``."""
        if holding:
            t.last = end
            t.overload |= overloading
            self._accumulate(t, excess, ratio, power)
            t.moved.append(moved)
            t.calm = min(t.calm + 1, self._calm_most) if still else t.calm
        else:
            t.moved.append(False)
        ready = sum(t.moved) >= MOVED_OF[0] or bool(self._carrier_ok[c])
        if allowed and above and ready and not any(t.ranged):
            return [self._start(t)]
        if end - t.last >= self._hang:
            self._tracks.remove(t)
            self.lines_seen += 1
        elif end - t.first >= self._line and holding:
            self._tracks.remove(t)
            self.lines_seen += 1
            self._known(c, excess, end, t.calm)
        return []

    def _start(self, t: _Track) -> Activity:
        pos, width = estimate(t.profile, self.bin_hz, t.anchor - self._ap)
        t.pos, t.freq_hz = pos, self._freq(pos)
        t.state, t.spoke = "busy", t.last
        # A known line beside it that has just moved is this transmission's own carrier.
        mine = [ln for ln in self._lines if abs(ln.pos - pos) <= self._half and any(ln.moved)]
        if mine:
            self._lines = [ln for ln in self._lines if all(ln is not o for o in mine)]
            self._remask()
        return Activity(
            "start", None, t.freq_hz, t.first, None, t.snr_db, t.power_db, None, t.overload,
            t.id, width,
        )  # fmt: skip

    def _end(self, t: _Track, reason: str | None = None) -> Activity:
        kind = "short" if t.last - t.first < self._min and reason != "carrier" else "end"
        _, width = estimate(t.profile, self.bin_hz, t.anchor - self._ap)
        return Activity(
            kind, None, t.freq_hz, t.first, t.last, t.snr_db, t.power_db, reason, t.overload,
            t.id, width,
        )  # fmt: skip

    def _shadowed(self, t: _Track, ratio: np.ndarray, rivals: list[tuple[float, float]]) -> bool:
        """The shadow of a stronger signal: its splatter within three channels (on a track,
        or on a channel in a transmission), or the receiver's mirror of it about the centre."""
        c = round(t.pos)
        mine = self._here_db(ratio, c)
        m = self._n - c  # the mirror about the centre
        if 2 <= m < self._n - 2 and self._here_db(ratio, m) - mine >= SHADOW_DB:
            return True
        others = [(o.pos, o.snr_db) for o in self._tracks if o is not t and o.state != "rising"]
        return any(
            abs(pos - t.pos) <= self._reach and snr - mine >= SHADOW_DB
            for pos, snr in others + rivals
        )

    # -- moving, and known lines ------------------------------------------------------------------

    def _moves(self) -> None:
        """How far the strongest bin about each track and each known line moved between this
        frame's FFTs, and whether the power about it was steady: its ``move``."""
        tracks = list(self._tracks)
        lines = [
            ln for ln in self._lines if not self._channel_zone[ln.pos] and not self._in_wide(ln.pos)
        ]
        for ln in self._lines:
            ln.move = None
        if not tracks and not lines:
            return
        centres = np.array([round(t.pos) for t in tracks] + [ln.pos for ln in lines])
        own = np.array([-2] * len(tracks) + [ln.pos for ln in lines])
        moves = zip(tracks + lines, *self._moving(centres, own), strict=True)
        for one, wander, steady, middle, ranged in moves:
            one.move = (float(wander), bool(steady), float(middle), bool(ranged))

    def _moving(
        self, centres: np.ndarray, own: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """For each of ``centres`` (bins): how far its strongest bin moved between the
        frame's FFTs (``_spread``), whether the power about it was steady, where the middle
        of its places was (in bins from the centre), and whether its carrier ranged as a wide
        signal's does (``widesignal.ranging``). The bins left to a known line count for
        nothing, but for the line itself (``own``: its bin). The spread is the greater of
        two: over the whole reach, and close in, where a second signal beside it does not
        take the strongest bin away."""
        ap = self._ap
        at = np.clip(centres[:, None] + np.arange(-ap, ap + 1)[None, :], 0, self._n - 1)
        left_to = self._line_at[at]
        keep = ((left_to < 0) | (left_to == own[:, None])) & ~self._static[at]
        cols = (at - self._n // 2) % self._n
        rows = np.concatenate([part[:, cols] for part in self._rows]) * keep
        k = ap - self._look - PEAK_BINS
        far, steady, far_middle = _spread(rows[:, :, k : 2 * ap + 1 - k])
        k = ap - self._look_near - PEAK_BINS
        near, _, near_middle = _spread(rows[:, :, k : 2 * ap + 1 - k])
        middle = np.where(far >= near, far_middle, near_middle)
        assert self._floor is not None
        total = np.concatenate([part.sum(axis=1) for part in self._rows])
        usual = total <= widesignal.CLICK * np.median(total)
        ranged = ranging(rows, self._floor[at] * self._norm, self._bound[centres], usual)
        return np.maximum(far, near), steady, middle, ranged

    def _known(self, c: int, excess: np.ndarray, end: int, calm: int) -> None:
        e = excess[c - self._guard : c + self._guard + 1]
        at = c - self._guard + int(np.argmax(e))
        if len(self._lines) >= MAX_LINES or any(abs(ln.pos - at) <= 1 for ln in self._lines):
            return
        self._lines.append(_Line(at, end, calm))
        self._remask()

    def _remask(self) -> None:
        """What is left out of the measurement: the static mask and the bins about each
        known line, each of those left to the line nearest to it."""
        mask = self._static.copy()
        left_to = np.full(self._n, -1, dtype=np.intp)
        away = np.full(self._n, self._n, dtype=np.intp)
        for ln in self._lines:
            lo, hi = max(0, ln.pos - self._guard), min(self._n, ln.pos + self._guard + 1)
            mask[lo:hi] = True
            mine = np.abs(np.arange(lo, hi) - ln.pos)
            closer = mine < away[lo:hi]
            left_to[lo:hi][closer] = ln.pos
            away[lo:hi][closer] = mine[closer]
        self._mask, self._line_at = mask, left_to

    def _listen_to_lines(
        self,
        power: np.ndarray,
        floor: np.ndarray,
        taken: np.ndarray,
        start: int,
        end: int,
    ) -> list[Activity]:
        """Each known line where it is now (it drifts, a bin at a time), forgotten when it
        has been gone long enough, and a transmission from the moment one that was still
        starts to move."""
        out: list[Activity] = []
        changed = False
        for ln in list(self._lines):
            was = ln.pos
            lo = max(0, ln.pos - 1)
            near = power[lo : ln.pos + 2]
            j = int(np.argmax(near))
            around = _median(floor[max(0, ln.pos - 24) : ln.pos + 25])
            if near[j] < LINE_OVER * around:
                ln.moved.append(False)
                if end - ln.last >= self._line_gone:
                    self._lines.remove(ln)
                    changed = True
                continue
            ln.last = end
            if lo + j != ln.pos:
                ln.pos, changed = lo + j, True
            onto = [o for o in self._lines if o is not ln and o.pos == ln.pos]
            if onto:  # it has drifted onto another: one line, which has not been still yet
                self._lines.remove(ln)
                onto[0].calm, onto[0].rest = 0, None
                onto[0].moved.clear()
                continue
            move = ln.move
            if move is None or (len(taken) and np.abs(taken - ln.pos).min() <= self._half):
                continue  # a channel's or a wide signal's, or a channel is measuring over it
            wander, steady, middle, ranged = move
            ln.ranged.append(ranged)
            if sum(ln.ranged) >= RANGED_OF[0]:  # a wide signal's carrier, at rest until now
                self._lines.remove(ln)
                changed = True
                b = int(self._bound[ln.pos])
                self._wide_at(ln.pos - b, ln.pos + b, start, end)
                continue
            here = was + middle
            # A voice moves a carrier about where it rests. What moves about another place
            # is a second line that came up beside this one.
            about = ln.rest is not None and abs(here - ln.rest) <= self._rest + REST_OF * wander
            moved = wander >= self._wander and about
            if moved and not any(ln.moved):
                ln.since = start
            ln.moved.append(moved)
            if steady and wander < STILL * self._wander:
                ln.calm = min(ln.calm + 1, self._calm_most)
                ln.rest = here if ln.rest is None else ln.rest + REST_FOLLOWS * (here - ln.rest)
                ln.fine = ln.pos + _between_bins(power[ln.pos - 1 : ln.pos + 2])
            elif not moved:
                ln.calm = max(ln.calm - 1, 0)
            spoken = sum(ln.moved) >= MOVED_OF[0] and (ln.calm >= self._calm or ln.landed)
            if spoken and not any(ln.ranged) and self._deaf_since is None:
                self._lines.remove(ln)
                changed = True
                out += self._spoken(ln, power, around, end)
        if changed:
            self._remask()
        return out

    def _spoken(self, ln: _Line, power: np.ndarray, around: float, end: int) -> list[Activity]:
        """A known line that moves as a voice does: a transmission from where it began to,
        at the carrier's own frequency. What was rising beside it was its own voice; a
        transmission already running there has it."""
        near = [t for t in self._tracks if abs(t.pos - ln.pos) <= self._half]
        if any(t.state == "busy" for t in near):
            return []
        self._tracks = [t for t in self._tracks if all(t is not o for o in near)]
        pos = ln.fine if ln.fine is not None and abs(ln.fine - ln.pos) <= 1 else float(ln.pos)
        t = _Track(self._ids(), pos, ln.pos, np.zeros(2 * self._ap + 1), "busy",
                   ln.since, end, line=True, spoke=end)  # fmt: skip
        first = t.anchor - self._ap
        t.profile += np.maximum(power[first : t.anchor + self._ap + 1] - around, 0.0)
        t.freq_hz = self._freq(t.pos)
        lo, hi = max(0, ln.pos - self._h), ln.pos + self._h + 1
        t.snr_db = 10 * float(np.log10(power[lo:hi].sum() / max(around * (hi - lo), 1e-30)))
        t.power_db = 10 * float(np.log10(power[lo:hi].sum() + 1e-20))
        self._tracks.append(t)
        _, width = estimate(t.profile, self.bin_hz, first)
        return [Activity("start", None, t.freq_hz, t.first, None, t.snr_db, t.power_db,
                         track=t.id, width_hz=width)]  # fmt: skip

    def _first_lines(self, first: int, start: int) -> None:
        """What was there when the floors were learnt. Wide signals (a broadcast station) are
        held from the first frame. Lines (a spur, a carrier already keyed) are known lines
        from the first frame, with the floor under them put back to what is about them: not
        inside a wide signal, where a carrier is the signal, or where a channel has its own
        rule."""
        floor = self._floor
        assert floor is not None
        self._lines = []
        found = widesignal.humps(floor, self._in_range, self._h, self._wide_bins, self.bin_hz)
        for a, b, level in found:
            w = self._wide_at(a, b, first, start)
            w.base, w.frames = min(w.base, level), max(w.frames, self._said - 1)
        inside = np.zeros(self._n, dtype=bool)
        for w in self._wides:
            inside[max(0, w.lo - self._guard) : w.hi + self._guard + 1] = True
        around = percentile_filter(floor, 50, size=49, mode="nearest")
        peak = (floor[1:-1] >= 8 * around[1:-1]) & (floor[1:-1] >= floor[:-2])
        peak &= floor[1:-1] > floor[2:]
        ok = self._in_range & ~self._static & ~self._carrier_ok & ~self._channel_zone & ~inside
        peaks = np.flatnonzero(peak & ok[1:-1]) + 1
        for pk in sorted(peaks, key=lambda k: -floor[k])[:MAX_LINES]:
            if any(abs(ln.pos - pk) <= self._guard for ln in self._lines):
                continue
            self._lines.append(_Line(int(pk), start, rest=float(pk), landed=True))
            floor[max(0, pk - self._guard) : pk + self._guard + 1] = around[pk]
        self._remask()

    # -- wide signals ----------------------------------------------------------------------------

    def _too_wide(self, t: _Track, c: int) -> bool:
        """Whether what the track has put over the floor so far is wider than a narrowband
        transmission is there."""
        if t.frames < START_FRAMES:
            return False
        _, width = estimate(t.profile, self.bin_hz)
        return width > self._narrow[c]

    def _widen(self, t: _Track, end: int) -> list[Activity]:
        """The track is a wide signal's: held as one from here on, and taken back if it had
        been said to be a transmission."""
        self._tracks.remove(t)
        b = int(self._bound[round(t.pos)])
        self._wide_at(round(t.pos) - b, round(t.pos) + b, t.first, end)
        if t.state != "busy":
            return []
        return [self._activity("absorbed", t, end)]

    def _wide_at(self, lo: int, hi: int, first: int, end: int) -> Wide:
        """The wide signal over bins ``lo`` to ``hi``: one that is there already and reaches
        them, grown to hold them, or a new one."""
        lo, hi = max(0, lo), min(self._n - 1, hi)
        floor = self._floor
        assert floor is not None
        base = float(floor[lo : hi + 1].mean())
        for w in self._wides:
            if lo <= w.hi + self._ap and hi >= w.lo - self._ap:
                w.lo, w.hi, w.base = min(w.lo, lo), max(w.hi, hi), min(w.base, base)
                return w
        w = Wide(self._ids(), lo, hi, first, end, base)
        self._wides.append(w)
        return w

    def _wide_signals(
        self, power: np.ndarray, floor: np.ndarray, hot: np.ndarray, end: int
    ) -> list[Activity]:
        """Each wide signal as it is in this frame: grown as far as the bins about it stand
        over their floors, with what was taken for a narrowband signal inside it taken back,
        noted once it has lasted, and gone when it has not been there for a while."""
        out: list[Activity] = []
        risen = runs(hot) if self._wides else []
        for w in list(self._wides):
            if all(w is not o for o in self._wides):
                continue  # another took it in this frame
            level = self._level(power, w) / self._rise
            if level < widesignal.OVER * w.base:
                since = end - w.last
                if since >= (self._wide_gone if w.said else self._hang):
                    self._wides.remove(w)
                    if w.said:
                        out.append(self._wide_activity("gone", w, w.last))
                continue
            w.last, w.frames = end, w.frames + 1
            for a, b in risen if self._rise < RISEN else ():
                if a - self._h <= w.hi and b + self._h >= w.lo and b - a > 2 * self._h:
                    w.lo, w.hi = min(w.lo, a + self._h), max(w.hi, b - self._h)
            w.base = min(w.base, self._level(floor, w))
            w.snr_db = max(w.snr_db, 10 * float(np.log10(level / w.base)))
            w.power_db = max(w.power_db, 10 * float(np.log10(level * (w.hi - w.lo + 1) + 1e-20)))
            out += self._take_in(w, end)
            w.width_hz = max(w.width_hz, (w.hi - w.lo + 1) * self.bin_hz)
            if not w.said and w.frames >= self._said:
                w.said = True
                w.freq_hz = self._freq((w.lo + w.hi) / 2)
                out.append(self._wide_activity("noted", w, None))
        return out

    def _level(self, per_bin: np.ndarray, w: Wide) -> float:
        """The mean of ``per_bin`` over a wide signal's bins, known lines left out: a line
        inside it is no part of it, and is there when it is not."""
        keep = ~self._mask[w.lo : w.hi + 1]
        return float(per_bin[w.lo : w.hi + 1][keep].mean()) if keep.any() else 0.0

    def _in_wide(self, pos: float) -> bool:
        return any(w.lo <= pos <= w.hi for w in self._wides)

    def _take_in(self, w: Wide, end: int) -> list[Activity]:
        """What is inside a wide signal is the signal's: tracks (a station is a bare carrier
        until someone speaks), and a younger wide signal it has grown into."""
        out: list[Activity] = []
        for t in list(self._tracks):
            if w.lo - self._h <= t.pos <= w.hi + self._h:
                self._tracks.remove(t)
                if t.state == "busy":
                    out.append(self._activity("absorbed", t, end))
        for o in list(self._wides):
            if o is w or o.lo > w.hi + self._h or o.hi < w.lo - self._h:
                continue
            self._wides.remove(o)
            w.lo, w.hi = min(w.lo, o.lo), max(w.hi, o.hi)
            w.first, w.frames = min(w.first, o.first), max(w.frames, o.frames)
            if o.said and w.said:  # two that were noted are one from here on
                out.append(self._wide_activity("gone", o, end))
            elif o.said:
                w.id, w.said, w.freq_hz, w.width_hz = o.id, True, o.freq_hz, o.width_hz
        return out

    def _activity(self, kind: str, t: _Track, end: int) -> Activity:
        _, width = estimate(t.profile, self.bin_hz)
        return Activity(
            kind, None, t.freq_hz, t.first, end, t.snr_db, t.power_db, None, t.overload,
            t.id, width,
        )  # fmt: skip

    def _wide_activity(self, kind: str, w: Wide, end: int | None) -> Activity:
        return Activity(
            kind, None, w.freq_hz, w.first, end, w.snr_db, w.power_db, track=w.id,
            width_hz=w.width_hz,
        )  # fmt: skip

    def flush(self) -> list[Activity]:
        """End every transmission still running off the channels."""
        out = [self._end(t, "stopped") for t in self._tracks if t.state == "busy"]
        self._tracks = []
        return out


def _median(some: np.ndarray) -> float:
    """The median of a handful of numbers, each line's every frame: a sort and no more."""
    ordered = np.sort(some)
    n = len(ordered)
    if n % 2:
        return float(ordered[n // 2])
    return 0.5 * float(ordered[n // 2 - 1] + ordered[n // 2])


def _between_bins(three: np.ndarray) -> float:
    """How far from the middle of three bins a line's peak is, in bins (a parabola through
    their logarithms)."""
    if len(three) != 3:
        return 0.0
    a, b, c = np.log(three + 1e-30)
    return float(np.clip(0.5 * (a - c) / (a - 2 * b + c - 1e-30), -0.5, 0.5))


def _spread(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """From ``rows`` (FFTs by places by the bins about each place): half the spread between the
    16th and 84th percentiles of where the strongest bin was in each FFT, in bins, whether
    the power there was steady from FFT to FFT, and the middle of where the strongest bin
    was, in bins from the place itself. No spread counts where the power was
    not steady (two lines too close for an FFT to tell apart beat instead; FM has a steady
    envelope), nor where the strongest bin jumped between two places without passing through
    the middle (two lines a little further apart), nor where the power stayed in its bins
    (two lines two bins apart, as alike as to take turns at being the stronger, are both
    there in every FFT; a carrier that moves leaves each bin it was in)."""
    w = PEAK_BINS
    total = rows.sum(axis=2)
    most = ENVELOPE * total.mean(axis=0)
    steady = np.std(np.diff(total, axis=0), axis=0) <= np.sqrt(2) * most
    steady &= np.std(total, axis=0) <= most
    # Where the power about the strongest bin has its middle. A carrier that moves takes its
    # power with it; two lines close together keep theirs where it is, whichever of them an
    # FFT happens to show as the stronger.
    p = np.argmax(rows[:, :, w:-w], axis=2) + w
    about = np.arange(-w, w + 1)
    near = np.take_along_axis(rows, p[:, :, None] + about, axis=2)
    x = p + (near * about).sum(axis=2) / (near.sum(axis=2) + 1e-30)
    ordered = np.sort(x, axis=0)
    lo, hi = _quantile(ordered, 0.16), _quantile(ordered, 0.84)
    span = hi - lo
    between = (x > lo + 0.25 * span) & (x < hi - 0.25 * span)
    passing = np.count_nonzero(between, axis=0) >= WANDER_MIDDLE * len(x)
    middle = _quantile(ordered, 0.5) - (rows.shape[2] - 1) / 2
    held = rows.min(axis=0).sum(axis=1) / (total.mean(axis=0) + 1e-30)
    moved = np.where(steady & passing & (held < HELD), span / 2, 0.0)
    return moved, steady & (passing | (span < 1e-9)), middle


def _clock_lines(at_hz: np.ndarray, clocks: Sequence[float]) -> np.ndarray:
    """The bins (those at ``at_hz``) a multiple of another receiver's clock falls in."""
    lines = np.zeros(len(at_hz), dtype=bool)
    for clock in clocks:
        if clock <= 0:
            continue
        k = np.round(at_hz / clock)
        lines |= (k > 0) & (np.abs(at_hz - k * clock) <= GUARD_HZ + CLOCK_PPM * 1e-6 * at_hz)
    return lines


def _quantile(ordered: np.ndarray, q: float) -> np.ndarray:
    """The ``q`` quantile down each column of ``ordered`` (sorted), between its rows."""
    at = q * (len(ordered) - 1)
    k = int(at)
    upper = ordered[min(k + 1, len(ordered) - 1)]
    return ordered[k] + (at - k) * (upper - ordered[k])
