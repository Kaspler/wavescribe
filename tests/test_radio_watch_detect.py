# SPDX-License-Identifier: Apache-2.0
"""A watch noticing and catching transmissions, on synthetic 8-bit IQ (no hardware)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np
import pytest

from wavescribe.config import SdrDevice, SdrTuning
from wavescribe.dsp.activity import IqHistory
from wavescribe.dsp.wfm import iq_from_u8
from wavescribe.radio.watch import Catch, Watch
from wavescribe.sources.watch import Catcher

FS = 1_200_000
BLOCK = FS * 256 // 1000  # what the dongle hands over at a time: 0.256 s
T0 = 1_000_000.0  # wall-clock time of the first sample
PMR = tuple(446_006_250 + 12_500 * k for k in range(16))  # PMR446 channels 1 to 16
BELOW = PMR[0] - 250_000  # where a watch puts the centre: under the band, clear of its spike
BETWEEN = 446_100_000  # the centre between channels 8 and 9, as a wide window has it


@dataclass
class Burst:
    freq_hz: int
    start_s: float
    stop_s: float
    amp: float = 0.1
    tone_hz: float = 1_000.0
    deviation_hz: float = 2_000.0
    image_db: float | None = None  # also its mirror about the centre, this far down


def air(
    seconds: float,
    bursts: list[Burst],
    *,
    centre: int = BELOW,
    noise: float = 0.05,
    extra: Callable[[np.ndarray], np.ndarray] | None = None,
) -> Iterator[tuple[np.ndarray, float]]:
    """Blocks of IQ as a dongle tuned to ``centre`` would send them: noise, the bursts, and
    whatever ``extra`` adds, quantised to 8 bits."""
    rng = np.random.default_rng(0)
    for k in range(int(seconds * FS) // BLOCK):
        t = (k * BLOCK + np.arange(BLOCK)) / FS
        x = noise * (rng.standard_normal(BLOCK) + 1j * rng.standard_normal(BLOCK))
        for b in bursts:
            on = (t >= b.start_s) & (t < b.stop_s)
            if not on.any():
                continue
            phase = 2 * np.pi * (b.freq_hz - centre) * t
            phase += b.deviation_hz / b.tone_hz * np.sin(2 * np.pi * b.tone_hz * t)
            wave = on * b.amp * np.exp(1j * phase)
            x += wave
            if b.image_db is not None:  # the dongle's I/Q imbalance
                x += 10 ** (-b.image_db / 20) * np.conj(wave)
        if extra is not None:
            x += extra(t)
        pairs = np.empty(2 * BLOCK)
        pairs[0::2], pairs[1::2] = x.real, x.imag
        raw = np.clip(np.rint(pairs * 128 + 127.4), 0, 255).astype(np.uint8)
        yield iq_from_u8(raw), T0 + (k + 1) * BLOCK / FS


def catcher(
    centre: int = BELOW, *, spurs: tuple[int, ...] = (), max_active: int = 4, carrier_s: float = 120
) -> Catcher:
    watch = Watch("pmr446", PMR, 12_500)
    device = SdrDevice("a", "00001234", centre, 30.0, FS)
    tuning = SdrTuning(device, PMR[7], mode="nfm", watch=watch)
    return Catcher(tuning, spurs=spurs, max_active=max_active, carrier_s=carrier_s)


def run(c: Catcher, blocks: Iterator[tuple[np.ndarray, float]]) -> list[Catch]:
    out = []
    for iq, t_end in blocks:
        out += c.feed(iq, t_end)
    return out


def dominant_hz(audio: np.ndarray) -> float:
    spec = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    return float(np.fft.rfftfreq(len(audio), 1 / 16_000)[np.argmax(spec)])


def heard_from(c: Catch) -> float:
    """When the catch's audio first carries sound: 10 ms windows against the loudest."""
    frames = c.audio[: len(c.audio) // 160 * 160].reshape(-1, 160)
    rms = np.sqrt((frames**2).mean(axis=1))
    return c.audio_from + int(np.argmax(rms > 0.2 * rms.max())) * 0.01


def test_a_burst_on_one_channel_is_caught_from_its_first_moment() -> None:
    c = catcher()
    [got] = run(c, air(4.5, [Burst(PMR[2], 1.30, 3.00)]))
    assert got.freq_hz == PMR[2] and got.mode == "nfm"
    assert got.t_start == pytest.approx(T0 + 1.30, abs=0.1)
    assert got.duration == pytest.approx(1.70, abs=0.15)
    assert got.audio_from < T0 + 1.30  # the pre-roll: from before it was noticed
    assert heard_from(got) == pytest.approx(T0 + 1.30, abs=0.05)  # the first syllable is there
    assert dominant_hz(got.audio) == pytest.approx(1_000, abs=10)
    assert got.snr_db > 15 and got.reason is None
    assert c.missed == 0 and c.short == 0


def test_two_overlapping_bursts_are_two_catches_never_mixed() -> None:
    bursts = [Burst(PMR[1], 1.0, 3.0, tone_hz=800), Burst(PMR[8], 1.8, 3.8, tone_hz=1_500)]
    got = {c.freq_hz: c for c in run(catcher(), air(5.5, bursts))}
    assert sorted(got) == [PMR[1], PMR[8]]
    first, second = got[PMR[1]], got[PMR[8]]
    assert first.t_start == pytest.approx(T0 + 1.0, abs=0.1)
    assert second.t_start == pytest.approx(T0 + 1.8, abs=0.1)
    assert dominant_hz(first.audio) == pytest.approx(800, abs=10)
    assert dominant_hz(second.audio) == pytest.approx(1_500, abs=10)
    for c, other in ((first, 1_500), (second, 800)):
        spec = np.abs(np.fft.rfft(c.audio)) ** 2
        f = np.fft.rfftfreq(len(c.audio), 1 / 16_000)
        assert spec[np.abs(f - other) < 20].sum() < 1e-4 * spec.sum()


def test_a_carrier_that_stays_on_ends_once_and_is_learnt_into_the_floor() -> None:
    c = catcher(carrier_s=2.0)
    got = run(c, air(7.0, [Burst(PMR[4], 1.0, 99.0, deviation_hz=0.0)]))
    assert len(got) == 1  # not one catch after another while it stays on
    assert got[0].reason == "carrier" and got[0].duration == pytest.approx(2.0, abs=0.15)
    assert c.detector.busy == []


def test_the_dongles_own_lines_are_no_catch() -> None:
    spur = PMR[3] + 1_500

    def own(t: np.ndarray) -> np.ndarray:
        # The centre's spike, jumping as the dongle's DC offset does, and a spur that comes
        # and goes with what the computer's USB is doing.
        dc = 0.3 * (1.0 + 0.5 * np.sign(np.sin(2 * np.pi * t)))
        line = 0.05 * ((t > 1.5) & (t < 3.0)) * np.exp(2j * np.pi * (spur - BETWEEN) * t)
        return dc + line

    assert run(catcher(BETWEEN, spurs=(spur,)), air(4.5, [], centre=BETWEEN, extra=own)) == []
    # Without the learnt spur it would have been one: the test is not passing by accident.
    [unlearnt] = run(catcher(BETWEEN), air(4.5, [], centre=BETWEEN, extra=own))
    assert unlearnt.freq_hz == PMR[3]


def test_a_burst_shorter_than_a_catch_is_counted_not_kept() -> None:
    c = catcher()
    assert run(c, air(3.5, [Burst(PMR[5], 1.0, 1.15)])) == []
    assert c.short == 1


def test_more_transmissions_than_demodulators_are_counted_as_missed() -> None:
    bursts = [Burst(PMR[k], 1.0, 2.5) for k in (0, 2, 4, 6, 8, 10)]
    c = catcher(max_active=4)
    got = run(c, air(4.0, bursts))
    assert len(got) == 4 and c.missed == 2
    assert len({g.freq_hz for g in got}) == 4


def test_the_mirror_image_of_a_strong_signal_is_no_catch() -> None:
    strong = Burst(PMR[11], 1.0, 2.5, amp=0.6, image_db=30)  # its image lands on channel 5
    [got] = run(catcher(BETWEEN), air(4.0, [strong], centre=BETWEEN, noise=0.02))
    assert got.freq_hz == PMR[11]


def test_the_history_gives_back_the_dongles_samples() -> None:
    rng = np.random.default_rng(1)
    raw = rng.integers(0, 256, size=2 * 3_000, dtype=np.uint8)
    h = IqHistory(1_000, seconds=1.0)  # holds 1000 samples
    for k in range(3):
        h.append(iq_from_u8(raw[2_000 * k : 2_000 * (k + 1)]))
    assert h.start == 2_000 and h.end == 3_000
    assert np.array_equal(h.since(2_500), iq_from_u8(raw[5_000:]))
    assert np.array_equal(h.since(0), iq_from_u8(raw[4_000:]))  # older is gone
