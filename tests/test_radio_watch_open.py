# SPDX-License-Identifier: Apache-2.0
"""A watch with an open range: transmissions between its channels and beyond them, the lines
a receiver makes by itself, and the channels reporting what they always did. Synthetic IQ."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tests.synthetic_iq import Clicks, Line, Scene, Tx, watch
from wavescribe.dsp.activity import Activity, ActivityDetector
from wavescribe.dsp.openwatch import OpenBand

S = Scene(1_200_000, 445_900_000)
SMALL = Scene(600_000, 446_000_000)  # the same bins from a quarter of the samples
PMR = tuple(446_006_250 + 12_500 * k for k in range(16))  # PMR446 channels 1 to 16


def off(freq_hz: float, scene: Scene = S) -> float:
    return freq_hz - scene.centre


def catches(events: list[Activity], near_hz: float, within_hz: float = 3_000.0) -> list[Activity]:
    """The transmissions that ended off the channels within ``within_hz`` of ``near_hz``."""
    return [
        e
        for e in events
        if e.kind == "end" and e.channel is None and abs(e.freq_hz - near_hz) <= within_hz
    ]


def timed(scene: Scene, detector: ActivityDetector, seconds: float, things: list) -> list:
    """Each event with the time (s) by which the detector had said it."""
    out, fed = [], 0
    for iq in scene.air(seconds, things):
        fed += len(iq)
        out += [(fed / scene.rate, e) for e in detector.feed(iq)]
    return out + [(fed / scene.rate, e) for e in detector.flush()]


def test_speech_between_the_channels_and_off_the_plan_is_caught_where_it_is() -> None:
    between = PMR[4] + 6_250  # halfway from channel 5 to channel 6
    beyond = PMR[15] + 137_500
    below = S.centre - 220_000
    places = (between, beyond, below)
    voices = [Tx(off(f), 1.5, 4.5, S.amp(8), seed=i) for i, f in enumerate(places)]
    events = watch(S.detector(PMR), S.air(6.5, voices))
    for f in places:
        [got] = catches(events, f)
        assert abs(got.freq_hz - f) <= 100
        assert S.seconds(got.start) == pytest.approx(1.5, abs=0.15)
        assert S.seconds(got.end) == pytest.approx(4.5, abs=0.3)
        assert 2_000 < got.width_hz < 10_000 and got.snr_db > 6
        [began] = [e for e in events if e.kind == "start" and e.track == got.track]
        assert began.channel is None and began.freq_hz == got.freq_hz and began.start == got.start
    assert len([e for e in events if e.kind == "end"]) == 3  # and nothing else, on or off


def test_a_carrier_a_drifting_line_a_blinking_line_and_clicks_are_no_transmission() -> None:
    junk = [
        Tx(-200_000, 1.0, 9.0, S.amp(15), speaks=False),  # keyed, and nobody speaks
        Line(60_000, S.line(25), drift_hz_s=300.0),
        Line(-80_000, S.line(25), period_s=2.0, on_s=0.7),
        Line(400_000, S.line(30)),
        Line(-420_000, S.line(22), start_s=1.0),
        Line(-419_300, S.line(22), start_s=1.0),  # two side by side that come up together
        Clicks(8, 0.05),
    ]
    detector = S.detector(PMR)
    assert watch(detector, S.air(10.0, junk)) == []
    assert detector.open.steady >= 3 and detector.open.lines_seen >= 2


def test_two_lines_two_bins_apart_that_take_turns_at_being_the_stronger_are_no_transmission() -> (
    None
):
    """One drifts toward the other, and the strongest bin goes from one to the other and
    back: both are there in every FFT, which a carrier that moves is not."""
    pair = [Line(50_126, SMALL.line(20)), Line(48_550, SMALL.line(20), drift_hz_s=20.0)]
    assert watch(SMALL.detector(), SMALL.air(20.0, pair)) == []


def test_a_carrier_that_stays_five_seconds_is_a_known_line_until_it_has_been_gone_thirty() -> None:
    detector = SMALL.detector()
    known = {}
    for k, iq in enumerate(SMALL.air(40.0, [Line(70_000, SMALL.line(20), 1.0, 8.0)])):
        assert detector.feed(iq) == []
        known[(k + 1) / 4] = detector.open.steady
    assert known[5.5] == 0  # not yet: it has been there four and a half seconds
    assert known[6.75] == 1
    assert known[37.5] == 1  # gone since 8 s, and still left out
    assert known[39.0] == 0


def test_two_transmissions_a_channel_apart_are_two_and_a_far_weaker_neighbour_is_none() -> None:
    a, b = -300_000, 100_000
    voices = [
        Tx(a, 1.5, 4.5, S.amp(20), seed=1),
        Tx(a + 12_500, 1.5, 4.5, S.amp(20), seed=2),
        Tx(b, 1.5, 4.5, S.amp(32), seed=3),
        Tx(b + 12_500, 2.5, 4.3, S.amp(9), seed=4),  # over 20 dB under the one beside it
    ]
    events = watch(S.detector(), S.air(6.5, voices))
    assert [abs(e.freq_hz - S.hz(a)) < 300 for e in catches(events, S.hz(a))] == [True]
    assert len(catches(events, S.hz(a + 12_500))) == 1
    assert [abs(e.freq_hz - S.hz(b)) < 300 for e in catches(events, S.hz(b))] == [True]
    assert len([e for e in events if e.kind == "end"]) == 3


def test_two_that_start_in_the_same_frame_six_kilohertz_apart_are_one_catch_the_stronger() -> None:
    a, b = -300_000, 100_000
    voices = [
        Tx(a, 1.5, 4.5, S.amp(20), seed=1),
        Tx(a + 6_000, 1.5, 4.5, S.amp(17), seed=2),
        Tx(b, 1.5, 4.5, S.amp(20), seed=3),  # and two as strong as each other
        Tx(b + 6_000, 1.5, 4.5, S.amp(20), seed=4),
    ]
    events = watch(S.detector(), S.air(6.5, voices))
    [stronger] = catches(events, S.hz(a), 10_000)
    assert abs(stronger.freq_hz - S.hz(a)) < 300
    equal = catches(events, S.hz(b + 3_000), 10_000)
    assert 1 <= len(equal) <= 2  # one of them or both, never neither
    assert all(
        min(abs(e.freq_hz - S.hz(b)), abs(e.freq_hz - S.hz(b + 6_000))) < 1_500 for e in equal
    )


def test_a_carrier_on_a_channel_is_the_channels_and_no_line() -> None:
    carriers = [
        Tx(off(PMR[2]), 1.5, 4.0, S.amp(15), speaks=False),
        Tx(off(PMR[9]) + 2_000, 1.5, 4.0, S.amp(15), speaks=False),  # a little off its channel
    ]
    detector = S.detector(PMR)
    events = watch(detector, S.air(6.0, carriers))
    assert sorted((e.kind, e.channel) for e in events) == [
        ("end", 2),
        ("end", 9),
        ("start", 2),
        ("start", 9),
    ]
    assert detector.open.steady == 0 and detector.open.lines_seen == 0


def test_where_a_carrier_is_the_signal_a_carrier_is_caught() -> None:
    am_band = OpenBand(S.hz(-400_000), S.hz(-100_000), carrier=True)
    things = [
        Tx(-300_000, 1.5, 4.5, S.amp(12, 8_000), speaks=False),
        Tx(-200_000, 1.5, 4.5, S.amp(12, 8_000), mode="am", seed=2),
        Tx(200_000, 1.5, 4.5, S.amp(12, 8_000), speaks=False),  # outside the band: a line
    ]
    events = watch(S.detector(open_bands=[am_band]), S.air(6.5, things))
    for f in (-300_000, -200_000):
        [got] = catches(events, S.hz(f))
        assert abs(got.freq_hz - S.hz(f)) <= 100
        assert S.seconds(got.start) == pytest.approx(1.5, abs=0.15)
    assert len(events) == 4


@pytest.fixture(scope="module")
def keyed_before_speaking() -> dict:
    """Carriers keyed at 2 s that say nothing for 6 s or for 30 s and then speak for 4 s, at
    8 dB and at 20 dB; two that never speak; and a line that was there from the start."""
    cases = {
        (6.0, 8): -210_000,
        (6.0, 20): -150_000,
        (30.0, 8): -90_000,
        (30.0, 20): 40_000,
    }
    things = [
        Tx(f, 2.0, 6.0 + silent, SMALL.amp(level), silent_s=silent, seed=i)
        for i, ((silent, level), f) in enumerate(cases.items())
    ]
    things += [
        Tx(100_000, 2.0, 99.0, SMALL.amp(8), speaks=False),
        Tx(160_000, 2.0, 99.0, SMALL.amp(20), speaks=False),
        Line(220_000, SMALL.line(20)),
    ]
    detector = SMALL.detector()
    return {"cases": cases, "events": timed(SMALL, detector, 63.0, things), "detector": detector}


@pytest.mark.parametrize("silent", [6.0, 30.0])
@pytest.mark.parametrize("level", [8, 20])
def test_a_carrier_keyed_before_the_first_word_is_caught_within_a_second_of_the_voice(
    keyed_before_speaking: dict, silent: float, level: int
) -> None:
    f = SMALL.hz(keyed_before_speaking["cases"][(silent, level)])
    spoke = 2.0 + silent
    mine = [(t, e) for t, e in keyed_before_speaking["events"] if abs(e.freq_hz - f) < 3_000]
    [(said, began)] = [(t, e) for t, e in mine if e.kind == "start"]
    [(_, ended)] = [(t, e) for t, e in mine if e.kind == "end"]
    assert spoke < said <= spoke + 1.0
    assert abs(began.freq_hz - f) <= 100 and began.track == ended.track
    assert SMALL.seconds(began.start) == pytest.approx(spoke, abs=0.5)
    assert SMALL.seconds(ended.end) == pytest.approx(spoke + 4.0, abs=1.3)


def test_a_carrier_that_never_speaks_is_never_caught(keyed_before_speaking: dict) -> None:
    events = [e for _, e in keyed_before_speaking["events"]]
    assert len(events) == 8  # the four that spoke: a start and an end each
    for f in (100_000, 160_000, 220_000):
        assert [e for e in events if abs(e.freq_hz - SMALL.hz(f)) < 20_000] == []
    assert keyed_before_speaking["detector"].open.steady >= 3


def test_speech_over_a_known_line_is_caught_when_it_is_the_stronger_and_beside_it_as_ever() -> None:
    lines = [Line(f, S.line(20)) for f in (-300_000, -100_000, 300_000)]
    voices = [
        Tx(-300_000, 3.0, 6.0, S.amp(22), seed=1),  # on top of the line
        Tx(-100_000 + 3_500, 3.0, 6.0, S.amp(8), seed=2),  # beside it
    ]
    detector = S.detector()
    events = watch(detector, S.air(8.0, lines + voices))
    [over] = catches(events, S.hz(-300_000))
    assert S.seconds(over.start) == pytest.approx(3.0, abs=0.5)
    [beside] = catches(events, S.hz(-96_500))
    assert abs(beside.freq_hz - S.hz(-96_500)) <= 150
    assert len(events) == 4 and detector.open.steady >= 2


def test_another_receivers_clock_is_left_out_of_the_open_range() -> None:
    scene = Scene(600_000, 432_050_000)  # 15 times 28.8 MHz is 50 kHz under the centre
    voice = [Tx(-50_000, 1.5, 4.5, scene.amp(15), seed=3)]
    assert catches(watch(scene.detector(), scene.air(6.0, voice)), 432_000_000)
    told = scene.detector(open_clocks=(28_800_000.0,))
    assert watch(told, scene.air(6.0, voice)) == []


def test_the_channels_report_the_same_with_an_open_range_as_without() -> None:
    """Speech and carriers that blink on the channels, speech between them (weak, and strong
    enough for the channels either side to hear it), lines, a drifting line and clicks."""
    scene = Scene(600_000, 446_100_000)  # the centre between channels 8 and 9

    def on(freq_hz: float) -> float:
        return off(freq_hz, scene)

    def things() -> list:
        return [
            Tx(on(PMR[1]), 1.0, 3.0, scene.amp(7), seed=1),
            Tx(on(PMR[3]), 1.2, 4.0, scene.amp(12), seed=2),
            Tx(on(PMR[4]), 2.0, 5.0, scene.amp(30), seed=3),
            Tx(on(PMR[8]) + 1_500, 3.0, 6.0, scene.amp(10), seed=4),
            Tx(on(PMR[12]), 6.0, 6.2, scene.amp(15), seed=5),  # too short to be a catch
            Line(on(PMR[14]), scene.line(25), period_s=2.5, on_s=0.8),
            Line(on(PMR[6]) + 700, scene.line(20), start_s=4.0, stop_s=8.0),
            Tx(on(PMR[10]) + 6_250, 2.0, 5.0, scene.amp(9), seed=6),
            Tx(on(PMR[0]) - 6_250, 5.0, 8.0, scene.amp(32), seed=7),
            Tx(on(PMR[13]) + 5_000, 6.5, 9.0, scene.amp(25), seed=8),
            Tx(-200_000, 1.0, 9.0, scene.amp(20), seed=9),
            Line(150_000, scene.line(30)),
            Line(200_000, scene.line(25), drift_hz_s=400.0),
            Line(on(PMR[15]) + 9_000, scene.line(28), period_s=3.0, on_s=1.0),
            Clicks(6, 0.05),
        ]

    alone = watch(scene.detector(PMR, open_range=None), scene.air(10.0, things()))
    both = watch(scene.detector(PMR), scene.air(10.0, things()))
    on_channels = [replace(e, track=0) for e in both if e.channel is not None]
    assert on_channels == [replace(e, track=0) for e in alone]
    assert len([e for e in alone if e.kind == "end"]) >= 6  # the scene does exercise them
    assert all(e.channel is not None for e in alone)
    assert catches(both, scene.hz(-200_000)) and catches(both, PMR[10] + 6_250)
