# SPDX-License-Identifier: Apache-2.0
"""The command as it is typed in this install, for every line that says what to run.

A Wavescribe folder (a clone or the unzipped download) is run through uv: ``uv run wavescribe
serve``, as the README and the guide say, and a bare ``wavescribe`` there is "command not
found". Installed as a package with its script on the PATH, it is ``wavescribe serve``. Every
hint the program prints, logs or sends to the page builds its command with ``typed``.

How the folder is told apart (``detect``): this package is imported from
``<folder>/src/wavescribe``, and ``<folder>`` holds a ``pyproject.toml`` whose project is
``wavescribe`` and the ``uv.lock`` beside it. That is what ``uv sync`` installs, as a link back
to the folder; an installed package is imported from its environment's site-packages instead.
How it was started this time doesn't count: the background service runs the folder's Python
directly, and a person still types ``uv run wavescribe`` there. One case says ``wavescribe`` in
a folder: no ``uv`` on the PATH but a ``wavescribe`` script on it, as in a folder whose
environment someone made with pip and activated.
"""

from __future__ import annotations

import functools
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path

UV = "uv run wavescribe"
INSTALLED = "wavescribe"


def is_checkout(package_dir: Path) -> bool:
    """Whether ``package_dir`` (where this package was imported from) is the ``src/wavescribe``
    of a uv project whose project is wavescribe."""
    if package_dir.parent.name != "src":
        return False
    root = package_dir.parent.parent
    if not (root / "uv.lock").is_file():
        return False
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return (project.get("project") or {}).get("name") == "wavescribe"


def detect(package_dir: Path, which: Callable[[str], str | None] = shutil.which) -> str:
    """``uv run wavescribe`` or ``wavescribe``, for the package imported from ``package_dir``."""
    if not is_checkout(package_dir):
        return INSTALLED
    if which("uv") is None and which("wavescribe") is not None:
        return INSTALLED
    return UV


@functools.cache
def invocation() -> str:
    """How this install's command is typed: ``uv run wavescribe`` or ``wavescribe``."""
    return detect(Path(__file__).resolve().parent)


def typed(command: str = "") -> str:
    """A Wavescribe command as it is typed in this install: ``typed("models fetch yamnet")`` is
    ``uv run wavescribe models fetch yamnet`` in a Wavescribe folder."""
    return f"{invocation()} {command}".rstrip()
