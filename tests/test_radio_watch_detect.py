# SPDX-License-Identifier: Apache-2.0
"""A watch noticing and catching transmissions, on synthetic 8-bit IQ (no hardware)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np
import pytest

from wavescribe.config import SdrDevice, SdrTuning
from wavescribe.dsp.activity import OVERLOAD_DB, IqHistory
from wavescribe.dsp.wfm import iq_from_u8
from wavescribe.radio.watch import Catch, Watch
from wavescribe.sources.watch import SAY_DEAF_S, Catcher

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
            if b.image_db is not None:  # the weaker mirror a receiver shows of strong signals
                x += 10 ** (-b.image_db / 20) * np.conj(wave)
        if extra is not None:
            x += extra(t)
        pairs = np.empty(2 * BLOCK)
        pairs[0::2], pairs[1::2] = x.real, x.imag
        raw = np.clip(np.rint(pairs * 128 + 127.4), 0, 255).astype(np.uint8)
        yield iq_from_u8(raw), T0 + (k + 1) * BLOCK / FS


def catcher(
    centre: int = BELOW,
    *,
    spurs: tuple[int, ...] = (),
    spur_bin_hz: float = 0.0,
    max_active: int = 4,
    carrier_s: float = 120,
    privacy: str = "private",
) -> Catcher:
    watch = Watch("pmr446", PMR, 12_500)
    device = SdrDevice("a", "00001234", centre, 30.0, FS)
    tuning = SdrTuning(device, PMR[7], mode="nfm", watch=watch)
    return Catcher(
        tuning,
        spurs=spurs,
        spur_bin_hz=spur_bin_hz,
        max_active=max_active,
        carrier_s=carrier_s,
        privacy=privacy,
    )


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


def test_a_learnt_spur_is_left_out_as_widely_as_its_bin() -> None:
    # Learnt at 6 kHz bins, a line listed 1.5 kHz above channel 4 may be 2.5 kHz off that.
    listed = PMR[3] + 1_500

    def own(t: np.ndarray) -> np.ndarray:
        return 0.05 * ((t > 1.5) & (t < 3.0)) * np.exp(2j * np.pi * (listed + 2_500 - BELOW) * t)

    assert run(catcher(spurs=(listed,), spur_bin_hz=6_000), air(4.5, [], extra=own)) == []
    # Taken as exact, the list would have let it through.
    [unplaced] = run(catcher(spurs=(listed,)), air(4.5, [], extra=own))
    assert unplaced.freq_hz == PMR[3]


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


def test_a_strong_signals_splatter_on_its_neighbours_is_no_catch() -> None:
    strong = Burst(PMR[6], 1.0, 3.0, amp=0.6)
    splatter = Burst(PMR[7], 1.0, 3.0, amp=0.6 / 20)  # 26 dB down, still well over the noise
    [got] = run(catcher(), air(4.5, [strong, splatter], noise=0.01))
    assert got.freq_hz == PMR[6]


def test_overlapping_channels_catch_a_transmission_once_where_it_is() -> None:
    """Region 2's 2 m plan: a 25 kHz channel every 5 kHz, so each transmission is heard on
    the channels either side of its own as well."""
    centre, f0 = 146_000_000, 146_520_000
    channels = tuple(146_300_000 + 5_000 * k for k in range(60))
    device = SdrDevice("a", "00001234", centre, 30.0, FS)
    watch = Watch((146.3, 146.595), channels, 5_000)
    c = Catcher(SdrTuning(device, channels[30], mode="nfm", channel_hz=25_000, watch=watch))

    def voice(t: np.ndarray) -> np.ndarray:
        on = (t >= 1.0) & (t < 5.0)
        m = np.sin(2 * np.pi * 300 * t) + 0.7 * np.sin(2 * np.pi * 1_100 * t + 1)
        m = (m + 0.5 * np.sin(2 * np.pi * 2_300 * t + 2)) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t))
        phase = 2 * np.pi * ((f0 - centre) * t + 2_500 * np.cumsum(m) / FS)
        return on * 0.3 * np.exp(1j * phase)

    [got] = run(c, air(7.0, [], centre=centre, extra=voice))
    assert got.freq_hz == f0 and got.duration == pytest.approx(4.0, abs=0.15)


def test_a_catch_after_a_carrier_never_repeats_the_carriers_audio() -> None:
    # A carrier, learnt into the floor after 2 s, then someone much stronger on top of it: the
    # second catch's pre-roll starts where the first catch's audio ended, not before.
    bursts = [Burst(PMR[3], 1.0, 6.0, deviation_hz=0.0), Burst(PMR[3], 3.2, 5.0, amp=0.5)]
    first, second = run(catcher(carrier_s=2.0), air(7.5, bursts))
    assert first.reason == "carrier" and second.t_start == pytest.approx(T0 + 3.2, abs=0.1)
    assert second.audio_from == pytest.approx(first.audio_to, abs=1e-6)


def louder(start_s: float, stop_s: float = 1e9, amp: float = 0.14) -> Callable:
    """More noise across the whole window (about 9 dB more): a charger, USB, overload."""
    rng = np.random.default_rng(5)

    def noise(t: np.ndarray) -> np.ndarray:
        on = (t >= start_s) & (t < stop_s)
        return on * amp * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))

    return noise


def test_noise_rising_across_the_window_is_no_catch() -> None:
    c = catcher()
    assert run(c, air(4.0, [], extra=louder(1.5, 2.5))) == []
    assert c.missed == 0 and c.deaf_s is None


def test_a_lasting_rise_of_the_noise_is_learnt_and_said(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO", logger="wavescribe")
    c = catcher()
    deaf = []
    got = []
    for iq, t_end in air(18.0, [Burst(PMR[3], 15.0, 16.5, amp=0.5)], extra=louder(2.0)):
        got += c.feed(iq, t_end)
        deaf.append(c.deaf_s)
    # Deaf while the noise is new, said so after a few seconds, and hearing again once its
    # floors have been learnt afresh: the burst after it is caught.
    assert max(d or 0 for d in deaf) == pytest.approx(10.0, abs=0.6) and deaf[-1] is None
    assert [g.freq_hz for g in got] == [PMR[3]]
    assert got[0].t_start == pytest.approx(T0 + 15.0, abs=0.1)
    said = [r for r in caplog.records if r.getMessage().startswith("watch_")]
    assert [r.getMessage().split()[0] for r in said] == ["watch_deaf", "watch_hearing"]
    assert [r.fields["reason"] for r in said] == ["interference", "interference"]


def keyed_close(
    freq_hz: int,
    start_s: float,
    stop_s: float,
    *,
    centre: int = BELOW,
    amp: float = 2.0,
    rise: float = 0.15,
    intermod: bool = True,
) -> Callable[[np.ndarray], np.ndarray]:
    """A walkie-talkie keyed a few metres from the antenna (a 1 kHz tone at 2 kHz deviation),
    with what it does to the receiver: ``amp`` times full scale, so the converter clips, and
    across the window noise (``rise``: about 24 dB over ``air``'s 0.01) and an intermodulation
    copy at twice the deviation, 100 kHz up and 30 dB down."""
    rng = np.random.default_rng(7)

    def overload(t: np.ndarray) -> np.ndarray:
        on = (t >= start_s) & (t < stop_s)
        if not on.any():
            return np.zeros(len(t))  # quicker over a long air
        x = amp * np.exp(1j * (2 * np.pi * (freq_hz - centre) * t + 2 * np.sin(2e3 * np.pi * t)))
        x = x + rise * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))
        if intermod:
            copy = 2 * np.pi * (freq_hz + 100_000 - centre) * t + 4 * np.sin(2e3 * np.pi * t)
            x = x + 0.03 * amp * np.exp(1j * copy)
        return on * x

    return overload


def test_a_walkie_talkie_overloading_the_receiver_is_caught_on_its_channel(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="wavescribe")
    c = catcher()
    risen, got = [], []
    for iq, t_end in air(8.5, [], noise=0.01, extra=keyed_close(PMR[3], 2.0, 6.0)):
        got += c.feed(iq, t_end)
        risen.append(int((c.detector.snr_db >= c.detector.threshold_db).sum()))
    # The whole window rose with it, as with interference, where the watch used to stay deaf.
    assert max(risen) == len(PMR)
    [caught] = got
    assert caught.freq_hz == PMR[3] and caught.reason == "overload"
    assert caught.t_start == pytest.approx(T0 + 2.0, abs=0.1)
    assert caught.duration == pytest.approx(4.0, abs=0.15)
    assert dominant_hz(caught.audio) == pytest.approx(1_000, abs=10)  # FM comes through clipping
    said = {r.getMessage(): r.fields for r in caplog.records if r.getMessage().startswith("watch")}
    assert said["watch_deaf"]["reason"] == "overload" and said["watch_deaf"]["clipped"] > 0.1
    assert "mhz" not in said["watch_deaf"]  # walkie-talkies: the log keeps no frequency
    assert said["watch_deaf"]["on_channel"] is True
    assert said["watch_hearing"]["reason"] == "overload" and not said["watch_hearing"]["learnt"]
    assert c.deaf_s is None and c.overload is not None
    assert c.overload["freq_hz"] == PMR[3] and c.overload["on_channel"] is True
    assert c.overload["t"] == pytest.approx(T0 + 6.0, abs=0.1)


def test_an_overload_longer_than_it_takes_to_learn_the_noise_is_one_catch() -> None:
    # Interference this long would be learnt as the new floor; a transmission isn't, so it is
    # caught all through, and a weak one just after it is heard over the floor as it was.
    after = Burst(PMR[9], 16.5, 18.0, amp=0.05)
    c = catcher()
    got = run(c, air(19.5, [after], noise=0.01, extra=keyed_close(PMR[12], 1.0, 15.0)))
    assert [(g.freq_hz, g.reason) for g in got] == [(PMR[12], "overload"), (PMR[9], None)]
    assert got[0].duration == pytest.approx(14.0, abs=0.15)
    assert c.detector.relearnt == 0


@pytest.mark.parametrize("window", ["pmr446", "2 m, about the centre"])
def test_clipping_hard_catches_the_signal_and_none_of_its_copies(window: str) -> None:
    # The converter's clipping copies a strong signal elsewhere in the window, 10 to 25 dB under
    # it: the third-order copy at three times its offset the other way, folded back into the
    # band, which a window about the centre has inside it.
    if window == "pmr446":
        c, centre, f = catcher(), BELOW, PMR[1]
    else:
        centre, f = 145_006_250, 145_100_000
        channels = tuple(144_600_000 + 12_500 * k for k in range(65))
        device = SdrDevice("a", "00001234", centre, 30.0, FS)
        watch = Watch((144.6, 145.4), channels, 12_500)
        c = Catcher(SdrTuning(device, channels[32], mode="nfm", watch=watch))
    loud = keyed_close(f, 1.0, 4.0, centre=centre, amp=3.0, rise=0.0, intermod=False)
    got, copies = [], []
    for iq, t_end in air(6.5, [], centre=centre, noise=0.01, extra=loud):
        got += c.feed(iq, t_end)
        others = c.detector.snr_db[np.array(c.watch.channels) != f]
        copies.append(float(others.max()))
    assert max(copies) > 40  # the copies are there, well over the noise
    assert [(g.freq_hz, g.reason) for g in got] == [(f, "overload")]


def test_a_window_rising_with_no_channel_on_top_stays_deaf() -> None:
    # Two walkie-talkies close by at once, a few dB apart: nothing stands over the rest.
    both = [keyed_close(PMR[3], 1.0, 4.0), keyed_close(PMR[11], 1.0, 4.0, amp=1.6)]
    c = catcher()
    reasons = set()
    for iq, t_end in air(6.5, [], noise=0.01, extra=lambda t: both[0](t) + both[1](t)):
        assert c.feed(iq, t_end) == []
        reasons.add(c.detector.deaf_reason)
    assert reasons == {None, "interference"} and c.overload is None

    # A switching supply: lines every 50 kHz on a raised floor.
    def comb(t: np.ndarray) -> np.ndarray:
        on = (t >= 1.0) & (t < 4.0)
        lines = sum(np.exp(2j * np.pi * (f - BELOW) * t) for f in PMR[1::4])
        return on * (0.05 * lines + louder(0.0)(t))

    c = catcher()
    assert run(c, air(6.5, [], noise=0.01, extra=comb)) == [] and c.overload is None


ASIDE = PMR[0] - 200_000  # a centre of our own, under the band like BELOW


def product(freq_hz: int, start_s: float, stop_s: float, centre: int, amp: float = 0.25):
    """One of a transmitter's products on another frequency (its own spurious emission, or the
    receiver's mixing): what made a channel stand over the rest on the air while the transmitter
    itself was on none of them."""

    def copy(t: np.ndarray) -> np.ndarray:
        on = (t >= start_s) & (t < stop_s)
        phase = 2 * np.pi * (freq_hz - centre) * t + 2 * np.sin(2e3 * np.pi * t)
        return on * amp * np.exp(1j * phase)

    return copy


def test_a_walkie_talkie_keyed_off_the_channels_at_the_centre_is_said_and_not_caught(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """On the air: a walkie-talkie keyed close by, a little under the receiver's centre, not on
    one of the band's channels. The whole window rose, and one of its products stood over the
    rest on channel 2: that channel was caught, as the transmitter close by, sending the speech
    model nothing but noise."""
    caplog.set_level("INFO", logger="wavescribe")
    tx = ASIDE - 1_250
    loud = keyed_close(tx, 2.0, 6.0, centre=ASIDE, intermod=False)
    copy = product(PMR[1], 2.0, 6.0, ASIDE)
    c = catcher(ASIDE)
    got, on_top, reasons = [], [], set()
    for iq, t_end in air(8.5, [], centre=ASIDE, noise=0.01, extra=lambda t: loud(t) + copy(t)):
        got += c.feed(iq, t_end)
        snr = c.detector.snr_db
        on_top.append(float(snr[1] - snr[5:].max()))  # over every channel beyond its splatter
        reasons.add(c.detector.deaf_reason)
    assert max(on_top) > OVERLOAD_DB  # the product would have been caught as the transmitter
    assert got == [] and c.detector.busy == []
    assert reasons == {None, "interference", "overload"}  # sure of it after two frames
    d = c.detector
    assert d.overload_hz == 445_805_000 and d.overload_on_channel is False
    assert c.overload is not None and c.overload["freq_hz"] == 445_805_000
    assert c.overload["on_channel"] is False
    assert c.overload["t"] == pytest.approx(T0 + 6.0, abs=0.1)
    assert ("It was at about 445.805 MHz, not on one of the channels watched (446.00625 to "
            "446.19375 MHz), so nothing was caught.") in str(c.overload["message"])  # fmt: skip
    said = {r.getMessage(): r.fields for r in caplog.records if r.getMessage().startswith("watch")}
    assert said["watch_deaf"]["reason"] == "overload" and said["watch_deaf"]["on_channel"] is False
    assert "mhz" not in said["watch_deaf"]  # walkie-talkies: the log keeps no frequency
    assert said["watch_hearing"]["reason"] == "overload"


@pytest.mark.parametrize(
    ("where", "tx", "named"),
    [
        ("between two channels", PMR[7] + 6_250, 446_100_000),
        ("under the band", PMR[0] - 100_000, 445_906_000),
        ("at the window's edge", PMR[-1] + 150_000, None),  # where signals from beyond fold in
    ],
)
def test_a_walkie_talkie_off_the_channels_elsewhere_in_the_window_is_not_caught(
    where: str, tx: int, named: int | None
) -> None:
    # With its copy 100 kHz up: under the band, that is channel 1, standing over the rest.
    c = catcher(privacy="amateur")
    assert run(c, air(8.0, [], noise=0.01, extra=keyed_close(tx, 2.0, 5.0))) == []
    assert c.detector.overload_hz == named and c.detector.overload_on_channel is False
    assert c.overload is not None and c.overload["freq_hz"] == named
    words = "It was at about 446.1 MHz, not" if named == 446_100_000 else ""
    assert words in str(c.overload["message"])


def test_an_overload_off_the_channels_is_said_at_once(caplog: pytest.LogCaptureFixture) -> None:
    # Too short to be said as deafness, and no catch says it: said all the same.
    caplog.set_level("INFO", logger="wavescribe")
    c = catcher(privacy="amateur")
    warnings = []
    for iq, t_end in air(4.0, [], noise=0.01, extra=keyed_close(PMR[0] - 100_000, 1.0, 2.0)):
        c.feed(iq, t_end)
        warnings.append(c.warning)
    assert any(w and "It was at about 445.906 MHz" in w for w in warnings) and warnings[-1] is None
    said = [(r.getMessage(), r.fields) for r in caplog.records if r.getMessage().startswith("w")]
    assert [m for m, _ in said] == ["watch_deaf", "watch_hearing"]
    assert said[0][1]["mhz"] == pytest.approx(445.906) and said[0][1]["on_channel"] is False
    assert said[1][1]["deaf_s"] < SAY_DEAF_S


@pytest.mark.parametrize("on_channel", [True, False])
def test_two_key_ups_a_minute_apart_are_two_overloads(
    on_channel: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """On the air, the second of two key-ups a minute apart left no trace in the log: shorter
    than it takes to be said as deafness, it was only caught, on a product's channel."""
    caplog.set_level("INFO", logger="wavescribe")
    tx = PMR[3] if on_channel else PMR[0] - 100_000
    first, second = keyed_close(tx, 1.0, 5.0), keyed_close(tx, 61.0, 63.0)
    c = catcher(privacy="amateur")
    got = run(c, air(64.5, [], noise=0.01, extra=lambda t: first(t) + second(t)))
    said = [(r.getMessage(), r.fields) for r in caplog.records if r.getMessage().startswith("w")]
    if on_channel:
        # The second is said with its catch, as before: too short to be deafness.
        assert [(g.freq_hz, g.reason) for g in got] == [(tx, "overload")] * 2
        assert got[1].t_start == pytest.approx(T0 + 61.0, abs=0.1)
        assert [m for m, _ in said] == ["watch_deaf", "watch_hearing"]
    else:
        assert got == []
        assert [m for m, _ in said] == ["watch_deaf", "watch_hearing"] * 2
        assert [f["mhz"] for m, f in said if m == "watch_deaf"] == [pytest.approx(445.906)] * 2
    assert all(f["reason"] == "overload" for _, f in said)
    assert c.overload is not None and c.overload["t"] == pytest.approx(T0 + 63.0, abs=0.1)


def test_one_overload_is_one_transmitter_named_from_its_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second transmitter close by keyed before the first's overload is over is part of it:
    the first's channel is the one caught and named, in the log, the status and the catch, and
    no other channel starts. Once it is over, the next overload is named afresh."""
    caplog.set_level("INFO", logger="wavescribe")
    keys = [keyed_close(PMR[3], 1.0, 4.0, intermod=False),
            keyed_close(PMR[11], 3.5, 6.0, intermod=False),
            keyed_close(PMR[11], 9.0, 11.0, intermod=False)]  # fmt: skip
    c = catcher(privacy="amateur")
    got, named, reasons = [], [], []
    for iq, t_end in air(13.0, [], noise=0.01, extra=lambda t: sum(k(t) for k in keys)):
        got += c.feed(iq, t_end)
        named.append((round(t_end - T0, 1), c.detector.overload_hz))
        reasons.append((round(t_end - T0, 1), c.detector.deaf_reason))
    assert [(g.freq_hz, g.reason) for g in got] == [(PMR[3], "overload"), (PMR[11], "overload")]
    assert got[1].t_start == pytest.approx(T0 + 9.0, abs=0.1)
    assert {hz for t, hz in named if 1.5 < t < 8.5} == {PMR[3]}
    assert {hz for t, hz in named if t > 9.5} == {PMR[11]}
    # Overloaded all along once sure of it, though no channel stood over the rest while both
    # were on: not interference halfway through.
    assert {r for t, r in reasons if 1.5 < t < 6.0} == {"overload"}
    deaf = [f for r in caplog.records if r.getMessage() == "watch_deaf" for f in [r.fields]]
    assert deaf[0]["reason"] == "overload" and deaf[0]["mhz"] == pytest.approx(PMR[3] / 1e6)


def test_the_history_gives_back_the_dongles_samples() -> None:
    rng = np.random.default_rng(1)
    raw = rng.integers(0, 256, size=2 * 3_000, dtype=np.uint8)
    h = IqHistory(1_000, seconds=1.0)  # holds 1000 samples
    for k in range(3):
        h.append(iq_from_u8(raw[2_000 * k : 2_000 * (k + 1)]))
    assert h.start == 2_000 and h.end == 3_000
    assert np.array_equal(h.since(2_500), iq_from_u8(raw[5_000:]))
    assert np.array_equal(h.since(0), iq_from_u8(raw[4_000:]))  # older is gone
