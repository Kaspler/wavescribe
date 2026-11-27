# SPDX-License-Identifier: Apache-2.0
"""This machine's control key: how a command on the Wavescribe machine reaches the page's
controls when nothing else tells its requests from another machine's.

Behind a proxy (``serve --behind-proxy``) every request counts as another machine's, even one
from this machine's own address: a proxy on the same machine connects from there too, and not
every proxy adds a header that says so. A request from this machine's address that also
carries the key counts as this machine's again. Only someone who can read the file has the
key, the same protection the settings file and its feed tokens have. Commands send it to a
server on this machine only, never to another address.
"""

from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

KEY_HEADER = "X-Wavescribe-Key"
KEY_FILE = "control.key"


def load_key(data_dir: Path, *, create: bool = False) -> str | None:
    """The key kept in ``data_dir``, or None when there is none yet. With ``create`` (``serve``
    at start), one is made when there is none, readable by the user only; every server started
    from the same folder shares it."""
    path = data_dir / KEY_FILE
    try:
        key = path.read_text("ascii").strip()
    except FileNotFoundError:
        key = ""
    except (OSError, UnicodeDecodeError):
        key = ""  # unreadable or not ours: made again below, when allowed
    if key or not create:
        if key and create:
            os.chmod(path, 0o600)  # copied in with looser rights: only the user reads it
        return key or None
    data_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix=".control-", suffix=".key")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(key + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return key
