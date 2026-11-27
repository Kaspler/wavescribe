# SPDX-License-Identifier: Apache-2.0
"""The watch detector's measurement: one sliding statistic that every channel reads, on
synthetic IQ (no hardware)."""

from __future__ import annotations

import time

import numpy as np

from tests.test_radio_watch_detect import BELOW, FS, PMR, Burst, air
from wavescribe.dsp.activity import GUARD_HZ, MEASURED, ActivityDetector

WIDTH = 12_500.0


class Tapped(ActivityDetector):
    """A detector that keeps what each frame was measured from, and what it read."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.seen: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []

    def _frame(self, power: np.ndarray, start: int, end: int) -> list:
        floor = None if self._floor is None else self._floor.copy()
        out = super()._frame(power, start, end)
        if floor is not None and self._floor is not None:
            self.seen.append((power.copy(), floor, self.snr_db.copy(), self.sliding_db()))
        return out


def per_channel_db(
    d: ActivityDetector, power: np.ndarray, floor: np.ndarray, spurs: tuple[int, ...] = ()
) -> np.ndarray:
    """Each channel against its floor, measured one channel at a time: the sum of the bins
    in the middle of the channel, clear of the centre and of the learnt spurs, over the sum
    of their floors."""
    offsets = (np.arange(len(power)) - len(power) // 2) * d.rate / len(power)
    left_out = np.abs(offsets) <= GUARD_HZ
    for s in spurs:
        left_out |= np.abs(offsets - (s - d.center_hz)) <= GUARD_HZ
    out = np.zeros(len(d.freqs))
    for i, f in enumerate(d.freqs):
        mine = (np.abs(offsets - (f - d.center_hz)) <= MEASURED * WIDTH / 2) & ~left_out
        out[i] = 10 * np.log10((power[mine].sum() + 1e-20) / (floor[mine].sum() + 1e-20))
    return out


def test_every_channel_reads_what_it_measured_one_channel_at_a_time() -> None:
    # Channels on a raster, one over the dongle's centre, one over a learnt spur, one beyond
    # the window; bursts on some, between two, and noise on the rest.
    channels = (*PMR, BELOW + 1_000, PMR[0] + 5_000_000)
    spurs = (PMR[9] + 2_000,)
    d = Tapped(FS, BELOW, channels, WIDTH, spurs=spurs)
    bursts = [
        Burst(PMR[2], 1.0, 2.0),
        Burst(PMR[9] + 2_300, 0.0, 9.0, amp=0.02, deviation_hz=0.0),
        Burst((PMR[12] + PMR[13]) // 2, 1.2, 2.2, amp=0.3),
    ]
    for iq, _ in air(2.6, bursts):
        d.feed(iq)
    assert len(d.seen) > 30
    worst = max(
        float(np.abs(read - per_channel_db(d, power, floor, spurs)).max())
        for power, floor, read, _ in d.seen
    )
    assert worst < 0.01
    strongest = max(float(read[2]) for _, _, read, _ in d.seen)
    assert strongest > 20  # the burst was in what was compared


def test_the_statistic_at_a_channels_centre_is_the_channels_own() -> None:
    # Channels centred on an FFT bin have the same bins either side of it, so what slides
    # across the window reads there exactly what the channel does.
    bin_hz = FS / 4096
    at = [900, 1_200, 2_048 + 700, 3_500]
    channels = tuple(round(BELOW + (k - 2_048) * bin_hz) for k in at)
    d = Tapped(FS, BELOW, channels, WIDTH)
    for iq, _ in air(1.6, [Burst(channels[1], 0.8, 1.4), Burst(channels[2] + 3_000, 0.9, 1.5)]):
        d.feed(iq)
    assert len(d.seen) > 15
    for _, _, read, sliding in d.seen:
        assert np.abs(sliding[at] - read).max() < 0.01
    assert max(float(read[1]) for _, _, read, _ in d.seen) > 20


def raster(step_hz: int, centre: int, rate: int) -> tuple[int, ...]:
    reach = rate // 2 - 75_000
    return tuple(range(centre - reach, centre + reach, step_hz))


def test_four_hundred_channels_cost_what_sixteen_do() -> None:
    rate, centre = 2_400_000, PMR[0] - 250_000
    many = raster(5_000, centre, rate)  # 25 kHz channels every 5 kHz, across the window
    assert len(many) >= 449
    rng = np.random.default_rng(3)
    block = (0.006 * rng.standard_normal(2 * 614_400, dtype=np.float32)).view(np.complex64)

    def seconds(channels: tuple[int, ...], width: float, step: float) -> float:
        d = ActivityDetector(rate, centre, channels, width, step_hz=step)
        for _ in range(3):
            d.feed(block)  # the floors are learnt
        t0 = time.perf_counter()
        for _ in range(4):
            d.feed(block)
        return time.perf_counter() - t0

    # Each against the other on the same machine at the same moment, the best of several
    # turns: a weight per channel per bin took about three times as long for these channels.
    few_s, many_s = 1e9, 1e9
    for _ in range(7):
        few_s = min(few_s, seconds(PMR, 12_500.0, 12_500.0))
        many_s = min(many_s, seconds(many, 25_000.0, 5_000.0))
    assert many_s < 2.0 * few_s

    # And nothing is held per channel per bin (that was 30 MB for these channels).
    d = ActivityDetector(rate, centre, many, 25_000.0, step_hz=5_000.0)
    d.feed(block)
    held = sum(v.nbytes for v in vars(d).values() if isinstance(v, np.ndarray))
    assert held < 3_000_000
