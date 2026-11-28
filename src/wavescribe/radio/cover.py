# SPDX-License-Identifier: Apache-2.0
"""Placing receivers so that a set of channels is heard: the windows the set needs, a centre for
each, and, when it needs more windows than there are free receivers, which groups compete.

A channel is heard from any centre within reach of it: an interval of centres, with a hole in
the middle for a narrow channel (the spike at a receiver's centre holds a squelch open). Finding
the fewest windows for a set is then placing the fewest points so that every channel's interval
holds one. Taking the channel whose interval ends first and placing the point at that end,
again and again, gives the fewest windows when the intervals have no holes (broadcast FM, bands
kept whole); a narrow channel's hole only ever moves a point off the spike, at worst costing a
window more than the least possible. Each window's centre is then placed, within what holds its
channels, as near as it goes to where a receiver usually sits for them: a little below, clear of
the spike, rather than at the very edge.

What it can't say: whether a receiver hears a channel well there (only measurements say that:
``strength_db`` and ``snr_db`` on each channel), or why a channel is weak.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from wavescribe.dsp import NARROW_MARGIN_HZ, OFF_CENTRE_HZ, margin_hz
from wavescribe.radio.writer import CONFIRM, Kept, Receiver, Written, assign, covers

# Why a channel of the set isn't placed. CONFIRM (radio/writer.py) as well.
OUT_OF_RANGE = "out_of_range"  # no receiver's tuner reaches a place that hears it
DIGITAL = "digital"  # digital radio: Wavescribe has no reader for it
BUSY = "busy"  # every receiver that could go is writing kept channels
COMPETE = "compete"  # its window is one more than the free receivers

# An 8-bit converter spans about 48 dB: a channel this far under the strongest signal in its
# window may be lost under the noise of the gain that signal needs (one gain per receiver).
SPREAD_DB = 40.0
WEAK_DB = 10.0  # a channel measured this little over its noise is heard weakly

Interval = tuple[int, int]


def feasible(k: Kept, r: Receiver) -> list[Interval]:
    """The centres from which receiver ``r`` would hold ``k`` (``writer.covers``), within its
    tuner's range: sorted, apart, whole Hz."""
    rate = r.sample_rate
    if k.whole:
        reach = rate / 2 - NARROW_MARGIN_HZ - k.width_hz / 2
        parts = [(max(k.freqs) - reach, min(k.freqs) + reach)]
    else:
        reach = rate / 2 - margin_hz(k.mode)
        parts = []
        for f in k.freqs:
            if k.mode == "wfm":
                parts.append((f - reach, f + reach))
            else:  # off the centre's spike by a whole channel width (``dsp.off_centre``)
                w = k.width_hz
                parts += [(f - reach, f - w), (f + w, f + reach)]
    lo, hi = r.tunes
    clipped = [(max(math.ceil(a), lo), min(math.floor(b), hi)) for a, b in parts]
    return _union([p for p in clipped if p[0] <= p[1]])


def _union(parts: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(parts):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def _intersect(x: list[Interval], y: list[Interval]) -> list[Interval]:
    out = []
    for a, b in x:
        for c, d in y:
            if max(a, c) <= min(b, d):
                out.append((max(a, c), min(b, d)))
    return _union(out)


def _holds(parts: list[Interval], c: int) -> bool:
    return any(a <= c <= b for a, b in parts)


def usual(k: Kept) -> float:
    """Where a receiver usually sits for ``k`` alone: a little below it, clear of the spike;
    for a band kept whole, about its middle."""
    if k.whole:
        return (min(k.freqs) + max(k.freqs)) / 2
    return k.freqs[0] - OFF_CENTRE_HZ[k.mode]


def window_for(kept: Sequence[Kept], r: Receiver) -> int | None:
    """A centre from which receiver ``r`` holds every one of ``kept``, as near their usual
    places as it goes (on a whole kHz where one fits), or None if there is none."""
    if not kept:
        return None
    parts = feasible(kept[0], r)
    for k in kept[1:]:
        parts = _intersect(parts, feasible(k, r))
    if not parts:
        return None
    target = sum(usual(k) for k in kept) / len(kept)
    best: int | None = None
    for a, b in parts:
        c = min(max(round(target), a), b)
        grid = [g for g in (math.floor(c / 1e3) * 1000, math.ceil(c / 1e3) * 1000) if a <= g <= b]
        if grid:  # a whole kHz, where one fits
            c = min(grid, key=lambda g: abs(g - target))
        if best is None or abs(c - target) < abs(best - target):
            best = c
    return best


@dataclass(frozen=True)
class Placement:
    """A receiver and the centre it goes to (or stays at: ``moves`` False), with the channels
    of the set it then hears. ``outshone``: those measured ``SPREAD_DB`` or more under the
    strongest signal known in that window; ``weak``: those measured under ``WEAK_DB`` over
    their noise."""

    receiver: str
    centre_hz: int
    channels: list[str]
    moves: bool = True
    outshone: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Competition:
    """More windows than free receivers: every window the set needs, in the order the first of
    each was chosen, and how many receivers are free. The first ``free`` are placed."""

    groups: list[list[str]]
    free: int


@dataclass(frozen=True)
class Cover:
    placements: list[Placement]
    unplaced: dict[str, str]  # channel → reason
    competition: Competition | None = None


def cover(
    chosen: Sequence[Kept],
    receivers: Sequence[Receiver],
    *,
    kept: Sequence[Kept] = (),
    others: Sequence[tuple[int, float]] = (),
) -> Cover:
    """Where to place the receivers so that ``chosen`` (in the order the user chose them) is
    heard. Receivers writing ``kept`` channels stay where they are; a channel one of them
    already holds needs no move. ``others`` are signals measured in the sky (frequency, dBFS),
    for saying when a channel shares a window with something much stronger."""
    unplaced: dict[str, str] = {}
    rest: list[Kept] = []
    already: dict[str, list[str]] = {}
    for k in chosen:
        if k.privacy == "digital":
            unplaced[k.id] = DIGITAL
        elif not k.confirmed:
            unplaced[k.id] = CONFIRM
        elif not any(feasible(k, r) for r in receivers):
            unplaced[k.id] = OUT_OF_RANGE
        elif holder := next((r for r in receivers if r.free and covers(r, k)), None):
            already.setdefault(holder.id, []).append(k.id)
        else:
            rest.append(k)

    writing = {a.receiver for a in assign(kept, receivers).values() if isinstance(a, Written)}
    free = [r for r in receivers if r.free and r.id not in writing and r.id not in already]
    groups = _groups(rest, free[0] if free else None)
    placed = groups[: len(free)]
    competition = Competition([[k.id for k in g] for g in groups], len(free)) if (
        len(groups) > len(free) and free) else None  # fmt: skip
    for g in groups[len(placed) :]:
        for k in g:
            unplaced[k.id] = COMPETE if free else BUSY

    by_id = {k.id: k for k in chosen}
    placements = []
    for rid, ids in already.items():
        r = next(r for r in receivers if r.id == rid)
        placements.append(_placement(r, r.center_hz, [by_id[i] for i in ids], others, False))
    for r, g in zip(free, placed, strict=False):
        c = window_for(g, r)
        if c is None:  # a receiver unlike the first: its window is narrower
            for k in g:
                unplaced[k.id] = COMPETE
            continue
        placements.append(_placement(r, c, g, others, True))
    return Cover(placements, unplaced, competition)


def _groups(items: list[Kept], r: Receiver | None) -> list[list[Kept]]:
    """The fewest windows (see the module's words) that hold ``items``, as receiver ``r`` would
    be placed: each window's channels, the windows in the order of their first-chosen channel."""
    if r is None or not items:
        return [[k] for k in items]
    parts = {k.id: feasible(k, r) for k in items}
    left = [k for k in items if parts[k.id]]
    groups: list[list[Kept]] = [[k] for k in items if not parts[k.id]]  # only others reach it
    while left:
        first = min(left, key=lambda k: parts[k.id][-1][1])  # the interval that ends first
        end = parts[first.id][-1][1]
        points = {b for k in left for _, b in parts[k.id] if b <= end} | {
            a for k in left for a, _ in parts[k.id] if a <= end
        }
        points = {c for c in points if _holds(parts[first.id], c)}
        # The most channels; among as many, the place nearest the end, as with no holes.
        best = max(points, key=lambda c: (sum(_holds(parts[k.id], c) for k in left), c))
        group = [k for k in left if _holds(parts[k.id], best)]
        groups.append(group)
        left = [k for k in left if k not in group]
    order = {k.id: i for i, k in enumerate(items)}
    return sorted(groups, key=lambda g: min(order[k.id] for k in g))


def _placement(r: Receiver, c: int, group: Sequence[Kept],
               others: Sequence[tuple[int, float]], moves: bool) -> Placement:  # fmt: skip
    levels = [db for f, db in others if abs(f - c) <= r.sample_rate / 2]
    levels += [k.strength_db for k in group if k.strength_db is not None]
    loudest = max(levels, default=None)
    outshone = [k.id for k in group if k.strength_db is not None and loudest is not None
                and loudest - k.strength_db >= SPREAD_DB]  # fmt: skip
    weak = [k.id for k in group if k.snr_db is not None and k.snr_db < WEAK_DB]
    return Placement(r.id, c, [k.id for k in group], moves, outshone, weak)
