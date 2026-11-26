# SPDX-License-Identifier: Apache-2.0
"""NFM and AM channels on synthetic IQ (no hardware, no recorded audio)."""

import numpy as np
import pytest

from wavescribe.dsp import off_centre
from wavescribe.dsp.channel import NarrowbandDemodulator, XlatingDecimator

FS = 2_400_000
# NFM audio: full deviation is 0.8 of full scale at 300 Hz, and the de-emphasis (one pole at
# 212 Hz) has taken 1 kHz down 8.9 dB from there.
TONE_1K = 0.8 * 10 ** (-8.9 / 20)


def nfm(
    n: int, offset: float, tone: float, deviation: float, amp: float, ctcss: float = 0
) -> np.ndarray:
    t = np.arange(n) / FS
    phase = 2 * np.pi * offset * t + deviation / tone * np.sin(2 * np.pi * tone * t)
    if ctcss:
        phase += 300 / ctcss * np.sin(2 * np.pi * ctcss * t)  # a sub-audible tone, ±300 Hz
    return amp * np.exp(1j * phase)


def am(n: int, offset: float, tone: float, depth: float, amp: float) -> np.ndarray:
    t = np.arange(n) / FS
    return amp * (1 + depth * np.sin(2 * np.pi * tone * t)) * np.exp(2j * np.pi * offset * t)


def noise(n: int, sigma: float = 0.003, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return sigma * (rng.normal(size=n) + 1j * rng.normal(size=n))


def carrier_amp(snr_db: float, width: float, sigma: float = 0.003) -> float:
    """The amplitude that puts a carrier ``snr_db`` over ``noise()`` in the channel's width."""
    return float(np.sqrt(2 * sigma**2 * width / FS * 10 ** (snr_db / 10)))


def tone_share_db(audio: np.ndarray, tone: float) -> float:
    seg = audio[8000:]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / 16_000)
    near = np.abs(f - tone) <= 30
    return float(10 * np.log10(spec[near].sum() / spec[~near].sum()))


def test_xlating_decimator_moves_the_channel_to_zero() -> None:
    n = FS // 4
    t = np.arange(n) / FS
    x = np.exp(2j * np.pi * (123_000 + 1_000) * t).astype(np.complex64)  # 1 kHz above the channel
    y = XlatingDecimator(FS, 123_000, 50, 5_000, 40_000)(x)
    f = np.fft.fftfreq(len(y), 1 / 48_000)[np.argmax(np.abs(np.fft.fft(y[200:], len(y))))]
    assert f == pytest.approx(1_000, abs=10)


@pytest.mark.parametrize("ctcss", [67.0, 88.5, 254.1])  # the lowest, a common one, the highest
def test_nfm_tone_comes_out_and_ctcss_is_removed(ctcss: float) -> None:
    x = (nfm(FS, 250e3, 1000, 2000, 0.05, ctcss=ctcss) + noise(FS)).astype(np.complex64)
    demod = NarrowbandDemodulator(FS, 250e3, "nfm")
    audio = demod(x)
    assert audio.dtype == np.float32 and abs(len(audio) - 16_000) < 20
    assert demod.open
    assert tone_share_db(audio, 1000) > 40
    seg = audio[8000:] * np.hanning(8000)
    spec = np.abs(np.fft.rfft(seg)) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / 16_000)
    # ±300 Hz of tone under ±2 kHz of audio: 40 dB down in what comes out
    assert spec[np.abs(f - ctcss) < 5].sum() < spec[np.abs(f - 1000) < 5].sum() / 10_000
    assert np.sqrt(2) * np.std(audio[8000:]) == pytest.approx(TONE_1K * 2 / 2.5, rel=0.03)


def test_pre_emphasised_audio_comes_out_flat() -> None:
    """A voice radio's modulator: every tone swings the carrier's phase alike (0.25 rad), so its
    deviation grows with its pitch, 6 dB per octave. The receiver must give them back equal."""
    tones = (300, 400, 700, 1000, 1600, 2200, 3000)
    t = np.arange(FS) / FS
    phase = 0.25 * sum(np.sin(2 * np.pi * f * t + k) for k, f in enumerate(tones))
    x = 0.05 * np.exp(1j * (2 * np.pi * 250e3 * t + phase)) + noise(FS)
    audio = NarrowbandDemodulator(FS, 250e3, "nfm")(x.astype(np.complex64))
    spec = np.abs(np.fft.rfft(audio[8000:] * np.hanning(8000))) ** 2
    f = np.fft.rfftfreq(8000, 1 / 16_000)
    level = {k: 10 * np.log10(spec[np.abs(f - k) <= 6].sum()) for k in tones}
    for k in tones:  # TIA-603 allows +1 to -3 dB against 1 kHz from 300 to 3000 Hz
        assert -2.5 < level[k] - level[1000] < 0.5
    assert max(level.values()) - min(level[k] for k in tones[1:]) < 1.5  # from 400 Hz up


def test_squelch_is_closed_on_noise_and_opens_on_a_carrier() -> None:
    demod = NarrowbandDemodulator(FS, -300e3, "nfm")
    quiet = demod(noise(FS, seed=1).astype(np.complex64))
    assert not demod.open and not np.any(quiet[4000:])
    talk = demod(
        (nfm(FS // 2, -300e3, 700, 2000, 0.05) + noise(FS // 2, seed=2)).astype(np.complex64)
    )
    assert demod.open and np.std(talk[4000:]) > 0.1


@pytest.mark.parametrize("squelch_db", [None, -40.0])
@pytest.mark.parametrize("late_ms", [0, 3, 5, 8])
def test_the_squelch_shuts_with_the_carrier(late_ms: int, squelch_db: float | None) -> None:
    """What follows a carrier is full-scale discriminator noise. The carrier ends anywhere
    within a 10 ms squelch block; none of the noise may come out."""
    n = FS // 2 + late_ms * FS // 1000
    end = n // 150  # the carrier's last sample, at 16 kHz

    x = noise(FS, seed=late_ms)
    x[:n] += nfm(n, 250e3, 1000, 2000, 0.05)
    demod = NarrowbandDemodulator(FS, 250e3, "nfm", squelch_db=squelch_db)
    audio = demod(x.astype(np.complex64))
    assert np.std(audio[4000 : end - 400]) == pytest.approx(
        TONE_1K * 2 / 2.5 / np.sqrt(2), rel=0.05
    )
    assert not demod.open
    assert not np.any(audio[end + 480 :])  # silent from 30 ms after the carrier

    # A carrier with nothing on it: whatever comes out after it is noise that got through.
    x = noise(FS, seed=late_ms)
    x[:n] += nfm(n, 250e3, 1000, 0, 0.05)
    audio = NarrowbandDemodulator(FS, 250e3, "nfm", squelch_db=squelch_db)(x.astype(np.complex64))
    assert np.abs(audio[end:]).max() < 0.02


def test_a_weak_signal_that_flutters_is_not_chopped() -> None:
    """Six fades a second between 10 and 2 dB of SNR in the channel: under the level that opens
    the squelch for much of the time, never gone."""
    n = 2 * FS
    snr = 6 + 4 * np.sin(2 * np.pi * 6 * np.arange(n) / FS)
    x = nfm(n, 250e3, 1000, 2000, 1.0) * carrier_amp(1, 12_500) * 10 ** ((snr - 1) / 20)
    audio = NarrowbandDemodulator(FS, 250e3, "nfm")((x + noise(n)).astype(np.complex64))[4000:]
    assert np.mean(audio != 0) > 0.95
    assert tone_share_db(audio, 1000) > 0


def opening_snr_db(width: float) -> int:
    """The weakest signal (a tone at the channel's full deviation, in 1 dB steps of SNR in the
    channel's width) that holds the squelch open for most of half a second."""
    n = FS // 2
    for snr in range(13):
        demod = NarrowbandDemodulator(FS, 250e3, "nfm", width)
        x = nfm(n, 250e3, 1000, 0.2 * width, carrier_amp(snr, width)) + noise(n, seed=snr)
        states = []
        for i in range(0, n, FS // 100):
            demod(x[i : i + FS // 100].astype(np.complex64))
            states.append(demod.open)
        if np.mean(states[10:]) >= 0.5:
            return snr
    return 99


def test_the_squelch_opens_at_the_same_snr_in_narrow_and_wide_channels() -> None:
    narrow, wide = opening_snr_db(12_500), opening_snr_db(25_000)
    assert 3 <= narrow <= 7 and 3 <= wide <= 7
    assert abs(narrow - wide) <= 2


def test_full_deviation_in_a_25_khz_channel_is_clean() -> None:
    # ±5 kHz (marine VHF, weather radio), 41 dB over the noise in the channel
    x = (nfm(FS, 250e3, 1000, 5000, 0.05) + noise(FS)).astype(np.complex64)
    audio = NarrowbandDemodulator(FS, 250e3, "nfm", 25_000)(x)
    assert tone_share_db(audio, 1000) > 50
    assert np.sqrt(2) * np.std(audio[8000:]) == pytest.approx(TONE_1K, rel=0.03)


@pytest.mark.parametrize(("mode", "width"), [("nfm", 6_250), ("nfm", 12_500), ("nfm", 25_000),
                                             ("am", 8_330), ("am", 25_000)])  # fmt: skip
def test_a_channel_off_the_centre_is_clear_of_the_dongles_spike(mode: str, width: int) -> None:
    """The spike at a dongle's centre frequency is a constant added to its samples: here a
    large one, 50 dB over the noise in the channel. On it a channel hears a carrier."""
    x = (noise(FS) + carrier_amp(50, width)).astype(np.complex64)
    on_it = NarrowbandDemodulator(FS, 0, mode, width)
    on_it(x)
    assert on_it.open and not off_centre(446e6, 446e6, mode, width)
    assert not off_centre(446e6 + 0.99 * width, 446e6, mode, width)

    assert off_centre(446e6 + width, 446e6, mode, width)  # the nearest that is let through
    clear = NarrowbandDemodulator(FS, width, mode, width)
    audio = clear(x)
    assert not clear.open and not np.any(audio[1600:])


def test_a_stronger_adjacent_channel_is_rejected() -> None:
    x = nfm(FS, 200e3, 1000, 2000, 0.02) + nfm(FS, 200e3 + 25e3, 2500, 2500, 0.2)
    audio = NarrowbandDemodulator(FS, 200e3, "nfm")((x + noise(FS)).astype(np.complex64))
    assert tone_share_db(audio, 1000) > 15


def test_am_tone_comes_out() -> None:
    x = (am(FS, -150e3, 800, 0.5, 0.05) + noise(FS)).astype(np.complex64)
    demod = NarrowbandDemodulator(FS, -150e3, "am")
    audio = demod(x)
    assert demod.open
    assert tone_share_db(audio, 800) > 20
    assert np.sqrt(2) * np.std(audio[8000:]) == pytest.approx(0.6 * 0.5, rel=0.2)


def test_output_does_not_depend_on_chunking() -> None:
    n = FS // 2
    x = noise(n)
    x[n // 8 : n * 5 // 8] += nfm(n // 2, 100e3, 900, 2000, 0.05)  # the squelch opens and shuts
    x = x.astype(np.complex64)
    whole = NarrowbandDemodulator(FS, 100e3, "nfm")(x)
    assert np.any(whole[2000:4000]) and not np.any(whole[:500]) and not np.any(whole[6000:])
    demod = NarrowbandDemodulator(FS, 100e3, "nfm")
    parts = [demod(x[i : i + 77_777]) for i in range(0, n, 77_777)]
    joined = np.concatenate(parts)
    m = min(len(whole), len(joined))
    assert m > 7_000
    np.testing.assert_allclose(joined[:m], whole[:m], atol=1e-4)


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        NarrowbandDemodulator(FS, 0, "usb")
