# SPDX-License-Identifier: Apache-2.0
"""Signal processing for SDR sources (numpy/scipy only; no GPL code in-process).

The constants live here, free of scipy, so config validation can use them cheaply.
"""

import math

from wavescribe.radio.bandplan import TUNER_RANGE_HZ

IF_RATE = 240_000  # Hz: the WFM demodulator's working rate; device rates are multiples of it
NARROW_IF_RATE = 48_000  # Hz: where NFM and AM channels are filtered (IF_RATE is a multiple)
# Channel widths the 48 kHz stage can cut: 6.25 kHz NFM to 25 kHz wide NFM, with room.
MIN_CHANNEL_HZ, MAX_CHANNEL_HZ = 2_500, 30_000
CHANNEL_STOP_HZ = IF_RATE // 2  # a station must sit this far inside the device's band edge
NARROW_MARGIN_HZ = 60_000  # a narrowband channel, clear of the dongle's own filter roll-off
MODES = ("wfm", "nfm", "am")  # broadcast FM, narrowband FM, AM
WFM_CHANNEL_HZ = 200_000  # a broadcast FM channel, for its edges (bandplan.lookup)
# Channel spacing by mode when neither the station nor the band plan gives one.
# NFM, 12.5 kHz (PMR446, European repeaters and business radio): the filter is flat to ±5.6 kHz,
# which holds ±2.5 kHz deviation and 3 kHz of audio. A ±5 kHz signal (marine VHF, weather radio,
# amateur FM in the Americas, GMRS) does not fit: it comes out twice as loud, so its low notes
# clip, and distorted where its sidebands leave the filter. A tone at ±5 kHz comes out at 29 to
# 41 dB SINAD depending on its pitch (500 Hz to 3 kHz), against 48 to 63 dB through a 25 kHz
# channel. Those need channel_khz = 25.
# AM, 8.33 kHz (the airband's raster in Europe): flat to ±3.7 kHz, all a voice needs, so stations
# on the 25 kHz raster are heard through it as well, and with less noise than a 25 kHz filter
# would let in. Only offset-carrier ground stations (several transmitters up to ±7.5 kHz off the
# channel) need channel_khz = 25.
DEFAULT_CHANNEL_HZ = {"nfm": 12_500, "am": 8_330}
# A dongle has a spike at its centre frequency (the DC offset of its converter). A device tuned
# for a station is tuned this far below it.
OFF_CENTRE_HZ = {"wfm": 300_000, "nfm": 250_000, "am": 250_000}


def frequency_hz(mhz: object) -> int:
    """A frequency someone gave in MHz, as whole Hz. ValueError, saying what to give instead,
    for what can't be one (not a number, not finite, not above zero) and for one plainly given
    in another unit: 446006250 for 446.00625 is a number like any other to everything after
    this. Whether the tuner reaches it is ``check_tunable``'s question."""
    try:
        value = float(mhz) if isinstance(mhz, int | float | str) else math.nan
    except ValueError:
        value = math.nan
    if isinstance(mhz, bool) or not math.isfinite(value) or value <= 0:
        given = "" if mhz in (None, "") else f"{mhz!r} isn't one: "
        raise ValueError(f"{given}give a frequency in MHz, e.g. 99.5 or 446.00625")
    lo, hi = TUNER_RANGE_HZ
    for unit, per_mhz in (("Hz", 1e6), ("kHz", 1e3)):
        if value * 1e6 > hi and lo <= value / per_mhz * 1e6 <= hi:
            meant = f"{value / per_mhz:.6f}".rstrip("0").rstrip(".")
            raise ValueError(f"{mhz} looks like {unit}; frequencies are in MHz: {meant}")
    if not math.isfinite(value * 1e6):  # 1e303 MHz is a number, but no whole number of Hz
        check_tunable(math.inf)
    return round(value * 1e6)


def check_tunable(hz: float) -> None:
    """ValueError unless an RTL-SDR's tuner reaches ``hz``."""
    lo, hi = TUNER_RANGE_HZ
    if not lo <= hz <= hi:
        below = "; medium wave and shortwave aren't received" if hz < lo else ", no higher"
        raise ValueError(f"the receiver tunes {lo / 1e6:g} to {hi / 1e6:g} MHz{below}")


def tunable_hz(mhz: object) -> int:
    """A frequency someone gave in MHz that the receiver tunes, as whole Hz (``frequency_hz``
    and ``check_tunable``): the same words wherever one is given."""
    hz = frequency_hz(mhz)
    check_tunable(hz)
    return hz


def supported_rate(rate: int) -> bool:
    """Device sample rates the WFM demodulator handles: whole multiples of 240 kHz."""
    return rate >= IF_RATE and rate % IF_RATE == 0


def margin_hz(mode: str = "wfm") -> int:
    """How far inside the device's band edge a channel of this mode must sit."""
    return CHANNEL_STOP_HZ if mode == "wfm" else NARROW_MARGIN_HZ


def in_band(freq_hz: float, center_hz: float, rate: int, mode: str = "wfm") -> bool:
    """Whether a station at ``freq_hz`` fits in a device tuned to ``center_hz`` at ``rate``."""
    return abs(freq_hz - center_hz) <= rate / 2 - margin_hz(mode)


def off_centre(
    freq_hz: float, center_hz: float, mode: str, channel_hz: float | None = None
) -> bool:
    """Whether a channel keeps clear of the spike at the device's centre frequency. To a
    narrowband demodulator the spike is a carrier with nothing on it: inside the channel filter
    it holds the squelch open on noise for as long as the device stays there. That happens up
    to 0.55 of the channel's width from the centre and never from 0.6, however strong the
    spike, so a whole width is clear of it. Broadcast FM, 200 kHz wide and far above the
    spike, isn't troubled by it."""
    return mode == "wfm" or abs(freq_hz - center_hz) >= (channel_hz or DEFAULT_CHANNEL_HZ[mode])
