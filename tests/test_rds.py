# SPDX-License-Identifier: Apache-2.0
"""RDS decoding on a synthetic multiplex: groups built from scratch, sent as biphase BPSK on a
57 kHz subcarrier next to a pilot and audio, then FM-modulated and demodulated again."""

import copy
from collections.abc import Callable

import numpy as np
import pytest
from scipy.signal import firwin, lfilter

from tests.rds_groups import Group, bits_of, name_groups, text_groups
from wavescribe.dsp.rds import RdsDecoder, char, correct, encode_block, pty_name, syndrome
from wavescribe.dsp.wfm import WfmDemodulator

MPX_RATE = 240_000


def groups(pi: int, ps: str, rt: str = "", pty: int = 1) -> list[int]:
    """Bits of PS groups (0A) and radiotext groups (2A), repeated."""
    return bits_of((name_groups(pi, ps, pty) + text_groups(pi, rt + "\r", pty)) * 3)


def history(
    sent: list[Group], what: str, blocks: Callable[[int, str], int] = encode_block
) -> tuple[RdsDecoder, list[object]]:
    """The decoder after ``sent``, and each value its ``info.<what>`` took on the way, looked at
    about once a group. The decoder needs the first two groups to find the blocks, and the
    signal's last one is still in its filters."""
    bits = bits_of(sent, blocks)
    signal = mpx(bits, len(bits) / 1187.5)
    dec = RdsDecoder()
    seen: list[object] = []
    for i in range(0, len(signal), 20_000):
        dec(signal[i : i + 20_000])
        now = copy.copy(getattr(dec.info, what))
        if now and now != (seen[-1] if seen else None):
            seen.append(now)
    return dec, seen


def mpx(bits: list[int], seconds: float, phase: float = 1.1, freq_error_hz: float = 0.8,
        rds_level: float = 0.04, seed: int = 0) -> np.ndarray:  # fmt: skip
    """A station's multiplex at 240 kHz: 1 kHz audio, a pilot and the RDS bits."""
    n = int(seconds * MPX_RATE)
    t = np.arange(n) / MPX_RATE
    # Differential coding, then one biphase symbol per bit (+ then - for a 1, - then + for a 0).
    sent, prev = [], 0
    for b in bits:
        prev ^= b
        sent.append(prev)
    pos = t * 1187.5
    k = np.minimum(pos.astype(int), len(sent) - 1)
    first_half = (pos - np.floor(pos)) < 0.5
    sym = np.where(np.array(sent)[k] == 1, 1.0, -1.0) * np.where(first_half, 1.0, -1.0)
    shaped = lfilter(firwin(301, 2400, fs=MPX_RATE), 1.0, sym)
    sub = np.cos(2 * np.pi * (57_000 + freq_error_hz) * t + phase)
    rng = np.random.default_rng(seed)
    return (0.4 * np.sin(2 * np.pi * 1000 * t) + 0.09 * np.cos(2 * np.pi * 19_000 * t)
            + rds_level * shaped * sub + 0.002 * rng.normal(size=n)).astype(np.float32)  # fmt: skip


def test_checkwords_round_trip() -> None:
    for offset, word in (("A", 0x0FC), ("B", 0x198), ("C", 0x168), ("D", 0x1B4)):
        assert syndrome(encode_block(0x1234, offset)) == word
    assert syndrome(encode_block(0x1234, "A") ^ (1 << 7)) != 0x0FC  # one flipped bit is caught


def test_name_pi_type_text_and_alternative_frequencies() -> None:
    bits = groups(0x2F15, "TEST FM", "Hello from the test")
    dec = RdsDecoder()
    signal = mpx(bits, len(bits) / 1187.5)
    for i in range(0, len(signal), 6000):  # chunked like a live station
        dec(signal[i : i + 6000])
    info = dec.info
    assert info.pi == 0x2F15
    assert info.name == "TEST FM"
    assert info.pty == 1 and pty_name(info.pty) == "News"
    assert info.tp and info.ta
    assert info.rt == "Hello from the test"
    assert info.af == {92.7}
    assert info.to_dict()["pi"] == "2F15"


@pytest.mark.parametrize("phase", [0.0, 2.0, -2.8])
def test_any_carrier_phase_decodes(phase: float) -> None:
    bits = groups(0x1ABC, "PHASE")
    dec = RdsDecoder()
    dec(mpx(bits, len(bits) / 1187.5, phase=phase, freq_error_hz=-2.0, seed=1))
    assert dec.info.pi == 0x1ABC and dec.info.name == "PHASE"


def test_through_fm_modulation_and_the_wfm_demodulator() -> None:
    bits = groups(0x5A5A, "ON AIR", "")[: 26 * 4 * 12]
    m = mpx(bits, len(bits) / 1187.5)
    fs, up = 2_400_000, 10
    m_up = np.repeat(m, up)  # the demodulator's channel filter smooths the steps
    phase = 2 * np.pi * (300e3 * np.arange(len(m_up)) / fs + 75e3 * np.cumsum(m_up) / fs)
    rng = np.random.default_rng(2)
    iq = (0.1 * np.exp(1j * phase) + 0.005 * (rng.normal(size=len(m_up))
          + 1j * rng.normal(size=len(m_up)))).astype(np.complex64)  # fmt: skip
    demod, dec = WfmDemodulator(fs, 300e3), RdsDecoder()
    demod.on_mpx = dec
    for i in range(0, len(iq), 614_400):
        demod(iq[i : i + 614_400])
    assert dec.info.pi == 0x5A5A and dec.info.name == "ON AIR"


def test_a_signal_as_weak_as_the_noise_in_its_band_decodes() -> None:
    """0 dB: the RDS signal against the noise in the 4.8 kHz around 57 kHz that the decoder's
    filter lets through. About where a station stops being worth listening to."""
    sent = name_groups(0x2F15, "WEAK FM") * 30
    bits = bits_of(sent)
    seconds = len(bits) / 1187.5
    signal = mpx(bits, seconds)
    rds_power = np.mean((signal - mpx(bits, seconds, rds_level=0.0)) ** 2)
    sigma = np.sqrt(rds_power / (4_800 / (MPX_RATE / 2)))  # white noise: that much in 4.8 kHz
    noisy = signal + sigma * np.random.default_rng(4).normal(size=len(signal))
    dec = RdsDecoder()
    dec(noisy.astype(np.float32))
    assert dec.info.pi == 0x2F15 and dec.info.name == "WEAK FM"
    assert dec.info.groups >= 0.9 * len(sent) and dec.info.corrected > 0


def test_noise_alone_names_nothing() -> None:
    rng = np.random.default_rng(3)
    dec = RdsDecoder()
    dec((0.3 * rng.normal(size=MPX_RATE * 3)).astype(np.float32))
    assert dec.info.pi is None and dec.info.name is None


# The code as the standard gives it, with nothing taken from wavescribe.dsp.rds: the powers of x
# in the generator polynomial, and the offset words c9 to c0.
GENERATOR = (10, 8, 7, 5, 4, 3, 0)
OFFSET_WORDS = {"A": "0011111100", "B": "0110011000", "C": "0101101000", "D": "0110110100"}


def block_by_long_division(data: int, offset: str) -> int:
    """A block worked out as on paper: the 16 data bits with ten zeros after them, divided by
    the generator one step per bit; the remainder plus the offset word is the checkword."""
    divisor = [int(10 - i in GENERATOR) for i in range(11)]
    work = [data >> (15 - i) & 1 for i in range(16)] + [0] * 10
    for i in range(16):
        if work[i]:
            for j, g in enumerate(divisor):
                work[i + j] ^= g
    check = [r ^ int(o) for r, o in zip(work[16:], OFFSET_WORDS[offset], strict=True)]
    return (data << 10) | int("".join(map(str, check)), 2)


def test_blocks_built_by_long_division_decode() -> None:
    sent = (name_groups(0xD314, "LONG DIV") + text_groups(0xD314, "Checked by hand\r")) * 3
    dec, _ = history(sent, "pi", blocks=block_by_long_division)
    info = dec.info
    assert (info.pi, info.name, info.rt) == (0xD314, "LONG DIV", "Checked by hand")
    assert info.groups >= len(sent) - 3 and info.bad_blocks == 0 and info.corrected == 0


@pytest.mark.parametrize("offset", ["A", "B", "C", "C'", "D"])
def test_one_wrong_bit_or_two_adjacent_anywhere_in_a_block_are_corrected(offset: str) -> None:
    block = encode_block(0xA5C3, offset)
    for pos in range(26):
        assert correct(block ^ (1 << pos), offset) == block
    for pos in range(25):
        assert correct(block ^ (0b11 << pos), offset) == block
    assert correct(block ^ 0b101, offset) != block  # two bits apart aren't a burst


def test_the_first_bit_of_a_block_is_corrected_like_the_rest() -> None:
    def first_bit_of_d_wrong(data: int, offset: str) -> int:
        return encode_block(data, offset) ^ ((offset == "D") << 25)

    dec, _ = history(name_groups(0x2F15, "FIRSTBIT") * 4, "pi", blocks=first_bit_of_d_wrong)
    assert dec.info.name == "FIRSTBIT" and dec.info.corrected >= 12


def test_a_short_text_with_no_end_mark_appears() -> None:
    """Not every station ends a text shorter than 64 characters with a carriage return: some
    send its few segments round and round."""
    sent = (name_groups(0x2F15, "SHORT") + text_groups(0x2F15, "Now: Song A")) * 4
    _, seen = history(sent, "rt")
    assert seen == ["Now: Song A"]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("The first text, a long one\r", "Second\r"),
        ("The first text, a long one, to its end", "Second"),
        ("A text as long as a text can be, all 64 characters of it. The end", "Another\r"),
    ],
)
def test_a_text_changed_without_the_flag_replaces_the_one_before(first: str, second: str) -> None:
    pi = 0x2F15
    sent = (name_groups(pi, "CHANGES") + text_groups(pi, first[:64])) * 4
    sent += (text_groups(pi, second) + name_groups(pi, "CHANGES")) * 3
    _, seen = history(sent, "rt")
    assert seen == [first[:64].strip(), second.strip()]  # and never a mix of the two


def test_one_wrong_segment_is_not_taken_for_text() -> None:
    pi = 0x2F15
    right = text_groups(pi, "Hello from the test\r")
    wrong = [*right[:1], (pi, right[1][1], 0x5858, 0x5858), *right[2:]]  # "XXXX", checkword intact
    _, seen = history(name_groups(pi, "ONE BAD") + wrong + right * 3 + wrong + right, "rt")
    assert seen == ["Hello from the test"]


def test_traffic_flags_and_frequencies_must_repeat() -> None:
    """One group that error correction got wrong says anything: an announcement that isn't on,
    frequencies the station doesn't have."""
    pi = 0x2F15
    steady = name_groups(pi, "TRAFFIC", ta=0)
    stray = name_groups(pi, "TRAFFIC", af=(117, 13), tp=0, ta=1)[:1]
    sent = steady * 2 + stray + steady[1:] + steady
    dec, announcements = history(sent, "ta")
    assert announcements == [] and dec.info.tp
    assert history(sent, "af")[1] == [{92.7}]
    assert history(name_groups(pi, "TRAFFIC") * 3, "ta")[1] == [True]


def test_a_long_or_medium_wave_alternative_is_not_an_fm_frequency() -> None:
    pi = 0x2F15
    fm = name_groups(pi, "LW ALSO", af=(0xE3, 52))  # three frequencies follow; 92.7 MHz
    lf_mf = name_groups(pi, "LW ALSO", af=(250, 30))  # an LF/MF channel follows; its number
    dec, _ = history([g for pair in zip(fm, lf_mf, strict=True) for g in pair] * 3, "af")
    assert dec.info.af == {92.7}  # code 30 as an FM frequency would be 90.5


def test_a_control_code_in_a_name_is_a_space() -> None:
    dec, _ = history(name_groups(0x2F15, "AB\rCD") * 3, "ps")
    assert dec.info.name == "AB CD"


def test_sharp_s() -> None:
    assert char(0x8D) == "ß" and char(0x91) == "ä"
