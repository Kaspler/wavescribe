# SPDX-License-Identifier: Apache-2.0
"""Placing receivers to hear a chosen set (radio/cover.py): the fewest windows, a centre for
each, and the plain reasons for what can't be placed."""

from __future__ import annotations

import itertools
import random

import pytest

from wavescribe.radio.cover import (
    BUSY,
    COMPETE,
    DIGITAL,
    OUT_OF_RANGE,
    Competition,
    _groups,
    cover,
    feasible,
    window_for,
)
from wavescribe.radio.writer import CONFIRM, Kept, Receiver, covers

MHZ = 1_000_000


def fm(kid: str, mhz: float, **kw: object) -> Kept:
    return Kept(kid, (round(mhz * MHZ),), "wfm", **kw)  # type: ignore[arg-type]


def nfm(kid: str, mhz: float, **kw: object) -> Kept:
    return Kept(kid, (round(mhz * MHZ),), "nfm", **kw)  # type: ignore[arg-type]


def rx(rid: str, mhz: float, **kw: object) -> Receiver:
    return Receiver(rid, round(mhz * MHZ), **kw)  # type: ignore[arg-type]


def fewest_windows(items: list[Kept], r: Receiver) -> int:
    """By trying every set of centres from the intervals' ends: the least number possible."""
    ends = sorted({e for k in items for part in feasible(k, r) for e in part})
    for n in range(1, len(items) + 1):
        for centres in itertools.combinations(ends, n):
            if all(any(a <= c <= b for c in centres for a, b in feasible(k, r)) for k in items):
                return n
    raise AssertionError("no cover")


@pytest.mark.parametrize("seed", range(40))
def test_broadcast_sets_get_the_fewest_windows(seed: int) -> None:
    rnd = random.Random(seed)
    items = [fm(f"s{i}", rnd.uniform(87.5, 108.0)) for i in range(rnd.randint(1, 7))]
    r = rx("a", 98.0)
    groups = _groups(items, r)
    assert len(groups) == fewest_windows(items, r)
    for g in groups:
        c = window_for(g, r)
        assert c is not None and all(covers(rx("a", c / MHZ), k) for k in g)


FM_SET = [fm("a", 95.0), fm("b", 88.1), fm("c", 91.5), fm("d", 88.9), fm("e", 92.0),
          fm("f", 89.9)]  # fmt: skip
FREE = [rx("x", 200.0), rx("y", 300.0), rx("z", 400.0)]


def test_three_windows_for_three_free_receivers() -> None:
    got = cover(FM_SET, FREE)
    assert got.unplaced == {} and got.competition is None
    assert [(p.receiver, sorted(p.channels)) for p in got.placements] == [
        ("x", ["a"]),
        ("y", ["b", "d", "f"]),
        ("z", ["c", "e"]),
    ]
    for p in got.placements:
        there = rx(p.receiver, p.centre_hz / MHZ)
        assert p.moves and p.centre_hz % 1000 == 0
        assert all(covers(there, k) for k in FM_SET if k.id in p.channels)


def test_with_two_free_receivers_the_groups_compete_in_the_order_they_were_chosen() -> None:
    got = cover(FM_SET, FREE[:2])
    assert got.competition == Competition([["a"], ["b", "d", "f"], ["c", "e"]], 2)
    assert got.unplaced == {"c": COMPETE, "e": COMPETE}
    assert [p.receiver for p in got.placements] == ["x", "y"]


def test_a_receiver_writing_kept_channels_stays_and_what_it_holds_needs_no_move() -> None:
    kept = [fm("kept", 92.2)]
    receivers = [rx("x", 92.0), rx("y", 300.0)]
    got = cover([fm("near", 92.5), fm("far", 99.0)], receivers, kept=kept)
    assert [(p.receiver, p.channels, p.moves) for p in got.placements] == [
        ("x", ["near"], False),
        ("y", ["far"], True),
    ]
    # Nothing free for the rest: every receiver is writing.
    busy = cover([fm("far", 99.0)], [rx("x", 92.0)], kept=kept)
    assert busy.unplaced == {"far": BUSY} and busy.competition is None


@pytest.mark.parametrize(
    ("item", "reason"),
    [
        (Kept("dab", (211_648_000,), "nfm", privacy="digital"), DIGITAL),
        (nfm("pmr", 446.03125, privacy="private", confirmed=False), CONFIRM),
        (nfm("hf", 14.2), OUT_OF_RANGE),  # below 24 MHz: not received
        (nfm("top", 1767.2), OUT_OF_RANGE),  # no centre up to 1766 MHz reaches it
    ],
)
def test_reasons_for_what_isnt_placed(item: Kept, reason: str) -> None:
    assert cover([item], FREE).unplaced == {item.id: reason}


def test_just_above_the_tuners_top_is_heard_from_its_top() -> None:
    (p,) = cover([nfm("top", 1766.9)], FREE).placements
    assert p.centre_hz <= 1_766_000_000 and covers(rx("x", p.centre_hz / MHZ), nfm("t", 1766.9))


def test_a_narrow_channel_goes_a_little_above_the_centre_clear_of_its_spike() -> None:
    assert window_for([nfm("ch", 446.03125)], rx("x", 100.0)) == 445_781_000
    # Two channels a step apart: neither may sit on the spike.
    pair = [nfm("one", 446.00625), nfm("two", 446.01875)]
    c = window_for(pair, rx("x", 100.0))
    assert c is not None and all(covers(rx("x", c / MHZ), k) for k in pair)


def test_a_band_kept_whole_goes_about_its_middle() -> None:
    band = Kept("pmr", (446_006_250, 446_193_750), "nfm", whole=True)
    assert window_for([band], rx("x", 100.0)) == 446_100_000
    too_wide = Kept("w", (440_000_000, 443_000_000), "nfm", whole=True)
    assert window_for([too_wide], rx("x", 100.0)) is None


def test_one_gain_per_receiver_a_channel_far_under_the_strongest_in_its_window_is_said() -> None:
    weak = nfm("weak", 446.05, strength_db=-62.0, snr_db=6.0)
    loud = nfm("loud", 446.5, strength_db=-30.0, snr_db=30.0)
    others = [(445_900_000, -8.0), (460_000_000, 0.0)]  # the second is outside the window
    (p,) = cover([weak, loud], FREE[:1], others=others).placements
    assert sorted(p.channels) == ["loud", "weak"]
    assert p.outshone == ["weak"]  # 54 dB under the -8 dBFS signal in its window
    assert p.weak == ["weak"]
