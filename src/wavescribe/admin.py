# SPDX-License-Identifier: Apache-2.0
"""What the page's Settings (Languages and models, Stations, Receivers) and Find need beyond
``App``: views of the model registry with this machine's fit, downloads, the station and model
catalogues, language detection for a new stream, and the SDR dongles.

Everything that touches the network here runs only when someone clicks for it on the
machine itself (ARCHITECTURE.md, "Network use").
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from wavescribe.config import ConfigError
from wavescribe.registry import (
    CATALOG_MODELS_PATH,
    ModelEntry,
    ModelError,
    ModelRegistry,
    _parse_entry,
)
from wavescribe.settings import device_dict, dumps

if TYPE_CHECKING:
    from wavescribe.app import App

log = logging.getLogger(__name__)

STATION_SNAPSHOT = Path("data/catalog/radio-browser.json.gz")


class AdminError(ValueError):
    pass


# -- models ------------------------------------------------------------------------------------


def models_view(app: App) -> dict[str, Any]:
    from wavescribe.asr.language import detects_languages
    from wavescribe.catalog import models as cat

    reg = app.registry
    m = cat.machine()
    plan = app.plan
    status = app.pipeline.status()["models"]
    used: dict[str, list[str]] = {}
    for sid, (entry, _) in plan.asr.items():
        used.setdefault(entry.id, []).append(sid)
    for label, item in (("translation", plan.mt), ("voice activity", plan.vad),
                        ("music gate", plan.gate)):  # fmt: skip
        if item is not None:
            used.setdefault(item[0].id, []).append(label)
    chains = reg.chains()
    entries = []
    sizes: dict[str, float | None] = {}
    for e in reg.entries():
        path = reg.local_path(e)
        size = e.size_gb or (cat.installed_size_gb(path) if path else None)
        sizes[e.id] = size
        # A speech model removed a moment ago stays in memory until it is let go as idle.
        mt_loaded = status["mt"] is not None and plan.mt is not None and plan.mt[0].id == e.id
        loaded = path is not None and (e.id in status["asr"] or mt_loaded)
        f = cat.fit(e.capability, size, m, backend=e.backend)
        entries.append({
            "id": e.id, "capability": e.capability, "backend": e.backend, "tier": e.tier,
            "license": e.license, "license_url": e.license_url, "languages": list(e.languages),
            "attribution": e.attribution, "notes": e.notes, "tested": e.tested,
            "origin": e.origin, "upstream": e.upstream, "revision": e.revision,
            "gated": e.gated, "unsafe_targets": list(e.unsafe_targets),
            "installed": path is not None, "size_gb": size,
            # Wavescribe downloaded it, so it can remove it (never a model at a path you gave).
            "removable": e.kind != "local",
            **_room(reg, e, path),
            # Whether it can be the speech model of a station in language "auto" (asr only).
            "detects_languages": detects_languages(e, path),
            "accepted": reg.licenses.accepted(e),
            "fit": dataclasses.asdict(f),
            "speed": cat.speed_hint(e.capability, size, m, backend=e.backend) if size else None,
            "in_use": used.get(e.id, []), "loaded": loaded,
            "error": status["errors"].get(e.id),
            "chain_position": (chains.get(e.capability, []).index(e.id) + 1
                               if e.id in chains.get(e.capability, []) else None),
        })  # fmt: skip
    languages = sorted({s.language for s in app.config.stations if not s.remote})
    recommended = {
        lang: [{"id": i, "why": why}
               for i, why in cat.recommend("asr", lang, reg.chain("asr"), m, sizes)]
        for lang in languages
    }  # fmt: skip
    recommended["translation"] = [
        {"id": i, "why": why} for i, why in cat.recommend(
            "translate", "*", reg.chain("translate"), m, sizes)
    ]  # fmt: skip
    machine = dataclasses.asdict(m)
    machine.update(gpu_limit_gb=round(m.gpu_limit_gb, 1), budget_gb=round(m.budget_gb, 1))
    return {
        "machine": machine,
        "models": entries,
        "chains": chains,
        "downloads": app.downloads.status(),
        "recommended": recommended,
        "loading": status["loading"],
    }


def _room(reg: ModelRegistry, e: ModelEntry, path: Path | None) -> dict[str, Any]:
    """For a model that isn't installed, before anyone clicks: how much of it an earlier
    download kept (``have_bytes``, which the next one goes on from), the bytes still to
    download (``left_bytes``, None with no size known), the library's part-files that no
    download continues (``cut_bytes``), and the free space on the disk it would go to
    (``free_bytes``, and which disk: ``disk``, to add up several models on one)."""
    if path is not None or e.kind == "local":
        return dict.fromkeys(("have_bytes", "left_bytes", "cut_bytes", "free_bytes", "disk"))
    from wavescribe import modelfetch

    folder = modelfetch.target_dir(reg, e)
    return {"have_bytes": modelfetch.model_bytes(folder, e.revision),
            "left_bytes": modelfetch.to_download(reg, e),
            "cut_bytes": modelfetch.cut_bytes(folder),
            "free_bytes": modelfetch.free_bytes(folder),
            "disk": str(modelfetch.disk_of(folder))}  # fmt: skip


def accept_model(app: App, model_id: str, license_id: str, non_commercial: bool) -> None:
    from wavescribe.registry import LicenseError

    entry = app.registry.get(model_id)
    try:
        app.registry.licenses.accept(entry, license_id, non_commercial=non_commercial)
    except LicenseError as e:
        raise AdminError(str(e)) from e


def search_models(query: str, capability: str) -> list[dict[str, Any]]:
    from wavescribe.catalog import models as cat

    m = cat.machine()
    out = []
    for r in cat.search_hf(query, capability, limit=30):
        d = dataclasses.asdict(r)
        d["fit"] = dataclasses.asdict(cat.fit(capability, r.weights_gb, m, backend=r.backend))
        out.append(d)
    return out


def add_catalog_model(app: App, raw: dict[str, Any], capability: str) -> dict[str, Any]:
    """Add a search result to config/models.catalog.toml (pinned to its commit, tier from its
    licence) and to the running registry, at the end of its chain.

    Only the repo and the commit come from the page. The licence, and with it the tier, is read
    again from the model card at that commit (online, on this click), so whatever sent the
    request can't make a model's licence look more permissive than it is."""
    from wavescribe.catalog import models as cat

    repo, sha = str(raw.get("repo") or ""), str(raw.get("sha") or "")
    try:
        model = cat.model_at(repo, sha, capability)
    except ValueError as e:  # not a repo and a commit, or not JSON
        raise AdminError(str(e)) from e
    except (OSError, cat.CatalogError) as e:  # URLError, HTTPError and timeouts are OSErrors
        raise AdminError(f"couldn't read {repo} from Hugging Face: {e}") from e
    try:
        entry_raw = cat.to_registry_entry(model, capability)
        if model.weights_gb:
            entry_raw["size_gb"] = round(model.weights_gb, 2)
        entry = dataclasses.replace(_parse_entry(entry_raw), origin="catalog")
    except (TypeError, ValueError, ModelError) as e:
        raise AdminError(str(e)) from e
    try:
        app.registry.get(entry.id)
        raise AdminError(f"{entry.id} is already in the registry")
    except ModelError:
        pass
    path = CATALOG_MODELS_PATH
    import tomllib

    existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    models = [*existing.get("model", []), entry_raw]
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Models added from the Wavescribe page's online catalogue (machine-written).\n"
        "# Each is pinned to the commit it was found at; delete an entry to remove it.\n\n"
    )
    path.write_text(header + dumps({"model": models}), encoding="utf-8")
    app.registry.add(entry)
    return {"id": entry.id, "tier": entry.tier}


# -- stations: catalogue, playlists, probing ----------------------------------------------------

_refresh = {"state": "idle", "error": None, "count": None, "finished": None}


def station_catalog(query: str, language: str | None, country: str | None,
                    online: bool) -> dict[str, Any]:  # fmt: skip
    from wavescribe.catalog import stations as cat

    if online:
        results = cat.search_online(query, language=language, country=country, limit=50)
        source = "online"
    elif STATION_SNAPSHOT.exists():
        results = cat.search(STATION_SNAPSHOT, query, language=language, country=country,
                             limit=50)  # fmt: skip
        source = "snapshot"
    else:
        results, source = [], "none"
    stamp = STATION_SNAPSHOT.stat().st_mtime if STATION_SNAPSHOT.exists() else None
    return {
        "results": [dataclasses.asdict(r) for r in results],
        "source": source,
        "snapshot_at": stamp,
        "refresh": dict(_refresh),
    }


def refresh_station_catalog() -> dict[str, Any]:
    from wavescribe.catalog import stations as cat

    if _refresh["state"] == "running":
        return dict(_refresh)

    def run() -> None:
        try:
            _refresh["count"] = cat.refresh_snapshot(STATION_SNAPSHOT)
            _refresh["state"], _refresh["error"] = "done", None
        except Exception as e:
            _refresh["state"], _refresh["error"] = "failed", str(e)[:300]
        _refresh["finished"] = time.time()

    _refresh["state"] = "running"
    threading.Thread(target=run, daemon=True, name="station-catalog").start()
    return dict(_refresh)


def import_playlist(text: str | None, url: str | None) -> list[dict[str, Any]]:
    from wavescribe.catalog import stations as cat

    if url:
        if not url.startswith(("http://", "https://")):
            raise AdminError("a playlist URL starts with http:// or https://")
        entries = cat.fetch_playlist(url)
    elif text:
        entries = cat.parse_playlist(text)
    else:
        raise AdminError("paste a playlist or give its URL")
    return [dataclasses.asdict(e) for e in entries[:500]]


def probe(app: App, url: str, detect: bool) -> dict[str, Any]:
    """Check a stream URL: what it carries, and (with ``detect``) which language it speaks,
    from 20 s of audio through Whisper's language identification."""
    from wavescribe.catalog import stations as cat

    if not url.startswith(("http://", "https://")):
        raise AdminError("a stream URL starts with http:// or https://")
    p = cat.probe_stream(url)
    out: dict[str, Any] = {"probe": dataclasses.asdict(p)}
    if p.ok and detect:
        out["detected"] = detect_language(app, url)
    return out


def detect_language(app: App, url: str, seconds: float = 20.0) -> dict[str, Any]:
    from wavescribe.catalog import stations as cat

    reg = app.registry
    whisper = next((e for e in reg.chain("asr") if "whisper" in e.id
                    and reg.local_path(e) is not None), None)  # fmt: skip
    if whisper is None:
        return {"error": "language detection needs a Whisper model; fetch whisper-large-v3-turbo"}
    try:
        samples = cat.sample_audio(url, seconds)
    except Exception as e:
        return {"error": f"could not record a sample: {e}"[:300]}
    samples, music_s = speech_only(reg, samples)
    if samples is None:
        return {"error": f"heard only music for {seconds:.0f} s (a song's language says nothing "
                "about the station's): try again later, or set the language yourself",
                "music": True}  # fmt: skip
    path = reg.local_path(whisper)
    done = threading.Event()
    box: dict[str, Any] = {}

    def run(get: Any) -> None:
        try:
            t = get(whisper, path)
            heard = t.detect_language(samples) if hasattr(t, "detect_language") else None
            if heard is None:
                box["error"] = f"{whisper.id} can't identify languages"
            else:
                box.update(language=heard.language, text=heard.text[:400], model=whisper.id)
        except Exception as e:
            box["error"] = str(e)[:300]
        finally:
            done.set()

    if not app.pipeline.request_asr(run):
        return {"error": "the speech model is busy; try again"}
    if not done.wait(120):
        return {"error": "no answer from the speech model in time"}
    if music_s:
        box["music_skipped_s"] = music_s
    if "language" in box:
        from wavescribe.languages import language_name

        try:
            box["language_name"] = language_name(box["language"])
        except Exception:
            box["language_name"] = box["language"]
    return box


def speech_only(reg: Any, samples: Any, chunk_s: float = 4.0) -> tuple[Any, float]:
    """The sample with its music taken out, by the music gate in 4 s pieces, so a song in
    English on a Portuguese station doesn't decide the station's language. ``(None, s)`` when
    it was all music; the sample unchanged when no gate is installed."""
    import numpy as np

    from wavescribe.audio import SAMPLE_RATE

    try:
        from wavescribe.gate.yamnet import YamnetGate

        _, path = reg.resolve("gate")
        gate = YamnetGate(path)
    except Exception:
        return samples, 0.0
    step = int(chunk_s * SAMPLE_RATE)
    keep, music = [], 0.0
    for i in range(0, len(samples), step):
        piece = samples[i : i + step]
        if len(piece) < SAMPLE_RATE:
            continue
        if gate.classify(piece).kind == "music":
            music += len(piece) / SAMPLE_RATE
        else:
            keep.append(piece)
    if not keep:
        return None, music
    return np.concatenate(keep), music


# -- receivers ---------------------------------------------------------------------------------

_monitors: dict[str, Any] = {}


def receivers_view(app: App, scan: bool = False) -> dict[str, Any]:
    from wavescribe.radio.service import reach_center_mhz, span_mhz
    from wavescribe.sources import sdr

    status = app.pipeline.status()["stations"]
    page_devices = {d.get("id") for d in app.settings.get("device") or []}
    overrides = app.settings.get("device_overrides") or {}
    devices = []
    for d in app.config.devices:
        dev = sdr.find_device(d.serial)
        stations = [s for s in app.config.stations if s.sdr and s.sdr.device.id == d.id]
        devices.append({
            **device_dict(d),
            "origin": "page" if d.id in page_devices else "file",
            "overridden": sorted(overrides.get(d.id, {})),
            "span_mhz": span_mhz(d),  # what it takes in, [lo, hi], as every view gives it
            "running": dev is not None and dev.connected,
            "reconnects": dev.reconnects if dev else 0,
            "clipped": dev.clipped if dev else None,
            "monitoring": d.id in _monitors,
            "stations": [{
                "id": s.id, "name": s.name, "mhz": s.sdr.freq_hz / 1e6,
                "running": s.id in status,
                "signal_db": status.get(s.id, {}).get("signal_db"),
                "clipped": status.get(s.id, {}).get("clipped"),
                # Why the receiver, tuned as it is, doesn't hear it; None when it does.
                "out_of_reach": None if s.disabled else s.out_of_reach,  # as /api/state
                "reach_center_mhz": reach_center_mhz(s, app.config.stations, status),
            } for s in stations],
        })  # fmt: skip
    out: dict[str, Any] = {"devices": devices, "rtl_sdr": _which("rtl_sdr")}
    if scan:
        out["dongles"] = sdr.recent_dongles()
    return out


def _which(cmd: str) -> bool:
    import shutil

    return shutil.which(cmd) is not None


def spectrum(app: App, device_id: str) -> dict[str, Any] | None:
    from wavescribe.sources import sdr

    d = next((x for x in app.config.devices if x.id == device_id), None)
    if d is None:
        raise AdminError(f"no dongle called {device_id!r}")
    dev = sdr.find_device(d.serial)
    if dev is None:
        return None
    dev.spectrum_wanted_until = time.time() + 3.0
    return dev.spectrum


def monitor(app: App, device_id: str, on: bool, *, force: bool = False) -> None:
    """Keep a dongle running with no station, to watch its spectrum (antenna and gain). While
    listening is paused it starts or stops with resume, unless ``force`` (pause itself)."""
    from wavescribe.sources import sdr

    d = next((x for x in app.config.devices if x.id == device_id), None)
    if d is None:
        raise AdminError(f"no dongle called {device_id!r}")
    if not force and app.hold_monitor(device_id, on):
        return
    if on:
        if device_id in _monitors:
            return
        try:
            dev = sdr.shared_device(d)
        except ValueError as e:
            raise AdminError(str(e)) from e
        if not dev.available:
            raise AdminError("rtl_sdr isn't installed (brew install librtlsdr)")
        mon = sdr.SpectrumMonitor(dev)
        dev.attach(mon)  # type: ignore[arg-type]
        _monitors[device_id] = mon
    else:
        mon = _monitors.pop(device_id, None)
        if mon is not None:
            mon.close()


def update_device(app: App, device_id: str, changes: dict[str, Any], *,
                  retune: bool = False) -> dict[str, Any]:  # fmt: skip
    """New centre, gain, sample rate or frequency correction for a dongle: its stations stop,
    the dongle reopens with the new settings, and the stations that were running start again.

    Stations the new tuning doesn't hear (``heard_by``) are left out: they stay listed,
    unavailable with the reason, and those running stop for good. Before it does that it asks
    (``retune_question``, code ``retune``), naming them, running or not; ``retune`` is the
    yes. A switched-off station, or one already out of reach, is never asked about."""
    from wavescribe.radio.service import retune_question
    from wavescribe.settings import DEVICE_OVERRIDE_KEYS
    from wavescribe.sources import sdr

    d = next((x for x in app.config.devices if x.id == device_id), None)
    if d is None:
        raise AdminError(f"no dongle called {device_id!r}")
    bad = set(changes) - DEVICE_OVERRIDE_KEYS
    if bad:
        raise AdminError(f"can't change {', '.join(sorted(bad))} here")

    def mutate(data: dict[str, Any]) -> None:
        over = data.setdefault("device_overrides", {}).setdefault(device_id, {})
        for k, v in changes.items():
            if v is None:
                over.pop(k, None)
            else:
                over[k] = v

    from wavescribe.settings import effective_config

    draft = app.settings.snapshot()
    mutate(draft)
    config, errors = effective_config(app.base, draft)
    on_device = [s for s in app.config.stations if s.sdr and s.sdr.device.id == device_id]
    bad_stations = {k: v for k, v in errors.items() if k == f"device:{device_id}" or (
        any(s.id == k for s in on_device) and k not in app.config_errors)}  # fmt: skip
    if bad_stations:
        raise AdminError(next(iter(bad_stations.values())))
    after = {s.id: s for s in config.stations}
    unfit = [s for s in on_device if not s.disabled and not s.out_of_reach
             and s.id in after and after[s.id].out_of_reach]  # fmt: skip
    running = [s.id for s in on_device if s.id in app.running()]
    if unfit and not retune:
        moved = next(x for x in config.devices if x.id == device_id)
        raise retune_question(device_id, moved.center_hz, unfit, set(running))
    watching = device_id in _monitors
    for sid in running:
        app.pipeline.remove_station(sid, wait=10)
    if watching:
        monitor(app, device_id, False)
    deadline = time.time() + 5
    while not sdr.forget_device(d.serial) and time.time() < deadline:
        time.sleep(0.1)
    app.settings.update(mutate)
    try:
        app._reload()
    except ConfigError as e:
        raise AdminError(str(e)) from e
    for sid in running:
        s = next((x for x in app.config.stations if x.id == sid), None)
        if s is not None and sid not in app.unavailable:
            app.pipeline.add_station(app._runtime(s))
        else:  # left out by the new tuning: stopped, and not started again by the next serve
            app.stop_station(sid)
    if watching:
        monitor(app, device_id, True)
    return next(x for x in receivers_view(app)["devices"] if x["id"] == device_id)


def add_device(app: App, raw: dict[str, Any]) -> dict[str, Any]:
    """A dongle added on the page (``[[device]]`` in config/ui.toml)."""
    from wavescribe.config import build_device
    from wavescribe.settings import effective_config

    raw = {k: v for k, v in raw.items() if v not in (None, "")}
    device_id = build_device(raw, "new dongle").id  # nothing broken is ever saved
    if any(d.id == device_id for d in app.config.devices):
        raise AdminError(f"there is already a dongle called {device_id!r}")
    draft = app.settings.snapshot()
    draft.setdefault("device", []).append(raw)
    _, errors = effective_config(app.base, draft)
    bad = [v for k, v in errors.items() if k in (f"device:{device_id}", "device:", device_id)]
    if bad:
        raise AdminError(bad[0])
    app.settings.update(lambda d: d.setdefault("device", []).append(raw))
    app._reload()
    return next(x for x in receivers_view(app)["devices"] if x["id"] == device_id)


def delete_device(app: App, device_id: str) -> None:
    if not any(d.get("id") == device_id for d in app.settings.get("device") or []):
        raise AdminError(f"{device_id} is in stations.toml: edit that file to remove it")
    if any(s.sdr and s.sdr.device.id == device_id for s in app.config.stations):
        raise AdminError("stations still use this dongle: move or delete them first")
    monitor(app, device_id, False)
    app.settings.update(lambda d: d.__setitem__(
        "device", [x for x in d.get("device", []) if x.get("id") != device_id]))  # fmt: skip
    app._reload()


def remote_stations(url: str, token: str | None) -> dict[str, Any]:
    """What another Wavescribe offers (its /feed/v1/node and /stations), fetched from here
    because the page can't read another origin."""
    import json
    import urllib.error
    import urllib.request

    from wavescribe import httpclient

    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise AdminError("the address starts with http:// or https://")
    headers = {"Accept": "application/json", "User-Agent": "wavescribe"}
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"
    out: dict[str, Any] = {}
    for key in ("node", "stations"):
        req = urllib.request.Request(f"{url}/feed/v1/{key}", headers=headers)
        try:
            # A redirect isn't followed: the token would go wherever it points.
            with httpclient.opener().open(req, timeout=10) as resp:
                out[key] = json.loads(resp.read(2_000_000))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AdminError("that Wavescribe needs a feed token (read scope)") from e
            raise AdminError(f"{url} answered {e.code}") from e
        except (OSError, ValueError) as e:
            raise AdminError(f"couldn't reach {url}: {e}") from e
    return out
