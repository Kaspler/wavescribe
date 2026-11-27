# SPDX-License-Identifier: Apache-2.0
"""A watch with an open range and wide signals in it: a broadcast station is noted, once,
and never caught; a loud talker on a 25 kHz channel is caught, and never noted. Synthetic IQ."""

from __future__ import annotations

import pytest

from tests.synthetic_iq import Clicks, Line, Scene, Tx, watch
from wavescribe.dsp.activity import Activity
from wavescribe.dsp.openwatch import OpenBand

S = Scene(1_200_000, 445_900_000)
WEAK, STRONG = -330_000, 300_000  # where the two stations are, from the centre
TALKER = STRONG - 120_000  # a walkie-talkie beside the strong one
LOUD = {8: -120_000, 30: -30_000}  # loud talkers on 25 kHz channels, by how strong (dB)


def station(offset_hz: float, level_db: float, start_s: float, stop_s: float, **kw: float) -> Tx:
    """A broadcast FM station: 75 kHz deviation, ``level_db`` over the noise in 200 kHz."""
    amp = S.amp(level_db, 200_000)
    return Tx(offset_hz, start_s, stop_s, amp, mode="wfm", deviation_hz=75_000.0, **kw)


def talkers() -> list[Tx]:
    loud = [
        Tx(f, 2.5, 6.0, S.amp(level, 25_000), deviation_hz=5_000.0, limit=60.0, seed=level)
        for level, f in LOUD.items()
    ]
    return [*loud, Tx(TALKER, 3.0, 6.0, S.amp(8), seed=5)]


def near(events: list[Activity], offset_hz: float, within_hz: float = 100_000.0) -> list[Activity]:
    return [e for e in events if abs(e.freq_hz - S.hz(offset_hz)) <= within_hz]


def kinds(events: list[Activity]) -> list[str]:
    return sorted(e.kind for e in events)


@pytest.fixture(scope="module")
def arriving() -> list[Activity]:
    """Two stations that come on at 1.5 s, one at 3 dB and one at 30 dB, with talkers about."""
    stations = [station(WEAK, 3, 1.5, 8.0, seed=1), station(STRONG, 30, 1.5, 8.0, seed=2)]
    return watch(S.detector(), S.air(8.0, stations + talkers()))


@pytest.fixture(scope="module")
def from_the_start() -> list[Activity]:
    """The same with the stations there before the watch begins, at 6 dB and at 30 dB."""
    stations = [station(WEAK, 6, 0.0, 8.0, seed=1), station(STRONG, 30, 0.0, 8.0, seed=2)]
    return watch(S.detector(), S.air(8.0, stations + talkers()))


@pytest.mark.parametrize("where", [WEAK, STRONG])
@pytest.mark.parametrize("scene", ["arriving", "from_the_start"])
def test_a_broadcast_station_is_noted_once_and_never_caught(
    scene: str, where: int, request: pytest.FixtureRequest
) -> None:
    events = near(request.getfixturevalue(scene), where, 90_000)
    [noted] = [e for e in events if e.kind == "noted"]
    assert noted.channel is None and noted.end is None and noted.track
    assert abs(noted.freq_hz - S.hz(where)) < 30_000
    assert noted.width_hz >= 20_000
    if scene == "arriving":
        assert S.seconds(noted.start) == pytest.approx(1.5, abs=0.5)
    # What was taken for a transmission before the station showed itself was taken back.
    started = {e.track for e in events if e.kind == "start"}
    assert started == {e.track for e in events if e.kind == "absorbed"}
    assert not [e for e in events if e.kind in ("end", "short", "gone")]


def test_a_strong_station_measures_about_as_wide_as_it_is(arriving: list[Activity]) -> None:
    [noted] = [e for e in near(arriving, STRONG, 90_000) if e.kind == "noted"]
    assert 60_000 <= noted.width_hz <= 200_000


@pytest.mark.parametrize("scene", ["arriving", "from_the_start"])
def test_a_walkie_talkie_beside_a_station_is_still_caught(
    scene: str, request: pytest.FixtureRequest
) -> None:
    events = near(request.getfixturevalue(scene), TALKER, 3_000)
    assert kinds(events) == ["end", "start"]
    assert abs(events[0].freq_hz - S.hz(TALKER)) <= 100
    assert S.seconds(events[0].start) == pytest.approx(3.0, abs=0.15)


@pytest.mark.parametrize("level", [8, 30])
@pytest.mark.parametrize("scene", ["arriving", "from_the_start"])
def test_a_loud_talker_at_full_deviation_on_a_25_khz_channel_is_caught_and_never_noted(
    scene: str, level: int, request: pytest.FixtureRequest
) -> None:
    events = near(request.getfixturevalue(scene), LOUD[level], 20_000)
    assert kinds(events) == ["end", "start"]
    assert abs(events[0].freq_hz - S.hz(LOUD[level])) <= 1_000
    assert 6_000 <= events[-1].width_hz <= 16_000  # wide for a voice, and narrowband still
    assert S.seconds(events[-1].end - events[-1].start) == pytest.approx(3.5, abs=0.5)


@pytest.mark.parametrize("silent", [2.0, 8.0])
def test_a_station_that_is_a_bare_carrier_first_is_noted_when_it_speaks(silent: float) -> None:
    """Two seconds of carrier is a candidate still, eight a known line by then."""
    events = watch(
        S.detector(), S.air(silent + 5.5, [station(STRONG, 15, 1.5, silent + 5.5, silent_s=silent)])
    )
    [noted] = [e for e in events if e.kind == "noted"]
    assert abs(noted.freq_hz - S.hz(STRONG)) < 15_000
    started = {e.track for e in events if e.kind == "start"}
    assert started == {e.track for e in events if e.kind == "absorbed"}
    assert not [e for e in events if e.kind in ("end", "short")]


def test_a_station_that_goes_is_reported_gone() -> None:
    detector = S.detector()
    events = watch(detector, S.air(14.0, [station(STRONG, 20, 1.5, 6.0)]))
    assert kinds(events) == ["gone", "noted"]
    noted, gone = events
    assert (gone.track, gone.freq_hz, gone.start) == (noted.track, noted.freq_hz, noted.start)
    assert S.seconds(gone.end) == pytest.approx(6.0, abs=0.2)
    assert detector.open.noted == []


def test_what_narrow_means_is_the_callers_to_say_for_each_part_of_the_range() -> None:
    """Where only 12.5 kHz channels are, a carrier that ranges 5 kHz either way is no
    narrowband transmission; where nothing is said, it is."""
    strict = OpenBand(S.hz(-400_000), S.hz(-100_000), narrow_hz=7_000.0)
    loud = [
        Tx(f, 1.5, 5.0, S.amp(20, 25_000), deviation_hz=5_000.0, limit=60.0, seed=3)
        for f in (-250_000, 250_000)
    ]
    events = watch(S.detector(open_bands=[strict]), S.air(6.5, loud))
    assert kinds(near(events, 250_000, 20_000)) == ["end", "start"]
    there = near(events, -250_000, 20_000)
    assert "noted" in kinds(there) and "end" not in kinds(there)


@pytest.mark.parametrize("per_s", [8, 20])
def test_clicks_are_no_wide_signal_beside_lines_or_alone(per_s: int) -> None:
    """A click lifts the whole window for a frame, unevenly, and lines stay where they are:
    neither is a signal that came, however often it clicks."""
    scene = Scene(1_200_000, 446_000_000)
    junk = [
        Line(-420_000, scene.line(22), start_s=1.0),
        Line(-419_300, scene.line(22), start_s=1.0),
        Line(60_000, scene.line(25), drift_hz_s=300.0),
        Clicks(per_s, 0.05),
    ]
    detector = scene.detector()
    assert watch(detector, scene.air(15.0, junk)) == []
    assert detector.open.noted == []
