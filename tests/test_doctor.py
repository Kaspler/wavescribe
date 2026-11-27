# SPDX-License-Identifier: Apache-2.0
"""`wavescribe doctor`: each check against a fake machine (commands, launchd, disk, models).
Nothing here opens a receiver, runs launchctl or reaches the network."""

import json
import logging
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_agent import CHECK_KEYS, SUMMARY_KEYS
from tests.test_service import FakeLaunchd
from wavescribe import doctor, service
from wavescribe.alerts.actions import parse_voices
from wavescribe.cli import main
from wavescribe.config import ConfigError, load_stations
from wavescribe.doctor import FAIL, GB, OK, SKIP, WARN, Doctor
from wavescribe.registry import Licenses, ModelEntry, ModelRegistry
from wavescribe.web.server import PortInUseError

STATIONS = """\
[defaults]
language = "es-ES"
translate_to = ["en"]

[[device]]
id = "a"
serial = "00001234"
center_mhz = 98.0
gain_db = 20

[[station]]
id = "uno"
stream = "http://radio.test/uno"

[[station]]
id = "dos"
device = "a"
fm_mhz = [98.3]
"""

ALERTS = """\
[speak.en]
voice = "Samantha"
fallback = "Attention."

[speak.es]
voice = "{es_voice}"
fallback = "Atención."

[actions]
critical = ["notify", "speak:es", "log"]
"""

# `ioreg -p IOUSB -l -w 0`, shortened: a hub, two RTL-SDR dongles and a keyboard.
IOREG = """\
+-o Root  <class IORegistryEntry, id 0x100000100, retain 30>
  +-o USB2.0 Hub@00100000  <class IOUSBHostDevice, id 0x100001, registered, matched, active>
  | | {
  | |   "idProduct" = 1154
  | |   "USB Product Name" = "USB2.0 Hub"
  | |   "idVendor" = 5296
  | | }
  | +-o Blog V4L@00110000  <class IOUSBHostDevice, id 0x100002, registered, matched, active>
  | |   {
  | |     "idProduct" = 10296
  | |     "iSerialNumber" = 3
  | |     "USB Product Name" = "Blog V4L"
  | |     "kUSBSerialNumberString" = "{first}"
  | |     "USB Vendor Name" = "RTLSDRBlog"
  | |     "IOPowerManagement" = {"DevicePowerState"=2,"CurrentPowerState"=2}
  | |     "idVendor" = 3034
  | |     "USB Serial Number" = "{first}"
  | |   }
  | +-o RTL2838UHIDIR@00120000  <class IOUSBHostDevice, id 0x100003, registered, matched>
  |     {
  |       "idProduct" = 10296
  |       "USB Product Name" = "RTL2838UHIDIR"
  |       "USB Vendor Name" = "Realtek"
  |       "idVendor" = 3034
  |       "USB Serial Number" = "00005678"
  |     }
  +-o Keyboard@00130000  <class IOUSBHostDevice, id 0x100004, registered, matched, active>
      {
        "idProduct" = 615
        "USB Product Name" = "Keyboard"
        "idVendor" = 1452
        "USB Serial Number" = "ABCD0000"
      }
"""

SAY = """\
Samantha            en_US    # Hello! My name is Samantha.
Daniel              en_GB    # Hello! My name is Daniel.
Paulina             es_MX    # Hola, me llamo Paulina.
Eddy (English (UK)) en_GB    # Hello! My name is Eddy.
"""


class FakeHost:
    """Commands answered from a table, keyed by their first words. Receivers are never opened:
    running an rtl tool fails the test."""

    platform = "darwin"
    machine = "arm64"

    def __init__(self, tools: dict[str, str], outputs: dict[tuple[str, ...], str]) -> None:
        self.tools = tools
        self.outputs = outputs
        self.ran: list[list[str]] = []

    def which(self, name: str) -> str | None:
        return self.tools.get(name)

    def run(self, args, timeout: float = 5.0) -> str | None:
        assert Path(args[0]).name not in doctor.NEVER_RUN, args
        self.ran.append(list(args))
        for key, out in self.outputs.items():
            if tuple(args[: len(key)]) == key:
                return out
        return None


def tools(root: Path) -> dict[str, str]:
    """ffmpeg, and rtl tools linked from a Homebrew-like Cellar, as files that exist."""
    out = {"say": "/usr/bin/say", "ffmpeg": str(root / "bin" / "ffmpeg")}
    (root / "bin").mkdir(parents=True, exist_ok=True)
    for name in ("rtl_sdr", "rtl_power"):
        real = root / "Cellar" / "librtlsdr" / "2.0.3" / "bin" / name
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("#!/bin/sh\nexit 99\n")
        real.chmod(0o755)
        link = root / "bin" / name
        link.unlink(missing_ok=True)
        link.symlink_to(real)
        out[name] = str(link)
    return out


def outputs(*, ioreg: str | None = None, say: str = SAY, lsof: str | None = None,
            ps: str | None = None, sleep_disabled: bool = False) -> dict:  # fmt: skip
    return {
        ("ffmpeg",): "ffmpeg version 7.1.1 Copyright (c) 2000-2025 the FFmpeg developers\n",
        ("ioreg",): IOREG.replace("{first}", "00001234") if ioreg is None else ioreg,
        ("say",): say,
        ("lsof",): lsof,
        ("ps",): ps,
        ("pmset", "-g", "batt"): " -InternalBattery-0 (id=1)\t100%; charged; present: true\n",
        ("pmset", "-g"): f" sleep  1\n SleepDisabled\t\t{int(sleep_disabled)}\n",
    }


def registry(root: Path, installed=("vad", "gate", "asr", "mt"), log: str | None = None):
    def entry(mid: str, capability: str, languages=("*",), size: float = 1.0) -> ModelEntry:
        return ModelEntry(mid, capability, "fake", f"local:{root / 'models' / mid}", "u", "MIT",
                          "u", "default", False, languages, "", size_gb=size)  # fmt: skip

    entries = [entry("vad", "vad", size=0.002), entry("gate", "gate", size=0.004),
               entry("asr", "asr", ("es", "en")), entry("mt", "translate", size=5.0)]  # fmt: skip
    for e in entries:
        if e.id in installed:
            Path(e.location).mkdir(parents=True, exist_ok=True)
    chains = {"vad": ["vad"], "gate": ["gate"], "asr": ["asr"], "translate": ["mt"]}

    def load() -> ModelRegistry:
        if log:
            logging.getLogger("wavescribe.registry").warning(log)
        return ModelRegistry(entries, chains, root / "models", Licenses(root / "accepted.json"))

    return load


@pytest.fixture(autouse=True)
def no_real_launchd(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("a test tried to reach the real launchd")

    monkeypatch.setattr(service, "_run", refuse)
    monkeypatch.setattr(service, "default_launchd", refuse)


@pytest.fixture
def folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Wavescribe folder with two stations (a stream, and one on receiver a) and alerts."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "stations.toml").write_text(STATIONS)
    (tmp_path / "config" / "alerts.toml").write_text(ALERTS.format(es_voice="Paulina"))
    (tmp_path / "home").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def run(root: Path, *serve: str, host=None, reg=None, launchd=None, probe=None,
        free_gb: float = 500.0) -> dict[str, list[doctor.Check]]:  # fmt: skip
    checks = Doctor(
        service.check_serve_args(list(serve)),
        host=host or FakeHost(tools(root), outputs()),
        registry=reg or registry(root),
        launchd=launchd or FakeLaunchd(),
        home=root / "home",
        port_probe=probe or (lambda _h, _p: None),
        disk_usage=lambda _p: SimpleNamespace(free=free_gb * GB, total=1000 * GB),
        workdir=root,
    ).run()
    by_name: dict[str, list[doctor.Check]] = {}
    for c in checks:
        by_name.setdefault(c.name, []).append(c)
    return by_name


def status(found: dict[str, list[doctor.Check]], name: str) -> str:
    return found[name][0].status


def test_a_machine_with_everything_passes(folder: Path) -> None:
    host = FakeHost(tools(folder), outputs())
    found = run(folder, host=host)
    problems = [c for cs in found.values() for c in cs if c.status in (WARN, FAIL)]
    assert problems == [] or all(c.group == "environment" for c in problems), problems
    assert found["ffmpeg"][0].summary.startswith("7.1.1")
    assert found["rtl_sdr"][0].summary.startswith("librtlsdr 2.0.3")
    assert "not started" in found["rtl_sdr"][0].summary
    assert found["speech es-ES"][0].summary == "asr for uno, dos"
    assert found["translation"][0].summary == "mt into en"
    assert "00001234 (Blog V4L)" in found["usb"][0].summary
    assert "00005678" in found["usb"][0].summary  # plugged in, not used yet
    assert found["receiver a"][0].summary == "serial 00001234 is plugged in, for dos"
    assert (
        status(found, "voice es") == OK and "no severity speaks it" in found["voice en"][0].summary
    )
    assert status(found, "port") == OK and status(found, "service") == SKIP
    assert status(found, "alerts") == OK and status(found, "stations") == OK
    assert "0 keyword, 0 LLM and 0 station rules" in found["alerts"][0].summary
    # Read-only: what was run, and never a receiver tool (FakeHost.run asserts that too).
    assert {c[0] for c in host.ran} <= {"ffmpeg", "ioreg", "say", "lsof", "pmset"}


def test_missing_tools_models_and_receiver_each_say_what_to_do(folder: Path) -> None:
    host = FakeHost({"say": "/usr/bin/say"}, outputs(ioreg="+-o Root  <class IORegistryEntry>\n"))
    found = run(folder, host=host, reg=registry(folder, installed=()), free_gb=3)
    expect = {
        "ffmpeg": (FAIL, "brew install ffmpeg"),
        "rtl_sdr": (FAIL, "brew install librtlsdr"),  # station dos listens on a receiver
        "rtl_power": (WARN, "brew install librtlsdr"),
        "speech detector": (FAIL, "wavescribe models fetch vad (2 MB)"),
        "music filter": (WARN, "wavescribe models fetch gate"),
        "speech es-ES": (FAIL, "wavescribe models fetch asr (1.0 GB)"),
        "translation": (FAIL, "wavescribe models fetch mt (5.0 GB)"),
        "usb": (FAIL, "Allow accessories to connect"),
        "receiver a": (FAIL, "Allow accessories to connect"),
        "space": (FAIL, "free some space"),  # 6 GB of models to download, 3 GB free
    }
    for name, (want, fix) in expect.items():
        check = found[name][0]
        assert check.status == want, (name, check)
        assert fix in check.fix, (name, check.fix)
    assert "dos can't run" in found["receiver a"][0].summary
    assert "models to download 6.0 GB" in found["space"][0].summary


def test_an_opt_in_model_gets_the_command_that_accepts_its_licence(folder: Path) -> None:
    entry = ModelEntry("asr-x", "asr", "fake", f"local:{folder / 'x'}", "u", "Some-Terms",
                       "https://terms", "opt-in", False, ("es",), "")  # fmt: skip

    def load() -> ModelRegistry:
        base = registry(folder)()
        return ModelRegistry([*base.entries(), entry], {**base.chains(), "asr": ["asr-x"]},
                             folder / "models", Licenses(folder / "accepted.json"))  # fmt: skip

    found = run(folder, reg=load)
    assert status(found, "speech es-ES") == FAIL
    assert "wavescribe models accept asr-x --license 'Some-Terms'" in found["speech es-ES"][0].fix


def test_receivers_are_read_from_the_usb_registry() -> None:
    dongles = doctor.parse_usb_dongles(IOREG.replace("{first}", "00001234"))
    assert [(d["serial"], d["product"], d["usb_id"]) for d in dongles] == [
        ("00001234", "Blog V4L", "0bda:2838"),
        ("00005678", "RTL2838UHIDIR", "0bda:2838"),
    ]


def test_two_dongles_with_one_serial_are_flagged(folder: Path) -> None:
    host = FakeHost(tools(folder), outputs(ioreg=IOREG.replace("{first}", "00005678")))
    found = run(folder, host=host)
    assert status(found, "usb") == WARN and "rtl_eeprom -s" in found["usb"][0].fix
    assert status(found, "receiver a") == FAIL  # 00001234 isn't plugged in, and dos needs it


def test_config_errors_are_the_ones_serve_stops_with(folder: Path) -> None:
    stations = folder / "config" / "stations.toml"
    stations.write_text('[[station]]\nid = "uno"\nlanguage = [\n')
    with pytest.raises(ConfigError) as serve_says:
        load_stations(Path("config/stations.toml"))
    found = run(folder)
    assert status(found, "stations") == FAIL
    assert found["stations"][0].summary == str(serve_says.value)
    assert status(found, "models") == SKIP and status(found, "voices") == SKIP

    stations.write_text(STATIONS)
    (folder / "config" / "ui.toml").write_text("[overrides.uno\n")
    found = run(folder)
    assert status(found, "page changes") == FAIL and "ui.toml" in found["page changes"][0].summary

    (folder / "config" / "ui.toml").write_text("[overrides.dos]\nfm_mhz = [104.0]\n")
    found = run(folder, "missing")
    assert status(found, "page changes") == WARN  # the page moved dos off its receiver
    assert (
        status(found, "station") == FAIL
        and "unknown station 'missing'" in found["station"][0].summary
    )

    (folder / "config" / "ui.toml").unlink()
    (folder / "config" / "alerts.toml").write_text('[actions]\ncritical = ["speak:fr"]\n')
    found = run(folder)
    assert status(found, "alerts") == FAIL and "speak.fr" in found["alerts"][0].summary
    assert status(found, "voices") == SKIP
    assert status(run(folder, "--no-alerts"), "alerts") == SKIP


@pytest.mark.parametrize(
    ("ps", "service_pid", "want", "said"),
    [
        ("1 /usr/bin/python3 serve.py", None, FAIL, "python3 serve.py (pid 777)"),
        (
            "4000 /ws/.venv/bin/python /ws/.venv/bin/wavescribe serve",
            None,
            WARN,
            "another Wavescribe",
        ),
        ("4242 /ws/.venv/bin/python -m wavescribe serve", 4242, OK, "background service"),
    ],
)
def test_the_port_says_who_holds_it(folder: Path, ps, service_pid, want, said) -> None:
    host = FakeHost(tools(folder), outputs(lsof="p777\n", ps=ps))
    launchd = FakeLaunchd(starts=service_pid is not None)
    if service_pid:
        job = service.Job(folder, "/ws/.venv/bin/python", (), {})
        service.install(job, launchd, folder / "home", sleep=lambda _s: None)
    found = run(folder, host=host, launchd=launchd)
    assert status(found, "port") == want and said in found["port"][0].summary


def test_a_port_held_where_lsof_cant_see_is_found_by_serves_own_probe(folder: Path) -> None:
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))  # an ephemeral port: nothing real is touched
        busy.listen()
        port = busy.getsockname()[1]
        found = run(folder, "--port", str(port), probe=service.port_probe)
    assert status(found, "port") == FAIL
    assert f"port {port} is already in use" in found["port"][0].summary

    def refuse(host: str, port: int) -> None:
        raise PortInUseError(f"port {port} is already in use on {host} by another program")

    found = run(folder, "--lan", "--port", "8793", probe=refuse)
    assert "on 0.0.0.0" in found["port"][0].summary and "--port" in found["port"][0].fix


def test_a_spoken_voice_that_isnt_installed_fails_with_the_ones_that_are(folder: Path) -> None:
    (folder / "config" / "alerts.toml").write_text(ALERTS.format(es_voice="Mónica"))
    found = run(folder)
    check = found["voice es"][0]
    assert check.status == FAIL and "'Mónica' isn't installed" in check.summary
    assert "installed for es: Paulina" in check.summary and "Manage voices" in check.fix
    assert status(found, "voice en") == OK  # installed; no severity speaks it


def test_names_from_say_match_what_alerts_accept() -> None:
    assert parse_voices(SAY) <= set(doctor.parse_voice_list(SAY))
    assert doctor.parse_voice_list(SAY)["Paulina"] == "es_MX"


def test_the_disk_is_weighed_against_the_limits(folder: Path) -> None:
    # Two stations: ~5.5 GB of text over 365 days, 2 GB of speech audio, 1 GB to spare.
    assert status(run(folder, free_gb=50), "space") == OK
    assert status(run(folder, free_gb=6), "space") == WARN
    assert status(run(folder, free_gb=0.5), "space") == FAIL
    assert status(run(folder, "--no-store", free_gb=2), "space") == OK
    (folder / "config" / "ui.toml").write_text("[retention]\ntext_days = 0\n")
    found = run(folder, free_gb=50)
    assert status(found, "space") == WARN and "no age limit" in found["space"][0].summary
    assert "will be created" in found["data folder"][0].summary
    (folder / "data").mkdir()
    assert run(folder)["data folder"][0].summary.endswith("is writable")


def test_the_background_service_and_sleep(folder: Path) -> None:
    launchd = FakeLaunchd()
    found = run(folder, launchd=launchd)
    assert status(found, "service") == SKIP and status(found, "sleep") == OK
    job = service.Job(folder, "/ws/.venv/bin/python", ("--lan",), {})
    service.install(job, launchd, folder / "home", sleep=lambda _s: None)
    found = run(folder, launchd=launchd)
    assert status(found, "service") == OK and "serve --lan" in found["service"][0].summary
    assert status(found, "sleep") == WARN and "disablesleep 1" in found["sleep"][0].fix
    launchd.pid, launchd.last_exit = None, "1"
    found = run(folder, launchd=launchd)
    assert status(found, "service") == FAIL and "service.log" in found["service"][0].fix
    host = FakeHost(tools(folder), outputs(sleep_disabled=True))
    assert status(run(folder, host=host, launchd=launchd), "sleep") == OK


def test_warnings_logged_by_a_check_and_a_check_that_breaks_show_up(folder: Path) -> None:
    found = run(folder, reg=registry(folder, log="config/models.catalog.toml: odd entry"))
    assert found["log"][0].status == WARN and "odd entry" in found["log"][0].summary

    def broken() -> ModelRegistry:
        raise RuntimeError("boom")

    found = run(folder, reg=broken)
    assert status(found, "doctor") == FAIL and "RuntimeError: boom" in found["doctor"][0].summary
    assert status(found, "port") == OK  # the other checks still ran


def test_the_real_host_never_runs_receiver_tools() -> None:
    for tool in ("rtl_test", "rtl_sdr", "/opt/homebrew/bin/rtl_power"):
        with pytest.raises(ValueError, match="never runs"):
            doctor.LocalHost().run([tool, "-d", "0"])


def test_the_report_and_its_verdict() -> None:
    checks = [doctor.Check("tools", "ffmpeg", FAIL, "not found", "brew install ffmpeg"),
              doctor.Check("disk", "space", WARN, "low", "free some"),
              doctor.Check("disk", "data folder", OK, "writable")]  # fmt: skip
    lines = doctor.render(checks)
    assert "Tools" in lines and "Disk" in lines
    assert any("FAIL" in line and "not found" in line for line in lines)
    assert any("→ brew install ffmpeg" in line for line in lines)
    assert lines[-1].startswith("1 problem to fix (FAIL), and 1 warning")
    assert doctor.verdict(checks)["status"] == FAIL
    assert doctor.render(checks[2:])[-1] == "All good: nothing to fix."


def test_the_command_checks_the_installed_services_options_and_speaks_json(
    folder: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    launchd, probed = FakeLaunchd(), []
    job = service.Job(folder, "/ws/.venv/bin/python", ("--port", "8793", "--no-alerts"), {})
    service.install(job, launchd, None, sleep=lambda _s: None)  # into the temporary HOME
    monkeypatch.setattr(service, "default_launchd", lambda: launchd)
    monkeypatch.setattr(service, "port_probe", lambda h, p: probed.append((h, p)))
    monkeypatch.setattr(doctor, "LocalHost", lambda: FakeHost(tools(folder), outputs()))
    monkeypatch.setattr(ModelRegistry, "load", staticmethod(registry(folder)))

    code = main(["doctor", "--json"])
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    *checks, summary = lines
    assert set(summary) == SUMMARY_KEYS and all(set(c) == CHECK_KEYS for c in checks)
    assert summary["type"] == "summary" and summary["from_service"] is True
    assert summary["options"] == ["--port", "8793", "--no-alerts"]
    assert code == (1 if summary["problems"] else 0)
    assert all(c["type"] == "check" and c["status"] in (OK, WARN, FAIL, SKIP) for c in checks)
    port = next(c for c in checks if c["check"] == "port")
    assert port["status"] == OK and probed == [("127.0.0.1", 8793)]
    assert next(c for c in checks if c["check"] == "alerts")["status"] == SKIP

    # Options given on the command line win over the service's.
    assert main(["doctor", "--port", "8794"]) in (0, 1)
    out = capsys.readouterr().out
    assert "Checking: serve --port 8794" in out and probed[-1] == ("127.0.0.1", 8794)
