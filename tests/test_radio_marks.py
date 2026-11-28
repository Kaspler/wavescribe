# SPDX-License-Identifier: Apache-2.0
"""Marks (radio/marks.py): places where a detector saw something, kept across looks; when one
is steady, a ghost or quiet now; what may be written, by class."""

from __future__ import annotations

import itertools

import pytest

from tests.fake_world import World
from wavescribe.config import SdrDevice
from wavescribe.dsp.wfm import iq_from_u8
from wavescribe.radio import marks as mk
from wavescribe.radio.ladder import Shape, Sound
from wavescribe.radio.marks import (
    AM,
    BACK,
    GHOST,
    NARROW,
    NEW,
    QUIET,
    STEADY,
    WIDE,
    Change,
    Look,
    Marks,
    snapped,
)
from wavescribe.radio.receiver import ReceiverSession

MHZ = 1_000_000
FM = 92.4 * MHZ
WINDOW = (91.0 * MHZ, 93.4 * MHZ)


def noted(marks: Marks, freq: float = FM, t: float = 0.0, snr: float = 30.0) -> list[Change]:
    return marks.seen("a", "noted", freq, 180_000, snr, t, None, 0)


def look(marks: Marks, t: float, *wide: float, deaf: bool = False) -> list[Change]:
    return marks.look(Look("a", *WINDOW, t, tuple((f, 180_000.0) for f in wide), deaf))


def talk(marks: Marks, freq: float, t: float, length: float = 2.0, track: int = 1) -> None:
    marks.seen("b", "start", freq, 3_000, 20.0, t, None, track)
    marks.seen("b", "end", freq, 3_000, 20.0, t, t + length, track)


def test_a_station_is_a_new_mark_at_its_channel_and_the_same_mark_after() -> None:
    marks = Marks("cept")
    (change,) = noted(marks, FM + 2_100)  # measured a little off its 100 kHz raster
    m = marks.marks[change.mark]
    assert change.what == NEW and m.freq_hz == 92_400_000 and m.kind == WIDE
    assert (m.privacy, m.band, m.state, m.new) == ("broadcast", "fm", "here", True)
    assert noted(marks, FM - 3_000, t=1.0) == [] and len(marks.marks) == 1


@pytest.mark.parametrize(
    ("measured", "place"),
    [
        (446_031_900, 446_031_250),  # within a quarter channel of PMR446 channel 3
        (446_040_000, 446_040_000),  # between two channels: the nearest kHz
        (433_920_400, 433_920_000),  # a scanning step is no channel: the nearest kHz
        (121_508_000, 121_508_333),  # airband's 8.33 kHz channels
        (300_123_456, 300_123_000),  # no plan lists it
    ],
)
def test_a_marks_place(measured: int, place: int) -> None:
    from wavescribe.radio import bandplan

    assert snapped(measured, bandplan.lookup(measured, "cept"), NARROW) == place


def test_sightings_within_two_khz_are_one_mark_and_further_another() -> None:
    marks = Marks("cept")
    talk(marks, 300_100_000, 0.0)
    talk(marks, 300_101_500, 10.0, track=2)
    talk(marks, 300_104_000, 20.0, track=3)
    assert sorted(m.freq_hz for m in marks.marks.values()) == [300_100_000, 300_104_000]


def test_a_station_gone_is_a_ghost_after_three_looks_over_thirty_seconds() -> None:
    marks = Marks("cept")
    noted(marks)
    for t in (15, 30, 45):
        look(marks, t, FM)
    assert look(marks, 60) == [] and look(marks, 75) == []  # two looks missed
    (ghost,) = look(marks, 90)  # the third, 30 s after the first
    assert ghost.what == GHOST and marks.marks[ghost.mark].state == "ghost"
    m = marks.marks[ghost.mark]
    assert (m.seen_looks, m.missed_looks) == (3, 3)
    (back,) = look(marks, 105, FM)
    assert back.what == BACK and m.state == "here"


def test_three_quick_looks_are_not_enough() -> None:
    marks = Marks("cept")
    noted(marks)
    assert [c.what for t in (5, 10, 15, 20, 25, 30) for c in look(marks, t)] == []
    assert [c.what for c in look(marks, 35)] == [GHOST]  # 30 s after the first miss


def test_a_deaf_window_never_makes_a_ghost() -> None:
    marks = Marks("cept")
    noted(marks)
    for t in range(15, 300, 15):
        assert look(marks, t, deaf=True) == []
    (m,) = marks.marks.values()
    assert (m.missed_looks, m.state) == (0, "here")


def test_a_weak_station_never_becomes_a_ghost() -> None:
    marks = Marks("cept")
    noted(marks, snr=3.0)
    assert [c for t in range(15, 300, 15) for c in look(marks, t)] == []
    (m,) = marks.marks.values()
    assert m.missed_looks == 19 and m.state == "seen"


def test_only_a_window_that_holds_the_whole_station_judges_it() -> None:
    marks = Marks("cept")
    noted(marks, 93.38 * MHZ)  # half of it beyond the window's top
    assert [c for t in range(15, 120, 15) for c in look(marks, t)] == []


def test_a_walkie_talkie_channel_is_quiet_now_never_a_ghost() -> None:
    marks = Marks("cept")
    marks.seen("b", "start", 446_031_300, 3_000, 15.0, 10.0, None, 7)
    (m,) = marks.marks.values()
    assert (m.kind, m.privacy, m.state, m.freq_hz) == (NARROW, "private", "active", 446_031_250)
    assert marks.seen("b", "end", 446_031_300, 3_000, 15.0, 10.0, 13.0, 7) == [Change(QUIET, m.id)]
    assert (m.state, m.last_active) == ("quiet", 13.0)
    look_there = Look("b", 445 * MHZ, 447 * MHZ, 100.0, (), False)
    assert all(marks.look(look_there) == [] for _ in range(10)) and m.state == "quiet"


def test_a_carrier_in_an_am_band_and_one_absorbed_into_a_wide_signal() -> None:
    marks = Marks("cept")
    marks.seen("c", "short", 120_500_200, 1_000, 20.0, 5.0, 5.3, 1)
    (m,) = marks.marks.values()
    assert (m.kind, m.privacy, m.freq_hz) == (AM, "utility", 120_500_000)
    marks.seen("a", "start", FM + 50_000, 3_000, 9.0, 8.0, None, 2)
    marks.seen("a", "absorbed", FM + 50_000, 3_000, 9.0, 8.0, None, 2)
    assert list(marks.marks) == [m.id]


def test_steady_after_looks_ten_minutes_apart_or_known_from_the_catalogue() -> None:
    marks = Marks("cept")
    noted(marks)
    changes = [c.what for t in range(0, 600, 60) for c in look(marks, t, FM)]
    assert changes == []
    assert [c.what for c in look(marks, 600, FM)] == [STEADY]
    known = Marks("cept", known=[92_400_000])
    assert noted(known) == []  # not new here
    (m,) = known.marks.values()
    assert m.steady and not m.new


def test_what_may_be_written_follows_each_classs_text() -> None:
    marks = Marks("cept")
    noted(marks, t=0.0)
    talk(marks, 446_031_250, 0.0)  # private: nothing kept by default
    marks.seen("c", "short", 120_500_000, 1_000, 20.0, 0.0, 0.3, 5)  # utility: three days
    kinds = lambda now: sorted(r["kind"] for r in marks.rows(now))  # noqa: E731
    assert kinds(3600.0) == [AM, WIDE]
    assert kinds(4 * mk.DAY_S) == [WIDE]
    assert [r["kind"] for r in marks.rows(0.0, {"private": 1.0})] == [NARROW]


def test_marks_read_back_are_seen_before_never_new_nor_quiet_now() -> None:
    marks = Marks("cept")
    noted(marks)
    marks.seen("c", "short", 120_500_000, 1_000, 20.0, 1.0, 1.3, 5)
    marks.describe(1, shape=Shape("wide_fm", 160_000, True, 1.0, 1, 3.0), name="VALLEY")
    again = Marks.restore(marks.rows(10.0), region="cept")
    wide, carrier = again.marks.values()
    assert (wide.new, wide.state, wide.name, wide.looks_like, wide.rung) == (
        False, "seen", "VALLEY", "wide_fm", 4)  # fmt: skip
    assert carrier.state == "seen" and carrier.last_active == 1.3
    assert [c.what for c in noted(again, t=20.0)] == [] and wide.state == "here"
    talk(again, 446_031_250, 30.0)
    assert max(again.marks) == 3  # ids go on from those read back


def test_dismissed_marks_are_left_out_and_rungs_attach() -> None:
    marks = Marks("cept")
    noted(marks)
    talk(marks, 446_031_250, 0.0)
    marks.dismiss(1)
    assert [m.freq_hz for m in marks.between(400 * MHZ, 500 * MHZ)] == [446_031_250]
    assert marks.between(0, 1e12) == [marks.marks[2]]
    assert len(marks.between(0, 1e12, dismissed=True)) == 2
    marks.describe(2, shape=Shape("narrow_fm", 5_000, False, 0.2, 3, None), sound=Sound("speech"))
    assert (marks.marks[2].looks_like, marks.marks[2].rung) == ("narrow_fm", 3)


def test_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mk, "MARKS_MAX", 5)
    marks = Marks("cept")
    noted(marks)
    for i in range(8):
        talk(marks, 300_000_000 + i * 10_000, float(i), track=i + 1)
    assert len(marks.marks) == 5 and 1 in marks.marks  # the station, held now, stays
    assert min(m.last_seen for m in marks.marks.values() if m.kind == NARROW) == 4.0 + 2.0


class Dongle:
    def __init__(self, center_hz: int) -> None:
        self.config = SdrDevice("a", "SIM00001", center_hz, 20.0)
        self.load = 0.0


def test_through_a_session_a_station_is_marked_within_a_second_and_looked_at() -> None:
    station = {"kind": "wfm", "mhz": 92.4, "level": 0.1}
    talking = {"kind": "nfm", "mhz": 92.0571, "voice": 3, "level": 0.01}
    sky = {"transmitters": [station, talking]}
    tuned = World(sky).tune(92_000_000, 2_400_000)
    session = ReceiverSession(Dongle(92_000_000), region="cept", look_s=5.0)  # type: ignore[arg-type]
    marks, looks, when, landed = Marks("cept"), [], {}, []
    now = 1000.0

    def seen(receiver: str, s: object) -> None:
        for c in marks.take(receiver, s):
            when.setdefault((c.what, c.mark), now)

    session.on_seen = seen
    session.on_look = lambda lk: (looks.append(lk), marks.look(lk))
    session.on_land = lambda r, t: (landed.append(t), marks.landed(r, t))
    for i in range(round(12.0 / 0.256)):
        now = 1000.0 + 0.256 * (i + 1)
        session._feed(iq_from_u8(tuned.u8()), now, i == 0)
    wide = next(m for m in marks.marks.values() if m.kind == WIDE)
    talking = next(m for m in marks.marks.values() if m.kind == NARROW)
    assert wide.freq_hz == 92_400_000 and when[(NEW, wide.id)] - 1000.0 <= 1.2
    assert talking.freq_hz == 92_057_000 and talking.privacy == "broadcast"
    assert looks[0].t - 1000.0 < 2.0
    assert [round(b.t - a.t) for a, b in itertools.pairwise(looks)] == [5, 5]
    assert looks[0].lo_hz == 92_000_000 - 1_140_000 and looks[0].wide
    assert wide.seen_looks == 3 and wide.state == "here" and landed == [1000.0]


def test_a_receiver_that_lands_elsewhere_lets_go_of_what_it_held() -> None:
    marks = Marks("cept")
    noted(marks)
    marks.seen("a", "start", 92_057_000, 3_000, 12.0, 5.0, None, 4)
    noted_by_b = marks.seen("b", "noted", FM, 180_000, 30.0, 6.0, None, 0)
    assert noted_by_b == []
    wide, talking = marks.marks.values()
    assert marks.landed("a", 9.0) == [Change(QUIET, talking.id)]
    assert (talking.state, talking.last_active) == ("quiet", 9.0)
    assert wide.held_by == {"b"} and wide.state == "here"
    marks.landed("b", 10.0)
    assert wide.state == "seen"
