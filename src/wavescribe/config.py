# SPDX-License-Identifier: Apache-2.0
"""Typed station configuration, loaded from TOML (see config/stations.example.toml)."""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from wavescribe.dsp import (
    MAX_CHANNEL_HZ,
    MIN_CHANNEL_HZ,
    MODES,
    OFF_CENTRE_HZ,
    WFM_CHANNEL_HZ,
    check_tunable,
    frequency_hz,
    in_band,
    margin_hz,
    off_centre,
    supported_rate,
)
from wavescribe.languages import translation_targets

log = logging.getLogger(__name__)

# Loose BCP-47 shape check (language, then optional script/region/variant subtags).
_BCP47 = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_STATION_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

_DEFAULT_KEYS = {"language", "translate_to", "alert_languages", "priority", "deemphasis_us"}
_STATION_KEYS = _DEFAULT_KEYS | {
    "id", "name", "fm_mhz", "mw_khz", "stream", "stream_hls", "device",
    "remote", "remote_station", "remote_token", "privacy",
    "mhz", "mode", "channel_khz", "squelch_db", "acknowledge", "transcribe", "translate",
    "privacy_floor",
}  # fmt: skip
_RADIO_KEYS = {"region", "policy"}
_DEVICE_KEYS = {"id", "serial", "center_mhz", "gain_db", "sample_rate", "ppm"}
MAX_PPM = 500  # far beyond any dongle's crystal (a cheap one is within 100)
DEFAULT_SAMPLE_RATE = 2_400_000
# The sample rates an RTL2832U takes (librtlsdr refuses any other, and rtl_sdr then exits).
RTL_RATES = ((225_001, 300_000), (900_001, 3_200_000))

DEFAULT_STATIONS_PATH = Path("config/stations.toml")
EXAMPLE_STATIONS_PATH = Path("config/stations.example.toml")


class ConfigError(ValueError):
    """The configuration file is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class SdrDevice:
    """One receiver dongle, addressed by its USB serial so the port doesn't matter."""

    id: str
    serial: str
    center_hz: int  # where the tuner sits; its stations lie within ±(rate/2 - 120 kHz)
    gain_db: float  # manual tuner gain; there is deliberately no AGC
    sample_rate: int = DEFAULT_SAMPLE_RATE
    # The crystal's error in parts per million, as `rtl_test -p` measures it. The rtl tools
    # correct the tuning and the sample rate with it (-p). 0 suits a dongle with a TCXO; an
    # uncorrected 50 ppm is 22 kHz at 446 MHz, two channels away from a 12.5 kHz one.
    ppm: int = 0


@dataclass(frozen=True)
class SdrTuning:
    """Where a station is heard on an SDR: the device and the station's frequency on it."""

    device: SdrDevice
    freq_hz: int
    deemphasis_us: float = 50.0  # 50 µs in Europe and most of the world, 75 µs in the Americas
    mode: str = "wfm"  # wfm (broadcast FM), nfm (narrowband FM), am
    channel_hz: float | None = None  # narrowband channel width; None: the mode's default
    squelch_db: float | None = None  # narrowband: a fixed squelch level (dBFS); None: automatic


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    language: str  # BCP-47 source language, e.g. "pt-PT"
    translate_to: tuple[str, ...]  # continuous translation targets (primary first)
    alert_languages: tuple[str, ...]
    priority: int = 5  # 1 = most important; drives the ASR queue order
    stream: str | None = None
    stream_hls: str | None = None
    fm_mhz: tuple[float, ...] = ()
    mhz: tuple[float, ...] = ()  # SDR frequencies in any mode (`mhz`); fm_mhz is broadcast FM
    mw_khz: float | None = None
    sdr: SdrTuning | None = None  # set when the station names a [[device]] that can hear it
    # Another Wavescribe's station, followed through its text feed (no audio, no models here).
    remote: str | None = None  # that machine's base URL, e.g. http://node.local:8765
    remote_station: str | None = None  # the station's id over there
    remote_token: str | None = None  # a feed token that machine issued, if it asks for one
    asr_model: str | None = None  # chosen on the page instead of the chain's pick
    mt_model: str | None = None  # a translator chosen on the page instead of the shared one
    disabled: bool = False  # switched off on the page: listed, never started
    transcribe: bool = True  # false: listen only (audio, level, RDS); no models needed
    translate: bool = True  # false: transcribed, not translated (translate_to is kept)
    # What may happen to what is heard (radio/bandplan.py, docs/RADIO.md): broadcast for streams
    # and broadcast bands; amateur, utility or private for other radio.
    privacy: str = "broadcast"
    band: str | None = None  # the band plan entry an SDR station's frequency is in
    # Set when a stricter class is kept after a change of region (``privacy_floor``): the class
    # the region gives, which applies once the user confirms it.
    privacy_region: str | None = None
    # A remote station's class from its config: the other machine may make it stricter, never
    # looser (remote.py).
    privacy_set: str | None = None

    @property
    def translation_targets(self) -> tuple[str, ...]:
        """``translate_to`` minus the station's own language (nothing to do for those); none
        while the station isn't transcribed or translated."""
        if not (self.transcribe and self.translate):
            return ()
        return translation_targets(self.language, self.translate_to)


@dataclass(frozen=True)
class StationsConfig:
    stations: tuple[Station, ...]
    path: Path
    devices: tuple[SdrDevice, ...] = ()
    defaults: Mapping[str, Any] = field(default_factory=dict)  # the file's [defaults]
    radio_region: str | None = None  # [radio] region; None: from the machine's locale
    radio_policies: Mapping[str, Any] = field(default_factory=dict)  # [radio.policy.<class>]

    def get(self, station_id: str) -> Station:
        for station in self.stations:
            if station.id == station_id:
                return station
        known = ", ".join(s.id for s in self.stations)
        raise ConfigError(f"unknown station {station_id!r} in {self.path} (known: {known})")


def resolve_stations_path(explicit: Path | None = None) -> Path:
    """Pick the stations file: an explicit path, else config/stations.toml, else the example."""
    if explicit is not None:
        return explicit
    if DEFAULT_STATIONS_PATH.exists():
        return DEFAULT_STATIONS_PATH
    if EXAMPLE_STATIONS_PATH.exists():
        log.info(
            "using %s; copy it to %s to customise", EXAMPLE_STATIONS_PATH, DEFAULT_STATIONS_PATH
        )
        return EXAMPLE_STATIONS_PATH
    raise ConfigError(f"no stations file: create {DEFAULT_STATIONS_PATH}")


def load_stations(path: Path) -> StationsConfig:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"{path}: not found") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e
    return parse_stations(raw, path)


def parse_stations(raw: Mapping[str, Any], path: Path) -> StationsConfig:
    unknown_top = set(raw) - {"defaults", "station", "device", "radio"}
    if unknown_top:
        raise ConfigError(f"{path}: unknown top-level keys {sorted(unknown_top)}")
    radio = _table(raw, "radio", path)
    _check_keys(radio, _RADIO_KEYS, f"{path} [radio]")
    region = radio.get("region")
    if region is not None:
        from wavescribe.radio.bandplan import REGIONS

        if region not in REGIONS:
            raise ConfigError(f"{path} [radio]: region must be one of {', '.join(REGIONS)}")
    from wavescribe.radio.bandplan import check_policies

    try:
        policies = check_policies(radio.get("policy", {}), f"{path} [radio.policy]")
    except ValueError as e:
        raise ConfigError(str(e)) from e

    defaults = _table(raw, "defaults", path)
    _check_keys(defaults, _DEFAULT_KEYS, f"{path} [defaults]")

    devices: dict[str, SdrDevice] = {}
    for where, entry in _tables(raw, "device", path):
        _check_keys(entry, _DEVICE_KEYS, where)
        device = build_device(entry, where)
        if device.id in devices:
            raise ConfigError(f"{where}: duplicate device id {device.id!r}")
        if any(d.serial == device.serial for d in devices.values()):
            raise ConfigError(f"{where}: serial {device.serial} is already used by another device")
        devices[device.id] = device

    stations: list[Station] = []
    seen: set[str] = set()
    for where, entry in _tables(raw, "station", path):
        _check_keys(entry, _STATION_KEYS, where)
        merged = {**defaults, **entry}
        station = build_station(merged, where, devices, region)
        if station.id in seen:
            raise ConfigError(f"{where}: duplicate station id {station.id!r}")
        seen.add(station.id)
        stations.append(station)

    if not stations:
        raise ConfigError(f"{path}: no [[station]] entries")
    return StationsConfig(
        stations=tuple(stations),
        path=path,
        devices=tuple(devices.values()),
        defaults=defaults,
        radio_region=region,
        radio_policies=policies,
    )


def _table(raw: Mapping[str, Any], key: str, path: Path) -> Mapping[str, Any]:
    """The ``[key]`` table of the file, empty when it is absent."""
    table = raw.get(key, {})
    if not isinstance(table, Mapping):
        raise ConfigError(f"{path}: [{key}] must be a table of settings, not {key} = {table!r}")
    return table


def _tables(raw: Mapping[str, Any], key: str, path: Path) -> list[tuple[str, Mapping[str, Any]]]:
    """The ``[[key]]`` blocks of the file, each with where it is for error messages. A single
    ``[key]`` table is refused here: read as a list it would yield its key names."""
    entries = raw.get(key, [])
    if not isinstance(entries, list):
        raise ConfigError(
            f"{path}: {key}s are written as [[{key}]], with double brackets, one block for each"
        )
    out = []
    for i, entry in enumerate(entries):
        where = f"{path} {key} #{i + 1}"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where}: must be a [[{key}]] table, not {entry!r}")
        out.append((where, entry))
    return out


def _languages(d: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    """A list of language tags. A bare string is refused: read as a list it gives letters."""
    value = d.get(key, ())
    if not isinstance(value, list | tuple):
        example = json.dumps([value] if isinstance(value, str) else ["en", "uk"])
        raise ConfigError(f"{where}: '{key}' must be a list of language tags, e.g. {example}")
    for tag in value:
        _check_language(tag, where)
    return tuple(value)


def _frequencies(d: Mapping[str, Any], key: str, where: str) -> tuple[float, ...]:
    """One frequency in MHz or a list of them (the same station on several transmitters)."""
    value = d.get(key, [])
    several = value if isinstance(value, list | tuple) else [value]
    if not all(_is_number(f) for f in several):
        raise ConfigError(f"{where}: '{key}' must be a frequency in MHz or a list of them, "
                          "e.g. 99.0 or [99.0, 94.4]")  # fmt: skip
    return tuple(float(f) for f in several)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_keys(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}")


def build_device(d: Mapping[str, Any], where: str) -> SdrDevice:
    device_id = d.get("id")
    if not isinstance(device_id, str) or not _STATION_ID.fullmatch(device_id):
        raise ConfigError(f"{where}: 'id' must be lowercase letters, digits, '-' or '_'")
    where = f"{where} ({device_id})"
    serial = d.get("serial")
    if not isinstance(serial, str) or not serial:
        raise ConfigError(f"{where}: 'serial' must be a string, e.g. \"00001234\"")
    _check_serial(serial, where)
    center = d.get("center_mhz")
    if not isinstance(center, int | float) or center <= 0:
        raise ConfigError(f"{where}: 'center_mhz' is required (MHz)")
    _check_mhz(center, f"{where}: 'center_mhz'", tunable=True)
    gain = d.get("gain_db")
    if not isinstance(gain, int | float) or gain <= 0:
        raise ConfigError(f"{where}: 'gain_db' is required and must be > 0 (manual gain, no AGC)")
    rate = d.get("sample_rate", DEFAULT_SAMPLE_RATE)
    if (
        not isinstance(rate, int)
        or not supported_rate(rate)
        or not any(lo <= rate <= hi for lo, hi in RTL_RATES)
    ):
        raise ConfigError(
            f"{where}: 'sample_rate' must be a multiple of 240000 that the dongle takes "
            "(240000, or 960000 to 3120000), e.g. 2400000"
        )
    ppm = d.get("ppm", 0)
    if not isinstance(ppm, int) or isinstance(ppm, bool) or abs(ppm) > MAX_PPM:
        raise ConfigError(
            f"{where}: 'ppm' must be a whole number of parts per million between -{MAX_PPM} "
            f"and {MAX_PPM}, e.g. 52 (rtl_test -p measures it)"
        )
    return SdrDevice(device_id, serial, round(center * 1e6), float(gain), rate, ppm)


def _check_serial(serial: str, where: str) -> None:
    # Serials that read as a device index to the rtl tools (the factory 00000001) are fine here:
    # sources/sdr.py turns them into the dongle's index when it opens one (device_arg).
    if not re.fullmatch(r"[\x21-\x7e]{1,64}", serial):
        raise ConfigError(f"{where}: serial {serial!r} must be printable, with no spaces")


def _check_mhz(mhz: object, where: str, tunable: bool = False) -> None:
    try:
        hz = frequency_hz(mhz)
        if tunable:
            check_tunable(hz)
    except ValueError as e:
        raise ConfigError(f"{where}: {e}") from e


def _pick_frequency(fm_mhz: tuple[float, ...], device: SdrDevice, mode: str = "wfm") -> int | None:
    """The station's frequency nearest the device's centre, if any is in the device's band."""
    heard = [
        round(f * 1e6) for f in fm_mhz
        if in_band(f * 1e6, device.center_hz, device.sample_rate, mode)
    ]  # fmt: skip
    return min(heard, key=lambda f: abs(f - device.center_hz)) if heard else None


def _reach_mhz(device: SdrDevice, mode: str = "wfm") -> float:
    return (device.sample_rate / 2 - margin_hz(mode)) / 1e6


def default_mode(freq_hz: float, region: str | None) -> str:
    """How a frequency is demodulated when the config doesn't say: from the band plan (AM in
    the airband, NFM for walkie-talkies), NFM where no plan lists it."""
    from wavescribe.radio.bandplan import default_region, lookup

    band = lookup(freq_hz, region or default_region())
    return band.mode if band is not None and band.mode else "nfm"


def radio_policy(freq_hz: float, mode: str, region: str | None, acknowledge: bool,
                 where: str, width_hz: float | None = None) -> tuple[str, str | None]:  # fmt: skip
    """The privacy class and band of an SDR frequency, and an acknowledgement where the band
    plan wants one: the user confirms they may receive it where they are (docs/RADIO.md).
    ``width_hz`` is the channel's width, when it's known: the class is the strictest of the
    bands its edges reach (``bandplan.lookup``). Digital services can be tuned too, but only
    analogue demodulators exist: no digital decoder, no decryption."""
    from wavescribe.radio.bandplan import AUTO, POLICIES, lookup

    band = lookup(freq_hz, region or AUTO, width_hz)
    privacy = band.privacy if band else "private"
    what = band.name if band else "a frequency no band plan lists"
    if mode not in MODES:
        raise ConfigError(f"{where}: 'mode' must be one of {', '.join(MODES)}")
    if POLICIES[privacy].needs_ack and not acknowledge:
        extra = (" It carries a digital service: Wavescribe has no digital decoder, so you will "
                 "hear noise.") if privacy == "digital" else ""  # fmt: skip
        mhz = f"{freq_hz / 1e6:.5f}".rstrip("0").rstrip(".")
        raise ConfigError(
            f"{where}: {mhz} MHz is {what} ({privacy} radio). Laws on receiving it "
            f"differ by country.{extra} Set acknowledge = true to confirm you may receive it "
            "where you are (docs/RADIO.md)"
        )
    return privacy, band.id if band else None


def parse_sdr_source(spec: str, region: str | None = None) -> SdrTuning:
    """``sdr:SERIAL/MHZ[?gain=20&ppm=0&center=99.2&rate=2400000&deemphasis=50&mode=nfm
    &channel=12.5&squelch=-40&ack=1]`` for ``--source``."""
    parts = urlsplit(spec)
    serial, _, mhz = parts.path.partition("/")
    if parts.scheme != "sdr" or not serial or not mhz:
        raise ConfigError(f"{spec!r}: expected sdr:SERIAL/MHZ, e.g. sdr:00001234/99.0")
    _check_serial(serial, spec)
    q = {k: v[-1] for k, v in parse_qs(parts.query).items()}
    unknown = set(q) - {
        "gain", "ppm", "center", "rate", "deemphasis", "mode", "channel", "squelch", "ack",
    }  # fmt: skip
    if unknown:
        raise ConfigError(f"{spec!r}: unknown options {sorted(unknown)}")
    _check_mhz(mhz, spec, tunable=True)
    try:
        freq = float(mhz)
        mode = q.get("mode") or default_mode(freq * 1e6, region)
        # Tune below the station so it doesn't sit on the centre (DC) bin.
        center = float(q.get("center", freq - OFF_CENTRE_HZ.get(mode, 250_000) / 1e6))
        rate = int(q.get("rate", DEFAULT_SAMPLE_RATE))
        gain = float(q.get("gain", 20))
        ppm = int(q.get("ppm", 0))
        deemphasis = float(q.get("deemphasis", 50))
        channel = float(q["channel"]) * 1e3 if "channel" in q else None
        squelch = float(q["squelch"]) if "squelch" in q else None
    except ValueError as e:
        raise ConfigError(f"{spec!r}: {e}") from e
    # As build_station checks them: a bad one would only fail on the dongle's thread.
    if channel is not None and not MIN_CHANNEL_HZ <= channel <= MAX_CHANNEL_HZ:
        raise ConfigError(f"{spec!r}: channel must be {MIN_CHANNEL_HZ / 1e3:g} to "
                          f"{MAX_CHANNEL_HZ / 1e3:g} kHz, e.g. 12.5 or 25")  # fmt: skip
    if not deemphasis > 0:  # NaN too
        raise ConfigError(f"{spec!r}: deemphasis must be a positive number of µs (50 or 75)")
    device = build_device(
        {
            "id": "cli",
            "serial": serial,
            "center_mhz": center,
            "gain_db": gain,
            "sample_rate": rate,
            "ppm": ppm,
        },
        spec,
    )
    tuning_hz = _pick_frequency((freq,), device, mode)
    if tuning_hz is None:
        raise ConfigError(f"{spec!r}: {freq} MHz is outside the band around {center} MHz")
    width = WFM_CHANNEL_HZ if mode == "wfm" else channel
    radio_policy(tuning_hz, mode, region, q.get("ack") in ("1", "true", "yes"), spec, width)
    if not off_centre(tuning_hz, device.center_hz, mode, channel):
        raise ConfigError(
            f"{spec!r}: {freq} MHz is at the centre of the band around {center} MHz, where every "
            "dongle has a spike of its own that would hold the squelch open; leave out center= "
            "or give another"
        )
    return SdrTuning(device, tuning_hz, deemphasis, mode, channel, squelch)


def build_station(
    d: Mapping[str, Any],
    where: str,
    devices: Mapping[str, SdrDevice] | None = None,
    region: str | None = None,
) -> Station:
    station_id = d.get("id")
    if not isinstance(station_id, str) or not _STATION_ID.fullmatch(station_id):
        raise ConfigError(f"{where}: 'id' must be lowercase letters, digits, '-' or '_'")
    where = f"{where} ({station_id})"

    language = d.get("language")
    if language is None:
        raise ConfigError(f"{where}: 'language' is required (here or in [defaults])")
    _check_language(language, where)
    translate_to = _languages(d, "translate_to", where)
    alert_languages = _languages(d, "alert_languages", where)

    priority = d.get("priority", 5)
    if not isinstance(priority, int) or priority < 1:
        raise ConfigError(f"{where}: 'priority' must be a positive integer")

    fm_mhz = _frequencies(d, "fm_mhz", where)
    # `mhz` is any SDR frequency (an NFM channel, an airband frequency); `fm_mhz` is broadcast FM.
    mhz = _frequencies(d, "mhz", where)
    freqs = mhz or fm_mhz
    for f in (*fm_mhz, *mhz):
        _check_mhz(f, f"{where}: frequency")
    mw_khz = d.get("mw_khz")
    if mw_khz is not None and not _is_number(mw_khz):
        raise ConfigError(f"{where}: 'mw_khz' must be a frequency in kHz, e.g. 666")
    # fm_mhz is broadcast FM; a bare mhz is demodulated as its band plan says.
    mode = d.get("mode") or ("wfm" if not mhz else default_mode(freqs[0] * 1e6, region))

    sdr, privacy, band, privacy_region = None, "broadcast", None, None
    if (device_id := d.get("device")) is not None:
        device = (devices or {}).get(device_id)
        if device is None:
            known = ", ".join(devices or {}) or "none"
            raise ConfigError(f"{where}: unknown device {device_id!r} (known: {known})")
        if mode not in MODES:
            raise ConfigError(f"{where}: 'mode' must be one of {', '.join(MODES)}")
        freq = _pick_frequency(freqs, device, mode)
        if freq is None:
            key = "mhz" if mhz else "fm_mhz"
            raise ConfigError(
                f"{where}: none of {key} {list(freqs)} is within device {device_id!r}'s band "
                f"({device.center_hz / 1e6:.2f} MHz ± {_reach_mhz(device, mode):.2f})"
            )
        deemphasis = d.get("deemphasis_us", 50.0)
        if not isinstance(deemphasis, int | float) or deemphasis <= 0:
            raise ConfigError(f"{where}: 'deemphasis_us' must be a positive number (50 or 75)")
        channel = d.get("channel_khz")
        squelch = d.get("squelch_db")
        for key, value in (("channel_khz", channel), ("squelch_db", squelch)):
            if value is not None and not isinstance(value, int | float):
                raise ConfigError(f"{where}: '{key}' must be a number")
        if channel and not MIN_CHANNEL_HZ <= channel * 1e3 <= MAX_CHANNEL_HZ:
            raise ConfigError(f"{where}: 'channel_khz' must be {MIN_CHANNEL_HZ / 1e3:g} to "
                              f"{MAX_CHANNEL_HZ / 1e3:g}, e.g. 12.5 or 25")  # fmt: skip
        width = WFM_CHANNEL_HZ if mode == "wfm" else channel * 1e3 if channel else None
        privacy, band = radio_policy(freq, mode, region, bool(d.get("acknowledge")), where, width)
        floor = d.get("privacy_floor")
        if floor is not None:
            from wavescribe.radio.bandplan import STRICTNESS, stricter

            if floor not in STRICTNESS:
                raise ConfigError(f"{where}: 'privacy_floor' must be one of "
                                  f"{', '.join(STRICTNESS)}")  # fmt: skip
            if stricter(floor, privacy):
                privacy_region, privacy = privacy, floor
        if not off_centre(freq, device.center_hz, mode, channel and channel * 1e3):
            raise ConfigError(
                f"{where}: {freq / 1e6:.5f} MHz is at the centre of device {device_id!r}, where "
                "every dongle has a spike of its own that would hold this channel's squelch "
                f"open. Give the device another center_mhz, e.g. "
                f"{(freq - OFF_CENTRE_HZ[mode]) / 1e6:.5f}"
            )
        sdr = SdrTuning(device, freq, float(deemphasis), mode,
                        channel * 1e3 if channel else None, squelch)  # fmt: skip

    remote, remote_station = d.get("remote"), d.get("remote_station")
    if remote is not None:
        pushed = remote == "push"  # the other machine pushes to our /feed/v1/ingest
        if not pushed and (
            not isinstance(remote, str) or not re.match(r"^https?://[^\s/]+", remote)
        ):
            raise ConfigError(f"{where}: 'remote' must be the other machine's http(s) URL")
        if not isinstance(remote_station, str) or not remote_station:
            raise ConfigError(f"{where}: 'remote' needs 'remote_station' (its id over there)")
    if (stated := d.get("privacy")) is not None:
        from wavescribe.radio.bandplan import PRIVACY

        if remote is None or stated not in PRIVACY:
            classes = ", ".join(PRIVACY)
            raise ConfigError(f"{where}: 'privacy' is for remote stations, one of {classes}; "
                              "radio heard here has its band's class")  # fmt: skip
        privacy = stated
    for key in ("stream", "stream_hls"):  # ffmpeg opens files and sockets too: http(s) only
        url = d.get(key)
        if url is not None and (
            not isinstance(url, str) or not re.match(r"^https?://[^\s/]+", url.strip(), re.I)
        ):
            raise ConfigError(f"{where}: '{key}' must be an http(s) URL")

    for key in ("transcribe", "translate"):
        if not isinstance(d.get(key, True), bool):
            raise ConfigError(f"{where}: '{key}' must be true or false")

    return Station(
        id=station_id,
        name=str(d.get("name", station_id)),
        language=language,
        translate_to=translate_to,
        alert_languages=alert_languages,
        priority=priority,
        stream=d.get("stream"),
        stream_hls=d.get("stream_hls"),
        fm_mhz=fm_mhz,
        mhz=mhz,
        mw_khz=float(mw_khz) if mw_khz is not None else None,
        sdr=sdr,
        remote=remote.rstrip("/") if remote else None,
        remote_station=remote_station if remote else None,
        remote_token=d.get("remote_token") if remote else None,
        privacy=privacy,
        band=band,
        privacy_region=privacy_region,
        privacy_set=stated,
        transcribe=d.get("transcribe", True),
        translate=d.get("translate", True),
    )


def _check_language(tag: object, where: str) -> None:
    if not isinstance(tag, str) or not _BCP47.fullmatch(tag):  # $ alone allows a trailing newline
        raise ConfigError(f"{where}: {tag!r} is not a BCP-47 language tag")
