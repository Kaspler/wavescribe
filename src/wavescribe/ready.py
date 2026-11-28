# SPDX-License-Identifier: Apache-2.0
"""What a program that starts ``wavescribe serve`` reads to find it, and how it follows it.

**The ready file** (``serve --ready-file PATH``, by default ``data/serve.json`` in the home) is
written once the page's socket listens, before the models may have loaded, and removed when
``serve`` stops. It is written whole or not at all (a temporary file, then a rename), readable
by its owner only, and holds ``fields``: the address, the process and the home. ``GET
/api/hello`` gives the same with how things stand now.

**The parent** (``serve --parent-pid PID``): when that process ends, ``serve`` stops as it
does on SIGTERM, and exits 0. On macOS the system says when it ends (kqueue's ``NOTE_EXIT``);
elsewhere it is looked for once a second.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import select
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from wavescribe import __version__
from wavescribe.events import iso
from wavescribe.web.ports import loopback_url

log = logging.getLogger(__name__)

DEFAULT_PATH = Path("data") / "serve.json"
POLL_S = 1.0  # where the system can't say when a process ends: how often to look


def fields(*, host: str, port: int, started: float, home: Path, pid: int | None = None
           ) -> dict[str, Any]:  # fmt: skip
    """The ready file's fields, in its order: ``url`` is this machine's loopback address, also
    under ``--lan``."""
    return {
        "app": "wavescribe",
        "version": __version__,
        "pid": os.getpid() if pid is None else pid,
        "host": host,
        "port": port,
        "url": loopback_url(host, port),
        "home": str(Path(home).resolve()),
        "started": iso(started),
    }


def write(path: Path, data: dict[str, Any]) -> None:
    """Writes ``data`` to ``path`` whole: a temporary file beside it, mode 0600, then renamed
    over it, so a reader finds the old file, the new one or none, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # whatever the umask
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def remove(path: Path, pid: int | None = None) -> None:
    """Removes the ready file when it is still this process's (``pid``): another ``serve``
    given the same path since has its own there."""
    pid = os.getpid() if pid is None else pid
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("pid") == pid:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


# -- the parent ------------------------------------------------------------------------------


def _kqueue_event(pid: int) -> Any:
    return select.kevent(pid, filter=select.KQ_FILTER_PROC,  # type: ignore[attr-defined]
                         flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,  # type: ignore[attr-defined]
                         fflags=select.KQ_NOTE_EXIT)  # type: ignore[attr-defined]  # fmt: skip


def has_kqueue() -> bool:
    return hasattr(select, "kqueue") and hasattr(select, "KQ_NOTE_EXIT")


def alive(pid: int) -> bool:
    """Whether process ``pid`` runs. One that ended and wasn't reaped yet (a zombie) doesn't:
    kqueue refuses to watch it, and elsewhere /proc says so where there is one."""
    if has_kqueue():
        kq = select.kqueue()  # type: ignore[attr-defined]
        try:
            kq.control([_kqueue_event(pid)], 0)
        except OSError as e:
            return e.errno != errno.ESRCH
        finally:
            kq.close()
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # another user's: it exists
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return True
    return stat.rsplit(")", 1)[-1].split()[:1] != ["Z"]


def watch_parent(pid: int, on_exit: Callable[[], None], *, poll_s: float = POLL_S
                 ) -> threading.Thread:  # fmt: skip
    """Calls ``on_exit`` once, from a thread of its own, when process ``pid`` ends (at once
    when it has ended already)."""

    def by_kqueue() -> None:
        kq = select.kqueue()  # type: ignore[attr-defined]
        try:
            try:
                kq.control([_kqueue_event(pid)], 0)
            except OSError as e:
                if e.errno != errno.ESRCH:
                    raise
                return  # gone already
            while not kq.control(None, 1, None):
                pass
        finally:
            kq.close()

    def by_polling() -> None:
        while alive(pid):
            time.sleep(poll_s)

    def run() -> None:
        try:
            (by_kqueue if has_kqueue() else by_polling)()
        except OSError:
            log.exception("watching process %s: looking once a second instead", pid)
            by_polling()
        log.info("process %s ended: stopping", pid)
        on_exit()

    thread = threading.Thread(target=run, daemon=True, name=f"parent-{pid}")
    thread.start()
    return thread
