# SPDX-License-Identifier: Apache-2.0
"""``wavescribe doctor``: an offline self-check that says what is wrong and what to do about it.

It reads and never changes anything:

- **No network at all.** Models are looked up in the registry and the local caches.
- **No radio receiver is opened.** Dongles are listed from macOS's USB registry (``ioreg``);
  rtl_sdr, rtl_power and rtl_test are never run, since opening a dongle disturbs a Wavescribe
  that is using it (``LocalHost.run`` refuses them).
- **No request to the page.** The port is checked with ``lsof`` and serve's own probe bind.
- **The background service** is read with ``launchctl print``, through ``service.Launchd``.

Each finding is a ``Check``: ok, warn (works, but worth knowing), fail (something won't work;
the exit code is then 1) or skip (doesn't apply here), and every problem comes with its next
step. Commands go through ``Host`` and launchctl through ``service.Launchd``, so the tests use
fakes. Doctor takes serve's options; with none given, it checks the options the installed
background service runs with, so the port, config and limits are the ones that really run.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.metadata
import logging
import os
import platform
import re
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from wavescribe.registry import ModelEntry, ModelRegistry
    from wavescribe.service import Launchd

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
GB = 1024**3
MB = 1024**2
# Text, translations and their search index of one busy talk station: the 34 h soak stored
# 31 MB a day for four (docs/MEASUREMENTS.md).
TEXT_MB_PER_STATION_DAY = 31 / 4
HEADROOM_GB = 1.0
SERVICE_LOG_WARN_BYTES = 100 * MB
# USB vendor and product ids of Realtek's RTL2832U; most RTL-SDR dongles use 0bda:2838. Others
# are recognised by name.
RTL_USB_IDS = {(0x0BDA, 0x2832), (0x0BDA, 0x2838)}
# Opening a dongle disturbs a Wavescribe already using it: doctor never runs these.
NEVER_RUN = {"rtl_sdr", "rtl_power", "rtl_test", "rtl_fm", "rtl_tcp", "rtl_eeprom", "rtl_adsb"}
GROUPS = {
    "environment": "Environment",
    "config": "Configuration",
    "tools": "Tools",
    "models": "Models",
    "receivers": "Receivers",
    "disk": "Disk",
    "page": "Web page",
    "voices": "Spoken alerts",
    "unattended": "Running unattended",
}
VOICES_WHERE = "System Settings → Accessibility → Spoken Content → System voice → Manage voices"
ALLOW_ACCESSORY = ("allow it when macOS asks (System Settings → Privacy & Security → Allow "
                   "accessories to connect), then unplug it and plug it in again")  # fmt: skip


@dataclass
class Check:
    group: str
    name: str
    status: str
    summary: str
    fix: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "check",
            "group": self.group,
            "check": self.name,
            "status": self.status,
            "summary": self.summary,
            "fix": self.fix or None,
            "details": self.details,
        }


class Host(Protocol):
    """The commands doctor reads the machine with. Tests pass a fake."""

    platform: str  # sys.platform
    machine: str  # platform.machine()

    def which(self, name: str) -> str | None: ...

    def run(self, args: Sequence[str], timeout: float = 5.0) -> str | None:
        """The command's stdout, or None when it is missing, fails silently or hangs."""
        ...


class LocalHost:
    def __init__(self) -> None:
        self.platform = sys.platform
        self.machine = platform.machine()

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, args: Sequence[str], timeout: float = 5.0) -> str | None:
        if Path(args[0]).name in NEVER_RUN:
            raise ValueError(f"doctor never runs {args[0]}: it would open a radio receiver")
        from wavescribe.system import run_command

        return run_command(args, timeout=timeout)


# -- parsers (pure, so the tests feed them sample output) -----------------------------------

_NODE = re.compile(r"\+-o (.+?)@[0-9a-fA-F]+\s+<class (\w+)")
_PROP = re.compile(r'^[\s|]*"([^"]+)" = (.*)$')


def _unquote(value: str) -> str:
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def parse_usb_dongles(text: str) -> list[dict[str, str]]:
    """RTL-SDR dongles in ``ioreg -p IOUSB -l -w 0``: serial, product, vendor, USB id.

    The USB registry lists what is plugged in (and allowed to connect) without opening
    anything, so a dongle in use by Wavescribe is listed and left alone.
    """
    nodes: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if m := _NODE.search(line):
            current = {"_name": m[1]}
            nodes.append(current)
        elif current is not None and (p := _PROP.match(line)):
            current.setdefault(p[1], p[2])
    out = []
    for n in nodes:
        try:
            vid, pid = int(n.get("idVendor", "")), int(n.get("idProduct", ""))
        except ValueError:
            continue
        product = _unquote(n.get("USB Product Name") or n.get("kUSBProductString") or n["_name"])
        vendor = _unquote(n.get("USB Vendor Name") or n.get("kUSBVendorString") or "")
        if (vid, pid) not in RTL_USB_IDS and not re.search(r"RTL28|RTL-?SDR", product + vendor,
                                                           re.I):  # fmt: skip
            continue
        serial = _unquote(n.get("USB Serial Number") or n.get("kUSBSerialNumberString") or "")
        out.append({"serial": serial, "product": product, "vendor": vendor,
                    "usb_id": f"{vid:04x}:{pid:04x}"})  # fmt: skip
    return out


def parse_voice_list(listing: str) -> dict[str, str]:
    """``say -v ?`` as {voice name: locale}, e.g. {"Daniel": "en_GB"}. The names are the ones
    ``alerts.actions.parse_voices`` reads."""
    out: dict[str, str] = {}
    for line in listing.splitlines():
        parts = line.split("#", 1)[0].rstrip().rsplit(None, 1)
        if len(parts) == 2 and re.fullmatch(r"[a-z]{2,3}_\w+", parts[1]):
            out[parts[0].strip()] = parts[1]
            if m := re.fullmatch(r"(.+?) \(.*\)", parts[0].strip()):
                out[m[1]] = parts[1]  # "Eddy (English (UK))" is also accepted as "Eddy"
    return out


def parse_lsof_pid(text: str | None) -> int | None:
    """The first process in ``lsof -F p`` output."""
    m = re.search(r"^p(\d+)$", text or "", re.M)
    return int(m[1]) if m else None


def size_text(n_bytes: float) -> str:
    return f"{n_bytes / GB:.1f} GB" if n_bytes >= 0.1 * GB else f"{n_bytes / MB:.0f} MB"


def program(command: str) -> str:
    """A command line with its program's path dropped: "Python serve.py"."""
    exe, _, rest = command.strip().partition(" ")
    return f"{Path(exe).name} {rest}".strip()


def short(path: str | Path | None) -> str:
    if path is None:
        return "-"
    text, home = str(path), str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home + os.sep) else text


# -- the checks ------------------------------------------------------------------------------


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message not in self.messages:
            self.messages.append(message)


@contextlib.contextmanager
def _captured_warnings() -> Iterator[list[str]]:
    """What the code doctor calls logs at WARNING or above, to show it as a check rather than
    on stderr."""
    logger = logging.getLogger("wavescribe")
    handler = _Collect()
    propagate = logger.propagate
    logger.addHandler(handler)
    logger.propagate = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        logger.propagate = propagate


class Doctor:
    """Runs every check against ``serve``, a namespace of serve's options."""

    def __init__(
        self,
        serve: argparse.Namespace,
        *,
        host: Host | None = None,
        registry: Callable[[], ModelRegistry] | None = None,
        launchd: Launchd | None = None,
        home: Path | None = None,
        port_probe: Callable[[str, int], None] | None = None,
        disk_usage: Callable[[Path], Any] = shutil.disk_usage,
        workdir: Path | None = None,
    ) -> None:
        from wavescribe import service
        from wavescribe.registry import ModelRegistry

        self.serve = serve
        self.host = host or LocalHost()
        self.registry_factory = registry or (lambda: ModelRegistry.load())
        self._launchd = launchd
        self.home = home
        self.port_probe = port_probe or (lambda h, p: service.port_probe(h, p))
        self.disk_usage = disk_usage
        self.workdir = workdir or Path.cwd()
        self.data_dir = self.workdir / "data"
        self.checks: list[Check] = []
        self.config: Any = None  # the stations with the page's changes, once they load
        self.settings: dict[str, Any] = {}
        self.alert_config: Any = None
        self.missing: dict[str, float] = {}  # models to download: id -> GB
        self._service: dict[str, Any] | bool | None = False  # False: not read yet

    def add(self, group: str, name: str, status: str, summary: str, fix: str = "",
            **details: Any) -> Check:  # fmt: skip
        check = Check(group, name, status, summary, fix, details)
        self.checks.append(check)
        return check

    def run(self) -> list[Check]:
        for group, step in (
            ("environment", self.check_environment),
            ("config", self.check_config),
            ("tools", self.check_tools),
            ("models", self.check_models),
            ("receivers", self.check_receivers),
            ("disk", self.check_disk),
            ("page", self.check_port),
            ("voices", self.check_voices),
            ("unattended", self.check_unattended),
        ):
            with _captured_warnings() as logged:
                try:
                    step()
                except Exception as e:  # one broken check mustn't hide the others
                    self.add(group, "doctor", FAIL, f"couldn't check this: {type(e).__name__}: "
                             f"{e}", "please report it, with this line")  # fmt: skip
            for message in logged:
                self.add(group, "log", WARN, message)
        return self.checks

    # -- environment ---------------------------------------------------------------------

    def check_environment(self) -> None:
        g = "environment"
        v = sys.version_info
        ver = f"{v.major}.{v.minor}.{v.micro}"
        own = (self.workdir / ".venv").resolve()
        if (v.major, v.minor) != (3, 12):
            self.add(g, "python", WARN, f"Python {ver}: Wavescribe is built and tested on 3.12",
                     "in the Wavescribe folder, `uv sync` installs 3.12 there; then run "
                     "commands as `uv run wavescribe …`")  # fmt: skip
        elif sys.prefix == sys.base_prefix:
            self.add(g, "python", WARN, f"Python {ver} outside a virtual environment "
                     f"({sys.executable})", "run it from the Wavescribe folder as "
                     "`uv run wavescribe doctor`")  # fmt: skip
        elif own.is_dir() and Path(sys.prefix).resolve() != own:
            self.add(
                g,
                "python",
                WARN,
                f"Python {ver} from {short(sys.prefix)}, not this folder's .venv",
                "run `uv run wavescribe doctor` inside the folder",
            )
        else:
            self.add(g, "python", OK, f"Python {ver} in {short(sys.prefix)}",
                     executable=sys.executable)  # fmt: skip

        apple = self.host.platform == "darwin" and self.host.machine == "arm64"
        if apple:
            mac = platform.mac_ver()[0]
            self.add(g, "platform", OK, f"macOS {mac} on Apple Silicon".replace("  ", " "))
        elif self.host.platform == "darwin":
            self.add(g, "platform", WARN, "an Intel Mac: transcription and translation run on "
                     "Apple Silicon only (MLX)", "listening works; for text, use a Mac with "
                     "Apple Silicon")  # fmt: skip
        else:
            self.add(g, "platform", WARN, f"{self.host.platform}: Wavescribe supports macOS on "
                     "Apple Silicon for now; transcription, translation and spoken alerts "
                     "won't work here", "use a Mac with Apple Silicon")  # fmt: skip

        try:
            requires = importlib.metadata.requires("wavescribe") or []
        except importlib.metadata.PackageNotFoundError:
            self.add(g, "libraries", WARN, "Wavescribe isn't installed in this environment",
                     "in the Wavescribe folder: uv sync")  # fmt: skip
        else:
            missing, found = [], {}
            for line in requires:
                spec, _, marker = line.partition(";")
                if "extra ==" in marker:
                    continue
                if ("sys_platform" in marker or "platform_machine" in marker) and not apple:
                    continue
                name = re.match(r"[A-Za-z0-9._-]+", spec.strip())
                if name is None:
                    continue
                try:
                    found[name[0]] = importlib.metadata.version(name[0])
                except importlib.metadata.PackageNotFoundError:
                    missing.append(name[0])
            if missing:
                self.add(g, "libraries", FAIL, f"missing: {', '.join(missing)}",
                         "in the Wavescribe folder: uv sync", installed=found)  # fmt: skip
            else:
                self.add(g, "libraries", OK, f"all {len(found)} installed", installed=found)

        if (self.workdir / "config").is_dir():
            self.add(g, "folder", OK, f"{short(self.workdir)}: config/ and data/ are here")
        else:
            self.add(g, "folder", WARN, f"no config/ folder in {short(self.workdir)}: Wavescribe "
                     "reads config/ and writes data/ in the folder it runs in",
                     "cd into the Wavescribe folder, then run it again")  # fmt: skip

    # -- configuration -------------------------------------------------------------------

    def check_config(self) -> None:
        from wavescribe.alerts import (
            DEFAULT_KEYWORDS_DIR,
            merge_page,
            parse_alerts,
            read_alerts,
            resolve_alerts_path,
        )
        from wavescribe.config import (
            EXAMPLE_STATIONS_PATH,
            ConfigError,
            load_stations,
            resolve_stations_path,
        )
        from wavescribe.settings import Settings, effective_config

        g = "config"
        base = None
        try:
            path = resolve_stations_path(self.serve.config)
            base = load_stations(path)
        except ConfigError as e:
            self.add(g, "stations", FAIL, str(e), "correct it (serve stops with this message), "
                     f"or start again from a copy of {EXAMPLE_STATIONS_PATH}")  # fmt: skip
        else:
            n, d = len(base.stations), len(base.devices)
            summary = f"{path}: {n} station{'s' * (n != 1)}"
            summary += f", {d} receiver{'s' * (d != 1)}" if d else ""
            if path == EXAMPLE_STATIONS_PATH:
                summary += " (the example list: copy it to config/stations.toml to make your own)"
            self.add(g, "stations", OK, summary, path=str(path))

        settings_path = Path(self.serve.settings)
        try:
            settings = Settings(settings_path)
        except ConfigError as e:
            self.add(
                g,
                "page changes",
                FAIL,
                str(e),
                f"correct {settings_path} or delete it: "
                "it holds only what was changed on the page; deleting it undoes that",
            )
            return
        self.settings = settings.data
        if base is None:
            return
        config, errors = effective_config(base, settings.data)
        self.config = config
        if errors:
            listed = "; ".join(f"{k}: {v}" for k, v in list(errors.items())[:2])
            more = f" (and {len(errors) - 2} more)" if len(errors) > 2 else ""
            self.add(g, "page changes", WARN, f"{len(errors)} not applied: {listed}{more}",
                     f"change them again on the page, or remove them from {settings_path}",
                     errors=errors)  # fmt: skip
        elif settings_path.exists():
            self.add(g, "page changes", OK, f"{settings_path}: they apply cleanly")
        else:
            self.add(g, "page changes", OK, f"none yet ({settings_path} doesn't exist)")
        policy = (settings.data.get("radio") or {}).get("policy") or {}
        if policy:
            from wavescribe.radio.bandplan import check_policies

            try:
                check_policies(policy, f"{settings_path} [radio.policy]")
            except ValueError as e:
                again = "set them again under Settings → Storage and privacy (What's kept)"
                self.add(g, "radio rules", WARN, f"ignored: {e}", again)
        for sid in self.serve.stations:
            try:
                config.get(sid)
            except ConfigError as e:
                self.add(g, "station", FAIL, str(e), "correct the station id on the command line")
        silent = [s.id for s in config.stations if not (s.remote or s.disabled or s.sdr
                                                         or s.stream or s.stream_hls)]  # fmt: skip
        if silent:
            self.add(g, "sources", WARN, f"no receiver or stream URL for: {', '.join(silent)}",
                     "give each a stream URL, or a [[device]] that can hear its frequency "
                     "(docs/RADIO.md)")  # fmt: skip

        if self.serve.no_alerts:
            self.add(g, "alerts", SKIP, "off (--no-alerts)")
            return
        apath = resolve_alerts_path(self.serve.alerts)
        if apath is None:
            self.add(g, "alerts", SKIP, "off: there is no config/alerts.toml (copy "
                     "config/alerts.example.toml to turn them on)")  # fmt: skip
            return
        try:
            raw, rules, disabled = merge_page(read_alerts(apath), settings.data.get("alerts") or {})
            alerts = parse_alerts(raw, apath, DEFAULT_KEYWORDS_DIR, rules, disabled)
        except (ConfigError, ValueError) as e:
            self.add(g, "alerts", FAIL, str(e), f"correct {apath} (serve stops with this "
                     "message), or run serve with --no-alerts")  # fmt: skip
            return
        self.alert_config = alerts
        packs = ", ".join(sorted(alerts.rules.packs)) or "none"
        self.add(g, "alerts", OK, f"{apath}: {len(alerts.rules.rules)} keyword, "
                 f"{len(alerts.rules.llm_rules)} LLM and {len(alerts.rules.station_rules)} "
                 f"station rules; keyword packs: {packs}")  # fmt: skip

    def _stations(self, *, transcribed: bool = False) -> list[Any]:
        if self.config is None:
            return []
        out = [s for s in self.config.stations if not s.disabled and not s.remote]
        return [s for s in out if s.transcribe] if transcribed else out

    # -- tools ---------------------------------------------------------------------------

    def check_tools(self) -> None:
        g = "tools"
        ffmpeg = self.host.which("ffmpeg")
        if not ffmpeg:
            self.add(g, "ffmpeg", FAIL, "not found: internet stations, the speech archive and "
                     "replay need it", "brew install ffmpeg")  # fmt: skip
        else:
            out = self.host.run(["ffmpeg", "-hide_banner", "-version"], timeout=10)
            m = re.search(r"ffmpeg version (\S+)", out or "")
            if out is None:
                self.add(g, "ffmpeg", FAIL, f"{ffmpeg} doesn't run", "brew reinstall ffmpeg")
            else:
                self.add(g, "ffmpeg", OK, f"{m[1] if m else 'runs'} ({ffmpeg})")

        devices = self.config.devices if self.config is not None else ()
        on_air = any(s.sdr for s in self._stations())
        for name, missing, purpose in (
            ("rtl_sdr", FAIL if on_air else WARN if devices else SKIP, "listening on a receiver"),
            ("rtl_power", WARN if devices else SKIP, "scanning bands (Find → On the air)"),
        ):
            path = self.host.which(name)
            if not path:
                hint = "" if devices else ", only with a radio receiver"
                self.add(g, name, missing, f"not installed: needed for {purpose}{hint}",
                         "brew install librtlsdr")  # fmt: skip
                continue
            real = os.path.realpath(path)
            if not os.path.isfile(real) or not os.access(real, os.X_OK):
                self.add(g, name, FAIL, f"{path} is broken (it leads to {real})",
                         "brew reinstall librtlsdr")  # fmt: skip
                continue
            m = re.search(r"/Cellar/([^/]+)/([^/]+)/", real)
            version = f"{m[1]} {m[2]}" if m else "installed"
            self.add(g, name, OK, f"{version} ({path}); not started, as that would open a "
                     "receiver")  # fmt: skip

    # -- models --------------------------------------------------------------------------

    def _fix_for(self, registry: ModelRegistry, capability: str, language: str,
                 chosen: str | None) -> str:  # fmt: skip
        """The next step for a capability with nothing usable, and what it would download."""
        from wavescribe.registry import ModelError, accept_command

        if chosen:
            try:
                entry = registry.get(chosen)
            except ModelError:
                return f"{chosen} isn't in the registry: choose another model on the page"
            if not entry.supports(language):
                return f"{chosen} doesn't know {language}: choose another model on the page"
        else:
            candidates = [e for e in registry.chain(capability) if e.supports(language)]
            if not candidates:
                return (f"no registered model knows {language}: see docs/MODELS.md, or add one "
                        "in config/models.local.toml")  # fmt: skip
            accepted = [e for e in candidates if registry.licenses.accepted(e)]
            entry = (accepted or candidates)[0]
        if not registry.licenses.accepted(entry):
            return f"{accept_command(entry)}, then: wavescribe models fetch {entry.id}"
        self.missing[entry.id] = entry.size_gb or 0.0
        size = f" ({size_text(entry.size_gb * GB)})" if entry.size_gb else ""
        return (f"wavescribe models fetch {entry.id}{size}, or download it under Settings → "
                "Languages and models")  # fmt: skip

    @staticmethod
    def _model(entry: ModelEntry) -> str:
        return entry.id + (" (non-commercial use only)" if entry.tier == "restricted" else "")

    def check_models(self) -> None:
        from wavescribe.pipeline import plan_station
        from wavescribe.registry import ModelError
        from wavescribe.translate import unsupported_targets

        g = "models"
        if self.config is None:
            self.add(g, "models", SKIP, "not checked: the configuration doesn't load")
            return
        try:
            registry = self.registry_factory()
        except (ModelError, OSError, ValueError) as e:
            self.add(g, "registry", FAIL, f"the model registry doesn't load: {e}",
                     "correct config/models.local.toml or config/models.catalog.toml")  # fmt: skip
            return
        for cap, ids in (self.settings.get("models") or {}).items():  # the page's model order
            known = [i for i in ids if any(e.id == i for e in registry.entries())]
            with contextlib.suppress(ModelError):
                registry.set_chain(cap, known)
        installed = [e.id for e in registry.entries() if registry.local_path(e) is not None]
        where = [short(registry.models_dir.resolve())]
        with contextlib.suppress(ImportError):
            from huggingface_hub import constants

            where.append(short(constants.HF_HUB_CACHE))
        self.add(g, "installed", OK, f"{len(installed)} of {len(registry.entries())} registered "
                 f"models, in {' and '.join(where)}", installed=installed)  # fmt: skip

        transcribed = self._stations(transcribed=True)
        if not transcribed:
            self.add(g, "speech", SKIP, "no station is transcribed: listening needs no models")
            return
        for cap, label, missing, why in (
            ("vad", "speech detector", FAIL, "every transcribed station needs it"),
            ("gate", "music filter", WARN, "without it, songs are transcribed too"),
        ):
            if cap == "gate" and self.serve.no_gate:
                self.add(g, label, SKIP, "off (--no-gate)")
                continue
            try:
                entry, _ = registry.resolve(cap)
            except ModelError:
                fix = self._fix_for(registry, cap, "*", None)
                self.add(g, label, missing, f"not installed: {why}", fix)
            else:
                self.add(g, label, OK, self._model(entry))

        groups: dict[tuple[str, str | None], list[Any]] = {}
        for s in transcribed:
            groups.setdefault((s.language, self.serve.asr or s.asr_model), []).append(s)
        for (language, chosen), stations in groups.items():
            ids = ", ".join(s.id for s in stations)
            try:
                entry, _ = plan_station(registry, stations[0], self.serve.asr)
            except ModelError as e:
                fix = self._fix_for(registry, "asr", language, chosen)
                said = f"{chosen} isn't usable" if chosen else "no speech model installed"
                self.add(g, f"speech {language}", FAIL, f"{said} for {ids}",
                         fix, error=str(e))  # fmt: skip
            else:
                self.add(g, f"speech {language}", OK, f"{self._model(entry)} for {ids}")

        targets = sorted({t for s in transcribed for t in s.translation_targets})
        if self.serve.no_translate or not targets:
            why = "off (--no-translate)" if self.serve.no_translate else "no station is translated"
            self.add(g, "translation", SKIP, why)
            return
        try:
            mt = self.serve.translator
            entry, path = registry.installed(mt) if mt else registry.resolve("translate")
        except ModelError as e:
            fix = self._fix_for(registry, "translate", "*", self.serve.translator)
            self.add(g, "translation", FAIL, f"no translator installed, for {', '.join(targets)}",
                     fix, error=str(e))  # fmt: skip
        else:
            self.add(g, "translation", OK, f"{self._model(entry)} into {', '.join(targets)}")
            unknown = unsupported_targets(entry, path, targets)
            if unknown:
                self.add(g, "translation", WARN, f"{entry.id} can't translate into "
                         f"{', '.join(unknown)}", "choose another translator, or correct "
                         "translate_to")  # fmt: skip
        for s in transcribed:
            if s.mt_model and not self.serve.translator:
                try:
                    registry.installed(s.mt_model)
                except ModelError as e:
                    self.add(g, f"translation {s.id}", WARN, f"{s.id}'s own translator isn't "
                             f"usable, so the shared one is used: {e}",
                             self._fix_for(registry, "translate", "*", s.mt_model))  # fmt: skip

    # -- receivers -----------------------------------------------------------------------

    def check_receivers(self) -> None:
        g = "receivers"
        devices = list(self.config.devices) if self.config is not None else []
        users = {d.id: [s.id for s in self._stations() if s.sdr and s.sdr.device.id == d.id]
                 for d in devices}  # fmt: skip
        if self.host.platform != "darwin":
            self.add(g, "usb", SKIP, "can't list receivers without opening them here (macOS's "
                     "ioreg can)")  # fmt: skip
            return
        out = self.host.run(["ioreg", "-p", "IOUSB", "-l", "-w", "0"], timeout=10)
        if out is None:
            self.add(g, "usb", WARN, "couldn't read the USB device list (ioreg)")
            return
        dongles = parse_usb_dongles(out)
        serials = [d["serial"] for d in dongles]
        configured = {d.serial for d in devices}
        if not dongles:
            if devices:
                self.add(g, "usb", FAIL, "no RTL-SDR receiver on USB",
                         f"plug it in; {ALLOW_ACCESSORY}")  # fmt: skip
            else:
                self.add(g, "usb", SKIP, "no RTL-SDR receiver on USB (only needed for radio "
                         "without the internet)")  # fmt: skip
        else:
            listed = ", ".join(f"{d['serial'] or 'no serial'} ({d['product']})" for d in dongles)
            summary = f"{len(dongles)} RTL-SDR on USB: {listed}"
            spare = [s for s in dict.fromkeys(serials) if s not in configured]
            if spare and devices:
                summary += f"; not used yet: {', '.join(spare)} (Find → On the air uses it)"
            shared = sorted({s for s in serials if serials.count(s) > 1})
            if shared:
                self.add(g, "usb", WARN, f"{summary}; {', '.join(shared)} is the serial of more "
                         "than one, so Wavescribe can't tell them apart",
                         "give each its own serial: with Wavescribe stopped and only that dongle "
                         "plugged in, rtl_eeprom -s 00001234 (any 8 digits), then replug it",
                         dongles=dongles)  # fmt: skip
            else:
                self.add(g, "usb", OK, summary, dongles=dongles)
        for d in devices:
            used = users[d.id]
            if d.serial in serials:
                who = f"for {', '.join(used)}" if used else "no station uses it yet"
                self.add(g, f"receiver {d.id}", OK, f"serial {d.serial} is plugged in, {who}")
            else:
                cost = f": {', '.join(used)} can't run" if used else ""
                self.add(g, f"receiver {d.id}", FAIL if used else WARN,
                         f"serial {d.serial} isn't on USB{cost}",
                         f"plug it in and {ALLOW_ACCESSORY}; the usb line lists the serials "
                         "macOS sees")  # fmt: skip

    # -- disk ----------------------------------------------------------------------------

    def check_disk(self) -> None:
        from wavescribe.system import tree_bytes

        g = "disk"
        data = self.data_dir
        probe = data
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent  # not created yet: the filesystem it will be on
        free = self.disk_usage(probe).free
        r = self.settings.get("retention") or {}
        s = self.serve
        audio_on = not s.no_store and s.audio_days > 0 and s.audio_max_gb > 0
        audio_on = audio_on and float(r.get("audio_days", s.audio_days)) > 0
        audio_cap = float(r.get("audio_max_gb", s.audio_max_gb)) * GB
        text_days = float(r.get("text_days", s.text_days if s.text_days > 0 else 0))
        stations = len(self._stations(transcribed=True))
        audio_now = tree_bytes(data / "audio") or 0
        db_now = sum(p.stat().st_size for p in data.glob("wavescribe.db*") if p.is_file())
        audio_more = max(0.0, audio_cap - audio_now) if audio_on else 0.0
        text_more: float | None = 0.0
        if not s.no_store and stations:
            rate = TEXT_MB_PER_STATION_DAY * stations * MB
            text_more = max(0.0, rate * text_days - db_now) if text_days > 0 else None
        models_more = sum(self.missing.values()) * GB
        need_now = models_more + HEADROOM_GB * GB
        need_all = need_now + audio_more + (text_more or 0.0)
        parts = []
        if audio_more:
            parts.append(f"speech audio {size_text(audio_more)}")
        if text_more:
            parts.append(f"text ~{size_text(text_more)} over {text_days:g} days for "
                         f"{stations} station{'s' * (stations != 1)}")  # fmt: skip
        if models_more:
            parts.append(f"models to download {size_text(models_more)}")
        room = f"; up to {size_text(need_all - HEADROOM_GB * GB)} more by the limits ("
        room = room + ", ".join(parts) + ")" if parts else ""
        details = {"free_gb": round(free / GB, 2), "audio_mb": round(audio_now / MB, 1),
                   "db_mb": round(db_now / MB, 1), "text_days": text_days,
                   "audio_max_gb": round(audio_cap / GB, 2) if audio_on else 0}  # fmt: skip
        head = f"{free / GB:.1f} GB free"
        if free < need_now:
            self.add(
                g,
                "space",
                FAIL,
                f"only {head}{room}",
                "free some space first (Wavescribe needs 1 GB to spare), or download fewer models",
                **details,
            )
        elif free < need_all:
            self.add(g, "space", WARN, f"{head}, not enough for the limits{room}", "free some "
                     "space, or lower the limits under Settings → Storage and privacy (How "
                     "long it's kept)", **details)  # fmt: skip
        elif text_more is None:
            self.add(g, "space", WARN, f"{head}; text is kept with no age limit (text days 0), "
                     "so it grows without bound", "set how long text is kept under Settings → "
                     "Storage and privacy", **details)  # fmt: skip
        else:
            self.add(g, "space", OK, head + room, **details)

        bad = [p for p in (data, data / "logs", data / "audio", data / "wavescribe.db")
               if p.exists() and not os.access(p, os.W_OK)]  # fmt: skip
        if not data.exists():
            parent_ok = os.access(data.parent, os.W_OK)
            self.add(g, "data folder", OK if parent_ok else FAIL,
                     f"{short(data)} will be created at the first start" if parent_ok else
                     f"{short(data.parent)} isn't writable, so data/ can't be created",
                     "" if parent_ok else "run Wavescribe from a folder you own")  # fmt: skip
        elif bad:
            self.add(g, "data folder", FAIL, f"can't write to {', '.join(map(short, bad))}",
                     f"they should be yours: ls -ld {short(bad[0])}")  # fmt: skip
        else:
            self.add(g, "data folder", OK, f"{short(data)} is writable")

    # -- the page's port -----------------------------------------------------------------

    def _port_owner(self, port: int) -> tuple[int, int | None, str] | None:
        """Who listens on the port: (pid, parent pid, command line), from lsof and ps."""
        pid = parse_lsof_pid(self.host.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN",
                                            "-Fp"], timeout=10))  # fmt: skip
        if pid is None:
            return None
        out = (self.host.run(["ps", "-o", "ppid=,command=", "-p", str(pid)]) or "").strip()
        ppid, _, command = out.partition(" ")
        return pid, int(ppid) if ppid.isdigit() else None, command.strip()

    def check_port(self) -> None:
        from wavescribe.web.server import PortInUseError

        g = "page"
        host = "0.0.0.0" if self.serve.lan else self.serve.host
        port = self.serve.port
        owner = self._port_owner(port) if self.host.platform == "darwin" else None
        if owner is not None:
            pid, ppid, command = owner
            service = self.service_status()
            if "wavescribe" in command:
                if service and service.get("pid") in (pid, ppid):
                    self.add(g, "port", OK, f"{port}: the background service serves the page "
                             f"(pid {pid})")  # fmt: skip
                else:
                    self.add(g, "port", WARN, f"{port} is in use by another Wavescribe (pid "
                             f"{pid}), so a second serve on it won't start", "that one is "
                             "already running: use its page, stop it, or start this one with "
                             "another --port", pid=pid, command=command)  # fmt: skip
            else:
                said = program(command)[:60] or "another program"
                self.add(g, "port", FAIL, f"{port} is in use by {said} (pid {pid}), so serve "
                         "won't start", "choose another port with "
                         "--port, or stop that program", pid=pid, command=command)  # fmt: skip
            return
        try:
            self.port_probe(host, port)
        except PortInUseError as e:
            self.add(g, "port", FAIL, str(e), "choose another port with --port")
        except OSError as e:
            self.add(g, "port", FAIL, f"can't use port {port} on {host}: {e}",
                     "choose another port with --port")  # fmt: skip
        else:
            where = " on every network interface (--lan)" if self.serve.lan else ""
            self.add(g, "port", OK, f"{port} is free{where}")

    # -- spoken alerts -------------------------------------------------------------------

    def check_voices(self) -> None:
        g = "voices"
        alerts = self.alert_config
        if alerts is None:
            self.add(g, "voices", SKIP, "alerts are off or don't load")
            return
        if not alerts.voices:
            self.add(g, "voices", SKIP, "no spoken alerts set up ([speak.<language>])")
            return
        spoken = {a.split(":", 1)[1] for acts in alerts.actions.values() for a in acts
                  if a.startswith("speak:")}  # fmt: skip
        if self.host.platform != "darwin" or not self.host.which("say"):
            self.add(g, "voices", WARN if spoken else SKIP, "spoken alerts need macOS's `say`",
                     "use notifications or webhooks here instead" if spoken else "")  # fmt: skip
            return
        listing = self.host.run(["say", "-v", "?"], timeout=15)
        if not listing:
            self.add(g, "voices", WARN, "couldn't list the installed voices (say -v ?)")
            return
        voices = parse_voice_list(listing)
        for lang, voice in sorted(alerts.voices.items()):
            unused = "" if lang in spoken else " (no severity speaks it yet)"
            if voice.voice in voices:
                self.add(g, f"voice {lang}", OK, f"{voice.voice} is installed{unused}")
                continue
            primary = lang.split("-")[0].lower()
            same = sorted({n for n, loc in voices.items()
                           if loc.split("_")[0] == primary and "(" not in n})  # fmt: skip
            hint = f"; installed for {lang}: {', '.join(same[:5])}" if same else ""
            self.add(g, f"voice {lang}", FAIL if lang in spoken else WARN,
                     f"{voice.voice!r} isn't installed, so spoken {lang} alerts would fail"
                     f"{unused}{hint}", f"download it in {VOICES_WHERE}, or choose an installed "
                     "voice under Settings → Alerts")  # fmt: skip

    # -- running unattended --------------------------------------------------------------

    def service_status(self) -> dict[str, Any] | None:
        """The background service's status, read once; None where there is no launchd."""
        if self._service is False:
            from wavescribe import service

            self._service = None
            if self._launchd is not None or self.host.platform == "darwin":
                try:
                    launchd = self._launchd or service.default_launchd()
                    self._service = service.status(launchd, self.home)
                except service.ServiceError:
                    self._service = None
        return self._service  # type: ignore[return-value]

    def check_unattended(self) -> None:
        g = "unattended"
        st = self.service_status()
        if st is None:
            self.add(g, "service", SKIP, "the background service needs macOS (launchd)")
        elif not st["installed"] and not st["loaded"]:
            self.add(
                g,
                "service",
                SKIP,
                "not installed: `wavescribe service install` runs "
                "serve in the background, restarts it after a crash, keeps the Mac awake",
            )
        elif st["running"]:
            args = " ".join(st["serve_args"] or [])
            runs = st["runs"] or 0
            restarted = f", started {runs} times since it was loaded" if runs > 1 else ""
            self.add(g, "service", OK, f"running (pid {st['pid']}){restarted}: serve {args}"
                     .rstrip(), log=st["log"])  # fmt: skip
        elif st["loaded"]:
            last = st["last_exit"] or "unknown"
            self.add(g, "service", FAIL, f"installed but not running (last exit {last})",
                     f"read {short(st['log'])}, fix what it says, then run "
                     "`wavescribe service install` again", log=st["log"])  # fmt: skip
        else:
            self.add(g, "service", FAIL, "installed but not loaded, so it won't start",
                     "wavescribe service install")  # fmt: skip
        if st and st["workdir"] and Path(st["workdir"]).resolve() != self.workdir.resolve():
            self.add(g, "service folder", WARN, f"the service runs in {short(st['workdir'])}, "
                     "not in this folder, so these checks are of another setup",
                     f"run doctor in {short(st['workdir'])}")  # fmt: skip
        if st and (st["log_bytes"] or 0) > SERVICE_LOG_WARN_BYTES:
            self.add(g, "service log", WARN, f"{short(st['log'])} is "
                     f"{st['log_bytes'] / MB:.0f} MB", "`wavescribe service install` moves it "
                     "aside")  # fmt: skip

        if self.host.platform != "darwin":
            return
        settings = self.host.run(["pmset", "-g"]) or ""
        laptop = "InternalBattery" in (self.host.run(["pmset", "-g", "batt"]) or "")
        installed = bool(st and st["installed"])
        if re.search(r"^\s*SleepDisabled\s+1\b", settings, re.M):
            self.add(g, "sleep", OK, "switched off (pmset disablesleep): the Mac stays on, even "
                     "with the lid closed")  # fmt: skip
        elif laptop and installed:
            self.add(g, "sleep", WARN, "a closed lid puts this MacBook to sleep, and "
                     "Wavescribe with it; the service only prevents idle sleep",
                     "keep the lid open, or, for a Mac that is always on: sudo pmset -a "
                     "disablesleep 1 (sudo pmset -a disablesleep 0 undoes it)")  # fmt: skip
        elif laptop:
            self.add(
                g,
                "sleep",
                OK,
                "the background service keeps the Mac from idle sleep "
                "while it runs (by hand: caffeinate -i); a closed lid still sleeps",
            )
        else:
            self.add(g, "sleep", OK, "the background service keeps the Mac from idle sleep "
                     "while it runs")  # fmt: skip


# -- output ----------------------------------------------------------------------------------


def verdict(checks: Sequence[Check]) -> dict[str, Any]:
    problems = sum(c.status == FAIL for c in checks)
    warnings = sum(c.status == WARN for c in checks)
    status = FAIL if problems else WARN if warnings else OK
    return {"type": "summary", "status": status, "problems": problems, "warnings": warnings}


_COLOURS = {OK: "\033[32m", WARN: "\033[33m", FAIL: "\033[1;31m", SKIP: "\033[2m"}


def render(checks: Sequence[Check], *, tty: bool = False) -> list[str]:
    lines: list[str] = []
    width = max((len(c.name) for c in checks), default=0) + 2
    group = None
    for c in checks:
        if c.group != group:
            group = c.group
            lines += ["", GROUPS.get(group, group)]
        word = (c.status.upper() if c.status == FAIL else c.status).ljust(4)
        if tty:
            word = f"{_COLOURS[c.status]}{word}\033[0m"
        lines.append(f"  {word}  {c.name.ljust(width)}{c.summary}")
        if c.fix and c.status in (WARN, FAIL):
            lines.append(f"  {'':4}  {'':{width}}→ {c.fix}")
    v = verdict(checks)
    lines.append("")
    if v["problems"]:
        warned = f", and {v['warnings']} warning{'s' * (v['warnings'] != 1)}"
        warned = warned if v["warnings"] else ""
        lines.append(f"{v['problems']} problem{'s' * (v['problems'] != 1)} to fix (FAIL)"
                     f"{warned}: each has its next step under it.")  # fmt: skip
    elif v["warnings"]:
        lines.append(f"Nothing broken; {v['warnings']} warning{'s' * (v['warnings'] != 1)} "
                     "worth a look.")  # fmt: skip
    else:
        lines.append("All good: nothing to fix.")
    return lines


def options_to_check(args: argparse.Namespace, home: Path | None = None
                     ) -> tuple[list[str], bool]:  # fmt: skip
    """The serve options to check: those given, else the installed service's (when it runs in
    this folder), else serve's defaults. Returns (options, whether they are the service's)."""
    from wavescribe import service

    given = service.serve_argv(args.serve_parser, args)
    if given or sys.platform != "darwin":
        return given, False
    plist = service.read_plist(home)
    workdir = (plist or {}).get("WorkingDirectory")
    theirs = service.serve_args_of(plist)
    if theirs is None or not workdir or Path(workdir).resolve() != Path.cwd().resolve():
        return given, False
    return theirs, True


def cmd_doctor(args: argparse.Namespace) -> int:
    from wavescribe import __version__, service
    from wavescribe.events import to_json

    options, from_service = options_to_check(args)
    try:
        serve = service.check_serve_args(options)
    except SystemExit:  # an installed service with options serve no longer takes
        print(f"wavescribe: the background service's options don't parse: {' '.join(options)}",
              file=sys.stderr)  # fmt: skip
        options, from_service = [], False
        serve = service.check_serve_args([])
    checks = Doctor(serve).run()
    summary = verdict(checks)
    if args.json:
        for c in checks:
            print(to_json(c.to_dict()))
        print(to_json({**summary, "version": __version__, "folder": str(Path.cwd()),
                       "options": options, "from_service": from_service}))  # fmt: skip
    else:
        print(f"Wavescribe {__version__} doctor, in {short(Path.cwd())}")
        if from_service:
            print(f"Checking the background service's options: serve {' '.join(options)}"
                  .rstrip())  # fmt: skip
        elif options:
            print(f"Checking: serve {' '.join(options)}")
        print("\n".join(render(checks, tty=sys.stdout.isatty())))
    return 1 if summary["problems"] else 0


def add_parser(sub: Any) -> None:
    """``doctor``. It takes serve's options (copied from serve's parser, which must already be
    on ``sub``), to check what ``serve`` with them needs."""
    serve = sub.choices["serve"]
    p = sub.add_parser(
        "doctor", parents=[serve], conflict_handler="resolve",
        help="check what Wavescribe needs, offline, and say what to fix",
        description="Check tools, Python, models, receivers, disk, configuration, the page's "
        "port, voices and the background service, offline and without opening any receiver. "
        "Takes serve's options, to check what `serve` with them needs; with none, checks the "
        "options of the installed background service. Exit code 1 when something is broken.",
    )  # fmt: skip
    from wavescribe.cli import add_json_arg  # cli is loaded: it is building its parser

    add_json_arg(p)  # a line per check, then a summary
    p.set_defaults(func=cmd_doctor, serve_parser=serve)
