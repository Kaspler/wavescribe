# SPDX-License-Identifier: Apache-2.0
"""What ``wavescribe models fetch`` does around each download.

Before anything is downloaded it says how much there is and checks that the disk has room. Each
download runs in a thread of its own, so Ctrl-C stops the command at once instead of when the
current file ends. A download that fails is one sentence: what failed, that nothing is broken,
and that the same command again downloads only what is missing.

What "continues" means: a model is one file or several, and a file that finished is kept. A
file that was cut off is downloaded again from its start: the Hugging Face library keeps no
part of it, and a model from a plain URL is one small file.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import logging
import os
import shutil
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from wavescribe.registry import PART_SUFFIX, RETRY, ModelEntry, ModelError, ModelRegistry

GB = 1e9  # sizes are decimal, as models.toml's size_gb and Finder are

# The wording a failed or stopped download ends with, in one place. The page shows the same
# sentences with RETRY_ON_PAGE where the command line says RETRY (downloads.py).
KEPT = "nothing is broken and the files that finished are kept"
AGAIN = f"{RETRY} and it downloads only what is missing"
RETRY_ON_PAGE = "start the download again"

PROGRESS_EVERY_S = 15.0  # between progress lines when the output isn't a terminal
STALE_PART_S = 3600.0  # a part-file nobody has written to for this long belongs to no download
_POLL_S = 0.2  # how often the waiting main thread looks up: Ctrl-C is answered within this

_exit: Callable[[int], NoReturn] = os._exit  # tests replace it


def size_text(n: float) -> str:
    """Bytes as a person reads them: 7.9 GB, 340 MB."""
    if n >= 0.95 * GB:
        return f"{n / GB:.1f} GB"
    return f"{round(n / 1e6)} MB" if n >= 0.5e6 else "under 1 MB"


# -- Where a model's bytes land, and how many are there ----------------------------------------


def _hf_cache() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return home / "hub"


def target_dir(registry: ModelRegistry, entry: ModelEntry) -> Path:
    """Where a download's bytes land (to measure progress and free space)."""
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


def _parts(folder: Path) -> list[Path]:
    """The part-files of downloads in progress. The Hugging Face library writes each file as
    ``blobs/<name>.incomplete`` and renames it when it is whole; a model from a URL is a
    ``.part`` file next to where it will be."""
    blobs = folder / "blobs"
    hub = sorted(blobs.glob("*.incomplete")) if blobs.is_dir() else []
    return hub + (sorted(folder.glob("*" + PART_SUFFIX)) if folder.is_dir() else [])


def remove_parts(
    folder: Path, *, older_than_s: float = 0.0, keep: frozenset[Path] = frozenset()
) -> int:
    """Delete part-files, and say how many bytes that freed. Nothing ever reuses one: a
    download that was killed leaves its part behind for good, up to the size of the model.
    ``keep`` names the ones to leave alone (they were there before this download began, so
    they may be another download's)."""
    freed = 0
    now = time.time()
    for part in _parts(folder):
        if part in keep:
            continue
        with contextlib.suppress(OSError):
            stat = part.stat()
            if now - stat.st_mtime >= older_than_s:
                part.unlink()
                freed += stat.st_size
    return freed


def _existing(path: Path) -> Path:
    path = path.absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def free_bytes(path: Path) -> int:
    """Free space on the disk that holds ``path`` (which needn't exist yet)."""
    return shutil.disk_usage(_existing(path)).free


# -- What there is to download -----------------------------------------------------------------


@dataclass
class Item:
    entry: ModelEntry
    folder: Path  # where its bytes land
    path: Path | None  # where the model is, when it is installed already
    left: int | None  # bytes still to download; None when the registry gives no size

    @property
    def id(self) -> str:
        return self.entry.id

    @property
    def installed(self) -> bool:
        return self.path is not None


def survey(registry: ModelRegistry, entries: list[ModelEntry]) -> list[Item]:
    """What is installed and how much is left to download. Part-files that a killed download
    left behind long ago are removed on the way (a recent one may be another download at work:
    the page's, say), so they neither count as progress nor take up the room."""
    items = []
    for entry in entries:
        folder = target_dir(registry, entry)
        path = registry.local_path(entry)
        left: int | None = 0
        if path is None and entry.kind != "local":
            remove_parts(folder, older_than_s=STALE_PART_S)
            size = int(entry.size_gb * GB) if entry.size_gb else None
            left = None if size is None else max(0, size - dir_bytes(folder))
        items.append(Item(entry, folder, path, left))
    return items


def no_room(items: list[Item]) -> str | None:
    """One sentence when a disk clearly can't take what is about to be downloaded, else None."""
    needed: dict[int, tuple[int, Path]] = {}
    for item in items:
        if item.installed or not item.left:
            continue
        place = _existing(item.folder)
        device = place.stat().st_dev
        needed[device] = (needed.get(device, (0, place))[0] + item.left, place)
    for need, place in needed.values():
        free = free_bytes(place)
        if free < need:
            return (
                f"not enough room on the disk: {size_text(need)} to download and "
                f"{size_text(free)} free at {place}; nothing was downloaded: make "
                f"{size_text(need - free)} of room, then {RETRY}."
            )
    return None


# -- A failed download, as one sentence ----------------------------------------------------------


def _chain(exc: BaseException) -> list[BaseException]:
    """The error and what caused it, outermost first."""
    seen: list[BaseException] = []
    todo: list[BaseException | None] = [exc]
    while todo:
        e = todo.pop(0)
        if e is None or any(e is s for s in seen):
            continue
        seen.append(e)
        reason = getattr(e, "reason", None)  # urllib's URLError wraps the real error here
        todo += [e.__cause__, e.__context__, reason if isinstance(reason, BaseException) else None]
    return seen


def _names(e: BaseException) -> set[str]:
    return {c.__name__ for c in type(e).__mro__}


def _plain(e: BaseException) -> str:
    """An error's own words on one line, without ``[Errno 61]`` and the like."""
    text = (e.strerror if isinstance(e, OSError) and e.strerror else str(e)) or type(e).__name__
    text = " ".join(text.split())
    if text.startswith("[") and "] " in text[:40]:
        text = text.split("] ", 1)[1]
    return text[:160].rstrip(" .")


def _host(entry: ModelEntry) -> str:
    if entry.kind == "hf":
        endpoint = os.environ.get("HF_ENDPOINT") or "https://huggingface.co"
        return urllib.parse.urlsplit(endpoint).hostname or "huggingface.co"
    return urllib.parse.urlsplit(entry.location).hostname or entry.location


# Class names, not classes: the Hugging Face library's HTTP client is its own dependency, and
# its errors are told apart here without importing it.
_NO_CONNECTION = {"ConnectError", "ConnectTimeout", "ProxyError", "ConnectionRefusedError",
                  "gaierror", "OfflineModeIsEnabled"}  # fmt: skip
_BROKE_OFF = {"ReadTimeout", "ReadError", "RemoteProtocolError", "IncompleteRead", "TimeoutError",
              "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "SSLError",
              "TimeoutException", "NetworkError"}  # fmt: skip


def _status(e: BaseException) -> int | None:
    """The HTTP status an error carries, whichever library raised it."""
    code = getattr(getattr(e, "response", None), "status_code", None)
    if code is None and isinstance(e, urllib.error.HTTPError):
        code = e.code
    return code if isinstance(code, int) else None


def explain(entry: ModelEntry, exc: BaseException, *, folder: Path | None = None) -> str:
    """Why a download failed and what to do, as one sentence (no traceback, no library text)."""
    chain = _chain(exc)
    host = _host(entry)
    head = f"couldn't download {entry.id}: "
    again = f"; {KEPT}: "

    cert = next((e for e in chain if isinstance(e, ssl.SSLCertVerificationError)), None)
    if cert is None:
        cert = next((e for e in chain if "CERTIFICATE_VERIFY_FAILED" in str(e)), None)
    if cert is not None:
        why = getattr(cert, "verify_message", None) or _plain(cert)
        return (
            f"{head}this computer doesn't trust the certificate that {host} presented ({why}); "
            "check the date and time, and if a proxy or a security program inspects HTTPS add "
            f"its certificate to the system keychain, then {AGAIN} ({KEPT})."
        )

    full = next((e for e in chain if isinstance(e, OSError) and e.errno == errno.ENOSPC), None)
    if full is not None:
        free = f" ({size_text(free_bytes(folder))} free)" if folder is not None else ""
        return f"{head}the disk is full{free}{again}make room, then {AGAIN}."

    status = next((code for e in chain if (code := _status(e)) is not None), None)
    if status is not None:
        answered = f"{head}{host} answered with an error (HTTP {status})"
        if status >= 500 or status in (408, 429):
            return f"{answered}{again}wait a few minutes, then {AGAIN}."
        return (
            f"{answered}: nothing on this computer is broken and the files that finished are "
            f"kept, but the same command will get the same answer until {host} offers "
            f"{entry.location} again"
            + (" or you log in there (`hf auth login`)" if status in (401, 403) else "")
            + "."
        )

    if isinstance(exc, ModelError):  # ours, and a sentence already
        return str(exc)

    for e in chain:
        if _names(e) & _NO_CONNECTION or type(e) is urllib.error.URLError:
            if any(isinstance(x, socket.gaierror) for x in chain):
                why = "its name couldn't be looked up, as when this computer is offline"
            elif any("Timeout" in type(x).__name__ for x in chain):
                why = "no answer in time"
            else:
                why = _plain(chain[-1])
            return f"{head}no connection to {host} ({why}){again}check the network, then {AGAIN}."
    for e in chain:
        if _names(e) & _BROKE_OFF or isinstance(e, http.client.HTTPException):
            why = _plain(chain[-1])
            if any("Timeout" in type(x).__name__ for x in chain):
                why = "no answer in time"
            elif "peer closed" in why or isinstance(e, http.client.IncompleteRead):
                why = "the other end closed it before the file was whole"
            return f"{head}the connection to {host} broke off ({why}){again}{AGAIN}."
    if "LocalEntryNotFoundError" in _names(exc):  # the library's word for "couldn't reach it"
        return f"{head}no connection to {host}{again}check the network, then {AGAIN}."
    inner = chain[-1]
    return f"{head}{type(inner).__name__}: {_plain(inner)}{again}{AGAIN}."


# -- Quiet where the library is loud ---------------------------------------------------------------


class _Calm(logging.Filter):
    """What the Hugging Face library says while it downloads, cut down to what helps.

    Dropped: the hub's "you are sending unauthenticated requests… set a HF_TOKEN" notice. The
    models in the registry are public and need no account, and the notice reads like a step
    someone has to take. Replaced by one plain line per model: the library's own account of
    each retry (the raw error and the address, up to five times over). The error itself is
    not lost: when the retries run out it is the sentence the command ends with. Every other
    warning and every error from the library still shows.
    """

    _RETRY = ("thrown while requesting", "Retrying in", "Trying to resume download",
              "Max retries exceeded")  # fmt: skip

    def __init__(self) -> None:
        super().__init__()
        self.said_retry = False

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        text = record.getMessage()
        if "unauthenticated" in text and "HF_TOKEN" in text:
            return False
        if any(phrase in text for phrase in self._RETRY):
            if not self.said_retry:
                self.said_retry = True
                print("  no proper answer from the server; trying again a few times", flush=True)
            return False
        return True


_calm = _Calm()
_LIBRARY_LOGGERS = ("huggingface_hub.utils._http", "huggingface_hub.file_download")


def calm_library(*, new_model: bool = False) -> None:
    """Put the filter on the library's loggers (once), and let the next model say again that
    it is retrying."""
    for name in _LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        if _calm not in logger.filters:
            logger.addFilter(_calm)
    if new_model:
        _calm.said_retry = False


def _plain_progress() -> bool:
    """Whether to print progress as plain lines. The library draws its bars on stderr, and only
    on a terminal; with either stream going elsewhere, lines that end in a newline are what
    stays readable (and the bars are switched off where they would be written to a file)."""
    if not sys.stderr.isatty():
        with contextlib.suppress(ImportError):
            from huggingface_hub.utils import disable_progress_bars

            disable_progress_bars()
    return not (sys.stdout.isatty() and sys.stderr.isatty())


# -- The downloads ---------------------------------------------------------------------------------


class _Progress:
    """Plain progress lines for an output that isn't a terminal (a log file, a pipe, the page's
    child process), where the library's redrawn bars are off: one line at most every
    PROGRESS_EVERY_S, and only when the figure has moved."""

    def __init__(self, item: Item, *, enabled: bool) -> None:
        self.item = item
        self.enabled = enabled
        self.start = dir_bytes(item.folder) if self.enabled else 0
        self.last_at = time.monotonic()
        self.last_said = ""

    def tick(self) -> None:
        if not self.enabled or time.monotonic() - self.last_at < PROGRESS_EVERY_S:
            return
        self.last_at = time.monotonic()
        done = max(0, dir_bytes(self.item.folder) - self.start)
        if done < 1e6:
            return
        if self.item.left:  # the registry gives the model's size
            done = min(done, self.item.left)
            percent = int(100 * done / self.item.left)
            line = f"  {size_text(done)} of {size_text(self.item.left)} ({percent}%)"
        else:
            line = f"  {size_text(done)} so far"
        if line != self.last_said:
            self.last_said = line
            print(line, flush=True)


def _stoppable(fn: Callable[[], Any], tick: Callable[[], None]) -> Any:
    """Run a download in a thread of its own and wait for it here.

    The library downloads a model's files in worker threads and waits for them, and a thread
    can't be interrupted: Ctrl-C took effect only when the file being downloaded was whole (110
    seconds for the largest model). The main thread waits here instead, in short steps, so
    KeyboardInterrupt (and the SystemExit that SIGTERM raises) reaches the caller at once.
    """
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    thread = threading.Thread(target=work, daemon=True, name="model-download")
    thread.start()
    while thread.is_alive():
        thread.join(_POLL_S)
        tick()
    if "error" in box:
        raise box["error"]
    return box["value"]


def _leave(code: int, current: Item, not_ours: frozenset[Path]) -> NoReturn:
    """Stop now. The download threads are still running and can't be told to stop, so the
    process ends without waiting for them; their part-files go first, because nothing would
    ever reuse or remove them."""
    remove_parts(current.folder, keep=not_ours)
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    _exit(code)


def _part_bytes(folder: Path) -> int:
    total = 0
    for part in _parts(folder):
        with contextlib.suppress(OSError):
            total += part.stat().st_size
    return total


def _stopped(items: list[Item], done: list[Item], current: Item) -> str:
    finished = ", ".join(i.id for i in items if i.installed or i in done) or "nothing"
    waiting = [i for i in items if not i.installed and i not in done]
    rest = []
    for item in waiting:
        note = ""
        if item is current:
            kept = dir_bytes(item.folder) - _part_bytes(item.folder)
            note = f" ({size_text(kept)} of it kept)" if kept >= 1e6 else ""
        rest.append(item.id + note)
    return (
        f"Stopped. Downloaded: {finished}. Not downloaded: {', '.join(rest)}. "
        f"{KEPT[0].upper()}{KEPT[1:]}: {AGAIN}."
    )


def run(registry: ModelRegistry, entries: list[ModelEntry]) -> int:
    """Download ``entries`` (their licences are settled already). 0 when all are installed."""
    items = survey(registry, entries)
    todo = [i for i in items if not i.installed]
    if todo:
        known = sum(i.left or 0 for i in todo)
        unknown = [i.id for i in todo if i.left is None]
        models = "1 model" if len(todo) == 1 else f"{len(todo)} models"
        size = f", {size_text(known)}" + (" in all" if len(todo) > 1 else "") if known else ""
        if unknown and known:
            size += f" plus {', '.join(unknown)} (size not known)"
        have = len(items) - len(todo)
        already = f" ({have} already installed)" if have else ""
        free = size_text(free_bytes(todo[0].folder))
        print(f"Downloading {models}{size}{already}; {free} free on the disk.", flush=True)
        refusal = no_room(items)
        if refusal:
            print(f"wavescribe: {refusal}", file=sys.stderr)
            return 1
    plain = _plain_progress()
    below_bars = "\n\n" if sys.stderr.isatty() else ""  # the library draws three, stacked
    done: list[Item] = []
    for item in items:
        entry = item.entry
        print(f"{entry.id}: {entry.license} ({entry.license_url})")
        if item.installed:
            print(f"  already installed: {item.path}", flush=True)
            continue
        source = f"  from {entry.source}" + (f" @ {entry.revision}" if entry.revision else "")
        print(source + (f", {size_text(item.left)}" if item.left else ""), flush=True)
        calm_library(new_model=True)
        not_ours = frozenset(_parts(item.folder))
        progress = _Progress(item, enabled=plain)
        try:
            path = _stoppable(lambda entry=entry: registry.fetch(entry), progress.tick)
        except KeyboardInterrupt:
            print(below_bars + _stopped(items, done, item), file=sys.stderr)
            _leave(130, item, not_ours)
        except SystemExit as e:  # SIGTERM: `serve` stopping the download it started
            _leave(e.code if isinstance(e.code, int) else 1, item, not_ours)
        except Exception as e:
            sentence = explain(entry, e, folder=item.folder)
            print(f"{below_bars}wavescribe: {sentence}", file=sys.stderr)
            return 1
        done.append(item)
        print(f"  -> {path}", flush=True)
    return 0
