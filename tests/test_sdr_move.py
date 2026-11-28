# SPDX-License-Identifier: Apache-2.0
"""Moving a receiver without letting it go (RtlSdrDevice.move): rtl_sdr starts again at the new
place at once, a channel pointed there goes on with a gap in its audio and nothing else torn
down, and a receiver's session lands there and settles."""

from __future__ import annotations

import json
import stat
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from wavescribe.config import SdrDevice, SdrTuning
from wavescribe.radio.receiver import ReceiverSession
from wavescribe.sources.sdr import RtlSdrDevice, SdrChannelSource

SKY = {
    "transmitters": [
        {"kind": "wfm", "mhz": 92.4, "ps": "VALLEY"},
        {"kind": "wfm", "mhz": 98.3, "ps": "RIVER"},
    ]
}
A = SdrDevice("a", "SIM00001", 92_000_000, 20.0)


@pytest.fixture
def fake_rtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    exe = tmp_path / "rtl_sdr"
    fake = Path(__file__).with_name("fake_rtl_sdr.py")
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_RTL_WORLD", json.dumps(SKY))
    return exe


def name(source: SdrChannelSource) -> str | None:
    return (source.health.rds or {}).get("ps")


class Listener:
    """Reads a channel on its own thread, as the pipeline does, keeping when each chunk came."""

    def __init__(self, source: SdrChannelSource) -> None:
        self.source = source
        self.chunks: list[tuple[float, bool]] = []  # (arrived, discontinuity)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        for chunk in self.source:
            self.chunks.append((time.monotonic(), chunk.discontinuity))

    def until(self, test: Callable[[], bool], timeout: float = 20.0) -> None:
        end = time.monotonic() + timeout
        while not test():
            assert time.monotonic() < end, "timed out"
            time.sleep(0.05)

    def stop(self) -> None:
        self.source.close()
        self._thread.join(5)


def test_a_move_keeps_the_channel_and_counts_no_reconnect(fake_rtl: Path) -> None:
    dev = RtlSdrDevice(A, command=str(fake_rtl))
    session = ReceiverSession(dev, region="cept")
    session.start()
    valley = SdrChannelSource("valley", SdrTuning(A, 92_400_000), device=dev)
    ear = Listener(valley)
    ear.until(lambda: name(valley) == "VALLEY")
    there = replace(A, center_hz=92_300_000)  # the same station, at another offset
    valley.repoint(SdrTuning(there, 92_400_000))
    moved_at, before = time.monotonic(), len(ear.chunks)
    dev.move(there)
    ear.until(lambda: any(d for _, d in ear.chunks[before:]))
    gap = next(t for t, d in ear.chunks[before:] if d) - moved_at
    ear.until(lambda: len(ear.chunks) > before + 8)
    ear.stop()
    session.close()
    assert dev.reconnects == 0 and dev.config == there
    assert name(valley) == "VALLEY"  # kept: the same frequency
    assert valley.health.signal_db is not None and valley.health.signal_db > -30
    assert [p.center_hz for p in session.places] == [92_000_000, 92_300_000]
    assert gap < 3.0  # the fake's own start; a dongle's is measured on the dongle


def test_a_channel_pointed_elsewhere_starts_its_name_afresh(fake_rtl: Path) -> None:
    dev = RtlSdrDevice(A, command=str(fake_rtl))
    ch = SdrChannelSource("x", SdrTuning(A, 92_400_000), device=dev)
    ear = Listener(ch)
    ear.until(lambda: name(ch) == "VALLEY")
    there = replace(A, center_hz=98_000_000)
    ch.repoint(SdrTuning(there, 98_300_000))
    dev.move(there)
    ear.until(lambda: name(ch) == "RIVER")
    ear.stop()
    assert dev.reconnects == 0


def test_a_move_is_of_the_same_dongle(fake_rtl: Path) -> None:
    dev = RtlSdrDevice(A, command=str(fake_rtl))
    with pytest.raises(ValueError, match="keeps the dongle"):
        dev.move(replace(A, serial="SIM00002"))
