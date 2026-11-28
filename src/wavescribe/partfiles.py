# SPDX-License-Identifier: Apache-2.0
"""Part-files: a model file on its way, which the next download continues.

The downloads Wavescribe makes itself (a model from a URL with a checksum, and the large files of
a model from Hugging Face) write to ``<name>.part`` next to where the file goes, and ask only for
what the part-file lacks (an HTTP range request). The file takes its place once the checksum of
the whole of it matches. A download that stops or breaks off leaves its part-file for the next
one: on the command line, on the page, or after a restart.

A download holds a lock on its part-file while it writes (``held``), so a second download of the
same file is refused instead of writing into it, and cleaning up (``remove``) never takes a
part-file from under a download.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import BinaryIO

SUFFIX = ".part"
KEEP_S = 7 * 86400.0  # a part-file nobody has written to for a week is removed


class Busy(RuntimeError):
    """Another download is writing the part-file."""


def _lock(f: BinaryIO) -> bool:
    """Lock an open part-file for this process; False when another holds it."""
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


@contextlib.contextmanager
def held(part: Path) -> Iterator[BinaryIO]:
    """The part-file, open for appending and locked against every other download until the
    block ends (the lock goes with the process, however it ends). Raises ``Busy``."""
    while True:
        f = part.open("ab")
        if not _lock(f):
            f.close()
            raise Busy(part)
        try:  # removed between the open and the lock: lock the file now at that path instead
            if os.path.samestat(os.fstat(f.fileno()), part.stat()):
                break
        except FileNotFoundError:
            pass
        f.close()
    try:
        yield f
    finally:
        f.close()


def found(folder: Path) -> list[Path]:
    """The part-files of a model: next to a model from a URL, and among the blobs of one from
    Hugging Face."""
    return sorted(folder.glob("*" + SUFFIX)) + sorted((folder / "blobs").glob("*" + SUFFIX))


def size(parts: Iterable[Path]) -> int:
    total = 0
    for part in parts:
        with contextlib.suppress(OSError):
            total += part.stat().st_size
    return total


def remove(parts: Iterable[Path], *, older_than_s: float = 0.0) -> int:
    """Delete the part-files that no download is writing, and that nobody has written to for
    ``older_than_s``. Returns the bytes that freed."""
    freed = 0
    now = time.time()
    for part in parts:
        with contextlib.suppress(OSError), part.open("rb") as f:
            stat = os.fstat(f.fileno())
            if now - stat.st_mtime >= older_than_s and _lock(f):
                part.unlink()  # under the lock: a download that opens it now starts a new one
                freed += stat.st_size
    return freed


def checksum(path: Path) -> str:
    """The sha256 of a file, as hex."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()
