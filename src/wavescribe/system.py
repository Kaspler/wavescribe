# SPDX-License-Identifier: Apache-2.0
"""Machine resources for the status panel: memory, CPU, GPU, power, disk and network.

Wavescribe runs 24/7 on a battery, so the page should show at a glance whether memory, disk or
power are running out. Everything here works without sudo and without extra dependencies. On
macOS it reads what Activity Monitor reads: a few libSystem calls through ctypes (host CPU
ticks, per-process usage) and the text output of ``sysctl``, ``vm_stat``, ``ioreg`` and
``netstat``. Elsewhere, what can't be read is None.

``ResourceMonitor.snapshot()`` is meant to be called every few seconds from one background
thread. One costs 30-50 ms on the M1 Max, two thirds of it in ``ioreg`` (2026-11-26). Rates
(CPU percent, network) are averages since the previous snapshot, so the first one has None
there. Two parts are cached: the battery (its gauge only refreshes about once a minute) and the
audio archive size (walking it stats every file). Sizes are binary: 1 GB = 1024³ bytes.

Per-process network rates are left out: ``nettop`` costs ~30 ms of CPU per call (5 s without
``-n``, which resolves host names), and it only counts sockets that are still open, so every
reconnect resets the count.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import logging
import os
import re
import resource
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"
POWER_TTL_S = 10.0  # the battery gauge refreshes about once a minute; a plug change shows in 10 s
SIZES_TTL_S = 60.0
COMMAND_TIMEOUT_S = 2.0
GB = 1024**3
MB = 1024**2

# Every section always has these keys, so the page can rely on them; values are None if unknown.
SECTIONS: dict[str, tuple[str, ...]] = {
    "process": (
        "rss_mb",
        "peak_rss_mb",
        "footprint_mb",
        "peak_footprint_mb",
        "cpu_percent",
        "threads",
    ),
    "memory": ("total_gb", "used_gb", "available_gb", "compressed_gb", "swap_used_gb", "pressure"),
    "cpu": ("cores", "load_1m", "load_5m", "total_percent"),
    "gpu": ("utilization_percent", "in_use_memory_gb", "allocated_memory_gb"),
    "power": (
        "on_ac",
        "battery_percent",
        "charging",
        "minutes_remaining",
        "battery_watts",
        "system_watts",
        "input_watts",
        "adapter_watts",
    ),
    "disk": ("total_gb", "free_gb", "free_percent", "db_mb", "audio_mb"),
    "network": ("rx_kbps", "tx_kbps"),
}
CHILD_KEYS = ("pid", "cpu_percent", "rss_mb")


# -- commands -------------------------------------------------------------------------------------


@functools.cache
def _which(name: str) -> str | None:
    # A launchd job's PATH may lack /usr/sbin, where sysctl, ioreg and netstat live.
    return shutil.which(name) or shutil.which(name, path="/usr/bin:/usr/sbin:/bin:/sbin")


def run_command(args: Sequence[str], timeout: float = COMMAND_TIMEOUT_S) -> str | None:
    """Run a command and return its stdout, or None if it is missing, fails or hangs."""
    exe = _which(args[0])
    if exe is None:
        return None
    try:
        # An absolute path and close_fds=False let CPython 3.12 posix_spawn instead of fork:
        # 1.5 ms instead of 3.7 ms per command in a 6 GB process. Our fds aren't inheritable.
        done = subprocess.run(
            [exe, *args[1:]],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=False,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # sysctl exits non-zero if one name is unknown but still prints the others.
    return done.stdout or None


# -- parsers (pure, so tests can feed sample output) ----------------------------------------------


def parse_vm_stat(text: str) -> dict[str, int]:
    """``vm_stat`` output as {lower-case name: pages}, plus "page size" in bytes."""
    out: dict[str, int] = {}
    if m := re.search(r"page size of (\d+) bytes", text):
        out["page size"] = int(m[1])
    for name, value in re.findall(r'^"?([^":\n]+)"?:\s+(\d+)\.?\s*$', text, re.M):
        out[name.strip().lower()] = int(value)
    return out


def memory_from_vm_stat(pages: Mapping[str, int], total_bytes: int) -> dict[str, float]:
    """Used, available and compressed GB, the way Activity Monitor counts them.

    used = app memory (anonymous minus purgeable) + wired + compressed, i.e. Activity Monitor's
    "Memory Used". available = total - used: free pages, the file cache and purgeable memory,
    which macOS hands out without compressing or swapping anything. The common "free + inactive
    + speculative + purgeable" counts inactive anonymous pages too, which can only be reclaimed
    by compressing or swapping them: 28.6 GB against 19.9 GB on a 64 GB Mac (2026-11-26).
    """
    try:
        page = pages["page size"]
        anonymous = pages["anonymous pages"]
        purgeable = pages["pages purgeable"]
        wired = pages["pages wired down"]
        compressed = pages["pages occupied by compressor"]
    except KeyError:
        return {}
    used = (anonymous - purgeable + wired + compressed) * page
    return {
        "used_gb": used / GB,
        "available_gb": max(0, total_bytes - used) / GB,
        "compressed_gb": compressed * page / GB,
    }


def parse_sysctl(text: str) -> dict[str, str]:
    """``sysctl name ...`` output as {name: value}."""
    return dict(
        (k.strip(), v.strip())
        for k, _, v in (line.partition(": ") for line in text.splitlines())
        if v
    )


_UNITS = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_swap_used(value: str) -> int | None:
    """Bytes of swap in use, from ``vm.swapusage`` ("total = 2048.00M  used = 1255.25M ...")."""
    m = re.search(r"used = ([\d.]+)([KMGT])", value)
    return int(float(m[1]) * _UNITS[m[2]]) if m else None


# kern.memorystatus_vm_pressure_level reports dispatch's memory-pressure bits.
PRESSURE = {1: "normal", 2: "warn", 4: "critical"}


def parse_pressure(value: str | None) -> str | None:
    try:
        return PRESSURE.get(int(value or ""))
    except ValueError:
        return None


def parse_gpu(text: str) -> dict[str, float]:
    """GPU busy % and memory from ``ioreg -r -d 1 -c IOAccelerator`` (Apple Silicon keys).

    The busy % is the driver's latest figure, not an average since the last snapshot, so short
    bursts (an ASR call) are sampled, not summed. "In use" is what the GPU holds right now;
    "allocated" covers every process's GPU-visible memory. With ``serve`` idle and its MLX models
    loaded (11 GB footprint), in use read 0.1-0.5 GB and allocated 16.7 GB (2026-11-26).
    """
    m = re.search(r'"PerformanceStatistics" = \{(.*)\}', text)
    if not m:
        return {}
    stats = {k: int(v) for k, v in re.findall(r'"([^"]+)"=(\d+)', m[1])}
    out: dict[str, float] = {}
    if "Device Utilization %" in stats:
        out["utilization_percent"] = stats["Device Utilization %"]
    if "In use system memory" in stats:
        out["in_use_memory_gb"] = stats["In use system memory"] / GB
    if "Alloc system memory" in stats:
        out["allocated_memory_gb"] = stats["Alloc system memory"] / GB
    return out


_PROP = re.compile(r'^[\s|]*"([^"]+)" = (.*)$', re.M)


def _ioreg_int(value: str | None) -> int | None:
    """An ioreg integer. ioreg prints negative numbers as unsigned 64-bit ones."""
    if value is None or not value.strip().isdigit():
        return None
    n = int(value)
    return n - 2**64 if n >= 2**63 else n


def _nested_int(value: str | None, key: str) -> int | None:
    m = re.search(rf'"{re.escape(key)}"=(\d+)', value or "")
    return _ioreg_int(m[1]) if m else None


def parse_battery(text: str) -> dict[str, Any]:
    """Power state from ``ioreg -r -d 1 -c AppleSmartBattery``; {} without a battery.

    battery_watts is Voltage times Amperage, negative while discharging. system_watts is
    PowerTelemetryData's SystemLoad: what the machine draws, from the adapter or the battery.
    input_watts is SystemPowerIn: what comes in from the adapter (the load plus charging).
    adapter_watts is the charger's rating.
    """
    props = dict(_PROP.findall(text))
    if "ExternalConnected" not in props:
        return {}
    on_ac = props["ExternalConnected"] == "Yes"
    charging = props.get("IsCharging") == "Yes"
    out: dict[str, Any] = {"on_ac": on_ac, "charging": charging}

    current = _ioreg_int(props.get("CurrentCapacity"))
    maximum = _ioreg_int(props.get("MaxCapacity"))  # 100 on Apple Silicon, mAh on Intel
    if current is not None and maximum:
        out["battery_percent"] = round(100 * current / maximum)

    # Minutes to empty while discharging, to full while charging; 65535 means "not known yet".
    minutes = _ioreg_int(props.get("AvgTimeToFull" if charging else "AvgTimeToEmpty"))
    if minutes is not None and minutes < 65535 and (charging or not on_ac):
        out["minutes_remaining"] = minutes

    millivolts = _ioreg_int(props.get("Voltage"))
    milliamps = _ioreg_int(props.get("Amperage"))
    if millivolts is not None and milliamps is not None:
        out["battery_watts"] = millivolts * milliamps / 1e6

    telemetry = props.get("PowerTelemetryData")
    load = _nested_int(telemetry, "SystemLoad")
    if load:
        out["system_watts"] = load / 1000
    elif not on_ac and out.get("battery_watts", 0) < 0:
        out["system_watts"] = -out["battery_watts"]
    power_in = _nested_int(telemetry, "SystemPowerIn")
    if power_in is not None:
        out["input_watts"] = power_in / 1000
    if on_ac:
        out["adapter_watts"] = _nested_int(props.get("AdapterDetails"), "Watts")
    return out


def parse_netstat(text: str) -> dict[str, tuple[int, int]]:
    """Link-level byte counters from ``netstat -ibn`` as {interface: (rx, tx)}."""
    out: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        f = line.split()
        # Name Mtu Network [Address] Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll; Address may be
        # blank (loopback, tunnels), so count from the right.
        if len(f) >= 10 and f[2].startswith("<Link#"):
            try:
                out[f[0].rstrip("*")] = (int(f[-5]), int(f[-2]))
            except ValueError:
                continue
    return out


def physical(interface: str) -> bool:
    """Ethernet and Wi-Fi (en0, en1 ...). VPN tunnels (utun) and bridges carry the same bytes
    again, and the default route may well be a tunnel, so only these are counted."""
    return re.fullmatch(r"en\d+", interface) is not None


def cpu_busy_percent(prev: Sequence[int], cur: Sequence[int]) -> float | None:
    """Whole-machine CPU busy % between two (user, system, idle, nice) tick samples."""
    deltas = [(c - p) % 2**32 for p, c in zip(prev, cur, strict=True)]  # 32-bit counters wrap
    total = sum(deltas)
    return 100 * (total - deltas[2]) / total if total else None


def tree_bytes(root: Path) -> int | None:
    """Total size of the files under ``root``, or None if it isn't a directory."""
    if not root.is_dir():
        return None
    total = 0
    stack = [str(root)]
    while stack:
        try:
            entries = os.scandir(stack.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    pass  # pruned while we walked
    return total


# -- libSystem calls (macOS) ----------------------------------------------------------------------


def _c_fields(ctype: Any, names: str) -> list[tuple[str, Any]]:
    return [(name, ctype) for name in names.split()]


class _RusageInfoV4(ctypes.Structure):
    # struct rusage_info_v4 from <sys/resource.h>
    _fields_ = [
        ("uuid", ctypes.c_uint8 * 16),
        *_c_fields(
            ctypes.c_uint64,
            "user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size "
            "resident_size phys_footprint proc_start_abstime proc_exit_abstime "
            "child_user_time child_system_time child_pkg_idle_wkups child_interrupt_wkups "
            "child_pageins child_elapsed_abstime diskio_bytesread diskio_byteswritten "
            "cpu_time_qos_default cpu_time_qos_maintenance cpu_time_qos_background "
            "cpu_time_qos_utility cpu_time_qos_legacy cpu_time_qos_user_initiated "
            "cpu_time_qos_user_interactive billed_system_time serviced_system_time "
            "logical_writes lifetime_max_phys_footprint instructions cycles billed_energy "
            "serviced_energy interval_max_phys_footprint runnable_time",
        ),
    ]


class _ProcTaskInfo(ctypes.Structure):
    # struct proc_taskinfo from <sys/proc_info.h>
    _fields_ = [
        *_c_fields(
            ctypes.c_uint64,
            "virtual_size resident_size total_user total_system threads_user threads_system",
        ),
        *_c_fields(
            ctypes.c_int32,
            "policy faults pageins cow_faults messages_sent messages_received syscalls_mach "
            "syscalls_unix csw threadnum numrunning priority",
        ),
    ]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_RUSAGE_INFO_V4 = 4
_PROC_PIDTASKINFO = 4
_HOST_CPU_LOAD_INFO = 3


class _LibSystem:
    """The few kernel counters with no cheap command: CPU ticks and per-process usage.

    Per-process calls work for our own processes (ffmpeg, rtl_sdr children) without sudo.
    """

    def __init__(self) -> None:
        lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        lib.mach_host_self.restype = ctypes.c_uint32
        lib.host_statistics.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        lib.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
        lib.proc_pidinfo.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        timebase = _Timebase()
        lib.mach_timebase_info(ctypes.byref(timebase))
        # Process CPU times come in Mach ticks: 1 ns on Intel, 125/3 ns on Apple Silicon.
        self._seconds_per_tick = timebase.numer / timebase.denom / 1e9
        self._host = lib.mach_host_self()  # taken once: each call adds a port reference
        self._lib = lib

    def cpu_ticks(self) -> tuple[int, int, int, int] | None:
        ticks = (ctypes.c_uint32 * 4)()
        count = ctypes.c_uint32(4)
        if self._lib.host_statistics(self._host, _HOST_CPU_LOAD_INFO, ticks, ctypes.byref(count)):
            return None
        return (ticks[0], ticks[1], ticks[2], ticks[3])

    def process(self, pid: int) -> dict[str, float] | None:
        """CPU seconds, resident and footprint bytes of one of our processes, or None."""
        info = _RusageInfoV4()
        if self._lib.proc_pid_rusage(pid, _RUSAGE_INFO_V4, ctypes.byref(info)) != 0:
            return None
        return {
            "cpu_s": (info.user_time + info.system_time) * self._seconds_per_tick,
            "rss": info.resident_size,
            "footprint": info.phys_footprint,
            "peak_footprint": info.lifetime_max_phys_footprint,
        }

    def threads(self, pid: int) -> int | None:
        info = _ProcTaskInfo()
        size = ctypes.sizeof(info)
        if self._lib.proc_pidinfo(pid, _PROC_PIDTASKINFO, 0, ctypes.byref(info), size) != size:
            return None
        return info.threadnum


@functools.cache
def _libsystem() -> _LibSystem | None:
    if not IS_MAC:
        return None
    try:
        return _LibSystem()
    except (OSError, AttributeError):
        log.warning("libSystem calls unavailable; CPU and process figures will be missing")
        return None


# -- the monitor ----------------------------------------------------------------------------------


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _section(name: str, values: Mapping[str, Any]) -> dict[str, Any]:
    return {k: values.get(k) for k in SECTIONS[name]}


class ResourceMonitor:
    """Takes cheap snapshots of this machine's resources for the status panel."""

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        run: Callable[[Sequence[str]], str | None] = run_command,
    ) -> None:
        self.data_dir = Path(data_dir)
        self._clock = clock
        self._run = run
        self._lock = threading.Lock()
        self._total_memory: int | None = None
        with contextlib.suppress(OSError, ValueError):
            self._total_memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        self._prev_self: tuple[float, float] | None = None  # (clock, CPU seconds)
        self._prev_children: dict[str, tuple[int, float, float]] = {}  # label: (pid, clock, s)
        self._prev_ticks: tuple[int, int, int, int] | None = None
        self._cpu_total: float | None = None
        self._prev_net: tuple[float, dict[str, tuple[int, int]]] | None = None
        self._power: tuple[float, dict[str, Any]] | None = None
        self._audio: tuple[float, int | None] | None = None  # (clock, bytes)
        self._walking = False
        self._failed: set[str] = set()

    def snapshot(self, pids: Mapping[str, int] | None = None) -> dict[str, Any]:
        """Current resources as a JSON-serialisable dict; ``pids`` maps labels to child pids.

        Keys: time, process, children (label: pid, cpu_percent, rss_mb), memory, cpu, gpu,
        power, disk, network, and cost_ms (how long this snapshot took).
        """
        with self._lock:
            start = time.perf_counter()
            now = self._clock()
            snap: dict[str, Any] = {"time": round(time.time(), 3)}
            for name, collect in (
                ("process", self._process),
                ("memory", self._memory),
                ("cpu", self._cpu),
                ("gpu", self._gpu),
                ("power", self._power_state),
                ("disk", self._disk),
                ("network", self._network),
            ):
                snap[name] = _section(name, self._guard(name, collect, now))
            snap["children"] = self._guard("children", self._children, now, pids or {})
            snap["cost_ms"] = round((time.perf_counter() - start) * 1000, 1)
            return snap

    def _guard(self, name: str, collect: Callable[..., dict[str, Any]], *args: Any) -> dict:
        try:
            return collect(*args)
        except Exception:
            if name not in self._failed:  # once, not every few seconds for days
                self._failed.add(name)
                log.warning("resource monitor: reading %s failed", name, exc_info=True)
            return {}

    # -- sections ---------------------------------------------------------------------------------

    def _process(self, now: float) -> dict[str, Any]:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_s = usage.ru_utime + usage.ru_stime
        out: dict[str, Any] = {
            # ru_maxrss is in bytes on macOS and in KiB on Linux.
            "peak_rss_mb": _round(usage.ru_maxrss / (MB if IS_MAC else 1024), 1),
            "cpu_percent": self._rate(self._prev_self, now, cpu_s),
        }
        self._prev_self = (now, cpu_s)
        lib = _libsystem()
        if lib is not None:
            pid = os.getpid()
            if info := lib.process(pid):
                out["rss_mb"] = _round(info["rss"] / MB, 1)
                out["footprint_mb"] = _round(info["footprint"] / MB, 1)
                out["peak_footprint_mb"] = _round(info["peak_footprint"] / MB, 1)
            out["threads"] = lib.threads(pid)
        return out

    @staticmethod
    def _rate(prev: tuple[float, float] | None, now: float, cpu_s: float) -> float | None:
        """Percent of one core since ``prev`` (like top: 250 means two and a half cores)."""
        if prev is None or now <= prev[0]:
            return None
        return round(max(0.0, 100 * (cpu_s - prev[1]) / (now - prev[0])), 1)

    def _children(self, now: float, pids: Mapping[str, int]) -> dict[str, Any]:
        lib = _libsystem()
        out: dict[str, Any] = {}
        current: dict[str, tuple[int, float, float]] = {}
        for label, pid in pids.items():
            values: dict[str, Any] = {"pid": pid}
            info = lib.process(pid) if lib is not None else None
            if info is not None:
                prev = self._prev_children.get(label)
                # A restarted child has a new pid: no rate until its second snapshot.
                if prev is not None and prev[0] == pid:
                    values["cpu_percent"] = self._rate(prev[1:], now, info["cpu_s"])
                values["rss_mb"] = _round(info["rss"] / MB, 1)
                current[label] = (pid, now, info["cpu_s"])
            out[label] = {k: values.get(k) for k in CHILD_KEYS}
        self._prev_children = current
        return out

    def _memory(self, now: float) -> dict[str, Any]:
        total = self._total_memory
        out: dict[str, Any] = {"total_gb": _round(total / GB, 2) if total else None}
        if not IS_MAC:
            return out
        if total and (text := self._run(["vm_stat"])):
            for key, value in memory_from_vm_stat(parse_vm_stat(text), total).items():
                out[key] = round(value, 2)
        text = self._run(["sysctl", "vm.swapusage", "kern.memorystatus_vm_pressure_level"])
        values = parse_sysctl(text or "")
        swap = parse_swap_used(values.get("vm.swapusage", ""))
        out["swap_used_gb"] = _round(swap / GB if swap is not None else None, 2)
        out["pressure"] = parse_pressure(values.get("kern.memorystatus_vm_pressure_level"))
        return out

    def _cpu(self, now: float) -> dict[str, Any]:
        out: dict[str, Any] = {"cores": os.cpu_count()}
        with contextlib.suppress(OSError):
            out["load_1m"], out["load_5m"], _ = (round(x, 2) for x in os.getloadavg())
        lib = _libsystem()
        ticks = lib.cpu_ticks() if lib is not None else None
        if ticks is not None:
            busy = cpu_busy_percent(self._prev_ticks, ticks) if self._prev_ticks else None
            # The kernel publishes ticks about once a second: with none new, keep the baseline
            # and repeat the last figure.
            if busy is not None or self._prev_ticks is None:
                self._prev_ticks = ticks
                self._cpu_total = _round(busy, 1)
            out["total_percent"] = self._cpu_total
        return out

    def _gpu(self, now: float) -> dict[str, Any]:
        if not IS_MAC:
            return {}
        text = self._run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"])
        stats = parse_gpu(text or "")
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in stats.items()}

    def _power_state(self, now: float) -> dict[str, Any]:
        if not IS_MAC:
            return {}
        if self._power is None or now - self._power[0] >= POWER_TTL_S:
            text = self._run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "AppleSmartBattery"])
            state = parse_battery(text or "")
            for key in ("battery_watts", "system_watts", "input_watts"):
                state[key] = _round(state.get(key), 2)
            self._power = (now, state)
        return self._power[1]

    def _disk(self, now: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        path = self.data_dir
        while not path.exists() and path != path.parent:
            path = path.parent  # not created yet: report the filesystem it will be on
        usage = shutil.disk_usage(path)
        out["total_gb"] = round(usage.total / GB, 2)
        out["free_gb"] = round(usage.free / GB, 2)
        out["free_percent"] = round(100 * usage.free / usage.total, 1) if usage.total else None
        db = self.data_dir / "wavescribe.db"
        if db.exists():
            size = 0
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    size += os.stat(f"{db}{suffix}").st_size
            out["db_mb"] = round(size / MB, 1)
        out["audio_mb"] = self._audio_mb(now)
        return out

    def _audio_mb(self, now: float) -> float | None:
        """The archive size from the last walk; a stale one starts a new walk in the background.

        A walk stats every file: 55 ms for 20k files on the M1 Max, and the default 2 GB cap
        holds ~70k, so it never runs inside a snapshot. The first snapshots have None.
        """
        cached = self._audio
        if (cached is None or now - cached[0] >= SIZES_TTL_S) and not self._walking:
            self._walking = True
            threading.Thread(
                target=self._walk_audio, args=(now,), name="audio-size", daemon=True
            ).start()
        if cached is None or cached[1] is None:
            return None
        return round(cached[1] / MB, 1)

    def _walk_audio(self, started: float) -> None:
        try:
            self._audio = (started, tree_bytes(self.data_dir / "audio"))
        finally:
            self._walking = False

    def _network(self, now: float) -> dict[str, Any]:
        if not IS_MAC:
            return {}
        counters = parse_netstat(self._run(["netstat", "-ibn"]) or "")
        counters = {k: v for k, v in counters.items() if physical(k)}
        out: dict[str, Any] = {}
        if counters and self._prev_net is not None and now > self._prev_net[0]:
            then, before = self._prev_net
            rx = tx = 0
            for name, (r, t) in counters.items():
                if name in before:
                    # An interface that went down and up again restarts from zero.
                    rx += max(0, r - before[name][0])
                    tx += max(0, t - before[name][1])
            out["rx_kbps"] = round(rx * 8 / 1000 / (now - then), 1)
            out["tx_kbps"] = round(tx * 8 / 1000 / (now - then), 1)
        self._prev_net = (now, counters) if counters else None
        return out
