# SPDX-License-Identifier: Apache-2.0
"""The WFM demodulator on synthetic FM signals (no hardware, no recorded audio)."""

import numpy as np
import pytest

from wavescribe.dsp.wfm import WfmDemodulator, iq_from_u8

FS = 2_400_000


def fm(n: int, offset: float, tone: float, deviation: float, amp: float) -> np.ndarray:
    t = np.arange(n) / FS
    return amp * np.exp(
        1j * (2 * np.pi * offset * t + deviation / tone * np.sin(2 * np.pi * tone * t))
    )


def noise(n: int, sigma: float = 0.01) -> np.ndarray:
    rng = np.random.default_rng(0)
    return sigma * (rng.normal(size=n) + 1j * rng.normal(size=n))


def sinad_db(audio: np.ndarray, tone: float) -> float:
    """Tone power over everything else, after the first 0.25 s (filters settling)."""
    seg = audio[4000:]
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    f = np.fft.rfftfreq(len(seg), 1 / 16_000)
    near = np.abs(f - tone) <= 30
    return float(10 * np.log10(spec[near].sum() / spec[~near].sum()))


def test_tone_comes_out_at_16k_with_the_right_level() -> None:
    x = (fm(FS, 300e3, 1000, 50e3, 0.05) + noise(FS)).astype(np.complex64)
    demod = WfmDemodulator(FS, 300e3)
    audio = demod(x)
    assert audio.dtype == np.float32 and len(audio) == 16_000
    assert sinad_db(audio, 1000) > 40
    # 50 of 75 kHz deviation, minus the 50 µs de-emphasis at 1 kHz.
    expected = 50 / 75 / np.sqrt(1 + (2 * np.pi * 1000 * 50e-6) ** 2)
    assert np.sqrt(2) * np.std(audio[4000:]) == pytest.approx(expected, rel=0.05)
    assert demod.power_db == pytest.approx(10 * np.log10(0.05**2), abs=1.0)


@pytest.mark.parametrize("away", [200e3, -200e3, 400e3])
def test_a_stronger_neighbour_station_is_rejected(away: float) -> None:
    x = fm(FS, 300e3, 1000, 50e3, 0.05) + fm(FS, 300e3 + away, 3000, 75e3, 0.5)
    audio = WfmDemodulator(FS, 300e3)((x + noise(FS)).astype(np.complex64))
    assert sinad_db(audio, 1000) > 40  # the 3 kHz tone of a +20 dB neighbour doesn't leak in


def test_output_does_not_depend_on_chunking() -> None:
    n = FS // 2
    x = fm(n, -250e3, 700, 60e3, 0.1) + noise(n)
    iq = np.empty(2 * n, np.float32)
    iq[0::2], iq[1::2] = x.real, x.imag
    raw = np.clip(np.round(iq * 128 + 127.4), 0, 255).astype(np.uint8).tobytes()
    whole = WfmDemodulator(FS, -250e3)(iq_from_u8(raw))
    demod = WfmDemodulator(FS, -250e3)
    parts, i = [], 0
    for size in [1000, 77_777, 2, 123_456, 1, 3] * 10:  # in IQ samples, some tiny
        parts.append(demod(iq_from_u8(raw[i : i + 2 * size])))
        i += 2 * size
    parts.append(demod(iq_from_u8(raw[i:])))
    chunked = np.concatenate(parts)
    assert len(chunked) == len(whole) == 8000
    assert np.max(np.abs(chunked - whole)) < 1e-5


def test_iq_from_u8_centres_and_pairs_samples() -> None:
    iq = iq_from_u8(bytes([127, 128, 255, 0, 7]))  # the odd trailing byte is ignored
    assert iq.dtype == np.complex64 and len(iq) == 2
    assert abs(iq[0]) < 0.01
    assert iq[1].real == pytest.approx(1.0, abs=0.01) and iq[1].imag == pytest.approx(
        -1.0, abs=0.01
    )


def test_rejects_unusable_rates_and_offsets() -> None:
    with pytest.raises(ValueError, match="multiple"):
        WfmDemodulator(2_048_000, 0)
    with pytest.raises(ValueError, match="outside"):
        WfmDemodulator(FS, 1.1e6)
