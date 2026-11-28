# SPDX-License-Identifier: Apache-2.0
"""A receiver's session (radio/receiver.py) on the made-up sky: the detector over its window,
settling where it lands, the waterfall ring, and leaving the detector out when the dongle's
thread falls behind."""

from __future__ import annotations

import json
import logging
import stat
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from tests.fake_world import World
from wavescribe.config import SdrDevice, SdrTuning
from wavescribe.dsp.wfm import iq_from_u8
from wavescribe.radio.receiver import LINE_BINS, LINES_PER_S, ReceiverSession
from wavescribe.sources.sdr import RtlSdrDevice, SdrChannelSource

RATE = 2_400_000
SKY = {
    "transmitters": [
        {"kind": "wfm", "mhz": 92.4, "ps": "VALLEY"},
        {"kind": "wfm", "mhz": 92.9, "tone_hz": 1500, "level": 0.1},
        {"kind": "wfm", "mhz": 97.5, "level": 0.1},
        {"kind": "line", "mhz": 91.3, "level": 0.02},
    ]
}


class Dongle:
    """Just what a session asks of its dongle."""

    def __init__(self, center_hz: int) -> None:
        self.config = SdrDevice("a", "SIM00001", center_hz, 20.0)
        self.load = 0.0

    def attach(self, _consumer: object) -> None: ...

    def detach(self, _consumer: object) -> None: ...


class Sky:
    """Blocks of the made-up sky from wherever the dongle is tuned, with wall times."""

    def __init__(self, session: ReceiverSession, dongle: Dongle, t: float = 1000.0) -> None:
        self.session, self.dongle, self.t = session, dongle, t
        self.tuned = World(SKY).tune(dongle.config.center_hz, RATE)
        self.first = True

    def move(self, center_hz: int) -> None:
        self.dongle.config = replace(self.dongle.config, center_hz=center_hz)
        self.tuned = World(SKY).tune(center_hz, RATE)
        self.first = True
        self.t += 0.8  # rtl_sdr starting again

    def run(self, seconds: float) -> None:
        for _ in range(round(seconds / 0.256)):
            self.t += 0.256
            self.session._feed(iq_from_u8(self.tuned.u8()), self.t, self.first)
            self.first = False


def session_at(mhz: float, **kw: object) -> tuple[ReceiverSession, Sky]:
    dongle = Dongle(round(mhz * 1e6))
    session = ReceiverSession(dongle, region="cept", **kw)  # type: ignore[arg-type]
    return session, Sky(session, dongle)


def test_the_detector_notes_the_wide_stations_in_its_window() -> None:
    session, sky = session_at(92.0)
    sky.run(3.0)
    noted = sorted(e.freq_hz for e in session.events if e.kind == "noted")
    assert noted == [pytest.approx(92_400_000, abs=5_000), pytest.approx(92_900_000, abs=5_000)]
    assert all(1000.0 < e.t_start < sky.t for e in session.events)
    status = session.status()
    assert status["center_hz"] == 92_000_000 and not status["settling"]
    assert len(status["wide"]) == 2  # type: ignore[arg-type]


def test_settling_where_it_lands_and_settled_within_seven_tenths_of_a_second() -> None:
    session, sky = session_at(92.0)
    sky.run(0.512)
    assert session.settling and session.lines()[0].size == 0
    sky.run(0.256)
    assert not session.settling


def test_the_ring_is_bounded_and_empty_after_a_move_until_settled() -> None:
    session, sky = session_at(92.0, ring_s=1.0)
    sky.run(3.0)
    times, lines = session.lines()
    assert len(times) == LINES_PER_S and lines.shape == (LINES_PER_S, LINE_BINS)
    assert np.all(np.diff(times) > 0) and times[-1] <= sky.t
    # The stations' bins stand far over the rest of the line.
    station = round((400_000 + RATE / 2) / RATE * LINE_BINS)
    assert int(lines[-1][station]) - int(np.median(lines[-1])) > 2 * 20  # 0.5 dB a step
    sky.move(98_000_000)
    sky.run(0.512)
    assert session.settling and session.lines()[0].size == 0
    assert [p.center_hz for p in session.places] == [92_000_000, 98_000_000]
    sky.run(1.0)
    after, _ = session.lines()
    assert len(after) > 0 and after[0] > sky.t - 1.5


def test_the_same_place_after_a_gap_keeps_its_place_and_learns_again() -> None:
    session, sky = session_at(92.0)
    sky.run(1.0)
    sky.first = True  # rtl_sdr came back after a dropout
    sky.run(0.256)
    assert len(session.places) == 1 and session.settling


def test_timing_goes_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    _, sky = session_at(92.0, timing_log_s=1.0)
    with caplog.at_level(logging.INFO, logger="wavescribe.radio.receiver"):
        sky.run(2.0)
    logged = [r for r in caplog.records if r.getMessage() == "receiver_timing"]
    assert logged and logged[0].fields["blocks"] >= 3  # type: ignore[attr-defined]
    assert logged[0].fields["p99_ms"] > 0  # type: ignore[attr-defined]


@pytest.fixture
def fake_rtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "rtl_sdr"
    fake = Path(__file__).with_name("fake_rtl_sdr.py")
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_RTL_WORLD", json.dumps(SKY))
    return exe


def listen(source: SdrChannelSource, seconds: float) -> float:
    it, got = iter(source), 0.0
    while got < seconds:
        got += next(it).duration
    source.close()
    it.close()
    return got


def test_through_rtl_sdr_beside_a_channel(fake_rtl: Path) -> None:
    dev = RtlSdrDevice(SdrDevice("a", "SIM00001", 92_000_000, 20.0), command=str(fake_rtl))
    session = ReceiverSession(dev, region="cept")
    session.start()
    channel = SdrChannelSource("valley", SdrTuning(dev.config, 92_400_000), device=dev)
    assert listen(channel, 3.0) >= 3.0
    session.close()
    assert channel.health.rds["ps"] == "VALLEY"
    assert sorted(round(e.freq_hz, -5) for e in session.events if e.kind == "noted") == [
        92_400_000,
        92_900_000,
    ]
    assert dev.load > 0


def test_behind_the_detector_skips_blocks_and_the_channels_get_every_sample(
    fake_rtl: Path,
) -> None:
    dev = RtlSdrDevice(SdrDevice("a", "SIM00001", 92_000_000, 20.0), command=str(fake_rtl))
    session = ReceiverSession(dev, region="cept", behind=0.0)  # always behind
    session.start()
    channel = SdrChannelSource("valley", SdrTuning(dev.config, 92_400_000), device=dev)
    assert listen(channel, 2.0) >= 2.0
    session.close()
    assert session.dropped >= 8 and not session.events and session.lines()[0].size == 0
    assert channel.dropped == 0 and channel.health.signal_db is not None


def caught_talking(**untruths: object) -> list[int]:
    """Where the session's detector caught a made-up walkie-talkie between any channels."""
    sky = {**untruths, "transmitters": [
        {"kind": "nfm", "mhz": 446.0571, "voice": 3, "period_s": 6.0, "on_s": 3.0,
         "level": 0.01}]}  # fmt: skip
    dongle = Dongle(446_100_000)
    session = ReceiverSession(dongle, region="cept")  # type: ignore[arg-type]
    tuned, t = World(sky).tune(dongle.config.center_hz, RATE), 1000.0
    for i in range(round(12.0 / 0.256)):
        t += 0.256
        session._feed(iq_from_u8(tuned.u8()), t, i == 0)
    return [e.freq_hz for e in session.events if e.kind == "start"]


def test_a_voice_between_channels_is_caught_and_a_raised_floor_hides_it() -> None:
    caught = caught_talking()
    assert caught and all(abs(f - 446_057_100) < 1_500 for f in caught)
    assert caught_talking(noise=0.08) == []
