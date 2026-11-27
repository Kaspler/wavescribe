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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from wavescribe.config import ConfigError, radio_policy
from wavescribe.dsp import OFF_CENTRE_HZ, check_tunable, frequency_hz, margin_hz
from wavescribe.radio import holders
from wavescribe.radio import scan as scanner
from wavescribe.radio.bandplan import (
    AUTO,
    DEFAULT_POLICIES,
    POLICIES,
    REGIONS,
    TUNER_RANGE_HZ,
    check_policies,
    default_region,
    lookup,
    mhz_text,
    region_band,
    region_bands,
)
from wavescribe.radio.catalog import Catalog
from wavescribe.radio.words import joined, masked_serial, receiver_name, sentence, them
from wavescribe.refusal import Refusal

if TYPE_CHECKING:
    from wavescribe.app import App

HELD_TTL_S = 3.0  # how long a reading of which programs hold the dongles is reused
# The country a band plan region is, where it is one (``view``'s ``country``); the others are
# many countries' (the CEPT countries, ITU Regions 1 to 3).
REGION_COUNTRY = {"jp": "JP"}


class RadioError(Refusal):
    """Something the page asked for can't be done; the message says why, and ``code`` (with
    the fields in ``extra``) what the page needs to ask or show (wavescribe/refusal.py)."""


def _coded(e: Exception) -> RadioError:
    """A refusal from below as the page is answered: the sentence, with its code if it has one."""
    return RadioError(sentence(str(e)), getattr(e, "code", None), **getattr(e, "extra", {}))


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
            by_hand = live is not None and live.is_open(d.id)
            if by_hand:
                busy.append("the live receiver")
            job = self._job
            scanning = bool(job and job["device"] == d.id and job["phase"] not in ("done", "error"))
            watching = any(s.id in running and s.sdr and s.sdr.watch for s in on)
            # What holds it, as one word for the page (``busy`` names it; ``view`` adds another
            # program's hold): a scan or the hand tuner has the dongle to itself, a watch's
            # receiver is watching even with stations of its own beside it.
            code = ("scan" if scanning else "live" if by_hand else "watch" if watching
                    else "station" if any(s.id in running for s in on)
                    else "monitor" if d.id in _monitors else None)  # fmt: skip
            out.append({
                "id": d.id, "serial": d.serial, "gain_db": d.gain_db,
                "center_mhz": d.center_hz / 1e6, "span_mhz": d.sample_rate / 1e6,
                "busy": busy, "busy_code": code, "scanning": scanning,
                "stations": [{"id": s.id, "name": s.name, "mhz": s.sdr.freq_hz / 1e6,
                              "running": s.id in running, "out_of_reach": s.out_of_reach}
                             for s in on if s.sdr],
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
            # Regions are band plans, most of them many countries': only Japan's is one country.
            "country": REGION_COUNTRY.get(region),
            "regions": list(REGIONS),
            "bands": [b.to_dict() for b in region_bands(region)],
            "receivers": [{**r, "held_elsewhere": held.get(r["serial"]),
                           "busy_code": r["busy_code"] or (
                               "other_program" if held.get(r["serial"]) else None)}
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
            raise RadioError(f"There is no receiver {device_id!r}.", "no_receiver")
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
                what = region_band(str(band), region).name  # band_range found it
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
                # The receiver by its id, and its dongle by the serial's last digits only: the
                # job's words can be pasted anywhere (the full serial is in ``receivers``).
                "device": d.id, "serial": masked_serial(d.serial), "band": band, "what": what,
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
            if sid not in here or sid in running:
                continue
            if sid in self.app.unavailable:  # switched off, or no longer heard: it stays off
                self.app.stop_station(sid)
            else:  # as they ran: listen-only stays so
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
        """Make a frequency a station on a receiver, and start it, in one call.

        The receiver stays where it is if it hears the frequency there. If not, it moves, to
        where it hears the new station and every station it has now (switched-off ones and
        those already out of reach aside). Where no place hears them all, nothing changes and
        the answer is the question (``retune_question``): which stations would be left out,
        and which of those stop. ``retune: true`` is the yes. A receiver tuned by hand (Find's
        Look here, Listen's Tune by hand) is let go, and what it paused starts again where it
        is still heard. The answer is the station as the page lists it, with where the
        receiver is (``receiver``), what was left out (``unfit``), whether the hand tuner was
        let go (``live_closed``), and ``start_error`` if it was made but couldn't start."""
        d = self._device(str(raw.get("device") or ""))
        region, _ = self.region()
        try:
            freq = frequency_hz(raw.get("mhz"))
        except ValueError as e:
            raise RadioError(sentence(str(e)), "bad_frequency") from e
        try:
            check_tunable(freq)
        except ValueError as e:
            raise RadioError(sentence(str(e)), "out_of_band") from e
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

        entry = next((e for e in self.catalog.entries() if abs(e["freq_hz"] - freq) <= 6_000), {})
        name = (raw.get("name") or entry.get("user_name") or entry.get("name")
                or (band.label(freq) if band and band.privacy != "broadcast" else None)
                or f"{mhz_text(mhz)} MHz")  # fmt: skip
        targets = raw.get("translate_to") or list(self.app.base.defaults.get("translate_to", ()))
        station: dict[str, Any] = {
            "name": name, "language": language, "device": d.id, "mhz": [mhz],
            "translate_to": targets,
        }  # fmt: skip
        if language == "auto" and raw.get("languages"):  # the ones it may be (asr/language.py)
            station["languages"] = list(raw["languages"])
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
        channel = station.get("channel_khz")
        channel_hz = channel and channel * 1e3
        extra, retune = {"privacy": privacy, "band": band_id}, bool(raw.get("retune"))

        def place(hand: int | None, running: set[str]) -> tuple[int, list[Any]]:
            return self._centre_for(d, freq, mode, channel_hz, hand, running)

        return self._add_on(d, station, place, retune, extra, freq_hz=freq)

    def add_station(self, raw: dict[str, Any], *, start: bool = False,
                    retune: bool = False) -> dict[str, Any]:  # fmt: skip
        """A station from the page (``POST /api/manage/stations``). A radio station or watch its
        receiver doesn't hear where it is tuned now moves the receiver as ``tune`` does: to
        where it hears the new station and every station it has now, or, where no place hears
        them all, nothing changes and the answer is the question (``retune_question``) until
        the same request comes with ``retune: true``. The answer is the station as the page
        lists it; after a move also ``receiver``, ``unfit``, ``live_closed`` and
        ``start_error``, as ``tune`` gives them."""
        entry = self.app.station_entry(raw)

        def add(draft: dict[str, Any]) -> None:
            draft.setdefault("station", []).append(entry)

        station = self.app._check_draft(add, str(entry["id"]), reach=False)
        if station.sdr is None or not station.out_of_reach or station.disabled:
            return self.app.add_station(entry, start=start)
        d = station.sdr.device
        job = self._job
        if job and job["device"] == d.id and job["phase"] not in ("done", "error"):
            raise RadioError(f"Receiver {receiver_name(d.id)} is scanning: wait for it, or stop "
                             "the scan.", "scanning")  # fmt: skip

        def place(hand: int | None, running: set[str]) -> tuple[int, list[Any]]:
            return self._centre_for_station(station, hand, running)

        return self._add_on(d, entry, place, retune, {}, heard=station.name, start=start)

    def _add_on(self, d: Any, station: dict[str, Any],
                place: Callable[[int | None, set[str]], tuple[int, list[Any]]], retune: bool,
                extra: dict[str, Any], *, freq_hz: int | None = None, heard: str | None = None,
                start: bool = True) -> dict[str, Any]:  # fmt: skip
        """``tune``'s second half, and a page station's whose receiver has to move: where the
        receiver goes (``place``, given where the hand tuner has it and the stations that run:
        the centre, and the stations it would leave out), then the station made and, with
        ``start``, started. ``freq_hz`` or ``heard`` is what the question says it moves for."""
        from wavescribe.admin import AdminError, update_device

        live = getattr(self.app, "_live", None)
        by_hand = live is not None and live.is_open(d.id)
        running = self._running_on(d, live)
        hand = live.centre(d.id) if by_hand else None
        centre, unfit = place(hand, running)
        if unfit and not retune:
            raise retune_question(d.id, centre, unfit, running, freq_hz=freq_hz, heard=heard)
        from wavescribe.app import slug

        # Nothing changes until the station is known to be one that can be made there.
        station.setdefault("id", self.app._free_id(slug(str(station["name"]))))
        try:
            self.app._check_name_free(str(station["name"]))
            self.app._check_draft(lambda draft: self._draft(draft, d, centre, station),
                                  station["id"])  # fmt: skip
        except ValueError as e:
            raise _coded(e) from e
        carried = live.close(d.id, resume=False) if by_hand else None
        try:
            if centre != d.center_hz:
                update_device(self.app, d.id, {"center_mhz": centre / 1e6}, retune=True)
            view = self.app.add_station(station)
        except (AdminError, ValueError) as e:
            raise _coded(e) from e
        finally:
            self._resume(carried)  # what the hand tuner paused, where it is still heard
        start_error = None
        try:
            if start:
                self.app.start_station(view["id"])
        except ValueError as e:  # made, and listed: the page says why it didn't start
            start_error = sentence(str(e))
        view = next(v for v in self.app.stations_view() if v["id"] == view["id"])
        return {
            **view, **extra, "start_error": start_error, "live_closed": by_hand,
            "receiver": {"id": d.id, "center_mhz": centre / 1e6, "moved": centre != d.center_hz},
            "unfit": [{"id": s.id, "name": s.name, "running": s.id in running} for s in unfit],
        }  # fmt: skip

    @staticmethod
    def _draft(draft: dict[str, Any], d: Any, centre: int, station: dict[str, Any]) -> None:
        if centre != d.center_hz:
            over = draft.setdefault("device_overrides", {}).setdefault(d.id, {})
            over["center_mhz"] = centre / 1e6
        kept = {k: v for k, v in station.items() if v not in (None, "", [])}  # as add_station
        draft.setdefault("station", []).append(kept)

    def _running_on(self, d: Any, live: Any) -> set[str]:
        """The stations on a receiver that run, or would but for the hand tuner's pause."""
        running = self.app.running()
        here = {s.id for s in self.app.config.stations if s.sdr and s.sdr.device.id == d.id}
        paused = set(live.paused_ids(d.id)) if live is not None else set()
        return (here & running) | (here & paused)

    def _centre_for(self, d: Any, freq: int, mode: str, channel_hz: float | None,
                    by_hand: int | None, running: set[str]) -> tuple[int, list[Any]]:  # fmt: skip
        """Where to tune receiver ``d`` so it hears a new station on ``freq``, and the stations
        it has now that it wouldn't hear there (none when a place hears them all).

        Places are tried in this order: where it is, where the hand tuner has it, the usual
        place for the new station (a little below it, clear of the centre's spike), then every
        kHz from which it hears the new station, nearest the usual place first. With no place
        for them all, the one that keeps the most running stations, then the most stations."""
        from wavescribe.config import hears

        usual = freq - OFF_CENTRE_HZ[mode]
        reach = int(d.sample_rate / 2 - margin_hz(mode))
        return self._place(d, lambda at: hears(at, freq, mode, channel_hz), usual,
                           range(freq - reach, freq + reach + 1, 1000), by_hand, running,
                           f"{mhz_text(freq / 1e6)} MHz")  # fmt: skip

    def _centre_for_station(self, s: Any, by_hand: int | None,
                            running: set[str]) -> tuple[int, list[Any]]:  # fmt: skip
        """``_centre_for`` for a station as it would be made (``heard_by``): a watch from where
        a watch is tuned (``plan_centre``), then every kHz from which it hears the whole window;
        another station from the usual place for its frequency."""
        from wavescribe.config import heard_by
        from wavescribe.dsp import DEFAULT_CHANNEL_HZ, NARROW_MARGIN_HZ
        from wavescribe.radio.watch import plan_centre

        t = s.sdr
        d = t.device
        if t.watch is not None:
            width = t.channel_hz or DEFAULT_CHANNEL_HZ[t.mode]
            usual = plan_centre(t.watch, width, d.sample_rate)
            reach = int(d.sample_rate / 2 - NARROW_MARGIN_HZ - width / 2)
            grid = range(t.watch.hi_hz - reach, t.watch.lo_hz + reach + 1, 1000)
            what = t.watch.label
        else:
            usual = t.freq_hz - OFF_CENTRE_HZ[t.mode]
            reach = int(d.sample_rate / 2 - margin_hz(t.mode))
            grid = range(t.freq_hz - reach, t.freq_hz + reach + 1, 1000)
            what = f"{mhz_text(t.freq_hz / 1e6)} MHz"
        return self._place(d, lambda at: heard_by(s, at), usual, grid, by_hand, running, what)

    def _place(self, d: Any, hears_it: Callable[[Any], bool], usual: int, grid: range,
               by_hand: int | None, running: set[str],
               what: str) -> tuple[int, list[Any]]:  # fmt: skip
        """The planner of ``_centre_for``: the first place, in its order, from which receiver
        ``d`` hears the new station (``hears_it``) and every station it has; else the best."""
        from dataclasses import replace

        from wavescribe.config import heard_by

        others = [s for s in self.app.config.stations
                  if s.sdr and s.sdr.device.id == d.id and not s.disabled
                  and not s.out_of_reach]  # fmt: skip
        best: tuple[tuple[int, int], int, list[Any]] | None = None
        for c in (d.center_hz, by_hand, usual, *sorted(grid, key=lambda c: abs(c - usual))):
            if c is None or not TUNER_RANGE_HZ[0] <= c <= TUNER_RANGE_HZ[1]:
                continue
            at = replace(d, center_hz=c)
            if not hears_it(at):
                continue
            kept = [s for s in others if heard_by(s, at)]
            if len(kept) == len(others):
                return c, []
            score = (sum(s.id in running for s in kept), len(kept))
            if best is None or score > best[0]:
                best = (score, c, kept)
        if best is None:
            raise RadioError(f"Receiver {receiver_name(d.id)} can't be tuned to hear {what}.",
                             "out_of_band")  # fmt: skip
        _, c, kept = best
        return c, [s for s in others if s not in kept]


def retune_question(device_id: str, centre_hz: int, unfit: list[Any], running: set[str], *,
                    freq_hz: int | None = None,
                    heard: str | None = None) -> RadioError:  # fmt: skip
    """What a retune asks before it leaves stations out (code ``retune``): the stations the
    receiver wouldn't hear from ``centre_hz``, which of them stop now, and that they stay
    listed. The page shows it and sends the same request again with ``retune: true``.
    ``holders`` names the running ones, ``unfit`` all of them, ``center_mhz`` where it goes.
    ``freq_hz`` (a frequency) or ``heard`` (a station's name) is what the receiver moves for."""
    rx, at = receiver_name(device_id), mhz_text(centre_hz / 1e6)
    if centre_hz % 1000:  # between two channels of a watch: said to the kHz, kept exact
        at = f"about {mhz_text(round(centre_hz, -3) / 1e6)}"
    names = [s.name for s in unfit]
    stops = [s.name for s in unfit if s.id in running]
    target = heard or (f"{mhz_text(freq_hz / 1e6)} MHz" if freq_hz is not None else None)
    why = f" to hear {target}" if target else ""  # one sentence, whatever moves it
    said = f"Receiver {rx} moves to {at} MHz{why}, where it no longer hears {joined(names, 'or')}."
    if stops:
        said += f" Retuning it stops {joined(stops)}."
    one = len(names) == 1
    said += (f" {'It stays' if one else 'They stay'} in your stations, unavailable until the "
             f"receiver hears {them(names)} again.")  # fmt: skip
    return RadioError(
        said, "retune", holders=stops, center_mhz=centre_hz / 1e6,
        unfit=[{"id": s.id, "name": s.name, "running": s.id in running} for s in unfit],
    )  # fmt: skip


def _which(cmd: str) -> bool:
    import shutil

    return shutil.which(cmd) is not None
