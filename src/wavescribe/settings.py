# SPDX-License-Identifier: Apache-2.0
"""Settings the web page changes, kept in their own machine-written file (``config/ui.toml``).

The hand-edited files (``config/stations.toml``, ``config/alerts.toml``) are never rewritten:
the page's changes live here and are laid over them when the app starts and whenever they
change. One file, one writer, atomic replaces. Sections:

    [node]               this machine's name and public URL (for feeds and webhook links)
    [retention]          audio_days, audio_max_gb, text_days
    [models]             chain order per capability, e.g. asr = ["whisper-…", "parakeet-…"]
    [overrides.<id>]     per station: name, language, translate_to, alert_languages, priority,
                         asr_model, disabled, device, fm_mhz, stream, stream_hls
    [device_overrides.<id>]  per SDR dongle: center_mhz, gain_db, sample_rate, ppm
    [[station]]          stations added from the page (same keys as stations.toml, plus remote)
    [[device]]           dongles added from the page
    [[rule]]             alert rules from the page (keyword or llm)
    [[output]]           webhooks and streams
    [[feed_token]]       access tokens for /feed/v1 from other machines
    [catalog]            when the station catalogue snapshot was refreshed
    [radio]              region (the band plan), and [radio.policy.<class>]: what is kept of
                         radio that isn't broadcast (radio/bandplan.py, docs/RADIO.md)
    [remote_privacy]     per remote station: the privacy class the other machine gave it
"""

from __future__ import annotations

import copy
import logging
import math
import os
import re
import tempfile
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from wavescribe.config import (
    ConfigError,
    SdrDevice,
    Station,
    StationsConfig,
    build_device,
    build_station,
    radio_policy,
)

log = logging.getLogger(__name__)

DEFAULT_SETTINGS_PATH = Path("config/ui.toml")
HEADER = (
    "# Written by the Wavescribe page (wavescribe serve). Your hand-edited stations.toml and\n"
    "# alerts.toml are never touched: what you change on the page is kept here and laid over\n"
    "# them. Safe to edit while Wavescribe is stopped; delete it to undo every page change.\n"
)
OVERRIDE_KEYS = {
    "name", "language", "translate_to", "alert_languages", "priority", "asr_model", "disabled",
    "device", "fm_mhz", "stream", "stream_hls", "deemphasis_us",
    "mhz", "mode", "channel_khz", "squelch_db", "acknowledge", "transcribe", "translate",
    "mt_model", "privacy_floor",
}  # fmt: skip
DEVICE_OVERRIDE_KEYS = {"center_mhz", "gain_db", "sample_rate", "ppm"}


class Settings:
    """The page's settings: read once, changed through ``update``, saved atomically."""

    def __init__(self, path: Path = DEFAULT_SETTINGS_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._observers: list[Callable[[dict[str, Any]], None]] = []
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = tomllib.loads(path.read_text(encoding="utf-8"))
            except tomllib.TOMLDecodeError as e:
                raise ConfigError(f"{path}: {e}") from e

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data)

    def observe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        self._observers.append(callback)

    def update(self, mutate: Callable[[dict[str, Any]], Any]) -> Any:
        """Apply ``mutate`` to a copy; if it doesn't raise, save it and tell the observers."""
        with self._lock:
            draft = copy.deepcopy(self.data)
            result = mutate(draft)
            _prune(draft)
            self._write(draft)
            self.data = draft
            data = copy.deepcopy(draft)
        for callback in list(self._observers):
            try:
                callback(data)
            except Exception:
                log.exception("settings observer %r failed", callback)
        return result

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = HEADER + "\n" + dumps(data)
        # The app doesn't start with a ui.toml it can't read, so what is about to replace
        # the file is read back first; if that fails, the file stays as it is.
        try:
            same = tomllib.loads(text) == _as_written(data)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{self.path}: not saved, it would not load again ({e})") from e
        if not same:
            raise ConfigError(f"{self.path}: not saved, it would not read back as it was set")
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".ui-", suffix=".toml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)  # it can hold webhook secrets and feed tokens
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _as_written(value: Any) -> Any:
    """``value`` as it reads back from the file: ``dumps`` leaves out None values, and TOML
    has one kind of array."""
    if isinstance(value, Mapping):
        return {k: _as_written(v) for k, v in value.items() if v is not None}
    if isinstance(value, list | tuple):
        return [_as_written(v) for v in value]
    return value


def _prune(data: dict[str, Any]) -> None:
    """Drop empty tables and lists, so the file stays readable."""
    for key in list(data):
        value = data[key]
        if isinstance(value, dict):
            _prune(value)
        if value in ({}, [], None):
            del data[key]


# -- the stations the app runs: stations.toml with the page's changes laid over it ---------------


def apply_radio_policies(base: StationsConfig, data: Mapping[str, Any]) -> None:
    """Put the user's rules for each class of radio into effect: the stations file's
    [radio.policy], then the page's. A page entry that doesn't check out is logged and skipped."""
    from wavescribe.radio.bandplan import apply_policies, check_policies

    page = (data.get("radio") or {}).get("policy") or {}
    try:
        page = check_policies(page, "config/ui.toml [radio.policy]")
    except ValueError as e:
        log.warning("ignoring the page's radio rules: %s", e)
        page = {}
    apply_policies(dict(base.radio_policies), page)


def effective_config(
    base: StationsConfig, data: Mapping[str, Any]
) -> tuple[StationsConfig, dict[str, str]]:
    """``base`` plus the page's devices, overrides and added stations. Entries the page broke
    (say, a device that no longer covers a station) are reported, not fatal: the station keeps
    its base definition, or an added one is left out. A station whose frequency the page's
    region moves into a class that needs confirming is reported too, and switched off."""
    errors: dict[str, str] = {}
    devices: dict[str, SdrDevice] = {}
    region = (data.get("radio") or {}).get("region") or base.radio_region
    dev_over = data.get("device_overrides", {})
    for d in base.devices:
        devices[d.id] = d
    for raw in data.get("device", []):
        try:
            d = build_device(raw, f"page device {raw.get('id')!r}")
        except ConfigError as e:
            errors[f"device:{raw.get('id')}"] = str(e)
            continue
        if d.id in devices:
            errors[f"device:{d.id}"] = f"device id {d.id!r} already exists"
            continue
        devices[d.id] = d
    for device_id, over in dev_over.items():
        d = devices.get(device_id)
        if d is None:
            continue
        raw = {**device_dict(d), **{k: v for k, v in over.items() if k in DEVICE_OVERRIDE_KEYS}}
        try:
            devices[device_id] = build_device(raw, f"device {device_id!r} (page change)")
        except ConfigError as e:
            errors[f"device:{device_id}"] = str(e)

    overrides = data.get("overrides", {})
    stations: list[Station] = []
    seen: set[str] = set()
    for s in base.stations:
        raw = station_dict(s)
        over = {k: v for k, v in overrides.get(s.id, {}).items() if k in OVERRIDE_KEYS}
        stations.append(_build(raw, over, devices, errors, s, region))
        seen.add(s.id)
    for raw in data.get("station", []):
        sid = raw.get("id")
        if sid in seen:
            errors[str(sid)] = f"station id {sid!r} already exists"
            continue
        merged = {**base.defaults, **raw}
        over = {k: v for k, v in overrides.get(sid, {}).items() if k in OVERRIDE_KEYS}
        station = _build(merged, over, devices, errors, None, region)
        if station is not None:
            stations.append(station)
            seen.add(station.id)
    classes = data.get("remote_privacy", {})
    stations = [_remote_class(s, classes[s.id]) if s.remote and s.id in classes else s
                for s in stations]  # fmt: skip
    config = replace(base, stations=tuple(stations), devices=tuple(devices.values()),
                     radio_region=region)  # fmt: skip
    return config, errors


def _remote_class(station: Station, said: object) -> Station:
    """A station heard through another machine has the class that machine gave it, which the
    app notes in [remote_privacy] as events arrive. One nobody gave a class stays broadcast
    (``read_utterance`` says why); a class that isn't one is handled as private. Never less
    strict than the station's own ``privacy``."""
    from wavescribe.radio.bandplan import PRIVACY, STRICTNESS

    cls = said if said in PRIVACY else "private"
    return replace(station, privacy=max(cls, station.privacy_set or cls, key=STRICTNESS.index))


def _build(
    raw: dict[str, Any],
    over: dict[str, Any],
    devices: Mapping[str, SdrDevice],
    errors: dict[str, str],
    fallback: Station | None,
    region: str | None = None,
) -> Station | None:
    sid = str(raw.get("id"))
    merged = {**raw, **over}
    # Page-only choices: in a page station's own entry, or in a file station's overrides.
    extra = {k: merged.pop(k) for k in ("asr_model", "mt_model", "disabled") if k in merged}
    try:
        station = build_station(merged, f"station {sid!r}", devices, region)
    except ConfigError as e:
        errors[sid] = str(e)
        if fallback is None:
            return None
        station = fallback  # the base definition still works
        if fallback.sdr is not None:
            # Unless a region chosen on the page puts its frequency in a class that wants a
            # confirmation the file never gave. It can't go on running as the class it was
            # made in: it stays listed, switched off, with what to do about it.
            where, confirmed = f"station {sid!r}", raw.get("acknowledge", False)
            try:
                radio_policy(fallback.sdr.freq_hz, fallback.sdr.mode, region, confirmed, where)
            except ConfigError as unconfirmed:
                errors[sid] = str(unconfirmed)
                return replace(fallback, disabled=True)
    return replace(
        station,
        asr_model=extra.get("asr_model") or None,
        mt_model=extra.get("mt_model") or None,
        disabled=bool(extra.get("disabled", station.disabled)),
    )


def station_dict(s: Station) -> dict[str, Any]:
    """A station back as the TOML keys it came from (for overlays and the page)."""
    d: dict[str, Any] = {
        "id": s.id,
        "name": s.name,
        "language": s.language,
        "translate_to": list(s.translate_to),
        "alert_languages": list(s.alert_languages),
        "priority": s.priority,
    }
    if not s.transcribe:
        d["transcribe"] = False
    if not s.translate:
        d["translate"] = False
    if s.stream:
        d["stream"] = s.stream
    if s.stream_hls:
        d["stream_hls"] = s.stream_hls
    if s.fm_mhz:
        d["fm_mhz"] = list(s.fm_mhz)
    if s.mw_khz is not None:
        d["mw_khz"] = s.mw_khz
    if s.mhz:
        d["mhz"] = list(s.mhz)
    if s.sdr is not None:
        d["device"] = s.sdr.device.id
        d["deemphasis_us"] = s.sdr.deemphasis_us
        if s.sdr.mode != "wfm":
            d["mode"] = s.sdr.mode
        if s.sdr.channel_hz:
            d["channel_khz"] = s.sdr.channel_hz / 1e3
        if s.sdr.squelch_db is not None:
            d["squelch_db"] = s.sdr.squelch_db
        if s.privacy in ("utility", "private", "digital"):
            d["acknowledge"] = s.privacy  # it was, for this class, when the station was made
        if s.privacy_region is not None:
            d["privacy_floor"] = s.privacy  # kept stricter after a change of region
    if s.remote is not None:
        d["remote"] = s.remote
        d["remote_station"] = s.remote_station
        if s.remote_token:
            d["remote_token"] = s.remote_token
        if s.privacy_set:
            d["privacy"] = s.privacy_set
    return d


def device_dict(d: SdrDevice) -> dict[str, Any]:
    return {
        "id": d.id,
        "serial": d.serial,
        "center_mhz": d.center_hz / 1e6,
        "gain_db": d.gain_db,
        "sample_rate": d.sample_rate,
        "ppm": d.ppm,
    }


# -- a small TOML writer (tomllib only reads) --------------------------------------------------

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def dumps(data: Mapping[str, Any]) -> str:
    """TOML for nested dicts and lists of dicts with simple values. Lists of dicts become arrays
    of tables at the top level and inline tables deeper down; ``None`` values are skipped."""
    lines: list[str] = []
    _table(data, [], lines)
    return "\n".join(lines).strip() + "\n"


def _table(data: Mapping[str, Any], path: list[str], lines: list[str]) -> None:
    scalars = {k: v for k, v in data.items() if not _is_table(v) and not _is_table_list(v)}
    tables = {k: v for k, v in data.items() if _is_table(v)}
    arrays = {k: v for k, v in data.items() if _is_table_list(v)}
    for k, v in scalars.items():
        if v is not None:
            lines.append(f"{_key(k)} = {_value(v)}")
    for k, v in tables.items():
        sub = [*path, k]
        if any(not _is_table(x) for x in v.values()):  # it has keys of its own
            lines += ["", f"[{'.'.join(_key(p) for p in sub)}]"]
        _table(v, sub, lines)
    for k, items in arrays.items():
        for item in items:
            lines += ["", f"[[{'.'.join(_key(p) for p in [*path, k])}]]"]
            for ik, iv in item.items():
                if iv is not None:
                    lines.append(f"{_key(ik)} = {_value(iv)}")


def _is_table(v: Any) -> bool:
    return isinstance(v, Mapping)


def _is_table_list(v: Any) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, Mapping) for x in v)


def _key(k: str) -> str:
    return k if _BARE_KEY.fullmatch(k) else _string(k)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"TOML can't hold {v}")
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, Mapping):
        return (
            "{ "
            + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items() if x is not None)
            + " }"
        )
    if isinstance(v, list | tuple):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def _string(s: str) -> str:
    """A TOML basic string: the quote, the backslash and every control character escaped
    (U+0000 to U+001F and U+007F; TOML allows a bare tab, the others it refuses)."""

    def escaped(c: str) -> str:
        if c in _ESCAPES:
            return _ESCAPES[c]
        return f"\\u{ord(c):04x}" if c < " " or c == "\x7f" else c

    return '"' + "".join(map(escaped, s)) + '"'
