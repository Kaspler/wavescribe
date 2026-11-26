# SPDX-License-Identifier: Apache-2.0
"""What this machine has heard: every signal a scan found, by frequency, with its RDS name when
it sent one. Built only from your own receptions, so it describes your location and needs no
outside data. Kept in ``data/radio/heard.json``; the last scans' spectra in ``data/radio/scans/``.

For private channels (walkie-talkies and anything no band plan lists) only the fact that a
channel was busy is kept, never what was said.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from wavescribe.radio.scan import Signal, Sweep

DEFAULT_DIR = Path("data/radio")
KEEP_SCANS = 20


class Catalog:
    def __init__(self, root: Path = DEFAULT_DIR) -> None:
        self.root = root
        self.path = root / "heard.json"
        self._lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {"version": 1, "entries": {}}
        data.setdefault("entries", {})
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    def entries(self) -> list[dict[str, Any]]:
        with self._lock:
            data = self._read()
        return sorted(data["entries"].values(), key=lambda e: e["freq_hz"])

    def record(
        self,
        signals: list[Signal],
        *,
        serial: str,
        start_hz: float,
        stop_hz: float,
        sweep: Sweep | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Merge a scan's findings. Entries inside the scanned range that weren't heard this time
        are kept, marked with when they were last heard."""
        now = now or time.time()
        with self._lock:
            data = self._read()
            entries: dict[str, Any] = data["entries"]
            for s in signals:
                if s.spur or s.privacy == "digital":
                    continue
                key = str(s.freq_hz)
                e = entries.get(key) or {"freq_hz": s.freq_hz, "first_heard": now, "times": 0}
                e.update({
                    "mhz": round(s.mhz, 5), "band": s.band, "label": s.label, "mode": s.mode,
                    "privacy": s.privacy, "level_db": s.level_db, "snr_db": s.snr_db,
                    "last_heard": now, "times": e.get("times", 0) + 1, "serial": serial,
                    "seen": s.seen, "passes": s.passes,
                })  # fmt: skip
                if s.rds and s.privacy == "broadcast":
                    rds = {k: v for k, v in s.rds.items() if v not in (None, [], "")}
                    e["rds"] = {**e.get("rds", {}), **rds}
                    if rds.get("ps"):
                        e["name"] = rds["ps"]
                entries[key] = e
            heard = {str(s.freq_hz) for s in signals}
            for key, e in entries.items():
                if start_hz <= e["freq_hz"] <= stop_hz:
                    e["missing"] = key not in heard
            data["last_scan"] = {"t": now, "serial": serial, "start_hz": start_hz,
                                 "stop_hz": stop_hz, "found": len(signals)}  # fmt: skip
            self._write(data)
        if sweep is not None:
            self._save_scan(now, serial, sweep, signals)
        return data["last_scan"]

    def name(self, freq_hz: int, name: str | None) -> None:
        """A name the user gives a frequency (overrides RDS)."""
        with self._lock:
            data = self._read()
            e = data["entries"].get(str(freq_hz))
            if e is None:
                raise KeyError(freq_hz)
            if name:
                e["user_name"] = name
            else:
                e.pop("user_name", None)
            self._write(data)

    def forget(self, freq_hz: int | None = None) -> None:
        with self._lock:
            data = self._read()
            if freq_hz is None:
                data["entries"] = {}
            else:
                data["entries"].pop(str(freq_hz), None)
            self._write(data)

    def _save_scan(self, now: float, serial: str, sweep: Sweep, signals: list[Signal]) -> None:
        d = self.root / "scans"
        d.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{serial}.json"
        (d / name).write_text(json.dumps({
            "t": now, "serial": serial, "seconds": round(sweep.seconds, 1),
            "spectrum": sweep.downsampled(), "signals": [s.to_dict() for s in signals],
        }, ensure_ascii=False), "utf-8")  # fmt: skip
        for old in sorted(d.glob("*.json"))[:-KEEP_SCANS]:
            old.unlink(missing_ok=True)

    # -- each receiver's own signals (learnt with no antenna) -------------------------------

    def spurs(self, serial: str) -> list[int]:
        try:
            data = json.loads((self.root / "spurs.json").read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        return [int(f) for f in (data.get(serial) or {}).get("freqs_hz", [])]

    def spur_info(self) -> dict[str, Any]:
        try:
            data = json.loads((self.root / "spurs.json").read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return {k: {"t": v.get("t"), "count": len(v.get("freqs_hz", []))} for k, v in data.items()}

    def save_spurs(self, serial: str, freqs_hz: list[int], start_hz: float, stop_hz: float,
                   now: float | None = None) -> None:  # fmt: skip
        path = self.root / "spurs.json"
        with self._lock:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}
            data[serial] = {"t": now or time.time(), "start_hz": start_hz, "stop_hz": stop_hz,
                            "freqs_hz": sorted(int(f) for f in freqs_hz)}  # fmt: skip
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1), "utf-8")
            os.replace(tmp, path)

    def forget_spurs(self, serial: str) -> None:
        path = self.root / "spurs.json"
        with self._lock:
            try:
                data = json.loads(path.read_text("utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return
            data.pop(serial, None)
            path.write_text(json.dumps(data, indent=1), "utf-8")

    def last_scan(self) -> dict[str, Any] | None:
        d = self.root / "scans"
        files = sorted(d.glob("*.json")) if d.exists() else []
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text("utf-8"))
        except json.JSONDecodeError:
            return None
