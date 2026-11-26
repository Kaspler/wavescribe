# SPDX-License-Identifier: Apache-2.0
"""ResourceMonitor: parsers against synthetic command output, rates with a fake clock, and a
smoke test on macOS. Samples are shaped like real output with made-up values and no serials."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from wavescribe import system
from wavescribe.system import (
    SECTIONS,
    ResourceMonitor,
    cpu_busy_percent,
    memory_from_vm_stat,
    parse_battery,
    parse_gpu,
    parse_netstat,
    parse_pressure,
    parse_swap_used,
    parse_sysctl,
    parse_vm_stat,
    physical,
    tree_bytes,
)

GB = 1024**3

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                                   200000.
Pages active:                                1500000.
Pages inactive:                              1500000.
Pages speculative:                             20000.
Pages throttled:                                   0.
Pages wired down:                             300000.
Pages purgeable:                               50000.
"Translation faults":                     2794006020.
Pages copy-on-write:                        28929077.
Pages zero filled:                        2095622745.
Pages reactivated:                          23590485.
Pages purged:                                8299709.
File-backed pages:                           1020000.
Anonymous pages:                             2000000.
Pages stored in compressor:                  1100000.
Pages occupied by compressor:                 500000.
Decompressions:                             16787116.
Compressions:                               19988430.
Pageins:                                    31473774.
Pageouts:                                      19770.
Swapins:                                      563571.
Swapouts:                                     743285.
"""

SYSCTL = """\
vm.swapusage: total = 2048.00M  used = 1255.25M  free = 792.75M  (encrypted)
kern.memorystatus_vm_pressure_level: 2
"""

IOACCELERATOR = """\
+-o AGXAcceleratorG13X  <class AGXAcceleratorG13X, id 0x100000abc, registered, matched, active, \
busy 0 (1000 ms), retain 90>
    {
      "IOClass" = "AGXAcceleratorG13X"
      "PerformanceStatistics" = {"In use system memory (driver)"=0,"Alloc system memory"=\
17922375680,"Tiler Utilization %"=12,"recoveryCount"=0,"lastRecoveryTime"=0,"Renderer \
Utilization %"=31,"TiledSceneBytes"=1540096,"Device Utilization %"=33,"SplitSceneCount"=0,\
"Allocated PB Size"=83755008,"In use system memory"=2147483648}
      "model" = "Apple M1 Max"
      "gpu-core-count" = 32
    }
"""

BATTERY_ON_AC = """\
+-o AppleSmartBattery  <class AppleSmartBattery, id 0x100000a30, registered, matched, active, \
busy 0 (15 ms), retain 13>
    {
      "TimeRemaining" = 65535
      "AvgTimeToEmpty" = 65535
      "AdapterDetails" = {"IsWireless"=No,"AdapterID"=0,"AdapterVoltage"=20000,"FamilyCode"=\
18446744073172697098,"Watts"=94,"Current"=4700,"UsbHvcMenu"=({"Index"=0,"MaxCurrent"=3000,\
"MaxVoltage"=5000}),"Description"="pd charger"}
      "ExternalConnected" = Yes
      "Voltage" = 12556
      "PowerTelemetryData" = {"SystemEnergyConsumed"=2609,"AdapterEfficiencyLoss"=56,\
"SystemLoad"=9393,"SystemCurrentIn"=463,"SystemVoltageIn"=20286,"SystemPowerIn"=9393,\
"BatteryPower"=0}
      "MaxCapacity" = 100
      "CurrentCapacity" = 100
      "AppleRawAdapterDetails" = ({"Watts"=60,"Description"="pd charger"})
      "InstantAmperage" = 0
      "FullyCharged" = Yes
      "Amperage" = 0
      "IsCharging" = No
      "AvgTimeToFull" = 65535
    }
"""

# Discharging at 1 A: ioreg prints -1000 as an unsigned 64-bit number.
BATTERY_DISCHARGING = """\
  | {
  |   "AvgTimeToEmpty" = 245
  |   "ExternalConnected" = No
  |   "Voltage" = 11850
  |   "PowerTelemetryData" = {"SystemLoad"=11900,"SystemPowerIn"=0,"BatteryPower"=11850}
  |   "MaxCapacity" = 100
  |   "CurrentCapacity" = 62
  |   "Amperage" = 18446744073709550616
  |   "IsCharging" = No
  |   "AvgTimeToFull" = 65535
  | }
"""

NETSTAT = """\
Name       Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll
lo0        16384 <Link#1>                      46383421     0 63513998244 46383421     0 63513998244     0
lo0        16384 127           127.0.0.1       46383421     - 63513998244 46383421     - 63513998244     -
gif0*      1280  <Link#2>                             0     0          0        0     0          0     0
en0        1500  <Link#14>   02:00:00:00:00:01 350437068     0 299489668312 236815763     0 119511281587     0
en0        1500  192.0.2       192.0.2.7       350437068     - 299489668312 236815763     - 119511281587     -
en1        1500  <Link#10>   02:00:00:00:00:02      120     0      90000       80     0      40000     0
bridge0    1500  <Link#15>   02:00:00:00:00:03        0     0          0        0     0          0     0
utun21     1420  <Link#46>                     53572106     0 36019116657 41271362     0 14480996749     0
"""  # noqa: E501


def test_vm_stat_counts_memory_like_activity_monitor() -> None:
    pages = parse_vm_stat(VM_STAT)
    assert pages["page size"] == 16384
    assert pages["translation faults"] == 2794006020
    assert pages["anonymous pages"] == 2_000_000
    mem = memory_from_vm_stat(pages, 64 * GB)
    pages_per_gb = GB // 16384
    # used = anonymous - purgeable + wired + compressed
    assert mem["used_gb"] == pytest.approx((2_000_000 - 50_000 + 300_000 + 500_000) / pages_per_gb)
    assert mem["available_gb"] == pytest.approx(64 - mem["used_gb"])
    assert mem["compressed_gb"] == pytest.approx(500_000 / pages_per_gb)
    assert memory_from_vm_stat(parse_vm_stat("garbage"), 64 * GB) == {}


def test_sysctl_swap_and_pressure() -> None:
    values = parse_sysctl(SYSCTL)
    assert parse_swap_used(values["vm.swapusage"]) == int(1255.25 * 1024**2)
    assert parse_swap_used("total = 0.00M  used = 0.00M  free = 0.00M") == 0
    assert parse_swap_used("") is None
    assert parse_pressure(values["kern.memorystatus_vm_pressure_level"]) == "warn"
    assert parse_pressure("1") == "normal"
    assert parse_pressure("4") == "critical"
    assert parse_pressure(None) is None
    assert parse_pressure("x") is None


def test_gpu() -> None:
    gpu = parse_gpu(IOACCELERATOR)
    assert gpu["utilization_percent"] == 33
    assert gpu["in_use_memory_gb"] == pytest.approx(2.0)  # not the "(driver)" key
    assert gpu["allocated_memory_gb"] == pytest.approx(17922375680 / GB)
    assert parse_gpu("") == {}


def test_battery_on_ac_and_full() -> None:
    power = parse_battery(BATTERY_ON_AC)
    assert power == {
        "on_ac": True,
        "charging": False,
        "battery_percent": 100,
        "battery_watts": 0.0,
        "system_watts": pytest.approx(9.393),
        "input_watts": pytest.approx(9.393),
        "adapter_watts": 94,  # AdapterDetails, not AppleRawAdapterDetails
    }


def test_battery_discharging_has_negative_watts() -> None:
    power = parse_battery(BATTERY_DISCHARGING)
    assert power["on_ac"] is False
    assert power["battery_percent"] == 62
    assert power["minutes_remaining"] == 245
    assert power["battery_watts"] == pytest.approx(-11.85)
    assert power["system_watts"] == pytest.approx(11.9)
    assert power["input_watts"] == 0.0
    assert "adapter_watts" not in power
    # Without PowerTelemetryData the system draw is what the battery gives.
    older = "\n".join(line for line in BATTERY_DISCHARGING.splitlines() if "Telemetry" not in line)
    assert parse_battery(older)["system_watts"] == pytest.approx(11.85)


def test_battery_charging_counts_minutes_to_full() -> None:
    text = (
        BATTERY_ON_AC.replace('"IsCharging" = No', '"IsCharging" = Yes')
        .replace('"AvgTimeToFull" = 65535', '"AvgTimeToFull" = 48')
        .replace('"Amperage" = 0', '"Amperage" = 2500')
    )
    power = parse_battery(text)
    assert power["charging"] is True
    assert power["minutes_remaining"] == 48
    assert power["battery_watts"] == pytest.approx(12.556 * 2.5)


def test_no_battery() -> None:
    assert parse_battery("") == {}


def test_netstat_counts_physical_interfaces() -> None:
    counters = parse_netstat(NETSTAT)
    assert counters["lo0"] == (63513998244, 63513998244)  # blank Address column
    assert counters["en0"] == (299489668312, 119511281587)
    assert counters["gif0"] == (0, 0)
    assert sorted(k for k in counters if physical(k)) == ["en0", "en1"]
    assert parse_netstat("") == {}


def test_cpu_busy_percent_survives_counter_wrap() -> None:
    # user +30, system +10, idle +160: 40 busy ticks out of 200
    assert cpu_busy_percent((100, 50, 800, 0), (130, 60, 960, 0)) == pytest.approx(20.0)
    near = 2**32 - 10
    # user +30, system +10, idle +60 across the wrap
    assert cpu_busy_percent((near, 0, near, 0), (20, 10, 50, 0)) == pytest.approx(40.0)
    assert cpu_busy_percent((1, 2, 3, 4), (1, 2, 3, 4)) is None


def test_tree_bytes(tmp_path: Path) -> None:
    assert tree_bytes(tmp_path / "missing") is None
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "x.pcm").write_bytes(b"\0" * 1000)
    (tmp_path / "a" / "b" / "y.pcm").write_bytes(b"\0" * 24)
    assert tree_bytes(tmp_path) == 1024


class FakeCommands:
    """Answers the monitor's commands with the samples; network counters grow 1 MB a call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, args: Sequence[str]) -> str | None:
        joined = " ".join(args)
        self.calls.append(joined)
        if args[0] == "vm_stat":
            return VM_STAT
        if args[0] == "sysctl":
            return SYSCTL
        if "IOAccelerator" in args:
            return IOACCELERATOR
        if "AppleSmartBattery" in args:
            return BATTERY_ON_AC
        if args[0] == "netstat":
            n = sum(c.startswith("netstat") for c in self.calls)
            grown = 299489668312 + n * 1_000_000
            return NETSTAT.replace("299489668312", str(grown))
        return None


def test_monitor_rates_and_caching(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system, "IS_MAC", True)  # the command parts, on any platform
    now = [1000.0]
    commands = FakeCommands()
    monitor = ResourceMonitor(tmp_path, clock=lambda: now[0], run=commands)

    first = monitor.snapshot()
    for name, keys in SECTIONS.items():
        assert tuple(first[name]) == keys
    assert first["network"] == {"rx_kbps": None, "tx_kbps": None}
    assert first["memory"]["pressure"] == "warn"
    assert first["gpu"]["utilization_percent"] == 33
    assert first["power"]["adapter_watts"] == 94

    now[0] += 2.0
    second = monitor.snapshot()
    assert second["network"]["rx_kbps"] == pytest.approx(1_000_000 * 8 / 1000 / 2.0)
    assert second["network"]["tx_kbps"] == 0.0
    # The battery is read again only after POWER_TTL_S.
    assert sum("AppleSmartBattery" in c for c in commands.calls) == 1
    now[0] += system.POWER_TTL_S
    monitor.snapshot()
    assert sum("AppleSmartBattery" in c for c in commands.calls) == 2


def test_monitor_survives_missing_commands(tmp_path: Path) -> None:
    snap = ResourceMonitor(tmp_path, run=lambda args: None).snapshot()
    assert snap["gpu"] == dict.fromkeys(SECTIONS["gpu"])
    assert snap["power"]["on_ac"] is None
    assert snap["disk"]["free_gb"] > 0
    assert snap["disk"]["db_mb"] is None
    json.dumps(snap)


@pytest.mark.skipif(sys.platform != "darwin", reason="reads macOS counters")
def test_smoke_on_this_mac(tmp_path: Path) -> None:
    (tmp_path / "wavescribe.db").write_bytes(b"\0" * 4096)
    (tmp_path / "audio" / "s1").mkdir(parents=True)
    (tmp_path / "audio" / "s1" / "1.pcm").write_bytes(b"\0" * 32000)
    child = subprocess.Popen([sys.executable, "-c", "while True: pass"])  # a busy child
    try:
        monitor = ResourceMonitor(tmp_path)
        pids = {"busy": child.pid, "gone": 2**22 + 12345}
        monitor.snapshot(pids)
        time.sleep(1.2)  # host CPU ticks are published about once a second
        snap = monitor.snapshot(pids)
        deadline = time.monotonic() + 5
        while snap["disk"]["audio_mb"] is None and time.monotonic() < deadline:
            time.sleep(0.05)  # the archive is walked in the background
            snap = monitor.snapshot(pids)
    finally:
        child.kill()
        child.wait()
    json.dumps(snap)
    assert snap["cost_ms"] < 1000

    proc = snap["process"]
    assert 0 < proc["rss_mb"] <= proc["peak_rss_mb"] * 1.5
    assert 0 < proc["footprint_mb"] <= proc["peak_footprint_mb"]
    assert proc["cpu_percent"] >= 0
    assert proc["threads"] >= 1
    busy = snap["children"]["busy"]
    assert busy["pid"] == child.pid
    assert 30 < busy["cpu_percent"] < 150  # one spinning core
    assert busy["rss_mb"] > 1
    assert snap["children"]["gone"] == {"pid": 2**22 + 12345, "cpu_percent": None, "rss_mb": None}

    mem = snap["memory"]
    assert mem["total_gb"] > 1
    assert 0 < mem["used_gb"] < mem["total_gb"]
    assert mem["used_gb"] + mem["available_gb"] == pytest.approx(mem["total_gb"], abs=0.02)
    assert 0 <= mem["compressed_gb"] < mem["total_gb"]
    assert mem["swap_used_gb"] >= 0
    assert mem["pressure"] in ("normal", "warn", "critical")

    cpu = snap["cpu"]
    assert cpu["cores"] >= 1
    assert cpu["load_1m"] >= 0
    assert 0 <= cpu["total_percent"] <= 100

    gpu = snap["gpu"]
    if gpu["utilization_percent"] is not None:  # Apple Silicon
        assert 0 <= gpu["utilization_percent"] <= 100
        assert gpu["in_use_memory_gb"] >= 0

    power = snap["power"]
    if power["on_ac"] is not None:  # a laptop
        assert 0 <= power["battery_percent"] <= 100
        assert isinstance(power["charging"], bool)
        assert -200 < power["battery_watts"] < 200

    disk = snap["disk"]
    assert 0 < disk["free_gb"] <= disk["total_gb"]
    assert 0 <= disk["free_percent"] <= 100
    assert disk["db_mb"] == pytest.approx(4096 / 1024**2, abs=0.1)
    assert disk["audio_mb"] == pytest.approx(32000 / 1024**2, abs=0.1)

    net = snap["network"]
    if net["rx_kbps"] is not None:
        assert net["rx_kbps"] >= 0
        assert net["tx_kbps"] >= 0
