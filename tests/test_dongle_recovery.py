# SPDX-License-Identifier: Apache-2.0
"""A dongle that vanishes from USB and comes back (a loose cable, a hub that lost power): its
device tries again with the usual backoff, and what listens on it carries on by itself once it
is back, without a restart: a watch catches transmissions again, the hand tuner plays again.

The stand-in rtl_sdr (tests/fake_rtl_sdr.py) plays the dongle: it stops sending, then says "No
matching devices found." at every start until it is plugged back in. No real receiver is
opened: the only rtl tool on the PATH is the stand-in."""

from __future__ import annotations

import os
import stat
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tests.test_app import wait_for
from tests.test_live import Heard
from tests.test_retune import running, stations
from tests.test_sdr_source import FAKE, dominant_hz
from wavescribe.config import SdrDevice, build_station
from wavescribe.radio import service
from wavescribe.sources import open_source, sdr

SERIAL = "00001234"
CH2 = 446_018_750  # PMR446 channel 2
BELOW = 446_006_250 - 250_000  # where a PMR446 watch tunes: under the band, clear of its spike
FAST = {"backoff_initial": 0.05, "backoff_max": 0.2}  # the backoff of a server, sped up


@pytest.fixture
def dongle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The stand-in, alone among the rtl tools on the PATH; returns the file it logs each start
    to. Each start after the first does what FAKE_RTL_THEN says."""
    exe = tmp_path / "bin" / "rtl_sdr"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("FAKE_RTL_SPAWNS", str(tmp_path / "spawns.txt"))
    monkeypatch.setattr(service.holders, "held_elsewhere", dict)  # never this Mac's dongles
    monkeypatch.chdir(tmp_path)  # the radio catalogue (no learnt spurs) is this test's own
    yield tmp_path / "spawns.txt"
    for serial in list(sdr._devices):
        dev = sdr._devices.pop(serial)
        for ch in dev._snapshot():
            dev.detach(ch)


def vanishes(monkeypatch: pytest.MonkeyPatch, after_s: float) -> None:
    """The dongle sends ``after_s`` seconds of IQ, then is gone from USB."""
    monkeypatch.setenv("FAKE_RTL_MAX_BYTES", str(int(2 * 2_400_000 * after_s)))
    monkeypatch.setenv("FAKE_RTL_THEN", "missing")


def comes_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_RTL_THEN", "stream")


def test_a_watch_catches_again_once_its_dongle_is_back(
    dongle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Someone keys up on channel 2 from 3.3 to 4.1 s of every 4.1 s; a broadcast carrier sits
    # far below. The dongle goes while the channel is quiet again, the first catch over, so
    # whatever is caught after it is back was heard after it was back.
    monkeypatch.setenv("FAKE_RTL_BURSTS", f"{CH2 - BELOW}:4.096:0.768")
    monkeypatch.setenv("FAKE_RTL_STATIONS", "-900000:1000")
    vanishes(monkeypatch, after_s=6.0)
    device = SdrDevice("a", SERIAL, BELOW, 30.0)
    station = build_station({"id": "walkies", "language": "auto", "device": "a",
                             "watch": "pmr446", "acknowledge": True},
                            "test", {"a": device}, "cept")  # fmt: skip
    dev = sdr.shared_device(device, **FAST)
    source = open_source(station)  # as the pipeline opens it: on the same shared dongle
    starts: list[tuple[float, int | None]] = []  # when each catch's audio began, and where
    failed: list[BaseException] = []

    def read() -> None:  # as the pipeline reads a source: until it is closed
        try:
            for chunk in source:
                if chunk.discontinuity:
                    starts.append((time.monotonic(), chunk.freq_hz))
        except BaseException as e:
            failed.append(e)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        wait_for(lambda: len(starts) >= 1, timeout=30)  # caught before the dongle went
        wait_for(lambda: dev.reconnects >= 3, timeout=30)  # gone, and tried again and again
        assert not source.health.connected and source.health.reconnects >= 3
        caught_before = len(starts)
        back_at = time.monotonic()
        comes_back(monkeypatch)
        wait_for(lambda: source.health.connected, timeout=30)
        wait_for(lambda: any(t > back_at for t, _ in starts), timeout=30)
        assert reader.is_alive() and not failed  # the station never stopped
        assert {f for _, f in starts} == {CH2} and len(starts) > caught_before
        assert source.health.reconnects >= 3  # the page can still say it was gone
        assert len(dongle.read_text().splitlines()) >= 4  # sent, gone (twice at least), back
    finally:
        source.close()
        reader.join(5)
    assert not failed


def test_the_hand_tuner_plays_again_once_its_dongle_is_back(
    dongle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RTL_STATIONS", "300000:1000,-400000:1500")
    vanishes(monkeypatch, after_s=1.0)
    raw = stations(98.0)
    raw["station"] = [s for s in raw["station"] if s["id"] != "ham2m"]  # the receiver is free
    r = running(tmp_path, raw)
    dev = sdr.shared_device(r.app.config.devices[0], **FAST)
    try:
        status, st = r.call("POST", "/api/manage/live/a", {"mhz": 98.3, "from": "listen"})
        assert status == 200 and st["open"], st
        rx = r.app.live._rx["a"]
        heard = Heard()
        played = rx._on_audio
        rx._on_audio = lambda audio: (heard(audio), played(audio))

        def tuner() -> dict:
            return r.call("GET", "/api/manage/live/a")[1]  # as the page polls it

        wait_for(lambda: dev.reconnects >= 3, timeout=30)
        st = tuner()
        assert st["open"] and not st["connected"] and st["reconnects"] >= 3, st
        assert st["error"] is None  # not answering, not broken: the page says it is unplugged
        before = len(heard.chunks)
        comes_back(monkeypatch)
        wait_for(lambda: tuner()["connected"], timeout=30)
        wait_for(lambda: sum(map(len, heard.chunks[before:])) >= 24_000, timeout=30)
        assert abs(dominant_hz(np.concatenate(heard.chunks[before:])) - 1000) < 30  # 98.3 MHz
        st = tuner()
        assert st["open"] and st["error"] is None and st["mhz"] == 98.3
        wait_for(lambda: tuner()["spectrum"] is not None)
        assert r.app.live._rx["a"] is rx  # the same receiver, never reopened
    finally:
        r.app.live.close_all()
        r.close()
