# SPDX-License-Identifier: Apache-2.0
"""Where to look around: the groups of bands a person chose, as the bands a scan visits.

The band plan puts every band in a plain group ("Aircraft", "Walkie-talkies and business
radio"; ``bandplan.groups``). A group is offered where the region has a band of it that a
receiver can tune. The scan visits a chosen group's bands only where their class needs no
confirmation or the person has confirmed it (``bandplan.POLICIES``, ``needs_ack``): ticking a
group never confirms a class, and a group of classes not confirmed is refused, not quietly
narrowed. What wasn't looked at is said by name.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from wavescribe.radio import bandplan
from wavescribe.radio.bandplan import TUNER_RANGE_HZ, Band, Group


@dataclass(frozen=True)
class Offer:
    """A group as the region has it: the bands a receiver can tune, their classes, and those
    of them that need the person's confirmation and don't have it."""

    group: Group
    bands: tuple[Band, ...]
    classes: tuple[str, ...]
    unconfirmed: tuple[str, ...]


@dataclass(frozen=True)
class Scope:
    """The bands to visit, lowest first, and the groups offered that won't be looked at."""

    bands: tuple[Band, ...]
    looked: tuple[str, ...]
    not_looked: tuple[str, ...]


def tunable(band: Band) -> bool:
    lo, hi = TUNER_RANGE_HZ
    return band.stop_hz >= lo and band.start_hz <= hi


def offered(region: str, confirmed: Iterable[str] = ()) -> list[Offer]:
    """The groups to choose from in ``region``, in the band plan's order: each with a band a
    receiver can tune there."""
    ok = set(confirmed)
    here = [b for b in bandplan.region_bands(region) if tunable(b)]
    out = []
    for g in bandplan.groups():
        mine = tuple(sorted((b for b in here if b.group == g.id), key=lambda b: b.start_hz))
        if not mine:
            continue
        classes = tuple(sorted({b.privacy for b in mine}, key=bandplan.STRICTNESS.index))
        unconfirmed = tuple(c for c in classes if _needs(c) and c not in ok)
        out.append(Offer(g, mine, classes, unconfirmed))
    return out


def default_groups(region: str) -> list[str]:
    """What a first look around covers: every offered group whose classes need no
    confirmation."""
    return [o.group.id for o in offered(region) if not any(_needs(c) for c in o.classes)]


def refused(
    groups: Iterable[str], region: str, confirmed: Iterable[str] = ()
) -> dict[str, tuple[str, ...]]:
    """Of the chosen ``groups``, those that hold a class needing a confirmation the person
    hasn't given, with those classes: a choice that must be refused, never narrowed. A group
    the region doesn't offer is an error."""
    offers = {o.group.id: o for o in offered(region, confirmed)}
    out = {}
    for g in groups:
        if g not in offers:
            raise ValueError(f"no group {g!r} to look at in region {region!r}")
        if offers[g].unconfirmed:
            out[g] = offers[g].unconfirmed
    return out


def scope(groups: Iterable[str], region: str, confirmed: Iterable[str] = ()) -> Scope:
    """The bands the chosen ``groups`` hold, lowest first, leaving out any band of a class
    that needs a confirmation not given (``refused`` says which); and the offered groups not
    looked at."""
    chosen = set(groups)
    refused(chosen, region, confirmed)  # unknown groups are an error
    ok = set(confirmed)
    visit: list[Band] = []
    looked: list[str] = []
    not_looked: list[str] = []
    for o in offered(region, ok):
        mine = [b for b in o.bands if not _needs(b.privacy) or b.privacy in ok]
        if o.group.id in chosen and mine:
            visit += mine
            looked.append(o.group.id)
        else:
            not_looked.append(o.group.id)
    return Scope(tuple(sorted(visit, key=lambda b: b.start_hz)), tuple(looked), tuple(not_looked))


def _needs(privacy: str) -> bool:
    return bandplan.POLICIES[privacy].needs_ack
