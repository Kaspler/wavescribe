# SPDX-License-Identifier: Apache-2.0
"""The made-up sky the stand-in rtl_sdr plays (tests/fake_world.py): what a receiver hears
depends on where it is tuned, and the dongle's own untruths change what the detectors see as
they would on the air."""

from __future__ import annotations

import json
import socket
import stat
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from tests import fake_world
from tests.fake_world import World
from wavescribe.config import SdrDevice, SdrTuning
from wavescribe.dsp.activity import ActivityDetector
from wavescribe.dsp.channel import NarrowbandDemodulator
from wavescribe.dsp.wfm import WfmDemodulator, iq_from_u8
from wavescribe.sources.sdr import RtlSdrDevice, SdrChannelSource

RATE = 2_400_000
FM = {
    "transmitters": [
        {"kind": "wfm", "mhz": 91.5, "tone_hz": 800},
        {"kind": "wfm", "mhz": 92.4, "ps": "VALLEY", "rt": "Hello from the valley"},
        {"kind": "wfm", "mhz": 92.9, "tone_hz": 1500},
        {"kind": "wfm", "mhz": 97.5, "ps": "HARBOUR"},
        {"kind": "wfm", "mhz": 98.3, "tone_hz": 400},
    ]
}
PMR = 446_000_000
CHANNELS = [446_006_250 + 12_500 * i for i in range(8)]


def heard(world: dict, center_hz: float, seconds: float = 1.0, **kw: object) -> np.ndarray:
    tuned = World(world).tune(center_hz, RATE, **kw)  # type: ignore[arg-type]
    return np.concatenate([iq_from_u8(tuned.u8()) for _ in range(round(seconds / 0.256))])


def occupied_khz(iq: np.ndarray) -> list[int]:
    """The middle of each stretch of the window 10 dB or more over the noise, to 100 kHz."""
    n = 4096
    power = np.zeros(n)
    for k in range(len(iq) // n):
        power += np.abs(np.fft.fft(iq[k * n : (k + 1) * n] * np.hanning(n))) ** 2
    power = np.convolve(np.fft.fftshift(power), np.ones(64) / 64, mode="same")  # 37 kHz
    freqs = (np.arange(n) - n // 2) * RATE / n
    loud = 10 * np.log10(power / np.percentile(power, 10)) > 10
    edges = np.flatnonzero(np.diff(np.concatenate([[0], loud.astype(int), [0]])))
    return [int(round((freqs[a] + freqs[b - 1]) / 2 / 100e3) * 100)
            for a, b in zip(edges[0::2], edges[1::2], strict=True)]  # fmt: skip


def dominant_hz(audio: np.ndarray) -> float:
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    return float(np.fft.rfftfreq(len(audio), 1 / 16_000)[np.argmax(spec)])


def test_a_receiver_hears_what_lies_in_its_window_and_nothing_else() -> None:
    assert occupied_khz(heard(FM, 92.0e6)) == [-500, 400, 900]
    assert occupied_khz(heard(FM, 98.0e6)) == [-500, 300]
    # 91.5 is 1.1 MHz off a window at 92.6: half of it would fold in at the other edge.
    assert occupied_khz(heard(FM, 92.6e6)) == [-200, 300]


@pytest.fixture
def fake_rtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "rtl_sdr"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{Path(fake_world.__file__).with_name("fake_rtl_sdr.py")}" "$@"\n')  # fmt: skip # noqa: E501
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "world.json").write_text(json.dumps(FM))
    monkeypatch.setenv("FAKE_RTL_WORLD", str(tmp_path / "world.json"))
    return exe


def listen(exe: Path, center_hz: int, freqs: list[int], seconds: float) -> list[SdrChannelSource]:
    """Stations at ``freqs`` on one dongle at ``center_hz``, through the rtl_sdr pipe."""
    dev = RtlSdrDevice(SdrDevice("a", "SIM00001", center_hz, 20.0), command=str(exe))
    sources = [SdrChannelSource(str(f), SdrTuning(dev.config, f), device=dev) for f in freqs]
    its = [iter(s) for s in sources]
    for s, it in zip(sources, its, strict=True):
        audio, got = [], 0.0
        while got < seconds:
            chunk = next(it)
            audio.append(chunk.samples)
            got += chunk.duration
        s.audio = np.concatenate(audio)  # type: ignore[attr-defined]
    for s, it in zip(sources, its, strict=True):
        s.close()
        it.close()
    return sources


def test_through_rtl_sdr_a_moved_receiver_hears_other_stations_and_their_names(
    fake_rtl: Path,
) -> None:
    valley, other = listen(fake_rtl, 92_000_000, [92_400_000, 92_900_000], 3.0)
    assert dominant_hz(valley.audio[8000:]) == pytest.approx(1000, abs=5)  # type: ignore[attr-defined]
    assert valley.health.rds["ps"] == "VALLEY"
    assert valley.health.rds["rt"] == "Hello from the valley"
    assert dominant_hz(other.audio[8000:]) == pytest.approx(1500, abs=5)  # type: ignore[attr-defined]
    assert other.health.rds.get("ps") is None
    (harbour,) = listen(fake_rtl, 98_000_000, [97_500_000], 3.0)
    assert harbour.health.rds["ps"] == "HARBOUR"


def test_a_recording_is_sent_again_after_its_gap(tmp_path: Path) -> None:
    t = np.arange(8_000) / 16_000
    (0.5 * np.sin(2 * np.pi * 700 * t)).astype(np.float32).tofile(tmp_path / "tone.f32")
    world = {"transmitters": [{"kind": "wfm", "mhz": 92.4, "audio": "tone.f32", "gap_s": 0.5}]}
    (tmp_path / "w.json").write_text(json.dumps(world))
    tuned = World.load(str(tmp_path / "w.json")).tune(92.0e6, RATE)
    demod = WfmDemodulator(RATE, 400_000)
    audio = np.concatenate([demod(iq_from_u8(tuned.u8())) for _ in range(16)])  # 4.1 s
    level = [np.sqrt(np.mean(audio[i : i + 1600] ** 2)) for i in range(0, len(audio), 1600)]
    loud = [x > 0.05 for x in level]  # 0.1 s each: 0.5 s of tone, 0.5 s of nothing, again
    assert sum(loud[5:40]) == pytest.approx(17, abs=3)
    assert dominant_hz(audio[2000:7000]) == pytest.approx(700, abs=5)


def test_a_walkie_talkie_keys_on_its_schedule_and_am_carries_its_sound() -> None:
    world = {
        "transmitters": [
            {"kind": "nfm", "mhz": 446.03125, "period_s": 2.048, "on_s": 1.024, "level": 0.05},
            {"kind": "am", "mhz": 446.5, "tone_hz": 600, "level": 0.05},
        ]
    }
    tuned = World(world).tune(PMR, RATE)
    nfm = NarrowbandDemodulator(RATE, 31_250, "nfm")
    am = NarrowbandDemodulator(RATE, 500_000, "am")
    opened, am_audio = [], []
    for _ in range(16):
        iq = iq_from_u8(tuned.u8())
        nfm(iq)
        opened.append(nfm.open)
        am_audio.append(am(iq))
    # Off for the first half of every 2.048 s (eight blocks), keyed for the second.
    assert opened == ([False] * 4 + [True] * 4) * 2
    assert dominant_hz(np.concatenate(am_audio)[16_000:]) == pytest.approx(600, abs=5)


def test_a_digital_block_is_flat_with_steep_sides_and_only_its_inside_is_heard() -> None:
    world = {"transmitters": [{"kind": "block", "mhz": 211.648, "width_khz": 1536}]}
    iq = heard(world, 211.648e6, 0.512)
    n = 4096
    power = sum(
        np.abs(np.fft.fft(iq[k * n : (k + 1) * n] * np.hanning(n))) ** 2
        for k in range(len(iq) // n)
    )
    db = 10 * np.log10(np.fft.fftshift(power))
    f = (np.arange(n) - n // 2) * RATE / n
    inside, outside = db[np.abs(f) < 700e3], db[(np.abs(f) > 820e3) & (np.abs(f) < 1.1e6)]
    assert np.percentile(inside, 95) - np.percentile(inside, 5) < 3
    assert np.median(inside) - np.median(outside) > 20
    # Half off the window's top: its lower half is there, nothing folds in at the bottom.
    assert occupied_khz(heard(world, 210.9e6, 0.512)) == [600]  # 0 to 1.2 MHz


def test_on_the_wall_clock_two_starts_share_one_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    world = {"clock": "wall", "epoch": 1000, "transmitters": [
        {"kind": "line", "mhz": 446.1, "period_s": 10, "on_s": 5, "level": 0.1}]}  # fmt: skip
    monkeypatch.setattr(fake_world.time, "time", lambda: 1003.0)  # 3 s in: off until 5 s
    first = World(world).tune(PMR, RATE)
    monkeypatch.setattr(fake_world.time, "time", lambda: 1007.0)  # 7 s in: on until 10 s
    second = World(world).tune(PMR, RATE)
    assert (first.t0, second.t0) == (3.0, 7.0)
    assert occupied_khz(iq_from_u8(first.u8())) == []
    assert occupied_khz(iq_from_u8(second.u8())) == [100]


def test_paced_it_comes_no_faster_than_a_dongle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RTL_PACE", "1")
    tuned = World({"transmitters": []}).tune(PMR, RATE)
    start = time.monotonic()
    for _ in range(4):
        tuned.u8()
    assert time.monotonic() - start >= 0.256 * 3.9


# The dongle's untruths, each against a detector the way the air would trouble it.


def caught(world: dict, channels: list[int] = CHANNELS, **kw: object) -> list[tuple[int, float]]:
    """(channel, dB over its floor) of each transmission the watch detector starts on."""
    tuned = World(world).tune(PMR, RATE, "SIM00001", **kw)  # type: ignore[arg-type]
    detector = ActivityDetector(RATE, PMR, channels, 12_500)
    events = []
    for _ in range(round(4.0 / 0.256)):
        events += detector.feed(iq_from_u8(tuned.u8()))
    return [(e.freq_hz, e.snr_db) for e in events if e.kind == "start"]


BURST = {"kind": "nfm", "mhz": 446.03125, "period_s": 2.048, "on_s": 1.024, "level": 0.01}


def test_a_raised_noise_floor_hides_a_weak_walkie_talkie() -> None:
    assert [f for f, _ in caught({"transmitters": [BURST]})] == [446_031_250]
    assert caught({"noise": 0.05, "transmitters": [BURST]}) == []


def test_a_clock_off_by_ppm_puts_a_transmission_on_another_channel_unless_corrected() -> None:
    world = {"receivers": {"SIM00001": {"ppm": 50}}, "transmitters": [BURST]}
    # 50 ppm at 446 MHz is 22 kHz: the dongle tunes high, so everything shows lower.
    assert [f for f, _ in caught(world)] == [446_006_250]
    assert [f for f, _ in caught(world, ppm_corrected=50)] == [446_031_250]


def test_a_tuner_rolling_off_at_the_edges_weakens_what_is_near_them() -> None:
    edge, inner = PMR + 1_100_000, PMR + 300_000
    world = {"transmitters": [{**BURST, "mhz": edge / 1e6}, {**BURST, "mhz": inner / 1e6}]}
    flat = dict(caught(world, [edge, inner]))
    rolled = dict(caught({**world, "rolloff_db": 12}, [edge, inner]))
    assert flat[edge] == pytest.approx(flat[inner], abs=1.5)
    assert rolled[inner] == pytest.approx(flat[inner], abs=1.5)
    assert edge not in rolled or rolled[edge] < flat[edge] - 6


def test_the_spike_at_the_centre_holds_a_squelch_open_there() -> None:
    def open_blocks(dc: float) -> int:
        tuned = World({"dc": dc, "transmitters": []}).tune(PMR, RATE)
        squelch = NarrowbandDemodulator(RATE, 0.0, "nfm")
        return sum(squelch(iq_from_u8(tuned.u8())) is not None and squelch.open for _ in range(8))

    assert open_blocks(0.0) == 0
    assert open_blocks(0.02) == 8


# The page's fixture: serve with the made-up receivers.


def test_the_page_fixture_serves_the_made_up_receivers() -> None:
    from tests.page_fixture import running

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with running(port, pace=False) as f:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = json.load(urllib.request.urlopen(f.url + "api/state", timeout=5))
            names = {
                sid: (s.get("rds") or {}).get("ps")
                for sid, s in state["status"]["stations"].items()
            }
            if names == {"valley": "VALLEY", "harbour": "HARBOUR"}:
                break
            time.sleep(0.5)
        assert names == {"valley": "VALLEY", "harbour": "HARBOUR"}
        assert (f.directory / "data").is_dir()  # its store, not the checkout's
