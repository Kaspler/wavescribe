# SPDX-License-Identifier: Apache-2.0
"""Watches: a station that listens to every channel of one window of spectrum and keeps
whatever is said on any of them (docs/RADIO.md, "Catching radio that only talks now and then").

A watch is one dongle's window at a time (about 2.25 MHz at 2.4 MS/s): all of PMR446, the 2 m
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
from dataclasses import dataclass, field, replace

import numpy as np

from wavescribe.audio import SAMPLE_RATE
from wavescribe.dsp import DEFAULT_CHANNEL_HZ, NARROW_MARGIN_HZ, OFF_CENTRE_HZ
from wavescribe.dsp.activity import THRESHOLD_DB
from wavescribe.radio.bandplan import (
    AUTO,
    STRICTNESS,
    Band,
    find,
    lookup,
    mhz_text,
    privacy_of,
    region_bands,
)

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
        a, b, n = _slice(watch, width, region, rate, mode, channel_hz)
        span = watch.hi_hz - watch.lo_hz + width
        others = other_kinds(watch, region, width)
        raise WatchError(
            f"{watch.label} is {span / 1e6:.2f} MHz wide, and a watch covers what one receiver "
            f"hears at once, about {usable_mhz(rate):g} MHz: watch a slice of it, "
            f"e.g. {a:.3f}:{b:.3f} ({n} channels)."
            + (f" Part of it is another kind of radio: {others}. A watch is kept as the "
               "strictest kind of radio in it." if others else "")
        )  # fmt: skip
    return watch, planned, width


def usable_mhz(rate: int) -> float:
    """How wide a window can be, START to STOP, rounded down to 50 kHz (2.25 MHz at 2.4 MS/s):
    what one receiver hears, less the edges its filter rolls off, less a narrow channel's width
    and the half step the centre may sit off the middle."""
    return math.floor((2 * (rate / 2 - NARROW_MARGIN_HZ) - 25_000) / 50_000) * 0.05


def _fits(watch: Watch, width: float, rate: int) -> bool:
    return not outside(watch, plan_centre(watch, width, rate), rate, width)


def _asked(watch: Watch, region: str) -> Band | None:
    """The band a window was asked as: the band of that id, or the band of a range's middle."""
    if isinstance(watch.spec, str):
        return next((b for b in region_bands(region) if b.id == watch.spec), None) or find(
            watch.spec
        )
    return lookup(watch.middle_hz, region)


def _own(watch: Watch, region: str, width: float) -> list[int]:
    """The window's channels of its band's kind of radio: not those of a narrower band of
    another kind inside it, nor those whose edges reach one (``lookup``)."""
    band = _asked(watch, region)
    if band is None:
        return list(watch.channels)
    return [c for c in watch.channels if privacy_of(c, region, width) == band.privacy]


def _slice(
    watch: Watch, width: float, region: str, rate: int, mode: str | None, channel_hz: float | None
) -> tuple[float, float, int]:
    """A slice of a window a watch can be tuned to, in MHz, and its number of channels: the one
    with the most of the band's own channels and none of another kind between them, nearest the
    middle among equals. Marine VHF's middle is land mobile radio, which keeps nothing: its
    slice is the ships' channels. The range is planned again from its own band, so the slice is
    checked as the user will give it."""
    own = _own(watch, region, width)
    rank = {c: i for i, c in enumerate(watch.channels)}
    middle = (watch.lo_hz + watch.hi_hz) / 2
    best: tuple[int, float, int, int] | None = None  # channels, -distance from the middle, i, j
    j = 0
    for i in range(len(own)):
        j = max(j, i)
        # As far as the channels run on without another kind between them, and still fit.
        while (
            j + 1 < len(own)
            and rank[own[j + 1]] == rank[own[j]] + 1
            and _fits(replace(watch, channels=tuple(own[i : j + 2])), width, rate)
        ):
            j += 1
        score = (j - i + 1, -abs((own[i] + own[j]) / 2 - middle), i, j)
        if best is None or score[:2] > best[:2]:
            best = score
    if best is not None:
        a, b = math.floor(own[best[2]] / 1e3) / 1e3, math.ceil(own[best[3]] / 1e3) / 1e3
        try:
            again, _, w = _plan((a, b), region, mode, channel_hz, watch.threshold_db)
        except WatchError:
            again = None
        if again is not None and _fits(again, w, rate):
            return a, b, len(again.channels)
    return _about_the_middle(watch, width, region, rate, mode, channel_hz)


def _about_the_middle(
    watch: Watch, width: float, region: str, rate: int, mode: str | None, channel_hz: float | None
) -> tuple[float, float, int]:
    """The widest slice about the middle of a window that a watch can be tuned to."""
    middle = (watch.lo_hz + watch.hi_hz) / 2
    half = rate / 2 - NARROW_MARGIN_HZ - width / 2
    while True:
        a, b = math.ceil((middle - half) / 1e3) / 1e3, math.floor((middle + half) / 1e3) / 1e3
        try:
            again, _, w = _plan((a, b), region, mode, channel_hz, watch.threshold_db)
        except WatchError:
            again = None
        if (again is not None and _fits(again, w, rate)) or half <= watch.step_hz:
            return a, b, len(again.channels) if again is not None else 0
        half -= watch.step_hz / 2


def other_kinds(watch: Watch, region: str, width: float) -> str:
    """Where a window is another kind of radio than its band, in words ("157.45-160.6 MHz is
    Land mobile radio (VHF), private radio"); "" when it is one kind all through."""
    band = _asked(watch, region)
    own_class = band.privacy if band is not None else None
    runs: list[tuple[int, int, str, list[str]]] = []  # first Hz, last Hz, class, band names
    for c in watch.channels:
        cls = privacy_of(c, region, width)
        here = lookup(c, region)  # the band it is in, without its edges
        name = here.name if here is not None else "in no band plan of the region"
        if runs and runs[-1][2] == cls:
            lo, _, _, names = runs[-1]
            runs[-1] = (lo, c, cls, names if name in names else [*names, name])
        else:
            runs.append((c, c, cls, [name]))
    said = []
    for lo, hi, cls, names in runs:
        if cls == own_class:
            continue
        # A channel of the band itself counts as the other kind when its edges reach that.
        names = [n for n in names if band is None or n != band.name] or names
        span = mhz_text(lo / 1e6) if lo == hi else f"{mhz_text(lo / 1e6)}-{mhz_text(hi / 1e6)}"
        said.append(f"{span} MHz is {' and '.join(names)}, {cls} radio")
    return "; ".join(said)


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
        # As the region uses it, where it does (CB is FM in the CEPT countries, AM elsewhere).
        band = next((b for b in region_bands(region) if b.id == spec), band)
        lo, hi, label, key = band.start_hz, band.stop_hz, band.name, spec
    else:
        pair = isinstance(spec, list | tuple) and len(spec) == 2
        if not pair or any(isinstance(x, bool) or not isinstance(x, int | float) for x in spec):
            raise WatchError(
                "'watch' is two frequencies in MHz, e.g. [446.0, 446.2], or a band id from "
                '`wavescribe radio bands`, e.g. "pmr446"'
            )
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


def on_raster(freq_hz: float, region: str) -> int:
    """A frequency as a watch's catches have it: on its band's raster, where it has one, so a
    note made at 446.006 MHz is found on what is caught at 446.00625."""
    band = lookup(freq_hz, region)
    return band.snap(freq_hz) if band is not None and band.step_hz else round(freq_hz)


MAX_CATCHES = 500  # at most this many at once: `radio catches`, /api/catches, /feed/v1/catches
CATCHES = 100  # by default


# What a list of catches may be asked for, checked by the same rule wherever it is asked
# (`radio catches`, the page's /api/catches, /feed/v1/catches): under the least is refused,
# with what it must be; a limit over the most is held to it.
def catch_hours(text: str) -> float:
    """How far back, in hours; 0 for everything kept."""
    return _from_zero(text, "a number from 0 up (0: everything kept)")


def catch_width(text: str) -> float:
    """How far either side of a frequency, in kHz."""
    return _from_zero(text, "a number of kHz from 0 up")


def catch_limit(text: str) -> int:
    """How many at most, held to ``MAX_CATCHES``."""
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise ValueError(
            f"must be a whole number from 1 up (more than {MAX_CATCHES} counts as "
            f"{MAX_CATCHES}), not {text!r}"
        )
    return min(n, MAX_CATCHES)


def _from_zero(text: str, what: str) -> float:
    try:
        x = float(text)
    except ValueError:
        x = math.nan
    if not math.isfinite(x) or x < 0:
        raise ValueError(f"must be {what}, not {text!r}")
    return x


def with_notes(rows: list[dict], notes: dict[int, dict]) -> list[dict]:
    """Catches as the store gives them, with what the user noted about each one's frequency
    (``Catalog.notes``): its name, note and tags. For this machine only: notes are the user's
    own, not something heard."""
    for r in rows:
        noted = notes.get(r["freq_hz"]) or {}
        r.update({k: noted[k] for k in ("name", "note", "tags") if k in noted})
    return rows


@dataclass(frozen=True)
class Hearing:
    """A watch made deaf by noise across its whole window (``warning`` says so), or hearing
    again (``warning`` None) after ``deaf_s``, with the noise learnt as the floor if it stayed."""

    station_id: str
    t: float  # when (epoch seconds)
    warning: str | None
    deaf_s: float = 0.0
    learnt: bool = False


# Who hears about catches: the store keeps them, the command line prints them. A catch is
# published once its audio has been through the VAD and the gate, so ``voice`` is known. The
# command line also prints when a watch goes deaf and hears again: it has no status to show.
_listeners: dict[type, list[Callable[..., object]]] = {Catch: [], Hearing: []}
_lock = threading.Lock()


def subscribe(listener: Callable[[Catch], object]) -> Callable[[], None]:
    """Call ``listener`` with every catch from now on; returns what stops it."""
    return _subscribe(Catch, listener)


def subscribe_hearing(listener: Callable[[Hearing], object]) -> Callable[[], None]:
    """Call ``listener`` each time a watch goes deaf or hears again; returns what stops it."""
    return _subscribe(Hearing, listener)


def _subscribe(kind: type, listener: Callable[..., object]) -> Callable[[], None]:
    with _lock:
        _listeners[kind].append(listener)

    def unsubscribe() -> None:
        with _lock:
            if listener in _listeners[kind]:
                _listeners[kind].remove(listener)

    return unsubscribe


def publish(item: Catch | Hearing) -> None:
    with _lock:
        listeners = list(_listeners[type(item)])
    for listener in listeners:
        try:
            listener(item)
        except Exception:
            log.exception("watch listener %r failed", listener)
