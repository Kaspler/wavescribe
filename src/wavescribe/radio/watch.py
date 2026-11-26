# SPDX-License-Identifier: Apache-2.0
"""Watches: a station that listens to every channel of one window of spectrum and keeps
whatever is said on any of them (docs/RADIO.md, "Catching radio that only talks now and then").

A watch is one dongle's window at a time (about 2.2 MHz at 2.4 MS/s): all of PMR446, the 2 m
amateur band, or a slice of anything else. ``dsp/activity.py`` notices transmissions in it and
``sources/watch.py`` demodulates each one, from just before it was noticed, into a catch.
Each catch's audio then goes through the VAD, the gate and ASR as the watch's own, and the
catch itself is published here for the store and the command line.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from wavescribe.audio import SAMPLE_RATE
from wavescribe.dsp import DEFAULT_CHANNEL_HZ, NARROW_MARGIN_HZ, OFF_CENTRE_HZ
from wavescribe.dsp.activity import THRESHOLD_DB
from wavescribe.radio.bandplan import AUTO, STRICTNESS, find, lookup, mhz_text, privacy_of

log = logging.getLogger(__name__)


class WatchError(ValueError):
    """A window a watch can't listen to, and what would work instead."""


@dataclass(frozen=True)
class Watch:
    """What a watch listens to (``SdrTuning.watch``): the channels of its window."""

    spec: str | tuple[float, float]  # as the user gave it: a band id, or (start, stop) in MHz
    channels: tuple[int, ...]  # Hz, ascending
    step_hz: float  # the raster the channels are on
    threshold_db: float = THRESHOLD_DB
    region: str = AUTO  # the band plan region, for naming what is caught
    label: str = ""  # what the window is called: the band's name, or its range

    @property
    def lo_hz(self) -> int:
        return self.channels[0]

    @property
    def hi_hz(self) -> int:
        return self.channels[-1]

    @property
    def middle_hz(self) -> int:
        return round((self.lo_hz + self.hi_hz) / 2)

    def strictest_in(self, region: str | None, width_hz: float | None = None) -> int:
        """The channel whose privacy class is the strictest under ``region``'s band plan: the
        class of the whole watch, so nothing caught anywhere in the window is kept or shared
        beyond what its band allows. Each channel's class is the strictest its edges reach,
        ``width_hz`` wide (by default, as wide as its band's channels)."""
        region = region or AUTO
        return max(self.channels, key=lambda c: STRICTNESS.index(privacy_of(c, region, width_hz)))


def plan_window(
    spec: str | tuple[float, float] | list[float],
    region: str = AUTO,
    *,
    rate: int,
    mode: str | None = None,
    channel_hz: float | None = None,
    threshold_db: float = THRESHOLD_DB,
) -> tuple[Watch, str, float]:
    """The channels of a band (by its id) or of a range in MHz, with the mode and channel width
    to demodulate them in: from the band plan unless given. A window wider than one receiver
    hears at once is refused, with a slice that fits."""
    watch, planned, width = _plan(spec, region, mode, channel_hz, threshold_db)
    if not _fits(watch, width, rate):
        a, b = _slice(watch, width, region, rate, mode, channel_hz)
        span = watch.hi_hz - watch.lo_hz + width
        raise WatchError(
            f"{watch.label} is {span / 1e6:.2f} MHz wide, and a watch covers what one receiver "
            f"hears at once, about {usable_mhz(rate)} MHz: watch a slice of it, "
            f"e.g. {a:.3f}:{b:.3f}"
        )
    return watch, planned, width


def usable_mhz(rate: int) -> float:
    """How wide a window can be, rounded down (2.2 MHz at 2.4 MS/s): what one receiver hears,
    less the edges its filter rolls off."""
    return math.floor(2 * (rate / 2 - NARROW_MARGIN_HZ) / 1e5) / 10


def _fits(watch: Watch, width: float, rate: int) -> bool:
    return not outside(watch, plan_centre(watch, width, rate), rate, width)


def _slice(
    watch: Watch, width: float, region: str, rate: int, mode: str | None, channel_hz: float | None
) -> tuple[float, float]:
    """The widest slice about the middle of a window that a watch can be tuned to, in MHz: the
    range is planned again from its own band, so the slice is checked as the user will give it."""
    middle = (watch.lo_hz + watch.hi_hz) / 2
    half = rate / 2 - NARROW_MARGIN_HZ - width / 2
    while True:
        a, b = math.ceil((middle - half) / 1e3) / 1e3, math.floor((middle + half) / 1e3) / 1e3
        try:
            again, _, w = _plan((a, b), region, mode, channel_hz, watch.threshold_db)
        except WatchError:
            again = None
        if (again is not None and _fits(again, w, rate)) or half <= watch.step_hz:
            return a, b
        half -= watch.step_hz / 2


def _plan(
    spec: str | tuple[float, float] | list[float],
    region: str,
    mode: str | None,
    channel_hz: float | None,
    threshold_db: float,
) -> tuple[Watch, str, float]:
    if isinstance(spec, str):
        try:
            band = find(spec)
        except KeyError:
            raise WatchError(
                f"no band {spec!r} in the band plan (wavescribe radio bands lists them), "
                "or give a range in MHz, e.g. 446.0:446.2"
            ) from None
        lo, hi, label, key = band.start_hz, band.stop_hz, band.name, spec
    else:
        if len(spec) != 2 or not all(isinstance(x, int | float) for x in spec):
            raise WatchError("a watch's range is two frequencies in MHz, e.g. [446.0, 446.2]")
        lo, hi = round(spec[0] * 1e6), round(spec[1] * 1e6)
        if lo >= hi:
            raise WatchError(f"{spec[0]}-{spec[1]} MHz: the start must be below the stop")
        band = lookup((lo + hi) / 2, region)
        label = f"{mhz_text(lo / 1e6)}-{mhz_text(hi / 1e6)} MHz"
        key = (float(spec[0]), float(spec[1]))
    mode = mode or (band.mode if band is not None and band.mode else "nfm")
    if mode not in DEFAULT_CHANNEL_HZ:
        raise WatchError(
            f"a watch listens for narrowband voice (nfm or am), not {mode}: "
            "broadcast stations talk all the time, so tune them instead"
        )
    step = band.step_hz if band is not None and band.step_hz else DEFAULT_CHANNEL_HZ[mode]
    width = channel_hz or (band.channel_hz if band is not None and band.channel_hz else None)
    width = width or DEFAULT_CHANNEL_HZ[mode]
    if band is not None and band.channels:
        channels = [c for c in band.channels if lo <= c <= hi]
    else:
        start = band.start_hz if band is not None else 0
        first, last = math.ceil((lo - start) / step - 1e-9), math.floor((hi - start) / step + 1e-9)
        channels = [round(start + n * step) for n in range(first, last + 1)]
    if not channels:
        raise WatchError(f"{label} has no channel on the {step / 1e3:g} kHz raster")
    return Watch(key, tuple(channels), step, threshold_db, region, label), mode, float(width)


def plan_centre(watch: Watch, channel_hz: float, rate: int) -> int:
    """Where to tune for a watch. A narrow window goes wholly above the centre, clear of the
    dongle's spike, so the weaker mirror of strong signals a receiver shows about its centre
    falls outside it. A wide one is centred between two channels."""
    reach = rate / 2 - NARROW_MARGIN_HZ
    below = watch.lo_hz - OFF_CENTRE_HZ["nfm"]
    if watch.hi_hz + channel_hz / 2 - below <= reach:
        return below
    k = math.floor((watch.middle_hz - watch.lo_hz) / watch.step_hz)
    return round(watch.lo_hz + (k + 0.5) * watch.step_hz)


def outside(watch: Watch, center_hz: int, rate: int, channel_hz: float) -> list[int]:
    """The channels of the window a dongle tuned to ``center_hz`` doesn't hear."""
    reach = rate / 2 - NARROW_MARGIN_HZ - channel_hz / 2
    return [c for c in watch.channels if abs(c - center_hz) > reach]


@dataclass
class Catch:
    """One transmission caught by a watch: when, where, how strong, and its audio."""

    freq_hz: int
    mode: str
    t_start: float  # the transmission's first moment above the noise (epoch seconds)
    t_end: float  # its last
    audio_from: float  # when ``audio`` starts: a moment before t_start, for the first syllable
    audio: np.ndarray = field(repr=False)  # 16 kHz mono float32
    snr_db: float = 0.0  # the strongest it was over the channel's noise floor
    peak_db: float = 0.0  # the channel's strongest power, dBFS
    reason: str | None = None  # "carrier": on for minutes, learnt into the noise floor
    station_id: str = ""
    privacy: str = "private"  # the watch's class: what is kept of it (radio/bandplan.py)
    band: str | None = None  # the band plan entry the frequency is in
    label: str = ""  # the band's name, with the channel number where it has one
    voice: bool | None = None  # speech passed the VAD and the gate; None: not checked
    id: int | None = None  # its row in the store, once kept

    @property
    def duration(self) -> float:
        return max(0.0, self.t_end - self.t_start)

    @property
    def audio_to(self) -> float:
        return self.audio_from + len(self.audio) / SAMPLE_RATE


def with_notes(rows: list[dict], notes: dict[int, dict]) -> list[dict]:
    """Catches as the store gives them, with what the user noted about each one's frequency
    (``Catalog.notes``): its name, note and tags. For this machine only: notes are the user's
    own, not something heard."""
    for r in rows:
        noted = notes.get(r["freq_hz"]) or {}
        r.update({k: noted[k] for k in ("name", "note", "tags") if k in noted})
    return rows


# Who hears about catches: the store keeps them, the command line prints them. A catch is
# published once its audio has been through the VAD and the gate, so ``voice`` is known.
_listeners: list[Callable[[Catch], object]] = []
_lock = threading.Lock()


def subscribe(listener: Callable[[Catch], object]) -> Callable[[], None]:
    """Call ``listener`` with every catch from now on; returns what stops it."""
    with _lock:
        _listeners.append(listener)

    def unsubscribe() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)

    return unsubscribe


def publish(catch: Catch) -> None:
    with _lock:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(catch)
        except Exception:
            log.exception("catch listener %r failed", listener)
