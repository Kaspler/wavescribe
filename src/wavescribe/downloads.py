# SPDX-License-Identifier: Apache-2.0
"""Model downloads started from the page: progress for the page.

Each download runs ``wavescribe models fetch <id>`` as a child process: the same licence check
and the rights holder's own repository at the pinned revision, and the only place the network
is used. The server process itself stays offline (``HF_HUB_OFFLINE=1``). Progress is the bytes
in the model's target folder, partial files included; a part-file an earlier download left,
which this one continues, counts as done from the start. Stopping ``serve`` stops the child
with SIGTERM, which leaves the same part-files behind as Ctrl-C on the command line.
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
from typing import Any

from wavescribe.log import event
from wavescribe.modelfetch import RETRY_ON_PAGE, dir_bytes, kept_bytes, target_dir
from wavescribe.registry import RETRY, ModelEntry, ModelRegistry

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 6 * 3600
STOP_WAIT_S = 5.0  # after asking a download to stop, before it is killed
ERROR_MAX_CHARS = 500  # the longest sentence `models fetch` ends with is under 400


def fetch_command(model_id: str) -> list[str]:
    return [sys.executable, "-m", "wavescribe", "models", "fetch", model_id]


def _why(output: str) -> str:
    """Why a download failed, for the page: the sentence `models fetch` ended with, where
    "run the same command again" becomes what the page offers."""
    lines = output.strip().splitlines()
    line = (lines[-1] if lines else "failed").strip().removeprefix("wavescribe: ")
    return line.replace(RETRY, RETRY_ON_PAGE)[:ERROR_MAX_CHARS]


class Downloads:
    def __init__(
        self, registry: ModelRegistry, on_done: Callable[[str], None] | None = None
    ) -> None:
        self.registry = registry
        self.on_done = on_done  # called with the model id after a download succeeds
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._closed = False

    def start(self, model_id: str) -> dict[str, Any]:
        entry = self.registry.get(model_id)
        self.registry.check_license(entry)  # fail now, on the page, not in the thread
        with self._lock:
            job = self._jobs.get(model_id)
            if job is not None and job["state"] == "running":
                return dict(job)
            target = target_dir(self.registry, entry)
            base = dir_bytes(target) - kept_bytes(target)  # what is continued counts as done
            job = {"model": model_id, "state": "running", "started": time.time(),
                   "error": None, "base_bytes": base,
                   "total_bytes": int(entry.size_gb * 1e9) if entry.size_gb else None}  # fmt: skip
            self._jobs[model_id] = job
        threading.Thread(target=self._run, args=(entry, job), daemon=True,
                         name=f"fetch-{model_id}").start()  # fmt: skip
        event(log, "model_fetch", model=model_id, source=entry.source)
        return dict(job)

    def _run(self, entry: ModelEntry, job: dict[str, Any]) -> None:
        env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
        try:
            with self._lock:  # close() stops every child it can see; none may start after it
                if self._closed:
                    raise OSError("Wavescribe stopped before the download began")
                proc = subprocess.Popen(fetch_command(entry.id), env=env, text=True,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)  # fmt: skip
                self._procs[entry.id] = proc
            try:
                out, err = proc.communicate(timeout=FETCH_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
            finally:
                with self._lock:
                    self._procs.pop(entry.id, None)
            if proc.returncode == 0:
                job["state"] = "done"
            elif self._closed:
                job["state"], job["error"] = "failed", "stopped when Wavescribe stopped"
            else:
                job["state"], job["error"] = "failed", _why(err or out or "")
        except (OSError, subprocess.SubprocessError) as e:
            job["state"], job["error"] = "failed", str(e)[:300]
        if job["state"] == "failed":
            log.warning("download of %s failed: %s", entry.id, job["error"])
        job["finished"] = time.time()
        event(log, "model_fetched", model=entry.id, state=job["state"], error=job["error"])
        if job["state"] == "done" and self.on_done is not None and not self._closed:
            try:
                self.on_done(entry.id)
            except Exception:
                log.exception("applying the downloaded %s failed", entry.id)

    def close(self) -> None:
        """Stop the downloads still running (``App.close``). Each is a child process, which
        would otherwise go on downloading after ``serve`` has stopped."""
        with self._lock:
            self._closed = True
            procs = list(self._procs.values())
        for proc in procs:
            with contextlib.suppress(OSError):
                proc.terminate()
        deadline = time.monotonic() + STOP_WAIT_S
        for proc in procs:
            try:
                proc.wait(max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()

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
