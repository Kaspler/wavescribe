# SPDX-License-Identifier: Apache-2.0
"""Marks: what the receivers' detectors saw at a place, kept across looks.

A mark is a place where something was seen: a wide signal (a broadcast station, a flat
block), a narrow transmission (a voice on a channel), or a carrier in an AM band. Its frequency
is the channel's centre when the measurement is within a quarter channel of one (in a band
with channels of its kind), else the nearest kHz; a later sighting this close (``STICKY_HZ``,
a quarter of a wide signal's width) is the same mark.

What the detector reports (``radio.receiver.Seen``) makes and updates marks. A **look** is a
receiver's window settled and hearing (not deaf) at one moment, with the wide signals its
detector holds then: every wide mark the window holds whole was there or wasn't.

- A wide mark seen in at least ``STEADY_SHARE`` of the looks that covered it, over looks at
  least ``STEADY_SPAN_S`` apart, is **steady** (so is one a broadcast entry of the heard
  catalogue already knew).
- A wide mark missed in ``GHOST_LOOKS`` looks in a row spanning ``GHOST_SPAN_S`` becomes a
  **ghost** ("not seen since …"), only if it was first seen ``GHOST_MIN_DB`` over the floor: a
  weak station present when a window lands may not be noted, which is no evidence it left. A
  look while the window is deaf counts for nothing.
- A narrow or AM mark is occasional: it is **active** while a transmission lasts and **quiet**
  after ("quiet now, last active …"), and it never becomes a ghost. "Quiet now" is only said of
  what was seen since Wavescribe started: marks read back after a restart are only "seen".

Marks follow their class's text retention (``bandplan.POLICIES``): ``rows`` gives only the
marks that may be written, and private and digital radio, kept nowhere by default, stay in
memory for the run. Nothing here demodulates; the ladder's rungs (``radio.ladder``) attach to a
mark as they are measured.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wavescribe.radio import bandplan, ladder

WIDE, NARROW, AM = "wide", "narrow", "am"
KINDS = (WIDE, NARROW, AM)
STICKY_HZ = 2_000.0  # a narrow sighting this close to a mark is that mark
WIDE_STICKY = 0.25  # of a wide mark's width
WIDE_MIN_HZ = 40_000.0  # a wide signal's width when the detector gives none
STEADY_SHARE = 0.8
STEADY_SPAN_S = 600.0
GHOST_LOOKS = 3
GHOST_SPAN_S = 30.0
GHOST_MIN_DB = 10.0
MARKS_MAX = 2_000  # in memory; the oldest occasional marks go first
DAY_S = 86_400.0

# What a change to a mark is, for the screen and the catalogue.
NEW, ACTIVE, QUIET, STEADY, GHOST, BACK = "new", "active", "quiet", "steady", "ghost", "back"


@dataclass
class Mark:
    id: int
    freq_hz: int
    kind: str  # WIDE, NARROW or AM
    width_hz: float
    privacy: str
    band: str | None
    first_seen: float
    last_seen: float
    first_snr_db: float
    seen_looks: int = 0
    missed_looks: int = 0
    first_look: float | None = None  # the first and last look it was seen in
    last_look: float | None = None
    steady: bool = False
    ghost: bool = False
    last_active: float | None = None  # a narrow or AM mark's last transmission
    dismissed: bool = False
    # The ladder: rung 1 at once, the others as they are measured.
    place: ladder.Place | None = None
    looks_like: str | None = None  # rung 2: ``ladder.Shape.kind``
    sound: ladder.Sound | None = None  # rung 3
    name: str | None = None  # rung 4
    # This run only.
    new: bool = True  # first seen this run, and not known before
    held_by: set[str] = field(default_factory=set)  # receivers whose detector holds it now
    active: bool = False
    since_start: bool = True  # seen since Wavescribe started
    miss_run: int = 0
    miss_from: float | None = None

    @property
    def here(self) -> bool:
        """A wide mark some receiver's detector holds now."""
        return bool(self.held_by)

    @property
    def state(self) -> str:
        """In a word, for the screen: "ghost", "active", "quiet", "here" or "seen"."""
        if self.ghost:
            return "ghost"
        if self.kind == WIDE:
            return "here" if self.here else "seen"
        if self.active:
            return "active"
        return "quiet" if self.since_start and self.last_active is not None else "seen"

    @property
    def rung(self) -> int:
        """The highest rung that says something of it."""
        if self.name:
            return 4
        if self.sound is not None:
            return 3
        return 2 if self.looks_like else 1


@dataclass(frozen=True)
class Change:
    what: str  # NEW, ACTIVE, QUIET, STEADY, GHOST or BACK
    mark: int


@dataclass(frozen=True)
class Look:
    """A receiver's window, settled, at ``t``: its edges, the wide signals its detector holds
    (centre, width), and whether it was deaf then."""

    receiver: str
    lo_hz: float
    hi_hz: float
    t: float
    wide: tuple[tuple[float, float], ...] = ()
    deaf: bool = False


@dataclass
class Marks:
    """Every mark, for all receivers. Safe to call from several threads."""

    region: str = bandplan.AUTO
    known: Sequence[int] = ()  # steady broadcast stations the heard catalogue knows (Hz)
    marks: dict[int, Mark] = field(default_factory=dict)
    _next: int = 1
    _tracks: dict[tuple[str, int], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # What the detectors report.

    def seen(self, receiver: str, kind: str, freq_hz: float, width_hz: float, snr_db: float,
             t_start: float, t_end: float | None, track: int) -> list[Change]:  # fmt: skip
        """One of a session's ``Seen`` (its fields): "noted"/"gone" for a wide signal,
        "start"/"end"/"short" for a transmission, "absorbed" for one that was a wide signal's."""
        with self._lock:
            if kind in ("noted", "gone"):
                return self._wide(receiver, kind == "noted", freq_hz, width_hz, snr_db, t_start)
            key = (receiver, track)
            if kind == "absorbed":
                m = self.marks.get(self._tracks.pop(key, -1))
                if m is not None and m.first_seen == t_start:
                    del self.marks[m.id]  # it never was a transmission of its own
                return []
            if kind == "start":
                m, out = self._narrow(freq_hz, width_hz, snr_db, t_start)
                self._tracks[key] = m.id
                m.active, m.last_active = True, t_start
                return [*out, Change(ACTIVE, m.id)]
            m = self.marks.get(self._tracks.pop(key, -1))
            out = []
            if m is None:  # "short", or the start was before this run's marks
                m, out = self._narrow(freq_hz, width_hz, snr_db, t_start)
            end = t_end if t_end is not None else t_start
            m.active, m.last_active, m.last_seen = False, end, max(m.last_seen, end)
            m.since_start = True
            return [*out, Change(QUIET, m.id)]

    def take(self, receiver: str, seen: Any) -> list[Change]:
        """A session's ``radio.receiver.Seen``, as ``seen`` takes it."""
        s = seen
        return self.seen(receiver, s.kind, s.freq_hz, s.width_hz, s.snr_db, s.t_start, s.t_end,
                         s.track)  # fmt: skip

    def landed(self, receiver: str, t: float) -> list[Change]:
        """A receiver's window starts afresh (moved, or back after a gap): what its detector
        held is no longer held by it, and the transmissions it was following end there."""
        out: list[Change] = []
        with self._lock:
            for m in self.marks.values():
                m.held_by.discard(receiver)
            for key in [k for k in self._tracks if k[0] == receiver]:
                m = self.marks.get(self._tracks.pop(key))
                if m is not None and m.active:
                    m.active, m.last_active = False, t
                    out.append(Change(QUIET, m.id))
        return out

    def look(self, look: Look) -> list[Change]:
        """A settled window: each wide mark inside it was there or wasn't."""
        if look.deaf:
            return []
        out: list[Change] = []
        with self._lock:
            for f, w in look.wide:  # held now, though noted before this run's marks: its strength
                if self._find(WIDE, f, w) is None:  # unknown, so it never becomes a ghost
                    out += self._wide(look.receiver, True, f, w, 0.0, look.t)
            for m in list(self.marks.values()):
                half = m.width_hz / 2
                whole = look.lo_hz <= m.freq_hz - half and m.freq_hz + half <= look.hi_hz
                if m.kind != WIDE or not whole:
                    continue  # only a wide mark the window holds whole
                if any(self._near(m, f) for f, _ in look.wide):
                    out += self._seen_in(m, look.receiver, look.t)
                else:
                    out += self._missed_in(m, look.receiver, look.t)
        return out

    def dismiss(self, mark_id: int) -> None:
        with self._lock:
            if mark_id in self.marks:
                self.marks[mark_id].dismissed = True

    def describe(self, mark_id: int, *, shape: ladder.Shape | None = None,
                 sound: ladder.Sound | None = None, name: str | None = None) -> None:  # fmt: skip
        """Attach what the ladder's rungs 2 to 4 measured."""
        with self._lock:
            m = self.marks.get(mark_id)
            if m is None:
                return
            if shape is not None and shape.kind != "nothing":
                m.looks_like = shape.kind
            m.sound = sound or m.sound
            m.name = name or m.name

    def between(self, lo_hz: float, hi_hz: float, *, dismissed: bool = False) -> list[Mark]:
        with self._lock:
            return sorted(
                (m for m in self.marks.values()
                 if lo_hz <= m.freq_hz <= hi_hz and (dismissed or not m.dismissed)),
                key=lambda m: m.freq_hz,
            )  # fmt: skip

    # What may be written, and reading it back.

    def rows(self, now: float, text_days: Mapping[str, float | None] | None = None) -> list[dict]:
        """The marks that may be written: those whose class keeps text (``text_days``, by
        default from ``bandplan.POLICIES``; None is the store's own retention, 0 never), last
        seen within that many days."""
        days = text_days or {c: p.text_days for c, p in bandplan.POLICIES.items()}
        with self._lock:
            keep = [m for m in self.marks.values() if _kept(m, days, now)]
            return [_row(m) for m in sorted(keep, key=lambda m: m.id)]

    @classmethod
    def restore(cls, rows: Iterable[Mapping], *, region: str = bandplan.AUTO,
                known: Sequence[int] = ()) -> Marks:  # fmt: skip
        """Marks read back: seen before this run, so never "new" and never "quiet now"."""
        marks = cls(region, known)
        for r in rows:
            m = Mark(
                int(r["id"]), int(r["freq_hz"]), str(r["kind"]), float(r["width_hz"]),
                str(r["privacy"]), r.get("band"), float(r["first_seen"]), float(r["last_seen"]),
                float(r["first_snr_db"]), int(r.get("seen_looks", 0)),
                int(r.get("missed_looks", 0)), r.get("first_look"), r.get("last_look"),
                bool(r.get("steady")), bool(r.get("ghost")), r.get("last_active"),
                bool(r.get("dismissed")),
            )  # fmt: skip
            m.place = ladder.place(m.freq_hz, region, m.width_hz if m.kind == WIDE else None)
            m.looks_like, m.name = r.get("looks_like"), r.get("name")
            m.new, m.since_start = False, False
            marks.marks[m.id] = m
            marks._next = max(marks._next, m.id + 1)
        return marks

    # Inside.

    def _wide(self, receiver: str, here: bool, freq: float, width: float, snr: float,
              t: float) -> list[Change]:  # fmt: skip
        width = max(width, WIDE_MIN_HZ)
        m = self._find(WIDE, freq, width)
        if m is None:
            if not here:
                return []
            m = self._make(WIDE, freq, width, snr, t)
            m.held_by.add(receiver)
            return [Change(NEW, m.id)] if m.new else []
        if not here:
            m.held_by.discard(receiver)
        else:
            m.held_by.add(receiver)
            m.last_seen = max(m.last_seen, t)
            m.width_hz = width
            m.since_start = True
            if m.ghost:
                m.ghost = False
                m.miss_run, m.miss_from = 0, None
                return [Change(BACK, m.id)]
        return []

    def _narrow(self, freq: float, width: float, snr: float, t: float) -> tuple[Mark, list]:
        band = bandplan.lookup(freq, self.region)
        kind = AM if band is not None and band.mode == "am" else NARROW
        m = self._find(kind, freq, width)
        if m is not None:
            m.last_seen = max(m.last_seen, t)
            m.since_start = True
            return m, []
        m = self._make(kind, freq, width, snr, t)
        return m, [Change(NEW, m.id)] if m.new else []

    def _make(self, kind: str, freq: float, width: float, snr: float, t: float) -> Mark:
        band = bandplan.lookup(freq, self.region, width if kind == WIDE else None)
        at = snapped(freq, band, kind)
        place = ladder.place(at, self.region, width if kind == WIDE else None)
        m = Mark(self._next, at, kind, width, place.privacy, place.band, t, t, snr, place=place)
        if kind == WIDE and any(abs(k - at) <= WIDE_STICKY * width for k in self.known):
            m.new, m.steady = False, True
        self._next += 1
        self.marks[m.id] = m
        self._bound()
        return m

    def _find(self, kind: str, freq: float, width: float) -> Mark | None:
        near = [m for m in self.marks.values() if m.kind == kind and self._near(m, freq, width)]
        return min(near, key=lambda m: abs(m.freq_hz - freq)) if near else None

    @staticmethod
    def _near(m: Mark, freq: float, width: float | None = None) -> bool:
        if m.kind == WIDE:
            return abs(m.freq_hz - freq) <= WIDE_STICKY * max(m.width_hz, width or 0.0)
        return abs(m.freq_hz - freq) <= STICKY_HZ

    def _seen_in(self, m: Mark, receiver: str, t: float) -> list[Change]:
        out: list[Change] = []
        m.seen_looks += 1
        m.first_look = t if m.first_look is None else m.first_look
        m.last_look, m.last_seen = t, max(m.last_seen, t)
        m.held_by.add(receiver)
        m.since_start = True
        m.miss_run, m.miss_from = 0, None
        if m.ghost:
            m.ghost = False
            out.append(Change(BACK, m.id))
        looks = m.seen_looks + m.missed_looks
        if (not m.steady and m.seen_looks / looks >= STEADY_SHARE
                and t - m.first_look >= STEADY_SPAN_S):  # fmt: skip
            m.steady = True
            out.append(Change(STEADY, m.id))
        return out

    def _missed_in(self, m: Mark, receiver: str, t: float) -> list[Change]:
        m.missed_looks += 1
        m.miss_run += 1
        m.miss_from = t if m.miss_from is None else m.miss_from
        m.held_by.discard(receiver)
        if (not m.ghost and m.first_snr_db >= GHOST_MIN_DB and m.miss_run >= GHOST_LOOKS
                and t - m.miss_from >= GHOST_SPAN_S):  # fmt: skip
            m.ghost = True
            return [Change(GHOST, m.id)]
        return []

    def _bound(self) -> None:
        if len(self.marks) <= MARKS_MAX:
            return
        idle = [m for m in self.marks.values() if not m.active and not m.here]
        for m in sorted(idle, key=lambda m: (m.kind == WIDE, m.last_seen))[
            : len(self.marks) - MARKS_MAX
        ]:
            del self.marks[m.id]


def snapped(freq: float, band: bandplan.Band | None, kind: str = NARROW) -> int:
    """A measured frequency as a mark's place: the channel's centre within a quarter channel
    of it, where the band has channels (a list, a channel width, or broadcast FM's raster) of
    the mark's kind; else the nearest kHz (a dongle and a transmitter are each about 1 ppm off:
    hundreds of Hz, nowhere near the Hz a measurement gives). A band's scanning step alone is
    no channel: short-range devices sit anywhere in theirs."""
    khz = round(freq / 1_000) * 1_000
    if band is None or band.mode is None or (kind == WIDE) != (band.mode == "wfm"):
        return khz
    if not (band.channels or band.channel_hz or band.mode == "wfm"):
        return khz
    centre = band.snap(freq)
    return centre if abs(centre - freq) <= bandplan.channel_width(band) / 4 else khz


def _kept(m: Mark, days: Mapping[str, float | None], now: float) -> bool:
    d = days.get(m.privacy, 0)
    if d == 0:
        return False
    return d is None or now - m.last_seen <= d * DAY_S


_ROW = ("id", "freq_hz", "kind", "width_hz", "privacy", "band", "first_seen", "last_seen",
        "first_snr_db", "seen_looks", "missed_looks", "first_look", "last_look", "steady",
        "ghost", "last_active", "dismissed", "looks_like", "name")  # fmt: skip


def _row(m: Mark) -> dict:
    return {k: getattr(m, k) for k in _ROW}
