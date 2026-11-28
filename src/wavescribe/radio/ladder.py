# SPDX-License-Identifier: Apache-2.0
"""What a signal is, rung by rung, each saying only what its evidence allows.

1. **The place** (at once): what the band plan lists there. It describes the place, never the
   signal: "walkie-talkies, channel 3" is a channel, not a walkie-talkie heard.
2. **The shape** (after a second or two of the spectrum): how wide, steady or now and then, and
   what it looks like: wide FM, narrow FM, AM, a flat block, data bursts, a bare carrier. Never
   what it is: a narrow FM signal may be a walkie-talkie or a radio microphone.
3. **The sound** (after seconds of audio, only where it may be demodulated): speech, music or
   nothing; a language only as "sounds like", and only from a model that tells languages
   apart with some confidence.
4. **A name** (where the signal carries one): an RDS name read twice. Until then everything is
   "looks like".

Nothing here demodulates or opens anything: each rung is a function over what the detector,
the demodulators and the models already measured.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from wavescribe.radio import bandplan

# Rung 2. Widths are measured 10 dB under the signal's peak.
WIDE_HZ = 40_000  # wider than this is a wide signal (as the open-range detector notes them)
BLOCK_MIN_HZ = 100_000  # a flat block is at least this wide
FLAT_DB = 1.5  # the top of a flat block varies this little (spread of its middle, in dB)
SIDEBANDS_HZ = 4_000  # AM: the carrier's sidebands reach this wide 30 dB under the peak
ON_DB = 10.0  # a frame in which the signal stands this far over the floor has it on
STEADY = 0.8  # on in this share of the frames: steady
BURST_S = 0.5  # data comes in bursts shorter than this
BURSTS = 3  # and at least this many of them

# Rung 3.
SOUNDS_P = 0.5  # a language-telling model's likeliest language at least this likely: "sounds"
SPEECH_S = 1.0  # this much speech found by the voice detector: speech
NOTHING_S = 10.0  # heard this long with no speech and no music: nothing heard
MUSIC_SHARE = 0.6  # this share of the music detector's windows calling it music: music


# Rung 1.


@dataclass(frozen=True)
class Place:
    band: str | None  # the band's id, None where no plan of the region lists one
    name: str | None  # its name, with the channel where it is on one ("…, channel 3")
    privacy: str  # the class the band plan gives (private where no plan lists it)


def place(freq_hz: float, region: str, width_hz: float | None = None) -> Place:
    """What the band plan lists at ``freq_hz``: rung 1. Cannot say what is transmitting."""
    band = bandplan.lookup(freq_hz, region, width_hz)
    privacy = bandplan.privacy_of(freq_hz, region, width_hz)
    if band is None:
        return Place(None, None, privacy)
    return Place(band.id, band.label(freq_hz), privacy)


# Rung 2.


@dataclass(frozen=True)
class Shape:
    """What the spectrum shows of one signal. ``kind`` is how it looks: "wide_fm",
    "narrow_fm", "am", "flat_block", "data_bursts", "carrier", or "nothing" (never on). It
    cannot say what the signal is or who sends it: a flat block is digital, and nothing in its
    shape says more."""

    kind: str
    width_hz: float
    steady: bool  # on in ``STEADY`` of the frames or more
    on_share: float
    bursts: int  # times it came on
    flat_db: float | None  # spread of a wide signal's top, dB (None when too narrow to say)


def power_frames(iq: np.ndarray, fft: int = 4096, per_frame: int = 29) -> np.ndarray:
    """IQ as frames of power (dB, linear average of ``per_frame`` Hann-windowed FFTs, centre
    in the middle): what the detector looks at, about 50 ms a frame at 2.4 MS/s."""
    n = len(iq) // (fft * per_frame) * fft * per_frame
    blocks = iq[:n].reshape(-1, per_frame, fft) * np.hanning(fft)
    power = (np.abs(np.fft.fft(blocks, axis=2)) ** 2).mean(axis=1)
    return 10 * np.log10(np.fft.fftshift(power, axes=1) + 1e-20)


def shape(frames_db: np.ndarray, floor_db: np.ndarray, bin_hz: float,
          frame_s: float) -> Shape:  # fmt: skip
    """Rung 2 from frames of power (a row per frame, a column per bin, dB) over a slice of
    spectrum holding one signal, and the noise floor of each bin."""
    over = frames_db - floor_db
    on = over.max(axis=1) >= ON_DB
    share = float(on.mean()) if len(on) else 0.0
    starts = int(np.count_nonzero(np.diff(np.concatenate([[0], on.astype(int)])) == 1))
    if not on.any():
        return Shape("nothing", 0.0, False, 0.0, 0, None)
    avg = 10 * np.log10(np.mean(10 ** (frames_db[on] / 10), axis=0))
    top = float(avg.max())
    above_floor = avg - floor_db
    # From the lowest bin to the highest within 10 dB (30 dB) of the peak: a spectrum of lines
    # (a tone on FM, the carrier and sidebands of AM) has gaps between them.
    peak = int(np.argmax(avg))
    lo, hi = _extent((avg >= top - 10) & (above_floor >= 6), peak)
    width10 = (hi - lo + 1) * bin_hz
    lo30, hi30 = _extent((avg >= top - 30) & (above_floor >= 3), peak)
    width30 = (hi30 - lo30 + 1) * bin_hz
    steady = share >= STEADY
    lengths = _runs(on) * frame_s
    flat = None
    kind: str
    if width10 >= WIDE_HZ:
        middle = avg[lo + (hi - lo) // 10 : hi - (hi - lo) // 10 + 1]
        flat = float(np.percentile(middle, 90) - np.percentile(middle, 10))
        kind = "flat_block" if width10 >= BLOCK_MIN_HZ and flat <= FLAT_DB else "wide_fm"
    elif width10 <= 3 * bin_hz:
        kind = "am" if width30 >= SIDEBANDS_HZ else "carrier"
    else:
        kind = "narrow_fm"
    if kind in ("narrow_fm", "am", "carrier") and starts >= BURSTS and (
            np.median(lengths) < BURST_S):  # fmt: skip
        kind = "data_bursts"
    return Shape(kind, float(width10), steady, share, starts, flat)


def _extent(mask: np.ndarray, peak: int) -> tuple[int, int]:
    """The first and last bin of ``mask``, or the peak's alone if it has none."""
    where = np.flatnonzero(mask)
    return (int(where[0]), int(where[-1])) if len(where) else (peak, peak)


def _runs(on: np.ndarray) -> np.ndarray:
    """Lengths, in frames, of each stretch it was on."""
    edges = np.flatnonzero(np.diff(np.concatenate([[0], on.astype(int), [0]])))
    return edges[1::2] - edges[0::2]


# Rung 3.


def may_probe(privacy: str, confirmed: bool) -> str | None:
    """What a probe may take from a signal of class ``privacy`` that nobody kept: "words"
    (speech recognised, shown, never stored), "voice" (only whether someone speaks: the voice
    detector, no recognition), or None (not demodulated at all). Broadcast and amateur radio
    give words; utility radio words once the user has confirmed it; private radio only voice,
    once confirmed; digital radio nothing, since an analogue demodulator gives it nothing but
    noise."""
    if privacy == "digital":
        return None
    if bandplan.POLICIES[privacy].needs_ack and not confirmed:
        return None
    return "voice" if privacy == "private" else "words"


@dataclass(frozen=True)
class Sound:
    """What was heard: "speech", "music", "nothing" (long enough, and neither) or "listening"
    (not long enough to say). ``language``, with speech, is a language only a model that tells
    languages apart gave as likely; the words for it are "sounds like", never "is"."""

    kind: str
    language: str | None = None


def sound(heard_s: float, speech_s: float, music_windows: int, windows: int,
          languages: dict[str, float] | None = None) -> Sound:  # fmt: skip
    """Rung 3 from what the demodulated audio gave: seconds heard, seconds the voice detector
    called speech, how many of the music detector's windows called it music, and a
    language-telling model's probabilities (None where none ran; a speech recogniser's own
    guess is no such model and must not be passed)."""
    if windows and music_windows / windows >= MUSIC_SHARE and speech_s < SPEECH_S:
        return Sound("music")
    if speech_s >= SPEECH_S:
        language = None
        if languages:
            best = max(languages, key=languages.__getitem__)
            language = best if languages[best] >= SOUNDS_P else None
        return Sound("speech", language)
    if heard_s >= NOTHING_S:
        return Sound("nothing")
    return Sound("listening")


# Rung 4.


def name(rds: object | None) -> str | None:
    """The name a station sends (``dsp.rds.RdsInfo.name``: read at least twice, or voted
    segment by segment), or None. Only this rung ends "looks like"."""
    return getattr(rds, "name", None) if rds is not None else None
