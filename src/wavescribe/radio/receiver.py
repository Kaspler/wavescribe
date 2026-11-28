# SPDX-License-Identifier: Apache-2.0
"""One receiver's session: its window, watched for as long as the receiver is on.

A session takes its dongle's IQ beside the channels the dongle writes (``RtlSdrDevice`` feeds
any object with ``_feed``, ``_disconnected`` and ``close``, as it does ``SpectrumMonitor``).
It holds:

- the place: the centre and gain, and the places before it (for Undo and "moved at …");
- the detector over the whole usable window (``ActivityDetector`` with an open range and no
  channels), made afresh wherever the window lands, and "settling" until it has learnt the
  floors there;
- the waterfall: lines cut from the detector's own frames (no FFT of its own), the strongest
  of four bins and of the frames in an eighth of a second, ten minutes of them at most, none
  while settling and none from before a move;
- for whoever keeps the marks (``radio.marks``): each thing the detector reports
  (``on_seen``), a look every ``LOOK_S`` once settled (``on_look``), and each landing
  (``on_land``), all on the dongle's thread.

It demodulates nothing: what the detector sees is drawn, never heard.

The detector runs in the dongle's thread, after the channels. When feeding a block to
everything on the dongle has come to take most of the block's own length (``BEHIND``), the
session leaves the detector out for that block: the channels get every sample, the detector
and the waterfall fewer. How long each block took, and how many were left out, go to the log.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from wavescribe.dsp import NARROW_MARGIN_HZ
from wavescribe.dsp.activity import FFT, Activity, ActivityDetector
from wavescribe.dsp.openwatch import OpenBand
from wavescribe.log import event
from wavescribe.radio import bandplan
from wavescribe.radio.marks import Look
from wavescribe.sources import SourceHealth

if TYPE_CHECKING:
    from wavescribe.config import SdrDevice
    from wavescribe.sources.sdr import RtlSdrDevice

log = logging.getLogger(__name__)

RING_S = 600.0  # ten minutes of waterfall
LINES_PER_S = 8
LINE_BINS = 1024
LINE_DB_LO = -120.0  # a line's byte 0; each step up is LINE_DB_STEP more
LINE_DB_STEP = 0.5
BEHIND = 0.8  # of a block's length spent feeding it: the detector skips the block
TIMING_LOG_S = 60.0
EVENTS_MAX = 500
PLACES_MAX = 50
REFERENCE_RATE = 2_400_000  # the detector's FFT keeps its 586 Hz bins at every rate
LOOK_S = 15.0  # a settled window looks again this often (``radio.marks``)
FIRST_LOOK_S = 1.0  # after the floors are learnt: wide signals are said half a second later


@dataclass(frozen=True)
class Place:
    center_hz: int
    gain_db: float
    since: float  # when the receiver came here


@dataclass(frozen=True)
class Seen:
    """Something the detector reported, with times on the wall clock."""

    kind: str  # "start", "end", "short", "noted", "gone", "absorbed"
    freq_hz: int
    width_hz: float
    snr_db: float
    t_start: float
    t_end: float | None
    track: int


class Ring:
    """Waterfall lines, oldest first, never more than ``seconds`` of them."""

    def __init__(self, seconds: float = RING_S, per_s: int = LINES_PER_S,
                 bins: int = LINE_BINS) -> None:  # fmt: skip
        self.size = max(1, round(seconds * per_s))
        self.lines = np.zeros((self.size, bins), dtype=np.uint8)
        self.times = np.zeros(self.size)
        self.count = 0  # lines ever added since the last clear
        self._lock = threading.Lock()

    def add(self, t: float, line: np.ndarray) -> None:
        with self._lock:
            i = self.count % self.size
            self.lines[i] = line
            self.times[i] = t
            self.count += 1

    def clear(self) -> None:
        with self._lock:
            self.count = 0

    def since(self, t: float) -> tuple[np.ndarray, np.ndarray]:
        """The times and lines after ``t``, oldest first."""
        with self._lock:
            n = min(self.count, self.size)
            order = (np.arange(self.count - n, self.count)) % self.size
            times, lines = self.times[order], self.lines[order]
        keep = times > t
        return times[keep].copy(), lines[keep].copy()


def detector_for(config: SdrDevice, region: str = bandplan.AUTO) -> ActivityDetector:
    """The detector over a receiver's whole usable window, with no channels: AM bands, where a
    bare carrier is the signal, have their own rule (``OpenBand(carrier=True)``)."""
    half = config.sample_rate / 2 - NARROW_MARGIN_HZ
    lo, hi = config.center_hz - half, config.center_hz + half
    bands = [
        OpenBand(max(lo, b.start_hz), min(hi, b.stop_hz), carrier=True)
        for b in bandplan.region_bands(region)
        if b.mode == "am" and b.start_hz < hi and b.stop_hz > lo
    ]
    fft = round(FFT * config.sample_rate / REFERENCE_RATE)
    return ActivityDetector(config.sample_rate, config.center_hz, [], 12_500, fft=fft,
                            open_range=(lo, hi), open_bands=bands)  # fmt: skip


class ReceiverSession:
    """The session of one dongle (see the module's words). ``start`` attaches it; ``close``
    lets the dongle go once nothing else holds it."""

    def __init__(
        self,
        device: RtlSdrDevice,
        *,
        region: str = bandplan.AUTO,
        clock: Callable[[], float] = time.time,
        ring_s: float = RING_S,
        behind: float = BEHIND,
        timing_log_s: float = TIMING_LOG_S,
        make_detector: Callable[[SdrDevice, str], ActivityDetector] = detector_for,
        look_s: float = LOOK_S,
    ) -> None:
        self.device = device
        self.station_id = f"session:{device.config.id}"
        self.health = SourceHealth()
        self.region = region
        self.ring = Ring(ring_s)
        self.events: deque[Seen] = deque(maxlen=EVENTS_MAX)
        self.places: deque[Place] = deque(maxlen=PLACES_MAX)
        self.dropped = 0  # blocks the detector was left out of
        self._clock = clock
        self._behind = behind
        self._timing_log_s = timing_log_s
        self._make = make_detector
        self._lock = threading.Lock()
        self._detector: ActivityDetector | None = None
        self._settle_samples = 0  # IQ the detector needs before its floors are learnt
        self._anchor = (0.0, 0)  # the wall time of a sample the detector was fed, its index
        self._slot: float | None = None  # the waterfall line being gathered: its start
        self._gathered: np.ndarray | None = None
        self._spent: list[float] = []  # seconds per block since the timing was last logged
        self._blocks = 0
        self._next_log = 0.0
        self._look_s = look_s
        self._first_look = 0  # samples into a landing before the first look there
        self._next_look = 0.0
        # Whoever keeps the marks: each thing seen (receiver id, ``Seen``) and each look, on
        # the dongle's thread.
        self.on_seen: Callable[[str, Seen], None] | None = None
        self.on_look: Callable[[Look], None] | None = None
        self.on_land: Callable[[str, float], None] | None = None  # (receiver id, when)

    # The place.

    @property
    def place(self) -> Place | None:
        return self.places[-1] if self.places else None

    @property
    def settling(self) -> bool:
        """Whether the detector is still learning the floors where the window is now: no
        marks, no waterfall yet."""
        d = self._detector
        return d is None or d.samples < self._settle_samples

    def start(self) -> None:
        self.device.attach(self)  # type: ignore[arg-type]

    def close(self) -> None:
        self.device.detach(self)  # type: ignore[arg-type]

    def lines(self, since: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Waterfall lines after ``since`` (wall time): their times, and one row of
        ``LINE_BINS`` bytes each (``LINE_DB_LO`` plus ``LINE_DB_STEP`` a step), lowest
        frequency first."""
        return self.ring.since(since)

    def status(self) -> dict[str, object]:
        d = self._detector
        p = self.place
        return {
            "receiver": self.device.config.id,
            "center_hz": p.center_hz if p else None,
            "since": p.since if p else None,
            "settling": self.settling,
            "deaf": d.deaf_reason if d is not None and d.deaf_s is not None else None,
            "wide": [{"freq_hz": f, "width_hz": w} for f, w in d.open.noted]
            if d is not None and d.open is not None
            else [],
            "lines_known": d.open.steady if d is not None and d.open is not None else 0,
            "dropped": self.dropped,
        }

    def timing(self) -> dict[str, float]:
        """Milliseconds per block spent in the session since the timing was last logged."""
        spent = np.array(self._spent) * 1e3 if self._spent else np.zeros(1)
        return {
            "blocks": float(len(self._spent)),
            "p50_ms": float(np.percentile(spent, 50)),
            "p99_ms": float(np.percentile(spent, 99)),
            "max_ms": float(spent.max()),
            "dropped": float(self.dropped),
        }

    # Called on the dongle's thread.

    def _feed(self, iq: np.ndarray, t_end: float, first: bool) -> None:
        began = time.perf_counter()
        config = self.device.config
        rate = config.sample_rate
        if first or self._detector is None:  # a move starts rtl_sdr again: ``first``
            self._land(config, t_end - len(iq) / rate)
        self.health.connected = True
        self.health.last_data_at = t_end
        d = self._detector
        assert d is not None
        if self.device.load >= self._behind:
            self.dropped += 1
        else:
            self._anchor = (t_end, d.samples + len(iq))  # the block's last sample, and when
            for a in d.feed(iq):
                seen = self._seen(a)
                self.events.append(seen)
                if self.on_seen is not None:
                    self.on_seen(config.id, seen)
            if d.samples >= self._first_look and t_end >= self._next_look:
                self._next_look = t_end + self._look_s
                if self.on_look is not None:
                    self.on_look(self._look(d, config, t_end))
        self._time(time.perf_counter() - began, t_end)

    def _disconnected(self, reconnects: int) -> None:
        self.health.connected = False
        self.health.reconnects = reconnects

    def _land(self, config: SdrDevice, t: float) -> None:
        """The window is somewhere new (or back after a gap): a fresh detector, and from a
        new place an empty waterfall."""
        p = self.place
        if p is None or p.center_hz != config.center_hz or p.gain_db != config.gain_db:
            self.places.append(Place(config.center_hz, config.gain_db, t))
            self.ring.clear()
        if self.on_land is not None:
            self.on_land(config.id, t)
        d = self._make(config, self.region)
        self._settle_samples = d.init_samples + 2 * d.frame_samples
        d.on_frame = self._frame
        self._first_look = self._settle_samples + round(FIRST_LOOK_S * config.sample_rate)
        self._next_look = 0.0
        self._detector = d
        self._slot = None
        self._gathered = None

    def _frame(self, power: np.ndarray, start: int, end: int) -> None:
        """One of the detector's frames (power by bin, centre in the middle): into the line
        being gathered, which goes to the waterfall once its eighth of a second is over."""
        if end < self._settle_samples:
            return
        t = self._when(end)
        merged = power.reshape(LINE_BINS, -1).max(axis=1)
        if self._slot is None:
            self._slot, self._gathered = t, merged
        else:
            assert self._gathered is not None
            np.maximum(self._gathered, merged, out=self._gathered)
        if t - self._slot >= 1 / LINES_PER_S:
            assert self._gathered is not None
            db = 10 * np.log10(self._gathered + 1e-20)
            steps = np.clip((db - LINE_DB_LO) / LINE_DB_STEP, 0, 255)
            self.ring.add(t, steps.astype(np.uint8))
            self._slot = None

    def _look(self, d: ActivityDetector, config: SdrDevice, t: float) -> Look:
        half = config.sample_rate / 2 - NARROW_MARGIN_HZ
        noted = d.open.noted if d.open is not None else []
        wide = tuple((float(f), float(w)) for f, w in noted)
        return Look(config.id, config.center_hz - half, config.center_hz + half, t, wide,
                    d.deaf_s is not None)  # fmt: skip

    def _when(self, index: int) -> float:
        """The wall time of the detector's sample ``index``."""
        t, at = self._anchor
        return t - (at - index) / self.device.config.sample_rate

    def _seen(self, a: Activity) -> Seen:
        end = None if a.end is None else self._when(a.end)
        return Seen(a.kind, a.freq_hz, a.width_hz, a.snr_db, self._when(a.start), end, a.track)

    def _time(self, spent: float, now: float) -> None:
        self._spent.append(spent)
        if now < self._next_log:
            return
        if self._next_log:
            t = self.timing()
            event(log, "receiver_timing", device=self.device.config.id,
                  blocks=int(t["blocks"]), p50_ms=round(t["p50_ms"], 2),
                  p99_ms=round(t["p99_ms"], 2), max_ms=round(t["max_ms"], 2),
                  dropped=self.dropped, load=round(self.device.load, 3))  # fmt: skip
        self._spent = []
        self._next_log = now + self._timing_log_s
