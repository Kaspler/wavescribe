# SPDX-License-Identifier: Apache-2.0
"""Model downloads started from the page: progress for the page, and a stop.

Each download runs ``wavescribe models fetch <id>`` as a child process: the same licence check
and the rights holder's own repository at the pinned revision, and the only place the network
is used. The server process itself stays offline (``HF_HUB_OFFLINE=1``). Progress is the bytes
in the model's target folder, partial files included; a part-file an earlier download left,
which this one continues, counts as done from the start.

Stopping a download (``stop``, the page's Stop) and stopping ``serve`` both end the child with
SIGTERM, which leaves the same files behind as Ctrl-C on the command line: Wavescribe's own
part-files, which the next download continues and checks against their sha256, stay; the
library's, which nothing continues (each is named after the process that wrote it), go. A child
that hasn't ended STOP_WAIT_S later is killed, and then its library part-files are removed here,
except those that were there before it began (another download's, perhaps).
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wavescribe.log import event
from wavescribe.modelfetch import (
    RETRY_ON_PAGE,
    dir_bytes,
    kept_bytes,
    library_parts,
    no_room_on_page,
    remove_parts,
    stopped_on_page,
    target_dir,
)
from wavescribe.registry import RETRY, ModelEntry, ModelError, ModelRegistry

log = logging.getLogger(__name__)

FETCH_TIMEOUT_S = 6 * 3600
STOP_WAIT_S = 5.0  # after asking a download to stop, before it is killed
ERROR_MAX_CHARS = 500  # the longest sentence `models fetch` ends with is under 400
SPEED_WINDOW_S = 20.0  # a download's speed is what arrived over about this long
SPEED_SPAN_S = 2.0  # and is said once two looks this far apart have measured it


def fetch_command(model_id: str) -> list[str]:
    return [sys.executable, "-m", "wavescribe", "models", "fetch", model_id]


def _why(output: str) -> str:
    """Why a download failed, for the page: the sentence `models fetch` ended with, where
    "run the same command again" becomes what the page offers."""
    lines = output.strip().splitlines()
    line = (lines[-1] if lines else "failed").strip().removeprefix("wavescribe: ")
    return line.replace(RETRY, RETRY_ON_PAGE)[:ERROR_MAX_CHARS]


def _end(procs: list[subprocess.Popen[str]]) -> None:
    """SIGTERM to each child, and SIGKILL to those still running STOP_WAIT_S later."""
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
            proc.wait()


class Downloads:
    def __init__(
        self, registry: ModelRegistry, on_done: Callable[[str], None] | None = None
    ) -> None:
        self.registry = registry
        self.on_done = on_done  # called with the model id after a download succeeds
        self._lock = threading.Lock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._threads: dict[str, threading.Thread] = {}
        # The library's part-files in each download's folder when it began: not its own.
        self._before: dict[str, frozenset[Path]] = {}
        # (when, bytes) each time a running download was measured, for its speed.
        self._samples: dict[str, deque[tuple[float, int]]] = {}
        self.clock: Callable[[], float] = time.monotonic
        self._closed = False

    def start(self, model_id: str) -> dict[str, Any]:
        """Start downloading a model, after its licence and the room on its disk are settled:
        a refusal is a ``ModelError`` with the sentence, before a byte is fetched."""
        entry = self.registry.get(model_id)
        self.registry.check_license(entry)  # fail now, on the page, not in the thread
        with self._lock:
            job = self._jobs.get(model_id)
            if job is not None and job["state"] in ("running", "stopping"):
                return dict(job)
        refusal = no_room_on_page(self.registry, entry)
        if refusal:
            raise ModelError(refusal)
        with self._lock:
            job = self._jobs.get(model_id)
            if job is not None and job["state"] in ("running", "stopping"):
                return dict(job)
            self._samples[model_id] = deque()
            target = target_dir(self.registry, entry)
            base = dir_bytes(target) - kept_bytes(target)  # what is continued counts as done
            job = {"model": model_id, "state": "running", "started": time.time(),
                   "error": None, "note": None, "base_bytes": base,
                   "total_bytes": int(entry.size_gb * 1e9) if entry.size_gb else None}  # fmt: skip
            self._jobs[model_id] = job
            self._before[model_id] = library_parts(target)
            thread = threading.Thread(target=self._run, args=(entry, job), daemon=True,
                                      name=f"fetch-{model_id}")  # fmt: skip
            self._threads[model_id] = thread
        thread.start()
        event(log, "model_fetch", model=model_id, source=entry.source)
        return dict(job)

    def _run(self, entry: ModelEntry, job: dict[str, Any]) -> None:
        env = {k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"}
        proc = None
        try:
            with self._lock:  # close() and stop() end every child they can see: none after
                if self._closed:
                    raise OSError("Wavescribe stopped before the download began")
                if job["state"] == "stopping":  # stopped before it began
                    raise _StoppedFirst
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
            elif job["state"] == "stopping":
                self._stopped(entry, job)
            else:
                job["state"], job["error"] = "failed", _why(err or out or "")
        except _StoppedFirst:
            self._stopped(entry, job)
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

    def _stopped(self, entry: ModelEntry, job: dict[str, Any]) -> None:
        """After the child ended on a stop: the library's part-files it left go (it removes
        them itself on SIGTERM, but not when it had to be killed); Wavescribe's own stay."""
        folder = target_dir(self.registry, entry)
        remove_parts(folder, keep=self._before.get(entry.id, frozenset()))
        job["state"], job["note"] = "stopped", stopped_on_page(folder)

    def stop(self, model_id: str) -> dict[str, Any]:
        """Stop a download that is running, as Ctrl-C stops `models fetch`, and return how it
        ended: "stopped", or "done" when it finished first. A download that has ended already
        is returned as it ended."""
        with self._lock:
            job = self._jobs.get(model_id)
            if job is None:
                raise ModelError(f"{model_id} isn't being downloaded: there is nothing to stop")
            if job["state"] == "running":
                job["state"] = "stopping"
            proc = self._procs.get(model_id)
            thread = self._threads.get(model_id)
        if proc is not None:
            _end([proc])
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_WAIT_S + 5)
        event(log, "model_fetch_stopped", model=model_id, state=job["state"])
        return self.status().get(model_id, dict(job))

    def forget(self, model_id: str) -> None:
        """Drop a download that has ended from the list (its model was removed since)."""
        with self._lock:
            job = self._jobs.get(model_id)
            if job is not None and job["state"] not in ("running", "stopping"):
                del self._jobs[model_id]

    def close(self) -> None:
        """Stop the downloads still running (``App.close``). Each is a child process, which
        would otherwise go on downloading after ``serve`` has stopped."""
        with self._lock:
            self._closed = True
            procs = list(self._procs.values())
        _end(procs)

    def status(self) -> dict[str, dict[str, Any]]:
        """Every download since the start, by model id. A running one has its ``bytes`` so
        far, its ``percent``, and from the second look on, SPEED_SPAN_S or more apart, its
        ``speed`` (bytes a second over the last SPEED_WINDOW_S) and ``eta_s``, the seconds
        left at that speed (None while nothing arrives, or with no size known)."""
        out = {}
        with self._lock:
            jobs = {k: dict(v) for k, v in self._jobs.items()}
        for model_id, job in jobs.items():
            if job["state"] in ("running", "stopping"):
                entry = self.registry.get(model_id)
                done = max(0, dir_bytes(target_dir(self.registry, entry)) - job["base_bytes"])
                job["bytes"] = done
                total = job.get("total_bytes")
                job["percent"] = min(99.0, round(100 * done / total, 1)) if total else None
                job["speed"] = speed = self._speed(model_id, done)
                left = max(0, total - done) if total else None
                job["eta_s"] = round(left / speed) if left is not None and speed else None
            out[model_id] = job
        return out

    def _speed(self, model_id: str, done: int) -> int | None:
        """Bytes a second over the last SPEED_WINDOW_S of looks at a download, or None until
        two looks SPEED_SPAN_S apart have measured it."""
        now = self.clock()
        with self._lock:
            samples = self._samples.setdefault(model_id, deque())
            samples.append((now, done))
            while len(samples) > 2 and now - samples[1][0] >= SPEED_WINDOW_S:
                samples.popleft()
            then, before = samples[0]
        if now - then < SPEED_SPAN_S:
            return None
        return max(0, round((done - before) / (now - then)))


class _StoppedFirst(Exception):
    """A download stopped before its child process began."""
