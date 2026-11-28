# SPDX-License-Identifier: Apache-2.0
"""The recognition ladder's rungs (radio/ladder.py), each on what it is allowed to see: the band
plan, the spectrum of the made-up sky (tests/fake_world.py), what the audio gave, a name."""

from __future__ import annotations

import numpy as np
import pytest

from tests.fake_world import World
from wavescribe.dsp.rds import RdsInfo
from wavescribe.dsp.wfm import iq_from_u8
from wavescribe.radio import ladder
from wavescribe.radio.ladder import Place, Sound, may_probe, name, place, power_frames, shape

RATE = 2_400_000
CENTRE = 446_000_000
FFT = 4096
BIN_HZ = RATE / FFT
FRAME_S = 29 * FFT / RATE


@pytest.mark.parametrize(
    ("freq", "expected"),
    [
        (446_031_250, Place("pmr446", "PMR446 walkie-talkies, channel 3", "private")),
        (92_400_000, Place("fm", "FM broadcast", "broadcast")),
        (120_500_000, Place("airband", "Airband (civil aviation voice)", "utility")),
        (25_000_000, Place(None, None, "private")),  # no plan lists it: kept as private
    ],
)
def test_rung_one_names_the_place_not_the_signal(freq: int, expected: Place) -> None:
    assert place(freq, "cept") == expected


def frames(transmitters: list[dict], seconds: float) -> np.ndarray:
    tuned = World({"transmitters": transmitters}).tune(CENTRE, RATE)
    iq = np.concatenate([iq_from_u8(tuned.u8()) for _ in range(round(seconds / 0.256))])
    return power_frames(iq, FFT)


FLOOR = np.median(frames([], 1.0), axis=0)


def looks(transmitters: list[dict], offset_hz: float, half_hz: float, seconds: float = 3.0):
    """Rung 2 over the slice ``offset_hz ± half_hz`` of the window."""
    lo = round(FFT / 2 + (offset_hz - half_hz) / BIN_HZ)
    hi = round(FFT / 2 + (offset_hz + half_hz) / BIN_HZ)
    f = frames(transmitters, seconds)
    return shape(f[:, lo:hi], FLOOR[lo:hi], BIN_HZ, FRAME_S)


def tx(kind: str, offset_hz: float, **kw: object) -> dict:
    return {"kind": kind, "mhz": (CENTRE + offset_hz) / 1e6, **kw}


def test_rung_two_broadcast_fm_is_wide_and_steady() -> None:
    s = looks([tx("wfm", 400_000, tone_hz=1000)], 400_000, 150_000)
    assert (s.kind, s.steady, s.bursts) == ("wide_fm", True, 1)
    assert 80_000 < s.width_hz < 200_000 and s.flat_db is not None and s.flat_db > ladder.FLAT_DB


def test_rung_two_a_digital_block_is_flat_topped() -> None:
    s = looks([tx("block", 0, width_khz=1536, level=0.05)], 0, 1_000_000)
    assert s.kind == "flat_block" and s.steady
    assert s.width_hz == pytest.approx(1_536_000, rel=0.05)


@pytest.mark.parametrize(
    ("transmitter", "kind"),
    [
        (tx("nfm", 300_000, tone_hz=1000, level=0.05), "narrow_fm"),
        (tx("am", 300_000, tone_hz=2500, level=0.05), "am"),
        (tx("line", 300_000, level=0.02), "carrier"),
    ],
)
def test_rung_two_narrow_signals(transmitter: dict, kind: str) -> None:
    s = looks([transmitter], 300_000, 15_000)
    assert s.kind == kind and s.steady
    assert s.width_hz < ladder.WIDE_HZ


def test_rung_two_short_bursts_again_and_again_look_like_data() -> None:
    burst = tx("nfm", 300_000, period_s=1.0, on_s=0.2, level=0.05)
    s = looks([burst], 300_000, 15_000, seconds=6.0)
    assert s.kind == "data_bursts" and not s.steady and s.bursts >= 5


def test_rung_two_a_walkie_talkie_now_and_then_is_narrow_fm_and_not_steady() -> None:
    talk = tx("nfm", 300_000, period_s=4.0, on_s=2.0, level=0.05)
    s = looks([talk], 300_000, 15_000, seconds=8.0)
    assert s.kind == "narrow_fm" and not s.steady and s.bursts == 2
    assert s.on_share == pytest.approx(0.5, abs=0.1)


def test_rung_two_with_nothing_there() -> None:
    assert looks([], 300_000, 15_000).kind == "nothing"


@pytest.mark.parametrize(
    ("privacy", "confirmed", "allowed"),
    [
        ("broadcast", False, "words"),
        ("amateur", False, "words"),
        ("utility", False, None),
        ("utility", True, "words"),
        ("private", False, None),
        ("private", True, "voice"),  # someone speaks: never what they say
        ("digital", True, None),  # an analogue demodulator gives it nothing but noise
    ],
)
def test_what_a_probe_may_take_by_class(privacy: str, confirmed: bool, allowed: str | None) -> None:
    assert may_probe(privacy, confirmed) == allowed


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((2.0, 0.0, 0, 0, None), Sound("listening")),
        ((12.0, 0.1, 0, 10, None), Sound("nothing")),
        ((6.0, 0.2, 5, 6, None), Sound("music")),
        ((6.0, 3.0, 5, 6, None), Sound("speech")),  # a voice over music is speech
        ((6.0, 3.0, 0, 6, {"pt": 0.9, "es": 0.08}), Sound("speech", "pt")),
        ((6.0, 3.0, 0, 6, {"pt": 0.45, "es": 0.4}), Sound("speech")),  # not sure enough
    ],
)
def test_rung_three_from_what_the_audio_gave(args: tuple, expected: Sound) -> None:
    assert ladder.sound(*args) == expected


def test_rung_four_a_name_read_twice() -> None:
    info = RdsInfo(names={"VALLEY": 1})
    assert name(info) is None and name(None) is None
    info.names["VALLEY"] = 2
    assert name(info) == "VALLEY"
