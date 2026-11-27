# SPDX-License-Identifier: Apache-2.0
"""The page's radio parts (``wavescribe serve``: Find → On the air, Listen → On the air here): scan
a band on an idle receiver in the background, keep what was found in the catalogue, and turn a
frequency into a station that is transcribed like any other.

Scans need the receiver to themselves (a dongle opens in one process at a time). A receiver
with running stations or a spectrum monitor is refused with what holds it, unless the page asks
to pause them: they stop for the scan and start again after it.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any

from wavescribe.config import ConfigError, radio_policy
from wavescribe.dsp import OFF_CENTRE_HZ, check_tunable, frequency_hz, in_band, off_centre
from wavescribe.radio import holders
from wavescribe.radio import scan as scanner
from wavescribe.radio.bandplan import (
    AUTO,
    DEFAULT_POLICIES,
    POLICIES,
    REGIONS,
    check_policies,
    default_region,
    lookup,
    mhz_text,
    region_bands,
)
from wavescribe.radio.catalog import Catalog
from wavescribe.radio.words import joined, receiver_name, sentence, them

if TYPE_CHECKING:
    from wavescribe.app import App

HELD_TTL_S = 3.0  # how long a reading of which programs hold the dongles is reused


class RadioError(ValueError):
    """Something the page asked for can't be done; the message says why."""

    def __init__(self, message: str, code: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.extra = extra


class RadioService:
    def __init__(self, app: App, catalog: Catalog | None = None) -> None:
        self.app = app
        self.catalog = catalog or Catalog()
        self._lock = threading.Lock()
        self._job: dict[str, Any] | None = None
        self._cancel: threading.Event | None = None
        self._held: tuple[float, dict[str, Any]] | None = None

    # -- what the panel shows --------------------------------------------------------------

    def region(self) -> tuple[str, str]:
        page = (self.app.settings.get("radio") or {}).get("region")
        if page:
            return page, "page"
        if self.app.base.radio_region:
            return self.app.base.radio_region, "config"
        return AUTO, "locale"

    def receivers(self) -> list[dict[str, Any]]:
        from wavescribe.admin import _monitors

        running = set(self.app.running())
        out = []
        for d in self.app.config.devices:
            on = [s for s in self.app.config.stations if s.sdr and s.sdr.device.id == d.id]
            busy = [s.name for s in on if s.id in running]
            if d.id in _monitors:
                busy.append("the spectrum monitor")
            live = getattr(self.app, "_live", None)
            if live is not None and live.is_open(d.id):
                busy.append("the live receiver")
            job = self._job
            scanning = bool(job and job["device"] == d.id and job["phase"] not in ("done", "error"))
            out.append({
                "id": d.id, "serial": d.serial, "gain_db": d.gain_db,
                "center_mhz": d.center_hz / 1e6, "span_mhz": d.sample_rate / 1e6,
                "busy": busy, "scanning": scanning,
                "stations": [{"id": s.id, "name": s.name, "mhz": s.sdr.freq_hz / 1e6,
                              "running": s.id in running} for s in on if s.sdr],
            })  # fmt: skip
        return out

    def view(self, dongles: bool = False) -> dict[str, Any]:
        region, source = self.region()
        held = self.held_elsewhere()
        by_freq: dict[int, str] = {}
        for s in self.app.config.stations:
            if s.sdr is not None:
                by_freq[s.sdr.freq_hz] = s.id
        heard = []
        for e in self.catalog.entries():
            # a station made from this frequency, within a channel's width
            sid = next((v for f, v in by_freq.items() if abs(f - e["freq_hz"]) <= 6_000), None)
            heard.append({**e, "station": sid})
        return {
            # Not chosen: the list shows the locale's region; the strictest class applies.
            "region": default_region() if region == AUTO else region, "region_source": source,
            "regions": list(REGIONS),
            "bands": [b.to_dict() for b in region_bands(region)],
            "receivers": [{**r, "held_elsewhere": held.get(r["serial"])}
                          for r in self.receivers()],
            "spurs": self.catalog.spur_info(),
            "heard": heard,
            "scan": self.scan_status(),
            "last_scan": self.last_scan(),
            "policies": {k: {**vars(v), "default": vars(DEFAULT_POLICIES[k]),
                             "changed": sorted(self._policy_changes().get(k, {}))}
                         for k, v in POLICIES.items()},
            "defaults": {"language": self.app.base.defaults.get("language"),
                         "translate_to": list(self.app.base.defaults.get("translate_to", ()))},
            "tools": {"rtl_power": _which("rtl_power"), "rtl_sdr": _which("rtl_sdr")},
            **({"dongles": [{**d, "held_elsewhere": held.get(d["serial"])}
                            for d in self.new_dongles()]}
               if dongles or not self.app.config.devices else {}),
        }  # fmt: skip

    def last_scan(self) -> dict[str, Any] | None:
        """The last saved scan. One saved before scans named their receiver gets the id of the
        receiver with its serial, if there still is one."""
        scan = self.catalog.last_scan()
        if scan is not None and not scan.get("device"):
            serial = scan.get("serial")
            scan["device"] = next((d.id for d in self.app.config.devices if d.serial == serial),
                                  None)  # fmt: skip
        return scan

    def held_elsewhere(self) -> dict[str, Any]:
        """Dongles another program holds, by serial (radio/holders.py: macOS's USB registry,
        nothing opened); read again at most every few seconds while the page polls."""
        now = time.monotonic()
        if self._held is None or now - self._held[0] > HELD_TTL_S:
            self._held = (now, holders.held_elsewhere())
        return self._held[1]

    def new_dongles(self) -> list[dict[str, str]]:
        """Dongles on USB that no receiver uses yet (listing them opens none)."""
        from wavescribe.sources.sdr import list_dongles

        known = {d.serial for d in self.app.config.devices}
        return [d for d in list_dongles() if d["serial"] not in known]

    # -- what is kept of each class of radio (the user decides; docs/RADIO.md) --------------

    def _policy_changes(self) -> dict[str, dict[str, Any]]:
        """The file's [radio.policy] with the page's over it: what differs from the defaults."""
        out: dict[str, dict[str, Any]] = {}
        page = (self.app.settings.get("radio") or {}).get("policy") or {}
        for layer in (self.app.base.radio_policies, page):
            for cls, changes in layer.items():
                out.setdefault(cls, {}).update(changes)
        return out

    def set_policy(self, cls: str, changes: dict[str, Any]) -> None:
        from wavescribe.settings import apply_radio_policies

        try:
            check_policies({cls: changes}, "radio rules")
        except ValueError as e:
            raise RadioError(str(e)) from e

        def mutate(d: dict[str, Any]) -> None:
            d.setdefault("radio", {}).setdefault("policy", {}).setdefault(cls, {}).update(changes)

        self.app.settings.update(mutate)
        apply_radio_policies(self.app.base, self.app.settings.snapshot())
        self._prune_soon()

    def _prune_soon(self) -> None:
        """A shorter time for a class takes effect within a minute, not at the next scheduled
        prune."""
        if self.app.store is not None:
            self.app.store.prune_soon()

    def reset_policies(self) -> None:
        """Back to the defaults (and the stations file's [radio.policy], if it has one)."""
        from wavescribe.settings import apply_radio_policies

        self.app.settings.update(lambda d: (d.get("radio") or {}).pop("policy", None))
        apply_radio_policies(self.app.base, self.app.settings.snapshot())
        self._prune_soon()

    def set_region(self, region: str | None) -> None:
        """Choose the band plan. A station the new region would put in a less strict class
        keeps the one it has (``privacy_floor``) until the user confirms the new class on the
        page, with the same acknowledgement as tuning it: a region alone never loosens."""
        if region is not None and region not in REGIONS:
            raise RadioError(f"There is no region {region!r}: choose {', '.join(REGIONS)}.")
        from wavescribe.config import ConfigError, radio_policy
        from wavescribe.radio.bandplan import stricter

        floors = {}
        for s in self.app.config.stations:
            if s.sdr is None:
                continue
            try:
                hz, width = s.sdr.policed(region)  # a watch: its strictest channel
                new, _ = radio_policy(hz, s.sdr.mode, region, True, s.id, width)
            except ConfigError:
                continue
            if stricter(s.privacy, new):
                floors[s.id] = s.privacy
        page = {e.get("id") for e in self.app.settings.get("station") or []}

        def mutate(d: dict[str, Any]) -> None:
            radio = d.setdefault("radio", {})
            if region:
                radio["region"] = region
            else:
                radio.pop("region", None)
            for sid, cls in floors.items():
                if sid in page:
                    entry = next(e for e in d["station"] if e.get("id") == sid)
                else:
                    entry = d.setdefault("overrides", {}).setdefault(sid, {})
                entry["privacy_floor"] = cls

        self.app.settings.update(mutate)
        self.app._reload()

    # -- scanning ---------------------------------------------------------------------------

    def _device(self, device_id: str) -> Any:
        d = next((x for x in self.app.config.devices if x.id == device_id), None)
        if d is None:
            raise RadioError(f"There is no receiver {device_id!r}.")
        return d

    def start_scan(self, raw: dict[str, Any]) -> dict[str, Any]:
        d = self._device(str(raw.get("device") or ""))
        rx = next(r for r in self.receivers() if r["id"] == d.id)
        if rx["busy"] and not raw.get("pause"):
            raise RadioError(
                f"Receiver {receiver_name(d.id)} is running {joined(rx['busy'])}. Pause "
                f"{them(rx['busy'])} for the scan, or scan with another receiver.", "busy",
                holders=rx["busy"],
            )  # fmt: skip
        region, _ = self.region()
        band = raw.get("band")
        try:
            if band == "all":
                start, stop = scanner.SWEEP_LIMITS_HZ
                what = "the whole tuner range"
            elif band:
                start, stop = scanner.band_range(str(band), region)
                what = next(b.name for b in region_bands(region) if b.id == band)
            else:
                start = frequency_hz(raw["start_mhz"])
                stop = frequency_hz(raw["stop_mhz"])
                check_tunable(start)
                check_tunable(stop)
                what = f"{start / 1e6:g} to {stop / 1e6:g} MHz"
        except (KeyError, TypeError, ValueError, scanner.ScanError) as e:
            raise RadioError(sentence(f"what to scan: {e}")) from e
        gain = float(raw.get("gain_db") or d.gain_db)
        rds = bool(raw.get("rds", True))
        watch_s = max(0.0, min(3600.0, float(raw.get("watch_min") or 0) * 60))
        learn = bool(raw.get("learn_spurs"))
        with self._lock:
            if self._job and self._job["phase"] not in ("done", "error"):
                other = receiver_name(self._job["device"])
                raise RadioError(f"Receiver {other} is already scanning: one scan runs at a "
                                 "time. Wait for it, or stop it.", "scanning")  # fmt: skip
            paused = self._pause(d, rx, take_live=True) if rx["busy"] else None
            cancel = threading.Event()
            self._cancel = cancel
            self._job = {
                "device": d.id, "serial": d.serial, "band": band, "what": what,
                "start_mhz": start / 1e6, "stop_mhz": stop / 1e6, "gain_db": gain,
                "phase": "starting", "done": 0, "total": 1, "started": time.time(),
                "signals": [], "spectrum": None, "error": None, "watch_s": watch_s,
                "learn_spurs": learn, "paused": rx["busy"] if paused else [],
            }  # fmt: skip
        args = (d.serial, start, stop, region, gain, d.ppm, rds, watch_s, learn, cancel, paused)
        threading.Thread(target=self._run, args=args, daemon=True, name="radio-scan").start()
        return self.scan_status()  # type: ignore[return-value]

    def _pause(self, d: Any, rx: dict[str, Any], *, take_live: bool = False) -> dict[str, Any]:
        """Stop what holds a receiver, for a scan; ``_resume`` starts it again. ``take_live``
        also ends listening on the Listen tab, and what that had paused waits for the scan."""
        from wavescribe.admin import _monitors, monitor
        from wavescribe.sources import sdr

        carried: dict[str, Any] = {}
        live = getattr(self.app, "_live", None)
        if take_live and live is not None and live.is_open(d.id):
            carried = live.close(d.id, resume=False) or {}
            rx = next(r for r in self.receivers() if r["id"] == d.id)
        stations = [s["id"] for s in rx["stations"] if s["running"]]
        watching = d.id in _monitors
        for sid in stations:
            self.app.pipeline.remove_station(sid, wait=10)
        if watching:
            monitor(self.app, d.id, False)
        deadline = time.time() + 5
        while not sdr.forget_device(d.serial) and time.time() < deadline:
            time.sleep(0.1)
        return {"device": d.id, "stations": sorted({*stations, *carried.get("stations", [])}),
                "monitor": watching or bool(carried.get("monitor"))}  # fmt: skip

    def _resume(self, paused: dict[str, Any] | None) -> None:
        from wavescribe.admin import AdminError, monitor

        if not paused:
            return
        here = {s.id for s in self.app.config.stations}
        running = set(self.app.running())
        for sid in paused["stations"]:
            if sid in here and sid not in running:  # as they ran: listen-only stays so
                self.app.pipeline.add_station(self.app._runtime(self.app.config.get(sid)))
        if paused["monitor"]:
            with contextlib.suppress(AdminError):
                monitor(self.app, paused["device"], True)

    def _run(self, serial: str, start: int, stop: int, region: str, gain: float, ppm: int,
             rds: bool, watch_s: float, learn: bool, cancel: threading.Event,
             paused: dict[str, Any] | None = None) -> None:  # fmt: skip
        job = self._job
        assert job is not None

        def progress(p: scanner.Progress) -> None:
            job.update(phase=p.phase, done=p.done, total=p.total, note=p.note)
            if p.signals:
                job["signals"] = [s.to_dict() for s in p.signals]

        try:
            if learn:
                sw, freqs = scanner.learn_spurs(serial, start, stop, gain_db=gain, ppm=ppm,
                                                on_progress=progress, cancel=cancel)  # fmt: skip
                self.catalog.save_spurs(serial, freqs, start, stop)
                job.update(phase="done", spurs=len(freqs), spectrum=sw.downsampled(),
                           seconds=round(time.time() - job["started"]))  # fmt: skip
                return
            sw, signals = scanner.scan(serial, start, stop, region, gain_db=gain, ppm=ppm, rds=rds,
                                       watch_s=watch_s, spurs=self.catalog.spurs(serial),
                                       on_progress=progress, cancel=cancel)  # fmt: skip
            self.catalog.record(signals, serial=serial, start_hz=start, stop_hz=stop, sweep=sw,
                                device=job["device"], what=job["what"])  # fmt: skip
            job.update(phase="done", signals=[s.to_dict() for s in signals],
                       spectrum=sw.downsampled(),
                       seconds=round(time.time() - job["started"]))  # fmt: skip
        except scanner.Cancelled:
            job.update(phase="error", error="cancelled")
        except Exception as e:
            job.update(phase="error", error=str(e))
        finally:
            self._resume(paused)

    def scan_status(self) -> dict[str, Any] | None:
        return dict(self._job) if self._job else None

    def cancel_scan(self) -> None:
        if self._cancel is not None:
            self._cancel.set()

    def adopt_rds_names(self) -> list[str]:
        """FM stations made from a bare frequency ("98.6 MHz") take the name the station sends
        about itself, once RDS has it (seen twice). Runs every few seconds with the page."""
        import re

        status = self.app.pipeline.status()["stations"]
        renamed = []
        for raw in self.app.settings.get("station") or []:
            sid, name = raw.get("id"), str(raw.get("name") or "")
            if not raw.get("fm_mhz") or not re.fullmatch(r"\d+(\.\d+)? MHz", name):
                continue
            ps = ((status.get(sid) or {}).get("rds") or {}).get("ps")
            if ps and ps.strip():
                self.app.update_station(sid, {"name": ps.strip()})
                renamed.append(sid)
        return renamed

    # -- tuning: a frequency becomes a station ----------------------------------------------

    def tune(self, raw: dict[str, Any]) -> dict[str, Any]:
        d = self._device(str(raw.get("device") or ""))
        region, _ = self.region()
        try:
            freq = frequency_hz(raw.get("mhz"))
            check_tunable(freq)
        except ValueError as e:
            raise RadioError(sentence(str(e))) from e
        mhz = freq / 1e6
        band = lookup(freq, region)
        mode = raw.get("mode") or (band.mode if band and band.mode else "nfm")
        ack = bool(raw.get("acknowledge"))
        try:
            privacy, band_id = radio_policy(freq, mode, region, ack, f"{mhz_text(mhz)} MHz")
        except ConfigError as e:
            needs = "acknowledge" in str(e)
            said = str(e).split(": ", 1)[-1].replace(" Set acknowledge = true to confirm",
                                                     " Confirm")  # fmt: skip
            raise RadioError(sentence(said), "acknowledge" if needs else "refused",
                             privacy=band.privacy if band else "private") from e  # fmt: skip
        job = self._job
        if job and job["device"] == d.id and job["phase"] not in ("done", "error"):
            raise RadioError(f"Receiver {receiver_name(d.id)} is scanning: wait for it, or stop "
                             "the scan.", "scanning")  # fmt: skip
        language = raw.get("language") or self.app.base.defaults.get("language")
        if not language:
            raise RadioError("Which language is spoken there?", "language")

        if not in_band(freq, d.center_hz, d.sample_rate, mode):
            self._retune(d, freq, mode, bool(raw.get("retune")))
        elif not off_centre(freq, d.center_hz, mode):
            self._retune(d, freq, mode, bool(raw.get("retune")), on_centre=True)
        entry = next((e for e in self.catalog.entries() if abs(e["freq_hz"] - freq) <= 6_000), {})
        name = (raw.get("name") or entry.get("user_name") or entry.get("name")
                or (band.label(freq) if band and band.privacy != "broadcast" else None)
                or f"{mhz_text(mhz)} MHz")  # fmt: skip
        targets = raw.get("translate_to") or list(self.app.base.defaults.get("translate_to", ()))
        station: dict[str, Any] = {
            "name": name, "language": language, "device": d.id, "mhz": [mhz],
            "translate_to": targets,
        }  # fmt: skip
        if mode == "wfm":
            station["fm_mhz"] = [mhz]
            del station["mhz"]
        else:
            station["mode"] = mode
            if band is not None and band.channel_hz and band.privacy != "broadcast":
                station["channel_khz"] = band.channel_hz / 1e3  # marine, amateur FM in the Americas
        if raw.get("squelch_db") is not None:
            station["squelch_db"] = float(raw["squelch_db"])
        for key in ("transcribe", "translate"):  # listen only, or not translated
            if raw.get(key) is False:
                station[key] = False
        if POLICIES.get(privacy) and POLICIES[privacy].needs_ack:
            station["acknowledge"] = privacy  # the class confirmed: another one asks again
        try:
            view = self.app.add_station(station, start=True)
        except Exception as e:
            raise RadioError(str(e)) from e
        return {**view, "privacy": privacy, "band": band_id}

    def _retune(self, d: Any, freq: int, mode: str, force: bool, on_centre: bool = False) -> None:
        """Move an idle receiver so ``freq`` fits, keeping it off the centre (DC) bin."""
        from wavescribe.admin import AdminError, update_device

        rx = next(r for r in self.receivers() if r["id"] == d.id)
        name = receiver_name(d.id)
        if rx["scanning"]:
            raise RadioError(f"Receiver {name} is scanning: wait for it, or stop the scan.",
                             "scanning")  # fmt: skip
        if rx["busy"] and not force:
            where = (
                f"is the very centre of what receiver {name} hears now, where the dongle's own "
                "spike would hold the squelch open"
                if on_centre
                else f"is outside what receiver {name} hears now "
                f"({d.center_hz / 1e6:.2f} MHz ± {d.sample_rate / 2e6 - 0.12:.2f})"
            )
            raise RadioError(
                f"{mhz_text(freq / 1e6)} MHz {where}. Retuning the receiver stops "
                f"{joined(rx['busy'])}.", "retune", holders=rx["busy"],
            )  # fmt: skip
        offset = OFF_CENTRE_HZ[mode]
        if force:
            for s in rx["stations"]:
                if s["running"]:
                    self.app.stop_station(s["id"])
        try:
            update_device(self.app, d.id, {"center_mhz": (freq - offset) / 1e6})
        except AdminError as e:
            raise RadioError(str(e)) from e


def _which(cmd: str) -> bool:
    import shutil

    return shutil.which(cmd) is not None
