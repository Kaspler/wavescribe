# SPDX-License-Identifier: Apache-2.0
"""Which receiver writes each kept channel, and what a move of a receiver changes.

A receiver has a place: its centre, and the window about it that its sample rate gives. A kept
channel is written by a receiver whose window holds it; otherwise it waits, for one reason.
``assign`` is that rule over plain records, so it can be asked of any set of places: the places
now, the places a move would make, the places a cover proposes (``radio/cover.py``).
``reconcile`` is the difference between what is assigned and what runs: the channels to start,
to stop, and to point at another receiver or place. ``move`` is what moving one receiver would
change, and the refusal a pinned channel gives it.

Nothing here opens a receiver or starts a station: the caller applies the answer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from wavescribe.dsp import DEFAULT_CHANNEL_HZ, NARROW_MARGIN_HZ, in_band, off_centre
from wavescribe.radio.bandplan import TUNER_RANGE_HZ

# Why a kept channel isn't written, in the order they are given: the first that holds.
OFF = "off"  # switched off
CONFIRM = "confirm"  # its kind of radio needs the user's confirmation, not given yet
NO_RECEIVER = "no_receiver"  # no receiver's window holds it
UNPLUGGED = "unplugged"  # only a receiver that isn't connected would hold it
HELD = "held"  # only a receiver another program has open would hold it
TURN = "turn"  # the Mac writes no more at once (``writers_max``)
REASONS = (OFF, CONFIRM, NO_RECEIVER, UNPLUGGED, HELD, TURN)

ANY = "any"  # a pin that any receiver may honour


@dataclass(frozen=True)
class Kept:
    """A kept channel, as placing it needs it.

    ``freqs`` are where it may be heard: any one of them, or with ``whole`` (a band kept whole,
    a watch) every one. ``confirmed`` is whether its kind of radio may be received: true where
    the class asks nothing first. ``pin`` keeps a receiver on it: that receiver's id, or
    ``ANY`` for whichever writes it. ``strength_db`` is how strong it was measured (dBFS), where
    known, so a cover can say when it shares a window with something much stronger, and
    ``snr_db`` how far over its noise, so it can say when it is heard weakly."""

    id: str
    freqs: tuple[int, ...]
    mode: str = "wfm"
    channel_hz: float | None = None
    whole: bool = False
    privacy: str = "broadcast"
    on: bool = True
    confirmed: bool = True
    pin: str | None = None
    priority: int = 5  # 1 is written first when the Mac can't write them all
    kept_at: float = 0.0  # when it was kept: the newest goes before older ones of a priority
    strength_db: float | None = None
    snr_db: float | None = None  # how far over its noise it was measured, where known

    @property
    def width_hz(self) -> float:
        if self.mode == "wfm":
            return 0.0  # its margin is in ``margin_hz``
        return float(self.channel_hz or DEFAULT_CHANNEL_HZ[self.mode])


@dataclass(frozen=True)
class Receiver:
    """A receiver and its place. ``since`` is when it came to this place: where windows
    overlap, the receiver there longest writes. ``tunes`` is the range its tuner reaches."""

    id: str
    center_hz: int
    sample_rate: int = 2_400_000
    connected: bool = True
    held_by: str | None = None  # another program that has it open
    since: float = 0.0
    tunes: tuple[int, int] = TUNER_RANGE_HZ

    @property
    def free(self) -> bool:
        return self.connected and self.held_by is None


@dataclass(frozen=True)
class Written:
    receiver: str


@dataclass(frozen=True)
class Waiting:
    reason: str


Assignment = Written | Waiting


def covers(r: Receiver, k: Kept) -> bool:
    """Whether receiver ``r``, where it is, holds ``k``: a channel inside its window and, if
    narrow, clear of the spike at its centre (``config.hears``); a band kept whole with every
    channel inside, a channel's half-width from the edge (``radio.watch.outside``)."""
    if k.whole:
        reach = r.sample_rate / 2 - NARROW_MARGIN_HZ - k.width_hz / 2
        return all(abs(f - r.center_hz) <= reach for f in k.freqs)
    return any(
        in_band(f, r.center_hz, r.sample_rate, k.mode)
        and off_centre(f, r.center_hz, k.mode, k.channel_hz)
        for f in k.freqs
    )


def assign(
    kept: Iterable[Kept], receivers: Iterable[Receiver], writers_max: int | None = None
) -> dict[str, Assignment]:
    """Who writes each kept channel, or why it waits (``REASONS``). A channel two windows hold
    is written once: by the receiver it is pinned to, else by the one there longest. With
    ``writers_max``, the channels beyond it wait for their turn: pinned ones first, then by
    priority, then the most recently kept."""
    rs = sorted(receivers, key=lambda r: (r.since, r.id))
    out: dict[str, Assignment] = {}
    candidates: list[Kept] = []
    for k in kept:
        if not k.on:
            out[k.id] = Waiting(OFF)
            continue
        if not k.confirmed:
            out[k.id] = Waiting(CONFIRM)
            continue
        holding = [r for r in rs if covers(r, k)]
        able = [r for r in holding if r.free]
        if able:
            pinned = [r for r in able if r.id == k.pin]
            out[k.id] = Written((pinned or able)[0].id)
            candidates.append(k)
        elif any(r.held_by is not None for r in holding):
            out[k.id] = Waiting(HELD)
        elif holding:
            out[k.id] = Waiting(UNPLUGGED)
        else:
            out[k.id] = Waiting(NO_RECEIVER)
    if writers_max is not None and len(candidates) > writers_max:
        order = sorted(candidates, key=lambda k: (k.pin is None, k.priority, -k.kept_at, k.id))
        for k in order[writers_max:]:
            out[k.id] = Waiting(TURN)
    return out


@dataclass(frozen=True)
class Changes:
    """What to do so that what runs is what is assigned."""

    start: list[str] = field(default_factory=list)
    stop: list[str] = field(default_factory=list)
    # Written still, but somewhere else: by another receiver, or by its own at a new place.
    # The caller points the running channel at it rather than stopping and starting it, so
    # what the channel had heard so far (its voice detector, its speech) carries on.
    repoint: dict[str, str] = field(default_factory=dict)


def reconcile(
    assignment: Mapping[str, Assignment],
    running: Mapping[str, tuple[str, int]],
    receivers: Iterable[Receiver],
) -> Changes:
    """The difference between ``assignment`` and ``running`` (channel id → the receiver and
    centre it runs at)."""
    centre = {r.id: r.center_hz for r in receivers}
    changes = Changes()
    for kid, a in assignment.items():
        on = running.get(kid)
        if isinstance(a, Written):
            if on is None:
                changes.start.append(kid)
            elif on != (a.receiver, centre[a.receiver]):
                changes.repoint[kid] = a.receiver
        elif on is not None:
            changes.stop.append(kid)
    changes.stop.extend(kid for kid in running if kid not in assignment)  # no longer kept
    return changes


@dataclass(frozen=True)
class Move:
    """What moving ``receiver`` from ``was`` to ``to`` changes. ``refused`` lists the pinned
    channels it would leave unwritten: then nothing moved (unless forced). ``instead`` is a
    free receiver and a centre from which it would write every channel in ``stopped``."""

    receiver: str
    was: int
    to: int
    stopped: list[str]
    started: list[str]
    refused: list[str] = field(default_factory=list)
    instead: tuple[str, int] | None = None
    assignment: dict[str, Assignment] = field(default_factory=dict)  # after the move

    @property
    def moved(self) -> bool:
        return not self.refused


def move(
    kept: Sequence[Kept],
    receivers: Sequence[Receiver],
    receiver_id: str,
    center_hz: int,
    *,
    now: float = 0.0,
    force: bool = False,
    writers_max: int | None = None,
) -> Move:
    """Moving receiver ``receiver_id`` to ``center_hz``: the channels it stops and starts
    writing, and a free receiver that could take over what it stops (radio/cover.py). A move
    that would leave a pinned channel unwritten is refused unless ``force``."""
    from wavescribe.radio.cover import window_for

    rs = {r.id: r for r in receivers}
    r = rs[receiver_id]
    lo, hi = r.tunes
    center_hz = min(max(center_hz, lo), hi)
    before = assign(kept, receivers, writers_max)
    after_rs = [replace(x, center_hz=center_hz, since=now) if x.id == receiver_id else x
                for x in receivers]  # fmt: skip
    after = assign(kept, after_rs, writers_max)
    written = {k: isinstance(a, Written) for k, a in after.items()}
    stopped = [k.id for k in kept if isinstance(before[k.id], Written) and not written[k.id]]
    started = [k.id for k in kept if not isinstance(before[k.id], Written) and written[k.id]]
    by_id = {k.id: k for k in kept}
    refused = [
        kid
        for kid in stopped
        if by_id[kid].pin == ANY
        or (by_id[kid].pin == receiver_id and before[kid] == Written(receiver_id))
    ]
    instead = None
    if stopped:
        busy = {a.receiver for a in before.values() if isinstance(a, Written)}
        for other in sorted(receivers, key=lambda x: x.id):
            if other.id == receiver_id or other.id in busy or not other.free:
                continue
            if (c := window_for([by_id[kid] for kid in stopped], other)) is not None:
                instead = (other.id, c)
                break
    if refused and not force:
        return Move(receiver_id, r.center_hz, r.center_hz, [], [], refused, instead, before)
    return Move(receiver_id, r.center_hz, center_hz, stopped, started, [], instead, after)
