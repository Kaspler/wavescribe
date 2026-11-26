# SPDX-License-Identifier: Apache-2.0
"""Model downloads started from the page: progress for the page.

Each download runs ``wavescribe models fetch <id>`` as a child process: the same licence check
and the rights holder's own repository at the pinned revision, and the only place the network
is used. The server process itself stays offline (``HF_HUB_OFFLINE=1``). Progress is the bytes
in the model's target folder, partial files included.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wavescribe.log import event
from wavescribe.registry import ModelEntry, ModelRegistry

log = logging.getLogger(__name__)


def _hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return home / "hub"


def target_dir(registry: ModelRegistry, entry: ModelEntry) -> Path:
    """Where a download's bytes land (to measure progress)."""
    if entry.kind == "hf":
        return _hf_cache() / ("models--" + entry.location.replace("/", "--"))
    return registry.models_dir / entry.id


def dir_bytes(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            p = Path(root) / f
            if p.is_symlink():  # HF snapshots link to blobs: count the blobs only
                continue
            with contextlib.suppress(OSError):
                total += p.stat().st_size
    return total


class Downloads:
    def __init__(
        self, registry: ModelRegistry, on_done: Callable[[str], None] | None = None
    ) -> None:
        self.registry = registry
        self.on_done = on_done  # called with the model id after a download succeeds
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}

    def start(self, model_id: str) -> dict[str, Any]:
        entry = self.registry.get(model_id)
        self.registry.check_license(entry)  # fail now, on the page, not in the thread
        with self._lock:
            job = self._jobs.get(model_id)
            if job is not None and job["state"] == "running":
                return dict(job)
            target = target_dir(self.registry, entry)
            job = {"model": model_id, "state": "running", "started": time.time(),
                   "error": None, "base_bytes": dir_bytes(target),
                   "total_bytes": int(entry.size_gb * 1e9) if entry.size_gb else None}  # fmt: skip
            self._jobs[model_id] = job
        threading.Thread(target=self._run, args=(entry, job), daemon=True,
                         name=f"fetch-{model_id}").start()  # fmt: skip
        event(log, "model_fetch", model=model_id, source=entry.source)
        return dict(job)

    def _run(self, entry: ModelEntry, job: dict[str, Any]) -> None:
        env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
        cmd = [sys.executable, "-m", "wavescribe", "models", "fetch", entry.id]
        try:
            done = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=6 * 3600)
            if done.returncode == 0:
                job["state"] = "done"
            else:
                lines = (done.stderr or done.stdout or "").strip().splitlines()
                job["state"], job["error"] = "failed", (lines[-1] if lines else "failed")[:300]
        except (OSError, subprocess.SubprocessError) as e:
            job["state"], job["error"] = "failed", str(e)[:300]
        if job["state"] == "failed":
            log.warning("download of %s failed: %s", entry.id, job["error"])
        job["finished"] = time.time()
        event(log, "model_fetched", model=entry.id, state=job["state"], error=job["error"])
        if job["state"] == "done" and self.on_done is not None:
            try:
                self.on_done(entry.id)
            except Exception:
                log.exception("applying the downloaded %s failed", entry.id)

    def status(self) -> dict[str, dict[str, Any]]:
        out = {}
        with self._lock:
            jobs = {k: dict(v) for k, v in self._jobs.items()}
        for model_id, job in jobs.items():
            if job["state"] == "running":
                entry = self.registry.get(model_id)
                done = max(0, dir_bytes(target_dir(self.registry, entry)) - job["base_bytes"])
                job["bytes"] = done
                total = job.get("total_bytes")
                job["percent"] = min(99.0, round(100 * done / total, 1)) if total else None
            out[model_id] = job
        return out
