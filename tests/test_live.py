# SPDX-License-Identifier: Apache-2.0
"""Listening by hand (radio/live.py): a receiver retuned in place, its waterfall, a scan that
stops on activity, and how it shares a dongle with stations."""

import os
import stat
import sys
import time
from pathlib import Path

import numpy as np
import pytest

from tests.test_app import Running, make_app, wait_for
from tests.test_sdr_source import FAKE, dominant_hz
from wavescribe.config import SdrDevice
from wavescribe.radio import live, service
from wavescribe.radio.catalog import Catalog
from wavescribe.radio.live import LiveReceiver, Scan, Tuning
from wavescribe.sources import sdr

CENTER = 90_000_000


@pytest.fixture
def fake_rtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`rtl_sdr` on PATH is a stand-in with FM carriers 300 kHz above and 400 kHz below the
    centre; returns the file it logs each start to."""
    exe = tmp_path / "bin" / "rtl_sdr"
    exe.parent.mkdir()
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ['PATH']}")
    spawns = tmp_path / "spawns.txt"
    monkeypatch.setenv("FAKE_RTL_SPAWNS", str(spawns))
    monkeypatch.setenv("FAKE_RTL_STATIONS", "300000:1000,-400000:1500")
    yield spawns
    for serial in list(sdr._devices):
        dev = sdr._devices.pop(serial)
        for ch in dev._snapshot():
            dev.detach(ch)


def spawned(spawns: Path) -> list[str]:
    return spawns.read_text().splitlines() if spawns.exists() else []


class Heard:
    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []

    def __call__(self, samples: np.ndarray) -> None:
        self.chunks.append(samples)

    def last(self, seconds: float) -> np.ndarray:
        audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0)
        return audio[-int(seconds * 16_000) :]


def test_retunes_in_place_and_draws_the_band(fake_rtl: Path) -> None:
    dev = sdr.shared_device(SdrDevice("a", "00001234", CENTER, 20.0))
    heard = Heard()
    rx = LiveReceiver(dev, Tuning(CENTER + 300_000, "wfm"), heard)
    dev.attach(rx)  # type: ignore[arg-type]
    rx.watched_until = time.time() + 60
    wait_for(lambda: len(heard.last(2.0)) >= 32_000)
    assert abs(dominant_hz(heard.last(1.5)) - 1000) < 30

    rx.tuning = Tuning(CENTER - 400_000, "wfm")  # another station in the band: no restart
    n = len(heard.chunks)
    wait_for(lambda: len(heard.chunks) >= n + 10)
    assert abs(dominant_hz(heard.last(1.5)) - 1500) < 30
    assert len(spawned(fake_rtl)) == 1

    spec = rx.spectrum()
    db = np.array(spec["db"])
    freqs = np.linspace(spec["start_hz"], spec["stop_hz"], len(db), endpoint=False)
    for offset in (300_000, -400_000):
        near = np.abs(freqs - (CENTER + offset)) < 60_000
        assert db[near].max() > spec["noise_db"] + 20  # both carriers stand out
    zoom = rx.spectrum(CENTER + 250_000, CENTER + 350_000)
    assert zoom["start_hz"] >= CENTER + 249_000 and zoom["stop_hz"] <= CENTER + 351_000
    assert len(zoom["db"]) > 300  # ~293 Hz bins: fine enough for narrow channels
    rx.close()


def test_a_scan_stops_on_the_active_channel(fake_rtl: Path) -> None:
    dev = sdr.shared_device(SdrDevice("a", "00001234", CENTER, 20.0))
    rx = LiveReceiver(dev, Tuning(CENTER + 100_000, "nfm"), Heard())
    channels = [CENTER + k for k in (100_000, 200_000, 300_000, 500_000)]
    rx.scan = Scan(channels, "nfm", 25_000)
    dev.attach(rx)  # type: ignore[arg-type]
    wait_for(lambda: rx.scan.holding is not None)
    assert rx.scan.holding == CENTER + 300_000 and rx.tuning.freq_hz == CENTER + 300_000
    assert rx.scan.levels[CENTER + 300_000] > 20 > rx.scan.levels[CENTER + 200_000]

    rx.scan.locked_out.add(CENTER + 300_000)  # a spur, say: skipped from now on
    rx.scan.holding = None
    time.sleep(0.6)
    assert rx.scan.holding is None
    rx.close()


@pytest.fixture
def node(tmp_path: Path, fake_rtl: Path):
    r = Running(make_app(tmp_path))
    r.app._radio = service.RadioService(r.app, Catalog(tmp_path / "radio"))
    r.app.settings.update(lambda d: d.setdefault("radio", {}).update(region="cept"))
    dongle = {"id": "x", "serial": "SN0042", "center_mhz": 445.85, "gain_db": 30}
    assert r.call("POST", "/api/manage/receivers", dongle)[0] == 200
    yield r
    r.app.live.close_all()
    r.close()


def test_listening_from_the_page(node: Running, fake_rtl: Path) -> None:
    status, err = node.call("POST", "/api/manage/live/x", {"mhz": 446.00625})
    assert status == 400 and err["code"] == "acknowledge" and err["privacy"] == "private"
    assert "Confirm" in err["error"]
    status, st = node.call("POST", "/api/manage/live/x", {"mhz": 446.00625, "acknowledge": True})
    assert status == 200 and st["open"] and st["mode"] == "nfm" and st["band"]["id"] == "pmr446"
    assert st["audio"] == "live.x" and st["channel_khz"] == 12.5

    wait_for(lambda: node.call("GET", "/api/manage/live/x")[1]["spectrum"] is not None)
    st = node.call("GET", "/api/manage/live/x?lo=445.9&hi=446.2")[1]
    assert st["spectrum"]["start_hz"] >= 445_899_000 and st["connected"]
    # Another channel of the same class needs no second confirmation.
    st = node.call("POST", "/api/manage/live/x", {"mhz": 446.09375})[1]
    assert st["mhz"] == 446.09375

    # Its audio plays like a station's, and nothing is stored.
    import http.client

    conn = http.client.HTTPConnection(*node.server.server_address, timeout=10)
    conn.request("GET", "/audio/live.x.pcm")
    resp = conn.getresponse()
    assert resp.status == 200 and len(resp.read(3200)) == 3200
    conn.close()
    assert node.app.store.recent("live.x") == []

    # Far away: the dongle moves (it is idle), and a station can't take it meanwhile.
    st = node.call("POST", "/api/manage/live/x", {"mhz": 99.5})[1]
    assert st["mode"] == "wfm" and st["center_mhz"] == pytest.approx(99.2)
    wait_for(lambda: len(spawned(fake_rtl)) == 2)  # one restart, at the new centre
    assert "-f 99200000" in spawned(fake_rtl)[-1]
    assert node.app.config.devices[0].center_hz == 445_850_000  # the setting isn't touched
    assert node.app.live.holds("x")

    node.call("DELETE", "/api/manage/live/x")
    assert not node.app.live.is_open("x") and sdr.find_device("SN0042") is None


def test_listening_far_away_pauses_stations_only_when_asked(node: Running) -> None:
    body = {"device": "x", "mhz": 446.00625, "language": "es-ES", "acknowledge": True}
    st = node.call("POST", "/api/manage/radio/tune", body)[1]
    held = {st["id"]}
    real = node.app.running
    node.app.running = lambda: real() | held  # type: ignore[method-assign]
    removed, added = [], []
    node.app.pipeline.remove_station = lambda sid, wait=0: (removed.append(sid), held.discard(sid))
    node.app.pipeline.add_station = lambda s, asr=None: (added.append(s.id), held.add(s.id))

    # In the band its station uses: they share the dongle.
    status, near = node.call("POST", "/api/manage/live/x", {"mhz": 446.1, "acknowledge": True})
    assert status == 200 and near["holders"] == [st["name"]] and removed == []

    status, err = node.call("POST", "/api/manage/live/x", {"mhz": 145.5})
    assert status == 400 and err["code"] == "busy" and err["holders"] == [st["name"]]
    status, far = node.call("POST", "/api/manage/live/x", {"mhz": 145.5, "pause": True})
    assert status == 200 and far["paused"] == [st["id"]] and removed == [st["id"]]

    node.call("DELETE", "/api/manage/live/x")
    assert added == [st["id"]]  # it starts again when listening ends


def test_keep_what_is_tuned_as_a_listen_only_station(node: Running) -> None:
    node.call("POST", "/api/manage/live/x", {"mhz": 99.5})
    status, st = node.call("POST", "/api/manage/live/x/station",
                           {"does": "listen", "language": "fr-FR", "retune": True})  # fmt: skip
    assert status == 200 and st["transcribe"] is False and st["fm_mhz"] == [99.5]
    assert not node.app.live.is_open("x")


def test_a_bad_channel_width_is_refused_and_cant_silence_the_dongle(node: Running) -> None:
    body = {"mhz": 446.1, "acknowledge": True}
    for bad in (60, 0.5, "wide"):
        status, err = node.call("POST", "/api/manage/live/x", {**body, "channel_khz": bad})
        assert status == 400 and "kHz wide" in err["error"]
    assert not node.app.live.is_open("x")
    status, err = node.call("POST", "/api/manage/live/x", {"mhz": 0.666, "acknowledge": True})
    assert status == 400 and "medium wave" in err["error"]  # below the tuner: refused, not opened
    assert not node.app.live.is_open("x")

    # Even if one got through, only that channel would stop: the dongle goes on for the rest.
    st = node.call("POST", "/api/manage/live/x", body)[1]
    rx = node.app.live._rx["x"]
    other = Heard()
    second = LiveReceiver(rx.device, Tuning(446_100_000, "nfm"), other)
    second._build = lambda t: (_ for _ in ()).throw(ValueError("broken"))  # type: ignore[method-assign]
    rx.device.attach(second)  # type: ignore[arg-type]
    wait_for(lambda: second.closed)
    n = node.call("GET", "/api/manage/live/x?lo=446&hi=446.2")[1]
    assert n["connected"] and st["open"]
    wait_for(lambda: rx.health.last_data_at and time.time() - rx.health.last_data_at < 1)


def test_a_narrow_mode_inside_a_broadcast_band_gets_a_narrow_channel(node: Running) -> None:
    # The FM band's raster is 100 kHz: no width for an NFM or AM channel.
    for mode, khz in (("nfm", 12.5), ("am", 8.33)):
        status, st = node.call("POST", "/api/manage/live/x", {"mhz": 99.5, "mode": mode})
        assert status == 200 and st["mode"] == mode and st["channel_khz"] == khz
    rx = node.app.live._rx["x"]
    wait_for(lambda: rx._applied is not None and rx._applied.mode == "am")
    assert not rx.closed and node.call("GET", "/api/manage/live/x")[1]["connected"]

    # The same from the Radio tab: the station runs on the mode's own width.
    body = {"device": "x", "mhz": 99.6, "mode": "nfm", "language": "pt-PT", "retune": True}
    node.call("DELETE", "/api/manage/live/x")
    status, st = node.call("POST", "/api/manage/radio/tune", body)
    assert status == 200, st
    tuned = node.app.config.get(st["id"]).sdr
    assert tuned.mode == "nfm" and tuned.channel_hz is None


def test_a_receiver_that_fails_says_so_until_tuned_again(node: Running, monkeypatch) -> None:
    class Broken(live.NarrowbandDemodulator):
        def __call__(self, iq: np.ndarray) -> np.ndarray:
            raise RuntimeError("the demodulator broke")

    node.call("POST", "/api/manage/live/x", {"mhz": 99.5})
    wait_for(lambda: node.call("GET", "/api/manage/live/x")[1]["spectrum"] is not None)
    monkeypatch.setattr(live, "NarrowbandDemodulator", Broken)
    status, st = node.call("POST", "/api/manage/live/x", {"mhz": 99.5, "mode": "nfm"})
    assert status == 200

    # The page is told, instead of being shown the last spectrum and signal level forever.
    wait_for(lambda: node.call("GET", "/api/manage/live/x")[1]["spectrum"] is None)
    st = node.call("GET", "/api/manage/live/x")[1]
    assert st["open"] and not st["connected"] and st["signal_db"] is None
    assert "the demodulator broke" in st["error"]

    # Back to FM, where the dongle already is: listening goes on.
    node.call("POST", "/api/manage/live/x", {"mhz": 99.5, "mode": "wfm"})
    wait_for(lambda: node.call("GET", "/api/manage/live/x")[1]["spectrum"] is not None)
    st = node.call("GET", "/api/manage/live/x")[1]
    assert st["connected"] and st["error"] is None
    import http.client

    conn = http.client.HTTPConnection(*node.server.server_address, timeout=10)
    conn.request("GET", "/audio/live.x.pcm")
    assert len(conn.getresponse().read(3200)) == 3200
    conn.close()


def test_what_cant_be_demodulated_is_refused_when_tuning(node: Running, monkeypatch) -> None:
    def refuse(*args: object) -> None:
        raise ValueError("no such channel")

    monkeypatch.setattr(live, "NarrowbandDemodulator", refuse)
    status, err = node.call("POST", "/api/manage/live/x", {"mhz": 446.1, "acknowledge": True})
    assert status == 400 and "no such channel" in err["error"]
    assert not node.app.live.is_open("x")


def test_a_channel_on_the_dongles_centre_moves_the_dongle(node: Running, fake_rtl: Path) -> None:
    listen = {"mhz": 445.85, "mode": "nfm", "acknowledge": True}  # where the receiver is set
    status, st = node.call("POST", "/api/manage/live/x", listen)
    assert status == 200 and st["mhz"] == 445.85 and st["center_mhz"] == pytest.approx(445.6)
    wait_for(lambda: spawned(fake_rtl))
    assert "-f 445600000" in spawned(fake_rtl)[-1]
    # A scan doesn't stop on the centre either: the channel there is left out.
    st = node.call("POST", "/api/manage/live/x", {"mhz": 445.8, "mode": "nfm"})[1]
    assert st["center_mhz"] == pytest.approx(445.6)
    scan = {"freqs_mhz": [445.6, 445.7, 445.8], "mode": "nfm"}
    st = node.call("POST", "/api/manage/live/x/scan", scan)[1]
    assert [round(f, 3) for f in st["scan"]["freqs_mhz"]] == [445.7, 445.8]


def test_a_frequency_in_the_wrong_unit_is_refused_with_the_right_one(node: Running) -> None:
    for mhz, message in (
        (446_006_250, "looks like Hz; frequencies are in MHz: 446.00625"),
        ("nan", "give a frequency in MHz"),
        (-99.5, "give a frequency in MHz"),
    ):
        status, err = node.call("POST", "/api/manage/live/x", {"mhz": mhz, "acknowledge": True})
        assert status == 400 and message in err["error"]
    assert not node.app.live.is_open("x")
    node.call("POST", "/api/manage/live/x", {"mhz": 446.1, "acknowledge": True})
    status, err = node.call("POST", "/api/manage/live/x/scan", {"freqs_mhz": [446.1, "inf"]})
    assert status == 400 and "give a frequency in MHz" in err["error"]


def test_moving_the_dongle_doesnt_wait_for_itself(node: Running) -> None:
    node.call("POST", "/api/manage/live/x", {"mhz": 446.1, "acknowledge": True})
    t = time.time()
    st = node.call("POST", "/api/manage/live/x", {"mhz": 99.5})[1]
    assert st["center_mhz"] == pytest.approx(99.2) and time.time() - t < 3


def test_an_idle_receiver_closes(node: Running, monkeypatch) -> None:
    monkeypatch.setattr(live, "IDLE_CLOSE_S", 0.0)
    monkeypatch.setattr(live, "REAP_EVERY_S", 0.1)
    node.call("POST", "/api/manage/live/x", {"mhz": 446.1, "acknowledge": True})
    wait_for(lambda: not node.app.live.is_open("x"))
