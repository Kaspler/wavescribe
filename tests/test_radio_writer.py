# SPDX-License-Identifier: Apache-2.0
"""Which receiver writes each kept channel (radio/writer.py): the rule, the difference from what
runs, and what a move changes."""

from __future__ import annotations

import pytest

from wavescribe.radio.writer import (
    ANY,
    CONFIRM,
    HELD,
    NO_RECEIVER,
    OFF,
    TURN,
    UNPLUGGED,
    Kept,
    Receiver,
    Waiting,
    Written,
    assign,
    covers,
    move,
    reconcile,
)

MHZ = 1_000_000


def fm(kid: str, mhz: float, **kw: object) -> Kept:
    return Kept(kid, (round(mhz * MHZ),), "wfm", **kw)  # type: ignore[arg-type]


def nfm(kid: str, mhz: float, **kw: object) -> Kept:
    return Kept(kid, (round(mhz * MHZ),), "nfm", **kw)  # type: ignore[arg-type]


def rx(rid: str, mhz: float, **kw: object) -> Receiver:
    return Receiver(rid, round(mhz * MHZ), **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kept", "receiver", "held"),
    [
        (fm("edge-in", 93.08), rx("a", 92.0), True),  # 1.08 MHz: the WFM window's edge
        (fm("edge-out", 93.081), rx("a", 92.0), False),
        (fm("on-centre", 92.0), rx("a", 92.0), True),  # broadcast FM is clear of the spike
        (nfm("narrow-edge", 447.14), rx("b", 446.0), True),  # 1.14 MHz for a narrow channel
        (nfm("on-spike", 446.005), rx("b", 446.0), False),  # within a channel width of it
        (nfm("off-spike", 446.0125), rx("b", 446.0), True),
        (Kept("second", (90_000_000, 92_500_000)), rx("a", 92.0), True),  # either frequency
        (Kept("band", (445_000_000, 447_140_000), "nfm", whole=True), rx("b", 446.0), False),
        (Kept("band", (445_100_000, 446_900_000), "nfm", whole=True), rx("b", 446.0), True),
    ],
)
def test_covers(kept: Kept, receiver: Receiver, held: bool) -> None:
    assert covers(receiver, kept) is held


@pytest.mark.parametrize(
    ("kept", "receivers", "expected"),
    [
        ([fm("v", 92.4, on=False)], [rx("a", 92.0)], Waiting(OFF)),
        ([fm("v", 92.4, confirmed=False)], [rx("a", 92.0)], Waiting(CONFIRM)),
        ([fm("v", 97.4)], [rx("a", 92.0)], Waiting(NO_RECEIVER)),
        ([fm("v", 92.4)], [rx("a", 92.0, held_by="another program")], Waiting(HELD)),
        ([fm("v", 92.4)], [rx("a", 92.0, connected=False)], Waiting(UNPLUGGED)),
        # Held and unplugged both: the program is the reason that can be acted on.
        ([fm("v", 92.4)], [rx("a", 92.0, connected=False), rx("b", 92.2, held_by="x")],
         Waiting(HELD)),
        # Two windows hold it: the receiver there longest writes it, once.
        ([fm("v", 92.4)], [rx("b", 92.3, since=5.0), rx("a", 92.0, since=9.0)], Written("b")),
        ([fm("v", 92.4)], [rx("a", 92.0, since=1.0), rx("b", 92.3, since=1.0)], Written("a")),
        # ...unless it is pinned to the other one.
        ([fm("v", 92.4, pin="a")], [rx("b", 92.3, since=5.0), rx("a", 92.0, since=9.0)],
         Written("a")),
        # A free receiver beats one another program has.
        ([fm("v", 92.4)], [rx("a", 92.0, held_by="x"), rx("b", 92.3, since=9.0)], Written("b")),
    ],
)  # fmt: skip
def test_assign(kept: list[Kept], receivers: list[Receiver], expected: object) -> None:
    assert assign(kept, receivers)["v"] == expected


def test_beyond_what_the_mac_writes_at_once_pinned_then_priority_then_newest_go_first() -> None:
    kept = [
        fm("old-low", 92.1, priority=5, kept_at=1.0),
        fm("new-low", 92.2, priority=5, kept_at=2.0),
        fm("high", 92.3, priority=1, kept_at=0.0),
        fm("pinned", 92.4, priority=9, pin=ANY),
        fm("off", 92.5, on=False),
    ]
    got = assign(kept, [rx("a", 92.0)], writers_max=3)
    assert got == {
        "pinned": Written("a"),
        "high": Written("a"),
        "new-low": Written("a"),
        "old-low": Waiting(TURN),
        "off": Waiting(OFF),
    }


def test_reconcile_starts_stops_and_repoints() -> None:
    receivers = [rx("a", 92.0), rx("b", 98.0)]
    assignment = {
        "new": Written("a"),
        "same": Written("a"),
        "moved-here": Written("b"),
        "replaced-in-window": Written("a"),
        "left": Waiting(NO_RECEIVER),
        "never": Waiting(OFF),
    }
    running = {
        "same": ("a", 92_000_000),
        "moved-here": ("a", 92_000_000),  # another receiver writes it now
        "replaced-in-window": ("a", 91_500_000),  # its receiver was moved, and still holds it
        "left": ("a", 92_000_000),
        "forgotten": ("b", 98_000_000),  # no longer kept at all
    }
    changes = reconcile(assignment, running, receivers)
    assert changes.start == ["new"]
    assert sorted(changes.stop) == ["forgotten", "left"]
    assert changes.repoint == {"moved-here": "b", "replaced-in-window": "a"}


KEPT = [fm("v", 92.4), fm("h", 92.9), fm("r", 97.5), nfm("pmr", 446.03125, privacy="private")]
PLACES = [rx("a", 92.0), rx("b", 446.1), rx("c", 120.3)]


def test_moving_a_receiver_stops_what_it_left_and_starts_what_it_reaches_and_back() -> None:
    there = move(KEPT, PLACES, "a", 98_000_000, now=10.0)
    assert there.moved and (there.was, there.to) == (92_000_000, 98_000_000)
    assert there.stopped == ["v", "h"] and there.started == ["r"]
    # C writes nothing: from one place it would write both stations A left.
    assert there.instead is not None and there.instead[0] == "c"
    c = rx("c", there.instead[1] / MHZ)
    assert covers(c, KEPT[0]) and covers(c, KEPT[1])
    moved = [rx("a", 98.0, since=10.0), *PLACES[1:]]
    back = move(KEPT, moved, "a", 92_000_000, now=20.0)  # Undo
    assert back.stopped == ["r"] and back.started == ["v", "h"]
    assert back.assignment == assign(KEPT, PLACES)


def test_a_channel_another_window_holds_neither_stops_nor_starts() -> None:
    receivers = [rx("a", 92.0, since=0.0), rx("c", 92.6, since=5.0)]
    there = move(KEPT, receivers, "a", 98_000_000, now=10.0)
    assert there.stopped == [] and there.started == ["r"]
    assert there.assignment["v"] == Written("c")


def test_a_pinned_channel_refuses_a_move_unless_forced() -> None:
    kept = [fm("v", 92.4, pin="a"), fm("h", 92.9)]
    refused = move(kept, PLACES, "a", 98_000_000)
    assert not refused.moved and refused.refused == ["v"] and refused.to == 92_000_000
    assert refused.stopped == [] and refused.assignment == assign(kept, PLACES)
    forced = move(kept, PLACES, "a", 98_000_000, force=True)
    assert forced.moved and forced.stopped == ["v", "h"]
    # Pinned to any receiver: B can't be moved off it either.
    anywhere = [nfm("pmr", 446.03125, pin=ANY)]
    assert move(anywhere, PLACES, "b", 450_000_000).refused == ["pmr"]


def test_a_move_is_clamped_to_the_tuners_range() -> None:
    assert move(KEPT, PLACES, "a", 10_000_000).to == 24_000_000
