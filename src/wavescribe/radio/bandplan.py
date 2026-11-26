# SPDX-License-Identifier: Apache-2.0
"""Band plans (``bands.toml``): what a frequency is, how its voice is demodulated, and what
Wavescribe may do with what it hears there (its privacy class; docs/RADIO.md)."""

from __future__ import annotations

import os
import subprocess
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wavescribe.config import Station

BANDS_PATH = Path(__file__).with_name("bands.toml")
PRIVACY = ("broadcast", "amateur", "utility", "private", "digital")
REGIONS = ("cept", "1", "2", "3", "jp")
MODES = ("wfm", "nfm", "am")
# No region chosen: bands, rasters and modes come from this machine's locale, but a frequency
# takes the strictest class any region gives it (unlisted counts as private), unless the locale's
# region lists it as broadcast. A locale says how a Mac is set up, not where it is: a Mac in
# Europe set to US English would otherwise call PMR446 walkie-talkies amateur radio.
AUTO = "auto"


@dataclass(frozen=True)
class Policy:
    """What happens to what is heard on a channel of one privacy class."""

    needs_ack: bool  # the operator confirms they may listen before a station is created
    text_days: float | None  # how long text is kept (None: the store's own retention; 0: never)
    audio: bool  # speech audio may be archived
    export: bool  # the text leaves the machine (feed, webhooks, remote followers)
    quote_in_alerts: bool  # alerts carry the words heard, not just that something matched


# The defaults: conservative, because laws on radio that isn't broadcast differ by country. The
# user can change every one but `needs_ack` ([radio.policy.<class>] in the stations file, or the
# page's Settings → Storage and privacy), and is responsible for what the law where they are
# allows (docs/RADIO.md).
DEFAULT_POLICIES = {
    "broadcast": Policy(False, None, True, True, True),
    "amateur": Policy(False, 7, False, False, True),
    "utility": Policy(True, 3, False, False, True),
    "private": Policy(True, 0, False, False, False),
    "digital": Policy(True, 0, False, False, False),
}
POLICIES = dict(DEFAULT_POLICIES)  # in effect: the defaults with the user's changes over them
# From the least strict to the most. A change of region alone never moves a station down this
# list: it keeps the stricter class until the user confirms the new one (docs/RADIO.md).
STRICTNESS = ("broadcast", "amateur", "utility", "private", "digital")


def stricter(a: str, b: str) -> bool:
    """Whether class ``a`` is stricter than class ``b``."""
    return STRICTNESS.index(a) > STRICTNESS.index(b)


POLICY_KEYS = ("text_days", "audio", "export", "quote_in_alerts")


def check_policies(raw: object, where: str) -> dict[str, dict[str, object]]:
    """``{class: {text_days, audio, export, quote_in_alerts}}`` as the user wrote it, checked.
    ``text_days`` is a number of days (0: never kept) or "keep" (the store's own retention)."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: policy must be a table per class")
    out: dict[str, dict[str, object]] = {}
    for cls, changes in raw.items():
        if cls not in DEFAULT_POLICIES or cls == "broadcast":
            raise ValueError(
                f"{where}: {cls!r} isn't one of amateur, utility, private, digital "
                "(broadcast radio is kept like any station)"
            )
        if not isinstance(changes, dict):
            raise ValueError(f"{where}.{cls}: expected a table")
        bad = set(changes) - set(POLICY_KEYS)
        if bad:
            raise ValueError(
                f"{where}.{cls}: unknown keys {sorted(bad)} (known: {', '.join(POLICY_KEYS)})"
            )
        for k, v in changes.items():
            if k == "text_days":
                ok = v == "keep" or (
                    isinstance(v, int | float) and not isinstance(v, bool) and v >= 0
                )
                if not ok:
                    raise ValueError(
                        f'{where}.{cls}.text_days: a number of days (0: never), or "keep"'
                    )
            elif not isinstance(v, bool):
                raise ValueError(f"{where}.{cls}.{k}: true or false")
        out[cls] = dict(changes)
    return out


def apply_policies(*layers: dict[str, dict[str, object]] | None) -> None:
    """Put the user's changes into effect, each layer over the one before (the stations file,
    then the page). In place, so every module sees the change at once."""
    merged = {cls: vars(p).copy() for cls, p in DEFAULT_POLICIES.items()}
    for layer in layers:
        for cls, changes in (layer or {}).items():
            for k, v in changes.items():
                merged[cls][k] = None if (k == "text_days" and v == "keep") else v
    POLICIES.clear()
    POLICIES.update({cls: Policy(**d) for cls, d in merged.items()})  # type: ignore[arg-type]


def policy_of(station: object) -> Policy | None:
    """The policy for a station (anything with a ``privacy``); None for broadcast or unknown,
    which have no restrictions beyond the store's own retention."""
    privacy = getattr(station, "privacy", "broadcast")
    return None if privacy == "broadcast" else POLICIES.get(privacy, POLICIES["private"])


def class_of(station: object) -> str:
    """A station's privacy class, as it is kept with what was heard on it; a class this version
    doesn't know counts as private, as it does for the policy."""
    privacy = getattr(station, "privacy", "broadcast")
    return privacy if privacy in POLICIES else "private"


def exportable(station: object) -> bool:
    p = policy_of(station)
    return p is None or p.export


def quotable(station: object) -> bool:
    p = policy_of(station)
    return p is None or p.quote_in_alerts


def loggable(station: object) -> bool:
    """Whether its words may go into the log file: broadcast only, since the log outlives the
    retention of every other class."""
    return policy_of(station) is None


UNQUOTED = "(words not quoted for this kind of radio)"


def logged_text(station: object, text: str, limit: int = 120) -> str:
    return text[:limit] if loggable(station) else UNQUOTED


def redact_alert(alert: dict) -> dict:
    """An alert as it may leave the machine when it was heard on radio that stays here: which
    rule, when and where, but no words, matched terms, LLM reason or audio."""
    return {**alert, "quote": "", "summaries": {}, "terms": [], "segment_id": None,
            "reason": None}  # fmt: skip


def mhz_text(mhz: float) -> str:
    """A frequency in MHz as people write it, to the hertz: 446.00625, 98.6 (``:g`` would make
    the first 446.006, which is no PMR446 channel)."""
    return f"{mhz:.6f}".rstrip("0").rstrip(".")


def outcomes_only(alert: dict) -> dict:
    """An alert's actions as they may leave: whether each worked, not how it failed (the error
    text can hold a local path)."""
    actions = alert.get("actions")
    if not actions:
        return alert
    return {**alert, "actions": {a: "ok" if o == "ok" else "failed" for a, o in actions.items()}}


TUNER_RANGE_HZ = (24_000_000, 1_766_000_000)  # what an RTL-SDR's R820T/R828D tuner reaches


@dataclass(frozen=True)
class Exports:
    """What may leave this machine as things stand: the stations that are configured and whose
    class exports, and the classes that export.

    Words that were heard earlier are judged by both. By the class their station had when they
    were heard, which is kept with them, so a station id moved to another frequency doesn't
    take its old words out under its new class. And by their station still being configured and
    exporting, so what a deleted or unknown station left behind stays here."""

    stations: frozenset[str]
    classes: frozenset[str]

    def heard(self, station_id: str, privacy: str | None) -> bool:
        """Whether words heard on ``station_id`` under class ``privacy`` may leave. None is a
        class nobody recorded (text kept by an earlier version): the station alone decides."""
        return station_id in self.stations and (privacy is None or privacy in self.classes)

    def utterance(self, utterance: Mapping) -> bool:
        """Whether an utterance (the page's payload shape, live or stored) may leave."""
        return self.heard(utterance.get("station", ""), utterance.get("privacy"))

    def quotes(self, alert: dict) -> bool:
        """Whether an alert's words may leave: every station it was heard on may be quoted
        from, and so may every class those stations had then."""
        stations = alert.get("stations") or ()
        classes = alert.get("privacy") or ()
        return all(s in self.stations for s in stations) and all(c in self.classes for c in classes)

    def alert(self, alert: dict) -> dict:
        """An alert as it may leave: whole when ``quotes`` allows it, otherwise without its
        words. Either way an action that failed says only that it failed."""
        alert = outcomes_only(alert)
        return alert if self.quotes(alert) else redact_alert(alert)


def exports(stations: Iterable[Station]) -> Exports:
    """What may leave this machine, with these stations configured and the policies in effect."""
    classes = frozenset(cls for cls, policy in POLICIES.items() if policy.export)
    return Exports(frozenset(s.id for s in stations if exportable(s)), classes)


@dataclass(frozen=True)
class Band:
    id: str
    name: str
    start_hz: int
    stop_hz: int
    privacy: str
    regions: tuple[str, ...]
    mode: str | None = None  # None for digital bands
    step_hz: float | None = None
    channel_hz: float | None = None
    channels: tuple[int, ...] = ()
    region_modes: tuple[tuple[str, str], ...] = ()  # (region, mode) where it isn't `mode`

    @property
    def width_hz(self) -> int:
        return self.stop_hz - self.start_hz

    def contains(self, hz: float) -> bool:
        return self.start_hz <= hz <= self.stop_hz

    def snap(self, hz: float) -> int:
        """The nearest channel: from the channel list when one is within a step, otherwise on
        the raster from the band's start (a band can span a gap between its channel groups)."""
        if self.channels:
            nearest = min(self.channels, key=lambda c: abs(c - hz))
            if abs(nearest - hz) <= (self.step_hz or 12_500):
                return nearest
        if not self.step_hz:
            return round(hz)
        n = round((hz - self.start_hz) / self.step_hz)
        return round(self.start_hz + n * self.step_hz)

    def channel_number(self, hz: float, tolerance_hz: float = 2_000) -> int | None:
        """1-based number in the channel list, when ``hz`` is on one."""
        for i, c in enumerate(self.channels):
            if abs(c - hz) <= tolerance_hz:
                return i + 1
        return None

    def label(self, hz: float) -> str:
        n = self.channel_number(hz)
        return f"{self.name}, channel {n}" if n else self.name

    def to_dict(self) -> dict[str, object]:
        lo, hi = TUNER_RANGE_HZ
        return {
            "id": self.id, "name": self.name, "start_mhz": self.start_hz / 1e6,
            "tunable": lo <= self.start_hz and self.stop_hz <= hi,
            "stop_mhz": self.stop_hz / 1e6, "mode": self.mode, "privacy": self.privacy,
            "step_khz": self.step_hz / 1e3 if self.step_hz else None,
            "channel_khz": self.channel_hz / 1e3 if self.channel_hz else None,
            "channels_mhz": [c / 1e6 for c in self.channels], "regions": list(self.regions),
        }  # fmt: skip


def _mhz(x: float) -> int:
    return round(x * 1e6)


@cache
def bands(path: Path = BANDS_PATH) -> tuple[Band, ...]:
    raw = tomllib.loads(path.read_text("utf-8"))
    out = []
    for b in raw["band"]:
        if b["privacy"] not in PRIVACY:
            raise ValueError(f"{path}: band {b['id']}: unknown privacy {b['privacy']!r}")
        step = b.get("step_khz")
        width = b.get("channel_khz")
        modes = b.get("region_mode", {})
        for r, m in modes.items():
            if r not in REGIONS or m not in MODES:
                raise ValueError(f"{path}: band {b['id']}: region_mode {r} = {m!r}")
        out.append(Band(
            id=b["id"], name=b["name"], start_hz=_mhz(b["start"]), stop_hz=_mhz(b["stop"]),
            privacy=b["privacy"], regions=tuple(b.get("regions", ())), mode=b.get("mode"),
            step_hz=step * 1e3 if step else None, channel_hz=width * 1e3 if width else None,
            channels=tuple(_mhz(c) for c in b.get("channels", ())),
            region_modes=tuple(modes.items()),
        ))  # fmt: skip
    return tuple(out)


def _wider(region: str) -> tuple[str, ...]:
    """A region and the ITU region it is part of: CEPT's bands are Region 1's and its own."""
    return {"cept": ("cept", "1"), "jp": ("jp", "3")}.get(region, (region,))


def _in_region(band: Band, region: str) -> bool:
    if region == "jp" and band.id == "fm":
        return False  # Japan's FM band is its own
    return bool(set(_wider(region)) & set(band.regions))


def _as_in(band: Band, region: str) -> Band:
    """A band as it is used in a region: its mode there, when the region's differs (CB is FM in
    the CEPT countries and AM elsewhere), the region's own entry before its ITU region's."""
    modes = dict(band.region_modes)
    mode = next((modes[r] for r in _wider(region) if r in modes), None)
    return replace(band, mode=mode) if mode and mode != band.mode else band


def region_bands(region: str) -> list[Band]:
    if region == AUTO:
        region = default_region()
    if region not in REGIONS:
        raise ValueError(f"unknown radio region {region!r} (one of {', '.join(REGIONS)})")
    return [_as_in(b, region) for b in bands() if _in_region(b, region)]


def lookup(hz: float, region: str) -> Band | None:
    """The narrowest band of the region that contains ``hz``. Digital services of other regions
    count too. With no region chosen (``AUTO``), see ``_lookup_auto``."""
    if region == AUTO:
        return _lookup_auto(hz)
    here = [b for b in region_bands(region) if b.contains(hz)]
    if here:
        return min(here, key=lambda b: b.width_hz)
    digital = [b for b in bands() if b.privacy == "digital" and b.contains(hz)]
    return min(digital, key=lambda b: b.width_hz) if digital else None


def _lookup_auto(hz: float) -> Band | None:
    """The locale's band (its raster, mode and name), with the strictest class of any region.
    FM broadcast in the locale's region stays broadcast: Japan doesn't list 95-108 MHz, and FM
    elsewhere mustn't become private because of it. Other broadcast (US weather radio) doesn't:
    its frequencies are land mobile and marine elsewhere."""
    base = lookup(hz, default_region())
    strictest = max((privacy_of(hz, r) for r in REGIONS), key=PRIVACY.index)
    fm = base is not None and base.privacy == "broadcast" and base.mode == "wfm"
    if base is not None and (base.privacy == strictest or (fm and strictest != "digital")):
        return base
    named = [_as_in(b, default_region()) for b in bands()
             if b.contains(hz) and b.privacy == strictest]  # fmt: skip
    if named:  # e.g. PMR446 for a Mac in Europe set to US English, not the US 70 cm band
        return min(named, key=lambda b: b.width_hz)
    return replace(base, privacy=strictest) if base is not None else None


def privacy_of(hz: float, region: str) -> str:
    """The privacy class of a frequency; anything no plan lists counts as private."""
    band = lookup(hz, region)
    return band.privacy if band else "private"


def find(band_id: str) -> Band:
    for b in bands():
        if b.id == band_id:
            return b
    raise KeyError(band_id)


# The region a machine is in, from its locale's country, when the config doesn't say.
_CEPT = set(
    [
        "AL",
        "AD",
        "AT",
        "AZ",
        "BE",
        "BA",
        "BG",
        "HR",
        "CY",
        "CZ",
        "DK",
        "EE",
        "FI",
        "FR",
        "GE",
        "DE",
        "GR",
        "HU",
        "IS",
        "IE",
        "IT",
        "LV",
        "LI",
        "LT",
        "LU",
        "MT",
        "MD",
        "MC",
        "ME",
        "NL",
        "MK",
        "NO",
        "PL",
        "PT",
        "RO",
        "SM",
        "RS",
        "SK",
        "SI",
        "ES",
        "SE",
        "CH",
        "TR",
        "UA",
        "GB",
        "VA",
    ]
)
_REGION2 = set(
    [
        "AG",
        "AR",
        "BS",
        "BB",
        "BZ",
        "BO",
        "BR",
        "CA",
        "CL",
        "CO",
        "CR",
        "CU",
        "DM",
        "DO",
        "EC",
        "SV",
        "GD",
        "GL",
        "GT",
        "GY",
        "HT",
        "HN",
        "JM",
        "MX",
        "NI",
        "PA",
        "PY",
        "PE",
        "PR",
        "KN",
        "LC",
        "VC",
        "SR",
        "TT",
        "US",
        "UY",
        "VE",
    ]
)
# Mongolia isn't here: the Radio Regulations put it in Region 1, with the former Soviet Union.
_REGION3 = set(
    [
        "AF",
        "AU",
        "BD",
        "BN",
        "BT",
        "CN",
        "FJ",
        "HK",
        "ID",
        "IN",
        "IR",
        "KH",
        "KP",
        "KR",
        "LA",
        "LK",
        "MM",
        "MO",
        "MV",
        "MY",
        "NP",
        "NZ",
        "PG",
        "PH",
        "PK",
        "SG",
        "TH",
        "TL",
        "TW",
        "VN",
    ]
)


def region_for_country(country: str | None) -> str:
    c = (country or "").upper()
    if c == "JP":
        return "jp"
    if c in _CEPT:
        return "cept"
    if c in _REGION2:
        return "2"
    if c in _REGION3:
        return "3"
    return "1"


def system_country() -> str | None:
    """The country part of the machine's locale (macOS's AppleLocale, else $LANG)."""
    try:
        done = subprocess.run(["defaults", "read", "-g", "AppleLocale"], capture_output=True,
                              text=True, timeout=2)  # fmt: skip
        loc = done.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        loc = ""
    loc = loc or os.environ.get("LC_ALL") or os.environ.get("LANG") or ""
    part = loc.split(".")[0].split("@")[0].replace("-", "_").split("_")
    return part[1] if len(part) > 1 and len(part[1]) == 2 else None


@cache
def default_region() -> str:
    return region_for_country(system_country())
