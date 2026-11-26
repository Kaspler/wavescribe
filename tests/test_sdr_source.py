# SPDX-License-Identifier: Apache-2.0
"""SdrChannelSource and RtlSdrDevice against a fake rtl_sdr that writes synthetic FM IQ."""

from __future__ import annotations

import stat
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tests.helpers import requires_ffmpeg, requires_say, say_to_pcm
from wavescribe.config import SdrDevice, SdrTuning, Station
from wavescribe.sources import open_source
from wavescribe.sources.sdr import RtlSdrDevice, SdrChannelSource, shared_device

FAKE = Path(__file__).with_name("fake_rtl_sdr.py")
CENTER = 90_000_000


@pytest.fixture
def fake_rtl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable `rtl_sdr` stand-in; returns the file it logs each start to."""
    exe = tmp_path / "rtl_sdr"
    exe.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    spawns = tmp_path / "spawns.txt"
    monkeypatch.setenv("FAKE_RTL_SPAWNS", str(spawns))
    monkeypatch.setenv("FAKE_RTL_STATIONS", "300000:1000,-400000:1500")
    return spawns


def device(tmp_path: Path, **kw: object) -> RtlSdrDevice:
    config = SdrDevice("a", "00001234", CENTER, 20.0)
    return RtlSdrDevice(config, command=str(tmp_path / "rtl_sdr"), backoff_initial=0.01, **kw)


def tuning(dev: RtlSdrDevice, offset: int) -> SdrTuning:
    return SdrTuning(dev.config, CENTER + offset)


def take(source: SdrChannelSource, seconds: float) -> list:
    it = iter(source)
    chunks, got = [], 0.0
    while got < seconds:
        chunks.append(next(it))
        got += chunks[-1].duration
    source.close()
    it.close()
    return chunks


def dominant_hz(audio: np.ndarray) -> float:
    seg = audio[4000:]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return float(np.fft.rfftfreq(len(seg), 1 / 16_000)[np.argmax(spec)])


def test_two_stations_share_one_rtl_sdr_process(tmp_path: Path, fake_rtl: Path) -> None:
    dev = device(tmp_path)
    a = SdrChannelSource("a", tuning(dev, 300_000), device=dev)
    b = SdrChannelSource("b", tuning(dev, -400_000), device=dev)
    it_b = iter(b)
    next(it_b)  # b attaches first and starts the device
    chunks_a = take(a, 1.5)
    chunks_b = [next(it_b) for _ in range(6)]
    b.close()
    it_b.close()
    assert dominant_hz(np.concatenate([c.samples for c in chunks_a])) == pytest.approx(1000, abs=5)
    assert dominant_hz(np.concatenate([c.samples for c in chunks_b])) == pytest.approx(1500, abs=5)
    assert chunks_a[0].discontinuity and not chunks_a[1].discontinuity
    assert a.health.signal_db == pytest.approx(10 * np.log10(0.2**2), abs=1.5)
    assert a.health.clipped is not None and a.health.clipped < 0.01
    assert fake_rtl.read_text().splitlines() == [f"-d 00001234 -f {CENTER} -s 2400000 -g 20 -"]
    _wait(lambda: dev._proc is None)  # the last detach stopped rtl_sdr


def test_a_dongles_frequency_correction_reaches_rtl_sdr(tmp_path: Path, fake_rtl: Path) -> None:
    config = SdrDevice("a", "00001234", CENTER, 20.0, ppm=-37)
    dev = RtlSdrDevice(config, command=str(tmp_path / "rtl_sdr"), backoff_initial=0.01)
    take(SdrChannelSource("a", tuning(dev, 300_000), device=dev), 0.3)
    # rtl_sdr corrects the tuner and the sample rate itself: nothing else has to know.
    assert fake_rtl.read_text().splitlines() == [
        f"-d 00001234 -f {CENTER} -s 2400000 -g 20 -p -37 -"
    ]


def test_device_restarts_rtl_sdr_and_marks_the_gap(
    tmp_path: Path, fake_rtl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RTL_MAX_BYTES", str(2 * 2_400_000))  # exits after 1 s of IQ
    dev = device(tmp_path)
    source = SdrChannelSource("a", tuning(dev, 300_000), device=dev)
    chunks = take(source, 3.0)
    assert dev.reconnects >= 1
    assert len(fake_rtl.read_text().splitlines()) >= 2
    assert chunks[0].discontinuity
    assert any(c.discontinuity for c in chunks[1:])  # the first chunk after the restart
    assert source.health.reconnects >= 1


def test_a_first_drop_counts_as_one_reconnect(
    tmp_path: Path, fake_rtl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RTL_MAX_BYTES", str(2 * 2_400_000))  # exits after 1 s of IQ
    config = SdrDevice("a", "00001234", CENTER, 20.0)
    dev = RtlSdrDevice(config, command=str(tmp_path / "rtl_sdr"), backoff_initial=30.0)
    source = SdrChannelSource("a", tuning(dev, 300_000), device=dev)
    it = iter(source)
    next(it)
    deadline = time.monotonic() + 5
    while source.health.connected and time.monotonic() < deadline:
        time.sleep(0.02)
    # The page reads 0 as "still connecting": a receiver that was working and dropped is not.
    assert not source.health.connected and source.health.reconnects == 1
    source.close()
    it.close()


def test_missing_dongle_backs_off_and_closes_promptly(
    tmp_path: Path, fake_rtl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_RTL_MISSING", "1")
    dev = device(tmp_path)
    source = SdrChannelSource("a", tuning(dev, 300_000), device=dev)
    it = iter(source)
    threading.Timer(0.5, source.close).start()
    t0 = time.monotonic()
    assert list(it) == []
    assert time.monotonic() - t0 < 2.0
    assert dev.reconnects >= 2 and not source.health.connected


def test_a_channel_whose_demodulator_raises_fails_loudly_and_can_start_again(
    tmp_path: Path, fake_rtl: Path
) -> None:
    dev = device(tmp_path)
    a = SdrChannelSource("a", tuning(dev, 300_000), device=dev)
    b = SdrChannelSource("b", tuning(dev, -400_000), device=dev)
    it_b = iter(b)
    next(it_b)

    def broken(iq: np.ndarray) -> np.ndarray:
        raise IndexError("RDS block offset out of range")

    a._demod = broken
    with pytest.raises(RuntimeError, match="RDS block offset out of range"):
        next(iter(a))  # not a quiet end, which would read as the station being stopped
    chunks = take(a, 1.0)  # iterated again, as the pipeline does after a delay
    assert chunks[0].discontinuity
    assert dominant_hz(np.concatenate([c.samples for c in chunks])) == pytest.approx(1000, abs=5)
    next(it_b)  # b played on throughout
    b.close()
    it_b.close()


def test_a_dongle_cannot_be_retuned_by_another_station() -> None:
    a = SdrDevice("a", "00000777", CENTER, 20.0)
    assert shared_device(a) is shared_device(a)
    with pytest.raises(ValueError, match="already in use"):
        shared_device(SdrDevice("a", "00000777", CENTER + 1_000_000, 20.0))


def test_open_source_prefers_the_sdr() -> None:
    sdr = SdrTuning(SdrDevice("x", "00000555", CENTER, 20.0), CENTER + 300_000)
    st = Station("s", "S", "pt-PT", ("en",), (), stream="https://example.invalid/s.mp3", sdr=sdr)
    assert isinstance(open_source(st), SdrChannelSource)
    with pytest.raises(ValueError, match="neither"):
        open_source(Station("t", "T", "pt-PT", ("en",), ()))


def _wait(cond, timeout: float = 3.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.02)


class FirstSeconds:
    """Ends a live source after a few seconds of audio, so the pipeline can finish."""

    def __init__(self, source: SdrChannelSource, seconds: float) -> None:
        self.station_id, self._source, self._seconds = source.station_id, source, seconds

    @property
    def health(self):
        return self._source.health

    def close(self) -> None:
        self._source.close()

    def __iter__(self):
        got = 0.0
        for chunk in self._source:
            yield chunk
            got += chunk.duration
            if got >= self._seconds:
                break
        self._source.close()


@pytest.mark.models
@requires_say
@requires_ffmpeg
def test_speech_broadcast_over_fm_is_transcribed_and_translated(
    tmp_path: Path, fake_rtl: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end without hardware: TTS speech, FM-modulated as a transmitter would, read through
    the rtl_sdr pipe, demodulated, then VAD, ASR and translation."""
    from wavescribe.pipeline import Pipeline, plan_models
    from wavescribe.registry import ModelError, ModelRegistry

    station = Station("fm", "FM", "pt-PT", ("en",), ())
    try:
        plan = plan_models(ModelRegistry.load(), [station], translate=True)
    except ModelError as e:
        pytest.skip(str(e))
    speech = say_to_pcm(tmp_path / "pt", "O abastecimento de água na cidade foi reposto.", "Joana")
    speech.astype(np.float32).tofile(tmp_path / "speech.f32")
    monkeypatch.setenv("FAKE_RTL_AUDIO", str(tmp_path / "speech.f32"))
    monkeypatch.setenv("FAKE_RTL_STATIONS", "300000:1000")
    dev = device(tmp_path)
    seconds = len(speech) / 16_000 + 3
    results: list = []
    Pipeline(
        [station],
        plan,
        results.append,
        source_factory=lambda st: FirstSeconds(
            SdrChannelSource(st.id, tuning(dev, 300_000), device=dev), seconds
        ),
    ).run(duration=120)
    text = " ".join(r.transcript.text for r in results).lower()
    assert "água" in text
    assert any("water" in r.translations.get("en", "").lower() for r in results)


def test_a_serial_that_reads_as_an_index_is_opened_by_its_index() -> None:
    from wavescribe.sources.sdr import device_arg, reads_as_index

    plugged = [{"index": "0", "serial": "00001234"}, {"index": "1", "serial": "00000001"}]
    assert reads_as_index("00000001") and reads_as_index("7") and not reads_as_index("00001234")
    assert device_arg("00000001", lambda: plugged) == "1"
    assert device_arg("00001234", lambda: plugged) == "00001234"  # no lookup needed
    assert device_arg("00000001", lambda: []) == "00000001"  # not listed: rtl_sdr says so
    shared = [{"index": "0", "serial": "00000001"}, {"index": "1", "serial": "00000001"}]
    assert device_arg("00000001", lambda: shared) == "00000001"  # and a warning to re-serial
